//go:build windows

package main

import "zenplus-agent/internal/secrets"

// hardenMachineDataTree replaces legacy permissions throughout a machine-scope
// data tree. Dashboard data remains read-only for ordinary users, while DPAPI
// credential blobs are restricted to LocalSystem, Administrators, and the
// named Windows service SID. Existing installations are migrated recursively.
func hardenMachineDataTree(root, serviceName string) error {
	return secrets.HardenMachineDataTree(root, serviceName)
}
