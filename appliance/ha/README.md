# Operational database HA profile

This directory introduces a fresh PostgreSQL and etcd cell. It does not enable
ZenPlus controller HA, autonomous sites, remote notification senders, or a
customer appliance release. Do not adopt an installed database with these files.

## Profiles

| Profile | Data members | Consensus voters | Behavior without a synchronous replica |
|---|---|---|---|
| Strict | 3 | 3 | New commits wait for a synchronous replica |
| Economy | 2 plus a vote-only witness | 3 | Patroni can remove the synchronous requirement; new writes can have only one copy |

Both profiles use one active PostgreSQL primary. A witness holds consensus data,
not PostgreSQL WAL. Three VMs on one workstation demonstrate process and VM
recovery; they do not create three independent physical failure domains.

`database_profile.py` validates three unique voters, profile membership and
separate credentials. It emits JSON accepted as YAML by Patroni and etcd, into a
new owner-only directory. Existing output is never overwritten. Use Linux for
the command-line renderer; credentials are loaded from an owner-only JSON file,
not command arguments. Example inventory with documentation-only addresses:

```json
{"cluster":"site-a","profile":"strict","nodes":[
  {"name":"a1","address":"192.0.2.11","data":true},
  {"name":"a2","address":"192.0.2.12","data":true},
  {"name":"a3","address":"192.0.2.13","data":true}
]}
```

```sh
python3 appliance/ha/database_profile.py --inventory /secure/inventory.json \
  --node a1 --credentials /secure/database-credentials.json --output /secure/new-a1
```

The credential file has `superuser`, `replication` and `rewind` values, each
distinct and at least 32 characters. Generated `patroni.json` is private. Keep it,
node private keys, CA private keys, database state and enrolled VM disks outside
Git, support bundles and generic appliance images.

## Runtime contract

The initial qualified lab combination is PostgreSQL 16.15, Patroni 4.1.5 and etcd
3.6.16 on Ubuntu 24.04. These are explicit tested versions, not floating latest
dependencies or a completed release dependency manifest.

Consensus client and peer connections require TLS client certificates. Patroni
REST also requires a certificate. PostgreSQL uses SCRAM over TLS with full server
verification for replication and administrative connections. The rendered HBA
allows only the listed data members and explicit database roles. Application
roles and external management clients require a separately reviewed HBA policy;
the renderer does not open an unrestricted application network.

Supply per-node SAN certificates for the configured IPs at
`/etc/zenplus-ha/tls/{etcd,patroni,postgres}.{crt,key}` and the dedicated cell CA at
`ca.crt`. Use separate etcd and postgres service accounts, owner-only private
keys and configuration files, and a root-owned public certificate directory.
Install etcd at `/usr/local/bin/etcd`, Patroni in `/opt/zenplus-ha/venv`, and
PostgreSQL 16 binaries under `/usr/lib/postgresql/16/bin`. The data directories
are `/var/lib/zenplus-ha/etcd` and `/var/lib/zenplus-ha/postgresql`. Provision fresh
empty directories with the matching service owners. Do not run the distribution
PostgreSQL service concurrently with Patroni.

The clock unit reuses `appliance/vmware/clock.py`, installed at
`/usr/local/lib/zenplus-ha/clock.py`. Its platform marker retains the existing
clock helper contract. Install and enable the unit before database services.
Persist network addressing by adapter MAC, not a transient interface name.

Default watchdog mode is **required**, with a timeout of half the 30-second DCS
TTL, a five-second HA loop and five-second retry timeout. Provide a commissioned
watchdog device accessible only to postgres. The lab explicitly loads Linux
softdog through a lab-only service because Ubuntu deny-lists automatic loading.
That lab unit is not a production fencing implementation. The explicit
`--lab-only-no-watchdog` option marks generated metadata as unqualified and must
never be used for a customer automatic-failover profile.

Patroni bootstrap DCS values are initialization settings. Editing the local file
does not update an existing cluster policy. Changes require authenticated Patroni
operations, audit and a fresh acceptance run. Do not silently change economy to
strict or lower durability to recover write availability.

## Release boundaries

A Patroni primary response is not a universal application fence. Hypervisor
suspension can suspend a guest watchdog. External infrastructure fencing and old
controller quarantine are still required for that failure model. Never authorize
a replacement controller solely because the other endpoint is unreachable.

Before enabling controller HA, implement and test the active-role manifest for
API background loops, poller, NetPath, passive collectors, NCM workers and timers,
support jobs and maintenance. Replace the standalone `zenplus-wait-deps` unit's
dependency on `postgresql.service`; reconcile application DB endpoints, Redis,
analytical stores, artifacts, sessions and signing trust. A database replica does
not carry those files and services. Updater orchestration must preserve this role
boundary, and target-changing commands need verified executor fencing.

Critical sites still need independent local incident evaluation, authentication,
rule state, provider access and isolation reconciliation. Preferred notification
sites still need origin-owned grants, remote durable inboxes and receipts,
admin policy APIs/UI, local safety delivery and explicit uncertain outcomes.
None of those capabilities are enabled by these database templates.

Do not sign or advertise a full HA appliance based on this layer's test results.
The signed release path additionally requires merged source, five CI checks,
clean tagged builds, exact-artifact reference acceptance, secret-free sealing,
signature verification and the full commissioned capability matrix.

## References

* [Patroni replication modes](https://patroni.readthedocs.io/en/latest/replication_modes.html)
* [Patroni watchdog limitations](https://patroni.readthedocs.io/en/latest/watchdog.html)
* [Patroni configuration](https://patroni.readthedocs.io/en/latest/yaml_configuration.html)
* [etcd transport security](https://etcd.io/docs/v3.6/op-guide/security/)

The guardrail tests are `server/tests/test_ha_database_profile.py`. VMware fault
evidence is retained separately in the task's implementation report. A passing
configuration test is not evidence of runtime failover.
