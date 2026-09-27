# ZenPlus 1.23.17

ZenPlus Agent 1.14.3 for Windows, and a fix for changing an agent's
monitoring profile from the appliance.

## Fix: Change monitoring profile

On 1.23.16, **Change monitoring profile** (Agent Fleet and the server page's
Agent tab) failed with "Request failed with status code 500". The database's
list of allowed agent commands did not include the new profile-change
command.

- **Migration 125** adds `set_monitoring_profile` to that list. It is
  constraint-only and safe to repeat.
- **Constraint repair** (`run-migrations.py`) now uses migration 125, so
  repairing a drifted list keeps the new command.
- **Support bundle:** the database diagnostic checks for the same list.
- **Regression test:** a new test fails the build if any command the server
  sends to agents is missing from the allowed list, the canonical migration
  or the support check.

## ZenPlus Agent 1.14.3 (bundled)

This release includes all four packages, signed by Zentryc LLC: server
monitoring (core) and full with APM, each as EXE and MSI. Updated appliances
serve them and can upgrade agents to 1.14.3 from Agent Fleet.

- **New Setup wizard:** Welcome, Terms (accept / do not accept),
  Installation options, Connect to ZenPlus (with Test connection), Ready,
  Installing and Finished pages, with Back / Next / Cancel. The
  server-monitoring package has no monitoring-profile section. Install shows
  the administrator shield when approval is needed.
- **Standard users:**
  - **Credential prompt:** a standard user installing for all users gets the
    Windows credential prompt; the approval request previously could fail
    without showing one.
  - **Up-front check:** Setup warns in advance when this computer does not
    allow elevation.
  - **Failure reason:** Setup shows the reason if the elevated install fails.
- **Silent installs from a prompt** print their result in the Command Prompt
  or PowerShell window.

## Dashboard

The Install agent dialog's mass-deployment section explains that
deployment-tool commands need `start "" /wait` (Command Prompt) or
`Start-Process -Wait` (PowerShell) when typed at a prompt.

## Upgrade notes

- Requires appliance version 1.23.9 or later.
- Adds migration 125 (a constraint change only).
- Appliances that applied migration 125 by hand record it again safely.
