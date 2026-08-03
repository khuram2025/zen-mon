# Deploying the ZenPlus Agent from the controller

The controller serves a **pre-configured MSI**: the controller URL and a
bootstrap enrollment token are baked into the binaries at build time, so an
operator can download one file, run it, and the host enrols itself with no
prompts and nothing to type.

Both baked-in values remain changeable afterwards — see *Changing settings
after install*.

## How the token gets into the package

The controller URL is compiled into the binaries at build time. The
**enrollment token is not** — baking a single long-lived token into a
published package would hand a permanent fleet-join credential to anyone who
can reach the (deliberately unauthenticated) download endpoint.

Instead the MSI ships with a fixed-width placeholder as the default value of
its `ENROLLMENT_TOKEN` property, and the controller rewrites that placeholder
**in the bytes it streams to the operator**. Every token is exactly 40
characters (`zpa_enr_` + 32), so the substitution is length-preserving: the
compound-file layout, stream sizes and string-pool offsets all stay valid, no
rebuild is needed, and each download carries its own token.

If a package is fetched outside that flow the placeholder survives to the
agent, which treats it as *no token* rather than attempting an enrollment that
would be rejected.

> Changing the length of `PlaceholderEnrollmentToken` (agent) or
> `PKG_TOKEN_PLACEHOLDER` (controller) breaks this. They are asserted equal at
> download time and covered by `TestPlaceholderMatchesRealTokenWidth`.

## 1. Build the package

```bash
powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -ControllerUrl "http://192.168.8.221"
```

`-ControllerUrl` (or `ZENPLUS_EMBED_CONTROLLER_URL`) is injected with
`-ldflags` into `internal/config`. Omit it for an unconfigured build that falls
back to `config.DefaultControllerURL` and requires MSI properties at install
time.

`-EnrollmentToken` also exists, but is only for air-gapped builds that cannot
use the controller's download flow. Prefer leaving it unset.

Output: `dist/zenplus-agent-<version>.msi`. The name must stay in the
`zenplus-agent-<major>.<minor>.<patch>.msi` form — the controller's publish
scanner ignores anything else.

`-SkipTests` skips the test run; use it only when endpoint protection blocks
freshly built test binaries, and run `go test ./...` separately.

## 2. Publish it to the controller

```bash
scp dist/zenplus-agent-<version>.msi zenplus:/opt/zenplus/artifacts/agents/windows/
curl -X POST http://<controller>/api/v1/agent-fleet/packages/publish -H "Authorization: Bearer <admin-jwt>"
```

The publish endpoint hashes every package in the store, registers it, and
flags the highest version `is_latest`. Verify:

```bash
curl "http://<controller>/api/v1/agents/packages/manifest?platform=windows&channel=stable&arch=amd64"
```

The `sha256` it returns must match the local file — installers verify against
this value and abort on mismatch.

## 3. Install on a host

**Download agent (the normal path).** On the Agent Fleet page, *Download
agent* asks how many servers the package is for and hands back an installer
with this controller's address and a matching enrollment token already inside
it. Run it on each host — nothing to type, no per-host setup.

The server count becomes the token's `max_uses`: one enrollment is consumed
per host, so a package downloaded for 10 servers installs on exactly 10. Need
more later, or want to segment by site? Download again — each download mints
its own token.

Silent/GPO/Intune equivalent, using the file straight from that dialog:

```bash
msiexec /i zenplus-agent-<version>.msi /qn /norestart
```

The same thing over the API:

```bash
curl -X POST http://<controller>/api/v1/agent-fleet/packages/download \
  -H "Authorization: Bearer <operator-jwt>" -H 'Content-Type: application/json' \
  -d '{"platform":"windows","server_count":10,"ttl_hours":72,"label":"DC-A rollout"}' \
  -o zenplus-agent.msi
```

Response headers carry `X-Token-Prefix`, `X-Token-Max-Uses` and
`X-Token-Expires-At` for auditing. The raw token is never logged or
persisted — the only copy is inside the downloaded file.

**Unconfigured package.** `GET /api/v1/agents/packages/windows/latest` serves
the published file with the placeholder intact. That is what `install.ps1` and
agent self-update fetch, both of which supply their own credentials.

**Per-host token (preferred for production).** The Deploy Agent dialog mints a
single-use token and emits an elevated PowerShell one-liner that fetches
`install.ps1`, verifies the MSI checksum against the manifest, and installs
silently. MSI properties override the baked-in values:

```bash
msiexec /i zenplus-agent-<version>.msi /qn CONTROLLER_URL="https://controller" ENROLLMENT_TOKEN="zpa_enr_..." SITE_ID="..." POLICY_ID="..."
```

`ENROLLMENT_TOKEN` is declared `Hidden` so it is not written to MSI logs.

## Changing settings after install

Edit `%ProgramData%\ZenPlus\Agent\config\agent.yaml` — on-disk values always
win over the values baked into the binary. The running agent re-reads this
file every 5 seconds and rebuilds its controller connection in place, so no
service restart is needed. To move a host to a different controller, set
`controller_url` and a fresh `enrollment_token`; the agent re-enrols and then
clears the token from the file.

`zenplus-agent enroll --token <token>` forces re-enrollment from the CLI.

## Security notes

Minting happens behind the dashboard's operator role, so a token only ever
reaches someone who could enroll hosts anyway. Keep `server_count` to what the
rollout actually needs and the TTL short: a package downloaded for 200 hosts
with a year of validity is a fleet-join credential sitting in someone's
Downloads folder.

Revoke a download's token with
`POST /api/v1/servers/enrollment-tokens/{id}/revoke` (the id is returned in
the `X-Token-Id` response header). Revoking stops further enrollments; hosts
that already enrolled keep working, because they hold their own API keys.

The `/api/v1/agents/packages/...` download endpoints stay unauthenticated by
design — installers run before a host has any credential — but they only ever
serve the placeholder build, never a tokenised one.

## Agent upgrades

Publish a newer package, then either set `desired_version` on the agent or
issue `trigger_upgrade` from the fleet page. The agent fetches the manifest,
downloads the package, verifies its SHA-256, and launches the installer
detached. Upgrades are serialised, and a given version is not retried more
than once an hour, so a bad package cannot cause an install loop.

Agents older than 1.1.0 have no self-update and must be upgraded manually.

## Controller clock

The controller rewrites agent sample timestamps to **server time** whenever
they differ by more than 5 minutes. That makes the controller's clock
authoritative for all stored telemetry: if it is wrong, every sample is
mis-stamped and dashboard charts — which query relative to the *browser's*
clock — render empty even though ingest succeeds.

Keep the controller NTP-synced. `timedatectl` must report
`System clock synchronized: yes`. Agents report their own offset in
`clock_skew_s`; if *every* agent shows the same non-zero skew, suspect the
controller, not the hosts.
