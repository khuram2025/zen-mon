package netflow

import (
	"net"
	"net/netip"
	"time"
)

// Record is one normalised flow record. Every decoder (NetFlow v5, v9, IPFIX
// and the sFlow sampler) produces this shape and the collector writes it to
// zenplus.flow_records unchanged, so a field means the same thing whichever
// protocol carried it (contract §4.1).
//
// Address storage follows the dual-column schema: an IPv4 flow carries 4-byte
// SrcAddr/DstAddr and leaves the *6 fields nil (written as ::); an IPv6 flow
// carries 16-byte SrcAddr6/DstAddr6 and leaves the IPv4 fields nil (written as
// 0.0.0.0). A nil address is "not exported", never a fabricated endpoint:
// decoders drop records that have no source or destination at all.
type Record struct {
	Timestamp   time.Time // flow end time (exporter clock unless clamped, see ClampClock)
	ReceivedAt  time.Time // appliance clock when the datagram arrived
	CollectorID string

	// ExporterIP is the storage form for the IPv4 exporter_ip column: the 4-byte
	// address of an IPv4 exporter, nil for an IPv6 exporter (written 0.0.0.0,
	// with the real address in exporter_ip6). ExporterAddr is the canonical,
	// unmapped identity used for every cache key and statistic.
	ExporterIP   net.IP
	ExporterAddr netip.Addr

	FlowVersion      uint8  // 5, 9, 10 (IPFIX); 0 for sFlow
	FlowSequence     uint32 // export header sequence as sent
	EngineType       uint8  // v5 header only
	EngineID         uint8  // v5 header only
	SamplingInterval uint32 // 1-in-N packet sampling applied by the exporter (1 = unsampled)

	SrcAddr  net.IP // IPv4 (4 bytes) or nil
	DstAddr  net.IP
	NextHop  net.IP
	SrcAddr6 net.IP // IPv6 (16 bytes) or nil
	DstAddr6 net.IP
	NextHop6 net.IP

	// InputSNMP/OutputSNMP are full-width ifIndexes (IE 10/14, or 252/253 when
	// those are absent). The storage layer writes them to input_if/output_if and
	// only copies them into the legacy 16-bit columns when they fit (LegacyIfIndex).
	InputSNMP  uint32
	OutputSNMP uint32

	Packets         uint64
	Bytes           uint64
	FirstSwitchedMS uint64 // exporter uptime ms (v5/v9 21/22) or epoch ms (absolute IEs)
	LastSwitchedMS  uint64
	SrcPort         uint16
	DstPort         uint16 // for ICMP/ICMPv6: type*256+code (see FieldICMPType)
	TCPFlags        uint8
	Protocol        uint8
	TOS             uint8
	SrcAS           uint32
	DstAS           uint32
	SrcMask         uint8
	DstMask         uint8

	IPVersion         uint8  // 4 or 6
	FieldsPresent     uint32 // Field* bits: which optional fields the exporter actually sent
	ObservationDomain uint32 // v9 source ID, IPFIX domain, v5 engine_type<<8|engine_id
	FlowDirection     uint8  // IE 61: 0 ingress, 1 egress, DirectionUnknown when absent
	ExportProtocol    string // Protocol* constant
	AppID             uint64 // NBAR2/App-ID: engine<<32 | selector (0 = none)
	FwEvent           uint8  // firewall event (0 none, 1 created, 2 teardown, 3 denied, 4 alert, 5 update)
	XlateSrc          net.IP // post-NAT addresses, nil when not exported
	XlateDst          net.IP
	XlateSrcPort      uint16
	XlateDstPort      uint16
	FlowEndReason     uint8 // IE 136 (0 = not exported)
}

