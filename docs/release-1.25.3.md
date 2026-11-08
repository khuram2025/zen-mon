# ZenPlus 1.25.3

NCM no longer reports "Running configuration changed" for FortiGates whose
configuration did not change. FortiOS re-encrypts every stored secret (VPN
pre-shared keys, local user passwords, SNMP communities, encrypted private
keys) with a new random value each time its configuration is read, so every
scheduled backup looked different. Comparison now ignores those re-encrypted
values; real changes, added or removed secrets, administrator password changes
and certificates are still detected, and other platforms are unaffected. After
the upgrade a one-time background pass corrects baseline drift and closes the
open change alerts it can prove were false (both compared backups still
archived and identical); older ones are left for an operator to close.

Alert notifications now say plainly what happened, to which asset (type, name
and address), when, and what it means, on every channel: for example "Switch
SW09 (192.168.24.34) rebooted at 14:44 — uptime is now 3 min (was 41 days)"
and, when it clears, "has stayed up for 16 min since its 14:44 reboot; no
further reboots". Raw counters such as "currently 1" or "now 0" are gone.
Reboot and server restart alerts keep one incident per asset, report a
further reboot as "rebooted again", and close after the rule's **Auto-clear
after** time (default 15 min) or a manual resolve; the rule editor shows only
the settings that apply to them.

New **alert storm policy** (Escalation & On-call → Alert storms, off by
default): when more than a chosen number of assets report the same kind of
event (down, reboot or restart, service checks, interfaces, ...) within a
window, across all assets or per site, device group, tag or appliance, every
channel receives one combined notification instead of one per asset, followed
by updates and a closing summary of what recovered and what is still affected.

New **custom page-access roles** (Settings → Users & Access → Roles → Pages):
limit a role to selected pages and network maps, for helpdesk teams and NOC
wall screens. Menus, direct links and the API all follow the limit. Existing
roles and users are unchanged.

Migrations 184 (alert storms) and 185 (role page access). Appliance-only
release requiring the signed 1.24.36 bridge; existing Windows agent packages
are preserved.
