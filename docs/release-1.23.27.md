# ZenPlus 1.23.27 — Optional automatic SSH trust for NCCM

Appliance-only cumulative release based on the 1.23.26 release source, including
its CI fixture correction. Minimum upgrade prerequisite remains 1.23.9.
Separately installed agent packages are preserved.

## Changes

- Connection profiles have an explicit **Automatically trust new devices** option.
  It defaults to off for existing and new profiles. Changing it requires both
  shared credential administration and SSH trust administration permission.
- For a previously unknown address and port, the first backup reads the SSH key
  without authenticating, saves and audits it, then verifies that exact key during
  the authenticated collection connection. No password is sent during the probe.
- Future connections reject changed keys before authentication. Existing saved
  keys are never replaced automatically. Retired trust cannot be relearned;
  it requires administrator registration. Malformed trust files fail closed.
- First-use entries are labelled **Automatically learned—not console verified**.
  Administrators can independently verify and promote the same key through the
  normal SSH trust registration form. Strict profiles require verified trust.
- Readiness permits queued manual, bulk and scheduled jobs for first-use profiles
  while showing that the first key has not yet been learned. Capture success and
  validation remain separate checks.

## Impact and schema

Automatic first-use trust does not independently establish the device identity
on its first connection. An impersonating SSH server could receive the password
or supply a false configuration. Strict verification remains the default.
Legacy SSH cryptography is a separate profile option and remains off by default.

Append-only migration `migrate-152-ncm-auto-trust.sql` adds
`ncm_credentials.ssh_auto_trust BOOLEAN NOT NULL DEFAULT false`. Existing trust,
profiles and captures are retained. No new system or signing-key bridge is needed.
Use normal updater code and database backups for recovery.

## Release gates

Required: isolated database and real SSH fixtures for first-use persistence,
repeat capture, changed-key rejection before authentication, retirement, strict
mode and permission enforcement; migration idempotency; strict dashboard build
and route smoke checks; all five CI checks; signed package verification; normal
canary upgrade on 192.168.8.221 and post-upgrade NCCM/UDT checks. Publication and
appliance evidence are recorded separately after these gates complete.
