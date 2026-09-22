"""Classify failures without returning third-party exception strings or CLI output."""
import socket
from paramiko import AuthenticationException, BadHostKeyException, SSHException
from paramiko.ssh_exception import NoValidConnectionsError
from netmiko.exceptions import NetmikoTimeoutException, ReadTimeout
from app.services.ncm_collection import CaptureError


def failure_message(exc, stage='collection'):
    if stage == 'credentials':
        return 'Credentials unavailable: verify the saved profile and appliance encryption key.'
    if stage == 'archive':
        return 'Archive failed: inspect database/storage health and encryption; the previous validated snapshot is preserved.'
    if isinstance(exc, BadHostKeyException):
        return 'SSH host key changed: independently verify device identity before updating managed SSH trust.'
    if isinstance(exc, AuthenticationException):
        return 'Authentication rejected: verify the dedicated account, password, SSH access and source-address restrictions.'
    if isinstance(exc, NoValidConnectionsError):
        return 'SSH unreachable: verify the management address, port, routing and access rules.'
    if isinstance(exc, (NetmikoTimeoutException, ReadTimeout, socket.timeout)):
        return 'SSH timed out: verify reachability, SSH service and device responsiveness.'
    if isinstance(exc, CaptureError):
        # CaptureError is deliberately restricted to application-authored messages.
        return str(exc)
    if isinstance(exc, SSHException):
        if 'not found in known_hosts' in str(exc).lower():
            return 'SSH host key is not trusted: independently verify the fingerprint and register the address and port.'
        return 'SSH negotiation failed: verify managed host keys and supported SSH algorithms.'
    if isinstance(exc, ConnectionError):
        return 'SSH connection interrupted: verify the device and network, then retry the capture.'
    return 'Collection failed: inspect the device connection and selected driver; no validated snapshot was published.'
