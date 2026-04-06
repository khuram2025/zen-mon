# ZenPlus OTA Update System

## Overview

A professional, secure over-the-air update system for ZenPlus network monitoring appliances. A central server at `https://zentryc.com` manages releases, rollouts, and fleet tracking. Each appliance runs a lightweight update agent that checks for updates, downloads, verifies, applies, and reports results.

```
┌─────────────────────┐         HTTPS          ┌──────────────────────┐
│   ZenPlus Appliance │ ◄────────────────────► │   zentryc.com        │
│                     │   check/download/report │                      │
│  zenplus-updater    │                         │  Update API Server   │
│  (systemd timer)    │                         │  Admin Dashboard     │
│                     │                         │  Package Storage     │
└─────────────────────┘                         └──────────────────────┘
       × N appliances                              1 central server
```

## Update Package Format (.zup)

An update bundle is a compressed archive (`.zup` = tar.gz) with a deterministic internal structure:

```
update-1.4.0.zup
├── manifest.json            # Package metadata and ordered steps
├── manifest.json.sig        # Ed25519 detached signature
├── checksums.sha256         # SHA-256 of every file in the package
├── code/                    # Changed source files (full replacement)
├── dashboard-dist.tar.gz   # Pre-built React dashboard
├── go-binaries/            # Pre-compiled Go binaries (arch-specific)
│   └── zenplus-poller
├── requirements.txt         # New/changed pip dependencies
├── migrations/              # SQL migration files
│   ├── pg-003-xxx.sql
│   └── ch-002-xxx.sql
├── systemd/                 # New/changed systemd units
│   └── zenplus-newservice.service
├── configs/                 # Config file templates
│   └── nginx-zenplus.conf
└── hooks/                   # Lifecycle scripts
    ├── pre-update.sh
    └── post-update.sh
```

## Manifest Format

```json
{
  "format_version": 2,
  "update_id": "550e8400-e29b-41d4-a716-446655440000",
  "version": "1.4.0",
  "from_version": null,
  "min_version": "1.2.0",
  "release_date": "2026-04-06T12:00:00Z",
  "changelog": "Added SNMP v3 support, fixed alert dedup bug",
  "severity": "normal",
  "arch": "amd64",
  "os_min": "ubuntu-22.04",
  "size_bytes": 15234567,
  "checksum_sha256": "abc123...",
  "steps": [
    {"type": "stop_services", "services": ["zenplus-api", "zenplus-poller"]},
    {"type": "backup", "targets": ["code", "database"]},
    {"type": "apply_code", "method": "replace", "source": "code/"},
    {"type": "pip_install", "requirements": "requirements.txt"},
    {"type": "run_migration", "engine": "postgres", "file": "migrations/pg-003-xxx.sql"},
    {"type": "run_migration", "engine": "clickhouse", "file": "migrations/ch-002-xxx.sql"},
    {"type": "build_dashboard", "prebuilt": true, "source": "dashboard-dist.tar.gz"},
    {"type": "install_binary", "source": "go-binaries/zenplus-poller", "dest": "/opt/zenplus/bin/zenplus-poller"},
    {"type": "install_systemd", "source": "systemd/zenplus-newservice.service"},
    {"type": "install_config", "source": "configs/nginx-zenplus.conf", "dest": "/etc/nginx/conf.d/zenplus.conf"},
    {"type": "run_hook", "script": "hooks/post-update.sh"},
    {"type": "start_services", "services": ["zenplus-api", "zenplus-poller", "zenplus-newservice", "nginx"]},
    {"type": "health_check", "url": "http://localhost:8000/api/v1/system/health", "timeout": 30}
  ],
  "rollback_steps": [
    {"type": "restore_backup"},
    {"type": "start_services", "services": ["zenplus-api", "zenplus-poller", "nginx"]}
  ]
}
```

## Security Model

### Signing
- Master Ed25519 keypair; private key stored offline/HSM
- Public key embedded on every appliance at `/opt/zenplus/updater/keys/zentryc-release.pub`
- `manifest.json` is signed; agent verifies before processing
- All files listed in `checksums.sha256`, referenced by manifest

