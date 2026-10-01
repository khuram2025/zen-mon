package netflow

import (
	"bytes"
	"encoding/binary"
	"math"
	"net"
	"net/netip"
	"strings"
	"time"
	"unicode"
)

// NetFlow v9 field types and IPFIX information element IDs. For the flow
// fields used here the two numbering spaces coincide (RFC 3954 §8, IANA IPFIX
// registry), which is why one decoder serves both protocols.
const (
	ieBytes                 uint16 = 1   // octetDeltaCount / IN_BYTES
	iePackets               uint16 = 2   // packetDeltaCount / IN_PKTS
	ieProtocol              uint16 = 4   // protocolIdentifier
	ieTOS                   uint16 = 5   // ipClassOfService / SRC_TOS
	ieTCPFlags              uint16 = 6   // tcpControlBits
	ieSrcPort               uint16 = 7   // sourceTransportPort
	ieSrcIPv4               uint16 = 8   // sourceIPv4Address
	ieSrcMask               uint16 = 9   // sourceIPv4PrefixLength
	ieInputIf               uint16 = 10  // ingressInterface / INPUT_SNMP
	ieDstPort               uint16 = 11  // destinationTransportPort
	ieDstIPv4               uint16 = 12  // destinationIPv4Address
	ieDstMask               uint16 = 13  // destinationIPv4PrefixLength
	ieOutputIf              uint16 = 14  // egressInterface / OUTPUT_SNMP
	ieNextHopIPv4           uint16 = 15  // ipNextHopIPv4Address
	ieSrcAS                 uint16 = 16  // bgpSourceAsNumber
	ieDstAS                 uint16 = 17  // bgpDestinationAsNumber
	ieBGPNextHopIPv4        uint16 = 18  // bgpNextHopIPv4Address
	ieLastSwitched          uint16 = 21  // flowEndSysUpTime
	ieFirstSwitched         uint16 = 22  // flowStartSysUpTime
	ieOutBytes              uint16 = 23  // postOctetDeltaCount / OUT_BYTES
	ieOutPackets            uint16 = 24  // postPacketDeltaCount / OUT_PKTS
	ieSrcIPv6               uint16 = 27  // sourceIPv6Address
	ieDstIPv6               uint16 = 28  // destinationIPv6Address
	ieSrcMaskIPv6           uint16 = 29  // sourceIPv6PrefixLength
	ieDstMaskIPv6           uint16 = 30  // destinationIPv6PrefixLength
	ieICMPTypeCodeIPv4      uint16 = 32  // icmpTypeCodeIPv4 (type*256+code)
	ieSamplingInterval      uint16 = 34  // samplingInterval / SAMPLING_INTERVAL
	ieSamplingAlgorithm     uint16 = 35  // samplingAlgorithm (1 deterministic, 2 random; no rate)
	ieSamplerID             uint16 = 48  // samplerId / FLOW_SAMPLER_ID
	ieSamplerMode           uint16 = 49  // samplerMode (no rate)
	ieSamplerRandomInterval uint16 = 50  // samplerRandomInterval
	ieDirection             uint16 = 61  // flowDirection
	ieNextHopIPv6           uint16 = 62  // ipNextHopIPv6Address
	ieBGPNextHopIPv6        uint16 = 63  // bgpNextHopIPv6Address
	ieTotalBytes            uint16 = 85  // octetTotalCount (running total)
	ieTotalPackets          uint16 = 86  // packetTotalCount (running total)
	ieApplicationDesc       uint16 = 94  // applicationDescription (options data)
	ieApplicationID         uint16 = 95  // applicationId
	ieApplicationName       uint16 = 96  // applicationName (options data)
	ieFlowEndReason         uint16 = 136 // flowEndReason
	ieICMPTypeCodeIPv6      uint16 = 139 // icmpTypeCodeIPv6
	ieFlowStartSeconds      uint16 = 150
	ieFlowEndSeconds        uint16 = 151
	ieFlowStartMillis       uint16 = 152
	ieFlowEndMillis         uint16 = 153
	ieFlowStartMicros       uint16 = 154 // dateTimeMicroseconds (NTP format)
	ieFlowEndMicros         uint16 = 155
	ieFlowStartNanos        uint16 = 156 // dateTimeNanoseconds (NTP format)
	ieFlowEndNanos          uint16 = 157
	ieFlowStartDeltaMicros  uint16 = 158 // microseconds before the export time
	ieFlowEndDeltaMicros    uint16 = 159
	ieSystemInitTimeMillis  uint16 = 160
	ieICMPTypeIPv4          uint16 = 176
	ieICMPCodeIPv4          uint16 = 177
	ieICMPTypeIPv6          uint16 = 178
	ieICMPCodeIPv6          uint16 = 179
	ieUDPSrcPort            uint16 = 180
	ieUDPDstPort            uint16 = 181
	ieTCPSrcPort            uint16 = 182
	ieTCPDstPort            uint16 = 183
	ieDSCP                  uint16 = 195 // ipDiffServCodePoint
	ieXlateSrcIPv4          uint16 = 225 // postNATSourceIPv4Address / NSEL NF_F_XLATE_SRC_ADDR_IPV4
	ieXlateDstIPv4          uint16 = 226
	ieXlateSrcPort          uint16 = 227 // postNAPTSourceTransportPort / NF_F_XLATE_SRC_PORT
	ieXlateDstPort          uint16 = 228
	ieInitiatorOctets       uint16 = 231 // initiatorOctets / NSEL NF_F_FWD_FLOW_DELTA_BYTES
	ieResponderOctets       uint16 = 232 // responderOctets / NSEL NF_F_REV_FLOW_DELTA_BYTES
	ieFirewallEvent         uint16 = 233 // firewallEvent / NSEL NF_F_FW_EVENT
	ieIngressPhysIf         uint16 = 252 // ingressPhysicalInterface
	ieEgressPhysIf          uint16 = 253
	ieXlateSrcIPv6          uint16 = 281 // postNATSourceIPv6Address
	ieXlateDstIPv6          uint16 = 282
	ieInitiatorPackets      uint16 = 298 // initiatorPackets
	ieResponderPackets      uint16 = 299 // responderPackets
	ieSelectorID            uint16 = 302 // selectorId (PSAMP)
	iePsampInterval         uint16 = 305 // samplingPacketInterval
	iePsampSpace            uint16 = 306 // samplingPacketSpace
	ieSamplingSize          uint16 = 309
	ieSamplingPopulation    uint16 = 310
	ieSamplingProbability   uint16 = 311
	ieEventTimeMillis       uint16 = 323 // observationTimeMilliseconds / NSEL NF_F_EVENT_TIME_MSEC
	ieApplicationCategory   uint16 = 372 // applicationCategoryName (options data)

	// Cisco ASA NSEL field IDs from before ASA 8.4 moved them to the IANA
	// numbers above ("_84" in Cisco's NSEL guide). Vendor v9 numbers: in IPFIX
	// these values carry the enterprise bit and mean something else, so they
	// are only read from NetFlow v9 (nselV9Field).
	ieNSELXlateSrc84     uint16 = 40001 // NF_F_XLATE_SRC_ADDR_84
	ieNSELXlateDst84     uint16 = 40002 // NF_F_XLATE_DST_ADDR_84
	ieNSELXlateSrcPort84 uint16 = 40003 // NF_F_XLATE_SRC_PORT_84
	ieNSELXlateDstPort84 uint16 = 40004 // NF_F_XLATE_DST_PORT_84
	ieNSELFwEvent84      uint16 = 40005 // NF_F_FW_EVENT_84 (same codes as IE 233)
)

