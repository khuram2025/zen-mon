"""Checks on what a visitor types into the trial request form.

Every refusal says what to do next: the form is a sales front door, and a
person turned away without a way forward is a lost evaluation.
"""
import os
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from . import conf

try:  # pragma: no cover - exercised through domain_accepts_mail
    import dns.exception
    import dns.resolver
except ImportError:  # pragma: no cover
    dns = None

try:
    import phonenumbers
except ImportError:  # pragma: no cover
    phonenumbers = None

_DATA = os.path.join(os.path.dirname(__file__), "lists")
_LISTS = {}


def _domains(name):
    if name not in _LISTS:
        with open(os.path.join(_DATA, name), encoding="utf-8") as handle:
            _LISTS[name] = frozenset(
                line.strip().lower() for line in handle if line.strip() and not line.startswith("#")
            )
    return _LISTS[name]


def _listed(domain, name):
    """True when the domain or one of its parent domains is on the list."""
    labels = domain.split(".")
    listed = _domains(name)
    return any(".".join(labels[i:]) in listed for i in range(len(labels) - 1))


def normalize_email(value):
    """Return (address, domain) with the domain in lower case."""
    value = (value or "").strip()
    validate_email(value)
    local, _, domain = value.rpartition("@")
    domain = domain.lower().rstrip(".")
    return "%s@%s" % (local, domain), domain


def domain_accepts_mail(domain):
    """True / False, or None when DNS could not answer.

    None is not a refusal: a resolver problem on our side must never turn a
    genuine evaluator away.
    """
    if dns is None:
        return None
    resolver = dns.resolver.Resolver()
    resolver.lifetime = resolver.timeout = float(conf.get("MX_TIMEOUT"))
    try:
        answers = resolver.resolve(domain, "MX")
        # RFC 7505 "null MX": the domain states that it accepts no mail.
        return any(str(r.exchange).rstrip(".") for r in answers)
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        pass  # no MX record: mail falls back to the address record
    except dns.exception.DNSException:
        return None
    for record in ("A", "AAAA"):
        try:
            resolver.resolve(domain, record)
            return True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            continue
        except dns.exception.DNSException:
            return None
    return False


def validate_work_email(value):
    """Return the normalised address or raise ValidationError."""
    try:
        address, domain = normalize_email(value)
    except ValidationError:
        raise ValidationError("Enter a valid email address, for example name@company.com.", code="invalid")
    if _listed(domain, "disposable_email_domains.txt"):
        raise ValidationError(
            "Temporary email addresses cannot receive the download link. Use your work email.",
            code="disposable",
        )
    if not conf.get("ALLOW_FREE_EMAIL") and _listed(domain, "free_email_domains.txt"):
        raise ValidationError(
            "Use your work email address. If your organisation has no email domain of its own, "
            "contact us and we will arrange the download.",
            code="free_provider",
        )
    if conf.get("CHECK_MX") and domain_accepts_mail(domain) is False:
        raise ValidationError(
            "%s cannot receive email. Check the address for a typing mistake." % domain,
            code="no_mail_server",
        )
    return address


def normalize_phone(value, country=None):
    """Return the number in E.164 form (+9665...) or raise ValidationError."""
    raw = (value or "").strip()
    message = "Enter a phone number we can reach you on, with the country code, for example +966 50 123 4567."
    if not raw or re.search(r"[^0-9+().\-\s]", raw):
        raise ValidationError(message, code="invalid")
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if phonenumbers is not None:
        try:
            parsed = phonenumbers.parse(raw, None if raw.startswith("+") else (country or None))
        except phonenumbers.NumberParseException:
            raise ValidationError(message, code="invalid")
        if not phonenumbers.is_valid_number(parsed):
            raise ValidationError(message, code="invalid")
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    # Without the phonenumbers library only the shape can be checked.
    digits = re.sub(r"\D", "", raw)
    if not raw.startswith("+") or not 8 <= len(digits) <= 15 or len(set(digits[-7:])) < 3:
        raise ValidationError(message, code="invalid")
    return "+" + digits


_JUNK = re.compile(r"^(n/?a|none|no|test|testing|asdf+|qwerty|xxx+|abc|company|private|personal|self|-+|\.+)$", re.I)


def validate_company(value):
    name = " ".join((value or "").split())
    if len(name) < 2 or not re.search(r"[^\W\d_]", name, re.UNICODE) or _JUNK.match(name):
        raise ValidationError("Enter the name of your company or organisation.", code="invalid")
    return name


def validate_person_name(value, label):
    name = " ".join((value or "").split())
    if not name or not re.search(r"[^\W\d_]", name, re.UNICODE) or re.search(r"https?:|www\.|[<>@]", name, re.I):
        raise ValidationError("Enter your %s." % label, code="invalid")
    return name