### Transport
- All communication over HTTPS (TLS 1.2+) to `https://zentryc.com/api/v1/updates/...`

### Authentication (per-appliance)
- Each appliance has a unique `appliance_id` (UUID) and `api_key` (256-bit random token)
- Generated during registration, stored at `/opt/zenplus/updater/config/agent.conf`
- Every request includes `Authorization: Bearer <api_key>` and `X-Appliance-ID: <uuid>`
- Server validates both; keys can be revoked

### Additional Hardening
- Update packages have max age; agent rejects manifests older than 30 days
- All hook scripts SHA-256 verified before execution
- Agent runs as root (needed for systemd) but drops privileges for downloads

## Server-Side Components (zentryc.com)

### Database Schema

```sql
-- Appliance registry
CREATE TABLE appliances (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    api_key_hash    VARCHAR(128) NOT NULL,
    hostname        VARCHAR(255),
    ip_address      INET,
    arch            VARCHAR(20) DEFAULT 'amd64',
    os_version      VARCHAR(50),
    current_version VARCHAR(20),
    agent_version   VARCHAR(20),
    last_checkin    TIMESTAMPTZ,
    registered_at   TIMESTAMPTZ DEFAULT NOW(),
    is_active       BOOLEAN DEFAULT TRUE,
    tags            JSONB DEFAULT '[]',
    metadata        JSONB DEFAULT '{}',
    rollout_group   VARCHAR(50) DEFAULT 'stable'
);

-- Update releases
CREATE TABLE releases (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version         VARCHAR(20) NOT NULL UNIQUE,
    min_version     VARCHAR(20),
    changelog       TEXT,
    severity        VARCHAR(20) DEFAULT 'normal'
                    CHECK (severity IN ('critical','security','normal','optional')),
    arch            VARCHAR(20) DEFAULT 'amd64',
    package_url     VARCHAR(500) NOT NULL,
    package_size    BIGINT,
    package_sha256  VARCHAR(64) NOT NULL,
    manifest_sig    TEXT NOT NULL,
    is_published    BOOLEAN DEFAULT FALSE,
    published_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by      VARCHAR(100)
);

-- Rollout policies
CREATE TABLE rollout_policies (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id      UUID NOT NULL REFERENCES releases(id),
    stage           VARCHAR(20) NOT NULL
                    CHECK (stage IN ('canary','percentage','full','paused','aborted')),
    target_group    VARCHAR(50),
    target_pct      INTEGER DEFAULT 100,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    auto_promote    BOOLEAN DEFAULT FALSE,
    promote_after   INTERVAL DEFAULT '24 hours',
    max_failure_pct INTEGER DEFAULT 5,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Per-appliance update tracking
CREATE TABLE update_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    appliance_id    UUID NOT NULL REFERENCES appliances(id),
    release_id      UUID NOT NULL REFERENCES releases(id),
    status          VARCHAR(20) NOT NULL
                    CHECK (status IN ('pending','downloading','applying','verifying',
                                      'success','failed','rolled_back')),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    error_message   TEXT,
    from_version    VARCHAR(20),
    to_version      VARCHAR(20),
    log_url         VARCHAR(500),
    attempt         INTEGER DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Audit log
CREATE TABLE audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor           VARCHAR(100),
    action          VARCHAR(100),
    target          VARCHAR(255),
    details         JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### API Endpoints

```
POST   /api/v1/appliances/register        — Register new appliance, returns {appliance_id, api_key}
POST   /api/v1/appliances/checkin          — Report status, get update instructions
GET    /api/v1/updates/check               — Check for available updates
GET    /api/v1/updates/download/<id>       — Download .zup file
POST   /api/v1/updates/report              — Report update result

