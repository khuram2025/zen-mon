# ZenPlus 1.24.39

Baseline policy names and result actions now open a dedicated detail page.
The policy page shows visible servers grouped as compliant, violated or needing
evaluation. Each server expands into rule evidence: required/prohibited software,
minimum version, found package/version, severity, notes, evaluation time and the
first failure time. Server detail compliance links lead to the same policy page.

Search, sortable server columns, bounded server pagination and URL state support
large fleets. Rule evidence loads only when opened. Automatic list refresh pauses
while evidence is open; explicit refresh accepts new values while holding rows.
The Rules view shows policy definitions and server counts per rule.

All read paths constrain results to current OS/site/tag scope and the user's
server visibility. Results predating policy edits are not current evidence, and
partially checked servers cannot appear fully compliant. Failed reads display an
error; disabled and empty policies do not imply compliance. The page distinguishes
inventory receipt from evaluation and explains that re-evaluation uses stored
inventory. Read-only roles can inspect results without mutation controls.

Signed appliance-only release requiring 1.24.36. Existing Windows agent installers
are preserved. No database migration or evaluation/alert threshold change.
