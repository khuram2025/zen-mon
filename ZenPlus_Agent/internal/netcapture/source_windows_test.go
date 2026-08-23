//go:build windows

package netcapture

import (
	"context"
	"errors"
	"reflect"
	"syscall"
	"testing"
	"time"

	gnet "github.com/shirou/gopsutil/v4/net"
)

func TestWindowsSocketClassificationIncludesTCPUDPIPv4IPv6(t *testing.T) {
	rows := []gnet.ConnectionStat{
		{Family: syscall.AF_INET, Type: syscall.SOCK_STREAM,
			Laddr: gnet.Addr{IP: "0.0.0.0", Port: 8080}, Status: "LISTEN", Pid: 100},
		{Family: syscall.AF_INET, Type: syscall.SOCK_STREAM,
			Laddr: gnet.Addr{IP: "192.0.2.10", Port: 8080},
			Raddr: gnet.Addr{IP: "203.0.113.5", Port: 55000}, Status: "ESTABLISHED", Pid: 4},
		{Family: syscall.AF_INET, Type: syscall.SOCK_STREAM,
			Laddr: gnet.Addr{IP: "192.0.2.10", Port: 62000},
			Raddr: gnet.Addr{IP: "198.51.100.20", Port: 443}, Status: "ESTABLISHED", Pid: 200},
		{Family: syscall.AF_INET6, Type: syscall.SOCK_STREAM,
			Laddr: gnet.Addr{IP: "::1", Port: 62001},
			Raddr: gnet.Addr{IP: "::1", Port: 9000}, Status: "ESTABLISHED", Pid: 201},
		{Family: syscall.AF_INET, Type: syscall.SOCK_DGRAM,
			Laddr: gnet.Addr{IP: "0.0.0.0", Port: 53}, Pid: 300},
		{Family: syscall.AF_INET6, Type: syscall.SOCK_DGRAM,
			Laddr: gnet.Addr{IP: "::", Port: 5353}, Pid: 301},
		{Family: syscall.AF_INET, Type: syscall.SOCK_STREAM,
			Laddr: gnet.Addr{IP: "192.0.2.10", Port: 62002},
			Raddr: gnet.Addr{IP: "198.51.100.30", Port: 80}, Status: "TIME_WAIT", Pid: 0},
		{Family: syscall.AF_INET, Type: 99,
			Laddr: gnet.Addr{IP: "192.0.2.10", Port: 9999}, Pid: 400},
	}
	s := &winSource{
		procNames: map[int32]string{0: "System Idle Process", 100: "server.exe", 200: "client.exe"},
		services:  map[int32]string{100: "WebService"},
		localIPs:  map[string]bool{"192.0.2.10": true, "::1": true},
	}
	got := s.toRawConnections(rows, nil)
	if len(got) != 7 {
		t.Fatalf("expected seven observable sockets, got %d: %+v", len(got), got)
	}

	assertSocket := func(index int, protocol, kind, direction, remoteIP string, remotePort uint32) {
		t.Helper()
		row := got[index]
		if row.Protocol != protocol || row.Kind != kind || row.Direction != direction ||
			row.RemoteIP != remoteIP || row.RemotePort != remotePort {
			t.Fatalf("socket %d classification mismatch: %+v", index, row)
		}
	}
	assertSocket(0, "tcp", "listener", "inbound", "", 0)
	assertSocket(1, "tcp", "connection", "inbound", "203.0.113.5", 55000)
	assertSocket(2, "tcp", "connection", "outbound", "198.51.100.20", 443)
	assertSocket(3, "tcp", "connection", "local", "::1", 9000)
	assertSocket(4, "udp", "endpoint", "local", "", 0)
	assertSocket(5, "udp", "endpoint", "local", "", 0)
	assertSocket(6, "tcp", "connection", "unknown", "198.51.100.30", 80)
	if got[4].State != "bound" || got[5].State != "bound" {
		t.Fatalf("UDP endpoints should be marked bound: %+v %+v", got[4], got[5])
	}
	if got[6].ProcessName != "" || got[6].ServiceName != "" {
		t.Fatalf("PID 0 must not be attributed to a process or service: %+v", got[6])
	}
	if got[0].ProcessName != "server.exe" || got[0].ServiceName != "WebService" {
		t.Fatalf("local ownership attribution was lost: %+v", got[0])
	}
}

