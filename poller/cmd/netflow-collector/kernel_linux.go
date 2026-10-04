package main

import (
	"bufio"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"syscall"
)

// udpRcvbufErrors reads the system-wide UDP receive-buffer overflow counters
// (/proc/net/snmp Udp RcvbufErrors + /proc/net/snmp6 Udp6RcvbufErrors):
// datagrams the kernel discarded because a socket buffer was full. They are
// host-wide, so the collector reports them on its process row and uses the
// per-socket drop count (socketDrops) for its own health.
func udpRcvbufErrors() (uint64, bool) {
	var total uint64
	ok := false
	if v, found := snmpCounter("/proc/net/snmp", "Udp:", "RcvbufErrors"); found {
		total += v
		ok = true
	}
	if v, found := snmp6Counter("/proc/net/snmp6", "Udp6RcvbufErrors"); found {
		total += v
		ok = true
	}
	return total, ok
}

// snmpCounter parses the header/value line pairs of /proc/net/snmp.
func snmpCounter(path, prefix, name string) (uint64, bool) {
	f, err := os.Open(path)
	if err != nil {
		return 0, false
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	var header []string
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) == 0 || fields[0] != prefix {
			continue
		}
		if header == nil {
			header = fields
			continue
		}
		for i, h := range header {
			if h == name && i < len(fields) {
				v, err := strconv.ParseUint(fields[i], 10, 64)
				return v, err == nil
			}
		}
		return 0, false
	}
	return 0, false
}

func snmp6Counter(path, name string) (uint64, bool) {
	f, err := os.Open(path)
	if err != nil {
		return 0, false
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) == 2 && fields[0] == name {
			v, err := strconv.ParseUint(fields[1], 10, 64)
			return v, err == nil
		}
	}
	return 0, false
}

// socketInode returns the inode of the UDP socket, the key of its row in
// /proc/net/udp{,6}.
func socketInode(conn *net.UDPConn) (string, bool) {
	raw, err := conn.SyscallConn()
	if err != nil {
		return "", false
	}
	var inode string
	_ = raw.Control(func(fd uintptr) {
		target, err := os.Readlink(fmt.Sprintf("/proc/self/fd/%d", fd))
		if err == nil && strings.HasPrefix(target, "socket:[") {
			inode = strings.TrimSuffix(strings.TrimPrefix(target, "socket:["), "]")
		}
	})
	return inode, inode != ""
}

// socketDrops reads the kernel's drop counter for this very socket (the last
// column of its /proc/net/udp{,6} row): datagrams lost because the collector
// did not read fast enough.
func socketDrops(inode string) (uint64, bool) {
	for _, path := range []string{"/proc/net/udp", "/proc/net/udp6"} {
		f, err := os.Open(path)
		if err != nil {
			continue
		}
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			fields := strings.Fields(sc.Text())
			// sl local rem st tx:rx tr:when retrnsmt uid timeout inode ref pointer drops
			if len(fields) >= 13 && fields[9] == inode {
				f.Close()
				v, err := strconv.ParseUint(fields[len(fields)-1], 10, 64)
				return v, err == nil
			}
		}
		f.Close()
	}
	return 0, false
}

// effectiveRcvbuf reports the receive buffer the kernel actually granted
// (Linux doubles the requested value for bookkeeping and caps the request at
// net.core.rmem_max).
func effectiveRcvbuf(conn *net.UDPConn) int {
	raw, err := conn.SyscallConn()
	if err != nil {
		return 0
	}
	size := 0
	_ = raw.Control(func(fd uintptr) {
		size, _ = syscall.GetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF)
	})
	return size
}