// penReverse is the private enterprise number of the RFC 5103 bidirectional
// flow reverse information elements: enterprise 29305 + IE n is "reverse IE n"
// (reverseOctetDeltaCount, reversePacketDeltaCount, reverseTcpControlBits).
const penReverse uint32 = 29305

// Firewall event codes (IE 233 firewallEvent; NSEL NF_F_FW_EVENT and the
// legacy 40005 use the same values). flow_records.fw_event stores them as sent.
const (
	FwEventCreated uint8 = 1
	FwEventDeleted uint8 = 2 // flow teardown
	FwEventDenied  uint8 = 3
	FwEventAlert   uint8 = 4
	FwEventUpdate  uint8 = 5
)

// Protocol numbers that carry ICMP type/code instead of transport ports.
const (
	protoICMP   = 1
	protoICMPv6 = 58
)

// ntpEpochOffset is the number of seconds between the NTP era (1900) used by
// the IPFIX dateTimeMicro/Nanoseconds types and the Unix epoch.
const ntpEpochOffset = 2_208_988_800

// seen* flags record which raw fields a data record carried, so precedence
// rules (bytes vs running totals, IPv4 vs IPv6, logical vs physical ifIndex,
// timing sources) are resolved after the whole record has been read and do
// not depend on the order of fields inside the template.
const (
	seenBytes uint64 = 1 << iota
	seenOutBytes
	seenTotalBytes
	seenPackets
	seenOutPackets
	seenTotalPackets
	seenSrc4
	seenDst4
	seenNextHop4
	seenBGPNextHop4
	seenSrc6
	seenDst6
	seenNextHop6
	seenBGPNextHop6
	seenInputIf
	seenOutputIf
	seenInputPhys
	seenOutputPhys
	seenSrcPort
	seenDstPort
	seenAltSrcPort
	seenAltDstPort
	seenICMPTypeCode4
	seenICMPType4
	seenICMPCode4
	seenICMPTypeCode6
	seenICMPType6
	seenICMPCode6
	seenTOS
	seenDSCP
	seenMask
	seenAS
	seenSamplerID
	seenFirstUptime
	seenLastUptime
	seenSysInit
	seenInitBytes
	seenRespBytes
	seenInitPackets
	seenRespPackets
	seenRevBytes // RFC 5103 reverseOctetDeltaCount
	seenRevPackets
	seenRevFlags
	seenEventTime
	seenFwEvent
	seenFwEvent84
	seenXlateSrc
	seenXlateDst
	seenXlateSrc84
	seenXlateDst84
	seenXlatePorts
	seenXlatePorts84
	seenAppID
)

// Precedence of flow-time sources, best first. A template may carry several
// (for example both milliseconds and sysUpTime); the most precise absolute one
// wins.
const (
	timeNone = iota
	timeMillis
	timeSeconds
	timeMicros
	timeNanos
	timeDelta
)

// samplingFields collects every way an exporter can state its packet sampling
// rate, inline in a flow record or in an options record describing a sampler.
type samplingFields struct {
	interval       uint64 // IE 34 samplingInterval / IE 50 samplerRandomInterval: 1-in-N directly
	hasInterval    bool
	psampInterval  uint64 // IE 305: packets selected ...
	psampSpace     uint64 // IE 306: ... then packets skipped
	hasPsamp       bool
	hasPsampSpace  bool
	sampSize       uint64 // IE 309 selected out of IE 310 population
	sampPopulation uint64
	hasSize        bool
	probability    float64 // IE 311
	hasProbability bool
}

func (s *samplingFields) assign(ie uint16, v []byte) bool {
	switch ie {
	case ieSamplingInterval, ieSamplerRandomInterval:
		s.interval, s.hasInterval = beUint(v), true
	case iePsampInterval:
		s.psampInterval, s.hasPsamp = beUint(v), true
	case iePsampSpace:
		s.psampSpace, s.hasPsampSpace = beUint(v), true
	case ieSamplingSize:
		s.sampSize, s.hasSize = beUint(v), true
	case ieSamplingPopulation:
		s.sampPopulation = beUint(v)
	case ieSamplingProbability:
		switch len(v) {
		case 8:
			s.probability, s.hasProbability = math.Float64frombits(binary.BigEndian.Uint64(v)), true
		case 4:
			s.probability, s.hasProbability = float64(math.Float32frombits(binary.BigEndian.Uint32(v))), true
		}
	default:
		return false
	}
	return true
}

// rate converts the collected fields to a 1-in-N interval. PSAMP systematic
// count-based sampling selects IE 305 packets then skips IE 306, so N =
// (305+306)/305 (RFC 5476); several exporters send only IE 305 carrying N
// itself, which is kept as the historical interpretation.
func (s *samplingFields) rate() (uint32, bool) {
	var n uint64
	switch {
	case s.hasInterval && s.interval > 0:
		n = s.interval
	case s.hasPsamp && s.psampInterval > 0 && s.hasPsampSpace:
		n = (s.psampInterval + s.psampSpace) / s.psampInterval
	case s.hasPsamp && s.psampInterval > 0:
		n = s.psampInterval
	case s.hasSize && s.sampSize > 0 && s.sampPopulation >= s.sampSize:
		n = s.sampPopulation / s.sampSize
	case s.hasProbability && s.probability > 0 && s.probability <= 1:
		n = uint64(math.Round(1 / s.probability))
	default:
		return 0, false
	}
	if n == 0 || n > math.MaxUint32 {
		return 0, false
	}
	return uint32(n), true
}

