package pinger

import (
	"net"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/devicestatus"
)

// Device represents a monitored network device.
type Device struct {
	ID           uuid.UUID
	Hostname     string
	IPAddress    net.IP
	PingInterval time.Duration
	PingEnabled  bool
	// Status is the displayed status (State.Displayed(): the confirmed hard
	// status, or 'maintenance' while the overlay is shown).
	Status     string
	LastSeen   time.Time
	LastRTT    float64
	LastPingAt time.Time

	// State is the confirmed status machine (device_status_state). The
	// engine owns it; syncDevices carries it across reloads.
	State devicestatus.State
	// StateLoaded is set by the loader when State is the persisted row
	// (false: seeded from devices.status).
	StateLoaded bool
	// SeedEndReason is set by the loader when State was (re)seeded: the
	// end_reason ('reseed' or 'owner_change') for closing a flap episode
	// that is still open from before, on the first persist.
	SeedEndReason string
	// SeedOwner is set by the loader for an owner_change seed: the owner of
	// the state row the seed replaces, which the first state write may take
	// over (DeviceStatusSave.TakeoverOwner).
	SeedOwner string

	// persisted is the state handed to the status writer by the last write
	// that included the state row; nil forces the next check to write it.
	persisted *devicestatus.State
	// stateWrittenAt is the check time of that write (stateHeartbeatFor).
	stateWrittenAt time.Time
	// takeover is the foreign state row owner the engine's seeded State may
	// replace, until a state write succeeds (DeviceStatusSave.TakeoverOwner).
	takeover string
	// ownerLost is set when a save found the device no longer centrally
	// polled: it is neither pinged nor stepped until the next sync reloads
	// it from the database (its in-memory state is no longer the truth).
	ownerLost bool
	// epoch identifies the state lineage: it changes whenever State is
	// replaced (load, reload, foreign reseed), and a job stepped from an
	// older lineage is dropped instead of written.
	epoch uint64
}

// PingResult holds the result of a ping check for a device.
type PingResult struct {
	DeviceID   uuid.UUID
	IPAddress  net.IP
	IsUp       bool
	RTT        time.Duration
	PacketLoss float32
	Jitter     time.Duration
	MinRTT     time.Duration
	MaxRTT     time.Duration
	Sent       int
	Received   int
	Timestamp  time.Time
	PollerID   string
}

// StatusChange represents a device status transition.
type StatusChange struct {
	DeviceID  uuid.UUID
	OldStatus string
	NewStatus string
	Reason    string
	// Timestamp is the time of the check that confirmed the change.
	Timestamp time.Time
	// Flapping reports whether the change's class (reachability or
	// performance) is flapping after this check.
	Flapping bool
	// IsFlapping is the device-level flag after this check.
	IsFlapping bool
}

// DeviceStatusSave is one persisted check of the central status machine
// (SaveDeviceStatus): the devices row, and when WriteState is set the
// device_status_state row and the flap episode derived from the state.
type DeviceStatusSave struct {
	DeviceID uuid.UUID
	// At is the check timestamp: last_seen when Responded, the change time
	// for status_changed_at and the episode open/close time.
	At        time.Time
	Responded bool
	// RTTMs is the check's round-trip time (0 when it got no reply). An
	// unanswered check keeps devices.last_rtt_ms unless the device is
	// confirmed down (then 0).
	RTTMs float64
	// State is the machine state after the check. devices.status is
	// State.Displayed(); the flap columns mirror it.
	State devicestatus.State
	// WriteState upserts the state row and opens, extends or closes the open
	// flap episode. Without it only the devices row is refreshed.
	WriteState bool
	// FlapChanges is the number of recent confirmed changes (both classes)
	// recorded as start_changes when an episode opens.
	FlapChanges int
	// FlapWindowMinutes is the policy window recorded on an opened episode.
	FlapWindowMinutes int
	// EndReason is the end_reason when an open episode is closed.
	EndReason string
	// TakeoverOwner is the owner of another writer's state row that this
	// save may replace: the row the engine's seed was taken over from. A
	// state row of any other non-central owner makes the save Foreign.
	TakeoverOwner string
}

// DeviceStatusSaved reports what SaveDeviceStatus wrote.
type DeviceStatusSaved struct {
	// Device is false when the devices row was not updated because the device
	// is no longer centrally polled (or the save is Foreign): the check's
	// events must be dropped.
	Device bool
	// State is false when the state row was not written although requested
	// (schema-lag fallback): the next check sends the full state again.
	State bool
	// Foreign is set when the device is centrally polled but its state row
	// belongs to another writer that wrote after the engine's state was built
	// (a sensor during a short ownership move). Nothing was written; the
	// engine reseeds from Status/StatusChangedAt (devices.status as that
	// writer left it) and takes ForeignOwner's row over with its next write.
	Foreign         bool
	ForeignOwner    string
	Status          string
	StatusChangedAt *time.Time
}

// HealthStatus represents the health of the poller engine.
type HealthStatus struct {
	Status            string `json:"status"`
	PollerID          string `json:"poller_id"`
	DeviceCount       int    `json:"device_count"`
	ServiceCheckCount int    `json:"service_check_count"`
	SNMPDeviceCount   int    `json:"snmp_device_count"`
	ActivePings       int    `json:"active_pings"`
	ActiveSNMP        int    `json:"active_snmp"`
	Uptime            string `json:"uptime"`
	LastCycleMs       int64  `json:"last_cycle_ms"`
}
