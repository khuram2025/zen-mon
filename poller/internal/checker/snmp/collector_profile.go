package snmp

import (
	"context"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	g "github.com/gosnmp/gosnmp"
)

// Template-driven collection (device_profiles.oid_groups). Runs as one
// step inside Collector.Collect for devices whose profile declares OID
// groups. Scalar groups are batched GETs; table groups are per-column
// walks correlated by index suffix.

// maxTableRows bounds per-group row count so a huge table (e.g. a
// thousand F5 virtual servers) cannot blow up metric-key cardinality in
// ClickHouse or stall the per-device poll budget.
const maxTableRows = 500

// scalarGetChunk is how many OIDs we pack into one SNMP GET.
const scalarGetChunk = 16

type tplSnap struct {
	val uint64
	at  time.Time
}

// collectTemplateMetrics polls every OID group of the device's template.
// Returns ClickHouse samples, Postgres latest-values, and the keys of the
// groups that were polled to completion (used to purge stale rows).
func (c *Collector) collectTemplateMetrics(
	ctx context.Context, s *g.GoSNMP, d *Device, ts time.Time,
) ([]MetricSample, []TemplateValue, []string) {
	var samples []MetricSample
	var values []TemplateValue
	var polled []string

	for gi := range d.OidGroups {
		if ctx.Err() != nil {
			break
		}
		gr := &d.OidGroups[gi]
		if !c.templateGroupDue(d.ID, gr, ts) {
			continue
		}
		var (
			grSamples []MetricSample
			grValues  []TemplateValue
			ok        bool
		)
		if gr.Kind == "table" {
			grSamples, grValues, ok = c.collectTemplateTable(ctx, s, d, gr, ts)
		} else {
			grSamples, grValues, ok = c.collectTemplateScalars(ctx, s, d, gr, ts)
		}
		// Rate-limit both successful and unsupported/failed capability probes.
		// Otherwise a device without (for example) OSPF would retry that walk
		// on every base poll instead of the group's bounded cadence.
		c.markTemplateGroupPolled(d.ID, gr, ts)
		samples = append(samples, grSamples...)
		values = append(values, grValues...)
		if ok {
			polled = append(polled, gr.Key)
		}
	}
	return samples, values, polled
}

