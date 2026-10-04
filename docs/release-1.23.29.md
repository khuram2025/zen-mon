# ZenPlus 1.23.29 — SSH host trust removed

Network device SSH connections no longer require, store or check a saved host
key. NCCM backups and discovery credential tests connect directly: the key the
device presents is accepted and the profile credentials are used to log in.

Removed:

- The **SSH host trust** readiness requirement and its "Set up SSH trust"
  action. A device is ready once it has a profile, an enabled enrollment and a
  working backup worker.
- The device **SSH trust** dialog and button, the `/devices/{id}/ncm-trust`
  endpoints, and the `ncm.trust` permission (stored role entries are ignored).
- The connection-profile option **Automatically trust new devices**.
- The `/etc/zenplus/known_hosts` file requirement for NCCM and for discovery
  credential tests (SSH authentication checks).
- The `host_key_untrusted` / `host_key_changed` failure classes. Past runs that
  recorded them keep their labels in history.

Unchanged: legacy SSH compatibility remains an explicit per-profile opt-in for
SHA-1 key exchange and RSA/SHA-1 signatures; read-only Fortinet collection;
credential handling and every other NCCM check. Existing `ncm_host_keys` rows
and the `ssh_auto_trust` column stay in the database, unused (no migration).

Security note: without host key checking, a device that impersonates another on
the management network could receive the profile's SSH credentials. Keep the
management network restricted and use dedicated least-privilege backup
accounts.

Appliance-only cumulative release (includes 1.23.26–1.23.28), minimum
prerequisite 1.23.9. Existing agent packages are retained. Every release carries
the complete migration set; this release adds none.
