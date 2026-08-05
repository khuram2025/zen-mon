package snmp

// Standard MIB OID constants + vendor-specific CPU/memory OIDs.
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

	// ── Cisco MIBs ──
	// CISCO-PROCESS-MIB — CPU (cpmCPUTotal5minRev, preferred over OLD-CISCO)
	OIDCiscoCPU5min       = "1.3.6.1.4.1.9.9.109.1.1.1.1.8.1"  // cpmCPUTotal5minRev
	OIDCiscoCPU1min       = "1.3.6.1.4.1.9.9.109.1.1.1.1.7.1"  // cpmCPUTotal1minRev
	OIDCiscoCPU5sec       = "1.3.6.1.4.1.9.9.109.1.1.1.1.6.1"  // cpmCPUTotal5secRev
	// OLD-CISCO-CPU-MIB fallback (IOS 11.x)
	OIDCiscoCPUBusy       = "1.3.6.1.4.1.9.2.1.58.0"           // avgBusy5
	// CISCO-MEMORY-POOL-MIB — Memory
	OIDCiscoMemPoolUsed   = "1.3.6.1.4.1.9.9.48.1.1.1.5"       // ciscoMemoryPoolUsed (walk)
	OIDCiscoMemPoolFree   = "1.3.6.1.4.1.9.9.48.1.1.1.6"       // ciscoMemoryPoolFree (walk)
	// CISCO-ENHANCED-MEMPOOL-MIB (Nexus/newer IOS-XE)
	OIDCiscoEnhMemUsed    = "1.3.6.1.4.1.9.9.221.1.1.1.1.18"   // cempMemPoolHCUsed
	OIDCiscoEnhMemFree    = "1.3.6.1.4.1.9.9.221.1.1.1.1.20"   // cempMemPoolHCFree

	// ── Fortinet MIBs ──
	// FORTINET-FORTIGATE-MIB
	OIDFortiCPU           = "1.3.6.1.4.1.12356.101.4.1.3.0"    // fgSysCpuUsage (%)
	OIDFortiMem           = "1.3.6.1.4.1.12356.101.4.1.4.0"    // fgSysMemUsage (%)
	OIDFortiSesCount      = "1.3.6.1.4.1.12356.101.4.1.8.0"    // fgSysSesCount
	OIDFortiSesRate       = "1.3.6.1.4.1.12356.101.4.1.11.0"   // fgSysSesRate6

	// ── Palo Alto MIBs ──
	// PAN-COMMON-MIB
	OIDPanCPU             = "1.3.6.1.4.1.25461.2.1.2.3.1.0"    // panSessionUtilization (proxy for CPU)
	OIDPanGPCPU           = "1.3.6.1.4.1.25461.2.1.2.3.16.0"   // panGPGatewayUtilizationPct
	// PAN-ENTITY-MIB (management plane)
	OIDPanSysCPU          = "1.3.6.1.4.1.25461.2.1.2.1.1.0"    // panSysCPULinuxPercent
	OIDPanSysMem          = "1.3.6.1.4.1.25461.2.1.2.1.2.0"    // panSysMemoryUtilization (KB total)

	// ── Juniper MIBs ──
	// JUNIPER-MIB
	OIDJnxCPU             = "1.3.6.1.4.1.2636.3.1.13.1.8"      // jnxOperatingCPU (walk, per-slot)
	OIDJnxMem             = "1.3.6.1.4.1.2636.3.1.13.1.11"     // jnxOperatingBuffer (walk, per-slot %)
	OIDJnxTemp            = "1.3.6.1.4.1.2636.3.1.13.1.7"      // jnxOperatingTemp (walk, per-slot °C)

	// ── Aruba / HPE MIBs ──
	OIDArubaAPCPU         = "1.3.6.1.4.1.14823.2.2.1.2.1.13.0" // wlsxSwitchTotalCpuUtilization
	OIDArubaAPMem         = "1.3.6.1.4.1.14823.2.2.1.2.1.15.0" // wlsxSwitchTotalMemoryUsed

	// ENTITY-SENSOR-MIB (RFC 3433)
	OIDEntPhySensorType        = "1.3.6.1.2.1.99.1.1.1.1"
	OIDEntPhySensorScale       = "1.3.6.1.2.1.99.1.1.1.2"
	OIDEntPhySensorPrecision   = "1.3.6.1.2.1.99.1.1.1.3"
	OIDEntPhySensorValue       = "1.3.6.1.2.1.99.1.1.1.4"
	OIDEntPhySensorOperStatus  = "1.3.6.1.2.1.99.1.1.1.5"
	OIDEntPhySensorUnitsDisplay = "1.3.6.1.2.1.99.1.1.1.6"

	// ── UDT: BRIDGE-MIB (RFC 4188) ──
	OIDDot1dBaseBridgeAddress = "1.3.6.1.2.1.17.1.1.0"
	OIDDot1dBasePortIfIndex   = "1.3.6.1.2.1.17.1.4.1.2" // bridge port -> ifIndex
	OIDDot1dTpFdbPort         = "1.3.6.1.2.1.17.4.3.1.2" // index: 6 MAC bytes
	OIDDot1dTpFdbStatus       = "1.3.6.1.2.1.17.4.3.1.3"

	// ── UDT: Q-BRIDGE-MIB (RFC 4363) — VLAN-aware FDB ──
	OIDDot1qTpFdbPort       = "1.3.6.1.2.1.17.7.1.2.2.1.2" // index: fdbId + 6 MAC bytes
	OIDDot1qTpFdbStatus     = "1.3.6.1.2.1.17.7.1.2.2.1.3"
	OIDDot1qVlanFdbID       = "1.3.6.1.2.1.17.7.1.4.2.1.3" // index: timeMark + vlan -> fdbId
	OIDDot1qVlanStaticName  = "1.3.6.1.2.1.17.7.1.4.3.1.1" // index: vlan
	OIDDot1qPvid            = "1.3.6.1.2.1.17.7.1.4.5.1.1" // index: bridge port

	// ── UDT: IP-MIB — ARP / neighbor tables ──
	OIDIpNetToMediaPhysAddress = "1.3.6.1.2.1.4.22.1.2" // index: ifIndex + IPv4
	OIDIpNetToMediaType        = "1.3.6.1.2.1.4.22.1.4" // 1 other 2 invalid 3 dynamic 4 static
	OIDIpNetToPhysicalPhysAddr = "1.3.6.1.2.1.4.35.1.4" // index: ifIndex + addrType + addrLen + addr

	// ── UDT: LLDP-MIB (IEEE 802.1AB) ──
	OIDLldpLocPortID           = "1.0.8802.1.1.2.1.3.7.1.3"  // index: lldpLocPortNum
	OIDLldpLocPortDesc         = "1.0.8802.1.1.2.1.3.7.1.4"
	OIDLldpRemChassisIDSubtype = "1.0.8802.1.1.2.1.4.1.1.4"  // index: timeMark.localPortNum.remIndex
	OIDLldpRemChassisID        = "1.0.8802.1.1.2.1.4.1.1.5"
	OIDLldpRemPortIDSubtype    = "1.0.8802.1.1.2.1.4.1.1.6"
	OIDLldpRemPortID           = "1.0.8802.1.1.2.1.4.1.1.7"
	OIDLldpRemPortDesc         = "1.0.8802.1.1.2.1.4.1.1.8"
	OIDLldpRemSysName          = "1.0.8802.1.1.2.1.4.1.1.9"
	OIDLldpRemSysDesc          = "1.0.8802.1.1.2.1.4.1.1.10"

	// ── UDT: CDP (CISCO-CDP-MIB) ──
	OIDCdpCacheAddress    = "1.3.6.1.4.1.9.9.23.1.2.1.1.4" // index: ifIndex.deviceIndex
	OIDCdpCacheDeviceID   = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"
	OIDCdpCacheDevicePort = "1.3.6.1.4.1.9.9.23.1.2.1.1.7"
	OIDCdpCachePlatform   = "1.3.6.1.4.1.9.9.23.1.2.1.1.8"

	// ── UDT: Cisco VLAN + trunk MIBs ──
	OIDVtpVlanState             = "1.3.6.1.4.1.9.9.46.1.3.1.1.2"  // index: mgmtDomain.vlan -> 1 operational
	OIDVtpVlanName              = "1.3.6.1.4.1.9.9.46.1.3.1.1.4"
	OIDVlanTrunkPortDynamicStat = "1.3.6.1.4.1.9.9.46.1.6.1.1.14" // index: ifIndex -> 1 trunking
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
