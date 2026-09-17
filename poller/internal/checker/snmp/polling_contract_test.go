package snmp

import (
	"github.com/google/uuid"
	g "github.com/gosnmp/gosnmp"
	"math"
	"net"
	"testing"
	"time"
)

func TestCounterRateBoundaries(t *testing.T) {
	for _, test := range []struct {
		name      string
		cur, prev uint64
		seconds   float64
		hc        bool
		want      float64
	}{
		{"one_gigabit", 7500000000, 0, 60, true, 1e9},
		{"hundred_gigabit", 750000000000, 0, 60, true, 1e11},
		{"exact_wrap", 9, math.MaxUint32, 1, false, 80},
		{"hc_reset", 10, 1000, 1, true, 0},
		{"zero_elapsed", 10, 0, 0, true, 0},
		{"negative_elapsed", 10, 0, -1, true, 0},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := rateBps(test.cur, test.prev, test.seconds, test.hc); got != test.want {
				t.Fatalf("rate %v, want %v", got, test.want)
			}
		})
	}
}

func TestInterfaceStatusUsesIFMIBCodes(t *testing.T) {
	c := NewCollector("fixture", nil)
	got := c.diffInterfaces(uuid.New(), []Interface{{IfIndex: 1, OperStatus: "down"}}, time.Unix(1, 0))
	if got[0].OperStatus != 2 {
		t.Fatalf("down encoded as %d; IF-MIB requires 2", got[0].OperStatus)
	}
}

func TestCounterWidthChangeStartsNewBaseline(t *testing.T) {
	c := NewCollector("fixture", nil)
	id := uuid.New()
	c.diffInterfaces(id, []Interface{{IfIndex: 1, InOctets: 100, HasHC: false}}, time.Unix(1, 0))
	got := c.diffInterfaces(id, []Interface{{IfIndex: 1, InOctets: 10000000, HasHC: true}}, time.Unix(61, 0))
	if got[0].InBps != 0 {
		t.Fatal("counter width transition produced a rate", got[0].InBps)
	}
}

func TestSessionRejectsPartialV3Security(t *testing.T) {
	_, err := NewSession(&Device{IPAddress: net.ParseIP("2001:db8::1"), Version: "3", V3Username: "fixture", PrivProtocol: "AES", PrivPassphrase: "test-only-passphrase"})
	if err == nil {
		t.Fatal("privacy without authentication silently downgraded")
	}
}

func TestIPv6AuthPrivSessionContract(t *testing.T) {
	s, err := NewSession(&Device{IPAddress: net.ParseIP("2001:db8::1"), Version: "3", V3Username: "fixture", V3Context: "test-vrf", AuthProtocol: "SHA256", AuthPassphrase: "fixture-auth", PrivProtocol: "AES", PrivPassphrase: "fixture-priv"})
	if err != nil {
		t.Fatal(err)
	}
	if s.Target != "2001:db8::1" || s.ContextName != "test-vrf" || s.MsgFlags != g.AuthPriv {
		t.Fatal("lost IPv6/security/context settings")
	}
}
