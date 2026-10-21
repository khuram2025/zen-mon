# Validation of "ZenPlus Enablement Risk Assessment" v1.1 (VAPT, 19 Oct 2026)

- Validated: 21 Oct 2026, on appliance 192.168.8.221 (Ubuntu 24.04.2, running build = main 29d3259)
- Source: the reviewed archive matches the report (SHA-256 b6dc7e13…ade1c). Every file the report cites is byte-identical on current main.
- Method: the report's evidence was checked line by line. I also checked the material the reviewers did not have (install.sh, systemd units, sudoers, nginx base config, migrations), then ran read-only checks on .221: no sudo, no secrets printed, nothing changed.
- Not tested live: anything that needs root (sudoers contents, the polkit directory, the nftables/iptables ruleset, `/opt/zenplus/.env`). I also did not try a login with the seeded admin password.

## Verdict summary

| ID | Report rating | Verdict | Revised rating | Live on .221 |
|---|---|---|---|---|
| ZP-01 SSH terminal AutoAddPolicy | High | **Genuine** | Medium | Yes (installed code line 87) |
| ZP-02 Default secrets | High | **Partially genuine** | Low (hardening) | Datastore defaults rejected |
| ZP-03 /api/ outside nginx restriction | Medium | **Not genuine** | n/a | Policy off on .221 |
| ZP-04 API on 0.0.0.0 | Medium | **Not genuine** | n/a | API on 127.0.0.1 |
| ZP-05 Polkit updater control | Medium | **Partially genuine** | Low (24.04) / Medium (20.04, 22.04) | Narrow rule only |
| ZP-06 Seeded admin password | High | **Partially genuine** | High on fresh install.sh boxes until first login | Not tested |
| ZP-07 Network helper writes stdin | Medium | **Not genuine** (latent dead code) | n/a | No sudo grant |
| ZP-08 TLS install follows symlinks | Low | **Genuine** | Low/Medium (zenplus → root file read) | Staging zenplus 0700 |
| ZP-09 `?token=` on streams | Low | **Mostly not genuine** | Informational | Query strings not logged |
| ZP-10 Linux agent signature stub | Low | **Not genuine** (fails closed) | n/a | n/a |
| ZP-11 f-string SQL | Info | **Not genuine** as injection | n/a | n/a |
| ZP-12 Proxy trust / staging path | Info | **Partially genuine** | Low | n/a |

Of the report's five Gate 0 items, ZP-04 is false. ZP-02 is not live on a correctly installed appliance. ZP-05 is the narrow rule on 24.04. The items that genuinely need fixing before enablement are ZP-01, the install.sh path of ZP-06, and two issues the report missed (N1 and N2 below).

---

## Findings

### ZP-01: SSH test terminal does not verify host keys. GENUINE (Medium)

**Evidence**
- `server/app/services/ssh_terminal.py:87` uses `AutoAddPolicy`, and the installed copy on .221 is identical.
- Access is gated by `require_permission("settings.manage")` (`api/v1/ssh_terminal.py:50,70,81,94,102`). The report's `permissions.py:144` is only the risk label. Only the admin role holds this permission.

**Reality**
- Any host and any port is accepted, including loopback and docker networks.
- Different error messages give an internal port-scan oracle.
- Session open, failed open and close are audit-logged, without the password. The password is not stored.
- Keystrokes and output pass through Redis (`zp:sshterm:{id}:in/out`, up to 1 h TTL).
- NCCM and discovery also dropped host-key checking in 1.23.29. Their exposure is larger, because they send stored credentials on a schedule.

**Impact**
- An on-path attacker (ARP or DNS spoofing on the management LAN) can capture the device password an admin types, then proxy the session.
- An admin can reach anything the appliance can route to.

**Mitigation**
- Replace `AutoAddPolicy` with a policy backed by `ncm_host_keys`. On first use, show the fingerprint and require confirmation before the password is sent. Hard-fail on a changed key.
- Apply the same policy to `ncm_collection.py`.
- Add a setting that is off by default, or a dedicated `system.ssh_terminal` permission.
- Optionally restrict targets to inventoried devices.
- Delete the Redis input key as soon as it is consumed.

