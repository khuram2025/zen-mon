# Storage management (Settings → Storage)

## Layout model

The **data volume** is whatever LVM logical volume backs `/data`. Appliances
built with a dedicated data LV use it; single-disk appliances (for example
`.221`) keep `/data` on the root LV `ubuntu-vg/ubuntu-lv`. Nothing assumes LV
names: the API and the root helper resolve the LV with `findmnt --target /data`.
The **storage pool** is the volume group holding that LV.

## Privilege boundary

All disk and volume changes go through the root-owned
`/usr/local/sbin/zenplus-volume-helper` (source `scripts/zenplus-volume-helper`),
installed and granted by `scripts/setup-storage.sh` (runs on every OTA). The
helper re-validates every request against the live system:

| Verb | Effect | Refuses when |
|---|---|---|
| `inventory` | lsblk, LVM reports, partition geometry, `/`, `/data` mounts | — |
| `rescan` | SCSI host scan + per-disk size rescan | — |
| `add-disk DISK [--wipe]` | `pvcreate` + `vgextend` into the data VG | system disk, mounted/held, in another VG, existing data without `--wipe` |
| `extend MOUNT all\|BYTES` | `lvextend` (whole extents) + `resize2fs`/`xfs_growfs` online | mount not `/` or `/data`, not enough free extents |
| `grow-pv PV` | `growpart` (partition PVs) + `pvresize` | PV outside the data VG |
| `evacuate PV` | background `pvmove` | other PVs lack free space for its extents |
| `abort-evacuation` | `pvmove --abort` | no move running |
| `remove-disk PV` | `vgreduce` + `pvremove` | PV still holds extents, last PV |
| `remove-missing` | `vgreduce --removemissing` (never `--force`) | no missing PVs |

Mutating verbs take an exclusive lock and refuse while a `pvmove` runs. Exit
status: `0` done, `2` rejected (nothing changed), `1` failed.
`bin/expand-storage.sh` is the operator CLI over the same helper.

## Operator procedures

- **Add a disk:** attach it in the hypervisor → *Rescan disks* → *Add to pool*,
  choosing all, part, or none of it for the data volume. Disks with partitions
  or filesystems require typing the device path to confirm erasing them.
- **Enlarge a disk:** grow it in the hypervisor (remove VMware snapshots first)
  → *Rescan disks* → *Claim space* (optionally allocating it immediately).
- **Remove a disk:** *Remove* moves its data to other pool disks online
  (requires that much unallocated space elsewhere), then a second *Remove*
  takes it out of the pool. Detach it only once it shows *Unused*. Keeping
  added capacity unallocated is what makes later removal possible: ext4 cannot
  shrink online.
- **Missing disk:** shown as critical when it held data (reattach and rescan);
  an empty missing disk can be forgotten.

## Auto-purge on shared volumes

The emergency purge deletes metrics only when doing so can bring usage back
under the target, or when usage reaches 95%. On a volume shared with the OS,
other files usually dominate; deleting monitoring history there would not free
the disk. Skips are recorded as `auto_purge_skipped` at most every 6 hours.
