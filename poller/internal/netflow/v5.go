package netflow

import (
	"encoding/binary"
	"fmt"
	"net"
	"net/netip"
	"time"
)

const (
	V5HeaderLen = 24
	V5RecordLen = 48
)

// V5Packet is a decoded NetFlow v5 datagram.
type V5Packet struct {
	Version      uint16
	Count        uint16
	SysUptimeMS  uint32
	UnixSecs     uint32
	UnixNsecs    uint32
	FlowSequence uint32 // total flows exported before this datagram (sequence-gap accounting)
	EngineType   uint8
	EngineID     uint8
	// SamplingMode and SamplingInterval are the two halves of the 16-bit header
	// word: the top 2 bits are the mode (0 none, 1 deterministic, 2 random) and
	// only the low 14 bits are the 1-in-N interval. Reading the whole word as the
	// interval turned "deterministic 1-in-100" (0x4064) into 16484 and inflated
	// sampled traffic ~165x (X-02).
	SamplingMode     uint8
	SamplingInterval uint16
	Records          []Record
}

// ObservationDomain is the v5 equivalent of a v9 source ID / IPFIX domain.
func (p *V5Packet) ObservationDomain() uint32 {
	return uint32(p.EngineType)<<8 | uint32(p.EngineID)
}

// ExportTime is the exporter wall-clock time stamped in the header.
func (p *V5Packet) ExportTime() time.Time {
	if p.UnixSecs == 0 {
		return time.Time{}
	}
	return time.Unix(int64(p.UnixSecs), int64(p.UnixNsecs)).UTC()
}

func ParseV5(data []byte, exporter netip.Addr, collectorID string, receivedAt time.Time) (*V5Packet, error) {
	if len(data) < V5HeaderLen {
		return nil, fmt.Errorf("netflow v5 packet too short: %d bytes", len(data))
	}
	version := binary.BigEndian.Uint16(data[0:2])
	if version != 5 {
		return nil, fmt.Errorf("unsupported netflow version %d", version)
	}
	count := binary.BigEndian.Uint16(data[2:4])
	if count > 30 {
		return nil, fmt.Errorf("invalid netflow v5 record count %d", count)
	}
	need := V5HeaderLen + int(count)*V5RecordLen
	if len(data) < need {
		return nil, fmt.Errorf("netflow v5 packet truncated: need %d bytes, got %d", need, len(data))
	}

	sampling := binary.BigEndian.Uint16(data[22:24])
	pkt := &V5Packet{
		Version:          version,
		Count:            count,
		SysUptimeMS:      binary.BigEndian.Uint32(data[4:8]),
		UnixSecs:         binary.BigEndian.Uint32(data[8:12]),
		UnixNsecs:        binary.BigEndian.Uint32(data[12:16]),
		FlowSequence:     binary.BigEndian.Uint32(data[16:20]),
		EngineType:       data[20],
		EngineID:         data[21],
		SamplingMode:     uint8(sampling >> 14),
		SamplingInterval: sampling & 0x3FFF,
		Records:          make([]Record, 0, count),
	}
	interval := uint32(pkt.SamplingInterval)
	if interval == 0 {
		interval = 1 // 0 means "not sampled" in the v5 header
	}

	exporter = CanonicalAddr(exporter)
	exporterIP := ExporterStorageIP(exporter)
	domain := pkt.ObservationDomain()
	exportTime := pkt.ExportTime()
	for i := 0; i < int(count); i++ {
		off := V5HeaderLen + i*V5RecordLen
		first := binary.BigEndian.Uint32(data[off+24 : off+28])
		last := binary.BigEndian.Uint32(data[off+28 : off+32])
		pkt.Records = append(pkt.Records, Record{
			Timestamp:         uptimeTimestamp(exportTime, pkt.SysUptimeMS, last, receivedAt),
			ReceivedAt:        receivedAt.UTC(),
			CollectorID:       collectorID,
			ExporterIP:        exporterIP,
			ExporterAddr:      exporter,
			FlowVersion:       5,
			FlowSequence:      pkt.FlowSequence,
			EngineType:        pkt.EngineType,
			EngineID:          pkt.EngineID,
			SamplingInterval:  interval,
			SrcAddr:           ipv4(data[off : off+4]),
			DstAddr:           ipv4(data[off+4 : off+8]),
			NextHop:           ipv4(data[off+8 : off+12]),
			InputSNMP:         uint32(binary.BigEndian.Uint16(data[off+12 : off+14])),
			OutputSNMP:        uint32(binary.BigEndian.Uint16(data[off+14 : off+16])),
			Packets:           uint64(binary.BigEndian.Uint32(data[off+16 : off+20])),
			Bytes:             uint64(binary.BigEndian.Uint32(data[off+20 : off+24])),
			FirstSwitchedMS:   uint64(first),
			LastSwitchedMS:    uint64(last),
			SrcPort:           binary.BigEndian.Uint16(data[off+32 : off+34]),
			DstPort:           binary.BigEndian.Uint16(data[off+34 : off+36]),
			TCPFlags:          data[off+37],
			Protocol:          data[off+38],
			TOS:               data[off+39],
			SrcAS:             uint32(binary.BigEndian.Uint16(data[off+40 : off+42])),
			DstAS:             uint32(binary.BigEndian.Uint16(data[off+42 : off+44])),
			SrcMask:           data[off+44],
			DstMask:           data[off+45],
			IPVersion:         4,
			FieldsPresent:     FieldsV5,
			ObservationDomain: domain,
			FlowDirection:     DirectionUnknown,
			ExportProtocol:    ProtocolNetFlow5,
		})
	}
	return pkt, nil
}

func ipv4(b []byte) net.IP {
	return net.IPv4(b[0], b[1], b[2], b[3]).To4()
}

// maxUptimeAge bounds how far before the export header a sysUptime-relative
// flow time may lie. The subtraction is done modulo 2^32 so a flow that ended
// just before the exporter's 49.7-day uptime counter wrapped still resolves
// correctly; anything older than a day is not a real flow-cache entry.
const maxUptimeAge = 24 * time.Hour

// uptimeTimestamp converts a sysUptime-relative flow time (v5 First/Last,
// v9 IE 21/22) to wall-clock time using the export header: the flow ended
// (sysUptime - flowUptime) ms before the exporter stamped the datagram. When
// the header carries no wall clock, or the flow time is ahead of the header
// uptime (inconsistent exporter), the receipt time is the only honest value.
func uptimeTimestamp(exportTime time.Time, sysUptimeMS, flowUptimeMS uint32, receivedAt time.Time) time.Time {
	if exportTime.IsZero() || sysUptimeMS == 0 {
		return receivedAt.UTC()
	}
	age := time.Duration(sysUptimeMS-flowUptimeMS) * time.Millisecond // uint32 arithmetic: wrap-safe
	if age > maxUptimeAge {
		return receivedAt.UTC()
	}
	return exportTime.Add(-age).UTC()
}
