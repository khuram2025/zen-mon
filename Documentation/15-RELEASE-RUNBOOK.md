# ZenPlus — Release Runbook (build & push to zentryc.com)

> **Audience.** Anyone cutting a ZenPlus release. This is the *only* document you need open while you're doing it.
>
> **What this gets you.** A new release published on `https://zentryc.com`, signed with the production private key, with a rollout policy attached so customer appliances will pick it up on their next checkin.
>
> **Time per release.** ~5 minutes once the build host is set up. First-time setup of a build host is ~15 minutes.
>
> **What you need before starting.**
>
> 1. **The private key file.** `zentryc-release.key`, 119 bytes, mode 0400. Keep it in your password manager / secure vault. Never commit, never email, never paste in chat.
> 2. **Admin login for zentryc.com.** Email `zenai-release@zentryc.com` and the password (in your secret manager). Do not put the password in the runbook.
> 3. **A Linux build host.** Ubuntu 22.04 / 24.04 with internet access. Doesn't need to run ZenPlus itself — just build it. The host this document was written for is the dev VM at `/opt/zenplus/`, but any Linux box with the prerequisites below works.

---

## 1. Build host prerequisites (one-time)

If you're using the existing dev VM (`/opt/zenplus/` already populated), **skip to §2**. Otherwise, on a fresh Ubuntu host:

```bash
# system packages
sudo apt update
sudo apt install -y \
    git curl jq build-essential openssl ca-certificates \
    python3 python3-venv python3-pip \
    nodejs npm

# Go 1.22+ (Ubuntu's apt go is too old)
GO_VERSION="1.22.5"
curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" | sudo tar -C /usr/local -xzf -
echo 'export PATH=$PATH:/usr/local/go/bin' | sudo tee /etc/profile.d/golang.sh
sudo chmod +x /etc/profile.d/golang.sh
source /etc/profile.d/golang.sh

# verify
python3 --version    # expect 3.10+
node --version       # expect 20+
go version           # expect 1.22+
```

