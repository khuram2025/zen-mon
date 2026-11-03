#!/usr/bin/env python3
"""Systemd ExecStartPre guard for all commissioned controller consumers."""
import sys
from app.core.controller_admission import validate

try:
    validate('/run/zenplus-controller/grant.json')
except (OSError, ValueError, TypeError, KeyError):
    sys.exit('No current controller authority')
