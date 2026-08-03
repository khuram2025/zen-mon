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
- Build `zenplus-agent-<version>.msi` on a release machine with WiX installed.
- Confirm the MSI `ProductVersion` matches `model.AgentVersion`.
- If the build bakes in a controller URL/token, confirm both appear in
  `dist\zenplus-agent.exe` and that the token is one you intend to publish.

## Signing

- Authenticode-sign every EXE.
- Authenticode-sign the MSI.
- Use a trusted code-signing certificate and timestamp server.
- Verify signatures with `Get-AuthenticodeSignature`.

## Installer QA

- Fresh install on a clean Windows VM.
- Current-user install without administrator rights.
- All-users install with administrator approval.
- Upgrade over a previous ZenPlus install.
- Upgrade over a legacy `%ProgramData%\ZenPlus\Agent\bin` install.
- Quiet install with `CONTROLLER_URL`, `ENROLLMENT_TOKEN`, `SITE_ID`, and `POLICY_ID`.
- Quiet install with **no** properties enrols using the baked-in controller/token.
- MSI properties override the baked-in controller URL and token.
- Download from `/api/v1/agents/packages/windows/latest` matches the manifest SHA-256.
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

## Security

- Verify config and state live under `%ProgramData%\ZenPlus\Agent`.
- Verify credentials are stored with DPAPI and are not printed in logs/UI.
- Verify installer does not expose enrollment tokens after enrollment.
- Review whether `LocalSystem` is still required for collectors before release.

## Release Notes

- Include artifact hashes.
- Include supported silent install and uninstall commands.
- Include signing certificate subject and timestamp details.
