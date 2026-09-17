package snmp

import (
	"context"
	g "github.com/gosnmp/gosnmp"
	"net"
	"testing"
	"time"
)

type trapFixture struct{ records []TrapRecord }

func (s *trapFixture) WriteTrap(r TrapRecord) { s.records = append(s.records, r) }

type fixtureLog struct{}

func (fixtureLog) Infof(string, ...any)  {}
func (fixtureLog) Warnf(string, ...any)  {}
func (fixtureLog) Errorf(string, ...any) {}

func TestTrapVersionNormalization(t *testing.T) {
	for _, test := range []struct {
		name   string
		packet *g.SnmpPacket
		oid    string
	}{
		{"v1_linkDown", &g.SnmpPacket{Version: g.Version1, SnmpTrap: g.SnmpTrap{Enterprise: ".1.3.6.1.4.1.9", GenericTrap: 2}}, "1.3.6.1.6.3.1.1.5.3"},
		{"v1_enterprise", &g.SnmpPacket{Version: g.Version1, SnmpTrap: g.SnmpTrap{Enterprise: ".1.3.6.1.4.1.9", GenericTrap: 6, SpecificTrap: 99}}, "1.3.6.1.4.1.9.0.99"},
		{"v2_linkDown", &g.SnmpPacket{Version: g.Version2c, Variables: []g.SnmpPDU{{Name: ".1.3.6.1.6.3.1.1.4.1.0", Type: g.ObjectIdentifier, Value: ".1.3.6.1.6.3.1.1.5.3"}}}, "1.3.6.1.6.3.1.1.5.3"},
	} {
		t.Run(test.name, func(t *testing.T) {
			sink := &trapFixture{}
			listener := NewTrapListener("fixture", "127.0.0.1:0", sink, nil, fixtureLog{})
			listener.onTrap(test.packet, &net.UDPAddr{IP: net.ParseIP("192.0.2.1"), Port: 123})
			if len(sink.records) != 1 || sink.records[0].TrapOID != test.oid {
				t.Fatalf("unexpected trap: %+v", sink.records)
			}
			if test.name != "v1_enterprise" && sink.records[0].Severity != "critical" {
				t.Fatal("linkDown severity lost")
			}
		})
	}
}

type channelTrapSink chan TrapRecord

func (s channelTrapSink) WriteTrap(r TrapRecord) { s <- r }

func TestTrapWireV3AuthPrivAndCredentialRotation(t *testing.T) {
	sink := make(channelTrapSink, 8)
	listener := NewTrapListener("fixture", "127.0.0.1:0", sink, nil, fixtureLog{})
	dev := &Device{IPAddress: net.ParseIP("127.0.0.1"), Version: "3", V3Username: "fixture",
		AuthProtocol: "SHA", AuthPassphrase: "fixture-auth-only", PrivProtocol: "AES", PrivPassphrase: "fixture-priv-only"}
	listener.SetV3Devices([]*Device{dev})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := listener.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	port := uint16(listener.listener.LocalAddr().(*net.UDPAddr).Port)
	send := func(d *Device, want bool) {
		t.Helper()
		s, err := NewSession(d)
		if err != nil {
			t.Fatal(err)
		}
		s.Port = port
		sp := s.SecurityParameters.(*g.UsmSecurityParameters)
		sp.AuthoritativeEngineID = string([]byte{0x80, 0, 0, 0, 1, 2, 3, 4})
		sp.AuthoritativeEngineBoots = 1
		sp.AuthoritativeEngineTime = 100
		if err := s.Connect(); err != nil {
			t.Fatal(err)
		}
		defer s.Conn.Close()
		_, err = s.SendTrap(g.SnmpTrap{Variables: []g.SnmpPDU{{Name: "1.3.6.1.6.3.1.1.4.1.0", Type: g.ObjectIdentifier, Value: "1.3.6.1.6.3.1.1.5.3"}}})
		if err != nil {
			t.Fatal(err)
		}
		select {
		case r := <-sink:
			if !want || r.TrapOID != "1.3.6.1.6.3.1.1.5.3" {
				t.Fatalf("unexpected trap acceptance: %v", r.TrapOID)
			}
		case <-time.After(250 * time.Millisecond):
			if want {
				t.Fatal("valid authPriv trap not received")
			}
		}
	}
	send(dev, true)
	wrong := *dev
	wrong.AuthPassphrase = "wrong-fixture-auth"
	send(&wrong, false)
	downgrade := *dev
	downgrade.AuthProtocol = ""
	downgrade.AuthPassphrase = ""
	downgrade.PrivProtocol = ""
	downgrade.PrivPassphrase = ""
	send(&downgrade, false)
	listener.SetV3Devices(nil)
	send(dev, false)
	listener.SetV3Devices([]*Device{dev})
	send(dev, true)
}

func TestTrapWireV2InformAcknowledged(t *testing.T) {
	sink := make(channelTrapSink, 2)
	listener := NewTrapListener("fixture", "127.0.0.1:0", sink, nil, fixtureLog{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := listener.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	s := &g.GoSNMP{Target: "127.0.0.1", Port: uint16(listener.listener.LocalAddr().(*net.UDPAddr).Port),
		Version: g.Version2c, Community: "fixture", Timeout: time.Second}
	if err := s.Connect(); err != nil {
		t.Fatal(err)
	}
	defer s.Conn.Close()
	r, err := s.SendTrap(g.SnmpTrap{IsInform: true, Variables: []g.SnmpPDU{{Name: "1.3.6.1.6.3.1.1.4.1.0", Type: g.ObjectIdentifier, Value: "1.3.6.1.6.3.1.1.5.4"}}})
	if err != nil || r == nil || r.PDUType != g.GetResponse {
		t.Fatalf("inform acknowledgement: %v", err)
	}
	select {
	case <-sink:
	case <-time.After(time.Second):
		t.Fatal("inform not persisted")
	}
}