### ZP-02: "changeme" defaults in config.py. PARTIALLY GENUINE (Low as deployed)

**Evidence:** accurate (`config.py:17,25,28,31`). There is no startup check.

**Reality**
- `install.sh:37-41` generates random secrets into `/opt/zenplus/.env` (root:zenplus 0640). `first-boot-init.sh` and the VMware first-boot do the same.
- Live on .221, every probe was rejected:
  - PostgreSQL `zenplus`/default: rejected.
  - Redis with the default password: rejected (WRONGPASS); with no auth: NOAUTH.
  - ClickHouse `default`/default: rejected; with an empty password: rejected.
- I could not read `.env`, so the JWT secret value is unconfirmed. It comes from the same generator.

**Residual risk**
- Nothing refuses startup if `.env` is missing or is a dev launch.
- `install.sh` falls back to timestamp-based secrets if `openssl` is absent; that runs before prerequisites are installed.
- The Go poller and netflow collector have their own dev defaults.

**The report's claim that "JWT forgery alone is blocked by session checks" is wrong.** See N1.

**Mitigation**
- Add a `Settings` validator that refuses empty or "changeme" values and a `JWT_SECRET` shorter than 32 bytes. Add the same check to Go `config.Load`.
- Generate secrets after the prerequisites step, and fail instead of falling back.

### ZP-03: nginx /api/ lacks the access-policy include. NOT GENUINE

The include is intentionally only on the UI locations. The API enforces the same allowlist itself:
- `management_access.check_web_access` is called in `get_current_user` (`security.py:266`), for streams (`:334`), at login (`auth.py:336`) and at OIDC login (`auth.py:629`).
- It uses `request.client.host`, which cannot be forged (see ZP-12).

Machine ingest (agents, sensors, OTLP, RUM, APM) must stay reachable and uses its own keys. On .221 `web_restricted` is `false`, so there is currently no restriction to bypass.

**Optional defence in depth:** add the include to `/api/v1/auth/` and `/api/v1/system/` in nginx.

### ZP-04: API listens on 0.0.0.0. NOT GENUINE

`API_HOST` is never read. uvicorn is started with `--host 127.0.0.1` (`install.sh:853` and the `security.conf` drop-in).

Live on .221:

| Port | Binding |
|---|---|
| 8000, 8001, 8081, 8091, 8123, 9000, 6379 | 127.0.0.1 |
| 5174 | absent |
| 5432, 9181 | 10.79.0.1, the WireGuard HA overlay `zpha` |
| *:9234 | dropped on everything except `zpha` by `zenplus-ha-firewall` (`INTERNAL_PORTS` list) |
| 22, 80, 443 and UDP 162/2055/6343/51822 | public, by design |

**Cleanup**
- Delete `API_HOST` and fix the base unit text that still says 0.0.0.0.
- Optionally add 5432, 6379, 8123 and 9000 to the access-helper drop chain.
- Note that `install.sh` does not enable a default-deny host firewall. The golden OVA does, using ufw.

### ZP-05: polkit lets zenplus control the updater. PARTIALLY GENUINE

**What is installed depends on the OS:**
- On Ubuntu 24.04 (.221), `install.sh` installs the `.rules` file. It allows `zenplus` to manage only `zenplus-updater.service/.timer`. Starting the updater only fetches and applies **signed** packages from a root-owned trust config, so this is low impact.
- On 20.04 and 22.04 (polkit 0.105), the `.pkla` is installed instead. It grants `manage-units` for **every** unit, so `zenplus` could stop ssh, nginx, PostgreSQL or the firewall service. That part is genuine (availability and integrity).

**"Unlogged" is mostly false:** systemd logs unit start and stop, and sudo logs every command.

**Mitigation**
- Delete the pkla. On polkit below 0.106, use only sudo.
- Also fix N2.

### ZP-06: seeded admin with a known password. PARTIALLY GENUINE (High until first login, install.sh only)

**The ordering concern is unfounded.** `install.sh` runs `init-postgres.sql`, then migrations, so 153 always runs after the seed. `install.sh:576` also sets `must_change_password` directly.

