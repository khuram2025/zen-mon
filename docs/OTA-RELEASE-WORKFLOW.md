# Appliance Build → Zentryc OTA Release Workflow

> **Purpose of this document**
> This describes, end-to-end, how the **ZenPlus** appliance builds a release, pushes it to the
> central **Zentryc** OTA server (`https://zentryc.com`), and how appliances in the field pull and
> apply it automatically. It is written so that **another product team (e.g. the Logs appliance)**
> can replicate the same mechanism for their own product with their own signing keys, services, and
> release channel.
>
> Read the two parts:
> - **Part A – How ZenPlus does it today** (concrete, copy-paste reference).
> - **Part B – How to stand up the same workflow for a new appliance (Logs)** — what to copy,
>   what to change, and the checklist.

---

## 1. The big picture

```
   ┌──────────────────────────┐         build + sign          ┌───────────────────────────┐
   │  BUILD APPLIANCE          │   scripts/build-release.py    │   ZENTRYC OTA SERVER       │
   │  (your dev/release box,   │ ────────────────────────────► │   https://zentryc.com      │
   │   runs as `zenplus` user) │   publish (upload .zup +      │                            │
   │                           │   manifest signature)         │  - stores releases         │
   │  - source code            │                               │  - admin auth (JWT)        │
   │  - Ed25519 PRIVATE key    │                               │  - rollout policies        │
   │  - dashboard/poller/etc   │                               │  - per-appliance check-in  │
   └──────────────────────────┘                               └───────────┬───────────────┘
                                                                           │  release available?
                                       check-in every 4h (systemd timer)   │  (next_action=update)
                                                                           ▼
                                                            ┌───────────────────────────────┐
                                                            │  FIELD APPLIANCE(S)            │
                                                            │  updater/agent.py              │
                                                            │  - Ed25519 PUBLIC key (verify) │
                                                            │  - api_key (registered)        │
                                                            │  - downloads .zup, verifies    │
                                                            │    sha256 + signature          │
                                                            │  - runs manifest steps         │
                                                            │  - reports status back         │
                                                            └───────────────────────────────┘
```

Three independent pieces, each owned by a different actor:

| Piece | What it is | Who runs it |
|-------|-----------|-------------|
| **Release builder** | `scripts/build-release.py` — packages code into a signed `.zup` and uploads it | The release engineer, on the build box, **as the `zenplus` user** |
| **Zentryc OTA server** | A hosted service exposing `/api/v1/admin/releases/*` (publish) and `/api/v1/appliances/*` + `/api/v1/updates/*` (consume) | Platform team (shared infra) |
| **Updater agent** | `updater/agent.py`, driven by a `systemd` timer on every appliance | Each deployed appliance, automatically |

The trust anchor is an **Ed25519 keypair**:
- **Private key** (`updater/keys/zentryc-release.key`) lives *only* on the build box, signs `manifest.json`.
- **Public key** (`updater/keys/zentryc-release.pub`) ships inside every appliance and verifies the signature before any step runs.

A release the appliance can't verify is rejected — so the private key is the single most important secret in the whole system.

---

# PART A — How ZenPlus does it today

## 2. The release package (`.zup`)

A `.zup` is just a `tar.gz` produced by `build_package()`. Its layout:

```
update-<version>.zup
├── manifest.json            # ordered list of steps + metadata (signed)
├── manifest.json.sig        # Ed25519 signature over manifest.json
├── checksums.sha256         # sha256 of every other file in the package
├── code/                    # source tree applied onto /opt/zenplus
│   ├── server/  poller/  scripts/  support/  updater/
│   ├── .version  docker-compose.yml
├── dashboard-dist.tar.gz    # prebuilt Vite bundle (extracted to dashboard/dist)
├── go-binaries/             # compiled poller, netflow-collector
├── sensor-artifacts/        # remote-sensor binary + its own manifest
├── requirements.txt         # pip deps installed during update
└── migrations/              # ONLY when --include-migrations is passed
```

### What gets included / excluded (important)
- **Included code dirs**: `server`, `poller`, `scripts`, `support`, plus `updater/` and root files
  `.version`, `docker-compose.yml`.
