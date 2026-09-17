# ZenPlus 1.23.14

Fixes the appliance update failure in 1.23.13: source files from a private build checkout retained mode 0600, preventing the unprivileged API service from importing them. Release archives now normalize public code and dashboard permissions, and publication verification rejects unreadable or unsafe modes. The compatibility hook also repairs incoming code directories when an older updater runs with a restrictive umask; appliance credentials remain private.

Rollback stops services before restoring binaries, replaces files atomically to avoid Linux `ETXTBSY`, checks recovered API health, and reports incomplete recovery accurately. Runtime updater locks are excluded from code reconciliation and backup.

Includes the application changes from 1.23.13. Signed appliance-only release; Windows installers remain separate. Minimum version: 1.23.9. The faulty 1.23.13 rollout is paused.
