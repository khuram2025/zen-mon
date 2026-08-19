package snmp

import (
	"math"
	"testing"
	"unicode/utf8"

	g "github.com/gosnmp/gosnmp"
)

func pdu(v any) g.SnmpPDU { return g.SnmpPDU{Type: g.OctetString, Value: v} }

// Every value stored in a Postgres text column must be valid UTF-8 with
// no NUL bytes; either one aborts the whole poll transaction (22021).
func assertStorable(t *testing.T, field, s string) {
	t.Helper()
	if !utf8.ValidString(s) {
		t.Errorf("%s: not valid UTF-8: %q", field, s)
	}
	for i := 0; i < len(s); i++ {
		if s[i] == 0 {
			t.Errorf("%s: contains NUL: %q", field, s)
			return
		}
	}
}

func TestCleanTextStripsUnstorableBytes(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"plain", "GigabitEthernet1/0/1", "GigabitEthernet1/0/1"},
		{"nul padded", "SW PORT\x00\x00\x00", "SW PORT"},
		{"all nul", "\x00\x00\x00\x00", ""},
		{"invalid utf8", "Cisco\xc2\x55Phone", "CiscoUPhone"},
		{"latin1 high byte", "caf\xe9", "caf"},
		{"keeps newline", "line1\nline2", "line1\nline2"},
		{"keeps unicode", "Wählen", "Wählen"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := cleanText(c.in)
			if got != c.want {
				t.Errorf("cleanText(%q) = %q, want %q", c.in, got, c.want)
			}
			assertStorable(t, "cleanText", got)
		})
	}
}

// The bytes below are what the production switches actually return; each
// one previously aborted the entire UDT transaction for its device.
func TestDecodeLldpIDRealWorldValues(t *testing.T) {
	const (
		macSubChassis, addrSubChassis = 4, 5
		macSubPort, addrSubPort       = 3, 4
	)
	cases := []struct {
		name    string
		raw     []byte
		subtype int
		macSub  int
		addrSub int
		want    string
	}{
		{
			// ArubaOS-CX reports this for attached Cisco IP phones. The
			// NUL bytes are what triggered SQLSTATE 22021.
			name: "aruba nul-padded chassis id",
			raw:  []byte{0x17, 0, 0, 0, 0, 0}, subtype: 5,
			macSub: macSubChassis, addrSub: addrSubChassis,
			want: "170000000000",
		},
		{
			name: "chassis id mac subtype",
			raw:  []byte{0x48, 0x74, 0x10, 0xb1, 0xf2, 0x99}, subtype: 4,
			macSub: macSubChassis, addrSub: addrSubChassis,
			want: "48:74:10:b1:f2:99",
		},
		{
			name: "chassis id ipv4 network address",
			raw:  []byte{1, 10, 10, 101, 201}, subtype: 5,
			macSub: macSubChassis, addrSub: addrSubChassis,
			want: "10.10.101.201",
		},
		{
			name: "port id mac subtype",
			raw:  []byte{0x00, 0x1c, 0x2e, 0xc2, 0x55, 0x01}, subtype: 3,
			macSub: macSubPort, addrSub: addrSubPort,
			want: "00:1c:2e:c2:55:01",
		},
		{
			name: "port id local subtype is text",
			raw:  []byte("70C9C66888DE:P1"), subtype: 7,
			macSub: macSubPort, addrSub: addrSubPort,
			want: "70C9C66888DE:P1",
		},
		{
			name: "unlabelled binary falls back to hex",
			raw:  []byte{0x9c, 0x92, 0xe4}, subtype: 7,
			macSub: macSubPort, addrSub: addrSubPort,
			want: "9c92e4",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := decodeLldpID(pdu(c.raw), c.subtype, c.macSub, c.addrSub)
			if got != c.want {
				t.Errorf("decodeLldpID = %q, want %q", got, c.want)
			}
			assertStorable(t, "decodeLldpID", got)
		})
	}
}

// A neighbor must never decode to "", or every neighbor on a port
// collapses into a single topology link.
func TestDecodeLldpIDNeverEmptyForNonEmptyInput(t *testing.T) {
	inputs := [][]byte{
		{0x17, 0, 0, 0, 0, 0},
		{0x00},
		{0xff, 0xfe},
		{0xc2, 0x55},
		[]byte("\x00\x00\x00"),
	}
	for _, in := range inputs {
		got := decodeLldpID(pdu(in), 7, 3, 4)
		if got == "" {
			t.Errorf("decodeLldpID(%x) returned empty", in)
		}
		assertStorable(t, "decodeLldpID", got)
	}
}

