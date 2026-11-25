# ZenPlus 1.25.29

Updates no longer roll back on appliances whose web service starts slowly.

After an update restarts the services, the updater checks that the web
service answers. That check gave up after about 15 seconds, and some
appliances need about that long before the web service accepts connections,
so good releases were rolled back. The update now waits up to 3 minutes for
the web service to answer before that check (also during a rollback). If the
service stops instead, the wait ends at once and the update log records the
service state and its latest log lines.

The wait is part of the update package, so it applies when installing this
release, whatever updater version the appliance runs. The updater installed
with this release also keeps retrying its own check for up to 3 minutes.

Includes everything in 1.25.28. No new database migration. The required
update checkpoint remains 1.24.36. Appliance-only package; signed Windows
agent catalog preserved. Details: PR #201.