// decodeCtx is the per-datagram context a data record is decoded in.
type decodeCtx struct {
	exporter    netip.Addr
	exporterIP  net.IP
	domain      uint32
	collectorID string
	receivedAt  time.Time
	exportTime  time.Time // header wall clock (zero when absent)
	sysUptimeMS uint32    // v9 header only
	sequence    uint32
	version     uint8
	protocol    string
	cache       *V9TemplateCache
	sysInitMS   uint64 // IPFIX IE 160 learned from options data (0 = unknown)
	hasSysInit  bool
}

// recordState accumulates one data record's raw field values; finish() applies
// the precedence rules and produces the Record.
type recordState struct {
	seen uint64
	r    Record

	bytes, outBytes, totalBytes       uint64
	packets, outPackets, totalPackets uint64

	src4, dst4, nextHop4, bgpNextHop4 [4]byte
	src6, dst6, nextHop6, bgpNextHop6 [16]byte

	inputIf, outputIf, inputPhys, outputPhys uint32
	altSrcPort, altDstPort                   uint16

	icmpTypeCode4, icmpTypeCode6               uint16
	icmpType4, icmpCode4, icmpType6, icmpCode6 uint8
	dscp                                       uint8
	firstUptime, lastUptime                    uint64
	firstAbsMS, lastAbsMS                      uint64
	firstRank, lastRank                        int
	sysInitMS                                  uint64
	samplerID                                  uint64
	sampling                                   samplingFields

	// Session (bidirectional) counters: NSEL / IPFIX initiator-responder
	// counters and the RFC 5103 reverse IEs.
	initBytes, respBytes, initPackets, respPackets uint64
	revBytes, revPackets                           uint64
	revFlags                                       uint8

	eventMS                        uint64 // IE 323 event time (NSEL): the flow end when nothing better is sent
	fwEvent, fwEvent84             uint8
	xlateSrc, xlateDst             netip.Addr // values are copied: the datagram buffer is reused
	xlateSrc84, xlateDst84         netip.Addr
	xlateSrcPort, xlateDstPort     uint16
	xlateSrcPort84, xlateDstPort84 uint16
}