func TestWindowsPeerlessTCPEndpointIsLocal(t *testing.T) {
	s := &winSource{procNames: map[int32]string{}, services: map[int32]string{}, localIPs: map[string]bool{}}
	got := s.toRawConnections([]gnet.ConnectionStat{{
		Family: syscall.AF_INET6, Type: syscall.SOCK_STREAM,
		Laddr: gnet.Addr{IP: "::", Port: 12345}, Status: "CLOSE_WAIT", Pid: 55,
	}}, nil)
	if len(got) != 1 || got[0].Kind != "endpoint" || got[0].Direction != "local" {
		t.Fatalf("unexpected peerless TCP classification: %+v", got)
	}
}

func TestSampleConnectionTablesEnumeratesAllFamilies(t *testing.T) {
	var calls []string
	lookup := func(_ context.Context, kind string) ([]gnet.ConnectionStat, error) {
		calls = append(calls, kind)
		return []gnet.ConnectionStat{{Type: syscall.SOCK_DGRAM,
			Laddr: gnet.Addr{IP: "0.0.0.0", Port: uint32(1000 + len(calls))}}}, nil
	}
	rows, err := sampleConnectionTables(context.Background(), lookup)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(calls, []string{"tcp4", "tcp6", "udp4", "udp6"}) {
		t.Fatalf("unexpected table calls: %v", calls)
	}
	if len(rows) != 4 {
		t.Fatalf("expected one row from every table, got %d", len(rows))
	}
}

func TestSampleConnectionTablesHonorsCancellationBetweenFamilies(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	var calls []string
	lookup := func(_ context.Context, kind string) ([]gnet.ConnectionStat, error) {
		calls = append(calls, kind)
		cancel()
		return []gnet.ConnectionStat{{}}, nil
	}
	_, err := sampleConnectionTables(ctx, lookup)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("expected context cancellation, got %v", err)
	}
	if !reflect.DeepEqual(calls, []string{"tcp4"}) {
		t.Fatalf("enumeration continued after cancellation: %v", calls)
	}
}

func TestSampleConnectionTablesBoundsBlockingNativeLookup(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	started := make(chan struct{})
	release := make(chan struct{})
	defer close(release)
	go func() {
		<-started
		cancel()
	}()
	lookup := func(_ context.Context, _ string) ([]gnet.ConnectionStat, error) {
		close(started)
		<-release
		return nil, nil
	}

	before := time.Now()
	_, err := sampleConnectionTables(ctx, lookup)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("expected context cancellation, got %v", err)
	}
	if elapsed := time.Since(before); elapsed > time.Second {
		t.Fatalf("blocking native lookup held cancellation for %s", elapsed)
	}
}

func TestRefreshProcessNamesUsesOnlyOwnedPIDs(t *testing.T) {
	var calls []int32
	s := &winSource{
		procNames: map[int32]string{},
		processNameFn: func(_ context.Context, pid int32) (string, error) {
			calls = append(calls, pid)
			if pid == 4 {
				return "", errors.New("protected")
			}
			return "worker.exe", nil
		},
	}
	s.refreshProcessNames(context.Background(), []gnet.ConnectionStat{
		{Pid: 42}, {Pid: 0}, {Pid: 4}, {Pid: 42},
	})
	if !reflect.DeepEqual(calls, []int32{4, 42}) {
		t.Fatalf("lookups should be unique, sorted, and exclude PID 0: %v", calls)
	}
	if s.processName(0) != "" || s.processName(4) != "System" || s.processName(42) != "worker.exe" {
		t.Fatalf("unexpected process cache: %+v", s.procNames)
	}
}

func TestFormatServiceNamesMarksSharedOwnership(t *testing.T) {
	if got := formatServiceNames([]string{"W32Time", "Dhcp", "Dhcp", ""}); got != "shared: Dhcp, W32Time" {
		t.Fatalf("unexpected shared-service label: %q", got)
	}
	if got := formatServiceNames([]string{"Dnscache"}); got != "Dnscache" {
		t.Fatalf("unexpected single-service label: %q", got)
	}
}