// Field presence bits stored in flow_records.fields_present (contract §4.2).
// A bit is set only when the exporter sent the field, so an explicit zero (for
// example TCP flags = 0) is distinguishable from a field the template never
// carried. Every record written by this collector has FieldTracked set;
// fields_present = 0 therefore means a legacy row whose presence is unknown.
const (
	FieldTCPFlags       uint32 = 1 << 0
	FieldTOS            uint32 = 1 << 1
	FieldL4Ports        uint32 = 1 << 2
	FieldInputIf        uint32 = 1 << 3
	FieldOutputIf       uint32 = 1 << 4
	FieldFlowTimes      uint32 = 1 << 5
	FieldAS             uint32 = 1 << 6
	FieldMasks          uint32 = 1 << 7
	FieldNextHop        uint32 = 1 << 8
	FieldDirection      uint32 = 1 << 9
	FieldAppID          uint32 = 1 << 10
	FieldICMPType       uint32 = 1 << 11
	FieldNAT            uint32 = 1 << 12
	FieldFwEvent        uint32 = 1 << 13
	FieldSamplingInline uint32 = 1 << 14
	FieldVLAN           uint32 = 1 << 15
	// FieldSamplingAmbiguous: the exporter announced several samplers with
	// different rates, the record named none of them and no domain default
	// exists, so its counters were left unscaled (X-08).
	FieldSamplingAmbiguous uint32 = 1 << 27
	FieldClockClamped      uint32 = 1 << 28
	FieldSampledSFlow      uint32 = 1 << 29
	FieldBiflowReverse     uint32 = 1 << 30
	FieldTracked           uint32 = 1 << 31

	// FieldsV5 is what every NetFlow v5 record carries by definition: the fixed
	// 48-byte record always includes flags, ToS, ports, both ifIndexes, the
	// switched times, AS numbers, masks and the next hop.
	FieldsV5 = FieldTCPFlags | FieldTOS | FieldL4Ports | FieldInputIf | FieldOutputIf |
		FieldFlowTimes | FieldAS | FieldMasks | FieldNextHop | FieldTracked
)

// Export protocol names stored in flow_records.export_protocol and used as
// the protocol dimension of the collector statistics.
const (
	ProtocolNetFlow5 = "netflow5"
	ProtocolNetFlow9 = "netflow9"
	ProtocolIPFIX    = "ipfix"
	ProtocolSFlow5   = "sflow5"
)

// DirectionUnknown is FlowDirection when the exporter did not send IE 61.
const DirectionUnknown uint8 = 255

// CanonicalAddr turns a datagram source address into the exporter identity
// used everywhere: IPv4-mapped IPv6 (::ffff:a.b.c.d, as seen on a dual-stack
// socket) becomes plain IPv4 so the same router is one exporter whichever
// socket family delivered it. An IPv6 exporter keeps its full address; it is
// never collapsed onto 0.0.0.0 (NF-32).
func CanonicalAddr(a netip.Addr) netip.Addr {
	return a.Unmap().WithZone("")
}

// ExporterStorageIP returns the value for the IPv4 exporter_ip column: the
// address itself for an IPv4 exporter, nil (written as 0.0.0.0, with the real
// identity in exporter_ip6) for an IPv6 exporter.
func ExporterStorageIP(a netip.Addr) net.IP {
	if !a.Is4() {
		return nil
	}
	b := a.As4()
	return net.IP(b[:])
}

// LegacyIfIndex is the value written to the legacy UInt16 input_snmp /
// output_snmp columns. Rollups and readers built before schema v2 still read
// those columns; an ifIndex above 65535 cannot be represented there, and
// truncating it (70000 -> 4464) would attribute the traffic to a different,
// real interface. Such indexes are therefore written as 0 ("unknown") in the
// legacy columns, while input_if/output_if always carry the full value (NF-33).
func LegacyIfIndex(ifIndex uint32) uint16 {
	if ifIndex > 0xFFFF {
		return 0
	}
	return uint16(ifIndex)
}

// Clock policy bounds (contract §4.3). A flow cannot legitimately end in the
// future, and no exporter cache holds a flow for two hours after it ended, so a
// flow time outside [receivedAt-ClockPastTolerance, receivedAt+ClockFutureTolerance]
// means the exporter's clock is wrong, not that the flow is old.
const (
	ClockFutureTolerance = 120 * time.Second
	ClockPastTolerance   = 7200 * time.Second
)

// ClampClock applies the "clamp" clock policy to r: when the flow end time is
// implausible relative to the appliance receipt time, the record is placed at
// ReceivedAt (so it lands in the window in which it was actually observed and
// is not removed by TTL or hidden from "last hour" views) and marked with
// FieldClockClamped. The exporter's original switched times are left untouched
// for forensics. It reports whether the record was clamped.
func ClampClock(r *Record) bool {
	if r.ReceivedAt.IsZero() {
		return false
	}
	if r.Timestamp.After(r.ReceivedAt.Add(ClockFutureTolerance)) || r.Timestamp.Before(r.ReceivedAt.Add(-ClockPastTolerance)) {
		r.Timestamp = r.ReceivedAt
		r.FieldsPresent |= FieldClockClamped
		return true
	}
	return false
}
