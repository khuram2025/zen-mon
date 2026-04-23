package snmp

import (
	"net"
	"sync"
	"time"

	"github.com/google/uuid"
)

// Device is the SNMP-relevant subset of the devices table.
// Passphrases are stored as plaintext here ONLY in memory, after
// crypto.Decrypt. They never hit disk in the poller process.
type Device struct {
	ID        uuid.UUID
	Hostname  string
	IPAddress net.IP

	Enabled        bool
	Version        string // "1" | "2c" | "3"
	Port           int
	Community      string
	V3Username     string
	V3Context      string
	AuthProtocol   string // MD5 | SHA | SHA224 | SHA256 | SHA384 | SHA512
	AuthPassphrase string
	PrivProtocol   string // DES | AES | AES128 | AES192 | AES256
	PrivPassphrase string

	TimeoutMs      int
	Retries        int
	MaxRepetitions int
	PollInterval   time.Duration

	ProfileID *uuid.UUID

	// Discovery cache (optional, for classification in Phase 2).
	SysObjectID string
	Vendor      string
	Model       string
	OSVersion   string
}

// SystemInfo is the result of CollectSystem.
type SystemInfo struct {
	SysDescr    string
	SysObjectID string
	SysUpTime   time.Duration
	SysContact  string
	SysName     string
	SysLocation string
}

// Interface is one row from IF-MIB / IF-MIB-extensions.
type Interface struct {
	IfIndex       int
	IfName        string
	IfDescr       string
	IfAlias       string
	IfType        int
	IfSpeed       uint64
	MACAddress    string
	AdminStatus   string
	OperStatus    string
	InOctets      uint64
	OutOctets     uint64
	InUcastPkts   uint64
	OutUcastPkts  uint64
	InErrors      uint64
	OutErrors     uint64
	InDiscards    uint64
	OutDiscards   uint64
	HasHC         bool // true if 64-bit HC counters were available
}

// Entity is one row from ENTITY-MIB::entPhysicalTable.
type Entity struct {
	EntIndex     int
	ParentIndex  int
	Class        string
	Name         string
	SerialNumber string
	ModelName    string
	HWRevision   string
	FWRevision   string
}

// Sensor is one row from ENTITY-SENSOR-MIB::entPhySensorTable.
type Sensor struct {
	SensorIndex int
	SensorType  string // celsius | fan | voltage | amperage | watts | percent | ...
	Unit        string
	Value       float64
	Description string
}

// MetricSample is a single scalar value bound for ClickHouse snmp_metrics.
type MetricSample struct {
	DeviceID  uuid.UUID
	Key       string // normalized metric key e.g. "cpu", "memory_used_pct", "temperature"
	Value     float64
	Unit      string
	Timestamp time.Time
	PollerID  string
}

// InterfaceSample is a single interface-counter snapshot bound for
// ClickHouse snmp_if_metrics. InBps/OutBps are computed from counter
// deltas by the caller (see Collector.diffInterfaces).
type InterfaceSample struct {
	DeviceID     uuid.UUID
	IfIndex      uint32
	InOctets     uint64
	OutOctets    uint64
	InUcastPkts  uint64
	OutUcastPkts uint64
	InErrors     uint64
	OutErrors    uint64
	InDiscards   uint64
	OutDiscards  uint64
	OperStatus   uint8
	InBps        float64
	OutBps       float64
	Timestamp    time.Time
	PollerID     string
}

// Result bundles one device's full SNMP poll output. The collector
// writes into this struct progressively so that partial results are
// preserved if the outer cycle times out on a slow device — basic
// identity (System, SysObjectID, vendor, model) can still be persisted
// even when the full interface walk never finishes.
type Result struct {
	Mu         sync.Mutex
	DeviceID   uuid.UUID
	Timestamp  time.Time
	Duration   time.Duration
	Err        error
	System     *SystemInfo
	Interfaces []Interface
	Entities   []Entity
	Sensors    []Sensor
	Scalars    []MetricSample // CPU, memory, temperature, etc.
	IfSamples  []InterfaceSample
}
