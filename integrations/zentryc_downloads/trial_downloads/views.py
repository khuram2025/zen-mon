import os
import re
from datetime import timedelta
from urllib.parse import urlencode

from django.core.cache import cache
from django.db import transaction
from django.db.models import F
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from . import access, catalog, conf, emails
from .forms import TrialRequestForm, new_stamp
from .models import DownloadEvent, TrialLead

# A person who restarts a download within this time is not counted twice.
_RECOUNT_AFTER = timedelta(minutes=10)


def _context(title, description, **extra):
    context = {
        conf.get("META_TITLE_KEY"): title,
        conf.get("META_DESCRIPTION_KEY"): description,
        "support_email": conf.get("REPLY_TO"),
    }
    context.update(extra)
    return context


def _private(response):
    """Pages that carry a personal link: not cached, not indexed, no referrer."""
    response["Cache-Control"] = "no-store, private"
    response["X-Robots-Tag"] = "noindex, nofollow"
    response["Referrer-Policy"] = "no-referrer"
    return response


def _client_ip(request):
    value = (request.META.get(conf.get("IP_HEADER")) or "").split(",")[0].strip()
    return value or None


def _over_limit(kind, key, limit):
    """Count one attempt; True when this hour's allowance is used up."""
    cache_key = "trial-downloads:%s:%s" % (kind, key)
    if cache.add(cache_key, 1, 3600):
        return False
    try:
        return cache.incr(cache_key) > limit
    except ValueError:  # the entry expired between add and incr
        cache.set(cache_key, 1, 3600)
        return False


def _release_rows(release, lead_token=None):
    rows = []
    for artifact in release["artifacts"]:
        row = dict(artifact)
        row["size"] = catalog.human_size(artifact["size_bytes"])
        base = "/downloads/zenplus-appliance/%s/%s" % (release["version"], artifact["filename"])
        row["checksum_url"] = base + ".sha256"
        row["signature_url"] = base + ".sha256.sig"
        row["request_url"] = reverse("trial_downloads:request") + "?" + urlencode({"platform": artifact["key"]})
        if lead_token:
            row["download_url"] = reverse("trial_downloads:file", args=[lead_token, artifact["key"]])
        rows.append(row)
    return rows


@require_safe
def downloads_index(request):
    release = catalog.current_release()
    return render(
        request,
        "trial_downloads/downloads_index.html",
        _context(
            "Download ZenPlus: free 30-day trial | Zentryc",
            "Download the ZenPlus virtual appliance for VMware or Hyper-V and start a free 30-day trial "
            "on your own network. Windows agent downloads for existing deployments.",
            release=release,
            artifacts=_release_rows(release),
        ),
    )


@require_safe
def release_latest(request):
    return HttpResponseRedirect(reverse("trial_downloads:release", args=[catalog.current_release()["version"]]))


@require_safe
def release_page(request, version):
    release = catalog.get_release(version)
    if release is None:
        raise Http404("No such release")
    return render(
        request,
        "trial_downloads/release.html",
        _context(
            "ZenPlus %s virtual appliance | Zentryc" % version,
            "ZenPlus %s virtual appliance for VMware and Hyper-V: requirements, signed checksums, "
            "verification and first setup." % version,
            release=release,
            released_on=catalog.released_on(release),
            artifacts=_release_rows(release),
            verify_example=release["artifacts"][0]["filename"],
            public_key_url="/downloads/zenplus-appliance/%s/zentryc-release.pub" % version,
        ),
    )


@never_cache
@require_http_methods(["GET", "HEAD", "POST"])
def trial_request(request):
    release = catalog.current_release()
    if request.method == "POST":
        form = TrialRequestForm(request.POST)
        if form.looks_automated():
            # Answer exactly like a real request so a script learns nothing.
            return HttpResponseRedirect(reverse("trial_downloads:sent"))
        ip = _client_ip(request)
        if _over_limit("ip", ip or "unknown", conf.get("RATE_IP_PER_HOUR")):
            form.add_error(None, "Too many requests from your network in the last hour. Try again later or contact us.")
        elif form.is_valid():
            data = form.cleaned_data
            if _over_limit("email", data["email"], conf.get("RATE_EMAIL_PER_HOUR")):
                # The link was sent moments ago; do not send a stream of mail.
                return HttpResponseRedirect(reverse("trial_downloads:sent"))
            lead, sent = _store_and_send(request, data, ip)
            if sent:
                return HttpResponseRedirect(reverse("trial_downloads:sent"))
            form.add_error(
                None,
                "We could not send the email just now. Try again in a few minutes, or contact us "
                "and quote reference %s." % lead.reference,
            )
    else:
        source = "&".join(
            "%s=%s" % (key, request.GET[key][:60])
            for key in ("utm_source", "utm_medium", "utm_campaign", "platform")
            if request.GET.get(key)
        )
        referrer = request.META.get("HTTP_REFERER", "")
        if referrer:
            source = (source + " " if source else "") + "from " + referrer[:160]
        form = TrialRequestForm(initial={"opened": new_stamp(), "src": source[:300]})
    response = render(
        request,
        "trial_downloads/request.html",
        _context(
            "Start your ZenPlus trial | Zentryc",
            "Tell us where to send your ZenPlus trial download. The 30-day evaluation runs on your own "
            "network, with no payment card.",
            form=form,
            release=release,
            artifacts=_release_rows(release),
            link_hours=conf.get("VERIFY_LINK_HOURS"),
        ),
        status=200 if request.method != "POST" else 422,
    )
    response["X-Robots-Tag"] = "noindex, follow"
    return response


