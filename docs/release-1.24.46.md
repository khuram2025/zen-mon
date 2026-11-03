# ZenPlus 1.24.46

Separate checking for appliance updates from installing them. Check for Updates
reads the eligible upgrade path without downloading packages or starting the
installer. Review installation shows the required intermediate versions and lets
the operator install through any offered step or complete the required path.
Install now explicitly starts the selected path, including outside the automatic
maintenance window, and stops at the selected version.

Bind manual installation to the reviewed release identities and package hashes.
Recheck eligibility before each step, stop on changes or failures, and reload the
new updater between steps. Keep signature, prerequisite, migration, backup and
health gates. A bounded root helper queues a fixed systemd template; an atomic
file lock excludes concurrent manual and scheduled installations. Manual failures
before download are visible on the Updates page.

Scheduled runs now honor Automatic Updates and the maintenance window in the
appliance's local timezone. With automatic installation disabled they check in
without installing. Explicit operator CLI installation remains available.

No schema migration. Appliance-only release; requires the signed 1.24.36 catalog
bridge. Preserve SMS delivery and controller restart protections from 1.24.45.
