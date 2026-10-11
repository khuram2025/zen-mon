package snmp

import (
	g "github.com/gosnmp/gosnmp"
	"testing"
)

func TestARPReporterLocalInterfacesAndInvalidNeighbors(t *testing.T) {
	mac := []byte{0xaa, 0xbb, 0xcc, 0xdd, 0xee, 2}
	pdu := func(oid string, value []byte) g.SnmpPDU {
		return g.SnmpPDU{Name: oid, Type: g.OctetString, Value: value}
	}
	rows := []g.SnmpPDU{
		pdu(OIDIpNetToMediaPhysAddress+".7.192.0.2.10", mac),
		pdu(OIDIpNetToMediaPhysAddress+".8.192.0.2.10", mac),
		pdu(OIDIpNetToMediaPhysAddress+".9.192.0.2.11", mac),
		pdu(OIDIpNetToMediaPhysAddress+".0.192.0.2.10", mac),
		pdu(OIDIpNetToMediaPhysAddress+".7.999.0.2.10", mac),
		pdu(OIDIpNetToMediaPhysAddress+".7.0.0.0.0", mac),
		pdu(OIDIpNetToMediaPhysAddress+".7.192.0.2.12", []byte{1, 0, 0x5e, 0, 0, 1}),
	}
	types := []g.SnmpPDU{{Name: OIDIpNetToMediaType + ".9.192.0.2.11", Type: g.Integer, Value: 2}}
	got := parseARPTables(rows, types, nil, nil, nil, nil)
	if len(got) != 2 || got[0].IfIndex != 7 || got[1].IfIndex != 8 {
		t.Fatalf("local evidence lost or invalid neighbors retained: %+v", got)
	}
}

func TestPhysicalNeighborStatesAndIPv6(t *testing.T) {
	mac := []byte{0xaa, 0xbb, 0xcc, 0xdd, 0xee, 2}
	base := "1.3.6.1.2.1.4.35.1."
	suffixes := []string{"7.1.4.192.0.2.10", "7.1.4.192.0.2.11", "7.1.4.192.0.2.12", "7.1.4.192.0.2.13", "7.2.16.32.1.13.184.0.0.0.0.0.0.0.0.0.0.0.1"}
	var rows []g.SnmpPDU
	for _, suffix := range suffixes {
		rows = append(rows, g.SnmpPDU{Name: OIDIpNetToPhysicalPhysAddr + "." + suffix, Type: g.OctetString, Value: mac})
	}
	state := []g.SnmpPDU{{Name: base + "6." + suffixes[1], Type: g.Integer, Value: 7}, {Name: base + "6." + suffixes[2], Type: g.Integer, Value: 5}}
	status := []g.SnmpPDU{{Name: base + "8." + suffixes[3], Type: g.Integer, Value: 2}}
	got := parseARPTables(nil, nil, rows, state, nil, status)
	if len(got) != 2 || got[0].IP != "192.0.2.10" || got[1].IP != "2001:db8::1" || got[1].Source != "nd" {
		t.Fatalf("invalid physical neighbor evidence: %+v", got)
	}
}

func TestFDBIdentifiersAreNotVLANIdentifiers(t *testing.T) {
	root := "1.3.6.1.2.1.17.7.1.4.2.1.3"
	rows := []g.SnmpPDU{
		{Name: root + ".0.24", Type: g.Integer, Value: 9000},
		{Name: root + ".0.25", Type: g.Integer, Value: 100},
		{Name: root + ".0.26", Type: g.Integer, Value: 100},
		{Name: root + ".0.4095", Type: g.Integer, Value: 200},
	}
	got := fdbVLANs(rows)
	if got[9000] != 24 || got[100] != 0 || len(got) != 2 {
		t.Fatalf("invented VLAN association: %+v", got)
	}
}
