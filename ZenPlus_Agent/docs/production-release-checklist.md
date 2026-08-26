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
- Upgrade over previous standalone and MSI ZenPlus installs, including a host
  with multiple stale related MSI registrations; verify one current Programs &
  Features entry, one `ZenPlusAgent` service, and one running agent process.
- Upgrade over a legacy `%ProgramData%\ZenPlus\Agent\bin` install.
- Verify upgrades preserve ProgramData config, durable identity, DPAPI
  credentials, spool, instrumentation state, and monitoring profile.
- Upgrade a pre-1.11.3 `combined` config whose infrastructure collectors are
  all disabled; CPU, memory, filesystem, disk, network, process, service,
  event-log, and inventory collection must be repaired without a profile arg.
- Quiet install with only `CONTROLLER_URL`.
- For an appliance using an internal/self-signed CA, provision a PEM bundle and
  install with `CONTROLLER_CA_FILE`; verify Server and APM traffic authenticate
  it while `verify_tls` remains enabled.
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

## APM QA

- Confirm the packaged gateway reports the version recorded in
  `apm\bundle-manifest.json` and lists `otlp`, `memory_limiter`, `resource`,
  `batch`, `otlp_http`, `health_check`, and `file_storage` components.
- Run the gateway `validate` command against the ZenPlus trace pipeline before
  packaging; a missing factory is a release blocker.
- Send an OTLP/HTTP protobuf trace through `127.0.0.1:4318` and verify it is
  stored by the appliance with the expected `zenplus.agent_id` and
  `zenplus.server_id` resource attributes. A healthy gateway port or accepted
  HTTP count alone is not sufficient evidence.
- Upgrade an agent with a legacy/unbound APM key (no credential metadata), and
  clone/re-enrol an agent with changed IDs. Verify each obtains a newly bound
  APM credential, rewrites the gateway environment/config, restarts the
  gateway, and stores an attributed span under the new agent/server IDs.
- Instrument and roll back a representative IIS pool plus .NET, Java, and
  Node.js Windows services. Verify existing environment values are restored,
  runtime switches remove stale settings, and stopped targets remain stopped.
- Disable instrumentation without restart and confirm the target remains
  upgrade/uninstall-protected until its process is stopped or restarted.
- Revoke the active APM ingest key and confirm the agent detects the export 401,
  replaces the stale bound credential, and resumes accepted traces.
- Send and reject known trace batches and confirm the local one-minute
  forwarded/error counters, persistent-queue depth/bytes, and dropped-span
  count follow the gateway's loopback-only internal telemetry. Confirm
  `telemetry-gateway.log` rotates at 10 MiB during runtime and startup and
  retains no more than three backups.
- Verify discovered process command lines contain argument shapes only and do
  not expose paths, URLs, credentials, SQL, or argument values.
- Create clean CPython 3.10, 3.11, 3.12, and 3.13 x64 virtual environments and
  run the bundled Python helper with network disabled; `pip check` must pass.
- Run `npm audit --omit=dev` against the locked Node runtime pack and record the
  result in release evidence.
- Confirm UI and release notes say "traces" rather than claiming OTLP
  application metrics or log ingest. Infrastructure metrics are a separate
  agent data path.

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
- Include the supported .NET/Java/Node/Python runtime matrix, trace-only signal
  scope, and application-owned runtime prerequisites.
- Generate and review a complete third-party software bill of materials and
  notices for the gateway plus every Node and Python dependency.
