# Project guidance

## Credentials

- Keep private credentials in the workspace-root `.env.local`; load them without printing values. Never commit credentials, runtime state, private keys or generated packages.
- Document variable names with safe placeholders in `.env.example`. Ask for missing variables by name only.
- Never publish unsigned Windows installers or unsigned OTA packages.

## Source and releases

- The installed application lives under `/opt/zenplus`. Build and test in a separate checkout. Preserve appliance-local edits, compare fingerprints before deployment, and back up affected source and binaries.
- Continue source work through branches and reviewed pull requests. Follow `Documentation/26-GITHUB-MULTI-MACHINE-AND-PUBLISHING-RUNBOOK.md` and `Documentation/15-RELEASE-RUNBOOK.md` for releases. Do not bypass independent review or signing gates.
- Migration files and historical lockfile entries are append-only. Add a forward migration for new schema; ship the complete migration set.
- Run the strict dashboard build, route smoke checks, Python contracts and Go tests for the modules changed. Database integration tests require disposable fixtures, never installed application databases.
- `dashboard/src/main.tsx` and `App.tsx` define the active UI. Avoid reintroducing superseded duplicate pages or a second auth store.

## Device Tracker

- Sidebar groups and breadcrumb ownership are in `dashboard/src/components/layout/navigation.ts`. Device Tracker is separate from Monitoring.
- Preserve prior IP observation periods. Summarize repeated addresses for display; do not delete history to remove duplicates.
- ARP/ND sightings from separate reporters do not establish globally exclusive IP ownership. Interface indexes are reporter-local, not global subnet/VRF identifiers.
- `udt_ip_evidence` retains endpoint/IP/reporter/interface/source observations. Replacement primary addresses require three observations within 24 hours from one reporter/interface/source. Never fabricate repeat evidence from legacy history counts.
