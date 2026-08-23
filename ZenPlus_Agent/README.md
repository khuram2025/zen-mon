# ZenPlus Windows Agent MVP

This workspace contains a runnable Windows host agent based on `agent.md.txt`.

## Quick Start

```powershell
.\build.cmd
.\run-agent-once.cmd
.\status.cmd
.\open-agent-app.cmd
```

The included config points to the test controller at `https://192.168.8.221`.
If the controller has not shipped the Windows-agent endpoints yet, the agent keeps collecting locally and spools batches in `data\state\spool.db` until upload succeeds.

## Registration and authorization

Configure only the appliance controller address:

```yaml
controller_url: https://192.168.8.221
```

The agent registers with the appliance and appears as **Pending authorization**
in Agent Fleet. After an appliance administrator approves it, the appliance
issues a unique durable credential. The agent protects that credential with
Windows DPAPI and then begins heartbeats and uploads. No token, site, or policy
identifier is entered on the endpoint.

## Commands

```powershell
.\dist\zenplus-agent.exe run --config .\config\agent.yaml
.\dist\zenplus-agent.exe run --config .\config\agent.yaml --once
.\dist\zenplus-agent.exe register --config .\config\agent.yaml
.\dist\zenplus-agent.exe install-service --config .\config\agent.yaml
.\dist\zenplus-agent.exe uninstall-service
.\dist\zenplus-agent-app.exe --config .\config\agent.yaml

.\dist\zenplus-agentctl.exe status
.\dist\zenplus-agentctl.exe status --json
.\dist\zenplus-agentctl.exe collect-now
.\dist\zenplus-agentctl.exe register
.\dist\zenplus-agentctl.exe print-config
.\dist\zenplus-agentctl.exe service-status
.\dist\zenplus-agentctl.exe reset-enrollment --force
```

## Windows Installer

Build the self-contained setup executable:

```powershell
.\build.cmd
```

The release artifacts are written to:

```powershell
.\dist\ZenPlusAgentSetup-x64.exe
.\dist\zenplus-agent-1.7.0.exe
.\dist\zenplus-agent-1.7.0.msi
.\dist\agent-manifest.json
```

The standalone x64 setup executable is the canonical, immutable Windows
package and the interactive wizard. Download it once and distribute the same
checksum-verified package through GPO, Intune, SCCM, or another fleet tool.
Only `CONTROLLER_URL` is accepted as an endpoint registration setting. An MSI
wrapper is also built for validation, but the appliance publishes the setup
executable as the supported install and upgrade path.

For a signed release, provide a certificate thumbprint and require signing so the build cannot silently publish an unsigned artifact:

```powershell
.\scripts\build.ps1 -ControllerUrl "https://controller.example" -SigningThumbprint "<sha1-thumbprint>" -RequireSigning
```

The certificate may be in the current-user or local-machine `My` store. `ZENPLUS_SIGNING_THUMBPRINT` and `ZENPLUS_TIMESTAMP_URL` are supported for CI/release automation.

The stable public download and update channel is:

```text
https://zentryc.com/downloads/zenplus-agent/
```

The dashboard checks its HTTPS manifest at startup and every six hours. It only offers an update after the MSI checksum matches the manifest and Windows reports a valid Authenticode signature. CI therefore publishes a release only when signing succeeds; an unsigned preview remains visibly marked as signing pending and cannot be applied by self-update.

Interactive install:

```powershell
.\dist\ZenPlusAgentSetup-x64.exe
```

The interactive setup shows a guided installer UI with install scope selection,
the controller address, a clear pending-approval explanation, progress details,
and an optional launch-after-install step. After installation, the dashboard
Settings dialog can change only the controller address; authorization is
managed from the appliance.

Install for current user without admin rights:

```powershell
.\dist\ZenPlusAgentSetup-x64.exe /user
```

Install for all users as an administrator with the Windows service:

```powershell
.\dist\ZenPlusAgentSetup-x64.exe /machine
```

Unattended install:

