package netflow

import (
	"encoding/binary"
	"fmt"
	"net"
	"time"
)

const IPFIXHeaderLen = 16

// ParseIPFIX decodes an IPFIX (NetFlow v10, RFC 7011) message. Templates and the
// sampling interval are cached exactly like v9, and field decoding reuses
// assignV9Field because IPFIX Information Element IDs match the v9 field-type
// numbers for the common flow IEs (octets=1, packets=2, protocol=4, ports 7/11,
// IPv4 addrs 8/12, interfaces 10/14, …). Enterprise-specific IEs and
// variable-length fields are parsed for correct alignment but not assigned.
func ParseIPFIX(data []byte, exporter net.IP, collectorID string, receivedAt time.Time, cache *V9TemplateCache) ([]Record, V9Stats, error) {
	var stats V9Stats
	if len(data) < IPFIXHeaderLen {
		return nil, stats, fmt.Errorf("ipfix packet too short: %d bytes", len(data))
	}
	if binary.BigEndian.Uint16(data[0:2]) != 10 {
		return nil, stats, fmt.Errorf("not an ipfix packet")
	}
	msgLen := int(binary.BigEndian.Uint16(data[2:4]))
	if msgLen <= 0 || msgLen > len(data) {
		msgLen = len(data)
	}
	exportSecs := binary.BigEndian.Uint32(data[4:8])
	sequence := binary.BigEndian.Uint32(data[8:12])
	domainID := binary.BigEndian.Uint32(data[12:16])

	exporter4 := exporter.To4()
	if exporter4 == nil {
		exporter4 = net.IPv4(0, 0, 0, 0).To4()
	}

	records := make([]Record, 0)
	for off := IPFIXHeaderLen; off+4 <= msgLen; {
		setID := binary.BigEndian.Uint16(data[off : off+2])
		setLen := int(binary.BigEndian.Uint16(data[off+2 : off+4]))
		if setLen < 4 {
			return records, stats, fmt.Errorf("invalid ipfix set length %d", setLen)
		}
		end := off + setLen
		if end > msgLen {
			return records, stats, fmt.Errorf("truncated ipfix set: need %d got %d", end, msgLen)
		}
		body := data[off+4 : end]

		switch {
		case setID == 2: // Template Set
			stats.TemplatesUpdated += parseIPFIXTemplates(body, exporter4, domainID, cache)
		case setID == 3: // Options Template Set
			stats.OptionsTemplatesUpdated += parseIPFIXOptionsTemplates(body, exporter4, domainID, cache)
		case setID >= 256: // Data Set (setID == template ID)
			if tpl, ok := cache.Get(exporter4, domainID, setID); ok {
				sampler := cache.GetSampler(exporter4, domainID)
				decoded := parseIPFIXDataSet(body, tpl, exporter4, collectorID, receivedAt, exportSecs, sequence, sampler)
				stats.RecordsDecoded += len(decoded)
				records = append(records, decoded...)
				break
			}
			if otpl, ok := cache.GetOptions(exporter4, domainID, setID); ok {
				// Options data: learn the sampler (BUG-05), then skip — never a flow.
				if iv, found := extractSampler(body, otpl); found {
					cache.PutSampler(exporter4, domainID, iv)
					stats.SamplersLearned++
				}
				stats.OptionsDataSkipped++
				break
			}
			stats.DataSetsWaiting++
		}
		off = end
	}
	return records, stats, nil
}

// parseIPFIXFieldSpecifiers reads fieldCount field specifiers starting at off.
// Returns the fields, the new offset, the summed fixed length (variable-length
// fields contribute 0), and ok=false on truncation.
func parseIPFIXFieldSpecifiers(body []byte, off, fieldCount int) ([]V9TemplateField, int, int, bool) {
	fields := make([]V9TemplateField, 0, fieldCount)
	total := 0
	for i := 0; i < fieldCount; i++ {
		if off+4 > len(body) {
			return fields, off, total, false
		}
		ieID := binary.BigEndian.Uint16(body[off : off+2])
		flen := binary.BigEndian.Uint16(body[off+2 : off+4])
		off += 4
		if ieID&0x8000 != 0 { // enterprise-specific IE: 4-byte enterprise number follows
			if off+4 > len(body) {
				return fields, off, total, false
			}
			off += 4
		}
		// Keep the enterprise bit set so the IE never collides with a standard
		// field type in assignV9Field — its value is skipped, alignment preserved.
		fields = append(fields, V9TemplateField{Type: ieID, Length: flen})
		if flen != 0xFFFF {
			total += int(flen)
		}
	}
	return fields, off, total, true
}

