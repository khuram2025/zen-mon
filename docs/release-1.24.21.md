# ZenPlus 1.24.21 — License capacity

Licenses are now applied to what an appliance monitors (see `docs/license-capacity.md`).

- **One node pool for devices and servers.** Each device and each server is one license node. A server linked to its device is the same machine and counts once. Controller-managed access points/switches and service checks are not counted.
- **Capacity is enforced.** At the limit, Add Device, bulk import, discovery imports, Register/Add Server, new agent enrollments, agent approval and accepting a cloned agent are refused with "You have reached your license capacity … Extend your license to add more devices or servers." Bulk imports add what still fits and report the rest. An expired or inactive license blocks additions. An unregistered appliance has an evaluation allowance of 50 nodes.
- **Nothing existing is switched off.** Devices and servers already monitored keep running, can be edited, and deleting one frees its node immediately.
- **Settings > General > Licenses** shows a Node capacity card: used/limit, devices, servers, linked servers, controller-managed children and the shared subscription pool. Devices, Servers, Agent Fleet and the add/import dialogs warn at 90% and disable adding when full.
- **Usage reported to zentryc.com** is now devices + servers (linked pairs once), the same count the appliance enforces. It previously counted devices + service checks.

No schema change.