// assign stores one field value. Unknown enterprise-specific fields are
// skipped (their bytes were already consumed for alignment); the RFC 5103
// reverse IEs are the only enterprise fields read.
func (st *recordState) assign(ctx *decodeCtx, f V9TemplateField, v []byte) {
	if len(v) == 0 {
		return
	}
	if f.Enterprise != 0 {
		if f.Enterprise == penReverse {
			st.assignReverse(f.Type&^0x8000, v)
		}
		return
	}
	r := &st.r
	switch f.Type {
	case ieBytes:
		st.bytes, st.seen = beUint(v), st.seen|seenBytes
	case iePackets:
		st.packets, st.seen = beUint(v), st.seen|seenPackets
	case ieOutBytes:
		st.outBytes, st.seen = beUint(v), st.seen|seenOutBytes
	case ieOutPackets:
		st.outPackets, st.seen = beUint(v), st.seen|seenOutPackets
	case ieTotalBytes:
		st.totalBytes, st.seen = beUint(v), st.seen|seenTotalBytes
	case ieTotalPackets:
		st.totalPackets, st.seen = beUint(v), st.seen|seenTotalPackets
	case ieProtocol:
		r.Protocol = uint8(beUint(v))
	case ieTOS:
		r.TOS, st.seen = uint8(beUint(v)), st.seen|seenTOS
	case ieDSCP:
		st.dscp, st.seen = uint8(beUint(v)), st.seen|seenDSCP
	case ieTCPFlags:
		// IPFIX tcpControlBits is 16 bits wide (NS/CWR/ECE above the classic 8);
		// the stored column keeps the classic flag byte.
		r.TCPFlags = uint8(beUint(v))
		r.FieldsPresent |= FieldTCPFlags
	case ieSrcPort:
		r.SrcPort, st.seen = uint16(beUint(v)), st.seen|seenSrcPort
	case ieDstPort:
		r.DstPort, st.seen = uint16(beUint(v)), st.seen|seenDstPort
	case ieUDPSrcPort, ieTCPSrcPort:
		st.altSrcPort, st.seen = uint16(beUint(v)), st.seen|seenAltSrcPort
	case ieUDPDstPort, ieTCPDstPort:
		st.altDstPort, st.seen = uint16(beUint(v)), st.seen|seenAltDstPort
	case ieSrcIPv4:
		if len(v) == 4 {
			copy(st.src4[:], v)
			st.seen |= seenSrc4
		}
	case ieDstIPv4:
		if len(v) == 4 {
			copy(st.dst4[:], v)
			st.seen |= seenDst4
		}
	case ieNextHopIPv4:
		if len(v) == 4 {
			copy(st.nextHop4[:], v)
			st.seen |= seenNextHop4
		}
	case ieBGPNextHopIPv4:
		if len(v) == 4 {
			copy(st.bgpNextHop4[:], v)
			st.seen |= seenBGPNextHop4
		}
	case ieSrcIPv6:
		if len(v) == 16 {
			copy(st.src6[:], v)
			st.seen |= seenSrc6
		}
	case ieDstIPv6:
		if len(v) == 16 {
			copy(st.dst6[:], v)
			st.seen |= seenDst6
		}
	case ieNextHopIPv6:
		if len(v) == 16 {
			copy(st.nextHop6[:], v)
			st.seen |= seenNextHop6
		}
	case ieBGPNextHopIPv6:
		if len(v) == 16 {
			copy(st.bgpNextHop6[:], v)
			st.seen |= seenBGPNextHop6
		}
	case ieSrcMask, ieSrcMaskIPv6:
		r.SrcMask, st.seen = uint8(beUint(v)), st.seen|seenMask
	case ieDstMask, ieDstMaskIPv6:
		r.DstMask, st.seen = uint8(beUint(v)), st.seen|seenMask
	case ieInputIf:
		st.inputIf, st.seen = uint32(beUint(v)), st.seen|seenInputIf
	case ieOutputIf:
		st.outputIf, st.seen = uint32(beUint(v)), st.seen|seenOutputIf
	case ieIngressPhysIf:
		st.inputPhys, st.seen = uint32(beUint(v)), st.seen|seenInputPhys
	case ieEgressPhysIf:
		st.outputPhys, st.seen = uint32(beUint(v)), st.seen|seenOutputPhys
	case ieSrcAS:
		r.SrcAS, st.seen = uint32(beUint(v)), st.seen|seenAS
	case ieDstAS:
		r.DstAS, st.seen = uint32(beUint(v)), st.seen|seenAS
	case ieDirection:
		r.FlowDirection = uint8(beUint(v))
		r.FieldsPresent |= FieldDirection
	case ieFlowEndReason:
		r.FlowEndReason = uint8(beUint(v))
	case ieICMPTypeCodeIPv4:
		st.icmpTypeCode4, st.seen = uint16(beUint(v)), st.seen|seenICMPTypeCode4
	case ieICMPTypeCodeIPv6:
		st.icmpTypeCode6, st.seen = uint16(beUint(v)), st.seen|seenICMPTypeCode6
	case ieICMPTypeIPv4:
		st.icmpType4, st.seen = uint8(beUint(v)), st.seen|seenICMPType4
	case ieICMPCodeIPv4:
		st.icmpCode4, st.seen = uint8(beUint(v)), st.seen|seenICMPCode4
	case ieICMPTypeIPv6:
		st.icmpType6, st.seen = uint8(beUint(v)), st.seen|seenICMPType6
	case ieICMPCodeIPv6:
		st.icmpCode6, st.seen = uint8(beUint(v)), st.seen|seenICMPCode6
	case ieLastSwitched:
		st.lastUptime, st.seen = beUint(v), st.seen|seenLastUptime
	case ieFirstSwitched:
		st.firstUptime, st.seen = beUint(v), st.seen|seenFirstUptime
	case ieFlowStartMillis:
		st.setTime(false, timeMillis, beUint(v))
	case ieFlowEndMillis:
		st.setTime(true, timeMillis, beUint(v))
	case ieFlowStartSeconds:
		st.setTime(false, timeSeconds, beUint(v)*1000)
	case ieFlowEndSeconds:
		st.setTime(true, timeSeconds, beUint(v)*1000)
	case ieFlowStartMicros, ieFlowStartNanos:
		if ms, ok := ntpMillis(v); ok {
			st.setTime(false, rankNTP(f.Type), ms)
		}
	case ieFlowEndMicros, ieFlowEndNanos:
		if ms, ok := ntpMillis(v); ok {
			st.setTime(true, rankNTP(f.Type), ms)
		}
	case ieFlowStartDeltaMicros, ieFlowEndDeltaMicros:
		exportMS, deltaMS := uint64(ctx.exportTime.UnixMilli()), beUint(v)/1000
		if ctx.exportTime.IsZero() || deltaMS > exportMS {
			return
		}
		st.setTime(f.Type == ieFlowEndDeltaMicros, timeDelta, exportMS-deltaMS)
	case ieSystemInitTimeMillis:
		st.sysInitMS, st.seen = beUint(v), st.seen|seenSysInit
	case ieSamplerID, ieSelectorID:
		st.samplerID, st.seen = beUint(v), st.seen|seenSamplerID
	case ieInitiatorOctets:
		st.initBytes, st.seen = beUint(v), st.seen|seenInitBytes
	case ieResponderOctets:
		st.respBytes, st.seen = beUint(v), st.seen|seenRespBytes
	case ieInitiatorPackets:
		st.initPackets, st.seen = beUint(v), st.seen|seenInitPackets
	case ieResponderPackets:
		st.respPackets, st.seen = beUint(v), st.seen|seenRespPackets
	case ieFirewallEvent:
		st.fwEvent, st.seen = uint8(beUint(v)), st.seen|seenFwEvent
	case ieXlateSrcIPv4, ieXlateSrcIPv6:
		if setAddr(&st.xlateSrc, v) {
			st.seen |= seenXlateSrc
		}
	case ieXlateDstIPv4, ieXlateDstIPv6:
		if setAddr(&st.xlateDst, v) {
			st.seen |= seenXlateDst
		}
	case ieXlateSrcPort:
		st.xlateSrcPort, st.seen = uint16(beUint(v)), st.seen|seenXlatePorts
	case ieXlateDstPort:
		st.xlateDstPort, st.seen = uint16(beUint(v)), st.seen|seenXlatePorts
	case ieEventTimeMillis:
		st.eventMS, st.seen = beUint(v), st.seen|seenEventTime
	case ieApplicationID:
		if id, ok := appID(v); ok {
			r.AppID, st.seen = id, st.seen|seenAppID
		}
	case ieNSELFwEvent84, ieNSELXlateSrc84, ieNSELXlateDst84, ieNSELXlateSrcPort84, ieNSELXlateDstPort84:
		if ctx.version == 9 {
			st.assignNSEL84(f.Type, v)
		}
	default:
		st.sampling.assign(f.Type, v)
	}
}

// assignReverse reads the RFC 5103 reverse information elements (enterprise
// 29305): the responder→initiator counters and TCP flags of a biflow record.
func (st *recordState) assignReverse(ie uint16, v []byte) {
	switch ie {
	case ieBytes:
		st.revBytes, st.seen = beUint(v), st.seen|seenRevBytes
	case iePackets:
		st.revPackets, st.seen = beUint(v), st.seen|seenRevPackets
	case ieTCPFlags:
		st.revFlags, st.seen = uint8(beUint(v)), st.seen|seenRevFlags
	}
}

// assignNSEL84 reads the pre-8.4 Cisco ASA NSEL fields. They are fallbacks:
// finish() prefers the IANA-numbered fields when a template carries both.
func (st *recordState) assignNSEL84(ie uint16, v []byte) {
	switch ie {
	case ieNSELFwEvent84:
		st.fwEvent84, st.seen = uint8(beUint(v)), st.seen|seenFwEvent84
	case ieNSELXlateSrc84:
		if setAddr(&st.xlateSrc84, v) {
			st.seen |= seenXlateSrc84
		}
	case ieNSELXlateDst84:
		if setAddr(&st.xlateDst84, v) {
			st.seen |= seenXlateDst84
		}
	case ieNSELXlateSrcPort84:
		st.xlateSrcPort84, st.seen = uint16(beUint(v)), st.seen|seenXlatePorts84
	case ieNSELXlateDstPort84:
		st.xlateDstPort84, st.seen = uint16(beUint(v)), st.seen|seenXlatePorts84
	}
}

// setAddr stores a 4- or 16-byte address field. A template carrying both an
// IPv4 and an IPv6 variant (dual-stack NAT) sends zeros in the family the
// flow does not use, so a zero value never replaces a real one.
func setAddr(dst *netip.Addr, v []byte) bool {
	a, ok := netip.AddrFromSlice(v)
	if !ok {
		return false
	}
	if !dst.IsValid() || dst.IsUnspecified() {
		*dst = a
	}
	return true
}

