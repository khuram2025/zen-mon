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
manifest reports `signature_status` as `NotSigned`. Unsigned development builds
must explicitly pass `-AllowUnsignedDevelopmentBuild` and must not be published.

### Microsoft Azure Artifact Signing

The Zentryc public publisher profile is `zentrycsigning/zentryc-public` in East US.
The checked-in `packaging/artifact-signing.json` contains only public resource
identifiers and selects Azure CLI authentication. Sign in to the Zentryc tenant
with Azure CLI before building. The identity must hold the Artifact Signing
Certificate Profile Signer role for this profile.

```powershell
az login --tenant d8e4f4d6-451d-454f-9f21-3a20a93b1022
.\scripts\build.ps1 -RequireSigning `
  -ArtifactSigningMetadata .\packaging\artifact-signing.json `
  -ArtifactSigningDlib "<client-tools>\x64\Azure.CodeSigning.Dlib.dll" `
  -SignToolPath "<windows-sdk>\x64\signtool.exe"
```

Install Microsoft's Artifact Signing client tools (including .NET 8 and the
Windows SDK signing tool), or use the official Microsoft NuGet packages and
runtime. This path uses Microsoft's RFC 3161 timestamp server. Every signed
artifact must pass SignTool verification and Windows Authenticode validation
with a timestamp before the release manifest is written. Keep credentials in
Azure's credential cache, never in the metadata file or release artifacts.

Signing establishes the verified publisher `Zentryc LLC`; it does not guarantee
that SmartScreen reputation checks or third-party antivirus products will
never warn. Investigate an actual detection and submit a false-positive report
to the detecting vendor when appropriate.

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

Version 1.12.8 defaults new installations to `https://10.11.40.31`. Existing
installations retain their saved controller unless an explicit controller
argument is supplied. Run bulk commands from an administrator or SYSTEM
process; silent machine installation fails with exit code 1 instead of waiting
for an interactive elevation prompt.

```powershell
$p = Start-Process .\zenplus-agent-1.12.9.exe -Wait -PassThru -ArgumentList @(
  '/machine', '/quiet', '/norestart',
  'CONTROLLER_URL=https://10.11.40.31', 'INSTALL_PROFILE=combined',
  '/log', '"C:\Windows\Temp\ZenPlus-install.log"'
)
exit $p.ExitCode
```

EXE switches are case-insensitive. `/silent`, `/verysilent`, `/S` and `/qn`
are aliases for `/quiet`; `/allusers` means `/machine`, and `/currentuser`
means `/user`. `--controller-url`, `/controller`, `CONTROLLER_URL=...`,
`--profile`, `INSTALL_PROFILE=...`, `CONTROLLER_CA_FILE=...`, `/log path`,
`/no-start-menu` and `/norestart` are supported. Profiles are `combined`,
`infrastructure` and `apm`. The log's parent directory must already exist.
Unknown switches/properties and conflicting scope choices return an error.

MSI deployment supports standard Windows Installer logging and UI switches:

```cmd
msiexec /i zenplus-agent-1.12.9.msi /qn /norestart CONTROLLER_URL="https://10.11.40.31" INSTALL_PROFILE="combined" /L*v "%TEMP%\ZenPlus-install.log"
```

Approve the pending record from Agent Fleet. The appliance then issues a
unique durable API credential, which the agent stores using Windows DPAPI.
Until approval, the agent collects and spools locally but does not send
heartbeats or telemetry.

## Changing settings after install

Open **ZenPlus Agent > Settings**, edit the controller or monitoring profile,
and press **Save**. Starting with 1.12.9, Save writes only those settings; it
does not open Setup or reinstall the agent. All-users changes request Windows
administrator approval. The helper rereads the protected configuration and
preserves its other values before replacing the file atomically. The running
agent picks up the change on its local-settings poll, without a service restart.
A different appliance may require authorization in Agent Fleet.

The unelevated dashboard reads the sanitized status snapshot. If that snapshot
is unavailable, it opens a standalone Settings dialog with administrator
approval. Cancel makes no changes. If applications are still instrumented,
uninstrument them before selecting a profile that disables APM.

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

For updates initiated at the Windows agent, publish the signed EXE and MSI
under immutable versioned filenames at `https://zentryc.com/downloads/zenplus-agent/stable/`.
Verify the uploaded sizes and SHA-256 values, then replace `stable/manifest.json`
last. The manifest's primary download must point to the EXE; the MSI has its
own `msi_download_url` and checksum fields. Keep the prior manifest and packages
for recovery. Never change a package after generating its manifest.

Starting with 1.12.7, open **ZenPlus Agent > Updates > Install update**. The app
downloads from the Zentryc public channel, verifies size, SHA-256 and Windows
Authenticode trust, then opens Setup. A machine installation requests
administrator approval before loading its protected existing configuration.
Complete Setup with the existing controller and monitoring profile. The
dashboard checks on startup and every six hours; **Updates** checks immediately.
It does not install releases without a user action.

Versions through 1.12.6 open the download in a browser. Use the signed EXE once
to upgrade those versions to 1.12.7 or later. Their service-side PowerShell
signature check is defective, so publishing a new package alone cannot repair
that older running updater.

For an appliance-managed rollout, publish the package to the enrolled
appliance and set `desired_version` or issue its upgrade command. The service
prefers the appliance manifest, preserving operation on private networks;
the public channel is a fallback when that manifest is unavailable. Updating
zentryc.com alone does not replace a valid older appliance manifest.

Before promoting a release, test an actual older enrolled installation:
record agent/server IDs, controller, profile and heartbeat; install the update;
confirm the new binary versions and signatures, running service, unchanged
identity/settings and a fresh successful heartbeat. Also check the dashboard's
up-to-date result. A successful download or installer launch alone is not a
completed upgrade test.

## Controller clock

Keep the controller NTP-synchronised. Agent and capture timestamps are UTC;
incorrect controller time makes relative-time charts appear empty even when
ingest succeeds.
