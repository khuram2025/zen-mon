//go:build !windows

package collectors

import "errors"

var errWindowsOnly = errors.New("this collector is only available on Windows")

func readSystemCounters() (map[string]float64, error) { return nil, errWindowsOnly }

func listServices() ([]serviceInfo, error) { return nil, errWindowsOnly }

func countEvents(eventLogQuery) (int, []int, error) { return 0, nil, errWindowsOnly }

func installedSoftware() ([]map[string]any, error) { return nil, errWindowsOnly }