func (c *Collector) templateGroupDue(deviceID uuid.UUID, gr *OidGroup, ts time.Time) bool {
	if gr.IntervalSeconds <= 0 {
		return true
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	last := c.lastTplGroup[deviceID][gr.Key]
	return last.IsZero() || ts.Sub(last) >= time.Duration(gr.IntervalSeconds)*time.Second
}

func (c *Collector) markTemplateGroupPolled(deviceID uuid.UUID, gr *OidGroup, ts time.Time) {
	if gr.IntervalSeconds <= 0 {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	groups := c.lastTplGroup[deviceID]
	if groups == nil {
		groups = make(map[string]time.Time)
		c.lastTplGroup[deviceID] = groups
	}
	groups[gr.Key] = ts
}

// collectTemplateScalars GETs all metrics of a scalar group in chunks.
// A metric whose OID the agent doesn't implement is skipped silently —
// templates deliberately over-declare (e.g. HA metrics on a standalone
// unit), and absence is not an error.
func (c *Collector) collectTemplateScalars(
	ctx context.Context, s *g.GoSNMP, d *Device, gr *OidGroup, ts time.Time,
) ([]MetricSample, []TemplateValue, bool) {
	var samples []MetricSample
	var values []TemplateValue

	anyRequestOk := false
	for start := 0; start < len(gr.Metrics); start += scalarGetChunk {
		if ctx.Err() != nil {
			return samples, values, false
		}
		end := start + scalarGetChunk
		if end > len(gr.Metrics) {
			end = len(gr.Metrics)
		}
		chunk := gr.Metrics[start:end]
		oids := make([]string, len(chunk))
		for i, m := range chunk {
			oids[i] = m.OID
		}
		res, err := s.Get(oids)
		if err != nil {
			// Whole request failed (timeout / agent gone) — give up on
			// the group but keep whatever earlier chunks returned.
			return samples, values, false
		}
		anyRequestOk = true
		byOID := make(map[string]g.SnmpPDU, len(res.Variables))
		for _, v := range res.Variables {
			byOID[strings.TrimPrefix(v.Name, ".")] = v
		}
		for i := range chunk {
			m := &chunk[i]
			pdu, found := byOID[m.OID]
			if !found {
				continue
			}
			sample, value, keep := c.templateValue(d, gr, m, "", "", pdu, ts)
			if !keep {
				continue
			}
			values = append(values, value)
			if sample != nil {
				samples = append(samples, *sample)
			}
		}
	}
	return samples, values, anyRequestOk
}

// collectTemplateTable walks each metric column plus the optional label
// column and correlates rows by OID index suffix.
func (c *Collector) collectTemplateTable(
	ctx context.Context, s *g.GoSNMP, d *Device, gr *OidGroup, ts time.Time,
) ([]MetricSample, []TemplateValue, bool) {
	cols := make(map[string]map[string]g.SnmpPDU, len(gr.Metrics)) // metric key → suffix → pdu
	suffixSet := map[string]struct{}{}

	anyWalkOk := false
	for i := range gr.Metrics {
		if ctx.Err() != nil {
			return nil, nil, false
		}
		m := &gr.Metrics[i]
		pdus, err := walkAll(s, m.OID)
		if err != nil {
			continue
		}
		anyWalkOk = true
		rows := make(map[string]g.SnmpPDU, len(pdus))
		for _, p := range pdus {
			if suf := oidSuffix(p.Name, m.OID); suf != "" {
				rows[suf] = p
				suffixSet[suf] = struct{}{}
			}
		}
		cols[m.Key] = rows
	}
	if !anyWalkOk {
		return nil, nil, false
	}

	labels := map[string]string{}
	if gr.Table != nil && gr.Table.LabelOID != "" && ctx.Err() == nil {
		if pdus, err := walkAll(s, gr.Table.LabelOID); err == nil {
			for _, p := range pdus {
				if suf := oidSuffix(p.Name, gr.Table.LabelOID); suf != "" {
					labels[suf] = strings.TrimSpace(asString(p))
				}
			}
		}
	}

	suffixes := make([]string, 0, len(suffixSet))
	for suf := range suffixSet {
		suffixes = append(suffixes, suf)
	}
	sort.Strings(suffixes)
	if len(suffixes) > maxTableRows {
		suffixes = suffixes[:maxTableRows]
	}

	var samples []MetricSample
	var values []TemplateValue
	for _, suf := range suffixes {
		inst := instanceToken(suf)
		label := labels[suf]
		if label == "" {
			label = suf
		}
		for i := range gr.Metrics {
			m := &gr.Metrics[i]
			pdu, found := cols[m.Key][suf]
			if !found {
				continue
			}
			sample, value, keep := c.templateValue(d, gr, m, inst, label, pdu, ts)
			if !keep {
				continue
			}
			values = append(values, value)
			if sample != nil {
				samples = append(samples, *sample)
			}
		}
	}
	return samples, values, true
}

// templateValue converts one PDU into a (ClickHouse sample, Postgres
// value) pair according to the metric's declared type. The sample is nil
// for string metrics and for a counter's first observation.
func (c *Collector) templateValue(
	d *Device, gr *OidGroup, m *OidMetric, instance, label string,
	pdu g.SnmpPDU, ts time.Time,
) (*MetricSample, TemplateValue, bool) {
	switch pdu.Type {
	case g.NoSuchObject, g.NoSuchInstance, g.EndOfMibView, g.Null:
		return nil, TemplateValue{}, false
	}

	seriesKey := "tpl_" + m.Key
	if instance != "" {
		seriesKey += "_" + instance
	}
	tv := TemplateValue{
		GroupKey:  gr.Key,
		MetricKey: m.Key,
		Instance:  instance,
		SeriesKey: seriesKey,
		Label:     label,
		Unit:      m.Unit,
	}

	var num *float64
	isStr := false
	switch pdu.Value.(type) {
	case string, []byte:
		isStr = true
	}

	if isStr {
		text := strings.TrimSpace(asString(pdu))
		if m.Type == "string" {
			tv.ValueText = text
			return nil, tv, text != ""
		}
		tv.ValueText = text
		if m.ValueMap != nil {
			if v, ok := lookupValueMap(m.ValueMap, text); ok {
				num = &v
			}
		}
		if num == nil {
			if f, err := strconv.ParseFloat(text, 64); err == nil {
				num = &f
			} else {
				return nil, tv, false
			}
		}
	} else {
		if m.Type == "string" {
			tv.ValueText = asString(pdu)
			return nil, tv, tv.ValueText != ""
		}
		f := float64(asInt(pdu))
		if f < 0 {
			// Counters/gauges are unsigned on the wire; a negative int64
			// means the value overflowed the signed cast.
			f = float64(asUint(pdu))
		}
		num = &f
	}

	scale := m.Scale
	if scale == 0 {
		scale = 1
	}

	switch m.Type {
	case "counter":
		rate, ok := c.templateRate(d, seriesKey, uint64(*num), ts)
		if !ok {
			// First observation: snapshot stored, nothing to report yet.
			return nil, tv, false
		}
		v := rate * scale
		num = &v
	case "gauge":
		v := *num * scale
		num = &v
	}
	// enum: keep the raw code, no scaling.

	tv.ValueNum = num
	sample := &MetricSample{
		DeviceID: d.ID, Key: seriesKey, Value: *num,
		Unit: m.Unit, Timestamp: ts, PollerID: c.pollerID,
	}
	return sample, tv, true
}

// templateRate turns a monotonic counter into a per-second rate using
// the previous cycle's snapshot. Returns ok=false on the first
// observation or after a counter reset.
func (c *Collector) templateRate(d *Device, seriesKey string, cur uint64, ts time.Time) (float64, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	prev := c.prevTpl[d.ID]
	if prev == nil {
		prev = make(map[string]tplSnap)
		c.prevTpl[d.ID] = prev
	}
	snap, seen := prev[seriesKey]
	prev[seriesKey] = tplSnap{val: cur, at: ts}
	if !seen {
		return 0, false
	}
	dt := ts.Sub(snap.at).Seconds()
	if dt <= 0 || cur < snap.val {
		return 0, false
	}
	return float64(cur-snap.val) / dt, true
}

func lookupValueMap(vm map[string]float64, text string) (float64, bool) {
	if v, ok := vm[text]; ok {
		return v, true
	}
	lower := strings.ToLower(text)
	for k, v := range vm {
		if strings.ToLower(k) == lower {
			return v, true
		}
	}
	return 0, false
}

// walkAll walks a subtree with GETBULK when the session supports it,
// falling back to GETNEXT for SNMPv1 agents. Row suffix extraction
// reuses oidSuffix from udt.go.
func walkAll(s *g.GoSNMP, oid string) ([]g.SnmpPDU, error) {
	if s.Version == g.Version1 {
		return s.WalkAll(oid)
	}
	return s.BulkWalkAll(oid)
}
