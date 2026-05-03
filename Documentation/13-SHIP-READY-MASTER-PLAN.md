# ZenPlus Ship-Ready Master Plan

> **Audience.** Product, engineering, and the remote-server (zentryc.com) operator. Read this end-to-end before shipping the first appliance.
>
> **Scope.** Everything that has to be true for the OVA to leave the building, every interaction the customer can have with it, and every interaction zentryc.com has with the fleet. System-level updates (apt, systemd units, kernel sysctls, polkit rules, even DB schema bumps) must flow through the same channel as application updates — that requirement is non-negotiable and is reflected throughout this plan.
>
> **Status of supporting docs.**
> - `12-APPLIANCE-BASE-SYSTEM.md` — base OS spec. Authoritative.
> - `10-OTA-UPDATE-SYSTEM.md` — OTA design overview. Authoritative.
> - `11-SERVER-SIDE-IMPLEMENTATION-GUIDE.md` — remote server contract. **Authoritative for the API**, but the public-key block has been corrected in this revision; treat the example database schema as a starting point and reconcile against §7 below.
> - `08-DEPLOYMENT.md`, `09-STARTUP-GUIDE.md` — older, dev-mode oriented. Useful for context but do not match the appliance shipping path.

---

## 1. Executive summary

ZenPlus ships as a virtual appliance (OVA, Ubuntu 24.04 LTS base) that boots, generates its own secrets on first start, lets the customer configure network and hostname through a Cisco-style CLI on the console, and then asks them to paste a license key in the web UI. From that moment the appliance is registered against zentryc.com, receives a heartbeat-driven OTA update offer every 4 hours, applies any signed update unattended (with full rollback on failure), and reports the result. There is no "ssh into the box and git pull" workflow on customer sites — the only human action ever required after the license key is the same license key (or a new one if their subscription changes).

The mechanism that delivers application updates is the same mechanism that delivers system-level updates. A release manifest can install apt packages, drop new systemd units, run polkit rules, install configs into `/etc`, run host-level scripts, and reload the daemon — all signed, all rolled back if any step fails, all reported back to zentryc.com. This is already implemented in the agent and exercised by `os_package.py`, `install_systemd.py`, `install_config.py`, and `run_hook.py`; what's missing is on the server side and in the build/sign pipeline, which §6–§9 specify.

The remaining work to get to first shipment is server-side, plus a small amount of appliance-side hardening. The exact shopping list is in §3 (Audit) and §11 (Pre-ship checklist).

---

## 2. End-to-end picture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                                CUSTOMER SITE                                  │
│                                                                                │
│   ┌───────────────────────────────────────────────────────────────────────┐  │
│   │            ZenPlus Appliance VM (OVA, Ubuntu 24.04)                  │  │
│   │                                                                        │  │
│   │  Console TTY  ──>  zenplus-cli  (network, hostname, NTP, status)     │  │
│   │  Web :80       ──>  Nginx ─> FastAPI ─> Postgres / ClickHouse / Redis│  │
│   │                                  │                                     │  │
│   │  systemd: zenplus-first-boot ───┘  (one-shot, gates everything)      │  │
│   │           zenplus-api / zenplus-poller / zenplus-dashboard           │  │
│   │           zenplus-updater.timer  ──> /opt/zenplus/venv/python -m updater│
│   │                                              │                          │
│   └─────────────────────────────────────────────┼──────────────────────────┘
│                                                 │ HTTPS (TLS 1.2+, pinned CA) │
└─────────────────────────────────────────────────┼─────────────────────────────┘
                                                  │
                                                  ▼
                       ┌───────────────────────────────────────────────────┐
                       │          zentryc.com  (the remote server)        │
                       │                                                    │
                       │   Nginx ── FastAPI ── Postgres ── Object store    │
                       │                                                    │
                       │   • Issues license keys (= registration tokens)   │
                       │   • Tracks fleet (one row per appliance)          │
                       │   • Stores .zup release packages + Ed25519 sigs  │
                       │   • Runs the rollout engine (canary / % / full)   │
                       │   • Admin dashboard for support                   │
                       └───────────────────────────────────────────────────┘
                                                  ▲
                                                  │
                                          ┌───────┴───────┐
                                          │  Build VM     │
                                          │  (this repo)  │
                                          │  build-release│
                                          └───────────────┘
