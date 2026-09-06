"""Root-owned management access policy; machine-ingest credentials are independent."""
import ipaddress
import json
from pathlib import Path
from fastapi import HTTPException
POLICY_PATH = Path('/etc/zenplus/access-policy.json')
DEFAULT_POLICY = {'web_restricted': False, 'ssh_restricted': False, 'allowed_cidrs': []}

def load_policy():
    try:
        return DEFAULT_POLICY | json.loads(POLICY_PATH.read_text())
    except FileNotFoundError:
        return dict(DEFAULT_POLICY)
    except (OSError, ValueError):
        raise HTTPException(503, 'Management access policy is unavailable')

def address_allowed(source, cidrs):
    try:
        address = ipaddress.ip_address(source)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return any(address in ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)
    except ValueError:
        return False

def check_web_access(source):
    policy = load_policy()
    if policy['web_restricted'] and not address_allowed(source, policy['allowed_cidrs']):
        raise HTTPException(403, 'Web administration is not allowed from this source address')
