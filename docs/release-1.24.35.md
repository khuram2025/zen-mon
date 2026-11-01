# ZenPlus 1.24.35

Complete server event monitoring release, including signed Windows agent
1.14.4 installers in both core and full flavours, as EXE and MSI packages.

Includes grouped Windows event details, affected components, role-based
collection presets, persistent cursors, duplicate prevention, bounded batches,
compression and visible collection health. Detailed event history is retained
for 14 days.

Enabled restart, shutdown and agent-unresponsive rules feed Alert Center.
Restart and shutdown messages use readable dates in the configured appliance
timezone and distinguish requests from observed lifecycle transitions.
Notification channels can be attached to these rules later.

The appliance installs the agent packages into its local catalog. Enrolled
Windows hosts continue to use the existing controlled upgrade workflow.
