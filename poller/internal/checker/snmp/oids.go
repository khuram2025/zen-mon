package snmp

// Standard MIB OID constants. These cover ~90% of real-world gear.
// Vendor-specific OIDs (CISCO-*, FORTINET-*, PAN-*) land in Phase 2
// via JSON profile packs.
const (
	// system group (RFC 1213)
	OIDSysDescr    = "1.3.6.1.2.1.1.1.0"
	OIDSysObjectID = "1.3.6.1.2.1.1.2.0"
	OIDSysUpTime   = "1.3.6.1.2.1.1.3.0"
	OIDSysContact  = "1.3.6.1.2.1.1.4.0"
	OIDSysName     = "1.3.6.1.2.1.1.5.0"
	OIDSysLocation = "1.3.6.1.2.1.1.6.0"

	// IF-MIB (RFC 2863) — interface table + extensions
	OIDIfTable       = "1.3.6.1.2.1.2.2.1"
	OIDIfIndex       = "1.3.6.1.2.1.2.2.1.1"
	OIDIfDescr       = "1.3.6.1.2.1.2.2.1.2"
	OIDIfType        = "1.3.6.1.2.1.2.2.1.3"
	OIDIfSpeed       = "1.3.6.1.2.1.2.2.1.5"
	OIDIfPhysAddress = "1.3.6.1.2.1.2.2.1.6"
	OIDIfAdminStatus = "1.3.6.1.2.1.2.2.1.7"
	OIDIfOperStatus  = "1.3.6.1.2.1.2.2.1.8"
	OIDIfInOctets    = "1.3.6.1.2.1.2.2.1.10"
	OIDIfInUcastPkts = "1.3.6.1.2.1.2.2.1.11"
	OIDIfInDiscards  = "1.3.6.1.2.1.2.2.1.13"
	OIDIfInErrors    = "1.3.6.1.2.1.2.2.1.14"
	OIDIfOutOctets   = "1.3.6.1.2.1.2.2.1.16"
	OIDIfOutUcastPkts = "1.3.6.1.2.1.2.2.1.17"
	OIDIfOutDiscards = "1.3.6.1.2.1.2.2.1.19"
	OIDIfOutErrors   = "1.3.6.1.2.1.2.2.1.20"

	// ifXTable (RFC 2863)
	OIDIfName           = "1.3.6.1.2.1.31.1.1.1.1"
	OIDIfAlias          = "1.3.6.1.2.1.31.1.1.1.18"
	OIDIfHCInOctets     = "1.3.6.1.2.1.31.1.1.1.6"
	OIDIfHCInUcastPkts  = "1.3.6.1.2.1.31.1.1.1.7"
	OIDIfHCOutOctets    = "1.3.6.1.2.1.31.1.1.1.10"
	OIDIfHCOutUcastPkts = "1.3.6.1.2.1.31.1.1.1.11"
	OIDIfHighSpeed      = "1.3.6.1.2.1.31.1.1.1.15"

	// HOST-RESOURCES-MIB (RFC 2790) — CPU, memory, storage
	OIDHrProcessorLoad        = "1.3.6.1.2.1.25.3.3.1.2"
	OIDHrStorageTable         = "1.3.6.1.2.1.25.2.3.1"
	OIDHrStorageType          = "1.3.6.1.2.1.25.2.3.1.2"
	OIDHrStorageDescr         = "1.3.6.1.2.1.25.2.3.1.3"
	OIDHrStorageAllocationU   = "1.3.6.1.2.1.25.2.3.1.4"
	OIDHrStorageSize          = "1.3.6.1.2.1.25.2.3.1.5"
	OIDHrStorageUsed          = "1.3.6.1.2.1.25.2.3.1.6"
	// Storage type OIDs
	OIDHrStorageRAM = "1.3.6.1.2.1.25.2.1.2"

	// ENTITY-MIB (RFC 4133)
	OIDEntPhysicalEntry      = "1.3.6.1.2.1.47.1.1.1.1"
	OIDEntPhysicalDescr      = "1.3.6.1.2.1.47.1.1.1.1.2"
	OIDEntPhysicalContained  = "1.3.6.1.2.1.47.1.1.1.1.4"
	OIDEntPhysicalClass      = "1.3.6.1.2.1.47.1.1.1.1.5"
	OIDEntPhysicalName       = "1.3.6.1.2.1.47.1.1.1.1.7"
	OIDEntPhysicalHWRev      = "1.3.6.1.2.1.47.1.1.1.1.8"
	OIDEntPhysicalFWRev      = "1.3.6.1.2.1.47.1.1.1.1.9"
	OIDEntPhysicalSWRev      = "1.3.6.1.2.1.47.1.1.1.1.10"
	OIDEntPhysicalSerialNum  = "1.3.6.1.2.1.47.1.1.1.1.11"
	OIDEntPhysicalMfgName    = "1.3.6.1.2.1.47.1.1.1.1.12"
	OIDEntPhysicalModelName  = "1.3.6.1.2.1.47.1.1.1.1.13"

	// ENTITY-SENSOR-MIB (RFC 3433)
	OIDEntPhySensorType        = "1.3.6.1.2.1.99.1.1.1.1"
	OIDEntPhySensorScale       = "1.3.6.1.2.1.99.1.1.1.2"
	OIDEntPhySensorPrecision   = "1.3.6.1.2.1.99.1.1.1.3"
	OIDEntPhySensorValue       = "1.3.6.1.2.1.99.1.1.1.4"
	OIDEntPhySensorOperStatus  = "1.3.6.1.2.1.99.1.1.1.5"
	OIDEntPhySensorUnitsDisplay = "1.3.6.1.2.1.99.1.1.1.6"
)

// entPhysicalClass values (ENTITY-MIB)
var EntPhysicalClassNames = map[int]string{
	1:  "other",
	2:  "unknown",
	3:  "chassis",
	4:  "backplane",
	5:  "container",
	6:  "powerSupply",
	7:  "fan",
	8:  "sensor",
	9:  "module",
	10: "port",
	11: "stack",
	12: "cpu",
}

// entPhySensorType values (ENTITY-SENSOR-MIB)
var EntPhySensorTypeNames = map[int]string{
	1:  "other",
	2:  "unknown",
	3:  "voltsAC",
	4:  "voltsDC",
	5:  "amperes",
	6:  "watts",
	7:  "hertz",
	8:  "celsius",
	9:  "percentRH",
	10: "rpm",
	11: "cmm",
	12: "truthvalue",
}

// Interface admin/oper status codes (IF-MIB)
var IfStatusNames = map[int]string{
	1: "up",
	2: "down",
	3: "testing",
	4: "unknown",
	5: "dormant",
	6: "notPresent",
	7: "lowerLayerDown",
}
