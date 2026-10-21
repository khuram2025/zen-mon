"""STARTTLS policy shared by every SMTP sender (alerts, reports, NCM, tests).

``encryption`` of an SMTP gateway:

* ``ssl``  — implicit TLS (SMTP_SSL); nothing to do here;
* ``tls``  — STARTTLS is required;
* ``none`` — STARTTLS when the server offers it, plain otherwise. Gateways
  saved before the ``encryption`` field existed show as ``none`` and always
  ran STARTTLS (Office 365 on 587 refuses AUTH without it), while a plain
  relay on port 25 does not offer it and must not be forced into it.
"""

from __future__ import annotations

import smtplib
import ssl
from typing import Optional


def starttls_if_needed(server: smtplib.SMTP, encryption: Optional[str],
                       context: Optional[ssl.SSLContext] = None) -> bool:
    """Upgrade ``server`` to TLS as the gateway's ``encryption`` requires;
    returns whether STARTTLS was used."""
    enc = (encryption or "tls").lower()
    if enc == "ssl":
        return False
    if enc == "none":
        try:
            server.ehlo_or_helo_if_needed()
        except smtplib.SMTPException:
            return False
        if not server.has_extn("starttls"):
            return False
    server.starttls(context=context or ssl.create_default_context())
    return True
