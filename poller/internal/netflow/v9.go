package netflow

import (
	"encoding/binary"
	"fmt"
	"net"
	"time"
)

const V9HeaderLen = 20

type V9TemplateField struct {
	Type   uint16
	Length uint16
}

type V9Template struct {
	ID     uint16
	Fields []V9TemplateField
	Length int
}

type V9TemplateCache struct {
	templates map[string]V9Template
}

func NewV9TemplateCache() *V9TemplateCache {
	return &V9TemplateCache{templates: make(map[string]V9Template)}
}

func (c *V9TemplateCache) Get(exporter net.IP, sourceID uint32, templateID uint16) (V9Template, bool) {
	t, ok := c.templates[v9TemplateKey(exporter, sourceID, templateID)]
	return t, ok
}

func (c *V9TemplateCache) Put(exporter net.IP, sourceID uint32, template V9Template) {
	c.templates[v9TemplateKey(exporter, sourceID, template.ID)] = template
}

func v9TemplateKey(exporter net.IP, sourceID uint32, templateID uint16) string {
	return fmt.Sprintf("%s/%d/%d", exporter.String(), sourceID, templateID)
}

type V9Stats struct {
	TemplatesUpdated int
	RecordsDecoded   int
	DataSetsWaiting  int
}

func ParseV9(data []byte, exporter net.IP, collectorID string, receivedAt time.Time, cache *V9TemplateCache) ([]Record, V9Stats, error) {
	var stats V9Stats
	if len(data) < V9HeaderLen {
		return nil, stats, fmt.Errorf("netflow v9 packet too short: %d bytes", len(data))
	}
	version := binary.BigEndian.Uint16(data[0:2])
	if version != 9 {
		return nil, stats, fmt.Errorf("unsupported netflow version %d", version)
	}
	sysUptimeMS := binary.BigEndian.Uint32(data[4:8])
	unixSecs := binary.BigEndian.Uint32(data[8:12])
	sequence := binary.BigEndian.Uint32(data[12:16])
	sourceID := binary.BigEndian.Uint32(data[16:20])

	exporter4 := exporter.To4()
	if exporter4 == nil {
		exporter4 = net.IPv4(0, 0, 0, 0).To4()
	}

	records := make([]Record, 0)
	for off := V9HeaderLen; off+4 <= len(data); {
		flowSetID := binary.BigEndian.Uint16(data[off : off+2])
		length := int(binary.BigEndian.Uint16(data[off+2 : off+4]))
		if length < 4 {
			return records, stats, fmt.Errorf("invalid netflow v9 flowset length %d", length)
		}
		end := off + length
		if end > len(data) {
			return records, stats, fmt.Errorf("truncated netflow v9 flowset: need %d got %d", end, len(data))
		}
		body := data[off+4 : end]

		switch {
		case flowSetID == 0:
			n, err := parseV9Templates(body, exporter4, sourceID, cache)
			if err != nil {
				return records, stats, err
			}
			stats.TemplatesUpdated += n
		case flowSetID == 1:
			// Options templates are useful for sampling metadata, but not required
			// for first traffic visibility. Ignore for now.
		case flowSetID >= 256:
			template, ok := cache.Get(exporter4, sourceID, flowSetID)
			if !ok {
				decoded := parseV9CiscoLike55DataSet(body, exporter4, collectorID, receivedAt, sysUptimeMS, unixSecs, sequence)
				if len(decoded) > 0 {
					stats.RecordsDecoded += len(decoded)
					records = append(records, decoded...)
				} else {
					stats.DataSetsWaiting++
				}
				break
			}
			decoded := parseV9DataSet(body, template, exporter4, collectorID, receivedAt, sysUptimeMS, unixSecs, sequence)
			stats.RecordsDecoded += len(decoded)
			records = append(records, decoded...)
		}

		off = end
	}
	return records, stats, nil
}

func parseV9Templates(body []byte, exporter net.IP, sourceID uint32, cache *V9TemplateCache) (int, error) {
	count := 0
	for off := 0; off+4 <= len(body); {
		templateID := binary.BigEndian.Uint16(body[off : off+2])
		fieldCount := int(binary.BigEndian.Uint16(body[off+2 : off+4]))
		off += 4
		if templateID == 0 && fieldCount == 0 {
			break
		}
		if fieldCount <= 0 || off+fieldCount*4 > len(body) {
			break
		}
		tpl := V9Template{ID: templateID, Fields: make([]V9TemplateField, 0, fieldCount)}
		for i := 0; i < fieldCount; i++ {
			fieldType := binary.BigEndian.Uint16(body[off : off+2])
			fieldLen := binary.BigEndian.Uint16(body[off+2 : off+4])
			off += 4
			tpl.Fields = append(tpl.Fields, V9TemplateField{Type: fieldType, Length: fieldLen})
			tpl.Length += int(fieldLen)
		}
		if tpl.Length > 0 {
			cache.Put(exporter, sourceID, tpl)
			count++
		}
	}
	return count, nil
}