```

There are exactly **three** trust anchors:

1. **Ed25519 release public key** — embedded on every shipped OVA at `/opt/zenplus/updater/keys/zentryc-release.pub`. Signs every manifest. Loss of the matching private key forces a key rotation (mechanism described in §6.3).
2. **TLS server certificate for zentryc.com** — appliance verifies via the system CA bundle. Customer must be able to reach zentryc.com on 443.
3. **Per-appliance API key** — issued at registration time, stored at `/opt/zenplus/updater/config/agent.conf` (mode 0600), revocable by zentryc.com admin.

Those three together make the channel safe even if DNS is poisoned or the customer terminates TLS at their own proxy.

---

## 3. Audit — what is already shippable, what isn't

I went through every module under `/home/net/zen-mon/`. The agent side is more complete than the docs claim; the server side is essentially documented but not built. Concretely:

### 3.1 What the appliance already does correctly

| Capability                                    | Where                                          | Verdict                                       |
|-----------------------------------------------|------------------------------------------------|-----------------------------------------------|
| First-boot secret generation, idempotent      | `bin/first-boot-init.sh`                       | ✅ Sentinel-gated, fails closed                |
| Agent registration via license key            | `updater/agent.py:register`                    | ✅ Posts to `/appliances/register`             |
| Heartbeat + update offer                      | `updater/agent.py:checkin` + timer             | ✅ Every 4h (5m post-boot), randomized 0–5m    |
| Signed manifest verification (Ed25519)        | `updater/crypto.py:verify_manifest`            | ✅ Plus 30-day max-age + future-date check     |
| Per-file SHA-256 verification                 | `updater/crypto.py:verify_checksums`           | ✅ Tar-extraction also rejects path traversal  |
| Resumable HTTPS download                      | `updater/downloader.py`                        | ✅ With expected-hash gate                     |
| Step executor with rollback                   | `updater/executor.py`                          | ✅ Manifest-driven, idempotent rollback steps  |
| Code replacement (`apply_code`)               | `updater/steps/apply_code.py`                  | ✅ Replace + git-patch modes                   |
| Pip install                                   | `updater/steps/pip_install.py`                 | ✅                                             |
| **System apt install/remove**                 | `updater/steps/os_package.py`                  | ✅ Protected-package safety list               |
| **systemd unit install + enable + reload**    | `updater/steps/install_systemd.py`             | ✅                                             |
| **Config file install (e.g. `/etc/...`)**     | `updater/steps/install_config.py`              | ✅ Backs up old version with `.pre-update`     |
| **Pre/post-update hook scripts**              | `updater/steps/run_hook.py`                    | ✅ Runs as root with `ZENPLUS_DIR` env         |
| Postgres + ClickHouse migrations              | `updater/steps/run_migration.py`               | ✅ Engine routed by step.engine                |
| Service control                               | `updater/steps/service_control.py`             | ✅ Skips missing units, force-kill on timeout  |
| Code+DB backup before update                  | `updater/rollback.py:create_backup`            | ✅ Tar.gz of code, gz pg_dump                  |
| Restore from most recent backup               | `updater/rollback.py:restore_backup`           | ✅ Wired as `restore_backup` step              |
| Concurrent-update lock                        | `updater/lockfile.py`                          | ✅                                             |
| Local update history (UI feeds off this)      | `updater/history.py` + `system_updates.py`     | ✅ Persisted JSON, reflected in dashboard      |
| Post-update health check                      | `updater/health.py` + `health_check` step      | ✅ HTTP 200 with timeout                       |
| Cisco-IOS console CLI                         | `bin/zenplus-cli`                              | ✅ Network/hostname/DNS/NTP/storage/status     |
| Network configuration via systemd-networkd    | `bin/zenplus-apply-network.sh`                 | ✅ Privileged helper with restart              |
| /data LVM growth (online)                     | `bin/expand-storage.sh` + UI endpoints         | ✅ Add disk, grow volume, rescan SCSI bus      |
| Dashboard "Updates" tab + license entry       | `server/app/api/v1/system_updates.py`          | ✅ Status, history, register, refresh-sub      |
| Ship-readiness verification                   | `bin/verify-ship-ready.sh`                     | ✅ Refuses snapshot if any prep step missed    |

### 3.2 What is still missing or incomplete

These are the gating items. Nothing else is between us and shipping.

| # | Gap                                                                                       | Severity | Owner                |
|---|-------------------------------------------------------------------------------------------|----------|----------------------|
| 1 | **zentryc.com server does not exist yet.** The agent talks to documented endpoints, but no implementation is hosted. | Critical | Remote-server team   |
| 2 | License-key issuance flow (admin generates → handed to customer → consumed at register).  | Critical | Remote-server team   |
| 3 | Subscription enforcement (`max_appliances`, `max_devices`, expiry) on the server side.    | Critical | Remote-server team   |
| 4 | Release-signing private-key custody policy (currently no documented home).                | Critical | Engineering lead     |
| 5 | Disk-space pre-flight in agent before download (don't fill the disk and brick the box).   | High     | Appliance team       |
| 6 | Maintenance-window enforcement in the agent (config fields exist, executor ignores them). | High     | Appliance team       |
| 7 | Build pipeline: where does `build-release.py` actually run, and who has the private key?  | High     | Engineering lead     |
| 8 | OVA snapshot/export procedure as a runnable script (today it's prose in §16 of doc 12).   | Medium   | Appliance team       |
| 9 | A demonstration release that exercises a *system-level* step end-to-end (proves §7).      | Medium   | Engineering lead     |
| 10| Audit log shipped to zentryc.com on every register/update event (nice-to-have for v1).    | Low      | Remote-server team   |

§6–§11 below tell each owner exactly what to build.

### 3.3 Doc drift fixed in this revision

- `11-SERVER-SIDE-IMPLEMENTATION-GUIDE.md` §6.4 has been updated through two key rotations and now reflects the active production key as of 2026-05-03 (`MCowBQYDK2VwAyEAhwZpk2+cPN57lhIbcsPAI3Xtx9MyfMPM5m3Ny81swF8=`, sha256 `58a71bf2a5eb37af460616ce7c6eafdcf0d52d4d6a18932e788fd7a602b70e57`, 113 B canonical). The forensic `.pub.old` artifacts referenced in earlier revisions have been removed from `updater/keys/`; rotation history lives in git only. See §6 below for what triggered each rotation.

---

## 4. The customer experience

Two flows, end to end. Every other "how does the customer do X" reduces to one of these.

### 4.1 Flow A — Initial setup (out of the box)

1. Customer imports the `zenplus-<version>.ova` into VMware/Hyper-V/Proxmox.
2. They power it on. DHCP grabs an address; the console banner shows that address.
3. They open `http://<address>` in a browser, log in as `admin / admin123`, and are forced to change the password.
4. Settings → **Subscription** → "Enter license key". They paste the key the sales/ops team handed them.
5. The dashboard `POST /api/v1/system/register` calls zentryc.com `POST /api/v1/appliances/register` with the license key. zentryc.com validates the key, checks the subscription has free slots, generates `appliance_id` + `api_key`, stores a bcrypt hash, and returns both plus the subscription metadata.
6. The appliance writes `agent.conf` (mode 0600), caches `subscription.json`, and arms `zenplus-updater.timer`.
7. Done. The customer never has to do anything else for OTA to work.

