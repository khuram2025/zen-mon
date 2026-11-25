import logging

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from . import catalog, conf

logger = logging.getLogger(__name__)


def send_download_link(lead, url):
    """Email the personal link. Returns False when the mail could not be handed over."""
    context = {
        "lead": lead,
        "url": url,
        "release": catalog.current_release(),
        "hours": conf.get("VERIFY_LINK_HOURS"),
        "days": conf.get("ACCESS_DAYS"),
        "site_url": conf.get("SITE_URL"),
    }
    message = EmailMultiAlternatives(
        subject="Your ZenPlus trial download",
        body=render_to_string("trial_downloads/email/download_link.txt", context),
        from_email=conf.from_email(),
        to=[lead.email],
        reply_to=[conf.get("REPLY_TO")] if conf.get("REPLY_TO") else None,
    )
    message.attach_alternative(render_to_string("trial_downloads/email/download_link.html", context), "text/html")
    try:
        message.send()
    except Exception:  # the visitor is told; the address is not written to the log
        logger.exception("trial download link could not be sent (request %s)", lead.reference)
        return False
    return True


def notify_sales(lead):
    """Tell sales once about a confirmed request. Never fails the visitor's page."""
    recipients = conf.get("NOTIFY")
    if not recipients or lead.sales_notified_at:
        return
    body = render_to_string("trial_downloads/email/sales_notification.txt", {"lead": lead, "site_url": conf.get("SITE_URL")})
    message = EmailMultiAlternatives(
        subject="ZenPlus trial request: %s, %s (%s)" % (lead.company, lead.get_device_count_display(), lead.reference),
        body=body,
        from_email=conf.from_email(),
        to=list(recipients),
        reply_to=[lead.email],
    )
    try:
        message.send()
    except Exception:
        logger.exception("sales notification failed (request %s)", lead.reference)
        return
    lead.sales_notified_at = timezone.now()
    lead.save(update_fields=["sales_notified_at", "updated_at"])
