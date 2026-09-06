package main

import (
	g "github.com/gosnmp/gosnmp"
	"testing"
)

func TestSNMPTransportAndNumericValues(t *testing.T) {
	d := configDevice{IPAddress: "127.0.0.1", SNMP: &snmpConfig{Version: "2c", Community: "example", Port: 161, TimeoutMS: 200, Retries: 0, OIDs: []string{"1.3.6.1.2.1.1.3.0"}}}
	session, err := snmpSession(d)
	if err != nil || session.Retries != 0 || session.Version != g.Version2c {
		t.Fatalf("session: %v, %v", session, err)
	}
	value, ok := numericSNMP(g.SnmpPDU{Type: g.Counter64, Value: uint64(1) << 63})
	if !ok || value <= 0 {
		t.Fatalf("unsigned counter overflow: %v", value)
	}
	if _, ok = numericSNMP(g.SnmpPDU{Type: g.NoSuchObject}); ok {
		t.Fatal("missing OID treated as numeric")
	}
	d.SNMP.OIDs = []string{"bad oid"}
	if _, err = snmpSession(d); err == nil {
		t.Fatal("accepted malformed OID")
	}
}

func TestSNMPv3DoesNotLoseSecurityParameters(t *testing.T) {
	d := configDevice{IPAddress: "127.0.0.1", SNMP: &snmpConfig{Version: "3", Port: 161, TimeoutMS: 200, Username: "probe", AuthProtocol: "SHA256", AuthPassphrase: "auth-example", PrivProtocol: "AES", PrivPassphrase: "priv-example", OIDs: []string{"1.3.6.1.2.1.1.3.0"}}}
	session, err := snmpSession(d)
	if err != nil {
		t.Fatal(err)
	}
	if session.MsgFlags != g.AuthPriv {
		t.Fatal("SNMPv3 protection downgraded")
	}
	d.SNMP.AuthPassphrase = ""
	if _, err = snmpSession(d); err == nil {
		t.Fatal("privacy without authentication accepted")
	}
}