// appID converts an applicationId (IE 95, RFC 6759: one classification
// engine byte followed by the selector) to the stored form engine<<32 |
// selector, where the selector is the big-endian value of its first four
// bytes. NBAR2 selectors are three bytes (13:453 = 13<<32 | 453); longer
// selectors (e.g. PANA-L7 with an enterprise number) keep their first four.
func appID(v []byte) (uint64, bool) {
	if len(v) < 2 {
		return 0, false
	}
	sel := v[1:]
	if len(sel) > 4 {
		sel = sel[:4]
	}
	return uint64(v[0])<<32 | beUint(sel), true
}

func rankNTP(ie uint16) int {
	if ie == ieFlowStartMicros || ie == ieFlowEndMicros {
		return timeMicros
	}
	return timeNanos
}

func (st *recordState) setTime(end bool, rank int, ms uint64) {
	if end {
		if st.lastRank == timeNone || rank < st.lastRank {
			st.lastAbsMS, st.lastRank = ms, rank
		}
		return
	}
	if st.firstRank == timeNone || rank < st.firstRank {
		st.firstAbsMS, st.firstRank = ms, rank
	}
}

// ntpMillis converts an IPFIX dateTimeMicroseconds/Nanoseconds value (NTP
// 64-bit timestamp: seconds since 1900 + 2^-32 fraction) to Unix milliseconds.
func ntpMillis(v []byte) (uint64, bool) {
	if len(v) != 8 {
		return 0, false
	}
	secs := uint64(binary.BigEndian.Uint32(v[0:4]))
	frac := uint64(binary.BigEndian.Uint32(v[4:8]))
	if secs < ntpEpochOffset {
		return 0, false
	}
	return (secs-ntpEpochOffset)*1000 + (frac*1000)>>32, true
}

// finish resolves the collected fields and appends the record to out: one
// record, or two for a bidirectional export (the responder→initiator half is
// a second record, see reverseOf). ok=false means the record carries no
// usable source/destination address and must be dropped (and counted) rather
// than stored with a fabricated 0.0.0.0 endpoint.
func (st *recordState) finish(ctx *decodeCtx, out []Record) ([]Record, bool) {
	r := st.r
	r.ReceivedAt = ctx.receivedAt.UTC()
	r.Timestamp = r.ReceivedAt
	r.CollectorID = ctx.collectorID
	r.ExporterIP = ctx.exporterIP
	r.ExporterAddr = ctx.exporter
	r.FlowVersion = ctx.version
	r.FlowSequence = ctx.sequence
	r.ObservationDomain = ctx.domain
	r.ExportProtocol = ctx.protocol
	r.FieldsPresent |= FieldTracked
	if r.FieldsPresent&FieldDirection == 0 {
		r.FlowDirection = DirectionUnknown
	}

	if !st.resolveAddresses(&r) {
		return out, false
	}
	rev := st.resolveCounters(&r)
	st.resolveFirewall(&r)
	if st.seen&seenAppID != 0 {
		r.FieldsPresent |= FieldAppID
	}

	// Interfaces: logical ifIndex (IE 10/14) first, the physical interface
	// (IE 252/253) when the template carries only that. Full 32-bit width.
	switch {
	case st.seen&seenInputIf != 0:
		r.InputSNMP = st.inputIf
		r.FieldsPresent |= FieldInputIf
	case st.seen&seenInputPhys != 0:
		r.InputSNMP = st.inputPhys
		r.FieldsPresent |= FieldInputIf
	}
	switch {
	case st.seen&seenOutputIf != 0:
		r.OutputSNMP = st.outputIf
		r.FieldsPresent |= FieldOutputIf
	case st.seen&seenOutputPhys != 0:
		r.OutputSNMP = st.outputPhys
		r.FieldsPresent |= FieldOutputIf
	}

	if st.seen&(seenSrcPort|seenDstPort|seenAltSrcPort|seenAltDstPort) != 0 {
		if st.seen&seenSrcPort == 0 && st.seen&seenAltSrcPort != 0 {
			r.SrcPort = st.altSrcPort
		}
		if st.seen&seenDstPort == 0 && st.seen&seenAltDstPort != 0 {
			r.DstPort = st.altDstPort
		}
		r.FieldsPresent |= FieldL4Ports
	}
	st.resolveICMP(&r)

	switch {
	case st.seen&seenTOS != 0:
		r.FieldsPresent |= FieldTOS
	case st.seen&seenDSCP != 0:
		r.TOS = st.dscp << 2
		r.FieldsPresent |= FieldTOS
	}
	if st.seen&seenMask != 0 {
		r.FieldsPresent |= FieldMasks
	}
	if st.seen&seenAS != 0 {
		r.FieldsPresent |= FieldAS
	}

	st.resolveTimes(ctx, &r)
	st.resolveSampling(ctx, &r)
	out = append(out, r)
	if rev.present {
		out = append(out, reverseOf(r, rev))
	}
	return out, true
}

// reverseCounters is the responder→initiator half of a bidirectional record.
type reverseCounters struct {
	present        bool
	bytes, packets uint64
	flags          uint8
	hasFlags       bool
}

