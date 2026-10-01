package netflow

import (
	"encoding/binary"
	"fmt"
	"net/netip"
	"time"
)

const IPFIXHeaderLen = 16

// ParseIPFIX decodes an IPFIX (NetFlow v10, RFC 7011) message. Templates,
// samplers and field decoding are shared with v9: IPFIX information element
// IDs match the v9 field-type numbers for the common flow IEs. Enterprise-
// specific and variable-length fields are parsed for correct alignment.
func ParseIPFIX(data []byte, exporter netip.Addr, collectorID string, receivedAt time.Time, cache *V9TemplateCache) ([]Record, V9Stats, error) {
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
	exporter = CanonicalAddr(exporter)
	exportSecs := binary.BigEndian.Uint32(data[4:8])
	stats.Sequence = binary.BigEndian.Uint32(data[8:12])
	stats.Domain = binary.BigEndian.Uint32(data[12:16])
	if exportSecs != 0 {
		stats.ExportTime = time.Unix(int64(exportSecs), 0).UTC()
	}
	ctx := &decodeCtx{
		exporter:    exporter,
		exporterIP:  ExporterStorageIP(exporter),
		domain:      stats.Domain,
		collectorID: collectorID,
		receivedAt:  receivedAt,
		exportTime:  stats.ExportTime,
		sequence:    stats.Sequence,
		version:     10,
		protocol:    ProtocolIPFIX,
		cache:       cache,
	}
	ctx.sysInitMS, ctx.hasSysInit = cache.InitTime(exporter, stats.Domain)

	var records []Record
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
			parseIPFIXTemplates(body, ctx, &stats)
		case setID == 3: // Options Template Set
			parseIPFIXOptionsTemplates(body, ctx, &stats)
		case setID >= 256: // Data Set (set ID == template ID)
			if tpl, ok := cache.Get(exporter, ctx.domain, setID); ok {
				decoded, seen, unsupported := decodeDataSet(body, tpl, ctx)
				stats.RecordsDecoded += len(decoded)
				stats.RecordsUnsupported += unsupported
				stats.DataRecords = addKnown(stats.DataRecords, seen)
				records = append(records, decoded...)
				break
			}
			if otpl, ok := cache.GetOptions(exporter, ctx.domain, setID); ok {
				res := decodeOptionsSet(body, otpl, ctx)
				stats.OptionsRecords += res.records
				stats.SamplersLearned += res.samplers
				stats.AppNames = append(stats.AppNames, res.apps...)
				stats.DataRecords = addKnown(stats.DataRecords, res.records)
				break
			}
			stats.DataSetsWaiting++
			stats.DataRecords = -1 // how many records the set held is unknowable
		}
		off = end
	}
	return records, stats, nil
}

// addKnown adds n to a running record count unless the count is already
// unknown (-1).
func addKnown(total, n int) int {
	if total < 0 {
		return total
	}
	return total + n
}

// parseIPFIXFieldSpecifiers reads fieldCount field specifiers starting at off.
// Returns the fields, the new offset, the summed fixed length (variable-length
// fields contribute 0), and ok=false on truncation.
func parseIPFIXFieldSpecifiers(body []byte, off, fieldCount int) ([]V9TemplateField, int, int, bool) {
	// The capacity is bounded by what the set can hold (4 bytes per
	// specifier), never taken from the field count alone: a forged count of
	// 65535 in every 8-byte template record would otherwise allocate 512 KB
	// per record, gigabytes per datagram.
	fields := make([]V9TemplateField, 0, min(fieldCount, max(len(body)-off, 0)/4))
	total := 0
	for i := 0; i < fieldCount; i++ {
		if off+4 > len(body) {
			return fields, off, total, false
		}
		f := V9TemplateField{Type: binary.BigEndian.Uint16(body[off : off+2]), Length: binary.BigEndian.Uint16(body[off+2 : off+4])}
		off += 4
		if f.Type&0x8000 != 0 { // enterprise-specific IE: 4-byte enterprise number follows
			if off+4 > len(body) {
				return fields, off, total, false
			}
			f.Enterprise = binary.BigEndian.Uint32(body[off : off+4])
			off += 4
		}
		fields = append(fields, f)
		if f.Length != 0xFFFF {
			total += int(f.Length)
		}
	}
	return fields, off, total, true
}