def _store_and_send(request, data, ip):
    fields = {name: data[name] for name in (
        "first_name", "last_name", "phone", "company", "job_title", "country",
        "device_count", "hypervisor", "current_tool", "marketing_opt_in",
    )}
    fields.update(
        email_domain=data["email"].rpartition("@")[2],
        accepted_terms_at=timezone.now(),
        terms_version=conf.get("TERMS_VERSION"),
        source=data.get("src", "")[:300],
        ip_address=ip,
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
    )
    with transaction.atomic():
        lead = TrialLead.objects.select_for_update().filter(email=data["email"]).first()
        if lead is None:
            lead = TrialLead(email=data["email"], **fields)
        else:
            # The same person asking again: keep the row, its reference and
            # its history, take the newer details and send a new link.
            for name, value in fields.items():
                setattr(lead, name, value)
        token = access.issue_link(lead)
        lead.save()
    return lead, emails.send_download_link(lead, access.access_url(lead, token))


@never_cache
@require_safe
def trial_sent(request):
    response = render(
        request,
        "trial_downloads/sent.html",
        _context(
            "Check your inbox | Zentryc",
            "We have emailed your ZenPlus trial download link.",
            link_hours=conf.get("VERIFY_LINK_HOURS"),
        ),
    )
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _unavailable(request, state):
    template = "trial_downloads/link_expired.html" if state in (access.EXPIRED, access.LIMIT) else None
    if template is None:
        raise Http404("Unknown download link")
    return _private(
        render(
            request,
            template,
            _context("Download link expired | Zentryc", "This ZenPlus download link no longer works.", state=state),
            status=410,
        )
    )


@require_safe
def trial_access(request, token):
    state, lead = access.resolve(token)
    if state in (access.INVALID, access.EXPIRED):
        return _unavailable(request, state)
    if state == access.FIRST_VISIT:
        emails.notify_sales(lead)
    release = catalog.current_release()
    return _private(
        render(
            request,
            "trial_downloads/access.html",
            _context(
                "Your ZenPlus download | Zentryc",
                "Your personal ZenPlus trial download page.",
                lead=lead,
                release=release,
                artifacts=_release_rows(release, token),
                verify_example=release["artifacts"][0]["filename"],
                public_key_url="/downloads/zenplus-appliance/%s/zentryc-release.pub" % release["version"],
                downloads_left=max(0, conf.get("MAX_DOWNLOADS") - lead.download_count),
            ),
        )
    )


_RANGE_FROM_START = re.compile(r"^bytes=0-")


@require_safe
def trial_file(request, token, key):
    state, lead = access.resolve(token)
    if state in (access.INVALID, access.EXPIRED):
        return _unavailable(request, state)
    release = catalog.current_release()
    artifact = catalog.get_artifact(release, key)
    if artifact is None:
        raise Http404("No such file")
    if state == access.FIRST_VISIT:
        emails.notify_sales(lead)

    # A browser resuming, or a download manager fetching in pieces, sends many
    # requests for one download. Only a request for the start of the file,
    # outside the recount window, is a new download.
    http_range = request.META.get("HTTP_RANGE", "")
    starts_file = request.method == "GET" and (not http_range or _RANGE_FROM_START.match(http_range))
    if starts_file:
        recent = lead.downloads.filter(
            artifact=key, version=release["version"], created_at__gte=timezone.now() - _RECOUNT_AFTER
        ).exists()
        if not recent:
            if lead.download_count >= conf.get("MAX_DOWNLOADS"):
                return _unavailable(request, access.LIMIT)
            DownloadEvent.objects.create(
                lead=lead,
                version=release["version"],
                artifact=key,
                filename=artifact["filename"],
                ip_address=_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
            )
            TrialLead.objects.filter(pk=lead.pk).update(
                download_count=F("download_count") + 1, last_download_at=timezone.now()
            )
            lead.advance_status("downloaded")
            lead.save(update_fields=["status", "updated_at"])

    relative = "%s/%s" % (release["version"], artifact["filename"])
    serve_root = conf.get("SERVE_ROOT")
    if serve_root:
        path = os.path.join(serve_root, release["version"], artifact["filename"])
        if not os.path.isfile(path):
            raise Http404("File not available")
        response = FileResponse(open(path, "rb"), as_attachment=True, filename=artifact["filename"])
    else:
        # nginx sends the file (ranges and resume included) from an internal
        # location that the public cannot request directly.
        response = HttpResponse(content_type="application/octet-stream")
        response["X-Accel-Redirect"] = conf.get("ACCEL_PREFIX") + relative
        response["Content-Disposition"] = 'attachment; filename="%s"' % artifact["filename"]
    return _private(response)