// resolveCounters sets the forward counters and returns the reverse ones.
//
// Forward, best first: initiator counters (IE 231/298, session exporters such
// as Cisco ASA/FTD NSEL, whose records carry no IE 1/2), the per-export delta
// (IE 1/2), OUT_BYTES/OUT_PKTS (23/24) when the template has no IE 1 (an
// egress-only template: they are the flow's own counters), and running totals
// (85/86) as a last resort: re-summing a running total per export over-counts
// long flows, so it never overwrites a delta the template also carried (X-07).
//
// Reverse (X-06): responder counters (IE 232/299), the RFC 5103 reverse IEs,
// or - NetFlow v9 only - IN_BYTES and OUT_BYTES both present and different:
// session-based v9 exporters (nProbe bi-flow mode, several firewalls) put the
// responder → initiator direction in OUT_BYTES/OUT_PKTS, which nfdump reads
// the same way. The old decoder kept only one of the two and lost the other
// direction. Equal values are a copy of the same counter, not a second
// direction. In IPFIX, IE 23/24 are postOctetDeltaCount/postPacketDeltaCount
// (RFC 5102): the SAME direction counted after the observation point (after
// a middlebox, a NAT or a policer), never the reply, so they only stand in
// for IE 1/2 when those are absent.
func (st *recordState) resolveCounters(r *Record) reverseCounters {
	switch {
	case st.seen&seenInitBytes != 0:
		r.Bytes = st.initBytes
	case st.seen&seenBytes != 0:
		r.Bytes = st.bytes
	case st.seen&seenOutBytes != 0:
		r.Bytes = st.outBytes
	case st.seen&seenTotalBytes != 0:
		r.Bytes = st.totalBytes
	}
	switch {
	case st.seen&seenInitPackets != 0:
		r.Packets = st.initPackets
	case st.seen&seenPackets != 0:
		r.Packets = st.packets
	case st.seen&seenOutPackets != 0 && st.seen&seenBytes == 0:
		r.Packets = st.outPackets
	case st.seen&seenTotalPackets != 0:
		r.Packets = st.totalPackets
	}

	var rev reverseCounters
	switch {
	case st.seen&(seenRespBytes|seenRespPackets) != 0:
		rev.bytes, rev.packets = st.respBytes, st.respPackets
	case st.seen&(seenRevBytes|seenRevPackets) != 0:
		rev.bytes, rev.packets = st.revBytes, st.revPackets
	case r.FlowVersion == 9 && st.seen&seenInitBytes == 0 && st.seen&seenBytes != 0 && st.seen&seenOutBytes != 0 &&
		(st.bytes != st.outBytes || (st.seen&seenPackets != 0 && st.seen&seenOutPackets != 0 && st.packets != st.outPackets)):
		rev.bytes = st.outBytes
		if st.seen&seenOutPackets != 0 {
			rev.packets = st.outPackets
		}
	}
	if st.seen&seenRevFlags != 0 {
		rev.flags, rev.hasFlags = st.revFlags, true
	}
	// A session event without responder traffic (NSEL create/deny, a flow
	// with no reply yet) is one record: a reverse record with nothing in it
	// would count a flow that never carried a packet.
	rev.present = rev.bytes > 0 || rev.packets > 0
	return rev
}

// resolveFirewall sets the NSEL / IPFIX firewall event and the post-NAT
// addresses and ports, preferring the IANA-numbered fields over the pre-8.4
// ASA ones. An all-zero translated address means "not translated" and is
// stored as none (::), never as a 0.0.0.0 endpoint.
func (st *recordState) resolveFirewall(r *Record) {
	switch {
	case st.seen&seenFwEvent != 0:
		r.FwEvent = st.fwEvent
		r.FieldsPresent |= FieldFwEvent
	case st.seen&seenFwEvent84 != 0:
		r.FwEvent = st.fwEvent84
		r.FieldsPresent |= FieldFwEvent
	}
	src, dst := st.xlateSrc, st.xlateDst
	if st.seen&seenXlateSrc == 0 {
		src = st.xlateSrc84
	}
	if st.seen&seenXlateDst == 0 {
		dst = st.xlateDst84
	}
	r.XlateSrc, r.XlateDst = natIP(src), natIP(dst)
	switch {
	case st.seen&seenXlatePorts != 0:
		r.XlateSrcPort, r.XlateDstPort = st.xlateSrcPort, st.xlateDstPort
	case st.seen&seenXlatePorts84 != 0:
		r.XlateSrcPort, r.XlateDstPort = st.xlateSrcPort84, st.xlateDstPort84
	}
	if st.seen&(seenXlateSrc|seenXlateDst|seenXlateSrc84|seenXlateDst84|seenXlatePorts|seenXlatePorts84) != 0 {
		r.FieldsPresent |= FieldNAT
	}
}

func natIP(a netip.Addr) net.IP {
	if !a.IsValid() || a.Unmap().IsUnspecified() {
		return nil
	}
	return ipFromAddr(a.Unmap())
}

// reverseOf builds the responder→initiator record of a bidirectional export:
// addresses, ports, interfaces, AS numbers, masks and translations swapped,
// carrying the reverse counters, marked FieldBiflowReverse. What the exporter
// reported only for the forward direction is not carried over: the next hop,
// the TCP flags (unless the reverse flags were exported, RFC 5103) and the
// ICMP type/code. Copying them would, for example, turn the reply half of
// every SYN-only probe into a second SYN-only flow. The flow direction (IE 61)
// is kept: with the interfaces swapped it still describes where the reverse
// traffic was observed relative to its own input interface.
func reverseOf(fwd Record, rev reverseCounters) Record {
	r := fwd
	r.SrcAddr, r.DstAddr = fwd.DstAddr, fwd.SrcAddr
	r.SrcAddr6, r.DstAddr6 = fwd.DstAddr6, fwd.SrcAddr6
	r.SrcPort, r.DstPort = fwd.DstPort, fwd.SrcPort
	r.InputSNMP, r.OutputSNMP = fwd.OutputSNMP, fwd.InputSNMP
	r.SrcAS, r.DstAS = fwd.DstAS, fwd.SrcAS
	r.SrcMask, r.DstMask = fwd.DstMask, fwd.SrcMask
	r.XlateSrc, r.XlateDst = fwd.XlateDst, fwd.XlateSrc
	r.XlateSrcPort, r.XlateDstPort = fwd.XlateDstPort, fwd.XlateSrcPort
	r.NextHop, r.NextHop6 = nil, nil
	r.Bytes, r.Packets = rev.bytes, rev.packets
	r.TCPFlags = 0

	fp := fwd.FieldsPresent &^ (FieldInputIf | FieldOutputIf | FieldTCPFlags | FieldNextHop | FieldICMPType)
	if fwd.FieldsPresent&FieldInputIf != 0 {
		fp |= FieldOutputIf
	}
	if fwd.FieldsPresent&FieldOutputIf != 0 {
		fp |= FieldInputIf
	}
	if rev.hasFlags {
		r.TCPFlags = rev.flags
		fp |= FieldTCPFlags
	}
	if fwd.Protocol == protoICMP || fwd.Protocol == protoICMPv6 {
		// ICMP has no ports: the "port" fields carry the type/code (IE 32/139/
		// 176-179, or in dst_port as many v9 exporters send it). The reply's
		// type is not exported (an echo request's reverse is an echo reply),
		// so leave it unknown rather than repeat the request's or move it to
		// src_port.
		r.SrcPort, r.DstPort = 0, 0
		fp &^= FieldL4Ports
	}
	r.FieldsPresent = fp | FieldBiflowReverse
	return r
}

