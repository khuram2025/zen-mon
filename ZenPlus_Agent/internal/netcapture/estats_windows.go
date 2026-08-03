//go:build windows

package netcapture

// TCP Extended Statistics (RFC 4898) via iphlpapi.
//
// Windows keeps per-connection byte counters but does not expose them through
// any counter set or CIM class; GetPerTcpConnectionEStats is the supported
// way to read them. Stats must be switched on per connection first (they are
// off by default), which needs administrator rights — the agent runs as
// LocalSystem, and newSource degrades gracefully when it does not.

import (
	"fmt"
	"net"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	modiphlpapi = windows.NewLazySystemDLL("iphlpapi.dll")

	procGetTcpTable2              = modiphlpapi.NewProc("GetTcpTable2")
	procGetPerTcpConnectionEStats = modiphlpapi.NewProc("GetPerTcpConnectionEStats")
	procSetPerTcpConnectionEStats = modiphlpapi.NewProc("SetPerTcpConnectionEStats")
)

// TCP_ESTATS_TYPE
const tcpConnectionEstatsData = 1

type mibTCPRow2 struct {
	State        uint32
	LocalAddr    uint32
	LocalPort    uint32
	RemoteAddr   uint32
	RemotePort   uint32
	OwningPid    uint32
	OffloadState uint32
}

type mibTCPTable2 struct {
	NumEntries uint32
	Table      [1]mibTCPRow2
}

// TCP_ESTATS_DATA_ROD_v0 — read-only dynamic data. Only the first three
// fields are read; the rest is kept so the struct size matches what the API
// writes.
type tcpEstatsDataROD struct {
	DataBytesOut      uint64
	DataSegsOut       uint64
	DataBytesIn       uint64
	DataSegsIn        uint64
	SegsOut           uint64
	SegsIn            uint64
	SoftErrors        uint32
	SoftErrorReason   uint32
	SndUna            uint32
	SndNxt            uint32
	SndMax            uint32
	ThruBytesAcked    uint64
	RcvNxt            uint32
	ThruBytesReceived uint64
}

// TCP_ESTATS_DATA_RW_v0
type tcpEstatsDataRW struct {
	EnableCollection uint8
	_                [3]byte
}

type estatsKey struct {
	localIP    string
	localPort  uint32
	remoteIP   string
	remotePort uint32
}

type estatsBytes struct {
	in  uint64
	out uint64
}

type estatsReader struct {
	// enabled tracks connections we have already switched collection on for,
	// so each is only written once.
	enabled map[estatsKey]bool
}

func newEstatsReader() (*estatsReader, error) {
	if err := procGetTcpTable2.Find(); err != nil {
		return nil, fmt.Errorf("GetTcpTable2 unavailable: %w", err)
	}
	if err := procGetPerTcpConnectionEStats.Find(); err != nil {
		return nil, fmt.Errorf("GetPerTcpConnectionEStats unavailable: %w", err)
	}
	r := &estatsReader{enabled: map[estatsKey]bool{}}
	// Probe once: enabling stats fails with ERROR_ACCESS_DENIED when not
	// elevated, and we want that known up front rather than per sample.
	rows, err := tcpTable()
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		if row.State != 5 { // MIB_TCP_STATE_ESTAB
			continue
		}
		if err := enableEstats(&row); err != nil {
			return nil, fmt.Errorf("enable TCP ESTATS: %w", err)
		}
		break
	}
	return r, nil
}

func (r *estatsReader) Close() {}

// Snapshot returns cumulative byte counters for every established TCP
// connection that has ESTATS collection running.
func (r *estatsReader) Snapshot() map[estatsKey]estatsBytes {
	rows, err := tcpTable()
	if err != nil {
		return nil
	}
	out := make(map[estatsKey]estatsBytes, len(rows))
	for i := range rows {
		row := rows[i]
		if row.State != 5 {
			continue
		}
		k := estatsKey{
			localIP:    ipv4String(row.LocalAddr),
			localPort:  portFromNetworkOrder(row.LocalPort),
			remoteIP:   ipv4String(row.RemoteAddr),
			remotePort: portFromNetworkOrder(row.RemotePort),
		}
		if !r.enabled[k] {
			// Newly seen connection: turn collection on. Counters start from
			// this moment, which is what a capture window wants anyway.
			if err := enableEstats(&row); err != nil {
				continue
			}
			r.enabled[k] = true
		}
		rod, err := readEstats(&row)
		if err != nil {
			continue
		}
		out[k] = estatsBytes{in: rod.DataBytesIn, out: rod.DataBytesOut}
	}
	return out
}

func tcpTable() ([]mibTCPRow2, error) {
	var size uint32
	ret, _, _ := procGetTcpTable2.Call(0, uintptr(unsafe.Pointer(&size)), 1)
	if ret != uintptr(syscall.ERROR_INSUFFICIENT_BUFFER) && ret != 0 {
		return nil, fmt.Errorf("GetTcpTable2 sizing failed: %d", ret)
	}
	if size == 0 {
		return nil, nil
	}
	buf := make([]byte, size)
	ret, _, _ = procGetTcpTable2.Call(
		uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&size)), 1)
	if ret != 0 {
		return nil, fmt.Errorf("GetTcpTable2 failed: %d", ret)
	}
	table := (*mibTCPTable2)(unsafe.Pointer(&buf[0]))
	if table.NumEntries == 0 {
		return nil, nil
	}
	return unsafe.Slice(&table.Table[0], table.NumEntries), nil
}

func enableEstats(row *mibTCPRow2) error {
	rw := tcpEstatsDataRW{EnableCollection: 1}
	ret, _, _ := procSetPerTcpConnectionEStats.Call(
		uintptr(unsafe.Pointer(row)),
		uintptr(tcpConnectionEstatsData),
		uintptr(unsafe.Pointer(&rw)),
		0,
		uintptr(unsafe.Sizeof(rw)),
		0,
	)
	if ret != 0 {
		return syscall.Errno(ret)
	}
	return nil
}

func readEstats(row *mibTCPRow2) (*tcpEstatsDataROD, error) {
	var rod tcpEstatsDataROD
	ret, _, _ := procGetPerTcpConnectionEStats.Call(
		uintptr(unsafe.Pointer(row)),
		uintptr(tcpConnectionEstatsData),
		0, 0, 0, // RW buffer unused
		0, 0, 0, // ROS buffer unused
		uintptr(unsafe.Pointer(&rod)),
		0,
		uintptr(unsafe.Sizeof(rod)),
	)
	if ret != 0 {
		return nil, syscall.Errno(ret)
	}
	return &rod, nil
}

func ipv4String(addr uint32) string {
	// MIB addresses are already in network byte order.
	b := (*[4]byte)(unsafe.Pointer(&addr))
	return net.IPv4(b[0], b[1], b[2], b[3]).String()
}

func portFromNetworkOrder(p uint32) uint32 {
	return uint32(((p & 0xff) << 8) | ((p >> 8) & 0xff))
}
