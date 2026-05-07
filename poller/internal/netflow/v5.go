package netflow

import (
	"encoding/binary"
	"fmt"
	"net"
	"time"
)

const (
	V5HeaderLen = 24
	V5RecordLen = 48
)

type Record struct {
	Timestamp         time.Time
	ReceivedAt        time.Time
	CollectorID       string
	ExporterIP        net.IP
	FlowVersion       uint8
	FlowSequence      uint32
	EngineType        uint8
	EngineID          uint8
	SamplingInterval  uint32
	SrcAddr           net.IP
	DstAddr           net.IP
	NextHop           net.IP
	InputSNMP         uint16
	OutputSNMP        uint16
	Packets           uint64
	Bytes             uint64
	FirstSwitchedMS   uint64
	LastSwitchedMS    uint64
	SrcPort           uint16
	DstPort           uint16
	TCPFlags          uint8
	Protocol          uint8
	TOS               uint8
	SrcAS             uint32
	DstAS             uint32
	SrcMask           uint8
	DstMask           uint8
}

type V5Packet struct {
	Version          uint16
	Count            uint16
	SysUptimeMS      uint32
	UnixSecs         uint32
	UnixNsecs        uint32
	FlowSequence     uint32
	EngineType       uint8
	EngineID         uint8
	SamplingInterval uint16
	Records          []Record
}

func ParseV5(data []byte, exporter net.IP, collectorID string, receivedAt time.Time) (*V5Packet, error) {
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

	pkt := &V5Packet{
		Version:          version,
		Count:            count,
		SysUptimeMS:      binary.BigEndian.Uint32(data[4:8]),
		UnixSecs:         binary.BigEndian.Uint32(data[8:12]),
		UnixNsecs:        binary.BigEndian.Uint32(data[12:16]),
		FlowSequence:     binary.BigEndian.Uint32(data[16:20]),
		EngineType:       data[20],
		EngineID:         data[21],
		SamplingInterval: binary.BigEndian.Uint16(data[22:24]),
		Records:          make([]Record, 0, count),
	}

	exporter4 := exporter.To4()
	if exporter4 == nil {
		exporter4 = net.IPv4(0, 0, 0, 0).To4()
	}

	for i := 0; i < int(count); i++ {
		off := V5HeaderLen + i*V5RecordLen
		first := binary.BigEndian.Uint32(data[off+24 : off+28])
		last := binary.BigEndian.Uint32(data[off+28 : off+32])
		ts := flowTimestamp(pkt.UnixSecs, pkt.SysUptimeMS, last, receivedAt)
		pkt.Records = append(pkt.Records, Record{
			Timestamp:        ts,
			ReceivedAt:       receivedAt,
			CollectorID:      collectorID,
			ExporterIP:       exporter4,
			FlowVersion:      5,
			FlowSequence:     pkt.FlowSequence,
			EngineType:       pkt.EngineType,
			EngineID:         pkt.EngineID,
			SamplingInterval: uint32(pkt.SamplingInterval),
			SrcAddr:          ipv4(data[off : off+4]),
			DstAddr:          ipv4(data[off+4 : off+8]),
			NextHop:          ipv4(data[off+8 : off+12]),
			InputSNMP:        binary.BigEndian.Uint16(data[off+12 : off+14]),
			OutputSNMP:       binary.BigEndian.Uint16(data[off+14 : off+16]),
			Packets:          uint64(binary.BigEndian.Uint32(data[off+16 : off+20])),
			Bytes:            uint64(binary.BigEndian.Uint32(data[off+20 : off+24])),
			FirstSwitchedMS:  uint64(first),
			LastSwitchedMS:   uint64(last),
			SrcPort:          binary.BigEndian.Uint16(data[off+32 : off+34]),
			DstPort:          binary.BigEndian.Uint16(data[off+34 : off+36]),
			TCPFlags:         data[off+37],
			Protocol:         data[off+38],
			TOS:              data[off+39],
			SrcAS:            uint32(binary.BigEndian.Uint16(data[off+40 : off+42])),
			DstAS:            uint32(binary.BigEndian.Uint16(data[off+42 : off+44])),
			SrcMask:          data[off+44],
			DstMask:          data[off+45],
		})
	}
	return pkt, nil
}

func ipv4(b []byte) net.IP {
	return net.IPv4(b[0], b[1], b[2], b[3]).To4()
}

func flowTimestamp(unixSecs uint32, sysUptimeMS uint32, flowUptimeMS uint32, fallback time.Time) time.Time {
	if unixSecs == 0 || sysUptimeMS == 0 || flowUptimeMS > sysUptimeMS {
		return fallback.UTC()
	}
	exporterNow := time.Unix(int64(unixSecs), 0).UTC()
	return exporterNow.Add(-time.Duration(sysUptimeMS-flowUptimeMS) * time.Millisecond)
}