// parseIPFIXTemplates handles Template Set records, including withdrawals
// (RFC 7011 §8.1: field count 0 withdraws that template ID; template ID 2 with
// field count 0 withdraws every data template of the domain).
func parseIPFIXTemplates(body []byte, ctx *decodeCtx, stats *V9Stats) {
	for off := 0; off+4 <= len(body); {
		templateID := binary.BigEndian.Uint16(body[off : off+2])
		fieldCount := int(binary.BigEndian.Uint16(body[off+2 : off+4]))
		off += 4
		if fieldCount == 0 {
			switch {
			case templateID == 0:
				return // padding
			case templateID == 2:
				ctx.cache.WithdrawAll(ctx.exporter, ctx.domain, false)
			case templateID >= 256:
				ctx.cache.Withdraw(ctx.exporter, ctx.domain, templateID)
			}
			continue
		}
		fields, newOff, total, ok := parseIPFIXFieldSpecifiers(body, off, fieldCount)
		if !ok {
			return
		}
		off = newOff
		if templateID < 256 || !decodable(fields) {
			continue
		}
		stats.TemplatesUpdated++
		register, stored := ctx.cache.Put(ctx.exporter, ctx.domain, V9Template{ID: templateID, Fields: fields, Length: total})
		if !stored {
			stats.TemplatesRefused++
		}
		if register {
			stats.TemplatesChanged++
			stats.Templates = append(stats.Templates, TemplateInfo{
				Exporter: ctx.exporter, Domain: ctx.domain, Protocol: ctx.protocol, ID: templateID, Fields: fields,
			})
		}
	}
}

// parseIPFIXOptionsTemplates handles Options Template Set records:
// templateID(2) | fieldCount(2) | scopeFieldCount(2) | specifiers..., or a
// 4-byte withdrawal (field count 0; template ID 3 withdraws all options templates).
func parseIPFIXOptionsTemplates(body []byte, ctx *decodeCtx, stats *V9Stats) {
	for off := 0; off+4 <= len(body); {
		templateID := binary.BigEndian.Uint16(body[off : off+2])
		fieldCount := int(binary.BigEndian.Uint16(body[off+2 : off+4]))
		if fieldCount == 0 {
			off += 4
			switch {
			case templateID == 0:
				return // padding
			case templateID == 3:
				ctx.cache.WithdrawAll(ctx.exporter, ctx.domain, true)
			case templateID >= 256:
				ctx.cache.Withdraw(ctx.exporter, ctx.domain, templateID)
			}
			continue
		}
		if off+6 > len(body) {
			return
		}
		scopeCount := int(binary.BigEndian.Uint16(body[off+4 : off+6]))
		off += 6
		if scopeCount == 0 || scopeCount > fieldCount {
			return
		}
		fields, newOff, total, ok := parseIPFIXFieldSpecifiers(body, off, fieldCount)
		if !ok {
			return
		}
		off = newOff
		if templateID < 256 || !decodable(fields) {
			continue
		}
		tpl := V9OptionsTemplate{
			ID:          templateID,
			ScopeFields: fields[:scopeCount],
			Fields:      fields[scopeCount:],
			Length:      total,
		}
		stats.OptionsTemplatesUpdated++
		register, stored := ctx.cache.PutOptions(ctx.exporter, ctx.domain, tpl)
		if !stored {
			stats.TemplatesRefused++
		}
		if register {
			stats.TemplatesChanged++
			stats.Templates = append(stats.Templates, optionsTemplateInfo(ctx, tpl))
		}
	}
}
