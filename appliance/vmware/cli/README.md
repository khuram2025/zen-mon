# Zentryc appliance CLI edition r2

The authenticated command console replaces the old numbered recovery menu.
The web application remains the separately signed ZenPlus 1.24.14 payload.

Fresh downloads use the public bootstrap login **admin / Zentryc@Setup123**.
Console setup requires a new console password, then a different web administrator
password. HTTP/HTTPS listeners remain loopback-only until both passwords are
changed. No administrator session can use the unchanged console password.
Passwords are hashed independently. Console credentials are root-owned mode 0600
under `/var/lib/zentryc`; they never create a shared Linux password or change web
MFA. Five failed console logins impose a one-minute lockout. Idle sessions expire
after five minutes. Password changes revoke other console sessions.

Existing initialized appliances keep their web account and password. Installing
the CLI does not reset identity, licensing, monitoring data, or MFA. An already
configured dashboard stays available during this local console migration.

## Commands

```
help
show version
show interfaces
show certificate
show services
show license
show updates
show storage
show backups
ping 192.0.2.1
enable
configure terminal
interface ens160
ip address 192.0.2.10/24
ip gateway 192.0.2.1
ip dns 1.1.1.1 8.8.8.8
commit
commit confirm
end
service restart api
backup create config
backup create full
backup restore <backup-uuid>
license activate
updates check
updates apply
password console
ssh key add
show ssh
system reboot
logout
```

`?` and Tab completion show the commands available in the current mode.
Network changes are staged in a candidate. `commit` applies them provisionally;
`commit confirm` must follow within 120 seconds. An independent systemd unit
restores live and persistent network configuration on timeout, even if the console
is restarted. Boot recovery restores an unconfirmed candidate before networkd
starts. `rollback` explicitly cancels a pending change. The CLI refuses a legacy
`90-zentryc-management.yaml` override instead of silently replacing it.

Operations are fixed commands, not a Linux root shell. Backup operations reuse
installed application services as the `zenplus` account and wait for completion.
License keys/passwords enter hidden prompts and travel through stdin, never argv.
License registration uses the existing verified-TLS registration flow. Update
checks do not apply packages; `updates apply` queues the existing signed updater
with its rollout and maintenance policy. No arbitrary package paths or URLs are
accepted. Mutation commands write safe action names to the authentication journal.

Remote CLI is optional. After setup, `enable` then `ssh key add` enrolls an OpenSSH
public key. Connect with `ssh -t zencli@<management-ip>`, then enter the separate
console login. SSH accepts keys only and forces the branded CLI; forwarding,
SFTP, arbitrary remote commands, and non-TTY sessions are unavailable for this
account. Existing owner-created support SSH accounts are unaffected.

## Factory derivation and tests

Run `test_cli.py` in a separate Python 3.12 test environment with the source-pinned
server dependencies. It uses temporary credential files, mocked host operations
and a disposable Nginx template fixture; no installed database is accessed.

Derive a new writable system image from the unbooted r1 sealed master. Mount only
that new image and run `install-cli.py --root <mount> --factory`. Supply the CLI
directory, the matching source `first-boot.py` and `zenplus-security-helper`.
The installer refuses factory use when an identity or application environment is
present. Data disks remain the sealed empty r1 data disks. Never export a booted
customer, owner, or test disk. Run sealed-state inspection after installation,
then export to new immutable r2 filenames. Test fresh imports of the exact final
artifacts before publication. Native hypervisor qualification must be reported
separately from unit tests and software-emulated guest tests.

On a running QA appliance, install without `--factory` and restart
`zentryc-console` after preserving a private backup of affected files. Runtime
installation does not rerun or replace initialized first-boot state.