- **Always excluded** (`CODE_IGNORE`): `__pycache__`, `*.pyc`, `node_modules`, `venv`/`.venv`,
  `dist`, `build`, caches, and the support runtime dirs `requests/`, `jobs/`, `bundles/`.
- **Updater is shipped but its appliance-local state is NOT**: `config/` (`agent.conf` holds the
  api_key, `subscription.json` is per-appliance), `keys/` (private key!), `logs/`, `backups/`.
  > ⚠️ This exclusion is what stops the **private signing key and one appliance's api_key from
  > leaking into every other appliance**. Preserve it exactly when you copy this for Logs.
- **Migrations are opt-in.** Historical migrations are not all safe to re-run, so a normal
  code-only release ships **no** migrations. You must pass `--include-migrations --migration FILE`
  explicitly (see §5).

## 3. The manifest = the update recipe

`build_package()` writes an ordered `steps` array into `manifest.json` (`format_version: 2`). The
appliance executor runs them in order. The current ZenPlus recipe is:

1. `stop_services` — `zenplus-api`, `zenplus-poller`, `zenplus-netflow-collector`
2. `backup` — `code` + `database` (used by `rollback_steps` if anything fails)
3. `apt_install` — heal OS prerequisites (`snmp`, `iputils-ping`); idempotent
4. `apply_code` — replace `/opt/zenplus` code from `code/`
5. `run_hook` — `setup-support.sh`, `fetch-geoip.py` (idempotent, best-effort)
6. `pip_install` — from `requirements.txt`
7. `run_migration` (per migration, only if packaged) — then `install_config` to drop the `.sql`
   into `/opt/zenplus/scripts/`
8. `build_dashboard` — extract the prebuilt `dashboard-dist.tar.gz`
9. `install_binary` / `install_systemd` — Go poller, netflow collector + its unit, remote sensor
10. `start_services` — api, poller, netflow, nginx, etc.
11. `health_check` — `GET http://localhost:8000/api/v1/system/health` (30s)

`rollback_steps` = `restore_backup` then `start_services`, so a failed update self-heals to the
previous version.

> **Note on migrations** — ClickHouse migrations also auto-apply on every update via
> `updater/clickhouse_sync.py` (it keeps a `zenplus.schema_migrations` ledger). But that only runs
> from the *already-installed* updater, so the release that **first introduces** a CH migration must
> still package it explicitly. Postgres migrations are always opt-in per release. See
> `docs`/memory notes on `clickhouse-migrations-auto-apply`.

## 4. Build & publish — the commands

**Where & who:** on the build box, in `/opt/zenplus`, **as the `zenplus` user** (so the private
key, owned `zenplus:zenplus 0400`, is readable to sign).

### One-shot (build + publish):
```bash
sudo -u zenplus /opt/zenplus/server/venv/bin/python scripts/build-release.py publish \
  --version 1.2.28 \
  --changelog "Device availability timeline; host-metric alerting" \
  --severity normal
```

### Two-step (build, inspect, then publish the artifact):
```bash
# 1) Build only — produces /tmp/zenplus-releases/update-1.2.28.zup (+ .meta.json)
sudo -u zenplus python3 scripts/build-release.py build \
  --version 1.2.28 --changelog "..." --severity normal

# 2) Publish the already-built file
sudo -u zenplus /opt/zenplus/server/venv/bin/python scripts/build-release.py publish \
  --file /tmp/zenplus-releases/update-1.2.28.zup --version 1.2.28 --changelog "..."
```

### Other commands
```bash
# List releases on the server
sudo -u zenplus server/venv/bin/python scripts/build-release.py list

# Staged rollout (canary → percentage → full) instead of 100% immediately
... publish ... --rollout canary --rollout-pct 10 --rollout-group beta
# or for an existing release:
scripts/build-release.py rollout --version 1.2.28 --stage canary --pct 10
```

