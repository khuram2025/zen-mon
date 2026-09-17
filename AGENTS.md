# Project guidance

## Credentials

- Keep private credentials in the workspace-root `.env.local`; load them without printing values. Never commit credentials, runtime state, private keys or generated packages.
- Document variable names with safe placeholders in `.env.example`. Ask for missing variables by name only.
- Never publish unsigned Windows installers or unsigned OTA packages.

## Source and releases

- The installed application lives under `/opt/zenplus`. Build and test in a separate checkout. Preserve appliance-local edits, compare fingerprints before deployment, and back up affected source and binaries.
- Continue source work through branches and pull requests. The sole repository owner explicitly authorized zero required independent approvals; retain all five required CI checks, conversation resolution and force-push/deletion protections. Do not reintroduce a second-reviewer requirement for this solo workflow. Follow the release runbooks with this owner-approved policy. Never bypass signing verification.
- Signed appliance-only releases are supported and preserve separately installed Windows agent packages. Windows installer signing is required when an installer is bundled, not for an appliance-only OTA package.
- Release tarballs must normalize public code/assets to 0644 (executables/directories 0755), including nested dashboard archives. Verify permissions and service-account readability even when the build checkout uses umask 077. Never broaden appliance credential permissions. Rollback must stop binary consumers before legacy restore and replace running executable inodes atomically.
- Migration files and historical lockfile entries are append-only. Add a forward migration for new schema; ship the complete migration set.
- Run the strict dashboard build, route smoke checks, Python contracts and Go tests for the modules changed. Database integration tests require disposable fixtures, never installed application databases.
- `dashboard/src/main.tsx` and `App.tsx` define the active UI. Avoid reintroducing superseded duplicate pages or a second auth store.
- Service-detail SLA uses `basis=confirmed`: only confirmed incidents affect availability; isolated failed probes remain diagnostic evidence. Keep monitoring gaps unknown and retain configured retry thresholds.
- Device list/detail availability headlines share `/devices/dashboard/uptime-stats` and two-decimal formatting. Use `uptime_pct` and `sample_count` for partial failures; legacy rollup `is_up` is only a majority flag.
- Device widget layouts are scoped by user/device/tab in browser storage. Vendor/type templates match normalized vendor + device type + tab + user; device overrides win. Preserve layout preferences for temporarily absent widgets.
- F5 overall RAM uses sysGlobalHostMemUsed / sysGlobalHostMemTotal, never the highest TMM/non-TMM percentage. Keep TMM, non-TMM and swap separate; use `f5_system_memory_pct` for corrected history. Missing sensors are not zero, and certificate inventory does not prove active TLS bindings.

## Device Tracker

- Sidebar groups and breadcrumb ownership are in `dashboard/src/components/layout/navigation.ts`. Device Tracker is separate from Monitoring.
- Preserve prior IP observation periods. Summarize repeated addresses for display; do not delete history to remove duplicates.
- ARP/ND sightings from separate reporters do not establish globally exclusive IP ownership. Interface indexes are reporter-local, not global subnet/VRF identifiers.
- `udt_ip_evidence` retains endpoint/IP/reporter/interface/source observations. Replacement primary addresses require three observations within 24 hours from one reporter/interface/source. Never fabricate repeat evidence from legacy history counts.
