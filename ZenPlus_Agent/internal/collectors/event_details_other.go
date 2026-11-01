//go:build !windows

package collectors

import "context"

func readNativeDetailedEvents(context.Context, detailQuery, string, int, int) (detailRead, error) {
	return detailRead{}, errWindowsOnly
}
