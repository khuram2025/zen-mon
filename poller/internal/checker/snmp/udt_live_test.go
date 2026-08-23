package snmp

import (
	"os"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	g "github.com/gosnmp/gosnmp"
)

// Live check against a real switch. Walks the LLDP tables the way the
// collector does and asserts every decoded value is safe to store, which
// is the failure that silently discarded whole devices' UDT snapshots.
//
// Run with:
//
//	UDT_LIVE_HOST=10.10.101.221 UDT_LIVE_USER=... UDT_LIVE_AUTH=... \
//	UDT_LIVE_PRIV=... go test ./internal/checker/snmp/ -run Live -v
func TestLiveLldpDecodesToStorableText(t *testing.T) {
	host := os.Getenv("UDT_LIVE_HOST")
	if host == "" {
		t.Skip("UDT_LIVE_HOST not set")
	}
	s := &g.GoSNMP{
		Target: host, Port: 161, Version: g.Version3,
		Timeout: 5 * time.Second, Retries: 1,
		SecurityModel: g.UserSecurityModel,
		MsgFlags:      g.AuthPriv,
		SecurityParameters: &g.UsmSecurityParameters{
			UserName:                 os.Getenv("UDT_LIVE_USER"),
			AuthenticationProtocol:   g.SHA,
			AuthenticationPassphrase: os.Getenv("UDT_LIVE_AUTH"),
			PrivacyProtocol:          g.AES,
			PrivacyPassphrase:        os.Getenv("UDT_LIVE_PRIV"),
		},
	}
	if err := s.Connect(); err != nil {
		t.Fatalf("connect %s: %v", host, err)
	}
	defer s.Conn.Close()

	subtypes := func(oid string) map[string]int {
		out := map[string]int{}
		pdus, err := s.BulkWalkAll(oid)
		if err != nil {
			t.Fatalf("walk %s: %v", oid, err)
		}
		for _, p := range pdus {
			out[oidSuffix(p.Name, oid)] = int(asInt(p))
		}
		return out
	}
	chassisSub := subtypes(OIDLldpRemChassisIDSubtype)
	portSub := subtypes(OIDLldpRemPortIDSubtype)

	checked := 0
	for _, col := range []struct {
		oid            string
		sub            map[string]int
		macSub, addrSb int
	}{
		{OIDLldpRemChassisID, chassisSub, 4, 5},
		{OIDLldpRemPortID, portSub, 3, 4},
	} {
		pdus, err := s.BulkWalkAll(col.oid)
		if err != nil {
			t.Fatalf("walk %s: %v", col.oid, err)
		}
		for _, p := range pdus {
			suffix := oidSuffix(p.Name, col.oid)
			got := decodeLldpID(p, col.sub[suffix], col.macSub, col.addrSb)
			raw := bytesOf(p)
			if !utf8.ValidString(got) {
				t.Errorf("%s.%s raw=%x -> invalid UTF-8 %q", col.oid, suffix, raw, got)
			}
			if strings.ContainsRune(got, 0) {
				t.Errorf("%s.%s raw=%x -> contains NUL %q", col.oid, suffix, raw, got)
			}
			if got == "" && len(raw) > 0 {
				t.Errorf("%s.%s raw=%x -> empty identifier", col.oid, suffix, raw)
			}
			checked++
			t.Logf("%-28s raw=%-16x subtype=%d -> %q", suffix, raw, col.sub[suffix], got)
		}
	}
	// Free-text columns go through asString and must be storable too.
	for _, oid := range []string{OIDLldpRemSysName, OIDLldpRemSysDesc, OIDLldpRemPortDesc} {
		pdus, _ := s.BulkWalkAll(oid)
		for _, p := range pdus {
			got := asString(p)
			if !utf8.ValidString(got) || strings.ContainsRune(got, 0) {
				t.Errorf("%s.%s raw=%x -> unstorable %q", oid, oidSuffix(p.Name, oid), bytesOf(p), got)
			}
			checked++
		}
	}
	if checked == 0 {
		t.Fatal("no LLDP data returned — cannot confirm the fix")
	}
	t.Logf("checked %d LLDP values, all storable", checked)
}