### Useful flags
| Flag | Meaning |
|------|---------|
| `--skip-dashboard` | Don't rebuild the Vite bundle (faster code-only Go/py release) |
| `--skip-go` | Don't rebuild Go binaries |
| `--include-migrations` + `--migration FILE` (repeatable) | Package schema migrations |
| `--min-version` | Refuse to apply on appliances older than this |
| `--severity` | `normal` / `security` / `critical` (drives appliance urgency) |

## 5. Shipping a schema migration

Migrations are **append-only once released** and tracked in `scripts/migrations.lock` (a sha256 of
every migration that has ever shipped). The build **lints** migrations against this lock and fails
if a previously-shipped file changed.

```bash
# 1) Register a brand-new migrate-*.sql in the lockfile
sudo -u zenplus python3 scripts/build-release.py lint-migrations --update-lock

# 2) Build/publish WITH the migration explicitly listed
sudo -u zenplus python3 scripts/build-release.py build --version 1.2.28 \
  --include-migrations --migration migrate-013-foo.sql
```
Engine is auto-detected per file (`*-clickhouse.sql` → ClickHouse, else Postgres).

## 6. The known gotchas (learned the hard way)

These cost real time on past releases — bake them into your runbook:

1. **Run as `zenplus`.** The signing key is `zenplus:zenplus 0400`. Building as another user logs
   `WARNING: No private key ... manifest unsigned!` and every appliance will then **reject** the
   release.
2. **`publish` needs `httpx`**, which is only in `server/venv`, not system python. Use
   `server/venv/bin/python` for `publish`/`list`/`rollout`. `build` (no httpx) can use `python3`.
3. **Admin creds.** `get_admin_token()` reads `~/.zenplus-admin-creds` (JSON `{email,password}`,
   chmod 600) of the *running* user. For the `zenplus` user that's `/opt/zenplus/.zenplus-admin-creds`.
   If you copy one in, **delete it afterward** — it's creds-at-rest in the repo root.
4. **Ownership of scratch dirs.** `/tmp/zenplus-releases` and `.git/objects` left owned by
   `root`/another user from past runs cause permission errors — `chown -R zenplus:zenplus` them first.
5. **`npm run build` is broken** (`tsc -b` fails on pre-existing type errors). The builder already
   uses `npx vite build` directly — that's expected, not a regression. (This is also why a manual
   dashboard rebuild uses `vite build`, run **as the `zenplus` user** since `dashboard/dist` is owned
   by `zenplus`.)

## 7. How the appliance consumes it (updater agent)

Config: `updater/config/agent.conf` (INI), secured 600 because it holds the `api_key`. Defaults
come from `updater/config.py`:

- `server.url = https://zentryc.com`
- `server.check_interval_seconds = 900` (timer overrides cadence; see below)
- `appliance.id` / `appliance.api_key` — issued at registration
- `security.public_key_path = updater/keys/zentryc-release.pub`
- `security.verify_tls = true`

**Lifecycle:**
1. **Register once** — `register()` POSTs `/api/v1/appliances/register` with a **license key**
   (registration token from the Zentryc subscription page) + host inventory; server returns
   `appliance_id` + `api_key`, persisted to `agent.conf`.
2. **Check-in on a timer** — `zenplus-updater.timer` (`OnBootSec=5min`, `OnUnitActiveSec=4h`,
   randomized delay) runs `zenplus-updater.service` → `checkin()` POSTs `/api/v1/appliances/checkin`.
   If the response has `next_action == "update"` with a `release`, the agent proceeds.
3. **Download + verify** — `download_and_extract()` pulls `package_url`, checks `package_sha256`,
   then verifies `manifest.json.sig` against the **public key**. Any mismatch aborts.
4. **Apply** — `run_update()` executes the manifest steps; on failure runs `rollback_steps`.
5. **Report** — `report_status()` writes local history (powers the dashboard Updates tab) **and**
   POSTs `/api/v1/updates/report` so the server tracks per-appliance success/failure.

**Server API surface the appliance uses** (all under the OTA server base URL):
`/api/v1/appliances/register`, `/checkin`, `/subscription`; `/api/v1/updates/check`, `/report`.
Admin/publish side: `/api/v1/admin/auth/login`, `/api/v1/admin/releases/create`,
`/api/v1/admin/releases/{id}/publish`, `/api/v1/admin/releases`, `/api/v1/admin/rollouts`.