func parseIPFIXTemplates(body []byte, exporter net.IP, domainID uint32, cache *V9TemplateCache) int {
	count := 0
	for off := 0; off+4 <= len(body); {
		templateID := binary.BigEndian.Uint16(body[off : off+2])
		fieldCount := int(binary.BigEndian.Uint16(body[off+2 : off+4]))
		off += 4
		if templateID == 0 && fieldCount == 0 {
			break // padding
		}
		fields, newOff, total, ok := parseIPFIXFieldSpecifiers(body, off, fieldCount)
		if !ok {
			break
		}
		off = newOff
		if len(fields) > 0 {
			cache.Put(exporter, domainID, V9Template{ID: templateID, Fields: fields, Length: total})
			count++
		}
	}
	return count
}

func parseIPFIXOptionsTemplates(body []byte, exporter net.IP, domainID uint32, cache *V9TemplateCache) int {
	count := 0
	for off := 0; off+6 <= len(body); {
		templateID := binary.BigEndian.Uint16(body[off : off+2])
		fieldCount := int(binary.BigEndian.Uint16(body[off+2 : off+4]))
		scopeCount := int(binary.BigEndian.Uint16(body[off+4 : off+6]))
		off += 6
		if templateID == 0 && fieldCount == 0 {
			break
		}
		if scopeCount == 0 || scopeCount > fieldCount {
			break
		}
		fields, newOff, total, ok := parseIPFIXFieldSpecifiers(body, off, fieldCount)
		if !ok {
			break
		}
		off = newOff
		tpl := V9OptionsTemplate{
			ID:          templateID,
			ScopeFields: fields[:scopeCount],
			Fields:      fields[scopeCount:],
			Length:      total,
		}
		cache.PutOptions(exporter, domainID, tpl)
		count++
	}
	return count
}

func parseIPFIXDataSet(body []byte, tpl V9Template, exporter net.IP, collectorID string, receivedAt time.Time, exportSecs uint32, sequence uint32, sampler uint32) []Record {
	if len(tpl.Fields) == 0 {
		return nil
	}
	if sampler == 0 {
		sampler = 1
	}
	records := make([]Record, 0)
	for off := 0; off < len(body); {
		r := Record{
			Timestamp:        receivedAt.UTC(),
			ReceivedAt:       receivedAt.UTC(),
			CollectorID:      collectorID,
			ExporterIP:       exporter,
			FlowVersion:      10,
			FlowSequence:     sequence,
			SamplingInterval: sampler,
		}
		pos := off
		var firstMS, lastMS uint32
		var firstAbsMS, lastAbsMS uint64
		ok := true
		for _, f := range tpl.Fields {
			flen := int(f.Length)
			if f.Length == 0xFFFF { // RFC 7011 variable-length encoding
				if pos >= len(body) {
					ok = false
					break
				}
				flen = int(body[pos])
				pos++
				if flen == 255 {
					if pos+2 > len(body) {
						ok = false
						break
					}
					flen = int(binary.BigEndian.Uint16(body[pos : pos+2]))
					pos += 2
				}
			}
			if pos+flen > len(body) {
				ok = false
				break
			}
			assignV9Field(&r, f.Type, body[pos:pos+flen], &firstMS, &lastMS, &firstAbsMS, &lastAbsMS)
			pos += flen
		}
		if !ok || pos == off {
			break // trailing padding / truncated record
		}
		applyFlowTimes(&r, firstMS, lastMS, firstAbsMS, lastAbsMS, exportSecs, 0, receivedAt)
		fillMissingAddrs(&r)
		records = append(records, r)
		off = pos
	}
	return records
}
