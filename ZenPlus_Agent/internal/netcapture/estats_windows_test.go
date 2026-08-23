//go:build windows

package netcapture

import (
	"testing"
	"unsafe"
)

func TestTCPStatsRWLayoutMatchesWindowsBoolean(t *testing.T) {
	if got := unsafe.Sizeof(tcpEstatsDataRW{}); got != 1 {
		t.Fatalf("TCP_ESTATS_DATA_RW_v0 must be one BOOLEAN byte, got %d", got)
	}
}

func TestTCP6RowLayoutMatchesWindowsABI(t *testing.T) {
	var row mibTCP6Row
	checks := []struct {
		name string
		got  uintptr
		want uintptr
	}{
		{"State", unsafe.Offsetof(row.State), 0},
		{"LocalAddr", unsafe.Offsetof(row.LocalAddr), 4},
		{"LocalScopeID", unsafe.Offsetof(row.LocalScopeID), 20},
		{"LocalPort", unsafe.Offsetof(row.LocalPort), 24},
		{"RemoteAddr", unsafe.Offsetof(row.RemoteAddr), 28},
		{"RemoteScopeID", unsafe.Offsetof(row.RemoteScopeID), 44},
		{"RemotePort", unsafe.Offsetof(row.RemotePort), 48},
		{"size", unsafe.Sizeof(row), 52},
	}
	for _, check := range checks {
		if check.got != check.want {
			t.Fatalf("MIB_TCP6ROW %s offset/size = %d, want %d", check.name, check.got, check.want)
		}
	}
}

func TestTCP6TableCanBeRead(t *testing.T) {
	if err := procGetTcp6Table.Find(); err != nil {
		t.Skipf("GetTcp6Table unavailable: %v", err)
	}
	rows, err := tcp6Table()
	if err != nil {
		t.Fatal(err)
	}
	for _, row := range rows {
		if row.State < 1 || row.State > 12 {
			t.Fatalf("invalid TCP state suggests an ABI mismatch: %+v", row)
		}
	}
}