**Enforcement is server-side on every API route:** `_enforce_sign_in_obligations` allows only the auth, me and time endpoints until the password is changed.

**The real gap is first-login takeover.**
- Whoever logs in first with the published password can change it (the old password is known) or enrol MFA, and own the appliance.
- `install.sh` prints the password in its summary.
- The window lasts from when nginx starts until the owner's first login, and can be over plain HTTP if the TLS step failed.
- OVA and first-boot paths are **not** affected: they use a random one-time password or lock the seeded hash.

**Mitigation**
- Make `install.sh` use the first-boot approach: lock the seed (`'!locked'`), generate a random password into a root-only 0600 file, and stop printing it.
- Block MFA enrolment while `must_change_password` is set.

### ZP-07: network helper writes stdin as root. NOT GENUINE (latent)

The path in the report is wrong; the file is `bin/zenplus-apply-network.sh`.

It is not reachable:
- No sudoers grant exists in the repo or on .221, where `sudoers.d` has only access, security, storage and updater.
- `zenplus-cli` is not installed.
- No API route calls it.

**Cleanup:** remove `zenplus-cli` and the script from `build-release.py` and `verify-ota-release.py`. If they are kept, build the file from validated fields.

### ZP-08: install-cert follows symlinks. GENUINE (Low/Medium)

**Live state on .221:** staging is `zenplus:zenplus 0700`; `/etc/zenplus/tls` is root 0755 with 0644 certificates. `validate_pair` parses the PEM, so a plain symlink to `/etc/shadow` fails.

**It can still be bypassed in two ways:**
1. Race: swap the symlink between validation and `install`.
2. Bundle trick: point to any root-only file that contains a PEM block. It is copied verbatim and published at 0644.

**Impact:** code execution as `zenplus` becomes a read of any root file. A web admin alone cannot do this.

**Mitigation**
- Copy from an `O_NOFOLLOW` open into a root-only temp dir.
- Then **re-encode** with `openssl x509 -out`, `openssl pkey -out` and `openssl pkcs7 -print_certs`, so only parsed certificate and key material is ever written.

### ZP-09: session token accepted as ?token=. MOSTLY NOT GENUINE

- The dashboard uses a 60-second, stream-only `?ticket=` (`useSSE.ts:79`). The legacy `?token=` is accepted, but no client sends it.
- Live on .221:
  - nginx logs `$uri` without the query string (`log_format zenplus_safe`, `nginx.conf:40`).
  - uvicorn runs with `--no-access-log`.
  - `token=` appears 0 times in `access.log`.

**Mitigation**
- Remove the `token` query parameter.
- Optionally make tickets single-use.
- Set `access_log … zenplus_safe` explicitly in the site config, so it does not depend on the soft hardening step.

### ZP-10: Linux agent signature check is a stub. NOT GENUINE

`verify_other.go` fails **closed**. On top of that, Linux `launchInstaller` always errors, and only Windows packages are built, so a Linux agent never installs an update itself.

On Windows, verification is Authenticode via WinVerifyTrust, with revocation checks and a pinned publisher, subject and root.

**Related real gap:** the first Linux install (`agents.py` install.sh) trusts the controller's TLS alone.

**Recommendation:** if Linux self-update is ever added, require an ed25519 signature against a key built into the agent.

### ZP-11: f-string SQL. NOT GENUINE as injection

There are about 918 SQL f-strings, not about 25. All of them were traced, and the cited `report_sections.py` example uses a constant condition with bound values.

Sort columns, buckets and identifiers are whitelisted or validated. ClickHouse and PostgreSQL values are bound. NetFlow filters bind or validate IPs and ports. **No user-reachable injection was found.** A separate issue sits next to this one (N3).

**Recommendation:** add a CI lint (for example semgrep) that blocks interpolation of non-whitelisted names.

### ZP-12: proxy header trust and staging path. PARTIALLY GENUINE (Low)

**What is safe**
- The staging path in the access helper uses `O_NOFOLLOW` with a size cap and a strict schema.
- uvicorn trusts forwarded headers only from 127.0.0.1, and nginx appends `X-Forwarded-For`. So the access restriction and the login audit trail use the real client IP.

