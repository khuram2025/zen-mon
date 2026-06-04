package netflow

import (
	"encoding/binary"
	"fmt"
	"net"
	"sync"
	"time"
)

const V9HeaderLen = 20

// v9TemplateTTL bounds how long a learned template is trusted without being
// re-advertised. Routers re-send templates periodically; a generous TTL avoids
// dropping data between refreshes while still expiring templates from exporters
// that have gone away (BUG-15).
const v9TemplateTTL = 24 * time.Hour

type V9TemplateField struct {
	Type   uint16
	Length uint16
}

type V9Template struct {
	ID     uint16
	Fields []V9TemplateField
	Length int
}

// V9OptionsTemplate describes a NetFlow v9 Options Template (FlowSet ID 1).
// Its data records carry exporter metadata (e.g. the sampler / sampling
// interval), not traffic flows, so they must never be decoded onto the flow
// path. We cache them only so their data records can be recognised and skipped.
type V9OptionsTemplate struct {
	ID          uint16
	ScopeFields []V9TemplateField
	Fields      []V9TemplateField
	Length      int
}

type v9CachedTemplate struct {
	tpl       V9Template
	learnedAt time.Time
}

type v9CachedOptions struct {
	tpl       V9OptionsTemplate
	learnedAt time.Time
}

// V9TemplateCache is safe for concurrent use (BUG-15: the map was previously
// unguarded). It also expires templates after ttl and remembers the learned
// sampling interval per exporter+observation domain (BUG-05).
type V9TemplateCache struct {
	mu               sync.RWMutex
	ttl              time.Duration
	templates        map[string]v9CachedTemplate
	optionsTemplates map[string]v9CachedOptions
	samplers         map[string]uint32
}

func NewV9TemplateCache() *V9TemplateCache {
	return &V9TemplateCache{
		ttl:              v9TemplateTTL,
		templates:        make(map[string]v9CachedTemplate),
		optionsTemplates: make(map[string]v9CachedOptions),
		samplers:         make(map[string]uint32),
	}
}

func (c *V9TemplateCache) Get(exporter net.IP, sourceID uint32, templateID uint16) (V9Template, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	e, ok := c.templates[v9TemplateKey(exporter, sourceID, templateID)]
	if !ok || time.Since(e.learnedAt) > c.ttl {
		return V9Template{}, false
	}
	return e.tpl, true
}

func (c *V9TemplateCache) Put(exporter net.IP, sourceID uint32, template V9Template) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.templates[v9TemplateKey(exporter, sourceID, template.ID)] = v9CachedTemplate{tpl: template, learnedAt: time.Now()}
}

// GetOptions reports whether templateID refers to a known Options Template for
// this exporter/sourceID (i.e. its data records are metadata, not flows).
func (c *V9TemplateCache) GetOptions(exporter net.IP, sourceID uint32, templateID uint16) (V9OptionsTemplate, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	e, ok := c.optionsTemplates[v9TemplateKey(exporter, sourceID, templateID)]
	if !ok || time.Since(e.learnedAt) > c.ttl {
		return V9OptionsTemplate{}, false
	}
	return e.tpl, true
}

func (c *V9TemplateCache) PutOptions(exporter net.IP, sourceID uint32, template V9OptionsTemplate) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.optionsTemplates[v9TemplateKey(exporter, sourceID, template.ID)] = v9CachedOptions{tpl: template, learnedAt: time.Now()}
}

// PutSampler/GetSampler store the learned 1-in-N sampling interval per
// exporter+observation domain (BUG-05). GetSampler returns 1 (unsampled) when
// nothing has been learned.
func (c *V9TemplateCache) PutSampler(exporter net.IP, sourceID uint32, interval uint32) {
	if interval == 0 {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.samplers[v9SamplerKey(exporter, sourceID)] = interval
}

func (c *V9TemplateCache) GetSampler(exporter net.IP, sourceID uint32) uint32 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	if v, ok := c.samplers[v9SamplerKey(exporter, sourceID)]; ok && v > 0 {
		return v
	}
	return 1
}

func v9TemplateKey(exporter net.IP, sourceID uint32, templateID uint16) string {
	return fmt.Sprintf("%s/%d/%d", exporter.String(), sourceID, templateID)
}

func v9SamplerKey(exporter net.IP, sourceID uint32) string {
	return fmt.Sprintf("%s/%d", exporter.String(), sourceID)
}

