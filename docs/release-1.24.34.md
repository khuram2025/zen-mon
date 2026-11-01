# ZenPlus 1.24.34

Restart and shutdown alerts now use plain sentences and readable dates in the
appliance's configured timezone. Messages distinguish a restart request, a new
boot, an unexpected shutdown, and a shutdown that has begun. A shutdown event
does not claim that the server is already powered off.

When a newer lifecycle signal belongs to an existing incident, its message
updates to describe the latest evidence. The incident keeps its original
identity, acknowledgement and notification state, preventing duplicate pages.

This appliance-only release preserves the installed Windows agent packages.
