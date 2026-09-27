package collectors

import (
	"context"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"

	"zenplus-agent/internal/config"
)

func TestAppendMissingWatchedServices(t *testing.T) {
	services := appendMissingWatchedServices([]serviceInfo{{ServiceName: "W3SVC"}}, []string{"W3SVC", "MSSQLSERVER"})
	if len(services) != 2 {
		t.Fatalf("got %d services", len(services))
	}
	if services[1].ServiceName != "MSSQLSERVER" {
		t.Fatalf("missing service not appended: %+v", services)
	}
	if services[1].State != "not_found" {
		t.Fatalf("missing service state = %q", services[1].State)
	}
}

func TestCollectIncludesWindowsServiceInventoryAndState(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("Windows service collector")
	}
	resetServiceTestState()
	t.Cleanup(resetServiceTestState)

	cfg := config.Default()
	cfg.Collectors.CPU.Enabled = false
	cfg.Collectors.Memory.Enabled = false
	cfg.Collectors.Filesystem.Enabled = false
	cfg.Collectors.DiskIO.Enabled = false
	cfg.Collectors.Network.Enabled = false
	cfg.Collectors.Processes.Enabled = false
	cfg.Collectors.EventLog.Enabled = false
	cfg.Collectors.Inventory.Enabled = false
	cfg.Collectors.Services.Enabled = true
	cfg.Collectors.Services.Watchlist = nil

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	result := Collect(ctx, cfg)
	if err := result.Errors["service_state"]; err != "" {
		t.Fatalf("service_state error: %s", err)
	}
	services, ok := result.Inventory["services"].([]map[string]any)
	if !ok || len(services) == 0 {
		t.Fatalf("service inventory missing: %#v", result.Inventory["services"])
	}
	serviceSamples := 0
	for _, metric := range result.Metrics {
		if metric.Kind == "service_state" {
			serviceSamples++
		}
	}
	if serviceSamples == 0 {
		t.Fatalf("expected service_state samples, got %d metrics", len(result.Metrics))
	}
}

func resetServiceTestState() {
	rateState.Lock()
	defer rateState.Unlock()
	rateState.services = map[string]string{}
}

func TestEventLogQueriesKeepControllerFilterPairingsAndIDs(t *testing.T) {
	filters := []config.EventLogFilter{
		{Channel: "System", Levels: []string{"Error"}, IDs: []int{41, 6008}},
		{Channel: "Application", Levels: []string{"Warning", "Critical"}, IDs: []int{1000}},
	}
	cfg := config.Default().Collectors.EventLog
	cfg.Filters = &filters
	cfg.LookbackMinutes = 5
	// These legacy fields must not broaden an authoritative filter list.
	cfg.Channels = []string{"Security"}
	cfg.Levels = []string{"Verbose"}

	queries, err := eventLogQueries(cfg)
	if err != nil {
		t.Fatal(err)
	}
	want := []eventLogQuery{
		{Channel: "System", Level: "error", XPaths: []string{
			"*[System[Level=2 and TimeCreated[timediff(@SystemTime) <= 300000] and (EventID=41 or EventID=6008)]]"}},
		{Channel: "Application", Level: "warning", XPaths: []string{
			"*[System[Level=3 and TimeCreated[timediff(@SystemTime) <= 300000] and (EventID=1000)]]"}},
		{Channel: "Application", Level: "critical", XPaths: []string{
			"*[System[Level=1 and TimeCreated[timediff(@SystemTime) <= 300000] and (EventID=1000)]]"}},
	}
	if !reflect.DeepEqual(queries, want) {
		t.Fatalf("queries = %#v\nwant %#v", queries, want)
	}
}

func TestEventLogQueriesSplitLongIDLists(t *testing.T) {
	ids := make([]int, 45)
	for i := range ids {
		ids[i] = i + 1
	}
	filters := []config.EventLogFilter{{Channel: "System", Levels: []string{"Error"}, IDs: ids}}
	cfg := config.Default().Collectors.EventLog
	cfg.Filters = &filters
	queries, err := eventLogQueries(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if len(queries) != 1 || len(queries[0].XPaths) != 3 {
		t.Fatalf("45 IDs should split into 3 XPath queries: %#v", queries)
	}
	seen := 0
	for _, xpath := range queries[0].XPaths {
		seen += strings.Count(xpath, "EventID=")
	}
	if seen != 45 {
		t.Fatalf("split queries cover %d IDs, want 45", seen)
	}
}

func TestEventLogFiltersFallBackToLegacyChannelsAndLevels(t *testing.T) {
	cfg := config.Default().Collectors.EventLog
	cfg.Filters = nil
	cfg.Channels = []string{"System", "Application"}
	cfg.Levels = []string{"Error", "Warning"}

	want := []config.EventLogFilter{
		{Channel: "System", Levels: []string{"Error", "Warning"}},
		{Channel: "Application", Levels: []string{"Error", "Warning"}},
	}
	if got := effectiveEventLogFilters(cfg); !reflect.DeepEqual(got, want) {
		t.Fatalf("effective filters = %#v, want %#v", got, want)
	}
	queries, err := eventLogQueries(cfg)
	if err != nil {
		t.Fatal(err)
	}
	var pairs []string
	for _, q := range queries {
		pairs = append(pairs, q.Channel+"/"+q.Level)
	}
	if got := strings.Join(pairs, ","); got != "System/error,System/warning,Application/error,Application/warning" {
		t.Fatalf("legacy filters produced %s", got)
	}
}

func TestExplicitEmptyEventLogFiltersDisableQueries(t *testing.T) {
	filters := []config.EventLogFilter{}
	cfg := config.Default().Collectors.EventLog
	cfg.Filters = &filters
	if got := effectiveEventLogFilters(cfg); len(got) != 0 {
		t.Fatalf("explicit empty filters produced filters: %#v", got)
	}
	queries, err := eventLogQueries(cfg)
	if err != nil {
		t.Fatal(err)
	}
	if len(queries) != 0 {
		t.Fatalf("explicit empty filters generated queries: %#v", queries)
	}
}

func TestEventLogPolicyRejectsInvalidOrOversizedFilters(t *testing.T) {
	tests := []struct {
		name    string
		filters []config.EventLogFilter
		wantErr string
	}{
		{
			name:    "empty levels",
			filters: []config.EventLogFilter{{Channel: "System"}},
			wantErr: "has no levels",
		},
		{
			name:    "non-positive event id",
			filters: []config.EventLogFilter{{Channel: "System", Levels: []string{"Error"}, IDs: []int{0}}},
			wantErr: "must be between 1",
		},
		{
			name:    "unsupported level",
			filters: []config.EventLogFilter{{Channel: "System", Levels: []string{"AuditFailure"}}},
			wantErr: "is unsupported",
		},
		{
			name:    "channel whitespace",
			filters: []config.EventLogFilter{{Channel: " System", Levels: []string{"Error"}}},
			wantErr: "leading or trailing whitespace",
		},
		{
			name: "too many filters",
			filters: func() []config.EventLogFilter {
				filters := make([]config.EventLogFilter, maxEventLogFilters+1)
				for i := range filters {
					filters[i] = config.EventLogFilter{Channel: "System", Levels: []string{"Error"}}
				}
				return filters
			}(),
			wantErr: "maximum is 32",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cfg := config.Default().Collectors.EventLog
			cfg.Filters = &tt.filters
			if _, err := eventLogQueries(cfg); err == nil || !strings.Contains(err.Error(), tt.wantErr) {
				t.Fatalf("error = %v, want containing %q", err, tt.wantErr)
			}
		})
	}
}
