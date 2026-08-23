//go:build windows

package netcapture

import (
	"context"
	"errors"
	"fmt"
	"net"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	gnet "github.com/shirou/gopsutil/v4/net"
	"github.com/shirou/gopsutil/v4/process"
	"golang.org/x/sys/windows"
)

const (
	socketSampleTimeout = 15 * time.Second
	processCacheTTL     = 5 * time.Second
	serviceCacheTTL     = 10 * time.Second
	localAddressTTL     = 30 * time.Second
	maxObservedSockets  = 50_000
	maxAttributionPIDs  = 2_048
	maxServiceEnumBytes = 64 << 20
)

var windowsConnectionKinds = []string{"tcp4", "tcp6", "udp4", "udp6"}

type rawConn struct {
	Protocol      string
	Kind          string
	Direction     string
	LocalIP       string
	LocalPort     uint32
	RemoteIP      string
	RemotePort    uint32
	PID           int32
	ProcessName   string
	ServiceName   string
	State         string
	BytesSent     uint64
	BytesReceived uint64
	BytesKnown    bool
}

type connectionLookupFunc func(context.Context, string) ([]gnet.ConnectionStat, error)
type processNameLookupFunc func(context.Context, int32) (string, error)

type winSource struct {
	estats        *estatsReader
	connectionFn  connectionLookupFunc
	processNameFn processNameLookupFunc
	procNames     map[int32]string
	procAt        time.Time
	services      map[int32]string
	svcAt         time.Time
	localIPs      map[string]bool
	localAt       time.Time
	mu            sync.RWMutex
}

func newSource() (source, string, bool) {
	s := &winSource{
		connectionFn:  gnet.ConnectionsWithContext,
		processNameFn: lookupProcessName,
		procNames:     map[int32]string{},
		services:      map[int32]string{},
		localIPs:      map[string]bool{},
	}
	est, err := newEstatsReader()
	if err != nil {
		// Byte attribution needs ESTATS, which needs administrator rights.
		// Endpoint and ownership capture remains useful without it.
		return s, fmt.Sprintf("per-connection byte counters unavailable (%v); "+
			"capturing connections and local endpoints without traffic volume", err), false
	}
	s.estats = est
	return s, "", true
}

type source interface {
	Sample(context.Context) ([]rawConn, error)
	Close()
}

func (s *winSource) Close() {
	if s.estats != nil {
		s.estats.Close()
	}
}

func (s *winSource) Sample(parent context.Context) ([]rawConn, error) {
	ctx, cancel := context.WithTimeout(parent, socketSampleTimeout)
	defer cancel()

	conns, err := sampleConnectionTables(ctx, s.connectionFn)
	if err != nil {
		return nil, err
	}
	s.refreshLocalAddresses(ctx)
	s.refreshProcessNames(ctx, conns)
	s.refreshServices(ctx)
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	var byConn map[estatsKey]estatsBytes
	if s.estats != nil {
		byConn = s.estats.Snapshot()
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return s.toRawConnections(conns, byConn), nil
}

type connectionTableResult struct {
	rows []gnet.ConnectionStat
	err  error
}

// lookupConnectionTable keeps the capture loop cancellation-bounded even
// though gopsutil's Windows implementation ignores the context while its IP
// Helper call is in progress. The buffered result lets a late native call
// finish without retaining the capture goroutine.
func lookupConnectionTable(ctx context.Context, lookup connectionLookupFunc, kind string) ([]gnet.ConnectionStat, error) {
	done := make(chan connectionTableResult, 1)
	go func() {
		rows, err := lookup(ctx, kind)
		done <- connectionTableResult{rows: rows, err: err}
	}()
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case result := <-done:
		return result.rows, result.err
	}
}

