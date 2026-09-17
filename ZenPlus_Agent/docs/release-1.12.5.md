# ZenPlus Agent 1.12.5

Status: validated unsigned build. Do not publish or roll out until the EXE and
MSI are Authenticode-signed and the production signing checks pass.

## Spool uploader resilience

- Runs normal spool replay in a dedicated background worker so slow ingest
  cannot delay collection, heartbeat, or durable enqueue.
- Serializes replay for the whole spool and fences stale client generations,
  preventing duplicate in-process POSTs and late ACK/defer/quarantine changes
  after credentials or controller settings are replaced.
- Bounds each host-results request to 29 seconds, below the 30-second HTTP
  client and 45-second nginx deadline while leaving margin above the
  appliance's 5-second claim plus 20-second processing budget.
- Persists bounded exponential retry state and honors both Retry-After seconds
  and HTTP-date values.
- Defers 202 Accepted and known in-progress 409/503 responses, skips those
  batch IDs, and continues newer due records instead of blocking the FIFO.
- Preserves generic schema-validation failures for compatibility recovery,
  while a three-record/five-minute circuit breaker bounds controller load and
  durably advances past failed heads on the next replay cycle.
- Moves proven terminal/collision records into bounded metadata-only
  quarantine, retaining batch ID, reason, original size, and SHA-256 without
  maintaining a second full-payload queue.
- Reports quarantine depth/bytes and the next upload retry time through local
  status and `zenplus-agentctl`.

Collectors, heartbeat payloads, APM behavior, and dashboard code are unchanged.

## Validation

- `go test -cover -count=1 -timeout 180s ./...`: passed.
- Repeated stress runs: uploader 20x, spool 20x, background worker 5x: passed.
- `go vet ./...`: passed.
- Embedded installer payload verification: exit 0.
- Setup and agent PE architecture: amd64.
- MSI ProductVersion: 1.12.5.
- Independent adversarial review: no remaining P1/P2 issue in queue fairness,
  stale mutations, retry cooldowns, or worker lifecycle.

The race detector was not run because this Windows toolchain has CGO disabled.
WiX ICE validation was unavailable under the workstation's application policy;
payload verification and MSI metadata validation completed successfully.

## Validation artifacts

| Artifact | Bytes | SHA-256 | Authenticode |
| --- | ---: | --- | --- |
| `dist/zenplus-agent-1.12.5.exe` | 225,893,376 | `dd501d1af05f2f426415d99e268e8c14649e97ea8a6323bc9f014eb842995278` | NotSigned |
| `dist/zenplus-agent-1.12.5.msi` | 227,414,016 | `4b9373dc89546a47edd5191463b17bcd92a1a3ebc20565c94b59b1468cc72d20` | NotSigned |

## Production gate

Build on the signing host with an accessible code-signing private key and
Windows signing tools, using the repository's required-signing path. Verify
both signatures and regenerate the manifest before publishing:

```powershell
.\scripts\build.ps1 -RequireSigning -SigningThumbprint <CODE_SIGNING_THUMBPRINT>
```

Never upload the unsigned validation artifacts to the stable OTA channel.
