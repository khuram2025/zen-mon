package netflow

import (
	"testing"
	"time"
)

// Clock policy (contract §4.3): flow end > 120 s ahead or > 7200 s behind the
// receipt time is clamped to the receipt time; switched times are preserved.
func TestClampClock(t *testing.T) {
	cases := []struct {
		name    string
		offset  time.Duration
		clamped bool
	}{
		{"on time", -30 * time.Second, false},
		{"just inside future tolerance", 119 * time.Second, false},
		{"future beyond tolerance", 121 * time.Second, true},
		{"just inside past tolerance", -7199 * time.Second, false},
		{"past beyond tolerance", -7201 * time.Second, true},
		{"years in the past", -20 * 365 * 24 * time.Hour, true},
	}
	for _, c := range cases {
		r := Record{
			ReceivedAt:      testReceivedAt,
			Timestamp:       testReceivedAt.Add(c.offset),
			FirstSwitchedMS: 11,
			LastSwitchedMS:  22,
			FieldsPresent:   FieldTracked,
		}
		got := ClampClock(&r)
		if got != c.clamped {
			t.Errorf("%s: clamped=%v want %v", c.name, got, c.clamped)
		}
		if c.clamped && (!r.Timestamp.Equal(testReceivedAt) || r.FieldsPresent&FieldClockClamped == 0) {
			t.Errorf("%s: timestamp=%v bits=%#x", c.name, r.Timestamp, r.FieldsPresent)
		}
		if !c.clamped && (r.FieldsPresent&FieldClockClamped != 0 || !r.Timestamp.Equal(testReceivedAt.Add(c.offset))) {
			t.Errorf("%s: an in-bounds record was modified", c.name)
		}
		if r.FirstSwitchedMS != 11 || r.LastSwitchedMS != 22 {
			t.Errorf("%s: original switched times must be preserved", c.name)
		}
	}
}

func TestLegacyIfIndex(t *testing.T) {
	for in, want := range map[uint32]uint16{0: 0, 3: 3, 65535: 65535, 65536: 0, 70000: 0, 436207616: 0} {
		if got := LegacyIfIndex(in); got != want {
			t.Errorf("LegacyIfIndex(%d) = %d, want %d", in, got, want)
		}
	}
}

func TestTemplateCapabilities(t *testing.T) {
	info := TemplateInfo{Protocol: ProtocolNetFlow9, Fields: []V9TemplateField{
		{Type: ieTCPFlags, Length: 1}, {Type: ieSrcIPv6, Length: 16}, {Type: ieInputIf, Length: 4},
		{Type: ieDirection, Length: 1}, {Type: ieApplicationID, Length: 4}, {Type: ieSamplerID, Length: 1},
		{Type: ieXlateSrcIPv4, Length: 4}, {Type: ieNSELFwEvent84, Length: 1},
	}}
	want := TemplateCapabilities{TCPFlags: true, IPv6: true, IfIndex32: true, Direction: true, AppID: true, Sampling: true, NAT: true, FwEvent: true}
	if got := info.Capabilities(); got != want {
		t.Fatalf("capabilities %+v want %+v", got, want)
	}
	// The pre-8.4 ASA NSEL numbers are NetFlow v9 vendor fields: an IPFIX
	// template never reports them as NAT / firewall-event capabilities.
	legacy := []V9TemplateField{{Type: ieNSELXlateSrc84, Length: 4}, {Type: ieNSELFwEvent84, Length: 1}}
	if got := (TemplateInfo{Protocol: ProtocolNetFlow9, Fields: legacy}).Capabilities(); !got.NAT || !got.FwEvent {
		t.Fatalf("v9 NSEL 40001/40005: %+v", got)
	}
	if got := (TemplateInfo{Protocol: ProtocolIPFIX, Fields: legacy}).Capabilities(); got.NAT || got.FwEvent {
		t.Fatalf("IPFIX 40001/40005 are not NSEL fields: %+v", got)
	}
	// 16-bit interface fields and v9 options scope types are not capabilities.
	scoped := TemplateInfo{Protocol: ProtocolNetFlow9, IsOptions: true, ScopeCount: 1, Fields: []V9TemplateField{
		{Type: 6, Length: 4}, // scope type 6 is not TCP_FLAGS
		{Type: ieInputIf, Length: 2},
	}}
	if got := scoped.Capabilities(); got != (TemplateCapabilities{}) {
		t.Fatalf("v9 scope/16-bit fields: %+v", got)
	}
}
