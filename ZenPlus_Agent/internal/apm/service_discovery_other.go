//go:build !windows

package apm

type windowsServiceInfo struct {
	Name        string
	DisplayName string
	BinaryPath  string
}

func windowsServicesByPID() map[int32]windowsServiceInfo { return nil }
