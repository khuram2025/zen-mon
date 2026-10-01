package netflow

import (
	"encoding/binary"
	"strings"
	"testing"
)

// CR-12: NBAR2 / App-ID. applicationId (IE 95) is one classification-engine
// byte followed by the selector; the stored app_id is engine<<32 | selector.

func TestAppIDEncoding(t *testing.T) {
	cases := []struct {
		name string
		v    []byte
		want uint64
		ok   bool
	}{
		{"NBAR2 engine 13, 3-byte selector", []byte{13, 0, 1, 0xC5}, 13<<32 | 453, true},
		{"IANA-L4 engine 3, 2-byte port", []byte{3, 0x01, 0xBB}, 3<<32 | 443, true},
		{"4-byte selector", []byte{20, 0, 0, 0x0C, 0x9A}, 20<<32 | 0x0C9A, true},
		{"longer selector keeps its first four bytes", []byte{20, 0, 0, 0, 9, 0xFF, 0xFF}, 20<<32 | 9, true},
		{"engine without selector", []byte{13}, 0, false},
	}
	for _, c := range cases {
		got, ok := appID(c.v)
		if got != c.want || ok != c.ok {
			t.Errorf("%s: appID=%#x ok=%v want %#x %v", c.name, got, ok, c.want, c.ok)
		}
	}
}

func TestParseAppIDInFlowRecords(t *testing.T) {
	rec := append(basic4("10.0.0.1", "142.250.74.46", 900, 3, 6), fv{ieApplicationID, 4, []byte{13, 0, 5, 0x83}})
	out, _ := decodeV9(t, NewV9TemplateCache(), "192.0.2.80", 256, rec)
	if out[0].AppID != 13<<32|1411 || out[0].FieldsPresent&FieldAppID == 0 {
		t.Fatalf("v9 app id: %#x fields %#x", out[0].AppID, out[0].FieldsPresent)
	}

	// IPFIX, variable-length encoding (RFC 7011 §7).
	fields := append(fieldsOf(basic4("10.0.0.1", "10.0.0.2", 1, 1, 17)), V9TemplateField{Type: ieApplicationID, Length: 0xFFFF})
	body := append(encodeRecords(t, basic4("10.0.0.1", "10.0.0.2", 1, 1, 17)), 3, 3, 0, 53)
	out, _, err := ParseIPFIX(ipfixMsg(1, 1, ipfixTemplate(256, fields), ipfixSet(256, pad4(body))), addr("192.0.2.81"), "t", testReceivedAt, NewV9TemplateCache())
	if err != nil || len(out) != 1 || out[0].AppID != 3<<32|53 {
		t.Fatalf("IPFIX varlen app id: err=%v %+v", err, out)
	}

	// A template without IE 95 exports no application: 0 and the bit clear.
	plain, _ := decodeV9(t, NewV9TemplateCache(), "192.0.2.80", 257, basic4("10.0.0.1", "10.0.0.2", 1, 1, 6))
	if plain[0].AppID != 0 || plain[0].FieldsPresent&FieldAppID != 0 {
		t.Fatalf("no IE 95: %+v", plain[0])
	}
}

// fixedText is a NUL-padded fixed-length string field.
func fixedText(s string, n int) []byte {
	b := make([]byte, n)
	copy(b, s)
	return b
}

