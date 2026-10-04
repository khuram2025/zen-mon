package netflow

import (
	"encoding/binary"
	"fmt"
	"net/netip"
	"time"
)

const V9HeaderLen = 20

// V9Stats describes one decoded v9 or IPFIX datagram for the collector's
// per-exporter statistics, sequence-gap accounting and template registry.
type V9Stats struct {
	Domain     uint32    // v9 source ID / IPFIX observation domain
	Sequence   uint32    // header sequence number as sent
	ExportTime time.Time // header wall clock (zero when the exporter sent 0)

	TemplatesUpdated        int // data template records parsed (learned or refreshed)
	OptionsTemplatesUpdated int // options template records parsed
	TemplatesChanged        int // templates that are new or whose layout changed
	TemplatesRefused        int // new templates refused by the cache bounds (their data waits)
	RecordsDecoded          int // flow records returned
	RecordsUnsupported      int // data records dropped: no source/destination address (NF-32)
	OptionsRecords          int // options data records read (samplers, init time, ...)
	SamplersLearned         int
	DataSetsWaiting         int // data sets skipped because their template is not known yet

	// AppNames are application definitions read from options data (NBAR2
	// application table / attributes), for the zenplus.flow_app_names registry.
	AppNames []AppName

	// DataRecords is the number of data records (flow + options) the datagram
	// carried, which IPFIX sequence numbers count. -1 when a data set could not
	// be decoded (template unknown), so its record count is unknown.
	DataRecords int

	// Templates lists templates to (re)write to the flow_templates registry:
	// new, changed, or due for the daily refresh.
	Templates []TemplateInfo
}

func ParseV9(data []byte, exporter netip.Addr, collectorID string, receivedAt time.Time, cache *V9TemplateCache) ([]Record, V9Stats, error) {
	var stats V9Stats
	if len(data) < V9HeaderLen {
		return nil, stats, fmt.Errorf("netflow v9 packet too short: %d bytes", len(data))
	}
	version := binary.BigEndian.Uint16(data[0:2])
	if version != 9 {
		return nil, stats, fmt.Errorf("unsupported netflow version %d", version)
	}
	exporter = CanonicalAddr(exporter)
	sysUptimeMS := binary.BigEndian.Uint32(data[4:8])
	unixSecs := binary.BigEndian.Uint32(data[8:12])
	stats.Sequence = binary.BigEndian.Uint32(data[12:16])
	stats.Domain = binary.BigEndian.Uint32(data[16:20])
	if unixSecs != 0 {
		stats.ExportTime = time.Unix(int64(unixSecs), 0).UTC()
	}
	ctx := &decodeCtx{
		exporter:    exporter,
		exporterIP:  ExporterStorageIP(exporter),
		domain:      stats.Domain,
		collectorID: collectorID,
		receivedAt:  receivedAt,
		exportTime:  stats.ExportTime,
		sysUptimeMS: sysUptimeMS,
		sequence:    stats.Sequence,
		version:     9,
		protocol:    ProtocolNetFlow9,
		cache:       cache,
	}

	var records []Record
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
			parseV9Templates(body, ctx, &stats)
		case flowSetID == 1:
			parseV9OptionsTemplates(body, ctx, &stats)
		case flowSetID >= 256:
			if tpl, ok := cache.Get(exporter, ctx.domain, flowSetID); ok {
				decoded, _, unsupported := decodeDataSet(body, tpl, ctx)
				stats.RecordsDecoded += len(decoded)
				stats.RecordsUnsupported += unsupported
				records = append(records, decoded...)
				break
			}
			if otpl, ok := cache.GetOptions(exporter, ctx.domain, flowSetID); ok {
				// Options data: exporter metadata, never traffic. The old 55-byte
				// fallback turned these into exabyte-scale flows (BUG-01).
				res := decodeOptionsSet(body, otpl, ctx)
				stats.OptionsRecords += res.records
				stats.SamplersLearned += res.samplers
				stats.AppNames = append(stats.AppNames, res.apps...)
				break
			}
			// No template yet: never guess a layout. Exporters re-advertise
			// templates periodically, so this self-heals; it is counted.
			stats.DataSetsWaiting++
		}
		off = end
	}
	return records, stats, nil
}