The console CLI is for installers who need static IP, custom hostname, or DNS pointing to the customer's internal resolvers before the dashboard is reachable. None of those steps are required if DHCP is fine.

### 4.2 Flow B — Receiving an update

1. The timer fires (every ~4h, ±5m). The agent posts `/appliances/checkin` with full inventory.
2. zentryc.com runs the rollout engine (`11-SERVER-SIDE-IMPLEMENTATION-GUIDE.md` §9) and either replies `{"next_action": "none"}` or returns a release object.
3. If a release came back, the agent acquires a lock, downloads the `.zup` (resumable, hashed), verifies the Ed25519 signature on `manifest.json`, verifies every file's SHA-256 from `checksums.sha256`, then runs the manifest's ordered steps under `updater/executor.py`.
4. Each step (apt install, systemd reload, migration, code replace, etc.) is committed in order; failure at any step triggers `rollback_steps` (typically `restore_backup` + `start_services`).
5. After the final `health_check` step, the agent rewrites `/opt/zenplus/.version` and reports `success` to zentryc.com. The dashboard "Updates" tab shows the new version and the changelog.
6. If three attempts fail in a row for the same release, the agent stops trying that release until a newer one is published. The fleet status on zentryc.com surfaces this for support.

The customer sees, in the worst case, a banner like *"Updating to 1.4.2…"* in the dashboard. Most updates take 60–180 seconds; updates that ship a new poller binary or apt package may take up to 5 minutes. The agent never reboots the appliance unless a step explicitly says so.

### 4.3 Failure mode the customer has to know about

If `/data` fills, the appliance will fail health checks and surface a critical banner. The Storage tab (`/api/v1/system/storage`) lets a customer admin add a disk and grow online. This is already in the appliance — see `bin/expand-storage.sh`. We document it in the support guide rather than the OTA flow, but it's relevant because it means we can't gracefully recover from "hard drive full" via OTA alone; the customer has to give the VM more disk first.

---

## 5. System-level updates — proving the channel can carry them

The single biggest professional-grade requirement is *"we can change the system, not just the application"*. Concretely that means a release should be able to:

- Install or remove an apt package (e.g., add `snmpd` for a future SNMP feature).
- Add a new systemd service unit and enable+start it (e.g., `zenplus-syslog.service`).
- Drop a config file into `/etc/` (e.g., `/etc/sysctl.d/99-zenplus.conf` for a kernel tunable).
- Run an arbitrary host-level script (e.g., add a polkit rule, run a one-off migration).
- Modify `/opt/zenplus/docker-compose.yml` and pull a new container image.
- Eventually, replace itself — i.e., update `updater/` modules and pick up new step types.

All of these are already supported by the agent. The proof is structural:

- `updater/systemd/zenplus-updater.service` runs as `User=root` (line 9). The polkit rule at `updater/polkit/50-zenplus-updater.rules` is *only* there so the FastAPI service (running as `zenplus`) can ask systemd to start the updater oneshot — root is a fait accompli for the updater itself.
- `updater/steps/os_package.py` registers `apt_update`, `apt_install`, `apt_remove`. It refuses to touch protected packages (`postgresql`, `nginx`, `clickhouse-server`, `systemd`, `libc6`, etc.) unless the protect-list is changed in code, which is itself update-able.
- `updater/steps/install_systemd.py` copies the unit, runs `daemon-reload`, optionally enables it.
- `updater/steps/install_config.py` writes to any path, with automatic `.pre-update` backup.
- `updater/steps/run_hook.py` executes any script bundled in `hooks/` with `cwd=/opt/zenplus` and `ZENPLUS_DIR` exported.

What's missing is **a release that demonstrates this**. Before we ship to a paying customer, we cut a v1.0.1 internal release whose only purpose is to add a noop apt package (e.g., `tree`) and a noop systemd timer that touches a sentinel file, then a v1.0.2 release that removes both. If those two transitions work cleanly on a fresh OVA on internal infrastructure, we have evidence — not just a design — that the channel does what we say it does.

