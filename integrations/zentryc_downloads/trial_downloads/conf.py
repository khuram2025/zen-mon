"""Settings of the trial download flow: ``settings.TRIAL_DOWNLOADS`` over these defaults."""
from django.conf import settings

DEFAULTS = {
    # Absolute address used in emails. No trailing slash.
    "SITE_URL": "https://zentryc.com",
    # A link in the email must be opened within this many hours.
    "VERIFY_LINK_HOURS": 72,
    # Once opened, the personal download page works for this many days.
    "ACCESS_DAYS": 7,
    # File downloads one person may start in one access period (resumed
    # downloads are not counted again).
    "MAX_DOWNLOADS": 12,
    # Personal mailboxes (gmail.com, outlook.com ...) are refused unless True.
    "ALLOW_FREE_EMAIL": False,
    # Ask DNS whether the email domain can receive mail at all.
    "CHECK_MX": True,
    "MX_TIMEOUT": 3.0,
    # ISO 3166-1 alpha-2 codes that may not download (export control is the
    # owner's decision; empty means no country is refused).
    "BLOCKED_COUNTRIES": [],
    # Who is told about each confirmed request, e.g. ["sales@zentryc.com"].
    "NOTIFY": [],
    # None = settings.DEFAULT_FROM_EMAIL.
    "FROM_EMAIL": None,
    "REPLY_TO": "info@zentryc.com",
    # nginx internal location that holds the appliance images.
    "ACCEL_PREFIX": "/_protected/zenplus-appliance/",
    # Development and tests only: serve files from this directory with Django
    # instead of handing the request to nginx.
    "SERVE_ROOT": None,
    # Optional JSON file with the release catalogue (see catalog.py).
    "CATALOG_FILE": None,
    # Requests per hour.
    "RATE_IP_PER_HOUR": 6,
    "RATE_EMAIL_PER_HOUR": 3,
    # A form posted faster than this was not filled in by a person.
    "MIN_FILL_SECONDS": 3,
    # Recorded with each consent so it is known which text was accepted.
    "TERMS_VERSION": "2026-04-05",
    # request.META key that holds the visitor address. Use
    # "HTTP_CF_CONNECTING_IP" only when the origin accepts Cloudflare alone.
    "IP_HEADER": "REMOTE_ADDR",
    # Context names the site's base template reads for <title> and the meta
    # description.
    "META_TITLE_KEY": "page_title",
    "META_DESCRIPTION_KEY": "meta_description",
}


def get(name):
    configured = getattr(settings, "TRIAL_DOWNLOADS", None) or {}
    if name in configured:
        return configured[name]
    return DEFAULTS[name]


def from_email():
    return get("FROM_EMAIL") or settings.DEFAULT_FROM_EMAIL
