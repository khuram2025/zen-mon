# ZenPlus 1.23.16

Redesigned server page, agent management from the appliance, and ZenPlus Agent
1.14.2 for Windows.

## Server page

Every server opens on a new page built for day-to-day operations.

- **Header:** health, address, OS, and the agent's version and monitoring
  profile, with six live tiles (CPU, memory, fullest disk, network,
  processes, services). Each tile shows its last hour and opens the tab that
  explains it.
- **Tabs:** Overview, Performance, Processes, Services, System, Events,
  Alerts, Compliance and Agent. Each tab shows a count when something needs
  attention. Links to the previous page layout still open the same content.
- **Overview:** starts with what needs attention (open alerts, agent
  problems, failed upgrades, volumes filling or forecast to fill, stopped
  automatic services, watched processes, compliance findings, clock drift),
  each linking to its fix. Below that are the last hour of resources, system
  and agent facts, volumes, top processes and per-core load.
- **Performance:** covers every resource the agent collects:
  - CPU user and system time, processor queue and a per-core heatmap.
  - Memory used, committed and cache, and the page file.
  - Throughput and I/O operations for each disk.
  - Usage for each volume, with growth over the last week and a
    days-until-full forecast once a day of history exists.
  - Throughput, packets, errors and drops for each network interface.

  All charts share one crosshair.
- **Events:** event counts by level over time.
- **Compliance:** a compliance score.
- **Agent:** upgrade the agent, switch its monitoring profile, run agent
  actions, view command history and edit server settings.

## Agent management from the appliance

- **Upgrades:** Trigger upgrade installs exactly the chosen published
  version. The agent reports downloading, installing, succeeded, or failed
  with the reason, and Agent Fleet shows the progress. A failed version is
  retried after an hour, not on every heartbeat.
- **Change monitoring profile:** adds APM (installs the APM package) or
  removes it (installs the server-monitoring package). Before removing APM,
  applications instrumented by it are restored; this happens only with
  explicit consent, because it restarts them. Agents that cannot take a
  change are listed with the reason.
- **Agent Fleet** shows each agent's profile and flags APM installed but
  unused.
- **Download agent** serves the installers from the appliance, so no internet
  access is needed.
- **Mass deployment:** the install dialog lists silent EXE, MSI and uninstall
  commands, exit codes and the detection rule for Group Policy, Intune and
  Configuration Manager. The appliance's install script checks the Zentryc LLC
  signature and returns Setup's exit code.

## ZenPlus Agent 1.14.2 (bundled)

This release includes all four Windows packages, Authenticode-signed by
Zentryc LLC: server monitoring (core) and full with APM, each as EXE and MSI.
Updated appliances serve and upgrade both.

- **Setup exit codes** follow Windows Installer: 1639 invalid switches, 740 not
  elevated, 1618 another installation running, 1603 failed. Installations
  never overlap.
- **Monitoring-profile changes** from the appliance, and progress reporting
  for upgrades.
- **Dashboard:** the Windows dashboard shows a status disc, the full profile
  name and plain-language connection messages.
- **Version information:** every executable carries Zentryc LLC version
  information, and Programs and Features lists the publisher as Zentryc LLC.

## Upgrade notes

- Requires appliance version 1.23.9 or later.
- Adds migrations 123 (`agents.update_status`) and 124
  (`agents.package_flavor`, `agents.monitoring_profile`). Both only add
  columns.
- Agents upgrade from Agent Fleet. Agents before 1.14.2 are skipped by
  monitoring-profile changes until upgraded.
- Windows agents do not yet report disk latency or busy time: the library they
  use truncates these timings. The page says so rather than showing wrong
  numbers.