The protected-package list (`os_package.py:PROTECTED_PACKAGES`) is intentionally conservative. To upgrade Postgres or ClickHouse via OTA we *don't* turn off the protection — we ship a hook script that performs the upgrade with proper backup/dump/restore choreography and accepts the risk explicitly. That separation is correct and we should keep it.

---

## 6. Cryptographic key management

This is the single area where one mistake makes the rest of the system meaningless. Ship-day rules:

### 6.1 Release-signing keypair

- **Type:** Ed25519. Already chosen (correct).
- **Generation:** New keypair created on the build VM via `cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey.generate()`. One-time per rotation. Future rotations should ideally happen on an air-gapped machine or hardware token (YubiHSM / Nitrokey HSM 2 / similar) — but until that infrastructure exists, on-VM generation followed by immediate off-VM escrow is the working procedure.
- **Active key (as of 2026-05-03):** sha256 `58a71bf2a5eb37af460616ce7c6eafdcf0d52d4d6a18932e788fd7a602b70e57`, 113 B canonical PEM. Signs all releases ≥ 1.2.1.
- **Private key location:** lives on the build VM at `/opt/zenplus/updater/keys/zentryc-release.key` (mode 0400, root-owned) **for as long as releases are being cut from this VM**. At least one off-VM escrow copy must exist at all times — the working procedure is to also write to `/home/net/zentryc-release.key` so the operator can `scp` it to durable storage immediately after generation. The "delete after every release" rule from the original plan revision is **suspended** until a hardware token / signing service makes the on-VM key file unnecessary in the first place. Removing the key requires explicit per-release authorization from the operator; never assume "shred after upload" is the default.
- **Public key location:** baked into every OVA at `/opt/zenplus/updater/keys/zentryc-release.pub`. Canonical source-tree copy lives at `/home/net/zen-mon/updater/keys/zentryc-release.pub` and is what gets included by every OVA bake. zentryc.com holds an independent copy for upload-time signature re-verification — this is what the build pipeline pushes against.
- **Signing scope:** `manifest.json` only. The manifest references every other artifact via SHA-256 in `checksums.sha256`, so signing the manifest implicitly signs the whole package.

> **Lessons from the 2026-05-02/03 rotation cycle.** We rotated three times in 18 hours: (1) original `7427d5a0…` retired because the matching private key had been previously shredded with no escrow; (2) replacement `342025db…` orphaned within minutes because the build pipeline shredded the working private key per the (now-revised) "delete after every release" rule before any escrow copy existed; (3) current `58a71bf2…` saved with explicit escrow path before signing 1.2.1. The revised rule above prevents repeats: never delete a private key without explicit operator authorization, and always write an escrow path alongside the working path on key generation.

### 6.2 Per-appliance API keys

- Generated by zentryc.com at registration time (256-bit hex via `secrets.token_hex(32)`).
- Returned in plaintext **once**, stored only as bcrypt hash on the server.
- Stored on the appliance at `/opt/zenplus/updater/config/agent.conf` (mode 0600, owned `zenplus:zenplus`).
- Revocable from the admin dashboard (sets `is_active=false` on the row; the appliance gets 403 on next checkin and surfaces "registration revoked" in the UI).
- Recovery: `POST /api/v1/system/reset-registration` (admin-only on the appliance) clears the local creds, customer pastes a new license key. zentryc.com admin must release the old slot first.

### 6.3 Key rotation

If the release-signing private key is compromised:

1. Generate a new keypair.
2. Cut release v(N+1) signed with the OLD key whose only step is `install_config` of the new public key over `/opt/zenplus/updater/keys/zentryc-release.pub`. Publish via the existing channel — appliances accept it because it's signed by the still-valid old key.
3. After 95% fleet rollout (visible in zentryc.com admin dashboard), rotate to signing with the new private key.
4. The 5% holdouts have to be re-registered manually (support-driven).

Plan this rotation drill. The first time you do it for real should not be the first time you do it.

### 6.4 TLS

- zentryc.com uses Let's Encrypt or a commercial cert — the appliance verifies via the system CA bundle (`verify_tls = true` in `agent.conf`).
- Pinning is **not** done. Rationale: customers run our appliances behind their own TLS-terminating proxies sometimes, and pinning would make those deployments fail. The Ed25519 manifest signature is the actual security boundary; TLS just buys us privacy and basic hijack resistance.

---

## 7. Remote server (zentryc.com) — the handoff

This is what you give the remote-server team. It is a derivative of `11-SERVER-SIDE-IMPLEMENTATION-GUIDE.md`, with the gaps from §3.2 closed and a few corrections.

### 7.1 What the appliance actually expects (canonical contract)

The agent code is the source of truth, not the documentation. From the code:

