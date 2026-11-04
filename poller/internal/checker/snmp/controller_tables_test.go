package snmp

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/google/uuid"
	g "github.com/gosnmp/gosnmp"
)

// Controller tables (FortiGate per-port, per-radio and per-client rows) need
// three things from template collection: binary OctetStrings kept as hex, the
// raw index suffix kept beside the hashed instance token, and a row cap a
// group can raise.

func TestHexFormatKeepsBinaryOctetStrings(t *testing.T) {
	c := NewCollector("test", nil)
	d := &Device{ID: uuid.New()}
	gr := &OidGroup{Key: "fap_clients", Kind: "table"}
	m := &OidMetric{Key: "fgt_sta_ip", Type: "string", Format: "hex"}
	pdu := g.SnmpPDU{Name: ".1.2.3", Type: g.OctetString, Value: []byte{10, 21, 5, 7}}
	sample, tv, keep := c.templateValue(d, gr, m, "x1", "", pdu, time.Now())
	if !keep || sample != nil {
		t.Fatalf("keep=%v sample=%v, want kept text value without a sample", keep, sample)
	}
	if tv.ValueText != "0a150507" {
		t.Fatalf("hex value = %q, want 0a150507", tv.ValueText)
	}

	// Without the format the same bytes are control characters and vanish.
	plain := &OidMetric{Key: "fgt_sta_ip_text", Type: "string"}
	_, tv, keep = c.templateValue(d, gr, plain, "x1", "", pdu, time.Now())
	if keep && tv.ValueText != "" {
		t.Fatalf("plain text unexpectedly kept %q", tv.ValueText)
	}
}

func TestNumericStringGaugeStillParses(t *testing.T) {
	// fgSwPortPOEPower is a DisplayString in mW; a gauge with scale turns it into watts.
	c := NewCollector("test", nil)
	d := &Device{ID: uuid.New()}
	gr := &OidGroup{Key: "fsw_ports", Kind: "table"}
	m := &OidMetric{Key: "fgt_swp_poe_power", Type: "gauge", Unit: "W", Scale: 0.001}
	pdu := g.SnmpPDU{Name: ".1", Type: g.OctetString, Value: []byte("4300")}
	sample, tv, keep := c.templateValue(d, gr, m, "x2", "", pdu, time.Now())
	if !keep || sample == nil || tv.ValueNum == nil || *tv.ValueNum != 4.3 {
		t.Fatalf("PoE power = %+v / %+v, want 4.3 W", sample, tv.ValueNum)
	}
}

func TestTableRowLimit(t *testing.T) {
	cases := []struct {
		table *OidGroupTable
		want  int
	}{
		{nil, maxTableRows},
		{&OidGroupTable{}, maxTableRows},
		{&OidGroupTable{MaxRows: 4000}, 4000},
		{&OidGroupTable{MaxRows: 100000}, maxTableRowsLimit},
		{&OidGroupTable{MaxRows: 50}, 50},
	}
	for _, tc := range cases {
		if got := tableRowLimit(&OidGroup{Table: tc.table}); got != tc.want {
			t.Fatalf("tableRowLimit(%+v) = %d, want %d", tc.table, got, tc.want)
		}
	}
}

func TestOidGroupsParseFormatAndMaxRows(t *testing.T) {
	raw := json.RawMessage(`[{"key":"fap_clients","name":"Clients","kind":"table",
		"table":{"max_rows":2000},
		"metrics":[{"key":"fgt_sta_ip","name":"IP","oid":"1.3.6.1.4.1.12356.101.14.5.1.7","type":"string","format":"hex"}]}]`)
	groups, errs := ParseOidGroups(raw)
	if len(errs) != 0 || len(groups) != 1 {
		t.Fatalf("parse: groups=%d errs=%v", len(groups), errs)
	}
	if groups[0].Table.MaxRows != 2000 || groups[0].Metrics[0].Format != "hex" {
		t.Fatalf("parsed %+v / %+v", groups[0].Table, groups[0].Metrics[0])
	}
}

func TestInstanceTokenHashesCompositeIndexes(t *testing.T) {
	// The reason TemplateValue.Index exists: a FortiAP radio row's composite
	// index hashes to a token unrelated to its AP's token.
	ap := "1.11.70.80.50.51.49.70.84.70.50.50.49"
	radio := ap + ".1"
	if instanceToken(radio)[:1] != "x" || instanceToken(ap) == instanceToken(radio) {
		t.Fatalf("tokens ap=%s radio=%s", instanceToken(ap), instanceToken(radio))
	}
}
