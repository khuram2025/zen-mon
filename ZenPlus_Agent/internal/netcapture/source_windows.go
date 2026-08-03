//go:build windows

package netcapture

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"
	"unsafe"

	"github.com/shirou/gopsutil/v4/net"
	"github.com/shirou/gopsutil/v4/process"
	"golang.org/x/sys/windows"
)

type rawConn struct {
	Protocol      string
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

type winSource struct {
	estats    *estatsReader
	procNames map[int32]string
	procAt    time.Time
	services  map[int32]string
	svcAt     time.Time
	mu        sync.Mutex
}

func newSource() (source, string, bool) {
	s := &winSource{procNames: map[int32]string{}, services: map[int32]string{}}
	est, err := newEstatsReader()
	if err != nil {
		// Byte attribution needs ESTATS, which needs administrator rights.
		// The capture still yields who-talks-to-whom, so degrade rather than
		// fail — but say so, so the UI does not present 0 as "no traffic".
		return s, fmt.Sprintf("per-connection byte counters unavailable (%v); "+
			"capturing connections without traffic volume", err), false
	}
	s.estats = est
	return s, "", true
}

type source interface {
	Sample() ([]rawConn, error)
	Close()
}

func (s *winSource) Close() {
	if s.estats != nil {
		s.estats.Close()
	}
}

func (s *winSource) Sample() ([]rawConn, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	conns, err := net.ConnectionsWithContext(ctx, "inet")
	if err != nil {
		return nil, err
	}
	s.refreshProcessNames(ctx)
	s.refreshServices(ctx)

	var byConn map[estatsKey]estatsBytes
	if s.estats != nil {
		byConn = s.estats.Snapshot()
	}

	out := make([]rawConn, 0, len(conns))
	for _, c := range conns {
		proto := "tcp"
		if c.Type == 2 { // SOCK_DGRAM
			proto = "udp"
		}
		rc := rawConn{
			Protocol:    proto,
			LocalIP:     c.Laddr.IP,
			LocalPort:   c.Laddr.Port,
			RemoteIP:    c.Raddr.IP,
			RemotePort:  c.Raddr.Port,
			PID:         c.Pid,
			State:       strings.ToLower(c.Status),
			ProcessName: s.processName(c.Pid),
			ServiceName: s.serviceName(c.Pid),
		}
		// Listeners have no peer; they are not conversations.
		if rc.RemoteIP == "" || rc.RemoteIP == "0.0.0.0" || rc.RemotePort == 0 {
			continue
		}
		if proto == "tcp" && byConn != nil {
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
	return out, nil
}

func (s *winSource) processName(pid int32) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.procNames[pid]
}

func (s *winSource) serviceName(pid int32) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.services[pid]
}

// refreshProcessNames rebuilds the PID→name map at most every 5s; enumerating
// processes on every 2s sample would dominate the capture's own cost.
func (s *winSource) refreshProcessNames(ctx context.Context) {
	s.mu.Lock()
	fresh := time.Since(s.procAt) < 5*time.Second
	s.mu.Unlock()
	if fresh {
		return
	}
	procs, err := process.ProcessesWithContext(ctx)
	if err != nil {
		return
	}
	next := make(map[int32]string, len(procs))
	for _, p := range procs {
		if name, err := p.NameWithContext(ctx); err == nil && name != "" {
			next[p.Pid] = name
		}
	}
	s.mu.Lock()
	s.procNames = next
	s.procAt = time.Now()
	s.mu.Unlock()
}

// refreshServices maps PIDs to Windows service names so a flow owned by
// svchost.exe can be attributed to the service actually using the socket.
func (s *winSource) refreshServices(ctx context.Context) {
	s.mu.Lock()
	fresh := time.Since(s.svcAt) < 30*time.Second
	s.mu.Unlock()
	if fresh {
		return
	}
	next := enumServicePIDs()
	s.mu.Lock()
	if len(next) > 0 {
		s.services = next
	}
	s.svcAt = time.Now()
	s.mu.Unlock()
}

func enumServicePIDs() map[int32]string {
	m, err := windows.OpenSCManager(nil, nil, windows.SC_MANAGER_ENUMERATE_SERVICE)
	if err != nil {
		return nil
	}
	defer windows.CloseServiceHandle(m)

	var bytesNeeded, servicesReturned, resume uint32
	_ = windows.EnumServicesStatusEx(m, windows.SC_ENUM_PROCESS_INFO,
		windows.SERVICE_WIN32, windows.SERVICE_STATE_ALL,
		nil, 0, &bytesNeeded, &servicesReturned, &resume, nil)
	if bytesNeeded == 0 {
		return nil
	}
	buf := make([]byte, bytesNeeded)
	if err := windows.EnumServicesStatusEx(m, windows.SC_ENUM_PROCESS_INFO,
		windows.SERVICE_WIN32, windows.SERVICE_STATE_ALL,
		&buf[0], uint32(len(buf)), &bytesNeeded, &servicesReturned, &resume, nil); err != nil {
		return nil
	}
	out := make(map[int32]string, servicesReturned)
	services := unsafe.Slice(
		(*windows.ENUM_SERVICE_STATUS_PROCESS)(unsafe.Pointer(&buf[0])), servicesReturned)
	for _, svc := range services {
		if svc.ServiceStatusProcess.ProcessId == 0 {
			continue
		}
		name := windows.UTF16PtrToString(svc.ServiceName)
		pid := int32(svc.ServiceStatusProcess.ProcessId)
		// A shared svchost hosts several services; keep them all rather than
		// letting the last one win silently.
		if existing, ok := out[pid]; ok && existing != "" {
			if !strings.Contains(existing, name) {
				out[pid] = existing + ", " + name
			}
			continue
		}
		out[pid] = name
	}
	return out
}
