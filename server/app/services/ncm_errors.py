"""Classify failures without returning third-party exception strings or CLI output.

Every failed attempt is reduced to a stable category, a safe message and a
retry decision. Permanent setup failures (authentication, driver,
privilege, credentials) fail the job immediately: repeating the same attempt
cannot succeed and only multiplies failed connections and alerts. Transient
failures (reachability, timeouts, interrupted sessions, storage) retry within
the job's bounded budget.
"""
import socket
from dataclasses import dataclass
from paramiko import AuthenticationException, SSHException
from paramiko.ssh_exception import NoValidConnectionsError
from netmiko.exceptions import NetmikoTimeoutException, ReadTimeout
from app.services.ncm_collection import CaptureError


@dataclass(frozen=True)
class Failure:
    category: str
    message: str
    retryable: bool


# Operator-facing remediation per category. Shown next to the safe message.
GUIDANCE = {
    'blocked': 'Resolve the readiness items on the device page, then queue the backup again.',
    'not_enrolled': 'Save an enabled enrollment with a connection profile.',
    'credentials': 'Re-save the connection profile secret; confirm the appliance archive key has not changed.',
    'authentication': 'Check the profile username/password or key, the account lockout state and source-address restrictions on the device.',
    'unreachable': 'Verify the management IP, routing, ACLs/firewall rules and that SSH is enabled on the device port.',
    'timeout': 'Verify reachability and device load; slow devices may need a less busy maintenance window.',
    'negotiation': 'The device and collector share no SSH algorithms or the handshake was refused; check SSH server settings.',
    'privilege': 'Grant the backup account read access to the full configuration (see the vendor access guidance).',
    'driver': 'Select the exact platform driver for this device; auto-detection could not resolve a supported collector.',
    'unsupported_type': 'Remove capture types the selected driver cannot collect.',
    'incomplete': 'The device output ended early or kept paging; retry, then check terminal/pager settings for the account.',
    'format': 'The output did not look like this platform\'s configuration; verify the selected driver and account privileges.',
    'interrupted': 'The SSH session dropped mid-capture; check device stability and retry.',
    'archive': 'Check appliance database/storage health and the archive encryption key; the previous validated copy is preserved.',
    'cancelled': 'The job was cancelled by an operator.',
    'unknown': 'Inspect the device connection and selected driver, then retry.',
}

STAGES = {
    'prerequisites': 'Prerequisite check',
    'credentials': 'Credential decryption',
    'connect': 'SSH connection',
    'collection': 'Configuration collection',
    'validation': 'Output validation',
    'archive': 'Archive',
}


def exception_chain(exc):
    """Follow Netmiko wrappers without exposing third-party exception text."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def classify(exc, stage='collection') -> Failure:
    if isinstance(exc, PrerequisiteError):
        return Failure(exc.category, str(exc), False)
    if stage == 'credentials':
        return Failure('credentials', 'Credentials unavailable: verify the saved profile and appliance encryption key.', False)
    if stage == 'archive':
        return Failure('archive', 'Archive failed: inspect database/storage health and encryption; the previous validated snapshot is preserved.', True)
    chain = list(exception_chain(exc))
    # Netmiko wraps handshake errors in NetmikoTimeoutException.
    # Resolve permanent causes before allowing the wrapper to trigger retries.
    for error in chain:
        if isinstance(error, AuthenticationException):
            return Failure('authentication', 'Authentication rejected: verify the dedicated account, password, SSH access and source-address restrictions.', False)
        if isinstance(error, NoValidConnectionsError):
            return Failure('unreachable', 'SSH unreachable: verify the management address, port, routing and access rules.', True)
        if isinstance(error, SSHException) and not isinstance(error, (NetmikoTimeoutException, ReadTimeout)):
            message = str(error).lower()
            if 'no existing session' not in message and 'error reading ssh protocol banner' not in message:
                return Failure('negotiation', 'SSH negotiation failed: verify the supported SSH algorithms.', False)
    if isinstance(exc, AuthenticationException):
        return Failure('authentication', 'Authentication rejected: verify the dedicated account, password, SSH access and source-address restrictions.', False)
    if isinstance(exc, NoValidConnectionsError):
        return Failure('unreachable', 'SSH unreachable: verify the management address, port, routing and access rules.', True)
    if isinstance(exc, (NetmikoTimeoutException, ReadTimeout, socket.timeout, TimeoutError)):
        return Failure('timeout', 'SSH timed out: verify reachability, SSH service and device responsiveness.', True)
    if isinstance(exc, CaptureError):
        # CaptureError is deliberately restricted to application-authored messages.
        category = getattr(exc, 'category', 'incomplete')
        return Failure(category, str(exc), category in ('incomplete', 'interrupted'))
    if isinstance(exc, SSHException):
        return Failure('negotiation', 'SSH negotiation failed: verify the supported SSH algorithms.', False)
    if isinstance(exc, (ConnectionError, EOFError, OSError)):
        return Failure('interrupted', 'SSH connection interrupted: verify the device and network, then retry the capture.', True)
    return Failure('unknown', 'Collection failed: inspect the device connection and selected driver; no validated snapshot was published.', True)


def failure_message(exc, stage='collection'):
    return classify(exc, stage).message


class PrerequisiteError(Exception):
    """A local prerequisite failed before any device connection was attempted."""

    def __init__(self, message, category='blocked'):
        super().__init__(message)
        self.category = category