// resolveAddresses picks the address family and fills the storage fields.
// Templates that carry both families (dual-stack caches such as nProbe's)
// send zeros in the family a flow does not use, so when both are present
// the family with a non-zero endpoint wins.
func (st *recordState) resolveAddresses(r *Record) bool {
	has4 := st.seen&(seenSrc4|seenDst4) != 0
	has6 := st.seen&(seenSrc6|seenDst6) != 0
	if has4 && has6 {
		has6 = st.src6 != [16]byte{} || st.dst6 != [16]byte{}
		has4 = !has6
	}
	switch {
	case has6:
		// Both endpoints are required: a missing side would be stored as ::,
		// an endpoint the exporter never reported.
		if st.seen&seenSrc6 == 0 || st.seen&seenDst6 == 0 {
			return false
		}
		src, dst := netip.AddrFrom16(st.src6), netip.AddrFrom16(st.dst6)
		if src.Is4In6() && dst.Is4In6() {
			// IPv4 traffic exported in IPv6 fields: store it as IPv4 so it
			// matches IPv4 filters and rollups.
			r.SrcAddr, r.DstAddr, r.IPVersion = ipFromAddr(src.Unmap()), ipFromAddr(dst.Unmap()), 4
		} else {
			r.SrcAddr6, r.DstAddr6, r.IPVersion = net.IP(append([]byte(nil), st.src6[:]...)), net.IP(append([]byte(nil), st.dst6[:]...)), 6
		}
	case has4:
		if st.seen&seenSrc4 == 0 || st.seen&seenDst4 == 0 {
			return false
		}
		r.SrcAddr, r.DstAddr, r.IPVersion = net.IP(append([]byte(nil), st.src4[:]...)), net.IP(append([]byte(nil), st.dst4[:]...)), 4
	default:
		return false
	}

	// Next hop: the IGP next hop wins over the BGP next hop.
	switch {
	case st.seen&seenNextHop4 != 0:
		r.NextHop = net.IP(append([]byte(nil), st.nextHop4[:]...))
	case st.seen&seenBGPNextHop4 != 0:
		r.NextHop = net.IP(append([]byte(nil), st.bgpNextHop4[:]...))
	}
	switch {
	case st.seen&seenNextHop6 != 0:
		r.NextHop6 = net.IP(append([]byte(nil), st.nextHop6[:]...))
	case st.seen&seenBGPNextHop6 != 0:
		r.NextHop6 = net.IP(append([]byte(nil), st.bgpNextHop6[:]...))
	}
	if r.NextHop != nil || r.NextHop6 != nil {
		r.FieldsPresent |= FieldNextHop
	}
	return true
}

func ipFromAddr(a netip.Addr) net.IP {
	if a.Is4() {
		b := a.As4()
		return net.IP(b[:])
	}
	b := a.As16()
	return net.IP(b[:])
}

// resolveICMP normalises ICMP type/code into DstPort (type*256+code), the
// convention v5 and most v9 exporters already use, so one column answers
// "which ICMP message" whichever IE the exporter chose.
func (st *recordState) resolveICMP(r *Record) {
	var (
		tc    uint16
		found bool
	)
	v4 := func() {
		switch {
		case st.seen&seenICMPTypeCode4 != 0:
			tc, found = st.icmpTypeCode4, true
		case st.seen&(seenICMPType4|seenICMPCode4) != 0:
			tc, found = uint16(st.icmpType4)<<8|uint16(st.icmpCode4), true
		}
	}
	v6 := func() {
		switch {
		case st.seen&seenICMPTypeCode6 != 0:
			tc, found = st.icmpTypeCode6, true
		case st.seen&(seenICMPType6|seenICMPCode6) != 0:
			tc, found = uint16(st.icmpType6)<<8|uint16(st.icmpCode6), true
		}
	}
	switch r.Protocol {
	case protoICMP:
		if v4(); !found {
			v6()
		}
	case protoICMPv6:
		if v6(); !found {
			v4()
		}
	default:
		return
	}
	if found {
		r.DstPort = tc
		r.FieldsPresent |= FieldICMPType
	}
}

func (st *recordState) resolveTimes(ctx *decodeCtx, r *Record) {
	sysInit, hasSysInit := ctx.sysInitMS, ctx.hasSysInit
	if st.seen&seenSysInit != 0 {
		sysInit, hasSysInit = st.sysInitMS, true
	}
	uptimeToAbs := func(up uint64) (uint64, bool) {
		if ctx.version == 10 && hasSysInit {
			return sysInit + up, true
		}
		return 0, false
	}

	switch {
	case st.lastRank != timeNone:
		r.Timestamp = time.UnixMilli(int64(st.lastAbsMS)).UTC()
		r.LastSwitchedMS = st.lastAbsMS
	case st.seen&seenLastUptime != 0:
		if abs, ok := uptimeToAbs(st.lastUptime); ok {
			r.Timestamp = time.UnixMilli(int64(abs)).UTC()
			r.LastSwitchedMS = abs
		} else {
			r.Timestamp = uptimeTimestamp(ctx.exportTime, ctx.sysUptimeMS, uint32(st.lastUptime), ctx.receivedAt)
			r.LastSwitchedMS = st.lastUptime
		}
	case st.seen&seenEventTime != 0 && st.eventMS > 0:
		// NSEL records carry no flow end time: the event time (the teardown,
		// update or denial) is when the record's traffic was accounted.
		r.Timestamp = time.UnixMilli(int64(st.eventMS)).UTC()
		r.LastSwitchedMS = st.eventMS
	}
	switch {
	case st.firstRank != timeNone:
		r.FirstSwitchedMS = st.firstAbsMS
	case st.seen&seenFirstUptime != 0:
		if abs, ok := uptimeToAbs(st.firstUptime); ok {
			r.FirstSwitchedMS = abs
		} else {
			r.FirstSwitchedMS = st.firstUptime
		}
	}
	if st.lastRank != timeNone || st.firstRank != timeNone || st.seen&(seenFirstUptime|seenLastUptime|seenEventTime) != 0 {
		r.FieldsPresent |= FieldFlowTimes
	}
}

// resolveSampling applies, in order: a rate carried inline in the record
// (IE 34/50/305-306/309-310/311), the rate of the sampler the record names
// (IE 48/302), the exporter-domain default learned from options data, the one
// rate all of the domain's samplers share, else 1. A record that names no
// known sampler on a domain whose samplers disagree stays 1 and is flagged
// FieldSamplingAmbiguous rather than scaled by an arbitrary rate (X-08).
func (st *recordState) resolveSampling(ctx *decodeCtx, r *Record) {
	if n, ok := st.sampling.rate(); ok {
		r.SamplingInterval = n
		r.FieldsPresent |= FieldSamplingInline
		return
	}
	n, state := ctx.cache.ResolveSampler(ctx.exporter, ctx.domain, st.samplerID, st.seen&seenSamplerID != 0)
	switch state {
	case SamplerKnown:
		r.SamplingInterval = n
		return
	case SamplerAmbiguous:
		r.FieldsPresent |= FieldSamplingAmbiguous
	}
	r.SamplingInterval = 1
}