--- Admin (JWT auth) ---
POST   /api/v1/admin/releases              — Create release
POST   /api/v1/admin/releases/<id>/publish — Publish release
POST   /api/v1/admin/rollouts              — Create rollout policy
GET    /api/v1/admin/rollouts/<id>/status  — Rollout progress
PATCH  /api/v1/admin/rollouts/<id>         — Promote/pause/abort rollout
GET    /api/v1/admin/appliances            — Fleet overview
```

## Client-Side Agent

### Directory Structure

```
/opt/zenplus/updater/
├── __init__.py
├── agent.py              # Main daemon: check → download → verify → apply → report
├── config.py             # Configuration loading
├── crypto.py             # Ed25519 signature verification, SHA-256 checksums
├── downloader.py         # HTTPS download with resume and integrity check
├── executor.py           # Step executor with rollback on failure
├── health.py             # Post-update health checks
├── inventory.py          # System info collection
├── lockfile.py           # Prevents concurrent updates
├── rollback.py           # Backup creation and restore
├── steps/                # Individual step handlers
│   ├── __init__.py
│   ├── apply_code.py
│   ├── backup.py
│   ├── build_dashboard.py
│   ├── health_check.py
│   ├── install_binary.py
│   ├── install_config.py
│   ├── install_systemd.py
│   ├── pip_install.py
│   ├── run_hook.py
│   ├── run_migration.py
│   ├── service_control.py
│   └── npm_install.py
├── keys/
│   └── zentryc-release.pub
├── config/
│   └── agent.conf
└── logs/
    └── update.log
```

### Agent Configuration (agent.conf)

```ini
[server]
url = https://zentryc.com
check_interval_seconds = 900
download_timeout_seconds = 600

[appliance]
id = <uuid>
api_key = <256-bit hex>

[security]
public_key_path = /opt/zenplus/updater/keys/zentryc-release.pub
max_manifest_age_days = 30
verify_tls = true

[update]
backup_dir = /opt/zenplus/updater/backups
max_backups = 3
auto_update = true
maintenance_window_start = 02:00
maintenance_window_end = 05:00

[logging]
log_file = /opt/zenplus/updater/logs/update.log
log_level = INFO
max_log_size_mb = 10
log_rotate_count = 5
```

## Rollback Strategy (4 Levels)

1. **Code Rollback** — tar snapshot of `/opt/zenplus/{server,poller,dashboard}` before update
2. **Database Rollback** — `pg_dump` before migrations; ClickHouse migrations must be idempotent
3. **Service Rollback** — restore old systemd units and binaries
4. **Full Rollback** — restore everything, restart all services, report `rolled_back`

## Staged Rollout Flow

```
canary (2-3 appliances) → 24h wait → 10% fleet → 24h wait → 100%
                              ↓
                    auto-abort if >5% failures
```

Eligibility is deterministic: `hash(appliance_id + release_id) mod 100 < target_pct`

## Implementation Phases

| Phase | Scope | Timeline |
|-------|-------|----------|
| 1 | Client agent foundation (config, crypto, download, check-in) | Week 1-2 |
| 2 | Step executors + rollback engine | Week 2-3 |
| 3 | Server API on zentryc.com | Week 3-4 |
| 4 | Systemd integration + CLI commands | Week 4 |
| 5 | Registration flow + installer integration | Week 4-5 |
| 6 | Admin dashboard for fleet management | Week 5-6 |

## Dependencies

Appliance-side (add to venv):
```
cryptography>=42.0.0    # Ed25519 signature verification
httpx>=0.28.0           # HTTP client (already present)
```

## Design Rationale

| Decision | Reasoning |
|----------|-----------|
| Custom `.zup` format | Handles heterogeneous components (Python, Go, React, SQL, systemd) with declarative manifest |
| Ed25519 over RSA | Smaller keys, faster verification, no side-channel vulnerabilities |
| Pre-built artifacts | Avoids npm/Go toolchain on appliances; faster, more reliable |
| systemd timer over cron | Better logging, dependency ordering, staggered fleet checks |
| Not `git pull` | Current approach is fragile: no verification, rollback, migration handling, or auth |
