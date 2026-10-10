# 1.24.5 — Appliance storage experience

Storage now presents capacity, data lifecycle, automatic protection and disk
operations using product-level categories. Internal database names, file paths,
table inventories and raw errors are replaced with actionable customer messages.
Retained appliance logs show grouped expiry dates; backups and cleanup describe
what data they affect. Disk addition, expansion and removal preserve identity
checks and explicit confirmation where existing contents could be erased.

Low-space reserves adapt to writable capacity and are recalculated after disk
expansion. Cleanup begins before full capacity and keeps the existing retention,
protected-record and bounded-work safeguards. Unneeded topology polling stops
when viewing other storage sections.

Validation: strict dashboard build, route and rendered storage tests; simulated
small/large/shared/separate filesystems, delayed reclamation and expansion; full
Python and protected CI checks. Release acceptance uses the signed package on the
owner-designated development appliance before promotion and production update.
Actual disk erasure/removal and deliberately filling production disks are not
part of release acceptance.

Legacy live smoke tests now require an explicit disposable target through
`ZENPLUS_API`; collecting the default suite does not contact the installed app.
