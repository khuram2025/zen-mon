# ZenPlus Agent 1.14.0

Windows x64, built 2026-09-27. Public Authenticode publisher: **Zentryc LLC**,
issued through Microsoft Artifact Signing and timestamped by Microsoft.

Performance release. Includes every 1.12.10 and 1.13.0 change. Not yet
published to the stable channel or to any appliance.

## Measured on the build host

Same configuration for both versions, steady state after start-up:

| | 1.12.9 | 1.14.0 |
| --- | --- | --- |
| Agent CPU | 0.62% of one core | 0.04% of one core |
| PowerShell processes started | about 3-4 per cycle, ~110 MB each | none |
| Agent private working set | 13.7 MB | 14.3 MB |
| One full collection cycle | 5.5 s | 0.12 s |
| Programs in a server-monitoring install | 5 | 3 |

## Changes

- **No more PowerShell on the collection path.** CPU and memory counters use a
  persistent Windows performance-counter query, CPU utilisation is the average
  since the previous collection (no one-second sampling sleep), services come
  from one Service Control Manager call, event-log counts from direct event
  queries, and installed software from the registry. Counter names are
  English-only, so localized Windows now reports queue length and cache and
  committed memory, which it previously did not.
- **One call for all processes.** Process metrics come from a single system
  query instead of about eight per process. Protected processes such as
  Defender are now included.
- **Smaller uploads.** The services, filesystem and network-interface
  inventory is sent when it changes and otherwise every 10 minutes. Result
  batches are gzip-compressed once the controller advertises support (this
  release's controller does).
- **Local queue scales.** The upload queue keeps an in-memory index. With 500
  queued batches, adding a batch takes 1.5 ms instead of 205 ms, and trimming
  to the size cap 1.6 ms instead of 27 s.
- **Rotating log.** `agent.log` rotates at 10 MB with 3 backups. Rotation
  survives the dashboard holding the file open.
- **Quieter idle loop.** Status files are written on change (or once a
  minute), the config is re-read only when it changes on disk, a 64 MiB soft
  heap limit applies, and the tray dashboard refreshes every minute while
  hidden.
- **Fewer programs.** `zenplus-agentctl` commands are subcommands of
  `zenplus-agent.exe` (a `zenplus-agentctl.cmd` shim keeps scripts working),
  and the per-user background agent is `zenplus-agent-app.exe --run-agent`.

## Controller (same release branch)

- Agent uploads with `Content-Encoding: gzip` are decompressed with size
  limits, and the heartbeat response advertises `accepts_gzip`.

## Validation

- All Go package tests and `go vet ./...` passed. New tests compare the
  native collectors with PowerShell on the host (services against
  Win32_Service, event counts against Get-WinEvent, the process snapshot
  against per-process queries). Further new tests cover the upload-queue
  index against a reopened store, inventory refresh, gzip uploads, log
  rotation while the file is held open, status publishing and the merged
  runner and shim.
- Server: 84 tests pass on the appliance's Python environment, including
  the gzip middleware (decompression, size caps, invalid and truncated
  bodies).
- Both setups and MSIs and all embedded programs have valid, timestamped
  Zentryc LLC signatures. `zenplus-agent version` reports 1.14.0. Both
  setups pass `/verify-payload`. The pinned self-update verifier accepts all
  four packages and rejects a tampered copy. Microsoft Defender found no
  threats.

| Package | 1.12.10 | 1.13.0 | 1.14.0 |
| --- | --- | --- | --- |
| Server monitoring (`zenplus-agent-core-*`) | n/a | 26 MB | 20 MB (about 29 MB installed) |
| Server monitoring + APM (`zenplus-agent-*`) | 205 MB | 93 MB | 87 MB |

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `zenplus-agent-core-1.14.0.exe` | 21265328 | `bd2ac47aa177d70083cb4eb03dcb886ca5152e9d7dcca1ffa8e1355743d06d84` |
| `zenplus-agent-core-1.14.0.msi` | 22491136 | `4fb6616c0fb89c32c930ee0be8d6772c7ddeff13b4127e23d501ca007af2166c` |
| `zenplus-agent-1.14.0.exe` | 90790824 | `5574970ce124e74fab2e3da5af8a8db2a0b4b2f11fb047a49876c46412f66a24` |
| `zenplus-agent-1.14.0.msi` | 92082176 | `355d875ae78b90706832d71bfe8a7374937ef28f37c53457895a24ae4878e158` |

Limits: no live install or upgrade on a separate machine yet, and the
controller changes (flavour migration, digest cache, gzip) are not deployed.
Measurements come from foreground runs on the build host with an
unreachable controller, not from a production server under load.