func parseV9DataSet(body []byte, template V9Template, exporter net.IP, collectorID string, receivedAt time.Time, sysUptimeMS uint32, unixSecs uint32, sequence uint32) []Record {
	if template.Length <= 0 {
		return nil
	}
	records := make([]Record, 0, len(body)/template.Length)
	for off := 0; off+template.Length <= len(body); off += template.Length {
		row := body[off : off+template.Length]
		r := Record{
			Timestamp:       receivedAt.UTC(),
			ReceivedAt:      receivedAt.UTC(),
			CollectorID:     collectorID,
			ExporterIP:      exporter,
			FlowVersion:     9,
			FlowSequence:    sequence,
			SamplingInterval: 1,
		}
		pos := 0
		var firstMS, lastMS uint32
		for _, field := range template.Fields {
			if pos+int(field.Length) > len(row) {
				break
			}
			value := row[pos : pos+int(field.Length)]
			assignV9Field(&r, field.Type, value, &firstMS, &lastMS)
			pos += int(field.Length)
		}
		if lastMS > 0 {
			r.Timestamp = flowTimestamp(unixSecs, sysUptimeMS, lastMS, receivedAt)
			r.LastSwitchedMS = uint64(lastMS)
		}
		if firstMS > 0 {
			r.FirstSwitchedMS = uint64(firstMS)
		}
		if r.SrcAddr == nil {
			r.SrcAddr = net.IPv4(0, 0, 0, 0).To4()
		}
		if r.DstAddr == nil {
			r.DstAddr = net.IPv4(0, 0, 0, 0).To4()
		}
		if r.NextHop == nil {
			r.NextHop = net.IPv4(0, 0, 0, 0).To4()
		}
		records = append(records, r)
	}
	return records
}

// parseV9CiscoLike55DataSet handles the common 55-byte NetFlow v9 export
// layout observed on Cisco-compatible routers when data sets arrive before the
// template refresh. Standards-compliant templates still take precedence.
func parseV9CiscoLike55DataSet(body []byte, exporter net.IP, collectorID string, receivedAt time.Time, sysUptimeMS uint32, unixSecs uint32, sequence uint32) []Record {
	const recordLen = 55
	if len(body) < recordLen {
		return nil
	}
	records := make([]Record, 0, len(body)/recordLen)
	for off := 0; off+recordLen <= len(body); off += recordLen {
		row := body[off : off+recordLen]
		lastMS := binary.BigEndian.Uint32(row[35:39])
		firstMS := binary.BigEndian.Uint32(row[39:43])
		r := Record{
			Timestamp:        flowTimestamp(unixSecs, sysUptimeMS, lastMS, receivedAt),
			ReceivedAt:       receivedAt.UTC(),
			CollectorID:      collectorID,
			ExporterIP:       exporter,
			FlowVersion:      9,
			FlowSequence:     sequence,
			SamplingInterval: 1,
			Bytes:            binary.BigEndian.Uint64(row[0:8]),
			Packets:          uint64(binary.BigEndian.Uint32(row[8:12])),
			Protocol:         row[12],
			TOS:              row[13],
			TCPFlags:         row[14],
			SrcPort:          binary.BigEndian.Uint16(row[15:17]),
			SrcAddr:          ipv4(row[17:21]),
			InputSNMP:        uint16(binary.BigEndian.Uint32(row[21:25])),
			DstPort:          binary.BigEndian.Uint16(row[25:27]),
			DstAddr:          ipv4(row[27:31]),
			OutputSNMP:       uint16(binary.BigEndian.Uint32(row[31:35])),
			LastSwitchedMS:   uint64(lastMS),
			FirstSwitchedMS:  uint64(firstMS),
			SrcAS:            binary.BigEndian.Uint32(row[43:47]),
			DstAS:            binary.BigEndian.Uint32(row[47:51]),
			NextHop:          ipv4(row[51:55]),
		}
		if r.Bytes == 0 && r.Packets == 0 && r.SrcAddr.Equal(net.IPv4(0, 0, 0, 0).To4()) && r.DstAddr.Equal(net.IPv4(0, 0, 0, 0).To4()) {
			continue
		}
		records = append(records, r)
	}
	return records
}

func assignV9Field(r *Record, fieldType uint16, value []byte, firstMS *uint32, lastMS *uint32) {
	switch fieldType {
	case 1: // IN_BYTES
		r.Bytes = uint64BE(value)
	case 2: // IN_PKTS
		r.Packets = uint64BE(value)
	case 4: // PROTOCOL
		r.Protocol = uint8(uint64BE(value))
	case 5: // SRC_TOS
		r.TOS = uint8(uint64BE(value))
	case 6: // TCP_FLAGS
		r.TCPFlags = uint8(uint64BE(value))
	case 7: // L4_SRC_PORT
		r.SrcPort = uint16(uint64BE(value))
	case 8: // IPV4_SRC_ADDR
		if len(value) == 4 {
			r.SrcAddr = ipv4(value)
		}
	case 9: // SRC_MASK
		r.SrcMask = uint8(uint64BE(value))
	case 10: // INPUT_SNMP
		r.InputSNMP = uint16(uint64BE(value))
	case 11: // L4_DST_PORT
		r.DstPort = uint16(uint64BE(value))
	case 12: // IPV4_DST_ADDR
		if len(value) == 4 {
			r.DstAddr = ipv4(value)
		}
	case 13: // DST_MASK
		r.DstMask = uint8(uint64BE(value))
	case 14: // OUTPUT_SNMP
		r.OutputSNMP = uint16(uint64BE(value))
	case 15: // IPV4_NEXT_HOP
		if len(value) == 4 {
			r.NextHop = ipv4(value)
		}
	case 16: // SRC_AS
		r.SrcAS = uint32(uint64BE(value))
	case 17: // DST_AS
		r.DstAS = uint32(uint64BE(value))
	case 21: // LAST_SWITCHED
		*lastMS = uint32(uint64BE(value))
	case 22: // FIRST_SWITCHED
		*firstMS = uint32(uint64BE(value))
	}
}

func uint64BE(value []byte) uint64 {
	var out uint64
	for _, b := range value {
		out = (out << 8) | uint64(b)
	}
	return out
}
