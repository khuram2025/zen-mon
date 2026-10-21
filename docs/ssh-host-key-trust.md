# SSH host key trust

The appliance records the SSH host keys it sees in `ncm_host_keys`
(`app.services.ssh_host_keys`). Each consumer applies its own policy.

| Consumer | Policy | On an unknown key | On a changed key |
|---|---|---|---|
| SSH test terminal (Settings) | Confirm on first use | 409 `host_key_unknown` with the fingerprint; the password is not sent. The client retries with `trust_fingerprint`, and the key is stored as `terminal_confirmed`. | 409 `host_key_changed`, refused. An administrator can `DELETE /api/v1/system/ssh-terminal/host-keys` to forget the stored key. |
| NCCM backups | Warn only | Recorded as `first_use`. | The backup still runs. The change is audited as `backup.host_key_changed` and returned as `host_key` in the fetch result. The stored key is kept. |
| UDT gateway collection | Operator-registered keys only | Accepted. | Refused, but only when an operator-registered key exists. Keys recorded as `first_use` or `terminal_confirmed` are ignored. |

## Rules

- The terminal never sends the password before the host key is accepted. The paramiko policy runs after key exchange and before authentication.
- Forgetting a key retires every active key for `host:port`. After that, the next terminal connection asks for confirmation again, and the next NCCM backup records the new key.
- NCCM never adopts a changed key automatically. To clear the warning, forget the stored key in the terminal, or verify the device and retire the old row.

## Audit events

- `ssh_terminal.host_key_trusted`
- `ssh_terminal.host_key_forgotten`
- `ssh_terminal.open_failed`: carries the key code and fingerprints when the failure was a host key refusal.
- `backup.host_key_changed`