```
POST /api/v1/appliances/register
  Body:  { hostname, arch, os_version, current_version, registration_token }
  → 200: { appliance_id, api_key, subscription: { ... } }

POST /api/v1/appliances/checkin
  Headers: X-Appliance-ID, Authorization: Bearer <api_key>
  Body:  { hostname, arch, os_version, current_version, agent_version,
           uptime, services_status, disk: {total,used,free} }
  → 200: { next_action: "none" | "update", release: {...} | null,
           subscription: { ... } | null }

GET  /api/v1/updates/check?current_version=X&arch=Y
  Headers: X-Appliance-ID, Authorization: Bearer <api_key>
  → 200: { available: bool, release: {...} | null }

GET  /api/v1/updates/download/{release_id}
  Headers: X-Appliance-ID, Authorization: Bearer <api_key>
  → 200 (octet-stream) or 302 (redirect to pre-signed URL)

POST /api/v1/updates/report
  Headers: X-Appliance-ID, Authorization: Bearer <api_key>
  Body:  { release_id, status, from_version, to_version, error_message, log_data }
  → 200: { acknowledged: true }

GET  /api/v1/appliances/subscription
  Headers: X-Appliance-ID, Authorization: Bearer <api_key>
  → 200: { subscription: { ... } } | 404
```

Release object the agent expects (every one of these fields is consumed by `agent.py:download_and_extract`):

```json
{
  "id":             "uuid",
  "version":        "1.4.0",
  "changelog":      "...",
  "severity":       "critical|security|normal|optional",
  "package_url":    "https://zentryc.com/api/v1/updates/download/<id>",
  "package_sha256": "<64 hex chars>",
  "manifest_sig":   "<base64 ed25519 sig>",
  "size":           15234567
}
```

Subscription object (consumed by `system_updates.py:RemoteSubscription`, mirrored in `updater/config/subscription.json`):

```json
{
  "id": "uuid",
  "name": "Acme Corp",
  "plan": "pro|trial|enterprise",
  "max_appliances": 5,
  "max_devices":    500,
  "used_slots":     2,
  "available_slots":3,
  "is_active":      true,
  "is_expired":     false,
  "expires_at":     "2026-12-31T00:00:00Z",
  "days_remaining": 240
}
```

Anything not in this list is not consumed by the agent and is therefore *server-side internal* — the team can model it however they like.

### 7.2 Schema additions beyond doc 11

Doc 11 §3 covers `appliances`, `releases`, `rollout_policies`, `update_history`, `admin_users`, `audit_log`. Add these:

```sql
-- License keys = registration tokens. One subscription can issue many.
CREATE TABLE subscriptions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    plan            VARCHAR(40)  NOT NULL,
    max_appliances  INTEGER      NOT NULL DEFAULT 1,
    max_devices     INTEGER      NOT NULL DEFAULT 50,
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE,
    expires_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    notes           TEXT
);

CREATE TABLE registration_tokens (
    token_hash      VARCHAR(128) PRIMARY KEY,         -- bcrypt of the license key
    subscription_id UUID         NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    label           VARCHAR(120),                     -- human label, e.g. "Acme HQ"
    created_at      TIMESTAMPTZ  DEFAULT NOW(),
    consumed_at     TIMESTAMPTZ,                      -- nullable; null = unused
    consumed_by     UUID REFERENCES appliances(id),
    is_active       BOOLEAN      NOT NULL DEFAULT TRUE
);

-- Link an appliance to its subscription (denormalized for speed).
ALTER TABLE appliances ADD COLUMN subscription_id UUID REFERENCES subscriptions(id);
CREATE INDEX idx_appliances_subscription ON appliances(subscription_id);
```

Two design decisions baked into that schema:

1. **License keys are single-use.** The customer dashboard shows the key once and offers them a download. The remote API marks the token consumed at registration and refuses further use. This is the simplest correct model and matches what the agent already does (it never sends the key again after register; it uses the issued api_key thereafter). If you want re-use semantics later, add a `max_uses` column.
2. **A subscription has a quota of slots, not of keys.** `max_appliances` is on the subscription. The admin can issue 50 keys for a 10-slot subscription if they want; only 10 will succeed at register. This avoids a UX trap where ops has to keep generating new keys when they pre-print stickers or whatever.

### 7.3 Rollout-engine corrections

Doc 11 §9 has a `from_version is None or current >= from_version` check missing for cases where we ship a release that is *only* eligible for upgrades from a specific minimum. The actual rule the agent honors (it relies on the server to filter — agent does not re-check `min_version`):

```python
def get_available_update(appliance) -> Release | None:
    rel = latest_published_release_for(arch=appliance.arch)
    if rel is None:
        return None
    if semver_cmp(appliance.current_version, rel.version) >= 0:
        return None
    if rel.min_version and semver_cmp(appliance.current_version, rel.min_version) < 0:
        return None
    if appliance_already_succeeded(appliance.id, rel.id):
        return None
    if appliance_failed_count(appliance.id, rel.id) >= 3:
        return None
    if not rollout_active_for(rel):
        return None
    if not appliance_in_rollout_target(appliance, rollout_active_for(rel)):
        return None
    if subscription_invalid(appliance.subscription_id):
        return None      # expired / disabled — don't ship updates to them
    return rel
```

