# ZenPlus 1.24.41

Baseline detail search, sorting, status filters, pagination and expansion now
compose changes against the most recently chosen URL. Rapid consecutive
controls cannot overwrite a pending sort or search with an older URL snapshot.
Browser navigation still restores the committed report state.

Includes the dedicated baseline policy detail view, grouped server compliance,
lazy per-rule evidence, scoped counts, expected/found versions, severity and
finding times. Refresh retains controls and keyboard focus; server columns use
fixed proportions. Partial or stale policy checks never imply full compliance.

Validation includes delayed-response focus retention and a rapid sort/page-size
regression that fails on the prior implementation and passes after the change.
Signed appliance-only release requiring the 1.24.36 catalog bridge. Installed
Windows agent packages are preserved. No schema, evaluator or alert threshold
changes.