Clone the repo into `/opt/zenplus` (the build script hardcodes that path; mismatching it requires editing the script, don't):

```bash
sudo git clone https://github.com/khuram2025/zen-mon.git /opt/zenplus
sudo chown -R $USER:$USER /opt/zenplus

cd /opt/zenplus
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r server/requirements.txt
./venv/bin/pip install cryptography httpx     # build-release.py runtime deps

cd /opt/zenplus/dashboard && npm install
```

Drop the public key (this is *not* secret, it ships in every OVA):

```bash
# Option A: copy from somewhere you have it
sudo cp /path/to/zentryc-release.pub /opt/zenplus/updater/keys/zentryc-release.pub
sudo chmod 0644 /opt/zenplus/updater/keys/zentryc-release.pub

# Option B: paste the canonical PEM
sudo tee /opt/zenplus/updater/keys/zentryc-release.pub > /dev/null <<'EOF'
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAhwZpk2+cPN57lhIbcsPAI3Xtx9MyfMPM5m3Ny81swF8=
-----END PUBLIC KEY-----
EOF
```

Verify the public key fingerprint matches what the server expects:

```bash
sha256sum /opt/zenplus/updater/keys/zentryc-release.pub
# expect: 58a71bf2a5eb37af460616ce7c6eafdcf0d52d4d6a18932e788fd7a602b70e57
wc -c /opt/zenplus/updater/keys/zentryc-release.pub
# expect: 113
```

If those don't match, **stop** — the server team has rotated the key and §6 of `13-SHIP-READY-MASTER-PLAN.md` covers the recovery.

Save admin credentials so the build script doesn't prompt every time:

```bash
cat > ~/.zenplus-admin-creds <<EOF
{"email": "zenai-release@zentryc.com", "password": "<your-admin-password>"}
EOF
chmod 0600 ~/.zenplus-admin-creds
```

That file is read by `scripts/build-release.py` only on this host. Mode 0600 is enforced.

---

## 2. Drop the private key (per release, *or* persist it)

The private key file must be at `/opt/zenplus/updater/keys/zentryc-release.key` for the build script to find it. Two patterns:

**Pattern A — persist it** (simpler if this host is yours and locked down):

```bash
# from your laptop (one time):
scp zentryc-release.key user@build-host:/tmp/zentryc-release.key

# on the build host:
sudo install -m 0400 -o root -g root \
    /tmp/zentryc-release.key \
    /opt/zenplus/updater/keys/zentryc-release.key
sudo shred -u /tmp/zentryc-release.key
```

The key sits on the build host indefinitely. Anyone with root on this host can read it. Only acceptable if you trust the host.

**Pattern B — bring it for each release, remove after** (matches the original master-plan rule):

Same as Pattern A's first three commands, then after the release publishes successfully:

```bash
sudo shred -u /opt/zenplus/updater/keys/zentryc-release.key
```

Pattern B requires you to re-deposit the key for every release. Per the lessons documented in master plan §6.1, **never run `shred -u` on the key without a confirmed off-VM escrow copy first** — if the laptop copy is lost and the on-VM copy is shredded, the key is gone.

Verify the deposit:

```bash
sudo ls -la /opt/zenplus/updater/keys/zentryc-release.key
# expect: -r-------- 1 root root 119 ...
```

---

## 3. Per-release workflow

This is the core of the runbook. Every release runs these steps.

### 3.1 Pull the source you want to release

```bash
cd /opt/zenplus
sudo -u $USER git fetch origin
sudo -u $USER git checkout main           # or whatever branch you're releasing
sudo -u $USER git pull --ff-only

# confirm you're on the commit you intend to ship
git log -1 --oneline
```

### 3.2 Pick the version number

The server enforces strict version monotonicity: the new release must be **higher** than every previously published release. Check the current top:

```bash
JWT=$(curl -sS -X POST https://zentryc.com/api/v1/admin/auth/login \
  -H "Content-Type: application/json" \
  -d "@${HOME}/.zenplus-admin-creds" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

curl -sS -H "Authorization: Bearer $JWT" https://zentryc.com/api/v1/admin/releases \
  | python3 -c "
import sys,json
d=json.load(sys.stdin)
rels = d if isinstance(d,list) else d.get('releases', d.get('results',[]))
print('top 5 by version:')
def vk(r):
    v=r.get('version','0.0.0').split('-')[0]
    return [int(p) if p.isdigit() else 0 for p in v.split('.')]
for r in sorted(rels, key=vk, reverse=True)[:5]:
    print(f\"  {r.get('version'):<10} pub={r.get('is_published')}\")"
```

Pick the next version using semver:

| Change scope                                | Bump                |
|---------------------------------------------|---------------------|
| Bug fixes only, no schema or API changes    | patch (`1.2.1` → `1.2.2`) |
| New features, no breaking changes           | minor (`1.2.5` → `1.3.0`) |
| Breaking changes (rare; coordinate first)   | major (`1.x.x` → `2.0.0`) |

### 3.3 Build, sign, upload, and create rollout — one command

```bash
cd /opt/zenplus
sudo /opt/zenplus/venv/bin/python /opt/zenplus/scripts/build-release.py publish \
  --version 1.2.2 \
  --changelog "Concise summary of what changed since the previous release." \
  --severity normal \
  --rollout canary \
  --rollout-pct 100
```

Replace `1.2.2` and the changelog with your values.

What this does, in order:

1. Copies source from `/opt/zenplus/` into a temp build dir.
2. Runs `npx vite build` to produce a fresh `dashboard/dist/`.
3. Cross-compiles the Go poller (`GOOS=linux GOARCH=amd64 CGO_ENABLED=0`).
4. Bundles migrations, requirements.txt, code/, dashboard-dist.tar.gz, go-binaries/.
5. Generates `manifest.json`, signs it with `/opt/zenplus/updater/keys/zentryc-release.key`.
6. Computes `checksums.sha256` for every file in the package.
7. Tars everything as `/tmp/zenplus-releases/update-<version>.zup`.
8. Logs in to zentryc.com using `~/.zenplus-admin-creds`, gets a 24h JWT.
9. Uploads via `POST /api/v1/admin/releases/create` (multipart, the server re-verifies the signature against its public key).
10. Calls `POST /api/v1/admin/releases/<id>/publish`.
11. Calls `POST /api/v1/admin/rollouts` with the parameters you passed.
12. Prints the release URL.

### 3.4 Verify the release is live

```bash
JWT=$(curl -sS -X POST https://zentryc.com/api/v1/admin/auth/login \
  -H "Content-Type: application/json" \
  -d "@${HOME}/.zenplus-admin-creds" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# top of catalog should show the new version
curl -sS -H "Authorization: Bearer $JWT" https://zentryc.com/api/v1/admin/releases \
  | python3 -m json.tool | head -30

# rollout should show as active
curl -sS -H "Authorization: Bearer $JWT" \
  "https://zentryc.com/api/v1/admin/releases/$(...id from step 3.3...)" \
  | python3 -m json.tool
```

Expect:
- `is_published: true`
- `published_at` is recent
- `package_sha256` matches the local hash printed in step 3.3
- `rollouts` array contains one entry with `stage: "canary"`, `target_pct: 100`

If all three are true, **you're done**. Customer appliances in the canary group will pick up the release on their next checkin (default ~5 min cadence).

---

## 4. Promoting canary → full rollout (optional, after soak)

Canary lets a small subset of appliances apply the release first. Once you've watched the rollout for some time and seen no failures (the `update_history` rows in the admin appliance details show `status: success`), promote to the stable group:

```bash
JWT=$(...)   # same as before

# find the rollout id of your release
RELEASE_ID=<from step 3.3>
ROLLOUT_ID=$(curl -sS -H "Authorization: Bearer $JWT" \
  "https://zentryc.com/api/v1/admin/releases/$RELEASE_ID" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['rollouts'][0]['id'])")

# promote
curl -sS -X PATCH "https://zentryc.com/api/v1/admin/rollouts/$ROLLOUT_ID" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"action":"promote"}'
```

`promote` walks the rollout from canary → percentage(10%) → full(100%), one stage per call. To go straight to full:

```bash
# create a fresh rollout instead of patching
curl -sS -X POST https://zentryc.com/api/v1/admin/rollouts \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d "{\"release_id\":\"$RELEASE_ID\",\"stage\":\"full\",\"target_group\":\"stable\",\"target_pct\":100}"
```

Server enforces auto-abort if failure rate exceeds 5% during a rollout — see master plan §7.3.

---

## 5. Failure modes — what each error means

| Server response                                        | Meaning                                                   | Fix                                                                      |
|---------------------------------------------------------|-----------------------------------------------------------|--------------------------------------------------------------------------|
| `400 {"error":"version is required"}`                  | Multipart fields incomplete                                | Use the `publish` command, not raw curl with just the file               |
| `400 {"error":"Manifest signature verification failed"}`| Wrong private key, or server's public key is out of sync   | Verify `sha256sum /opt/zenplus/updater/keys/zentryc-release.pub` against your records |
| `400 {"error":"Version 1.2.2 already exists"}`         | You already published this version                        | Bump to 1.2.3; the server rejects re-uses                                |
| `400 {"hostname":["This field is required."]}`         | DRF-style validation error on a non-release endpoint      | Read the field name; supply it                                            |
| `401 {"error":"Token has expired"}`                    | JWT is >24h old                                            | Re-login (the publish script does this automatically per run)            |
| `curl: (55) Recv failure: Connection reset by peer`    | HTTP/2 streaming issue with Cloudflare on multipart       | If you're invoking curl manually, add `--http1.1`. The publish script uses httpx which defaults to 1.1, no flag needed |
| `403 {"error":"Registration token has already been used"}` | Single-use registration token re-submission             | Issue a new token from the admin dashboard or via `zenplus-admin issue-token` |
| Build script: `ERROR: Dashboard build failed:`         | Vite found a fatal error in dashboard source              | Inspect `npx vite build` output directly; fix the source. (TS-only errors are bypassed; vite-fatal errors are not.) |
| Build script: `WARNING: No private key at ..., manifest unsigned!` | Key file missing or unreadable by root            | See §2; the upload will be rejected if you continue                      |

---

## 6. Cleanup after a release

If you're using **Pattern A** (persistent key on build host): nothing to do. The key stays. Future releases reuse it.

If you're using **Pattern B** (key brought per release):

```bash
# only after you've confirmed the release is live AND you have an off-host escrow copy
sudo shred -u /opt/zenplus/updater/keys/zentryc-release.key
```

Optional, irrespective of pattern — clear the temp build artifacts:

```bash
sudo rm -rf /tmp/zenplus-releases/update-*.zup /tmp/zenplus-build-*
```

The published `.zup` lives on zentryc.com from now on; the local copy is no longer needed.

---

## 7. Reference — what the published artifact looks like

After step 3.3 succeeds, the release exists on zentryc.com with this shape (visible via `GET /api/v1/admin/releases/<id>`):

```json
{
  "id":             "uuid",
  "version":        "1.2.2",
  "min_version":    null,
  "changelog":      "...",
  "severity":       "normal",
  "arch":           "amd64",
  "package_url":    "https://zentryc.com/api/v1/updates/download/<uuid>",
  "package_size":   <bytes>,
  "package_sha256": "<64 hex>",
  "is_published":   true,
  "published_at":   "2026-MM-DDThh:mm:ssZ",
  "rollouts": [
    {
      "id":           "uuid",
      "stage":        "canary" | "percentage" | "full",
      "target_group": "canary" | "stable" | ...,
      "target_pct":   <0-100>,
      "started_at":   "..."
    }
  ]
}
```

The `manifest.json.sig` (Ed25519 signature of `manifest.json`) is embedded inside the `.zup` archive itself — appliances read it from the package, not from the API response. There is no separate `manifest_sig` field returned by the API in the current server build.

---

## 8. Quick reference — the whole release in 5 lines

Once §1 and §2 have been done once on this host, every release is just:

```bash
cd /opt/zenplus && git pull --ff-only
sudo /opt/zenplus/venv/bin/python /opt/zenplus/scripts/build-release.py publish \
    --version <X.Y.Z> \
    --changelog "<one-line summary>" \
    --severity normal \
    --rollout canary --rollout-pct 100
```

Memorize that.

---

*Document version: 1.0 | Authoritative as of release 1.2.1 (2026-05-03) | Owner: ZenPlus Release Engineering*