`subscription_invalid` is the new clause. An expired subscription does not lose access to the dashboard (we're not bricking customer data) — it loses access to *future updates*, which is the leverage the business wants.

### 7.4 What "initial setup" means on the server side

Doc 11 already describes both registration and update. The "initial setup" the user asked for is, on the server, simply:

1. The admin creates a subscription (UI or script).
2. The admin generates one or more registration tokens for that subscription.
3. The admin hands the token (= license key) to the customer via whatever channel (email, sales portal, etc.).

There is **no second protocol** for "initial setup" — the appliance's first call to `/appliances/register` *is* the initial setup. The reason this design works is that the appliance ships with the public key already embedded, so it can verify any update that comes back from zentryc.com regardless of whether registration has happened yet. Registration just gates *eligibility for* updates, not the *trust* of updates.

### 7.5 What "upgrade" means on the server side

The full §11 flow. Ops uploads a `.zup` via the admin dashboard or `build-release.py publish`, the server verifies the manifest signature against the public key on disk (sanity check — if this fails, somebody signed with the wrong key), stores the package in object storage, creates a release row, and waits for the admin to publish. The admin then creates a rollout policy starting at canary. The auto-promote loop in `check_rollout_health()` (doc 11 §9.2) walks the policy through canary → 10% → 100% over 24h windows unless the failure rate breaches `max_failure_pct`.

This is the upgrade pipeline. It is the same as the §11 design; nothing new here. The piece worth emphasizing for the remote-server team is that **`max_failure_pct=5` and `promote_after=24h` are appropriate defaults, not hardcoded values** — surface them in the UI for every release.

---

## 8. Build & release workflow on this VM

The build machine is the dev machine you're sitting at. From a clean checkout of `main`:

```bash
# 1. Sanity. Make sure the VM is clean and the agent works.
sudo zenplus status
sudo systemctl restart zenplus-updater.service && sudo journalctl -u zenplus-updater -f

# 2. Cut a release. Plug in the hardware token (or mount the encrypted volume
#    that holds zentryc-release.key). It must end up at:
#    /opt/zenplus/updater/keys/zentryc-release.key
sudo cp /run/media/...token.../zentryc-release.key /opt/zenplus/updater/keys/zentryc-release.key
sudo chmod 400 /opt/zenplus/updater/keys/zentryc-release.key

# 3. Build, sign, and publish.
sudo /opt/zenplus/venv/bin/python /opt/zenplus/scripts/build-release.py publish \
    --version 1.4.2 \
    --changelog "Add SNMP probe diagnostics; fix alert dedup race." \
    --severity normal \
    --rollout canary

# 4. IMMEDIATELY remove the private key from the build VM.
sudo shred -u /opt/zenplus/updater/keys/zentryc-release.key

# 5. Watch the canary stage on zentryc.com admin dashboard.
#    After 24h, auto-promote moves to 10%; after another 24h, to 100%.
#    If anything goes red, hit "Pause" or "Abort" from the admin UI.
```

Two non-obvious bits:

- `build-release.py` looks for the private key at a hardcoded path. That's fine *because we delete the key after every release*. The path is convenient for the script and dangerous as a permanent home — those properties combine.
- The current `build-release.py` excludes `updater/config/`, `updater/keys/`, `updater/logs/` from the `code/` directory it ships. That is correct — we don't want one customer's `agent.conf` overwriting another's.

A future improvement (not required for v1) is to move signing into a separate signing service that the build script calls over a localhost socket, so the private key never sits on disk even briefly. Track this as tech debt.

---

## 9. Pre-ship test matrix

Run all of these against a fresh OVA in our internal lab before declaring v1 ready.

### 9.1 Out-of-box

| # | Scenario                                                                     | Pass criteria                                                              |
|---|------------------------------------------------------------------------------|----------------------------------------------------------------------------|
| 1 | Power on; let it idle 5 min.                                                 | Console shows hostname + IP; dashboard at http://IP loads; admin/admin123 works. |
| 2 | Try to skip first-boot-init (e.g. delete `/var/lib/zenplus/.initialized`).   | API and poller refuse to start because `Requires=zenplus-first-boot.service`. |
| 3 | Set static IP via `zenplus-cli` then reboot.                                 | Address survives reboot. `ip a` shows the configured address.              |
| 4 | First login forces password change.                                          | `admin123` works once, then UI demands a new password before anything else.|

### 9.2 Registration

| # | Scenario                                                                                                  | Pass criteria                                                        |
|---|-----------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------|
| 5 | Paste a valid license key in Settings → Subscription.                                                     | UI shows registered status, `agent.conf` populated, timer enabled.    |
| 6 | Paste an invalid key.                                                                                     | UI shows error from server; no creds written.                         |
| 7 | Paste a valid key but subscription has zero free slots.                                                   | UI shows "subscription full"; no creds written.                       |
| 8 | Paste a valid key when already registered.                                                                | UI shows 409 with the existing appliance ID; no overwrite.            |
| 9 | Admin runs `POST /system/reset-registration`, customer pastes a new key.                                  | Registration succeeds with a new appliance ID. Old slot must be freed by the OTA admin separately (deliberate manual step). |

### 9.3 Update — application-level

| # | Scenario                                                                                                | Pass criteria                                                       |
|---|---------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------|
|10 | Publish a normal app-only release; trigger checkin via `sudo systemctl start zenplus-updater.service`.  | Dashboard shows new version, history record `success`, server fleet shows new version. |
|11 | Publish a release whose manifest fails signature verification (sign with wrong key).                    | Agent rejects with `SecurityError`, reports `failed`. Local code unchanged.|
|12 | Publish a release whose `manifest.json` SHA-256 in `checksums.sha256` is corrupt.                       | Agent rejects, reports `failed`. Local code unchanged.              |
|13 | Update with a step that fails (e.g., a hook that exits 1).                                              | Rollback runs (`restore_backup` + `start_services`); services back up; report `failed`. |
|14 | Update with a step that times out (e.g., a 200s migration with 30s step timeout).                       | Step is killed, rollback runs.                                       |
|15 | Network drops mid-download. Reconnect and retry on next timer fire.                                     | Resumed download verifies; no duplicate state in history.            |
|16 | `/data` has only 100MB free; release is 500MB.                                                          | **GAP**: agent currently downloads anyway. Must add disk-space pre-flight (§3.2 row 5). |

### 9.4 Update — system-level

| # | Scenario                                                                                                 | Pass criteria                                                       |
|---|----------------------------------------------------------------------------------------------------------|---------------------------------------------------------------------|
|17 | Release that adds an apt package via `apt_install` step.                                                 | Package installed. `dpkg -l <pkg>` confirms.                        |
|18 | Release that drops a new `.service` file via `install_systemd` and starts it via `start_services`.       | Service is enabled, started, surviving reboot.                      |
|19 | Release that runs a `run_hook` script which writes to `/etc/sysctl.d/`.                                  | sysctl applied; reboot keeps it.                                    |
|20 | Release that updates the `updater/` module itself (changes step handler), then a follow-up release that uses the new step type. | Both succeed; the new step type is recognized after the first update. |
|21 | Release that tries to remove a protected package (e.g., `nginx`).                                        | Step rejects with explicit error; rollback restores nothing because nothing changed. |

### 9.5 Rollback

| # | Scenario                                                                       | Pass criteria                                              |
|---|--------------------------------------------------------------------------------|------------------------------------------------------------|
|22 | Force a rollback at every step type at least once.                             | Each rollback path runs without secondary failure.         |
|23 | Rollback from a state where DB migration partially applied.                    | Migration's `IF NOT EXISTS` makes restore idempotent. (Confirms our migration discipline; flag any migration that doesn't follow this rule.) |

