# ZenPlus 1.23.28 — NCCM backups without manual first-use trust setup

Includes the optional per-profile first-use trust feature described in
`release-1.23.27.md`. The signed 1.23.27 draft was superseded before publication
to incorporate the requested removal of the manual SSH trust setup step.

When **Automatically trust new devices** is enabled on a profile, normal backup
readiness omits the SSH host trust setup row. New addresses and ports learn and
audit their first key automatically, then use strict checking during login.
Changed keys never get replaced automatically. Actual trust problems, including
retired trust or malformed trust files, remain visible and stop collection.

Migration 152 adds the profile option with a false default. The rollout on
192.168.8.221 enables the option for its existing NCCM profiles as requested.
Other appliances retain their configured policy; administrators can opt in
through Connection profiles. The first connection is not independently verified.

This is an appliance-only cumulative release with minimum prerequisite 1.23.9.
Existing agent packages and previously saved verified keys are retained. Normal
updater backups provide the recovery path. Release gates remain the strict
dashboard build, route checks, focused SSH/database fixtures, all five CI checks,
signature verification, canary upgrade and live NCCM/UDT verification.
