# Deploying the ZenPlus Windows agent

ZenPlus publishes one immutable, generic x64 setup executable. The package contains no
registration secret and is never modified after it is built. The same
checksummed (and optionally Authenticode-signed) package can be distributed to any
number of hosts through GPO, Intune, SCCM, or another software-management
system. Only the controller URL is configured on an endpoint.

This separation is important: changing bytes in an MSI after signing breaks
its signature and makes package hashes non-reproducible.

## 1. Build the package

```powershell
.\scripts\build.ps1 -ControllerUrl "https://192.168.8.221"
```

The script uses `.tools\go\bin\go.exe` when available and otherwise uses Go
from `PATH`. It runs the Go test suite unless `-SkipTests` is explicitly set.

Outputs:

- `dist/zenplus-agent-<version>.exe` -- canonical x64 setup and interactive wizard.
- `dist/zenplus-agent-<version>.msi` -- validation-only MSI wrapper; the
  appliance publishes the setup executable.
- `dist/ZenPlusAgentSetup-x64.exe` -- interactive/self-contained setup.
- `dist/agent-manifest.json` -- version, architecture, file size, build time,
  signature state, and SHA-256 of the canonical setup executable.

`-ControllerUrl` may set a convenient default controller address. Credentials
cannot be compiled into the binaries or MSI. The `CONTROLLER_URL` MSI property
can override the address during installation.

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

Copy the versioned setup executable into the controller artifact store:

```bash
scp dist/zenplus-agent-<version>.exe zenplus:/opt/zenplus/artifacts/agents/windows/
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

Download the setup executable once and install it with the appliance address. Each new
installation creates its own protected registration proof and appears in
Agent Fleet as **Pending authorization**.

Example managed rollout:

```powershell
.\zenplus-agent-<version>.exe /machine /quiet /norestart `
  CONTROLLER_URL="https://controller.example"
```

Approve the pending record from Agent Fleet. The appliance then issues a
unique durable API credential, which the agent stores using Windows DPAPI.
Until approval, the agent collects and spools locally but does not send
heartbeats or telemetry.

## Changing settings after install

Open **ZenPlus Agent > Settings**. For an all-users installation, profile or
controller changes open the installed Setup program and request administrator
approval before updating `%ProgramData%\ZenPlus\Agent\config\agent.yaml`.
Moving a host to another controller clears the old enrollment binding; the
host registers with the new appliance and waits for administrator approval.

You can also force a registration poll from an elevated terminal:

```powershell
zenplus-agent.exe register
```

`zenplus-agentctl.exe print-config` never prints the appliance-issued API key,
and agent logging redacts credential and bearer values.

For all-users installs, `%ProgramData%\ZenPlus\Agent` is restricted to
SYSTEM, administrators, and the agent service SID. The unelevated desktop app
reads only a sanitized status snapshot under
`%ProgramData%\ZenPlus\AgentDashboard`; raw spool records, gateway queues,
logs, rollback state, and credentials are not exposed to ordinary users.

## Uninstall and purge

Normal uninstall preserves agent state. Set `PURGE=1` only when identity,
configuration, logs, and queued telemetry should also be removed:

```powershell
.\zenplus-agent-<version>.exe /machine /uninstall /quiet /purge
```

## Agent upgrades

Publish a newer generic setup package and set `desired_version` or issue
`trigger_upgrade`. The agent downloads the portal manifest, verifies SHA-256,
verifies the Authenticode policy, and starts the installer. Upgrades preserve
the appliance-issued credential and the selected monitoring profile.

## Controller clock

Keep the controller NTP-synchronised. Agent and capture timestamps are UTC;
incorrect controller time makes relative-time charts appear empty even when
ingest succeeds.
