# Controller authority and VMware validation

The controller role now follows the single writable PostgreSQL member of a
commissioned cell. Migration 172 records a protected controller generation and
fence receipts. A root supervisor renews that generation, publishes a bounded
local admission grant and owns the lifecycle of application workers. Standalone
installations retain their existing behavior when the HA admission environment
variable is absent. This is a source and lab qualification change, not a finished
multisite appliance release.

## Ownership and fencing

The application role cannot write the authority tables. The infrastructure
supervisor checks the local Patroni primary endpoint, database incarnation,
node identity, TLS trust and commissioned dependencies. Its database and Patroni
addresses must identify the same local interface. Lease renewal requires a
durable database commit. The local admission allowance is six seconds from the
start of the authority operation; the database lease is twelve seconds.

Every managed service has a root admission check and a systemd dependency on the
supervisor. Losing the supervisor stops its dependents. An eight-second systemd
watchdog handles a frozen supervisor. API middleware refuses requests without a
current boot-bound grant. The supervisor stops timers, paths and services and
checks their cgroups before reactivation. Unknown ZenPlus units prevent startup.
Automatic OTA is refused until a cluster-aware upgrade procedure is commissioned.

A lease or health check cannot stop a paused VM. Patroni's pre-promotion hook
therefore requires a positive infrastructure power-off receipt for the previous
controller before database promotion. The supervisor also requires a bound fence
receipt before changing controller ownership. The old identity remains in the
authority row after release. If fencing fails, promotion fails closed.

The hook runs as postgres without privilege escalation; it does not weaken the
Patroni service's NoNewPrivileges setting. Its configuration and executable are
root controlled. Fencing requests bind cell, node, boot and request identities.
The external fence service must authenticate callers, constrain target inventory,
protect its administrative credentials and remain available outside the failed
node. The Workstation adapter in the test harness is lab infrastructure, not a
universal production fence agent.

## Commissioning requirements

Commission a new database cell, application database, full migration inventory,
application grants, shared application encryption and session keys, per-node
certificates and local dependencies. Do not point these files at an existing
standalone database. Use a private root configuration at
`/etc/zenplus-ha/controller.json`, a public root-controlled promotion configuration
at `/etc/zenplus-ha/promotion.json`, and the exact hook path validated by the
supervisor. Root authority credentials must not appear in the application
environment or a distributed image.

Install the reviewed `controller_units.py` drop-ins for every present consumer,
including timers and path-triggered work. Commission all unit dependencies before
clearing standalone dependencies. The API listens on loopback port 8000 because
the poller uses that local endpoint; an HTTPS frontend provides external access.
PostgreSQL connections from both Python and Go verify the commissioned CA and
server identity. Never copy a lab enrollment or private CA into a generic image.

For a single LAN, the optional `service_address` is an unused IPv4 host/prefix
on the same subnet as the node address. The supervisor requires one matching
interface and publishes that address only after the API reports its configured
database and dependencies healthy and the API/poller units are active. It removes
the address on readiness or authority loss and announces ownership with gratuitous
ARP. All frontends need certificates covering that address. This is a same-subnet
endpoint; it does not implement WAN address mobility or DNS failover. Use the
stable URL as `APP_BASE_URL` when commissioning application links.

## Measured lab results

Tests on 29 October 2026 used three disposable Ubuntu 24.04 VMs, PostgreSQL
16.15, Patroni 4.1.5 and etcd 3.6.16. The strict profile requires a synchronous
data replica. Application services use the replicated `ha_controller` fixture.
The production primary at 192.168.8.221 was not a fault target and its API process
remained running throughout these tests.

| Scenario | Observed outcome | Measured time |
|---|---|---|
| Active VM hard power loss | Replacement API accepted the existing login session; committed marker preserved; former VM fenced | 42.22 seconds |
| Frozen supervisor process | Watchdog stopped consumers; same node recovered with a new generation | 16.92 seconds |
| Suspended active VM | Saved execution state invalidated by the external fence; replacement activated; old VM rejoined as a replica | 40.05 seconds |
| Two consensus voters stopped | Every API and poller stopped; no writable PostgreSQL primary; recovered after majority returned | 23.94 seconds to stop |
| HTTP outage followed by active VM power loss | Same incident survived; replacement sent recovery; SMTP capture received exactly one trigger and one recovery in this run | 45.94 seconds to authenticated replacement API |
| Active VM power loss through the fixed TLS service address | The client reused the same address and login session; exactly one address holder after rejoin | 48.84 seconds |
| Power loss after incident and notification enqueue, before send | Replacement drained the committed job; isolated SMTP accepted one trigger | 46.59 seconds |

Recovery measurements include test polling and are observations, not an SLA or a
maximum bound. Three VMs on one workstation do not provide independent physical
failure domains. Email acceptance was observed at an isolated capture service
restricted to `lab@acceptance.invalid`; no customer recipient was used.

The first suspended-VM run correctly refused takeover: Workstation's `vmrun stop`
reported the VM off while its saved execution image remained resumable. The lab
adapter was corrected to acquire exclusive access to the allowlisted VM
configuration, quarantine the saved state and clear its checkpoint reference.
The corrected test passed. Production hypervisor fencing needs its own tested
power and saved-state semantics.

## Qualification boundaries

The current fixtures have separate local ClickHouse instances and no replicated
analytics store. The fixed HTTPS endpoint has passed an authenticated API power
loss test; full browser, WebSocket and sensor reconnection qualification remains.
Local dependency failure stops admission but does not yet automatically migrate
the database role. Device/service transitions and flap settlement now enqueue
within the incident transaction, alongside migrated metric producers. The
explicit post-enqueue power-loss test covers that crash boundary. Escalations,
digests, reports and other legacy producers still require separate qualification.

Independent critical-site cells, sender delegation, administrator-ordered remote
notification failover and optional local-safety delivery now have separate
[cross-site lab evidence](notification-sites.md). The same-cell tests above do
not establish those properties by themselves. Support/artifact storage, collector
traffic redirection, client endpoint failover, full role inventory, upgrade and
rollback qualification, clean-image sanitization and signing remain release
gates. Do not advertise these files as a completed customer HA appliance.

## Technical references

Patroni documents its [primary health endpoint](https://patroni.readthedocs.io/en/latest/rest_api.html),
[pre-promotion hook](https://patroni.readthedocs.io/en/latest/yaml_configuration.html)
and [watchdog limitations](https://patroni.readthedocs.io/en/latest/watchdog.html).
Systemd's [kill semantics](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)
explain why managed consumers use control-group termination. These references
describe component behavior; the measured results above are ZenPlus lab evidence.