### 9.6 Console / out-of-band

| # | Scenario                                                                                  | Pass criteria                                              |
|---|-------------------------------------------------------------------------------------------|------------------------------------------------------------|
|24 | Lose network access to zentryc.com for 24h, then restore.                                 | Appliance keeps running; no updates applied; first checkin after restore picks up backlog. |
|25 | Customer expands the VM disk in the hypervisor, then clicks Storage → Grow Volume.        | `/data` reflects the new size; ClickHouse table sizes unchanged. |
|26 | Customer adds a second virtual disk, then clicks Storage → Add Disk.                      | LVM `vgextend` + `lvextend` + `resize2fs` complete; `/data` larger. |

A failure on any of 1–26 is a ship-blocker. Lab runs of all 26, captured as a checklist with timestamps and the running engineer's initials, become the artifact attached to the v1 release ticket.

---

## 10. Operational runbook (post-ship)

### 10.1 New customer onboarding

1. Sales hands customer email to ops.
2. Ops creates a subscription on zentryc.com (plan, max_appliances, expiry).
3. Ops generates one registration token per appliance the customer has.
4. Ops sends the customer the OVA download link plus the registration token(s) over a secure channel.
5. Customer follows the printed Quick-Start card. Done.

### 10.2 Customer reports "update is stuck"

1. Open the appliance's row in the admin dashboard. Read the `update_history` table for that appliance.
2. If `status=applying` for >15 min, the agent is mid-step. Ask the customer to send `/opt/zenplus/updater/logs/update.log` (the dashboard exposes this via the Updates tab).
3. If three consecutive `failed` rows for the same release, the agent has given up. Investigate the root cause; either ship a fixed release (next version bumps replace the failed one) or, in extremis, ssh in via support and rollback manually.

### 10.3 Customer reports "can't reach zentryc.com"

99% of the time, this is the customer's outbound firewall. Document the required egress: `tcp/443 zentryc.com` and that's it. The agent does not need any inbound port opened on the customer side.

### 10.4 Releasing a hotfix

1. `build-release.py publish --version <X.Y.Z+1> --severity security --rollout full --rollout-pct 100`
2. The rollout engine bypasses the staged ramp because we set `target_pct=100` and there's no canary stage.
3. Watch failure rate; if it goes above 5%, the existing auto-abort triggers.

### 10.5 Recovering a bricked appliance

In order of preference:

1. From the dashboard's Updates tab, click "Roll back to previous version" (already implemented locally via the `restore_backup` step; needs a UI button — open issue).
2. If the dashboard is down, ssh in as `zpsupport`, run `sudo /opt/zenplus/venv/bin/python -m updater --rollback` (this CLI subcommand needs to be added — open issue).
3. Last resort: re-import the OVA and have the customer paste a fresh registration token. They lose runtime data unless they have backups; their *configuration* (devices, alerts, users) lives in Postgres which is unaffected by re-import only if they took an external backup.

A nightly Postgres dump to a customer-controlled location is on the v1.1 roadmap for exactly this reason.

---

