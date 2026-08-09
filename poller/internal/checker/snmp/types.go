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

	// Per-device UDT settings, joined from udt_device_settings at load
	// time. A device with no settings row gets UdtEnabled=true and
	// UdtInterval=0 (0 = follow the engine's global cadence).
	UdtEnabled  bool
	UdtInterval time.Duration
	// UdtCredential, when non-nil, replaces the device's own SNMP
	// settings for UDT walks only — e.g. a v3 user with per-VLAN
	// context access that the regular monitoring credential lacks.
	UdtCredential *UdtCredential

	// OidGroups is the device's monitoring-template content, joined
	// from device_profiles.oid_groups at load time. Empty when the
	// device has no template (or the template declares no groups).
	OidGroups []OidGroup

	// Discovery cache (optional, for classification in Phase 2).
	SysObjectID string
	Vendor      string
	Model       string
	OSVersion   string
}

// UdtCredential is a reusable snmp_credentials row selected for a
// device's UDT collection. Zero-valued fields fall back to the same
// defaults NewSession applies for a Device.
type UdtCredential struct {
	Version        string
	Port           int
	Community      string
	V3Username     string
	V3Context      string
	AuthProtocol   string
	AuthPassphrase string
	PrivProtocol   string
	PrivPassphrase string
	TimeoutMs      int
	Retries        int
}

// ApplyTo overwrites d's session-relevant fields with the credential's.
func (c *UdtCredential) ApplyTo(d *Device) {
	d.Version = c.Version
	if c.Port > 0 {
		d.Port = c.Port
	}
	d.Community = c.Community
	d.V3Username = c.V3Username
	d.V3Context = c.V3Context
	d.AuthProtocol = c.AuthProtocol
	d.AuthPassphrase = c.AuthPassphrase
	d.PrivProtocol = c.PrivProtocol
	d.PrivPassphrase = c.PrivPassphrase
	if c.TimeoutMs > 0 {
		d.TimeoutMs = c.TimeoutMs
	}
	if c.Retries > 0 {
		d.Retries = c.Retries
	}
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

// FdbEntry is one learned MAC from a bridge forwarding database
// (BRIDGE-MIB dot1dTpFdbTable or Q-BRIDGE-MIB dot1qTpFdbTable).
type FdbEntry struct {
	VlanID  int    // 0 when the device gave no VLAN context
	MAC     string // aa:bb:cc:dd:ee:ff
	IfIndex int    // resolved via dot1dBasePortIfIndex
	Status  int    // 1 other, 3 learned (2 invalid / 4 self / 5 mgmt are filtered)
}

// ArpEntry is one IP->MAC binding from ipNetToMediaTable (IPv4) or
// ipNetToPhysicalTable (IPv4+IPv6 ND).
type ArpEntry struct {
	IfIndex int
	IP      string
	MAC     string
	IsIPv6  bool
	Source  string // "arp" | "nd"
}

// LldpNeighbor is one discovered L2 neighbor (LLDP or CDP).
type LldpNeighbor struct {
	LocalIfIndex     int
	Protocol         string // "lldp" | "cdp"
	ChassisIDSubtype int    // LLDP: 4 = macAddress
	ChassisID        string // MAC string when subtype 4, else raw
	PortID           string
	PortDesc         string
	SysName          string
	SysDescr         string
}

// VlanInfo is one VLAN known to the device.
type VlanInfo struct {
	ID   int
	Name string
}

// UdtData bundles one device's user-device-tracker poll output:
// forwarding databases, ARP/ND caches, neighbors and VLAN inventory.
type UdtData struct {
	Fdb        []FdbEntry
	Arp        []ArpEntry
	Neighbors  []LldpNeighbor
	Vlans      []VlanInfo
	Pvids      map[int]int  // ifIndex -> untagged VLAN
	TrunkPorts map[int]bool // ifIndex set (Cisco VTP trunking status)
	BridgeMAC  string
	OwnMACs    map[string]bool // the device's own interface MACs

	// FdbNote explains an empty or partial forwarding database. A switch
	// that returns no MACs is almost always a configuration problem on the
	// device (Cisco per-VLAN SNMP contexts in particular), not an outage,
	// and without this the poller reports "0 fdb" with no reason.
	FdbNote string
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
	WantUdt    bool // set by the engine before Collect when UDT is due
	Err        error
	System     *SystemInfo
	Interfaces []Interface
	Entities   []Entity
	Sensors    []Sensor
	Scalars    []MetricSample // CPU, memory, temperature, etc.
	IfSamples  []InterfaceSample
	Udt        *UdtData // populated only on UDT-due cycles

	// Template-driven collection output (device_profiles.oid_groups).
	TplValues []TemplateValue // latest snapshot rows for Postgres
	TplGroups []string        // group keys polled to completion (stale-row purge)
}
