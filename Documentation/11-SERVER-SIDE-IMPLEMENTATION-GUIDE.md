# ZenPlus OTA Update Server — Implementation Guide

> **Audience:** Server-side development team building the update infrastructure on `zentryc.com`
> **Client Status:** The appliance-side update agent is fully built and tested at `/opt/zenplus/updater/`. This document defines the exact API contract it expects.
> **Date:** 2026-04-06

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Tech Stack Recommendation](#2-tech-stack-recommendation)
3. [Database Schema](#3-database-schema)
4. [API Endpoints — Appliance-Facing](#4-api-endpoints--appliance-facing)
5. [API Endpoints — Admin](#5-api-endpoints--admin)
6. [Authentication & Security](#6-authentication--security)
7. [Update Package (.zup) Format](#7-update-package-zup-format)
8. [Package Build Pipeline](#8-package-build-pipeline)
9. [Rollout Engine](#9-rollout-engine)
10. [File Storage](#10-file-storage)
11. [Admin Dashboard UI](#11-admin-dashboard-ui)
12. [Deployment & Infrastructure](#12-deployment--infrastructure)
13. [Testing Checklist](#13-testing-checklist)

---

## 1. Architecture Overview

```
                        ┌─────────────────────────────────────────────┐
                        │              zentryc.com                     │
                        │                                              │
  ┌──────────────┐      │  ┌──────────┐  ┌──────────┐  ┌───────────┐ │
  │  Appliance 1 │─────►│  │  Nginx   │──│  FastAPI  │──│ PostgreSQL│ │
  │  Appliance 2 │─────►│  │  (TLS)   │  │  Server   │  │           │ │
  │  Appliance N │─────►│  └──────────┘  └─────┬─────┘  └───────────┘ │
  └──────────────┘      │                      │                       │
                        │               ┌──────┴──────┐               │
      ┌──────────┐      │               │  S3 / R2    │               │
      │  Admin   │─────►│               │  (packages) │               │
      │  Browser │      │               └─────────────┘               │
      └──────────┘      └─────────────────────────────────────────────┘

  Flow:
  1. Appliance registers → gets UUID + API key
  2. Every 15 min: appliance checks in → server decides if update available
  3. If yes: appliance downloads .zup, verifies, applies, reports result
  4. Admin: uploads releases, manages rollouts, monitors fleet
```

---

## 2. Tech Stack Recommendation

| Component | Recommended | Notes |
|-----------|------------|-------|
| Framework | **FastAPI** (Python 3.11+) | Async, auto-docs, familiar to team |
| Database | **PostgreSQL 15+** | UUID gen, JSONB, interval types needed |
| ORM | **SQLAlchemy 2.0** + Alembic | Async support, migration management |
| Auth | **bcrypt** (appliance keys), **JWT** (admin) | PyJWT + passlib |
| File Storage | **S3 / Cloudflare R2 / local disk** | For .zup packages |
| Task Queue | **Celery + Redis** (optional) | For rollout auto-promote cron |
| Reverse Proxy | **Nginx** | TLS termination, rate limiting |
| Containerization | **Docker Compose** | For deployment |

---

## 3. Database Schema

Run these migrations on the zentryc.com PostgreSQL instance.

### 3.1 Tables

```sql
-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- APPLIANCES — one row per deployed ZenPlus appliance
-- ============================================================
CREATE TABLE appliances (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    api_key_hash    VARCHAR(128) NOT NULL,          -- bcrypt hash of the API key
    hostname        VARCHAR(255),
    ip_address      INET,
    arch            VARCHAR(20) DEFAULT 'amd64',
    os_version      VARCHAR(50),
    current_version VARCHAR(20),
    agent_version   VARCHAR(20),
    last_checkin    TIMESTAMPTZ,
    registered_at   TIMESTAMPTZ DEFAULT NOW(),
    is_active       BOOLEAN DEFAULT TRUE,
    tags            JSONB DEFAULT '[]'::jsonb,       -- for targeting: ["customer:acme", "env:prod"]
    metadata        JSONB DEFAULT '{}'::jsonb,       -- disk info, hw fingerprint, etc.
    rollout_group   VARCHAR(50) DEFAULT 'stable'     -- canary | beta | stable
);

CREATE INDEX idx_appliances_active ON appliances(is_active) WHERE is_active = true;
CREATE INDEX idx_appliances_version ON appliances(current_version);
CREATE INDEX idx_appliances_group ON appliances(rollout_group);

-- ============================================================
-- RELEASES — each published update version
-- ============================================================
CREATE TABLE releases (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version         VARCHAR(20) NOT NULL UNIQUE,
    min_version     VARCHAR(20),                    -- minimum appliance version eligible
    changelog       TEXT,
    severity        VARCHAR(20) DEFAULT 'normal'
                    CHECK (severity IN ('critical', 'security', 'normal', 'optional')),
    arch            VARCHAR(20) DEFAULT 'amd64',
    package_url     VARCHAR(500) NOT NULL,           -- CDN/S3 URL to the .zup file
    package_size    BIGINT,
    package_sha256  VARCHAR(64) NOT NULL,
    manifest_sig    TEXT NOT NULL,                    -- base64-encoded Ed25519 signature
    is_published    BOOLEAN DEFAULT FALSE,
    published_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by      VARCHAR(100)
);

CREATE INDEX idx_releases_published ON releases(is_published, version);

-- ============================================================
-- ROLLOUT POLICIES — staged rollout control per release
-- ============================================================
CREATE TABLE rollout_policies (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id      UUID NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    stage           VARCHAR(20) NOT NULL
                    CHECK (stage IN ('canary', 'percentage', 'full', 'paused', 'aborted')),
    target_group    VARCHAR(50),                     -- NULL = all groups eligible
    target_pct      INTEGER DEFAULT 100              -- percentage of eligible appliances
                    CHECK (target_pct BETWEEN 0 AND 100),
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    auto_promote    BOOLEAN DEFAULT FALSE,
    promote_after   INTERVAL DEFAULT '24 hours',
    max_failure_pct INTEGER DEFAULT 5                -- auto-abort threshold
                    CHECK (max_failure_pct BETWEEN 0 AND 100),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_rollout_release ON rollout_policies(release_id);

-- ============================================================
-- UPDATE HISTORY — tracks every update attempt per appliance
-- ============================================================
CREATE TABLE update_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    appliance_id    UUID NOT NULL REFERENCES appliances(id) ON DELETE CASCADE,
    release_id      UUID NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    status          VARCHAR(20) NOT NULL
                    CHECK (status IN ('pending', 'downloading', 'applying',
                                      'verifying', 'success', 'failed', 'rolled_back')),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    error_message   TEXT,
    from_version    VARCHAR(20),
    to_version      VARCHAR(20),
    log_url         VARCHAR(500),
    attempt         INTEGER DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_history_appliance ON update_history(appliance_id, created_at DESC);
CREATE INDEX idx_history_release ON update_history(release_id, status);
CREATE UNIQUE INDEX idx_history_unique_attempt
    ON update_history(appliance_id, release_id, attempt);

-- ============================================================
-- ADMIN USERS — for dashboard / admin API access
-- ============================================================
CREATE TABLE admin_users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           VARCHAR(255) NOT NULL UNIQUE,
    password_hash   VARCHAR(128) NOT NULL,
    name            VARCHAR(100),
    role            VARCHAR(20) DEFAULT 'admin'
                    CHECK (role IN ('admin', 'viewer')),
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- AUDIT LOG — who did what
-- ============================================================
CREATE TABLE audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor           VARCHAR(100),                    -- admin email or "system"
    action          VARCHAR(100) NOT NULL,
    target          VARCHAR(255),
    details         JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_audit_created ON audit_log(created_at DESC);
```

---

## 4. API Endpoints — Appliance-Facing

All appliance endpoints are under `/api/v1/`. The client sends these headers on every request:

```
Authorization: Bearer <api_key>
X-Appliance-ID: <uuid>
User-Agent: zenplus-updater/1.0.0
Content-Type: application/json
```

> **IMPORTANT:** The registration endpoint is the only one that accepts empty auth headers (it creates the credentials).

---

### 4.1 POST `/api/v1/appliances/register`

Registers a new appliance. Called once during initial setup.

**Auth:** None (open endpoint — consider rate limiting + registration token)

**Request Body:**
```json
{
    "hostname": "DJSPR-DNSlog01",
    "arch": "amd64",
    "os_version": "ubuntu-22.04",
    "current_version": "1.3.0"
}
```

**Server Logic:**
1. Generate a UUID for `appliance_id`
2. Generate a 256-bit random hex string for `api_key`
3. Hash the `api_key` with bcrypt → store the hash
4. Insert into `appliances` table
5. Log to `audit_log`

**Response (200):**
```json
{
    "appliance_id": "550e8400-e29b-41d4-a716-446655440000",
    "api_key": "a3f8b2c1d4e5f6789012345678abcdef0123456789abcdef0123456789abcdef"
}
```

> **CRITICAL:** The `api_key` is returned in plaintext **only once**. The server stores only the bcrypt hash. If lost, a new key must be issued.

**Error Responses:**
- `429` — Rate limited
- `400` — Missing required fields

---

### 4.2 POST `/api/v1/appliances/checkin`

Periodic heartbeat + update check. Called every 15 minutes by the agent.

**Auth:** Required (Bearer + X-Appliance-ID)

**Request Body:**
```json
{
    "hostname": "DJSPR-DNSlog01",
    "arch": "amd64",
    "os_version": "ubuntu-22.04",
    "current_version": "1.3.0",
    "agent_version": "1.0.0",
    "uptime": 86400,
    "services_status": {
        "zenplus-api": "active",
        "zenplus-poller": "active",
        "netmon-gunicorn": "active",
        "netmon-celery": "active",
        "netmon-celery-beat": "active",
        "nginx": "active",
        "redis-server": "active",
        "postgresql@14-main": "active"
    },
    "disk": {
        "total": 107374182400,
        "used": 21474836480,
        "free": 85899345920
    }
}
```

**Server Logic:**
1. Validate auth (see Section 6)
2. Update `appliances` row: `hostname`, `arch`, `os_version`, `current_version`, `agent_version`, `last_checkin = NOW()`, `ip_address` (from request IP), `metadata.disk` and `metadata.services_status`
3. Evaluate if an update is available using the **Rollout Engine** (see Section 9)
4. Return result

**Response — No update:**
```json
{
    "next_action": "none",
    "release": null
}
```

**Response — Update available:**
```json
{
    "next_action": "update",
    "release": {
        "id": "660e8400-e29b-41d4-a716-446655440001",
        "version": "1.4.0",
        "changelog": "Added SNMP v3 support, fixed alert dedup bug",
        "severity": "normal",
        "package_url": "https://zentryc.com/packages/update-1.4.0.zup",
        "package_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "manifest_sig": "base64-encoded-ed25519-signature-of-manifest.json",
        "size": 15234567
    }
}
```

> **The `release` object fields are critical — the client expects exactly these fields.** The `manifest_sig` is the base64-encoded raw Ed25519 signature of the `manifest.json` file contents. The `package_url` must be a directly downloadable HTTPS URL.

---

### 4.3 GET `/api/v1/updates/check`

Explicit update check (alternative to checkin). Used by `zenplus-updater --check`.

**Auth:** Required

**Query Parameters:**
- `current_version` (required) — e.g., `1.3.0`
- `arch` (required) — e.g., `amd64`

**Response — No update:**
```json
{
    "available": false,
    "release": null
}
```

**Response — Update available:**
```json
{
    "available": true,
    "release": {
        "id": "660e8400-e29b-41d4-a716-446655440001",
        "version": "1.4.0",
        "changelog": "Added SNMP v3 support, fixed alert dedup bug",
        "severity": "normal",
        "package_url": "https://zentryc.com/packages/update-1.4.0.zup",
        "package_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "manifest_sig": "base64-encoded-ed25519-signature",
        "size": 15234567
    }
}
```

---

### 4.4 GET `/api/v1/updates/download/{release_id}`

Download the .zup package file.

**Auth:** Required

**Behavior Options (choose one):**
- **Option A — Direct serve:** Stream the `.zup` file from local storage with `Content-Type: application/octet-stream`
- **Option B — Redirect:** Return `302` redirect to a pre-signed S3/R2 URL

**Required Headers in Response:**
- `Content-Length: <file size in bytes>`
- `Accept-Ranges: bytes` (for resume support)

**Resume Support:** The client may send `Range: bytes=N-` header. The server MUST support this for large packages. Return `206 Partial Content` with the requested byte range.

**Error Responses:**
- `404` — Release not found
- `403` — Appliance not eligible for this release

---

### 4.5 POST `/api/v1/updates/report`

Report update result after an update attempt.

**Auth:** Required

**Request Body:**
```json
{
    "release_id": "660e8400-e29b-41d4-a716-446655440001",
    "status": "success",
    "from_version": "1.3.0",
    "to_version": "1.4.0",
    "error_message": "",
    "log_data": ""
}
```

**Valid `status` values the client sends:**
| Status | When |
|--------|------|
| `downloading` | Download started |
| `applying` | Extraction verified, applying steps |
| `success` | All steps + health check passed |
| `failed` | A step failed (before or after rollback) |

**Server Logic:**
1. Upsert into `update_history`:
   - If `status` is `downloading` or `applying`: set `started_at = NOW()`
   - If `status` is `success` or `failed`: set `completed_at = NOW()`
2. If `status` is `success`: update `appliances.current_version` to `to_version`
3. If `status` is `failed`: check rollout failure rate, potentially auto-abort rollout
4. If `log_data` is non-empty: store to file/S3, save URL in `update_history.log_url`

**Response:**
```json
{
    "acknowledged": true
}
```

---

## 5. API Endpoints — Admin

Admin endpoints require JWT auth (standard `Authorization: Bearer <jwt_token>`). These are for the admin dashboard UI.

### 5.1 Auth

```
POST /api/v1/admin/auth/login
  Body: {"email": "...", "password": "..."}
  Response: {"token": "jwt...", "expires_at": "..."}
```

### 5.2 Release Management

```
GET    /api/v1/admin/releases
  → List all releases, paginated
  Response: {"releases": [...], "total": N, "page": 1, "per_page": 20}

POST   /api/v1/admin/releases
  → Create a new release (upload .zup + metadata)
  Body: multipart/form-data with:
    - file: the .zup package
    - version: "1.4.0"
    - changelog: "..."
    - severity: "normal"
    - min_version: "1.2.0" (optional)
  Server: compute SHA-256, extract manifest, store package, create DB row

GET    /api/v1/admin/releases/{id}
  → Get release details + rollout status + per-appliance results

POST   /api/v1/admin/releases/{id}/publish
  → Set is_published=true, published_at=NOW()

DELETE /api/v1/admin/releases/{id}
  → Only if not published. Delete package file + DB row.
```

### 5.3 Rollout Management

```
POST   /api/v1/admin/rollouts
  → Create a rollout policy for a release
  Body: {
    "release_id": "...",
    "stage": "canary",
    "target_group": "canary",   // null = all
    "target_pct": 100,
    "auto_promote": true,
    "promote_after": "24 hours",
    "max_failure_pct": 5
  }

GET    /api/v1/admin/rollouts/{id}/status
  → Rollout progress: total eligible, deployed, success, failed, pending
  Response: {
    "stage": "canary",
    "total_eligible": 3,
    "deployed": 2,
    "success": 2,
    "failed": 0,
    "pending": 1,
    "failure_rate": 0.0,
    "started_at": "...",
    "can_promote": true
  }

PATCH  /api/v1/admin/rollouts/{id}
  → Promote to next stage, pause, or abort
  Body: {"action": "promote" | "pause" | "abort"}
  
  promote: creates new rollout_policy row with next stage:
    canary → percentage (target_pct=10)
    percentage → full (target_pct=100)
  
  pause: sets stage to "paused" — appliances stop receiving this update
  abort: sets stage to "aborted" — update is dead
```

### 5.4 Fleet Management

```
GET    /api/v1/admin/appliances
  → List all appliances with current status
  Query params: ?page=1&per_page=50&group=stable&version=1.3.0&active=true
  Response: {
    "appliances": [
      {
        "id": "...",
        "hostname": "DJSPR-DNSlog01",
        "ip_address": "10.11.50.30",
        "current_version": "1.3.0",
        "agent_version": "1.0.0",
        "last_checkin": "2026-04-06T11:00:00Z",
        "rollout_group": "stable",
        "is_active": true,
        "services_status": {...},
        "disk": {...}
      }
    ],
    "total": 45
  }

GET    /api/v1/admin/appliances/{id}
  → Detailed appliance info + update history

GET    /api/v1/admin/appliances/{id}/history
  → Update history for this appliance

PATCH  /api/v1/admin/appliances/{id}
  → Update rollout_group, tags, is_active
  Body: {"rollout_group": "canary", "tags": ["env:staging"]}

POST   /api/v1/admin/appliances/{id}/revoke-key
  → Revoke API key, generate new one
  Response: {"new_api_key": "..."}

DELETE /api/v1/admin/appliances/{id}
  → Deactivate appliance (set is_active=false, do NOT hard delete)
```

### 5.5 Dashboard Stats

```
GET    /api/v1/admin/dashboard/stats
  Response: {
    "total_appliances": 45,
    "active_appliances": 42,
    "version_distribution": {"1.3.0": 30, "1.4.0": 12},
    "last_24h_checkins": 40,
    "active_rollouts": 1,
    "recent_failures": 2
  }
```

---

## 6. Authentication & Security

### 6.1 Appliance Authentication

Every appliance request includes two headers:
```
Authorization: Bearer <api_key>
X-Appliance-ID: <uuid>
```

**Validation logic (middleware):**
```python
async def validate_appliance(request):
    appliance_id = request.headers.get("X-Appliance-ID")
    api_key = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    
    if not appliance_id or not api_key:
        return 401, {"error": "Missing credentials"}
    
    appliance = await db.get(appliances, id=appliance_id)
    if not appliance or not appliance.is_active:
        return 403, {"error": "Appliance not found or deactivated"}
    
    if not bcrypt.checkpw(api_key.encode(), appliance.api_key_hash.encode()):
        return 403, {"error": "Invalid API key"}
    
    # Store appliance in request state for downstream use
    request.state.appliance = appliance
```

### 6.2 Registration Security

The `/register` endpoint is open. To prevent abuse:
- **Rate limit:** 5 registrations per IP per hour
- **Optional:** Require a one-time registration token (pre-generated, given to customer with appliance). Add `registration_token` field to the request, validate against a `registration_tokens` table.

### 6.3 Admin Authentication

- Standard JWT with 24h expiry
- Password hashed with bcrypt (cost factor 12)
- Refresh token optional

### 6.4 Ed25519 Signing

The release signing keypair has been generated. The keys are at:
- **Private key** (keep this on the build server ONLY): provided separately
- **Public key** (embedded on every appliance): `/opt/zenplus/updater/keys/zentryc-release.pub`

```
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAllOqQ4Iy8yEs5NS5byit3hT6Mj8ajOTYeD3MXzjYNjg=
-----END PUBLIC KEY-----
```

**To sign a manifest during package build:**
```python
from cryptography.hazmat.primitives.serialization import load_pem_private_key
import base64

# Load private key
with open("zentryc-release.key", "rb") as f:
    private_key = load_pem_private_key(f.read(), password=None)

# Read manifest
with open("manifest.json", "rb") as f:
    manifest_data = f.read()

# Sign
signature = private_key.sign(manifest_data)

# Save raw signature (for .sig file inside .zup)
with open("manifest.json.sig", "wb") as f:
    f.write(signature)

# For API response (base64-encoded)
manifest_sig_b64 = base64.b64encode(signature).decode()
```

### 6.5 TLS

- Enforce TLS 1.2+ on Nginx
- Use Let's Encrypt or a commercial cert for `zentryc.com`
- HSTS recommended

---

## 7. Update Package (.zup) Format

A `.zup` file is a `tar.gz` archive. When a release is uploaded, the server should:

1. Verify it's a valid tar.gz
2. Extract and validate `manifest.json` exists
3. Verify `manifest.json.sig` against the release public key
4. Compute SHA-256 of the entire `.zup` file → store as `package_sha256`
5. Record file size → store as `package_size`
6. Store the `.zup` file in the package storage (S3/R2/disk)
7. Store the base64-encoded signature as `manifest_sig` in the `releases` table

**Package internal structure:**
```
├── manifest.json            # REQUIRED — update metadata + ordered steps
├── manifest.json.sig        # REQUIRED — Ed25519 raw signature (64 bytes)
├── checksums.sha256         # REQUIRED — "sha256  filename" per line
├── code/                    # OPTIONAL — replacement source files
├── dashboard-dist.tar.gz   # OPTIONAL — pre-built React dist
├── go-binaries/            # OPTIONAL — compiled Go binaries
├── requirements.txt         # OPTIONAL — pip dependencies
├── migrations/              # OPTIONAL — SQL files
├── systemd/                 # OPTIONAL — .service files
├── configs/                 # OPTIONAL — config templates
└── hooks/                   # OPTIONAL — shell scripts
```

---

## 8. Package Build Pipeline

This is the workflow for creating a `.zup` release package. Can be automated via CI/CD or run manually.

### 8.1 Build Steps

```bash
#!/bin/bash
# build-release.sh — run on the build server

VERSION="1.4.0"
BUILD_DIR="/tmp/zenplus-build-${VERSION}"
REPO_DIR="/path/to/zen-mon"
PRIVATE_KEY="/secure/zentryc-release.key"

# 1. Clean build directory
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"/{code,migrations,go-binaries,systemd,configs,hooks}

# 2. Copy changed source files
# (use git diff to determine what changed, or copy full directories)
cp -r "${REPO_DIR}/server/" "${BUILD_DIR}/code/server/"
cp -r "${REPO_DIR}/poller/" "${BUILD_DIR}/code/poller/"
cp -r "${REPO_DIR}/scripts/" "${BUILD_DIR}/code/scripts/"

# 3. Build dashboard
cd "${REPO_DIR}/dashboard"
npm ci && npm run build
tar czf "${BUILD_DIR}/dashboard-dist.tar.gz" -C dist .

# 4. Build Go binary
cd "${REPO_DIR}/poller"
GOOS=linux GOARCH=amd64 go build -o "${BUILD_DIR}/go-binaries/zenplus-poller" ./cmd/poller

# 5. Copy migrations (only new ones since last release)
cp "${REPO_DIR}/migrations/pg-004-*.sql" "${BUILD_DIR}/migrations/" 2>/dev/null || true
cp "${REPO_DIR}/migrations/ch-003-*.sql" "${BUILD_DIR}/migrations/" 2>/dev/null || true

# 6. Copy new pip requirements if changed
cp "${REPO_DIR}/server/requirements.txt" "${BUILD_DIR}/requirements.txt"

# 7. Copy new systemd units if any
# cp new-service.service "${BUILD_DIR}/systemd/"

# 8. Create manifest.json
cat > "${BUILD_DIR}/manifest.json" << 'MANIFEST'
{
    "format_version": 2,
    "update_id": "$(uuidgen)",
    "version": "1.4.0",
    "from_version": null,
    "min_version": "1.2.0",
    "release_date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "changelog": "Added SNMP v3 support, fixed alert dedup bug",
    "severity": "normal",
    "arch": "amd64",
    "os_min": "ubuntu-22.04",
    "steps": [
        {"type": "stop_services", "services": ["zenplus-api", "zenplus-poller"]},
        {"type": "backup", "targets": ["code", "database"]},
        {"type": "apply_code", "method": "replace", "source": "code/"},
        {"type": "pip_install", "requirements": "requirements.txt"},
        {"type": "build_dashboard", "prebuilt": true, "source": "dashboard-dist.tar.gz"},
        {"type": "install_binary", "source": "go-binaries/zenplus-poller", "dest": "/opt/zenplus/bin/zenplus-poller"},
        {"type": "start_services", "services": ["zenplus-api", "zenplus-poller", "nginx"]},
        {"type": "health_check", "url": "http://localhost:8000/api/v1/system/health", "timeout": 30}
    ],
    "rollback_steps": [
        {"type": "restore_backup"},
        {"type": "start_services", "services": ["zenplus-api", "zenplus-poller", "nginx"]}
    ]
}
MANIFEST

# 9. Generate checksums
cd "${BUILD_DIR}"
find . -type f ! -name 'checksums.sha256' ! -name 'manifest.json.sig' \
    -exec sha256sum {} \; > checksums.sha256

# 10. Sign manifest
python3 -c "
from cryptography.hazmat.primitives.serialization import load_pem_private_key
with open('${PRIVATE_KEY}', 'rb') as f:
    key = load_pem_private_key(f.read(), password=None)
with open('manifest.json', 'rb') as f:
    data = f.read()
sig = key.sign(data)
with open('manifest.json.sig', 'wb') as f:
    f.write(sig)
"

# 11. Create .zup package
cd /tmp
tar czf "update-${VERSION}.zup" -C "${BUILD_DIR}" .

# 12. Compute package hash
sha256sum "update-${VERSION}.zup"

echo "Package ready: /tmp/update-${VERSION}.zup"
```

### 8.2 Upload to Server

```bash
curl -X POST https://zentryc.com/api/v1/admin/releases \
    -H "Authorization: Bearer <admin-jwt>" \
    -F "file=@/tmp/update-1.4.0.zup" \
    -F "version=1.4.0" \
    -F "changelog=Added SNMP v3 support, fixed alert dedup bug" \
    -F "severity=normal" \
    -F "min_version=1.2.0"
```

---

## 9. Rollout Engine

The rollout engine is the core decision logic. It answers: **"Should this appliance receive this update right now?"**

### 9.1 Decision Algorithm

Called during every `/checkin` and `/updates/check`:

```python
def get_available_update(appliance) -> Release | None:
    """Determine if an appliance should receive an update."""
    
    # 1. Find the latest published release
    release = db.query(Release).filter(
        Release.is_published == True,
        Release.arch == appliance.arch,
    ).order_by(Release.version.desc()).first()
    
    if not release:
        return None
    
    # 2. Check version: is the appliance behind?
    if semver_compare(appliance.current_version, release.version) >= 0:
        return None  # already up to date
    
    # 3. Check min_version constraint
    if release.min_version:
        if semver_compare(appliance.current_version, release.min_version) < 0:
            return None  # appliance too old, needs intermediate update
    
    # 4. Check if already attempted and succeeded
    existing = db.query(UpdateHistory).filter(
        UpdateHistory.appliance_id == appliance.id,
        UpdateHistory.release_id == release.id,
        UpdateHistory.status == 'success',
    ).first()
    if existing:
        return None
    
    # 5. Check if already failed too many times (max 3 attempts)
    fail_count = db.query(UpdateHistory).filter(
        UpdateHistory.appliance_id == appliance.id,
        UpdateHistory.release_id == release.id,
        UpdateHistory.status.in_(['failed', 'rolled_back']),
    ).count()
    if fail_count >= 3:
        return None
    
    # 6. Check rollout policy
    rollout = db.query(RolloutPolicy).filter(
        RolloutPolicy.release_id == release.id,
        RolloutPolicy.stage.notin_(['paused', 'aborted']),
    ).order_by(RolloutPolicy.created_at.desc()).first()
    
    if not rollout:
        return None  # no active rollout = not available
    
    # 7. Check group eligibility
    if rollout.target_group and appliance.rollout_group != rollout.target_group:
        return None
    
    # 8. Check percentage eligibility (deterministic)
    if rollout.target_pct < 100:
        hash_input = f"{appliance.id}:{release.id}"
        bucket = int(hashlib.sha256(hash_input.encode()).hexdigest(), 16) % 100
        if bucket >= rollout.target_pct:
            return None
    
    return release
```

### 9.2 Auto-Promote / Auto-Abort

Run this as a periodic task (every 5 minutes via Celery beat or pg_cron):

```python
def check_rollout_health():
    """Auto-promote or auto-abort rollouts based on results."""
    
    active_rollouts = db.query(RolloutPolicy).filter(
        RolloutPolicy.stage.notin_(['paused', 'aborted']),
        RolloutPolicy.completed_at.is_(None),
    ).all()
    
    for rollout in active_rollouts:
        stats = get_rollout_stats(rollout)
        
        # Auto-abort: failure rate exceeds threshold
        if stats['deployed'] > 0:
            failure_rate = stats['failed'] / stats['deployed'] * 100
            if failure_rate > rollout.max_failure_pct:
                rollout.stage = 'aborted'
                rollout.completed_at = now()
                log_audit("system", "rollout_aborted",
                         f"Release {rollout.release.version}",
                         {"reason": f"Failure rate {failure_rate:.1f}% > {rollout.max_failure_pct}%"})
                continue
        
        # Auto-promote: all conditions met
        if rollout.auto_promote:
            elapsed = now() - rollout.started_at
            if elapsed >= rollout.promote_after:
                if stats['pending'] == 0 and stats['failed'] == 0:
                    promote_rollout(rollout)
```

---

## 10. File Storage

### Option A: Local Disk (Simple)
```
/var/www/zentryc/packages/
├── update-1.3.0.zup
├── update-1.4.0.zup
└── update-1.5.0.zup
```
Serve via Nginx with auth proxy. Set `package_url` to `https://zentryc.com/api/v1/updates/download/{release_id}` and stream from disk.

### Option B: S3 / Cloudflare R2 (Scalable)
- Upload `.zup` to bucket during release creation
- Generate pre-signed URL (valid 1 hour) during checkin
- Set `package_url` to the pre-signed URL
- Appliance downloads directly from CDN — no load on API server

**Recommended:** Start with Option A, migrate to Option B when fleet grows beyond ~50 appliances.

---

## 11. Admin Dashboard UI

Build a web UI at `https://zentryc.com/admin/` (React, Vue, or server-rendered). Key screens:

### 11.1 Fleet Overview
- Total appliances, active count, offline count (no checkin in 1h)
- Version distribution pie/bar chart
- Map or table of all appliances with status badges
- Filter by version, group, active/inactive

### 11.2 Release Management
- List all releases (version, date, severity, status, rollout progress)
- Upload new release (.zup drag-and-drop)
- View release details: changelog, eligible appliances, deployment progress
- Publish / unpublish / delete

### 11.3 Rollout Control
- Visual rollout progress bar: canary → percentage → full
- Per-stage stats: deployed / success / failed / pending
- Promote / pause / abort buttons
- Real-time failure rate indicator with auto-abort threshold

### 11.4 Appliance Detail
- System info: hostname, IP, version, arch, OS, uptime
- Services status grid (green/red badges)
- Disk usage bar
- Update history timeline
- Change rollout group, manage tags

### 11.5 Audit Log
- Chronological list of admin actions
- Filter by actor, action type, date range

---

## 12. Deployment & Infrastructure

### 12.1 Recommended Setup on zentryc.com

```
zentryc.com server
├── Docker Compose
│   ├── nginx (TLS, reverse proxy, rate limiting)
│   ├── fastapi-app (the update server)
│   ├── postgres (database)
│   ├── redis (caching + celery broker)
│   └── celery-worker (rollout health checks)
├── /var/www/zentryc/packages/   (package storage)
└── /etc/zentryc/keys/           (signing keys — restricted access)
```

### 12.2 Docker Compose Example

```yaml
version: "3.8"

services:
  db:
    image: postgres:15-alpine
    environment:
      POSTGRES_DB: zentryc
      POSTGRES_USER: zentryc
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
    restart: always

  redis:
    image: redis:7-alpine
    restart: always

  api:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
    environment:
      DATABASE_URL: postgresql+asyncpg://zentryc:${DB_PASSWORD}@db:5432/zentryc
      REDIS_URL: redis://redis:6379/0
      SECRET_KEY: ${SECRET_KEY}
      PACKAGE_STORAGE_PATH: /packages
      SIGNING_KEY_PATH: /keys/zentryc-release.key
    volumes:
      - packages:/packages
      - ./keys:/keys:ro
    depends_on:
      - db
      - redis
    restart: always

  celery:
    build: .
    command: celery -A app.tasks worker -B --loglevel=info
    environment:
      DATABASE_URL: postgresql+asyncpg://zentryc:${DB_PASSWORD}@db:5432/zentryc
      REDIS_URL: redis://redis:6379/0
    depends_on:
      - db
      - redis
    restart: always

  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf
      - /etc/letsencrypt:/etc/letsencrypt:ro
    depends_on:
      - api
    restart: always

volumes:
  pgdata:
  packages:
```

### 12.3 Nginx Config for zentryc.com

```nginx
server {
    listen 443 ssl http2;
    server_name zentryc.com;

    ssl_certificate /etc/letsencrypt/live/zentryc.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/zentryc.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    # Rate limiting
    limit_req_zone $binary_remote_addr zone=register:10m rate=5r/h;
    limit_req_zone $binary_remote_addr zone=checkin:10m rate=10r/m;
    limit_req_zone $binary_remote_addr zone=download:10m rate=5r/m;

    # Appliance API
    location /api/v1/appliances/register {
        limit_req zone=register burst=2;
        proxy_pass http://api:8000;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location /api/v1/appliances/checkin {
        limit_req zone=checkin burst=5;
        proxy_pass http://api:8000;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location /api/v1/updates/download/ {
        limit_req zone=download burst=2;
        proxy_pass http://api:8000;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 600;
        proxy_buffering off;
    }

    location /api/v1/ {
        proxy_pass http://api:8000;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # Admin dashboard
    location /admin/ {
        root /var/www/zentryc;
        try_files $uri $uri/ /admin/index.html;
    }

    # Package downloads (if serving directly)
    location /packages/ {
        internal;  # only via X-Accel-Redirect from API
        alias /var/www/zentryc/packages/;
    }
}

server {
    listen 80;
    server_name zentryc.com;
    return 301 https://$server_name$request_uri;
}
```

### 12.4 Environment Variables

```env
# .env on zentryc.com server
DB_PASSWORD=<strong-random-password>
SECRET_KEY=<random-256-bit-hex>
PACKAGE_STORAGE_PATH=/var/www/zentryc/packages
SIGNING_KEY_PATH=/etc/zentryc/keys/zentryc-release.key

# Optional
SENTRY_DSN=https://...
SMTP_HOST=smtp.example.com
ALERT_EMAIL=ops@zentryc.com
```

---

## 13. Testing Checklist

Before going live, verify each of these scenarios end-to-end:

### Registration Flow
- [ ] New appliance registers successfully, receives UUID + API key
- [ ] Duplicate registration from same hostname creates a new appliance (not error)
- [ ] Rate limiting blocks excessive registrations
- [ ] Invalid registration body returns 400

### Check-in Flow
- [ ] Valid checkin returns `"next_action": "none"` when no update available
- [ ] Valid checkin returns update when rollout is active and appliance is eligible
- [ ] Checkin updates `last_checkin`, `current_version`, `ip_address` in DB
- [ ] Invalid API key returns 403
- [ ] Deactivated appliance returns 403

### Update Flow
- [ ] Download endpoint serves .zup file correctly
- [ ] Resume download works (Range header → 206 response)
- [ ] SHA-256 of downloaded file matches `package_sha256`
- [ ] Status reports (downloading → applying → success) all recorded in `update_history`
- [ ] After success, `appliances.current_version` is updated
- [ ] Failed status with error_message is recorded

### Rollout Engine
- [ ] Canary rollout only targets appliances in the "canary" group
- [ ] Percentage rollout deterministically selects the right percentage
- [ ] Same appliance always gets the same bucket (deterministic hash)
- [ ] Paused rollout stops serving updates to all appliances
- [ ] Aborted rollout permanently stops
- [ ] Auto-promote works after `promote_after` interval
- [ ] Auto-abort triggers when failure rate exceeds `max_failure_pct`
- [ ] Appliance that already succeeded is not offered the same update again
- [ ] Appliance that failed 3 times is not offered the same update again

### Security
- [ ] Manifest signature verification works with the production public key
- [ ] Tampered manifest (modified after signing) is rejected
- [ ] Package with wrong SHA-256 is rejected
- [ ] Expired manifest (>30 days old) is rejected
- [ ] API key validation works (bcrypt hash comparison)
- [ ] TLS is enforced (HTTP redirects to HTTPS)

### Edge Cases
- [ ] Appliance with `current_version` below `min_version` is not offered the update
- [ ] Multiple releases published: appliance gets the latest eligible one
- [ ] Appliance reconnects after being offline for days: gets the latest update
- [ ] Concurrent checkins from same appliance don't create duplicate history rows
- [ ] Large .zup file (>100MB) downloads without timeout

---

## Quick Reference: What the Client Expects

| Field in release object | Type | Required | Description |
|------------------------|------|----------|-------------|
| `id` | string (UUID) | Yes | Release ID for status reporting |
| `version` | string | Yes | Semver version string |
| `changelog` | string | No | Human-readable description |
| `severity` | string | No | `critical`, `security`, `normal`, `optional` |
| `package_url` | string (HTTPS URL) | Yes | Direct download URL for .zup file |
| `package_sha256` | string (64 hex chars) | Yes | SHA-256 of the .zup file |
| `manifest_sig` | string (base64) | No* | Ed25519 signature of manifest.json |
| `size` | integer | No | Package size in bytes |

> *`manifest_sig` can be omitted if the signature is included inside the .zup as `manifest.json.sig`

---

**Questions?** Contact the appliance team. The client agent code is at `/opt/zenplus/updater/` in the zen-mon repo.
