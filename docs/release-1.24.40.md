# ZenPlus 1.24.40

Baseline policy detail pages keep search controls, server rows and fixed column
positions visible while a new filtered or sorted report loads. Debounced search
retains keyboard focus. Page clamping and expanded-server cleanup wait for the
current response, avoiding changes based on the previous report. Previous data
is never carried across different policy identities; failed reads still show an
explicit error.

Includes the dedicated baseline detail page with scoped server compliance,
lazy per-rule evidence, expected/found software versions, severity and finding
times. Policy names and server compliance links open the same detail view.
Server filters, sorting and pagination remain in the URL. Incomplete or stale
results cannot indicate full compliance; read-only roles have no mutation
controls.

Validation includes a delayed-response regression that first reproduces loss
of the search control, then checks focus retention and a sort change while the
search request is pending. This is a signed appliance-only update requiring
1.24.36 and preserving installed Windows agent packages. No database migration,
baseline evaluation or alert threshold changes.
