# ZenPlus 1.24.26 — Security hardening from the external VAPT assessment

Fixes the confirmed findings of the external enablement risk assessment of 19 October 2026. The full validation, including the false positives, is in `security-audit/2026-10-21/VAPT-REPORT-VALIDATION.md`. A correctly installed appliance keeps working as before; the changes that you will see are listed first.

**What changes for operators**
- **SSH terminal (Settings):** the first connection to a device shows its host key fingerprint, and your password is only sent after you click **Trust and connect**. If a device's key changes later, the terminal refuses to connect until an administrator clicks **Forget stored key**. See `docs/ssh-host-key-trust.md`.
- **NCCM backups:** the first host key each device presents is recorded. A later change is written to the audit log as `backup.host_key_changed`, but the backup still runs.
- **Fresh installs (install.sh):** there is no default admin/admin123 login any more. The installer writes a random one-time password to `/var/lib/zenplus/initial-admin-password` (root only) and never prints it. Existing admin passwords are not changed.
- **First sign-in:** MFA cannot be set up while a password change is still pending.
- **After this update everyone signs in again once** if the appliance's JWT secret was weak; the update replaces it automatically.

**Security fixes**
- Login tokens must belong to a server-side session. Session tokens never travel in a URL; live streams use the one-minute stream ticket.
- The API, poller and NetFlow collector refuse to start on placeholder secrets. The update itself stops, before changing anything, if a database password in `/opt/zenplus/.env` is still a placeholder.
- Client addresses recorded for agents, RUM and APM come from nginx's view of the caller. A forged `X-Forwarded-For` header is ignored. uvicorn trusts forwarded headers from 127.0.0.1 only.
- Certificate install refuses symlinks and other non-regular staged files and installs only re-encoded certificate, key and chain data.
- The updater's free-form sudo `tee` grant is replaced by a root helper that only accepts a check interval of 1–168 hours. On Ubuntu 20.04/22.04 the old polkit grant (which covered every service) is removed; the dashboard's update controls use sudo.
- nginx logs requests without query strings.
- Support bundles redact more secret formats: webhook and Telegram URLs, PagerDuty keys, bare Bearer tokens, cloud, GitHub and ZenPlus keys, and CLI passwords.
- Backup upload and restore check the archive: table and backup names, schema statements (zenplus `CREATE` statements on safe engines only) and tar members (no links or devices).
- SSH terminal keystrokes are dropped as soon as a session closes.
- Removed: the unused `API_HOST` setting and the unused console network helper (`zenplus-cli`, `zenplus-apply-network.sh`).

Schema: no migration.

Appliance-only cumulative release (includes 1.24.24 and 1.24.25), minimum version 1.23.9.
