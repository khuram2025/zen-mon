"""Personal download links: who may download, and until when."""
from datetime import timedelta

from django.core import signing
from django.core.exceptions import ValidationError
from django.utils import timezone

from . import conf
from .models import TrialLead, new_nonce

_SALT = "trial-downloads.access"

OK, FIRST_VISIT, EXPIRED, INVALID, LIMIT = "ok", "first", "expired", "invalid", "limit"


def issue_link(lead):
    """Cancel earlier links and start a new one. The caller saves the lead."""
    lead.access_nonce = new_nonce()
    lead.link_sent_at = timezone.now()
    lead.link_sent_count += 1
    lead.access_expires_at = None
    lead.download_count = 0
    return make_token(lead)


def make_token(lead):
    return signing.dumps({"l": str(lead.pk), "n": lead.access_nonce}, salt=_SALT, compress=True)


def access_url(lead, token=None):
    from django.urls import reverse

    return conf.get("SITE_URL") + reverse("trial_downloads:access", args=[token or make_token(lead)])


def resolve(token):
    """Return (state, lead). Opening a fresh link confirms the email address.

    The signature proves we issued the link; the nonce proves it is the latest
    one; the dates on the lead decide whether it still works. The token itself
    never expires, so its lifetime can be changed without re-sending mail.
    """
    try:
        payload = signing.loads(token, salt=_SALT)
        lead = TrialLead.objects.get(pk=payload["l"])
    except (signing.BadSignature, TrialLead.DoesNotExist, ValidationError, KeyError, TypeError, ValueError):
        return INVALID, None
    if payload.get("n") != lead.access_nonce or lead.link_sent_at is None:
        return INVALID, None
    now = timezone.now()
    if lead.access_expires_at is None:
        if now > lead.link_sent_at + timedelta(hours=conf.get("VERIFY_LINK_HOURS")):
            return EXPIRED, lead
        lead.access_expires_at = now + timedelta(days=conf.get("ACCESS_DAYS"))
        if lead.verified_at is None:
            lead.verified_at = now
        lead.advance_status("confirmed")
        lead.save(update_fields=["access_expires_at", "verified_at", "status", "updated_at"])
        return FIRST_VISIT, lead
    if now > lead.access_expires_at:
        return EXPIRED, lead
    return OK, lead