// maxPreallocRecords is the record capacity reserved per data set up front;
// larger sets grow the slice as their records are actually decoded.
const maxPreallocRecords = 64

// nextRecord walks one record of fields starting at off, calling fn for each
// field value. It returns the offset after the record and ok=false when the
// remaining bytes cannot hold a complete record (trailing padding or a
// truncated set), in which case nothing more should be read.
func nextRecord(body []byte, off int, fields []V9TemplateField, fn func(i int, f V9TemplateField, v []byte)) (int, bool) {
	pos := off
	for i, f := range fields {
		flen := int(f.Length)
		if f.Length == 0xFFFF { // RFC 7011 §7 variable-length encoding
			if pos >= len(body) {
				return off, false
			}
			flen = int(body[pos])
			pos++
			if flen == 255 {
				if pos+2 > len(body) {
					return off, false
				}
				flen = int(binary.BigEndian.Uint16(body[pos : pos+2]))
				pos += 2
			}
		}
		if pos+flen > len(body) {
			return off, false
		}
		if fn != nil {
			fn(i, f, body[pos:pos+flen])
		}
		pos += flen
	}
	return pos, pos > off
}

// decodeDataSet decodes every flow record of a data set. It returns the
// records, the number of data records the set held (for IPFIX sequence
// accounting) and how many were dropped for lacking addresses.
func decodeDataSet(body []byte, tpl V9Template, ctx *decodeCtx) (records []Record, seen int, unsupported int) {
	minLen := tpl.dec.minLen
	if minLen == 0 {
		return nil, 0, 0
	}
	// Preallocate for a typical data set only: a template with a 1-byte
	// record would otherwise reserve room for 65,000 records (28 MB) per
	// datagram before a single address is checked.
	records = make([]Record, 0, min(len(body)/minLen, maxPreallocRecords))
	for off := 0; len(body)-off >= minLen; {
		var st recordState
		next, ok := nextRecord(body, off, tpl.dec.fields, func(_ int, f V9TemplateField, v []byte) {
			st.assign(ctx, f, v)
		})
		if !ok {
			break
		}
		off = next
		seen++
		if records, ok = st.finish(ctx, records); !ok {
			unsupported++
		}
	}
	return records, seen, unsupported
}

// AppName is one application an exporter describes in options data (the
// Cisco NBAR2 "application table" / "application attributes" option
// templates): IE 95 applicationId with IE 96 applicationName, IE 94
// applicationDescription and IE 372 applicationCategoryName. An exporter may
// send the name and the category in separate records; empty fields mean "not
// in this record", not "no name".
type AppName struct {
	Exporter    netip.Addr
	AppID       uint64 // engine<<32 | selector, as in Record.AppID
	Name        string
	Description string
	Category    string
}

// maxAppText bounds a stored application name / description / category.
const maxAppText = 256

// appText turns an options string field into clean text: fixed-length fields
// are NUL-padded (and some exporters leave garbage after the terminator), and
// the value ends up in UI labels, so it is cut at the first NUL, made valid
// UTF-8 without control characters, trimmed and bounded.
func appText(v []byte) string {
	if i := bytes.IndexByte(v, 0); i >= 0 {
		v = v[:i]
	}
	s := strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return -1
		}
		return r
	}, strings.ToValidUTF8(string(v), ""))
	s = strings.TrimSpace(s)
	if len(s) > maxAppText {
		s = strings.ToValidUTF8(s[:maxAppText], "")
	}
	return s
}

// optionsRecord is what one options data record says about its exporter.
type optionsRecord struct {
	samplerID    uint64
	hasSamplerID bool
	sampling     samplingFields
	sysInitMS    uint64
	hasSysInit   bool
	app          AppName
	hasAppID     bool
}

// optionsResult is what an options data set taught the decoder.
type optionsResult struct {
	records  int // options data records read
	samplers int // sampler definitions learned
	apps     []AppName
}

// decodeOptionsSet reads EVERY record of an options data set (an exporter
// with several samplers or applications describes each in its own record) and
// applies what it learns to the cache. v9 scope fields use their own type
// space (1 System, 2 Interface, ...), so they are skipped; IPFIX scope fields
// are ordinary IEs (e.g. selectorId, applicationId) and are read.
func decodeOptionsSet(body []byte, tpl V9OptionsTemplate, ctx *decodeCtx) optionsResult {
	var res optionsResult
	fields, minLen, scopeCount := tpl.dec.fields, tpl.dec.minLen, tpl.dec.scope
	if minLen == 0 {
		return res
	}
	for off := 0; len(body)-off >= minLen; {
		var o optionsRecord
		next, ok := nextRecord(body, off, fields, func(i int, f V9TemplateField, v []byte) {
			if f.Enterprise != 0 || len(v) == 0 || (i < scopeCount && ctx.version == 9) {
				return
			}
			switch f.Type {
			case ieSamplerID, ieSelectorID:
				o.samplerID, o.hasSamplerID = beUint(v), true
			case ieSystemInitTimeMillis:
				o.sysInitMS, o.hasSysInit = beUint(v), true
			case ieApplicationID:
				o.app.AppID, o.hasAppID = appID(v)
			case ieApplicationName:
				o.app.Name = appText(v)
			case ieApplicationDesc:
				o.app.Description = appText(v)
			case ieApplicationCategory:
				o.app.Category = appText(v)
			default:
				o.sampling.assign(f.Type, v)
			}
		})
		if !ok {
			break
		}
		off = next
		res.records++
		if n, ok := o.sampling.rate(); ok {
			ctx.cache.PutSampler(ctx.exporter, ctx.domain, o.samplerID, o.hasSamplerID, n)
			res.samplers++
		}
		if o.hasSysInit && o.sysInitMS > 0 {
			ctx.cache.PutInitTime(ctx.exporter, ctx.domain, o.sysInitMS)
			ctx.sysInitMS, ctx.hasSysInit = o.sysInitMS, true
		}
		if o.hasAppID && o.app.AppID != 0 && (o.app.Name != "" || o.app.Description != "" || o.app.Category != "") {
			o.app.Exporter = ctx.exporter
			res.apps = append(res.apps, o.app)
		}
	}
	return res
}

func beUint(value []byte) uint64 {
	var out uint64
	for _, b := range value {
		out = (out << 8) | uint64(b)
	}
	return out
}
