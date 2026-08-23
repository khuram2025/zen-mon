package netiface

import "testing"

func TestFindMatchesAliasOrDescription(t *testing.T) {
	counters := []Counter{{Name: "Ethernet 2", Description: "Contoso Adapter", InterfaceIndex: 9}}
	for _, name := range []string{"ethernet 2", "CONTOSO ADAPTER"} {
		got, ok := Find(counters, name)
		if !ok || got.InterfaceIndex != 9 {
			t.Fatalf("Find(%q) = %+v, %v", name, got, ok)
		}
	}
	if _, ok := Find(counters, "missing"); ok {
		t.Fatal("unexpected match for missing interface")
	}
}

func TestSpeedMbps(t *testing.T) {
	if got := SpeedMbps(2_500_000_000); got != 2500 {
		t.Fatalf("SpeedMbps returned %v", got)
	}
}