```powershell
.\dist\ZenPlusAgentSetup-x64.exe /machine /quiet CONTROLLER_URL="https://monitor.example.com"
.\dist\ZenPlusAgentSetup-x64.exe /user /quiet CONTROLLER_URL="https://monitor.example.com"
```

Uninstall:

```powershell
.\dist\ZenPlusAgentSetup-x64.exe /machine /uninstall
.\dist\ZenPlusAgentSetup-x64.exe /machine /uninstall /quiet /purge
.\dist\ZenPlusAgentSetup-x64.exe /user /uninstall
.\dist\ZenPlusAgentSetup-x64.exe /user /uninstall /quiet /purge
```

Use the `/machine` uninstall commands from an elevated prompt, or approve the administrator prompt, when removing an old all-users install under `%ProgramFiles%\ZenPlus\Agent`.

The setup installs binaries under `%ProgramFiles%\ZenPlus\Agent`, stores configuration and agent data under `%ProgramData%\ZenPlus\Agent`, registers and starts the `ZenPlus Agent` Windows service, creates Start Menu shortcuts, and registers a standard Programs & Features uninstall entry.
For current-user installs, setup installs under `%LOCALAPPDATA%\Programs\ZenPlus\Agent`, stores data under `%LOCALAPPDATA%\ZenPlus\Agent`, and starts a no-console per-user background runner plus the tray dashboard at sign-in.
It also creates Startup shortcuts that launch the dashboard with `--start-hidden`, so the companion app starts in the Windows notification area/tray after sign-in. Windows decides whether that tray icon is shown directly or under the hidden-icons overflow.
During install and uninstall the setup removes stale Start Menu entries, stops old ZenPlus agent/dashboard processes without showing console windows, removes the legacy `%ProgramData%\ZenPlus\Agent\bin` runtime folder, and reinstalls the service with restart-on-failure recovery actions for machine installs.

To avoid Microsoft SmartScreen or "Unknown Publisher" warnings for customers, sign the MSI, setup, and embedded executables with a trusted Authenticode code-signing certificate in the release pipeline. Recompute the published SHA-256 after signing and never patch a signed MSI.

## Implemented

- Foreground agent runner and Windows service mode.
- Windows DPAPI credential storage.
- Enrollment contract from `agent_srv_team.txt`, including stable `agent_uid`.
- Heartbeat, config polling, command polling, and uncompressed `results/host` batch upload.
- Durable local spool with bbolt.
- CPU, memory, filesystem, disk IO, network, process, service_state, event_log, agent_health, and inventory samples using the `kind/timestamp/data` contract.
- Windows CPU user/system/iowait/load fields, memory committed/cache counters, and host boot time/uptime inventory.
- Controller-only registration and appliance-managed authorization without reinstalling.
- Local status file and `zenplus-agentctl` commands.
- Native Windows desktop dashboard through `zenplus-agent-app.exe`.
- Windows tray/taskbar companion with Open Dashboard, Collect Now, hide-to-tray, and Quit UI actions.
- Single-instance dashboard/tray process guard.
- Service status visibility in both dashboard and `zenplus-agentctl service-status`.
- On-demand, cancellable network capture with process/service, endpoint, port, protocol, and byte counters.
- Per-interface cumulative traffic, current/peak throughput, link speed, and utilisation samples during capture windows.

## Controller Contract

The agent expects these endpoints:

- `POST /api/v1/agents/enroll`
- `POST /api/v1/agents/heartbeat`
- `GET /api/v1/agents/config`
- `POST /api/v1/agents/results/host`
- `POST /api/v1/agents/commands/poll`
- `POST /api/v1/agents/commands/{id}/result`
- `POST /api/v1/agents/network-capture`

Heartbeat capabilities in version 1.7.0 are `network_capture_v1`, `capture_stop_v1`, `interface_traffic_v1`, and `apm_status_v1`. The installer and agent Settings include a local APM enable/disable switch. The dashboard distinguishes that local setting and listener detection from read-only APM availability and ingest activity supplied by the appliance.