## 11. Pre-ship checklist (the OVA snapshot day)

Run on the build VM, in this order. Do not skip any step.

```bash
# 1. Code is green, tests pass, lab matrix in §9 is signed off.

# 2. Fetch latest main, rebuild, ensure all services healthy.
cd /opt/zenplus
sudo git fetch origin && sudo git reset --hard origin/main
sudo zenplus update           # rebuilds Go, dashboard, pip deps, restarts services
sudo zenplus status           # everything green

# 3. Make sure the public key on disk is the production one.
sudo cat /opt/zenplus/updater/keys/zentryc-release.pub
# Must match: MCowBQYDK2VwAyEAhwZpk2+cPN57lhIbcsPAI3Xtx9MyfMPM5m3Ny81swF8=
# sha256: 58a71bf2a5eb37af460616ce7c6eafdcf0d52d4d6a18932e788fd7a602b70e57
# 113 bytes canonical PEM (BEGIN/END/body each terminated by LF)

# 4. Clear all per-appliance state. (first-boot-init.sh re-creates this.)
sudo systemctl stop zenplus-api zenplus-poller zenplus-dashboard zenplus-updater.timer
sudo docker compose -f /opt/zenplus/docker-compose.yml down
sudo rm -f /opt/zenplus/.env
sudo rm -f /var/lib/zenplus/.initialized
sudo rm -f /opt/zenplus/updater/logs/update.log
sudo rm -f /opt/zenplus/updater/logs/update-history.json
sudo rm -f /opt/zenplus/updater/config/subscription.json
# Blank api_key + id in agent.conf
sudo sed -i -E 's|^(id[[:space:]]*=).*|\1|; s|^(api_key[[:space:]]*=).*|\1|' /opt/zenplus/updater/config/agent.conf
sudo truncate -s 0 /var/log/zenplus/first-boot.log 2>/dev/null || true

# 5. Clear shell histories and SSH host keys.
sudo truncate -s 0 /root/.bash_history /home/*/.bash_history 2>/dev/null
sudo rm -rf /home/*/.claude /root/.claude 2>/dev/null
sudo rm -f /etc/ssh/ssh_host_*
sudo truncate -s 0 /etc/machine-id

# 6. Vacuum journals, zero free space, prepare for compression.
sudo journalctl --rotate
sudo journalctl --vacuum-time=1s
sudo apt clean
sudo dd if=/dev/zero of=/EMPTY bs=1M status=none 2>/dev/null || true
sudo rm -f /EMPTY

# 7. Run the verifier. It refuses to pass if any of the above was missed.
sudo /opt/zenplus/bin/verify-ship-ready.sh
# === READY TO SNAPSHOT === must appear.

# 8. Shutdown.
sudo shutdown -h now

# 9. From the hypervisor, snapshot then export OVA.
#    Name: zenplus-<X.Y.Z>.ova
```

---

## 12. Roadmap (post-v1, in priority order)

| Item                                                                              | Why                                                          |
|-----------------------------------------------------------------------------------|--------------------------------------------------------------|
| Disk-space pre-flight in agent (§3.2 row 5)                                       | Prevents the obvious self-inflicted brick.                   |
| Maintenance-window enforcement in agent (§3.2 row 6)                              | Customers in regulated environments need it.                 |
| `zenplus-updater --rollback` CLI subcommand                                       | Currently rollback only happens automatically.               |
| "Roll back" button in the dashboard Updates tab                                   | Same.                                                        |
| Nightly Postgres dump to S3-compatible storage (per-customer config)              | Only durable disaster recovery story.                        |
| Move signing to a localhost socket service so the private key never lands on disk | Hardening; current rule of "delete after each release" works but is human-error-prone. |
| Multi-arch builds (arm64) once we have a customer that wants it                   | Schema and agent already arch-aware; just need the build job.|
| Audit-log shipping from appliance to zentryc.com                                  | Support visibility into what happened on the customer's box. |

---

## 13. Decision log (so we don't relitigate these)

- **Deb packages, not Snap or Flatpak.** Customers don't run unattended Snap.
- **Ed25519, not RSA, not ECDSA.** Smaller, faster, fewer footguns.
- **Pre-built artifacts in the .zup, not "compile on the appliance".** Avoids npm/Go on customer machines. Faster, more reliable, reproducible.
- **System-level changes go through the OTA channel, not a separate "system update" channel.** One pipeline, one set of guarantees, one set of incidents to learn from.
- **The agent runs as root.** That's the only way to do system-level changes. We compensate with signing + per-file SHA-256 + protected-package list + rollback + per-step audit log, not with privilege separation.
- **License key issuance is server-driven and single-use.** Re-use semantics are easy to add later; un-doing them is hard.
- **The OVA bakes the public key.** Re-fetching at first boot from zentryc.com would be a chicken-and-egg vulnerability.
- **Failure of three update attempts in a row halts retries for that release.** Forces a human to look at it instead of churning the disk and the support team.
- **No SSH for customers by default.** The console CLI (`zenplus-cli`) and the web UI cover everything. A `zpsupport` user exists for our team, with credentials shared via support tooling, not baked in.

---

*Document version: 1.0 | Updated: 2026-05-02 | Owner: ZenPlus Engineering*
