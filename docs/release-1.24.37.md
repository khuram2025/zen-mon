# ZenPlus 1.24.37

Agent Fleet now searches the displayed host/server names and IP addresses, as
well as agent identity and version. Automatic ordering puts exact matches first,
then prefixes, then other matches. IP searches use numeric address order; names
and versions use natural ordering (server2 precedes server10). Search punctuation
such as percent and underscore is treated literally.

Every Agent Fleet data column supports ascending/descending sorting. An explicit
column sort overrides automatic match order and is kept in the URL across searches
and reloads; Reset sort restores automatic ordering. Server-side ordering precedes
pagination and ends with stable identity tie-breakers. Heartbeats no longer move
the default list on every refresh.

Selections follow record IDs when an operator sorts. While selecting, automatic
list refresh pauses and incoming updates retain the committed row sequence without
discarding new cell values. An intentional sort/filter/page change starts a new
sequence. Servers, Devices and Service Checks share this selection behavior.

Servers and Devices share exact/prefix search ranking and natural ordering, including
numeric IPv4/IPv6 order. Device column sorting is applied before the API result cap.
Inventory Assets adds Best match ordering and retains explicit column sorts.
Service Checks ranks search matches within its selected status/type/time priority,
preserving triage order. All existing visibility filters remain in force.

Validation includes disposable PostgreSQL list/pagination tests over 205 agents and
205 devices, React DOM tests for column sorting, selection, in-flight reorder, URL
restoration and numeric IP/name comparators, plus existing inventory regressions.
This is a signed appliance-only release; existing Windows agent installers and
installed endpoint agents are preserved.