// sampleConnectionTables requests each address-family/protocol table
// explicitly, checks cancellation between reads, and bounds the caller even
// if one native table read is slow.
func sampleConnectionTables(ctx context.Context, lookup connectionLookupFunc) ([]gnet.ConnectionStat, error) {
	out := make([]gnet.ConnectionStat, 0, 256)
	var lookupErrors []error
	for _, kind := range windowsConnectionKinds {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		rows, err := lookupConnectionTable(ctx, lookup, kind)
		if err != nil {
			lookupErrors = append(lookupErrors, fmt.Errorf("%s: %w", kind, err))
			continue
		}
		remaining := maxObservedSockets - len(out)
		if remaining <= 0 {
			break
		}
		if len(rows) > remaining {
			rows = rows[:remaining]
		}
		out = append(out, rows...)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if len(out) == 0 && len(lookupErrors) > 0 {
		return nil, errors.Join(lookupErrors...)
	}
	return out, nil
}

func (s *winSource) toRawConnections(conns []gnet.ConnectionStat, byConn map[estatsKey]estatsBytes) []rawConn {
	listeners := tcpListeners(conns)
	localIPs := s.localAddressSnapshot()
	out := make([]rawConn, 0, len(conns))
	for _, c := range conns {
		protocol := socketProtocol(c.Type)
		if protocol == "" || c.Laddr.Port == 0 {
			continue
		}
		state := strings.ToLower(strings.TrimSpace(c.Status))
		hasPeer := hasRemotePeer(c.Raddr)
		kind := "connection"
		direction := "unknown"
		switch protocol {
		case "udp":
			// Windows' UDP owner table deliberately has no remote endpoint.
			// Preserve the local bind and PID without inventing a peer.
			kind = "endpoint"
			direction = "local"
			if state == "" {
				state = "bound"
			}
		case "tcp":
			switch {
			case state == "listen":
				kind = "listener"
				direction = "inbound"
			case !hasPeer:
				kind = "endpoint"
				direction = "local"
			default:
				direction = connectionDirection(c, listeners, localIPs)
			}
			if state == "" {
				state = "unknown"
			}
		}

		rc := rawConn{
			Protocol:    protocol,
			Kind:        kind,
			Direction:   direction,
			LocalIP:     canonicalIP(c.Laddr.IP),
			LocalPort:   c.Laddr.Port,
			PID:         c.Pid,
			ProcessName: s.processName(c.Pid),
			ServiceName: s.serviceName(c.Pid),
			State:       state,
		}
		if hasPeer {
			rc.RemoteIP = canonicalIP(c.Raddr.IP)
			rc.RemotePort = c.Raddr.Port
		}
		if protocol == "tcp" && kind == "connection" && byConn != nil {
			k := estatsKey{
				localIP:    rc.LocalIP,
				localPort:  rc.LocalPort,
				remoteIP:   rc.RemoteIP,
				remotePort: rc.RemotePort,
			}
			if b, ok := byConn[k]; ok {
				rc.BytesSent = b.out
				rc.BytesReceived = b.in
				rc.BytesKnown = true
			}
		}
		out = append(out, rc)
	}
	return out
}

func socketProtocol(socketType uint32) string {
	switch socketType {
	case syscall.SOCK_STREAM:
		return "tcp"
	case syscall.SOCK_DGRAM:
		return "udp"
	default:
		return ""
	}
}

func hasRemotePeer(addr gnet.Addr) bool {
	return addr.Port > 0 && !isUnspecifiedAddress(addr.IP)
}

func isUnspecifiedAddress(value string) bool {
	value = strings.TrimSpace(value)
	if value == "" {
		return true
	}
	ip := net.ParseIP(value)
	return ip != nil && ip.IsUnspecified()
}

func canonicalIP(value string) string {
	value = strings.TrimSpace(value)
	if ip := net.ParseIP(value); ip != nil {
		return ip.String()
	}
	return value
}

func tcpListeners(conns []gnet.ConnectionStat) []gnet.ConnectionStat {
	out := make([]gnet.ConnectionStat, 0)
	for _, c := range conns {
		if c.Type == syscall.SOCK_STREAM && strings.EqualFold(c.Status, "listen") && c.Laddr.Port > 0 {
			out = append(out, c)
		}
	}
	return out
}

func connectionDirection(c gnet.ConnectionStat, listeners []gnet.ConnectionStat, localIPs map[string]bool) string {
	remoteIP := canonicalIP(c.Raddr.IP)
	if ip := net.ParseIP(remoteIP); ip != nil && ip.IsLoopback() {
		return "local"
	}
	if localIPs[remoteIP] {
		return "local"
	}
	if c.Pid <= 0 {
		return "unknown"
	}
	localIP := canonicalIP(c.Laddr.IP)
	matchesListener := func(listener gnet.ConnectionStat) bool {
		if listener.Family != c.Family || listener.Laddr.Port != c.Laddr.Port {
			return false
		}
		listenerIP := canonicalIP(listener.Laddr.IP)
		return isUnspecifiedAddress(listenerIP) || listenerIP == localIP
	}
	// Same-PID ownership is the strongest signal, so prefer it when present.
	for _, listener := range listeners {
		if listener.Pid == c.Pid && matchesListener(listener) {
			return "inbound"
		}
	}
	// Windows HTTP.sys and other kernel brokers can own an accepted connection
	// under a different local PID than the listening service. An exact local
	// family/address/port match is still a stronger inbound signal than a port
	// number heuristic, and makes no claim about the remote owner.
	for _, listener := range listeners {
		if matchesListener(listener) {
			return "inbound"
		}
	}
	// The local process owns this peer row and no same-PID listener matches.
	// This says nothing about who owns the remote address.
	return "outbound"
}

func (s *winSource) processName(pid int32) string {
	if pid <= 0 {
		return ""
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.procNames[pid]
}

func (s *winSource) serviceName(pid int32) string {
	if pid <= 0 {
		return ""
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.services[pid]
}

func lookupProcessName(ctx context.Context, pid int32) (string, error) {
	p, err := process.NewProcessWithContext(ctx, pid)
	if err != nil {
		return "", err
	}
	return p.NameWithContext(ctx)
}

func ownedPIDs(conns []gnet.ConnectionStat) []int32 {
	seen := make(map[int32]struct{})
	for _, conn := range conns {
		if conn.Pid > 0 {
			seen[conn.Pid] = struct{}{}
		}
	}
	pids := make([]int32, 0, len(seen))
	for pid := range seen {
		pids = append(pids, pid)
	}
	sort.Slice(pids, func(i, j int) bool { return pids[i] < pids[j] })
	if len(pids) > maxAttributionPIDs {
		pids = pids[:maxAttributionPIDs]
	}
	return pids
}

// refreshProcessNames resolves only PIDs present in the current socket table.
// PID 0 is intentionally excluded: TIME_WAIT rows commonly lose ownership,
// and labeling them as "System Idle Process" is misleading.
func (s *winSource) refreshProcessNames(ctx context.Context, conns []gnet.ConnectionStat) {
	s.mu.RLock()
	fresh := time.Since(s.procAt) < processCacheTTL
	s.mu.RUnlock()
	if fresh {
		return
	}
	next := make(map[int32]string)
	for _, pid := range ownedPIDs(conns) {
		if ctx.Err() != nil {
			return
		}
		name, err := s.processNameFn(ctx, pid)
		if err == nil && strings.TrimSpace(name) != "" {
			next[pid] = strings.TrimSpace(name)
			continue
		}
		// PID 4 is Windows' real kernel System process. Keep that attribution,
		// but never reinterpret it as a user-mode IIS worker or remote owner.
		if pid == 4 {
			next[pid] = "System"
		}
	}
	if ctx.Err() != nil {
		return
	}
	s.mu.Lock()
	s.procNames = next
	s.procAt = time.Now()
	s.mu.Unlock()
}

// refreshServices maps a PID to one service name when unique. Shared service
// host PIDs are explicitly labelled as candidate services rather than falsely
// claiming that one service owns the socket.
func (s *winSource) refreshServices(ctx context.Context) {
	s.mu.RLock()
	fresh := time.Since(s.svcAt) < serviceCacheTTL
	s.mu.RUnlock()
	if fresh || ctx.Err() != nil {
		return
	}
	next, err := enumServicePIDs()
	if err != nil || ctx.Err() != nil {
		return
	}
	s.mu.Lock()
	s.services = next
	s.svcAt = time.Now()
	s.mu.Unlock()
}

func (s *winSource) refreshLocalAddresses(ctx context.Context) {
	s.mu.RLock()
	fresh := time.Since(s.localAt) < localAddressTTL
	s.mu.RUnlock()
	if fresh || ctx.Err() != nil {
		return
	}
	next := map[string]bool{}
	if addrs, err := net.InterfaceAddrs(); err == nil {
		for _, addr := range addrs {
			value := addr.String()
			if ip, _, err := net.ParseCIDR(value); err == nil {
				next[ip.String()] = true
				continue
			}
			if ip := net.ParseIP(value); ip != nil {
				next[ip.String()] = true
			}
		}
	}
	if ctx.Err() != nil {
		return
	}
	s.mu.Lock()
	s.localIPs = next
	s.localAt = time.Now()
	s.mu.Unlock()
}

func (s *winSource) localAddressSnapshot() map[string]bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make(map[string]bool, len(s.localIPs))
	for ip := range s.localIPs {
		out[ip] = true
	}
	return out
}

func enumServicePIDs() (map[int32]string, error) {
	m, err := windows.OpenSCManager(nil, nil, windows.SC_MANAGER_ENUMERATE_SERVICE)
	if err != nil {
		return nil, err
	}
	defer windows.CloseServiceHandle(m)

	var bytesNeeded, servicesReturned, resume uint32
	err = windows.EnumServicesStatusEx(m, windows.SC_ENUM_PROCESS_INFO,
		windows.SERVICE_WIN32, windows.SERVICE_ACTIVE,
		nil, 0, &bytesNeeded, &servicesReturned, &resume, nil)
	if err != nil && !errors.Is(err, windows.ERROR_MORE_DATA) {
		return nil, err
	}
	if bytesNeeded == 0 {
		return map[int32]string{}, nil
	}

	groups := make(map[int32][]string)
	for page := 0; page < 8 && bytesNeeded > 0; page++ {
		if bytesNeeded > maxServiceEnumBytes {
			return nil, fmt.Errorf("service enumeration requested an excessive %d-byte buffer", bytesNeeded)
		}
		buf := make([]byte, bytesNeeded)
		servicesReturned = 0
		err = windows.EnumServicesStatusEx(m, windows.SC_ENUM_PROCESS_INFO,
			windows.SERVICE_WIN32, windows.SERVICE_ACTIVE,
			&buf[0], uint32(len(buf)), &bytesNeeded, &servicesReturned, &resume, nil)
		if err != nil && !errors.Is(err, windows.ERROR_MORE_DATA) {
			return nil, err
		}
		if servicesReturned > 0 {
			services := unsafe.Slice(
				(*windows.ENUM_SERVICE_STATUS_PROCESS)(unsafe.Pointer(&buf[0])), servicesReturned)
			for _, service := range services {
				pid := int32(service.ServiceStatusProcess.ProcessId)
				if pid <= 0 {
					continue
				}
				name := strings.TrimSpace(windows.UTF16PtrToString(service.ServiceName))
				if name != "" {
					groups[pid] = append(groups[pid], name)
				}
			}
		}
		if err == nil || resume == 0 {
			break
		}
	}

	out := make(map[int32]string, len(groups))
	for pid, names := range groups {
		out[pid] = formatServiceNames(names)
	}
	return out, nil
}

func formatServiceNames(names []string) string {
	unique := make(map[string]string)
	for _, name := range names {
		name = strings.TrimSpace(name)
		if name != "" {
			unique[strings.ToLower(name)] = name
		}
	}
	names = names[:0]
	for _, name := range unique {
		names = append(names, name)
	}
	sort.Slice(names, func(i, j int) bool { return strings.ToLower(names[i]) < strings.ToLower(names[j]) })
	if len(names) == 0 {
		return ""
	}
	label := names[0]
	if len(names) > 1 {
		label = "shared: " + strings.Join(names, ", ")
	}
	runes := []rune(label)
	if len(runes) > 255 {
		label = string(runes[:252]) + "..."
	}
	return label
}
