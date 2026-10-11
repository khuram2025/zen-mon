# ZenPlus 1.24.13

Clicking an endpoint count in Switch Ports now opens only the active observations
for that switch and port. The endpoint list shows the selected location, preserves
it while searching or paging, and provides a clear action to remove the filter.
Filtered rows display the selected port even if another switch also observed the MAC.
The switch selector scrolls independently so selecting a switch keeps its port table visible.

No data migration or history deletion is included. Controller-managed ports remain read-only.
Validate direct and managed switch filters, same-number ports on different switches,
stale observations, search combinations and displayed location consistency with
disposable PostgreSQL fixtures, strict dashboard build and live development acceptance.