---

# PART B — Standing up the same workflow for a NEW appliance (Logs)

The Logs team wants the *same* round-trip: build a signed package, push to Zentryc, and have Logs
appliances auto-pull. You do **not** need to rewrite the mechanism — you need to **fork the three
pieces with your own identity**. Below is exactly what to copy, what to change, and the order to do
it.

## B1. Decide your identity & namespace

| Concept | ZenPlus value | Pick yours (Logs) |
|---------|---------------|-------------------|
| Product/install dir | `/opt/zenplus` | e.g. `/opt/zenlogs` |
| Run-as user | `zenplus` | e.g. `zenlogs` |
| Signing keypair | `zentryc-release.key/.pub` | **new keypair** — never reuse ZenPlus's |
| Services managed by manifest | `zenplus-api`, `zenplus-poller`, … | your own units |
| Updater units | `zenplus-updater.{service,timer}` | `zenlogs-updater.{service,timer}` |
| `.version` file | `/opt/zenplus/.version` | `/opt/zenlogs/.version` |
| Release scratch | `/tmp/zenplus-releases` | `/tmp/zenlogs-releases` |
| OTA server | `https://zentryc.com` | same server, **separate product/channel** |

> **Talk to the Zentryc platform team first** (see B5): they must provision your product as a
> separate release channel so Logs releases never get offered to ZenPlus appliances and vice-versa.
> Appliances are matched by their registered product/license — confirm how that scoping is keyed on
> the server before you publish anything.

## B2. Generate your OWN signing keypair

Never reuse the ZenPlus key. Ed25519, same scheme. `updater/crypto.py` has a generator, or via CLI:

```bash
# Private (sign) — keep ONLY on the build box, owned by your release user, mode 0400
openssl genpkey -algorithm ed25519 -out zenlogs-release.key
# Public (verify) — ships inside every appliance
openssl pkey -in zenlogs-release.key -pubout -out zenlogs-release.pub

sudo install -o zenlogs -g zenlogs -m 0400 zenlogs-release.key /opt/zenlogs/updater/keys/
sudo install -o zenlogs -g zenlogs -m 0644 zenlogs-release.pub /opt/zenlogs/updater/keys/
```
- Build box: **private + public**.
- Appliance image: **public only**. The private key must never be in a `.zup` (the updater copy
  step already excludes `keys/` and `*.key` — keep that exclusion).

## B3. Fork the builder (`scripts/build-release.py`)

It's already parameterized by env vars — most changes are configuration, not code:

| Env var | Default | Set to (Logs) |
|---------|---------|---------------|
| `ZENPLUS_DIR` | `/opt/zenplus` | `/opt/zenlogs` |
| `ZENPLUS_RELEASE_DIR` | `/tmp/zenplus-releases` | `/tmp/zenlogs-releases` |
| `ZENPLUS_RELEASE_PRIVATE_KEY` | `updater/keys/zentryc-release.key` | your `.key` path |
| `ZENPLUS_RELEASE_SERVER_URL` | `https://zentryc.com` | same (or staging) |

Then edit these **in-code** lists to match your product:
- `CODE_DIRS` / `CODE_FILES` — the source dirs your appliance actually ships (Logs won't have
  `poller`/`netflow`; it'll have its own log-ingest services).
- The **manifest `steps`** in `build_package()` — replace `stop_services`/`start_services` service
  names, the Go `install_binary`/`install_systemd` steps, and the `health_check` URL with your
  product's services and health endpoint.
- The dashboard/Go/sensor build blocks — drop the ones Logs doesn't have (`--skip-go` etc., or
  remove the blocks).
- `get_admin_token()` creds filename if you don't want to share `.zenplus-admin-creds`.

