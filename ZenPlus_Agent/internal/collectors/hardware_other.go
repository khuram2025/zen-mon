//go:build !windows

package collectors

import "context"

func collectPhysicalDiskInventory(context.Context) ([]map[string]any, error) {
	return nil, nil
}
