# ZenPlus Agent 1.14.1

Windows x64, built 2026-09-27. Public Authenticode publisher: **Zentryc LLC**,
issued through Microsoft Artifact Signing and timestamped by Microsoft.

Upgrade and presentation release. Includes every 1.14.0 change.

## Upgrades from the appliance

- **Exact version.** An upgrade started in Agent Fleet ("Trigger upgrade", or
  a desired version) installs exactly the requested version, downloaded from
  the appliance and verified (SHA-256, Zentryc LLC Authenticode signature,
  publisher pinning) before it runs. It is no longer "whatever is newest".
- **Reported progress.** The agent reports downloading, installing, succeeded
  or failed in its heartbeat. Agent Fleet shows it next to the version. A
  failure includes the reason, and a Setup rollback includes the end of
  Setup's log (`logs\update-<version>.log`).
- **Survives the restart.** Setup stops the agent, so each attempt is recorded
  on disk first. After the restart the agent reports whether the new version
  is running.
- **No retry storms.** A failed version is retried after an hour, not on every
  heartbeat. A different version is attempted at once. Downgrades are refused
  and reported.

## Tray and dashboard

- Brand mark in the dashboard header and the installer banner.
- Tray icon shows status as a badge: green (reporting), amber (connecting or
  updating), red (problem), grey (stopped).
- Controller connectivity comes from the agent's own heartbeat. Appliances
  with internal certificates no longer show a false "Controller unreachable".
- The APM tiles are hidden on server-monitoring-only installs.
- The Updates tile shows an appliance-requested upgrade and its result.

## Controller (same release branch)

- Migration `migrate-123-agent-update-status.sql` adds `agents.update_status`.
- The heartbeat stores the reported update progress. It clears
  `desired_version` once the agent runs it.
- `GET /api/v1/agents/packages/manifest?version=` returns one published
  version. `GET /api/v1/agents/packages/{platform}/versions/{version}`
  downloads it, verified against its recorded hash like the latest package.
- "Trigger upgrade" offers the versions published on the appliance and
  rejects versions that are not published.
- The Download Agent dialog downloads from the appliance, showing the
  published version. zentryc.com is offered for the MSI and as the fallback.
- A duplicated block in the heartbeat handler was removed.

## Compatibility

- Agents before 1.14.1 ignore the new fields and upgrade to the latest
  published package as before.
- Controllers without this release ignore the `version` parameter. A pinned
  upgrade then falls back to the public channel only if its latest release
  is the requested version.
