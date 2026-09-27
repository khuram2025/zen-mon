# ZenPlus Agent 1.14.2

Windows x64, built 2026-09-27. Public Authenticode publisher: **Zentryc LLC**,
issued through Microsoft Artifact Signing and timestamped by Microsoft.

Fleet-management release. Includes every 1.14.1 change.

## Change the monitoring profile from Agent Fleet

Select agents and choose **Change monitoring profile**:

- **Server monitoring only.** Agents with APM installed switch to the
  server-monitoring package, which removes the APM components. APM gateway
  processes stop, and disk and memory use drop.
- **Server monitoring + APM** / **APM only.** Agents without APM download
  the APM package from the appliance and install it. Instrument applications
  afterwards from APM.
- **Package choice.** The package is the agent's current version when the
  appliance publishes it in the other package, otherwise the newest published
  one, never an older one. A change that needs no package swap (for example
  combined to APM only) just rewrites the profile.
- **Instrumented applications.** Applications instrumented by ZenPlus APM
  load its profiler, so turning APM off first restores them to their original
  settings, which restarts them. That happens only when *Also remove APM
  instrumentation* is ticked. Otherwise the agent refuses and reports how
  many applications are affected.
- **Progress** appears next to the version: Adding APM, Removing APM, then
  APM added / APM removed, or the failure and its reason. A failed swap is
  rolled back by Setup, and the agent keeps running the previous package.
- **Skipped agents.** Agents that cannot take the change are skipped and
  named with the reason: older than 1.14.2, current-user installations,
  disabled agents, or the needed package not published on the appliance.
- **Monitoring column.** Agent Fleet shows each agent's profile, and flags
  "APM installed, unused" where the APM package runs server monitoring only.

## Mass deployment

- **Exit codes.** Setup's exit codes follow Windows Installer: 1639 invalid
  switches or profile for the package, 740 not elevated, 1618 another
  installation in progress, 1603 failed. Intune and Configuration Manager
  retry 1618 automatically.
- **No overlapping installs.** Installation and removal take a system-wide
  lock, so a deployment-tool install cannot collide with a self-update or a
  second deployment.
- **`install.ps1`.** The appliance's script now checks the Zentryc LLC
  signature as well as the SHA-256, and exits with Setup's code.
- **Dashboard.** The Install dialog lists ready-to-copy EXE, MSI and
  uninstall commands and the Intune/Configuration Manager detection rule.
- **Guide.** `docs/bulk-deployment.md` covers every switch and recipes for
  Group Policy, Intune, Configuration Manager, PDQ and remote PowerShell.

## Controller (same release branch)

- Migration `migrate-124-agent-monitoring-profile.sql` adds
  `agents.package_flavor` and `agents.monitoring_profile`, reported in
  heartbeats.
- Bulk action `change_monitoring_profile` with `monitoring_profile` and
  `remove_instrumentation`. The response lists skipped agents with reasons.

## Compatibility

- Agents before 1.14.2 are skipped by profile changes; upgrade them first,
  then change the profile.
- The Windows Settings dialog on the server still changes the profile
  locally, as before.
