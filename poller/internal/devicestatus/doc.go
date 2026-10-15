// Package devicestatus is the confirmed device status machine shared by the
// central poller and (in Python) the remote-sensor writer.
//
// A device's visible status is a HARD status (up, degraded, down, unknown)
// that only changes once the evidence is confirmed: DOWN after
// down_confirm_checks consecutive failed checks, DEGRADED when
// degraded_confirm_checks of the last degraded_window_checks checks were
// slow, lossy or unanswered, and back to UP from DEGRADED only after
// degraded_clear_checks checks inside the (lower) recovery limits or a full
// window without bad evidence. Maintenance is an overlay on top of the hard
// status. Flapping is a flag, not a status: it is raised when a class of
// confirmed changes (reachability or performance) repeats within a time
// window and lowered when the changes stop.
//
// Everything here is pure: Step takes the persisted State, one Check and the
// effective Policy and returns the next State plus the events to deliver. It
// performs no I/O and reads no clock other than the check timestamp, so the
// Go and Python implementations can be held to the same shared vectors
// (scripts/dev/device-status-contract/vectors.json).
package devicestatus
