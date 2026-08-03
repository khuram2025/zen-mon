//go:build !windows

package appstate

func ReadServiceSnapshot() ServiceSnapshot {
	return ServiceSnapshot{Name: "ZenPlusAgent"}
}
