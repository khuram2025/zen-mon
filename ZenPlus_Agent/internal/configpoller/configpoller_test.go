package configpoller

import (
	"reflect"
	"testing"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
)

func TestApplyControllerConfigPropagatesMetricInterval(t *testing.T) {
	current := config.Default()
	inventoryInterval := current.Collectors.Inventory.IntervalSeconds

	next := applyControllerConfig(current, model.AgentConfigResponse{
		MetricIntervalS: 30,
	})

	if next.CollectIntervalSeconds != 30 {
		t.Fatalf("collect interval = %d, want 30", next.CollectIntervalSeconds)
	}
	intervals := map[string]int{
		"cpu":        next.Collectors.CPU.IntervalSeconds,
		"memory":     next.Collectors.Memory.IntervalSeconds,
		"filesystem": next.Collectors.Filesystem.IntervalSeconds,
		"disk_io":    next.Collectors.DiskIO.IntervalSeconds,
		"network":    next.Collectors.Network.IntervalSeconds,
		"processes":  next.Collectors.Processes.IntervalSeconds,
		"services":   next.Collectors.Services.IntervalSeconds,
		"event_log":  next.Collectors.EventLog.IntervalSeconds,
	}
	for name, interval := range intervals {
		if interval != 30 {
			t.Errorf("%s interval = %d, want 30", name, interval)
		}
	}
	if next.Collectors.Inventory.IntervalSeconds != inventoryInterval {
		t.Fatalf("inventory interval = %d, want unchanged %d", next.Collectors.Inventory.IntervalSeconds, inventoryInterval)
	}
}

func TestApplyControllerConfigExplicitEmptyListsClearLocalValues(t *testing.T) {
	current := config.Default()
	current.Collectors.Processes.Watchlist = []string{"legacy.exe"}
	current.Collectors.Services.Watchlist = []string{"LegacyService"}
	current.Collectors.EventLog.Channels = []string{"System"}
	current.Collectors.EventLog.Levels = []string{"Error"}
	legacyFilters := []config.EventLogFilter{{Channel: "Legacy", Levels: []string{"Warning"}, IDs: []int{7}}}
	current.Collectors.EventLog.Filters = &legacyFilters
	current.DiskIgnore = []string{"legacy-volume"}
	current.NetworkIgnore = []string{"legacy-nic"}

	next := applyControllerConfig(current, model.AgentConfigResponse{
		ServiceWatchlist: []string{},
		ProcessWatchlist: []string{},
		EventLogFilters:  []model.EventLogFilter{},
		DiskIgnore:       []string{},
		NetworkIgnore:    []string{},
	})

	if len(next.Collectors.Services.Watchlist) != 0 {
		t.Errorf("service watchlist was not cleared: %v", next.Collectors.Services.Watchlist)
	}
	if len(next.Collectors.Processes.Watchlist) != 0 {
		t.Errorf("process watchlist was not cleared: %v", next.Collectors.Processes.Watchlist)
	}
	if len(next.Collectors.EventLog.Channels) != 0 || len(next.Collectors.EventLog.Levels) != 0 {
		t.Errorf("event-log filters were not cleared: channels=%v levels=%v", next.Collectors.EventLog.Channels, next.Collectors.EventLog.Levels)
	}
	if next.Collectors.EventLog.Filters == nil || len(*next.Collectors.EventLog.Filters) != 0 {
		t.Errorf("authoritative event-log filter list was not explicitly cleared: %#v", next.Collectors.EventLog.Filters)
	}
	if len(next.DiskIgnore) != 0 {
		t.Errorf("disk ignore list was not cleared: %v", next.DiskIgnore)
	}
	if len(next.NetworkIgnore) != 0 {
		t.Errorf("network ignore list was not cleared: %v", next.NetworkIgnore)
	}
}

func TestApplyControllerConfigAbsentListsPreserveLocalValues(t *testing.T) {
	current := config.Default()
	current.Collectors.Processes.Watchlist = []string{"local.exe"}
	current.Collectors.Services.Watchlist = []string{"LocalService"}
	current.Collectors.EventLog.Channels = []string{"System"}
	current.Collectors.EventLog.Levels = []string{"Error"}
	localFilters := []config.EventLogFilter{{Channel: "Security", Levels: []string{"Critical"}, IDs: []int{4625}}}
	current.Collectors.EventLog.Filters = &localFilters
	current.DiskIgnore = []string{"local-volume"}
	current.NetworkIgnore = []string{"local-nic"}

	next := applyControllerConfig(current, model.AgentConfigResponse{})

	if !reflect.DeepEqual(next.Collectors.Processes.Watchlist, current.Collectors.Processes.Watchlist) ||
		!reflect.DeepEqual(next.Collectors.Services.Watchlist, current.Collectors.Services.Watchlist) ||
		!reflect.DeepEqual(next.Collectors.EventLog.Filters, current.Collectors.EventLog.Filters) ||
		!reflect.DeepEqual(next.Collectors.EventLog.Channels, current.Collectors.EventLog.Channels) ||
		!reflect.DeepEqual(next.Collectors.EventLog.Levels, current.Collectors.EventLog.Levels) ||
		!reflect.DeepEqual(next.DiskIgnore, current.DiskIgnore) ||
		!reflect.DeepEqual(next.NetworkIgnore, current.NetworkIgnore) {
		t.Fatalf("absent controller lists changed local values: %#v", next)
	}
}

func TestApplyControllerConfigPreservesEventFiltersExactly(t *testing.T) {
	current := config.Default()
	remote := model.AgentConfigResponse{EventLogFilters: []model.EventLogFilter{
		{Channel: "System", Levels: []string{"Error", "Warning"}, IDs: []int{41, 6008}},
		{Channel: "Application", Levels: []string{"Warning", "Critical"}, IDs: []int{1000}},
		{Channel: "System", Levels: []string{"Error"}},
	}}
	next := applyControllerConfig(current, remote)

	want := []config.EventLogFilter{
		{Channel: "System", Levels: []string{"Error", "Warning"}, IDs: []int{41, 6008}},
		{Channel: "Application", Levels: []string{"Warning", "Critical"}, IDs: []int{1000}},
		{Channel: "System", Levels: []string{"Error"}},
	}
	if next.Collectors.EventLog.Filters == nil || !reflect.DeepEqual(*next.Collectors.EventLog.Filters, want) {
		t.Fatalf("filters = %#v, want %#v", next.Collectors.EventLog.Filters, want)
	}
	if next.Collectors.EventLog.Channels != nil || next.Collectors.EventLog.Levels != nil {
		t.Fatalf("legacy filter projection was retained: channels=%v levels=%v", next.Collectors.EventLog.Channels, next.Collectors.EventLog.Levels)
	}

	remote.EventLogFilters[0].Levels[0] = "Critical"
	remote.EventLogFilters[0].IDs[0] = 999
	if !reflect.DeepEqual(*next.Collectors.EventLog.Filters, want) {
		t.Fatal("local filters alias the controller response")
	}
}