func TestAsStringSanitizes(t *testing.T) {
	for _, in := range []any{
		[]byte{0x17, 0, 0, 0, 0, 0},
		[]byte("SW PORT\x00\x00"),
		"Cisco\xc2\x55Phone",
	} {
		assertStorable(t, "asString", asString(pdu(in)))
	}
}

func TestIsAuthzError(t *testing.T) {
	if !isAuthzError(errString("Error in packet. Reason: authorizationError (access denied to that object)")) {
		t.Error("authorizationError not detected")
	}
	if !isAuthzError(errString("Bad context specified")) {
		t.Error("bad context not detected")
	}
	if isAuthzError(errString("request timeout (after 2 retries)")) {
		t.Error("timeout misreported as an authorization failure")
	}
	if isAuthzError(nil) {
		t.Error("nil misreported as an authorization failure")
	}
}

type errString string

func (e errString) Error() string { return string(e) }

func TestArubaControllerUtilizationOIDsAreScalars(t *testing.T) {
	if got, want := oidArubaControllerCPU, "1.3.6.1.4.1.14823.2.2.1.2.1.30.0"; got != want {
		t.Fatalf("Aruba controller CPU OID = %s, want %s", got, want)
	}
	if got, want := oidArubaControllerMem, "1.3.6.1.4.1.14823.2.2.1.2.1.31.0"; got != want {
		t.Fatalf("Aruba controller memory OID = %s, want %s", got, want)
	}
}

func TestCanonicalVendorMetricsMapsArubaTemplateValues(t *testing.T) {
	got := canonicalVendorMetrics([]MetricSample{
		{Key: "tpl_aruba_cpu", Value: 23, Unit: "%"},
		{Key: "tpl_aruba_mem", Value: 61, Unit: "%"},
	})

	values := metricValues(got)
	if values["cpu"] != 23 {
		t.Errorf("canonical CPU = %v, want 23", values["cpu"])
	}
	if values["memory"] != 61 {
		t.Errorf("canonical memory = %v, want 61", values["memory"])
	}
}

func TestF5MemoryDomainsUseWorstPercentageWithoutSumming(t *testing.T) {
	const gib = 1024 * 1024 * 1024
	domain, tmmPct, hostPct := selectF5MemoryDomain(
		1.7*gib, 13.3*gib,
		13.9*gib, 18.1*gib,
	)

	assertClose(t, "TMM memory", tmmPct, 12.7819548872)
	assertClose(t, "host memory", hostPct, 76.7955801105)
	assertClose(t, "canonical memory", domain.pct, 76.7955801105)
	if domain.used != 13.9*gib || domain.total != 18.1*gib {
		t.Errorf("canonical bytes = %v/%v, want host domain %v/%v", domain.used, domain.total, 13.9*gib, 18.1*gib)
	}
	if domain.pct == 99 {
		t.Fatal("canonical memory retained misleading HOST-RESOURCES value")
	}
}

func TestF5TemplateMetricsReplaceGenericMemory(t *testing.T) {
	const gib = 1024 * 1024 * 1024
	generic := []MetricSample{{Key: "memory", Value: 99, Unit: "percent"}}
	template := []MetricSample{
		{Key: "tpl_f5_tmm_mem_used", Value: 1.7 * gib, Unit: "bytes"},
		{Key: "tpl_f5_tmm_mem_total", Value: 13.3 * gib, Unit: "bytes"},
		{Key: "tpl_f5_other_mem_used", Value: 13.9 * gib, Unit: "bytes"},
		{Key: "tpl_f5_other_mem_total", Value: 18.1 * gib, Unit: "bytes"},
	}
	got := upsertMetricSamples(generic, canonicalVendorMetrics(template))
	values := metricValues(got)

	assertClose(t, "canonical memory", values["memory"], 76.7955801105)
	assertClose(t, "TMM diagnostic", values["f5_tmm_memory_pct"], 12.7819548872)
	assertClose(t, "host diagnostic", values["f5_host_memory_pct"], 76.7955801105)
	if values["memory_used_bytes"] != 13.9*gib || values["memory_total_bytes"] != 18.1*gib {
		t.Error("canonical numerator and denominator do not match selected host domain")
	}

	count := 0
	for _, sample := range got {
		if sample.Key == "memory" {
			count++
		}
	}
	if count != 1 {
		t.Errorf("memory sample count = %d, want 1", count)
	}
}

func metricValues(samples []MetricSample) map[string]float64 {
	values := make(map[string]float64, len(samples))
	for _, sample := range samples {
		values[sample.Key] = sample.Value
	}
	return values
}

func assertClose(t *testing.T, name string, got, want float64) {
	t.Helper()
	if math.Abs(got-want) > 0.000001 {
		t.Errorf("%s = %.10f, want %.10f", name, got, want)
	}
}