Keep unchanged (they're the security/quality backbone): manifest signing, `checksums.sha256`,
the migrations lockfile/lint, the updater-state exclusions, and `format_version: 2`.

## B4. Fork the updater agent (`updater/`)

Copy the `updater/` package into the Logs tree and change:
- `updater/config.py` defaults: `server.url`, `public_key_path`, install dir, your service names.
- `agent.conf` location & the units `zenlogs-updater.service` / `.timer` (copy
  `updater/systemd/zenplus-updater.{service,timer}`, rename, repoint `ExecStart` to your agent).
- The executor step handlers (`updater/steps/`, `executor.py`) only if you add new step *types*.
  If Logs uses the same step vocabulary (stop/apply_code/pip_install/install_binary/…), no change.
- `clickhouse_sync.py` — keep if Logs uses ClickHouse; otherwise remove the call from
  `run_update()`.

Then on the appliance image: install the public key, enable the timer:
```bash
systemctl enable --now zenlogs-updater.timer
```

## B5. Coordinate with the Zentryc (server) side

The build box only *uploads*; the server decides which appliance sees which release. Before first
publish, confirm with the platform team:
1. **A separate product/channel** for Logs so releases are scoped (Logs appliances ≠ ZenPlus).
2. **An admin account** for Logs releases (used by `get_admin_token()` →
   `/api/v1/admin/auth/login`). Confirm the `/api/v1/admin/releases/*` endpoints accept your
   product's uploads.
3. **Registration/licensing**: how a Logs appliance's `registration_token` (license key) maps to a
   subscription, so `/api/v1/appliances/register` issues an `api_key` scoped to Logs.
4. That the server stores/serves **your** `manifest_sig` and `package_sha256` unmodified (the
   appliance re-verifies both).

## B6. First-release checklist (Logs)

- [ ] New Ed25519 keypair generated; **private** on build box (0400, release user), **public** baked
      into the appliance image.
- [ ] `build-release.py` env vars + `CODE_DIRS`/manifest service names/health URL adjusted.
- [ ] Updater forked: `config.py` defaults, renamed systemd units, public key path.
- [ ] Zentryc provisioned Logs as a separate channel + admin creds + license/registration mapping.
- [ ] Admin creds file present for the release user (`~/.<product>-admin-creds`, 0600) — **deleted
      after** if it sat in the repo.
- [ ] Scratch dirs (`/tmp/zenlogs-releases`, `.git/objects`) owned by the release user.
- [ ] `migrations.lock` initialized (`lint-migrations --update-lock`) if Logs ships migrations.
- [ ] Dry run: `build` only, inspect the `.zup` (`tar tzf`), confirm `manifest.json.sig` present and
      **no** `keys/`, `agent.conf`, or `subscription.json` inside.
- [ ] Register ONE test appliance, publish to a **canary** rollout, confirm it pulls, verifies,
      applies, and `report`s success before going to `full`.

## B7. Mental model to hand the Logs team

> "The build box signs a tarball with a private key and uploads it to Zentryc. Every appliance has
> the matching public key, checks in on a timer, downloads the tarball, verifies the signature and
> checksum, runs an ordered list of steps from a manifest, and reports back. To make this yours:
> generate your own keypair, point the builder and the updater at your install dir / services /
> server channel, and ask the platform team to give Logs its own release channel and admin login.
> Everything else — packaging, signing, verification, rollback, migrations — you inherit unchanged."

---

## Appendix — file map (ZenPlus, for reference)

| Path | Role |
|------|------|
| `scripts/build-release.py` | Builder + publisher CLI (`build`/`publish`/`list`/`rollout`/`lint-migrations`) |
| `scripts/migrations.lock` | Append-only sha256 ledger of shipped migrations |
| `updater/keys/zentryc-release.key` | **Private** signing key (build box only, 0400) |
| `updater/keys/zentryc-release.pub` | **Public** verify key (ships in appliance) |
| `updater/agent.py` | Register / check-in / download+verify / apply / report |
| `updater/config.py` + `config/agent.conf` | Agent config (server url, api_key, public key path) |
| `updater/executor.py`, `updater/steps/` | Manifest step handlers |
| `updater/clickhouse_sync.py` | Auto-applies pending ClickHouse migrations on update |
| `updater/systemd/zenplus-updater.{service,timer}` | The 4-hourly check-in driver |
| `.version` | Current installed version (compared on check-in) |