**What is genuine:** `agents.py:169`, `rum.py:697` and `apm.py:59` take the **left-most** `X-Forwarded-For` hop, which the client controls. That allows forged agent and server IP records and RUM GeoIP data, and bypasses the per-IP RUM quota.

**Mitigation**
- Use one shared helper based on `request.client.host`.
- Pin `--forwarded-allow-ips 127.0.0.1` explicitly.

---

## Issues the report missed

| # | Issue | Severity | Fix |
|---|---|---|---|
| N1 | `_authenticate_token` checks the server-side session only `if jti:`. A token signed with the real secret but without `jti` skips revocation and MFA. User UUIDs are listable by viewers (`/alerts/assignees`). It is exploitable only if `JWT_SECRET` leaks or is default, but it removes the safety net the report assumed. | Medium | Require `jti` on session tokens (`security.py:231`). |
| N2 | The sudoers grant allows `tee /etc/systemd/system/zenplus-updater.timer.d/override.conf` with arbitrary stdin. That lets `zenplus` write a timer override with `Unit=` pointing to any unit, for example `reboot.target`. | Medium | Replace it with a root helper that accepts only a validated interval and writes a fixed template. |
| N3 | Backup upload and restore (admin only) trusts `manifest.json`: table names are interpolated into `DROP TABLE`, `clickhouse_schema.sql` runs verbatim, and tar extraction does not reject symlink or hardlink members. | Low/Medium | Sign backups with an appliance HMAC, validate names against known tables, accept only CREATE statements, and extract with `tar.extract(filter="data")`. |
| N4 | Redaction in support bundles misses: secrets inside URL paths (Slack and Teams webhooks, Telegram `/bot<token>/`), the `routing_key`, `auth_key`, `priv_key` and `smtp_pass` keys, a bare `Bearer`, `X-Agent-Key` and `X-Sensor-Key`. | Low | Add those patterns and key names, plus a test corpus. |
| N5 | SSH terminal keystrokes sit in Redis for up to an hour. | Low | Delete each input key once it is consumed and shorten the TTLs. |
| N6 | The install.sh fallback to timestamp secrets when openssl is missing. | Low | Fail closed. |

**Corrections to the data and outbound sections (Sections 4 to 6)**
- **Telegram:** used only by the "test channel" button. Live alerts have no Telegram sender.
- **db-ip:** GeoIP is a monthly database download, or an uploaded `.mmdb`. Lookups are **local**; no IP addresses are sent to db-ip.
- **Destinations the report missed:**
  - IEEE and Wireshark OUI downloads, automatic when the table is empty;
  - threat-intel feeds, which are opt-in;
  - the Windows tray app's direct check to zentryc.com;
  - operator-configured SMTP, SMS, webhook, Slack, Teams and Discord;
  - OIDC, LDAP and RADIUS;
  - NTP.
- **Updater:** the Bearer API key is also sent to the `package_url` host the server returns. Pin it to the trust-config origin.
- **Backups:** `/var/lib/zenplus-appliance-backups` does not exist on .221.
- **Support bundles:** stored at `zenplus:zenplus 0750`, never uploaded, row counts only.

## Recommended fix order

1. **Before enablement:**
   - ZP-01: host-key pinning for the terminal and NCCM, with the terminal off by default;
   - ZP-06: install.sh one-time password, no printed default;
   - N1: require `jti`;
   - ZP-02: refuse default or short secrets at startup.
2. **Within 30 days:**
   - ZP-08: re-encode certificates;
   - N2: helper in place of the sudo `tee`;
   - ZP-05: drop the pkla;
   - ZP-12: one shared client-IP helper;
   - ZP-09: remove `?token=`;
   - N4: redaction gaps.
3. **Hygiene:**
   - ZP-04 and ZP-07: dead code and settings;
   - N3: backup trust;
   - an SQL lint in CI.

Live checks to repeat on each appliance (read-only): `ss -ltn`; `systemctl cat zenplus-api`; `grep zenplus_safe /etc/nginx/nginx.conf`; `ls /etc/sudoers.d`; `cat /etc/zenplus/access-policy.json`; plus, as root, `sudo -l -U zenplus` and `ls /var/lib/polkit-1/localauth`.
