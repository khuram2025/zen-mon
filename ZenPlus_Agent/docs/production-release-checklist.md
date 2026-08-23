# ZenPlus Agent Production Release Checklist

Use this checklist before distributing ZenPlus Agent outside a test environment.

See `deployment.md` for the build → publish → install flow.

## Build

- Run `.\build.cmd`.
- Confirm `go test ./...` passes.
- Confirm these artifacts exist in `dist`:
  - `zenplus-agent.exe`
  - `zenplus-agentctl.exe`
  - `zenplus-agent-app.exe`
  - `ZenPlusAgentSetup-x64.exe`
  - `zenplus-agent-<version>.exe`
  - `zenplus-agent-<version>.msi` (optional enterprise wrapper)
  - `agent-manifest.json`
- Confirm the MSI `ProductVersion` matches `model.AgentVersion`.
- Confirm the MSI summary template is `x64;1033`, its components are 64-bit,
  and every EXE has PE machine type `0x8664`.
- Confirm the MSI and binaries contain no enrollment-token placeholder or
  compiled enrollment credential.
- Confirm the canonical setup SHA-256 and byte length match `agent-manifest.json`.

## Signing

- Authenticode-sign every EXE.
- Authenticode-sign the MSI.
- Use a trusted code-signing certificate and timestamp server.
- Verify signatures with `Get-AuthenticodeSignature`.
- Confirm an unsigned build fails the production workflow before publication.
- Confirm the public manifest requires Authenticode and points only to immutable HTTPS release URLs.

## Installer QA

- Fresh install on a clean Windows VM.
- Current-user install without administrator rights.
- All-users install with administrator approval.
- Upgrade over a previous ZenPlus install.
- Upgrade over a legacy `%ProgramData%\ZenPlus\Agent\bin` install.
- Quiet install with only `CONTROLLER_URL`.
- A new install appears as pending without an authentication retry storm.
- MSI properties override only the default controller URL.
- Appliance approval issues a unique credential and the agent begins reporting.
- Download from `/api/v1/agents/packages/windows/latest` matches the manifest SHA-256.
- Download from `https://zentryc.com/downloads/zenplus-agent/` matches the public manifest SHA-256.
- Uninstall without purge preserves config/state.
- Current-user uninstall with purge removes `%LOCALAPPDATA%\ZenPlus\Agent`.
- All-users uninstall with purge removes `%ProgramData%\ZenPlus\Agent`.
- Guided installer UI shows install scope, controller settings, acceptance policy, progress, and launch-after-install option.
- Programs & Features entry displays publisher, version, icon, and quiet uninstall string.
- Start Menu shortcuts launch dashboard, status, and uninstall correctly.

## Runtime QA

- `ZenPlusAgent` service is `Automatic` and `Running` after install.
- Service recovery restarts the service after failure.
- Dashboard starts with `--start-hidden` after sign-in.
- Dashboard shows the current version and checks the public update channel at startup, every six hours, and on demand.
- A valid signed newer release is offered; unsigned or checksum-invalid releases are blocked.
- Only one dashboard/tray process runs per user session.
- `zenplus-agentctl service-status` matches Services.msc.
- Dashboard reports service stopped/degraded/healthy correctly.
- Dashboard remains responsive during controller outage and spool growth.
- During a controller outage, retries back off exponentially (no tight retry loop).
- After the controller revokes an agent's key, the agent stops sending and
  re-enrols instead of repeatedly retrying unauthenticated.
- Restoring a golden image / cloning the VM produces a new `agent_uid` rather
  than two hosts sharing one identity.
- Controller reports `System clock synchronized: yes`; agents show
  `clock_skew_s` near zero and detail-page charts render points.
- Heartbeat advertises `network_capture_v1`, `capture_stop_v1`, and
  `interface_traffic_v1`.
- A five-minute capture starts on demand, streams running updates, completes,
  and records process/service, endpoints, ports, and available byte totals.
- Stopping a capture produces `cancelled`; duplicate start/stop commands are
  idempotent and do not create concurrent collectors.
- Interface samples show cumulative RX/TX bytes, current/peak bit rates, link
  speed, and utilisation for the selected NIC or all NICs.

## Security

- Verify config and state live under `%ProgramData%\ZenPlus\Agent`.
- Verify credentials are stored with DPAPI and are not printed in logs/UI.
- Verify installer and Settings expose no token, site, or policy input.
- Verify `zenplus-agentctl print-config` and logs redact credential and bearer values.
- Review whether `LocalSystem` is still required for collectors before release.

## Release Notes

- Include artifact hashes.
- Include supported silent install and uninstall commands.
- Include signing certificate subject and timestamp details.
