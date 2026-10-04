//go:build !linux

package main

import "net"

// The kernel drop counters are read from Linux procfs; the appliance runs
// Linux, other platforms simply report them as unavailable.

func udpRcvbufErrors() (uint64, bool) { return 0, false }

func socketInode(*net.UDPConn) (string, bool) { return "", false }

func socketDrops(string) (uint64, bool) { return 0, false }

func effectiveRcvbuf(*net.UDPConn) int { return 0 }
