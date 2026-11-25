# Service check connections

How a service check reaches what it monitors: the address it connects to, and the certificates it accepts.
Both are appliance-wide settings an administrator manages. They are found from
**Settings > General > Monitoring > Service check connections**.

| Setting | Where it is edited | Stored in | API |
| --- | --- | --- | --- |
| Manual host addresses | Monitoring tab, "Service check connections" | `system_settings['monitoring.host_overrides']` | `/system/monitoring/host-overrides` |
| Trusted certificates | Security & TLS tab, "Service probe trust" (summarised and linked from the Monitoring tab) | `system_settings['security.probe_trust']` | `/system/security/probe-trust` |

## Manual host addresses

A manual host address tells service checks which address to connect to for a host name. Use it when a name is
not in the DNS the appliance uses, or when a name must be checked at one specific address.

### What changes and what does not

Only the address the check dials changes. The URL, the `Host` header, the TLS server name and the certificate
name check keep the name. This is the behaviour of `curl --resolve`.

- A certificate issued for another name still fails, even if it is valid for the address.
- Certificate trust scoped to a host name keeps applying to that name.
- A redirect to another host is looked up for that host: its own manual address if it has one, DNS otherwise.
- A check whose target is already an address is not affected. Addresses never match as names.

It is an application setting, never `/etc/hosts`. A hosts file would also redirect updates, licensing, mail and
directory sign-in, needs root, and does not reach remote sensors.

### Which checks use it

| Check type | Uses manual addresses |
| --- | --- |
| HTTP(S), including multi-step workflows and NTLM | Yes |
| TLS certificate | Yes |
| TCP port | Yes |
| ICMP ping | Yes |
| DNS | **Never.** A DNS check tests name resolution itself; a name that exists only as a manual address stays Down. |

Device polling (ping, SNMP), APM synthetic scenarios and every other outbound connection of the appliance do not
use manual addresses.

### Entries

Each entry has:

- **Host name**: one exact name, compared without case and without a trailing dot. No wildcards, no port, no URL.
  A subdomain is a different name. A name with non-ASCII characters is stored in its ASCII (punycode) form.
- **Address**: one IPv4 or IPv6 address. The unspecified address, multicast addresses and addresses with an
  interface zone are refused.
- **Applies**: everywhere, this appliance only, or one named sensor.
- **Note**: optional, up to 200 characters.

Rules:

- One name has one address per place. A second entry for the same name and place is refused (409).
- An entry for a place wins there over an "everywhere" entry for the same name. Order never matters.
- At most 200 entries.
- A check restricted to IPv4 or IPv6 with a manual address of the other family fails with a message that says so;
  it does not fall back to DNS.
- An entry whose sensor was removed applies nowhere and is shown as such.

Both sides compute "which address applies at this place" the same way and pass
`scripts/dev/host-overrides-contract/vectors.json`: `checker.ResolveHostOverrides` in the Go poller and
`app.services.host_overrides.resolve_for` on the server. Change both and the vectors together.

### Security

A check that carries a credential sends it to the address given here. So:

- Reading and changing entries needs `system.admin` (`require_admin_user`), like probe trust. Other accounts see
  only a note that an administrator manages the section.
- Every change is audit-logged (`monitoring.host_overrides.add`, `.update`, `.remove`) with the name, address and
  place, and the previous values on an update. A change whose audit entry cannot be stored is refused (503) and
  not saved.
- Rows that are not a name and an address are skipped by the poller, so a damaged row cannot redirect a check to
  a name of its own.
- A damaged stored document never stops monitoring: the poller leaves out a row (or the whole document) that is
  not what the API writes and loads the checks as usual.

### Where it is applied

- **Central appliance**: the Go poller loads the entries with the service checks
  (`poller/internal/store/postgres.go`), keeps the ones for `central` and applies them in the dial step
  (`poller/internal/checker/host_overrides.go`, `probe_tls.go`, `tls.go`, `tcp.go`, `icmp.go`). A change is picked
  up at the next check reload, normally within a minute. As with a change to trusted certificates, a check that
  is part-way through confirming a failure starts its retries again.
- **Remote sensors**: the controller sends each sensor only the entries that apply to it, already chosen
  (`host_overrides` in the sensor configuration, beside `probe_trust`). The entries are part of the configuration
  ETag for that sensor only, so a change reaches exactly the sensors it concerns.
- **Check tests** ("Test" on a check and the test in the check form) run on the appliance and use the entries
  that apply to it, so a test shows what the central poller will do. A check that runs on a sensor may use a
  different address than its test.

### Sensors too old to apply them

A sensor names what it can apply in every heartbeat (`capabilities`, stored in `sensors.capabilities`,
migrate-215). A sensor that does not name `host_overrides` ignores the entries and keeps resolving names itself.
A heartbeat is never refused over this list; names that are not lower-case words are dropped when it is stored.
The table says so on every row that reaches such a sensor ("Not applied by …"), and the add/edit dialog warns
when that sensor is chosen. The entry is stored and takes effect once the sensor is updated. Sensors that never
enrolled or are disabled run no checks and are not named in the warning.

### Results say when a manual address was used

A check that is Up through a manual address says nothing about DNS. Every result that used one records it in its
connection diagnostics (`override_host`, `override_address` in `network_diagnostics`), also when the check
failed. The service check page shows "Manual address" with the address under the latest probe, and a test result
states "Connected to the manual address … DNS was not used".

## Trusted certificates

HTTP and TLS checks accept a certificate only when it chains to a certificate authority the appliance trusts.
The trusted list (your own certificate authorities, or a self-signed service certificate with its host names)
and the automatic recovery of missing intermediate certificates are managed in **Security & TLS > Service probe
trust**. The Monitoring tab shows a one-line summary and links there; that card links back.

## A failed test points to the right place

When a check test fails, the result says in plain words what kind of problem it is and where to fix it:

| Diagnosis | Meaning | Points to |
| --- | --- | --- |
| `tls_trust` | The certificate's issuer is not trusted by the appliance, or it is self-signed | Trusted certificates (Security & TLS) |
| `tls_name` | The certificate was issued for a different name | Manual host addresses (is the address the right server?) |
| `dns` | The name could not be resolved | Manual host addresses (never for a DNS check, which does not use them) |
| `tls` | Any other TLS failure (expired, handshake) | Nothing to configure |

Accounts that cannot change these settings are told what to ask an administrator for, without a link.

## Not in this release

- A "connect to address" field on one check (several servers behind one name).
- Trusting a certificate with one click from a failed test.
- Trusted certificates and manual addresses for APM synthetic scenarios.
