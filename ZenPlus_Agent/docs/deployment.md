# Deploying the ZenPlus Windows agent

ZenPlus publishes one immutable, generic x64 MSI. The package contains no
enrollment token and is never modified after it is built. A rollout token is
provided at install time, so the same checksummed (and optionally
Authenticode-signed) MSI can be distributed to any number of hosts through
GPO, Intune, SCCM, or another software-management system.

This separation is important: changing bytes in an MSI after signing breaks
its signature and makes package hashes non-reproducible.

## 1. Build the package

```powershell
.\scripts\build.ps1 -ControllerUrl "http://192.168.8.221"
```

The script uses `.tools\go\bin\go.exe` when available and otherwise uses Go
from `PATH`. It runs the Go test suite unless `-SkipTests` is explicitly set.

Outputs:

- `dist/zenplus-agent-<version>.msi` -- generic x64 MSI.
- `dist/ZenPlusAgentSetup-x64.exe` -- interactive/self-contained setup.
- `dist/agent-manifest.json` -- version, architecture, file size, build time,
  and SHA-256 of the MSI.

`-ControllerUrl` may set a convenient default controller address. Enrollment
credentials cannot be compiled into the binaries or MSI. MSI properties can
still override the controller address during installation.

For a production release, provide the organisation's Authenticode certificate
and require signing:

```powershell
.\scripts\build.ps1 -ControllerUrl "https://controller.example" `
  -SigningThumbprint "<sha1-thumbprint>" -RequireSigning
```

The script signs and verifies each executable before it is embedded, then the
setup executable, and finally the MSI. It creates the manifest only after
signing, so the published checksum covers the signed bytes. Never alter a
package after signing. Development builds may omit the thumbprint; their
manifest reports `signature_status` as `NotSigned`.

## 2. Publish it with the portal release

Copy the versioned MSI into the controller artifact store:

```bash
scp dist/zenplus-agent-<version>.msi zenplus:/opt/zenplus/artifacts/agents/windows/
```

The portal's release workflow must carry the contents of
`/opt/zenplus/artifacts/agents/windows/` into every appliance release. This
keeps the Windows download available immediately after a portal upgrade and
prevents the "No Windows package is published yet" state.

Verify the portal manifest and compare its SHA-256 with
`dist/agent-manifest.json`:

```bash
curl "http://<controller>/api/v1/agents/packages/manifest?platform=windows&channel=stable&arch=amd64"
```

The artifact is immutable: publishing or downloading it must not inject a
token or rewrite any MSI bytes.

## 3. Perform a scalable rollout

Download the MSI once. Separately create a short-lived rollout enrollment
token with a maximum-use count sized for the deployment (plus a small safety
margin), and pass that same token to every intended host. Enrollment consumes
one use per host; downloading the package consumes none.

Example for a 500-host managed rollout:

```powershell
msiexec.exe /i zenplus-agent-<version>.msi /qn /norestart `
  CONTROLLER_URL="https://controller.example" `
  ENROLLMENT_TOKEN="zpa_enr_..." `
  SITE_ID="production"
```

Use the secret-distribution facility in GPO, Intune, SCCM, or the deployment
system instead of storing the token in a public script. `ENROLLMENT_TOKEN` is
marked as both `Secure` and `Hidden`, and the deferred custom action hides its
target from MSI logs. Command-line arguments can still be visible to local
administrators while installation is running, so keep rollout tokens
short-lived and revoke them after the rollout.

For small or high-assurance deployments, mint a single-use token per host.
That remains an option, but is not required for a fleet rollout.

After successful enrollment the agent stores its unique durable API
credential using Windows DPAPI and clears the bootstrap enrollment token from
configuration. Revoking the rollout token does not disconnect enrolled hosts.

## Changing settings after install

Edit `%ProgramData%\ZenPlus\Agent\config\agent.yaml`. The running agent
re-reads local settings every five seconds. To move a host to another
controller, provide the new controller URL and a fresh enrollment token; once
the host is enrolled, the token is removed.

You can also force enrollment from an elevated terminal:

```powershell
zenplus-agent.exe enroll --token <token>
```

`zenplus-agentctl.exe print-config` deliberately omits the enrollment token,
and agent logging redacts enrollment tokens, bearer credentials, and token
properties.

## Uninstall and purge

Normal uninstall preserves agent state. Set `PURGE=1` only when identity,
configuration, logs, and queued telemetry should also be removed:

```powershell
msiexec.exe /x zenplus-agent-<version>.msi /qn PURGE=1
```

## Agent upgrades

Publish a newer generic MSI and set `desired_version` or issue
`trigger_upgrade`. The agent downloads the portal manifest, verifies SHA-256,
and starts the installer. Because the published MSI never contains a bootstrap
token, an upgrade does not create or consume an enrollment token.

## Controller clock

Keep the controller NTP-synchronised. Agent and capture timestamps are UTC;
incorrect controller time makes relative-time charts appear empty even when
ingest succeeds.
