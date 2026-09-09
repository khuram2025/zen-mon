# ZenPlus 1.23.8

HTTP monitoring can now select Auto, IPv4 only or IPv6 only per service. Auto remains the default for existing checks. The setting applies to scheduled controller and sensor probes, manual tests, redirects and authenticated workflows. Hostnames, DNS resolution and certificate verification are preserved.

Scheduled HTTP results retain connection diagnostics: DNS time, TCP connection time, TLS handshake time, selected remote IP and failure stage. The latest-probe panel displays these values. Timings accumulate across attempts and workflow connections; missing phases remain absent. Existing history and older sensor uploads remain valid.

The reliability form now says **Failures before Down** and **Sensor retry delay**. Help explains that the controller counts consecutive scheduled failures while sensors retry within one run. New checks created in the form start at two failures; existing check thresholds are preserved. Individual failed probes continue to contribute to availability history.

For networks without IPv6 connectivity, select **IPv4 only** on the affected HTTP check. This prevents IPv6 connection attempts; it does not repair an intermittent DNS resolver problem or bypass TLS checks. No domain-specific rules or permanent IP mappings are included.

## Validation

- Go checker, store, controller and sensor tests with the race detector, including IPv4, IPv6 loopback, redirects, timing capture and untrusted certificate rejection.
- API tests covering address-family validation, manual HTTP connections, certificate trust, workflows, availability and sensor compatibility.
- Production Vite build. The full TypeScript check has pre-existing failures; comparison against unchanged main produced no additional errors.

The additive ClickHouse migration 114 stores optional diagnostic metadata. Update the controller before deploying the included sensor binary. This appliance release does not replace workstation/server agent packages.