func parseV9Templates(body []byte, ctx *decodeCtx, stats *V9Stats) {
	for off := 0; off+4 <= len(body); {
		templateID := binary.BigEndian.Uint16(body[off : off+2])
		fieldCount := int(binary.BigEndian.Uint16(body[off+2 : off+4]))
		off += 4
		if templateID == 0 && fieldCount == 0 {
			return // padding
		}
		if fieldCount <= 0 || off+fieldCount*4 > len(body) {
			return
		}
		tpl := V9Template{ID: templateID, Fields: make([]V9TemplateField, 0, fieldCount)}
		for i := 0; i < fieldCount; i++ {
			f := V9TemplateField{Type: binary.BigEndian.Uint16(body[off : off+2]), Length: binary.BigEndian.Uint16(body[off+2 : off+4])}
			off += 4
			tpl.Fields = append(tpl.Fields, f)
			tpl.Length += int(f.Length)
		}
		// Data templates live in the ID space >= 256; a template without a
		// field of non-zero length cannot describe a record.
		if templateID < 256 || !decodable(tpl.Fields) {
			continue
		}
		stats.TemplatesUpdated++
		register, stored := ctx.cache.Put(ctx.exporter, ctx.domain, tpl)
		if !stored {
			stats.TemplatesRefused++
		}
		if register {
			stats.TemplatesChanged++
			stats.Templates = append(stats.Templates, TemplateInfo{
				Exporter: ctx.exporter, Domain: ctx.domain, Protocol: ctx.protocol, ID: templateID, Fields: tpl.Fields,
			})
		}
	}
}

// parseV9OptionsTemplates decodes NetFlow v9 Options Template records
// (FlowSet 1, RFC 3954 §6.1):
//
//	templateID(2) | optionScopeLength(2) | optionLength(2) |
//	scope fields [type(2) len(2)]... | option fields [type(2) len(2)]...
//
// optionScopeLength / optionLength are byte counts of the two sections.
func parseV9OptionsTemplates(body []byte, ctx *decodeCtx, stats *V9Stats) {
	for off := 0; off+6 <= len(body); {
		templateID := binary.BigEndian.Uint16(body[off : off+2])
		scopeLen := int(binary.BigEndian.Uint16(body[off+2 : off+4]))
		optionLen := int(binary.BigEndian.Uint16(body[off+4 : off+6]))
		off += 6
		if templateID == 0 && scopeLen == 0 && optionLen == 0 {
			return // trailing padding
		}
		if scopeLen <= 0 || optionLen < 0 || scopeLen%4 != 0 || optionLen%4 != 0 || off+scopeLen+optionLen > len(body) {
			return
		}
		tpl := V9OptionsTemplate{ID: templateID}
		for i := 0; i < scopeLen/4; i++ {
			f := V9TemplateField{Type: binary.BigEndian.Uint16(body[off : off+2]), Length: binary.BigEndian.Uint16(body[off+2 : off+4])}
			off += 4
			tpl.ScopeFields = append(tpl.ScopeFields, f)
			tpl.Length += int(f.Length)
		}
		for i := 0; i < optionLen/4; i++ {
			f := V9TemplateField{Type: binary.BigEndian.Uint16(body[off : off+2]), Length: binary.BigEndian.Uint16(body[off+2 : off+4])}
			off += 4
			tpl.Fields = append(tpl.Fields, f)
			tpl.Length += int(f.Length)
		}
		if templateID < 256 || !(decodable(tpl.ScopeFields) || decodable(tpl.Fields)) {
			continue
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

func optionsTemplateInfo(ctx *decodeCtx, tpl V9OptionsTemplate) TemplateInfo {
	fields := make([]V9TemplateField, 0, len(tpl.ScopeFields)+len(tpl.Fields))
	fields = append(fields, tpl.ScopeFields...)
	fields = append(fields, tpl.Fields...)
	return TemplateInfo{
		Exporter: ctx.exporter, Domain: ctx.domain, Protocol: ctx.protocol, ID: tpl.ID,
		IsOptions: true, ScopeCount: len(tpl.ScopeFields), Fields: fields,
	}
}
