package snmp

import (
	"encoding/json"
	"fmt"
	"hash/fnv"
	"regexp"
	"strings"
)

// This file implements the poller-side model of device_profiles.oid_groups —
// the "monitoring template" contents. A template is a list of OID groups;
// each group is either a set of scalar OIDs (single GET) or an SNMP table
// (one BulkWalk per column, rows correlated by index suffix).
//
// The JSON shape is shared with the API (server/app/schemas/snmp.py) and the
// template editor UI. The poller only reads the fields it needs for
// collection; presentation fields (labels, thresholds, render hints) are
// passed through untouched.

// OidMetric is one collected value inside a group.
type OidMetric struct {
	// Key is the metric identifier, unique across the whole template
	// (e.g. "fgt_cpu"). The ClickHouse series key becomes "tpl_<key>"
	// for scalars and "tpl_<key>_<instance>" for table columns.
	Key  string `json:"key"`
	Name string `json:"name"`
	OID  string `json:"oid"`
	// Type: "gauge" (numeric level), "counter" (monotonic — emitted as a
	// per-second rate), "enum" (integer state, labels applied in the UI),
	// "string" (textual — stored in Postgres only, never ClickHouse).
	Type string `json:"type"`
	Unit string `json:"unit,omitempty"`
	// Scale multiplies the raw value (e.g. 0.001 for KB→MB). 0 means 1.
	Scale float64 `json:"scale,omitempty"`
	// ValueMap coerces string agent responses to numeric codes, for
	// devices that expose states as strings (e.g. PAN-OS HA "active").
	// Matching is case-insensitive on the trimmed value.
	ValueMap map[string]float64 `json:"value_map,omitempty"`
}

// OidGroupTable configures table-kind groups.
type OidGroupTable struct {
	// LabelOID is an optional column walked to produce per-row display
	// labels (e.g. tunnel name, AP name). Falls back to the row index.
	LabelOID string `json:"label_oid,omitempty"`
}

// OidGroup is one section of a monitoring template.
type OidGroup struct {
	Key  string `json:"key"`
	Name string `json:"name"`
	// Kind: "scalar" | "table".
	Kind    string         `json:"kind"`
	Table   *OidGroupTable `json:"table,omitempty"`
	Metrics []OidMetric    `json:"metrics"`
}

// TemplateValue is one collected template datapoint destined for the
// device_template_values latest-snapshot table in Postgres. Numeric
// values additionally flow to ClickHouse snmp_metrics as MetricSample
// under SeriesKey for history/charting.
type TemplateValue struct {
	GroupKey  string
	MetricKey string
	Instance  string // "" for scalars; row index token for table cells
	SeriesKey string
	Label     string // row label for table cells, "" for scalars
	Unit      string
	ValueNum  *float64
	ValueText string
}

var keyRe = regexp.MustCompile(`^[a-z0-9][a-z0-9_]{0,62}$`)

// ParseOidGroups decodes and sanity-checks an oid_groups JSON document.
// Invalid groups/metrics are dropped (not fatal) so one bad entry cannot
// disable an entire template; the returned error list is for logging.
func ParseOidGroups(raw json.RawMessage) ([]OidGroup, []error) {
	if len(raw) == 0 {
		return nil, nil
	}
	var groups []OidGroup
	if err := json.Unmarshal(raw, &groups); err != nil {
		return nil, []error{fmt.Errorf("parse oid_groups: %w", err)}
	}
	var errs []error
	out := make([]OidGroup, 0, len(groups))
	for _, gr := range groups {
		if !keyRe.MatchString(gr.Key) {
			errs = append(errs, fmt.Errorf("group %q: invalid key", gr.Key))
			continue
		}
		if gr.Kind != "scalar" && gr.Kind != "table" {
			errs = append(errs, fmt.Errorf("group %s: invalid kind %q", gr.Key, gr.Kind))
			continue
		}
		metrics := make([]OidMetric, 0, len(gr.Metrics))
		for _, m := range gr.Metrics {
			if !keyRe.MatchString(m.Key) {
				errs = append(errs, fmt.Errorf("group %s: metric %q: invalid key", gr.Key, m.Key))
				continue
			}
			m.OID = strings.TrimPrefix(strings.TrimSpace(m.OID), ".")
			if m.OID == "" || !validOID(m.OID) {
				errs = append(errs, fmt.Errorf("group %s: metric %s: invalid oid", gr.Key, m.Key))
				continue
			}
			switch m.Type {
			case "gauge", "counter", "enum", "string":
			default:
				m.Type = "gauge"
			}
			metrics = append(metrics, m)
		}
		if len(metrics) == 0 {
			continue
		}
		gr.Metrics = metrics
		if gr.Table != nil {
			gr.Table.LabelOID = strings.TrimPrefix(strings.TrimSpace(gr.Table.LabelOID), ".")
		}
		out = append(out, gr)
	}
	return out, errs
}

func validOID(oid string) bool {
	for _, part := range strings.Split(oid, ".") {
		if part == "" {
			return false
		}
		for _, r := range part {
			if r < '0' || r > '9' {
				return false
			}
		}
	}
	return true
}

// instanceToken converts a table row's index suffix into a compact,
// ClickHouse-safe series-key component. Pure numeric suffixes are kept
// as-is; composite or string indexes (e.g. F5's name-encoded indexes,
// FortiOS 7 two-part phase2 indexes) are hashed to 8 hex chars to bound
// metric-key cardinality.
func instanceToken(suffix string) string {
	if suffix == "" {
		return ""
	}
	isNum := true
	for _, r := range suffix {
		if r < '0' || r > '9' {
			isNum = false
			break
		}
	}
	if isNum && len(suffix) <= 12 {
		return suffix
	}
	h := fnv.New32a()
	h.Write([]byte(suffix))
	return fmt.Sprintf("x%08x", h.Sum32())
}
