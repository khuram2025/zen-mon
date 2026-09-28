package snmp

import (
	"reflect"
	"testing"
)

func TestApplyLinkedCredentialFillsImportedDevice(t *testing.T) {
	// Discovery import: link only, device row at column defaults.
	d := &Device{Version: "2c", Port: 161, TimeoutMs: 2000, Retries: 2}
	c := &UdtCredential{Version: "2c", Port: 1161, Community: "s3cret", TimeoutMs: 5000, Retries: 0}
	if !ApplyLinkedCredential(d, c) {
		t.Fatal("expected linked credential to be applied")
	}
	if d.Community != "s3cret" || d.Port != 1161 || d.TimeoutMs != 5000 || d.Retries != 0 {
		t.Fatalf("unexpected device after merge: %+v", d)
	}
}

func TestApplyLinkedCredentialV3(t *testing.T) {
	d := &Device{Version: "2c", Port: 161, TimeoutMs: 2000, Retries: 2}
	c := &UdtCredential{
		Version: "3", Port: 161, V3Username: "monitor", V3Context: "vlan-10",
		AuthProtocol: "SHA", AuthPassphrase: "authpass",
		PrivProtocol: "AES", PrivPassphrase: "privpass",
		TimeoutMs: 3000, Retries: 3,
	}
	if !ApplyLinkedCredential(d, c) {
		t.Fatal("expected linked credential to be applied")
	}
	if d.Version != "3" || d.V3Username != "monitor" || d.V3Context != "vlan-10" ||
		d.AuthPassphrase != "authpass" || d.PrivPassphrase != "privpass" ||
		d.AuthProtocol != "SHA" || d.PrivProtocol != "AES" || d.Retries != 3 {
		t.Fatalf("unexpected device after merge: %+v", d)
	}
}

func TestApplyLinkedCredentialDeviceValuesWin(t *testing.T) {
	cases := []*Device{
		{Version: "2c", Port: 10161, Community: "device-own", TimeoutMs: 1000, Retries: 1},
		{Version: "3", Port: 10161, V3Username: "device-user", AuthPassphrase: "a", TimeoutMs: 1000, Retries: 1},
	}
	for _, d := range cases {
		before := *d
		c := &UdtCredential{Version: "2c", Port: 161, Community: "linked", TimeoutMs: 5000, Retries: 4}
		if ApplyLinkedCredential(d, c) {
			t.Fatalf("device with own auth must keep its values: %+v", before)
		}
		if !reflect.DeepEqual(*d, before) {
			t.Fatalf("device changed: before %+v after %+v", before, *d)
		}
	}
}

func TestApplyLinkedCredentialWithoutAuthIsIgnored(t *testing.T) {
	d := &Device{Version: "2c", Port: 161, TimeoutMs: 2000, Retries: 2}
	before := *d
	if ApplyLinkedCredential(d, &UdtCredential{Version: "3", Port: 1161}) || ApplyLinkedCredential(d, nil) {
		t.Fatal("credential without auth must not be applied")
	}
	if !reflect.DeepEqual(*d, before) {
		t.Fatalf("device changed: %+v", *d)
	}
}

func TestNormalizeOID(t *testing.T) {
	for _, in := range []string{
		"iso.3.6.1.4.1.9.1.1208", ".1.3.6.1.4.1.9.1.1208", "1.3.6.1.4.1.9.1.1208", "  .1.3.6.1.4.1.9.1.1208 ",
	} {
		if got := NormalizeOID(in); got != "1.3.6.1.4.1.9.1.1208" {
			t.Errorf("NormalizeOID(%q) = %q", in, got)
		}
	}
}

func TestClassifierMatchesAnyOIDForm(t *testing.T) {
	c := NewClassifier()
	generic := &Profile{Name: "generic", Match: MatchRules{SysObjectIDPrefixes: []string{"1.3.6.1.4.1"}}}
	cisco := &Profile{Name: "cisco-catalyst", Match: MatchRules{SysObjectIDPrefixes: []string{"iso.3.6.1.4.1.9.1"}}}
	if errs := c.SetProfiles([]*Profile{generic, cisco}); len(errs) > 0 {
		t.Fatal(errs)
	}
	for _, oid := range []string{"iso.3.6.1.4.1.9.1.1208", ".1.3.6.1.4.1.9.1.1208", "1.3.6.1.4.1.9.1.1208"} {
		if p := c.Match(oid, ""); p != cisco {
			t.Errorf("Match(%q) = %v, want cisco-catalyst", oid, p)
		}
	}
}