type V9Stats struct {
	TemplatesUpdated        int
	OptionsTemplatesUpdated int
	RecordsDecoded          int
	OptionsDataSkipped      int
	SamplersLearned         int
	DataSetsWaiting         int
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
			// Options Template: cache it so its data records (which carry exporter
			// metadata such as the sampling interval, NOT traffic) are recognised
			// and routed away from the flow path.
			stats.OptionsTemplatesUpdated += parseV9OptionsTemplate(body, exporter4, sourceID, cache)
		case flowSetID >= 256:
			if template, ok := cache.Get(exporter4, sourceID, flowSetID); ok {
				sampler := cache.GetSampler(exporter4, sourceID)
				decoded := parseV9DataSet(body, template, exporter4, collectorID, receivedAt, sysUptimeMS, unixSecs, sequence, sampler)
				stats.RecordsDecoded += len(decoded)
				records = append(records, decoded...)
				break
			}
			if otpl, ok := cache.GetOptions(exporter4, sourceID, flowSetID); ok {
				// Options data: metadata records, never traffic flows. Learn the
				// sampling interval if present (BUG-05), then skip — never feed to
				// the flow path. The old 55-byte fallback turned these into
				// exabyte-scale garbage byte counts that dominated every stat (BUG-01).
				if iv, found := extractSampler(body, otpl); found {
					cache.PutSampler(exporter4, sourceID, iv)
					stats.SamplersLearned++
				}
				stats.OptionsDataSkipped++
				break
			}
			// Data set with no template learned yet. Do NOT guess a layout; wait
			// for the exporter to (re)send the template — routers re-advertise
			// templates periodically, so this self-heals within seconds.
			stats.DataSetsWaiting++
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

// parseV9OptionsTemplate decodes one or more NetFlow v9 Options Template
// records (FlowSet ID 1, RFC 3954 §6.1) and caches them. Per-record layout:
//
//	templateID(2) | optionScopeLength(2) | optionLength(2) |
//	scope fields [type(2) len(2)]... | option fields [type(2) len(2)]...
//
// optionScopeLength / optionLength are byte counts of the two field sections.
// We keep the parsed fields for future sampler extraction (BUG-05); for now
// caching is enough to recognise — and skip — the matching data records.
func parseV9OptionsTemplate(body []byte, exporter net.IP, sourceID uint32, cache *V9TemplateCache) int {
	count := 0
	for off := 0; off+6 <= len(body); {
		templateID := binary.BigEndian.Uint16(body[off : off+2])
		scopeLen := int(binary.BigEndian.Uint16(body[off+2 : off+4]))
		optionLen := int(binary.BigEndian.Uint16(body[off+4 : off+6]))
		off += 6
		if templateID == 0 && scopeLen == 0 && optionLen == 0 {
			break // trailing padding
		}
		if scopeLen <= 0 || optionLen < 0 || scopeLen%4 != 0 || optionLen%4 != 0 {
			break
		}
		if off+scopeLen+optionLen > len(body) {
			break
		}
		tpl := V9OptionsTemplate{ID: templateID}
		for i := 0; i < scopeLen/4; i++ {
			ft := binary.BigEndian.Uint16(body[off : off+2])
			fl := binary.BigEndian.Uint16(body[off+2 : off+4])
			off += 4
			tpl.ScopeFields = append(tpl.ScopeFields, V9TemplateField{Type: ft, Length: fl})
			tpl.Length += int(fl)
		}
		for i := 0; i < optionLen/4; i++ {
			ft := binary.BigEndian.Uint16(body[off : off+2])
			fl := binary.BigEndian.Uint16(body[off+2 : off+4])
			off += 4
			tpl.Fields = append(tpl.Fields, V9TemplateField{Type: ft, Length: fl})
			tpl.Length += int(fl)
		}
		cache.PutOptions(exporter, sourceID, tpl)
		count++
	}
	return count
}

func parseV9DataSet(body []byte, template V9Template, exporter net.IP, collectorID string, receivedAt time.Time, sysUptimeMS uint32, unixSecs uint32, sequence uint32, sampler uint32) []Record {
	if template.Length <= 0 {
		return nil
	}
	if sampler == 0 {
		sampler = 1
	}
	records := make([]Record, 0, len(body)/template.Length)
	for off := 0; off+template.Length <= len(body); off += template.Length {
		row := body[off : off+template.Length]
		r := Record{
			Timestamp:        receivedAt.UTC(),
			ReceivedAt:       receivedAt.UTC(),
			CollectorID:      collectorID,
			ExporterIP:       exporter,
			FlowVersion:      9,
			FlowSequence:     sequence,
			SamplingInterval: sampler,
		}
		pos := 0
		var firstMS, lastMS uint32
		var firstAbsMS, lastAbsMS uint64
		for _, field := range template.Fields {
			if pos+int(field.Length) > len(row) {
				break
			}
			value := row[pos : pos+int(field.Length)]
			assignV9Field(&r, field.Type, value, &firstMS, &lastMS, &firstAbsMS, &lastAbsMS)
			pos += int(field.Length)
		}
		applyFlowTimes(&r, firstMS, lastMS, firstAbsMS, lastAbsMS, unixSecs, sysUptimeMS, receivedAt)
		fillMissingAddrs(&r)
		records = append(records, r)
	}
	return records
}

// applyFlowTimes resolves a record's timestamp from whichever flow-time fields
// the template provided: absolute epoch (fields 150–153) take precedence over
// sysUptime-relative LAST/FIRST_SWITCHED (21/22).
func applyFlowTimes(r *Record, firstMS, lastMS uint32, firstAbsMS, lastAbsMS uint64, unixSecs, sysUptimeMS uint32, receivedAt time.Time) {
	switch {
	case lastAbsMS > 0:
		r.Timestamp = time.UnixMilli(int64(lastAbsMS)).UTC()
		r.LastSwitchedMS = lastAbsMS
	case lastMS > 0:
		r.Timestamp = flowTimestamp(unixSecs, sysUptimeMS, lastMS, receivedAt)
		r.LastSwitchedMS = uint64(lastMS)
	}
	switch {
	case firstAbsMS > 0:
		r.FirstSwitchedMS = firstAbsMS
	case firstMS > 0:
		r.FirstSwitchedMS = uint64(firstMS)
	}
}

func fillMissingAddrs(r *Record) {
	if r.SrcAddr == nil {
		r.SrcAddr = net.IPv4(0, 0, 0, 0).To4()
	}
	if r.DstAddr == nil {
		r.DstAddr = net.IPv4(0, 0, 0, 0).To4()
	}
	if r.NextHop == nil {
		r.NextHop = net.IPv4(0, 0, 0, 0).To4()
	}
}

// isSamplerField reports whether a v9/IPFIX information element carries a packet
// sampling interval (1-in-N): SAMPLING_INTERVAL(34), FLOW_SAMPLER_RANDOM_INTERVAL(50),
// samplingPacketInterval(305).
func isSamplerField(t uint16) bool {
	return t == 34 || t == 50 || t == 305
}

// extractSampler walks one options-data record (scope values then option values,
// per the cached options template) and returns the first sampling interval it
// finds (BUG-05).
func extractSampler(body []byte, tpl V9OptionsTemplate) (uint32, bool) {
	if tpl.Length <= 0 || len(body) < tpl.Length {
		return 0, false
	}
	row := body[:tpl.Length]
	pos := 0
	fields := make([]V9TemplateField, 0, len(tpl.ScopeFields)+len(tpl.Fields))
	fields = append(fields, tpl.ScopeFields...)
	fields = append(fields, tpl.Fields...)
	for _, f := range fields {
		if pos+int(f.Length) > len(row) {
			break
		}
		if isSamplerField(f.Type) {
			if v := uint32(uint64BE(row[pos : pos+int(f.Length)])); v > 0 {
				return v, true
			}
		}
		pos += int(f.Length)
	}
	return 0, false
}

func assignV9Field(r *Record, fieldType uint16, value []byte, firstMS, lastMS *uint32, firstAbsMS, lastAbsMS *uint64) {
	switch fieldType {
	case 1: // IN_BYTES
		r.Bytes = uint64BE(value)
	case 2: // IN_PKTS
		r.Packets = uint64BE(value)
	case 23: // OUT_BYTES — egress-direction templates; use only if IN_BYTES absent (BUG-06)
		if r.Bytes == 0 {
			r.Bytes = uint64BE(value)
		}
	case 24: // OUT_PKTS
		if r.Packets == 0 {
			r.Packets = uint64BE(value)
		}
	case 85: // octetTotalCount — 64-bit byte counter (BUG-06)
		r.Bytes = uint64BE(value)
	case 86: // packetTotalCount — 64-bit packet counter (BUG-06)
		r.Packets = uint64BE(value)
	case 150: // flowStartSeconds (absolute epoch s)
		*firstAbsMS = uint64BE(value) * 1000
	case 151: // flowEndSeconds (absolute epoch s)
		*lastAbsMS = uint64BE(value) * 1000
	case 152: // flowStartMilliseconds (absolute epoch ms)
		*firstAbsMS = uint64BE(value)
	case 153: // flowEndMilliseconds (absolute epoch ms)
		*lastAbsMS = uint64BE(value)
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