// NBAR2 in NetFlow v9: the "application table" option template (scope
// System, options 95 + 96 + 94) and the "application attributes" template
// (95 + 372) arrive as separate options records.
func TestParseV9ApplicationTable(t *testing.T) {
	cache := NewV9TemplateCache()
	exp := addr("192.0.2.82")
	scope := []V9TemplateField{{Type: 1, Length: 4}}
	table := v9OptionsTemplateSet(300, scope, []V9TemplateField{
		{Type: ieApplicationID, Length: 4}, {Type: ieApplicationName, Length: 24}, {Type: ieApplicationDesc, Length: 32},
	})
	attrs := v9OptionsTemplateSet(301, scope, []V9TemplateField{{Type: ieApplicationID, Length: 4}, {Type: ieApplicationCategory, Length: 32}})
	var tableData []byte
	for _, app := range []struct {
		id         []byte
		name, desc string
	}{
		{[]byte{13, 0, 1, 0xC5}, "ssl", "Secure Socket Layer"},
		{[]byte{13, 0, 5, 0x83}, "youtube", "YouTube\x00garbage after the terminator"},
		{[]byte{13, 0, 9, 0x99}, "", ""}, // no text at all: nothing to learn
	} {
		tableData = binary.BigEndian.AppendUint32(tableData, 0)
		tableData = append(tableData, app.id...)
		tableData = append(tableData, fixedText(app.name, 24)...)
		tableData = append(tableData, fixedText(app.desc, 32)...)
	}
	attrData := binary.BigEndian.AppendUint32(nil, 0)
	attrData = append(attrData, 13, 0, 5, 0x83)
	attrData = append(attrData, fixedText("consumer-streaming", 32)...)

	pkt := v9Packet(0, 1, table, attrs, v9Set(300, tableData), v9Set(301, attrData))
	records, st, err := ParseV9(pkt, exp, "t", testReceivedAt, cache)
	if err != nil || len(records) != 0 {
		t.Fatalf("options data is never traffic: err=%v records=%d", err, len(records))
	}
	if st.OptionsRecords != 4 || len(st.AppNames) != 3 {
		t.Fatalf("options records=%d app names=%+v", st.OptionsRecords, st.AppNames)
	}
	want := []AppName{
		{Exporter: exp, AppID: 13<<32 | 453, Name: "ssl", Description: "Secure Socket Layer"},
		{Exporter: exp, AppID: 13<<32 | 1411, Name: "youtube", Description: "YouTube"},
		{Exporter: exp, AppID: 13<<32 | 1411, Category: "consumer-streaming"},
	}
	for i, w := range want {
		if st.AppNames[i] != w {
			t.Errorf("app %d: %+v want %+v", i, st.AppNames[i], w)
		}
	}
}

// NBAR2 in IPFIX: applicationId is the scope field of the options template
// (and is read, unlike v9 scope types); names may be variable length.
func TestParseIPFIXApplicationTable(t *testing.T) {
	scope := []V9TemplateField{{Type: ieApplicationID, Length: 4}}
	opts := []V9TemplateField{{Type: ieApplicationName, Length: 0xFFFF}, {Type: ieApplicationCategory, Length: 0xFFFF}}
	var data []byte
	data = append(data, 13, 0, 0, 0x50)
	data = append(data, 4)
	data = append(data, "http"...)
	data = append(data, 8)
	data = append(data, "browsing"...)
	msg := ipfixMsg(7, 1, ipfixOptionsTemplate(400, scope, opts), ipfixSet(400, pad4(data)))
	_, st, err := ParseIPFIX(msg, addr("2001:db8::82"), "t", testReceivedAt, NewV9TemplateCache())
	if err != nil || len(st.AppNames) != 1 {
		t.Fatalf("err=%v apps=%+v", err, st.AppNames)
	}
	if a := st.AppNames[0]; a.AppID != 13<<32|80 || a.Name != "http" || a.Category != "browsing" || a.Exporter != addr("2001:db8::82") {
		t.Fatalf("IPFIX app name: %+v", a)
	}
	if st.DataRecords != 1 {
		t.Fatalf("options records count toward the IPFIX sequence: %d", st.DataRecords)
	}
}

func TestAppText(t *testing.T) {
	cases := map[string]string{
		"ssl\x00\x00\x00":                "ssl",
		"  padded name  ":                "padded name",
		"bell\x07and\ttab":               "bellandtab",
		"bad utf8 \xff\xfe here":         "bad utf8  here",
		"cut\x00after the terminator":    "cut",
		strings.Repeat("x", 300) + "end": strings.Repeat("x", maxAppText),
	}
	for in, want := range cases {
		if got := appText([]byte(in)); got != want {
			t.Errorf("appText(%q) = %q, want %q", in, got, want)
		}
	}
}
