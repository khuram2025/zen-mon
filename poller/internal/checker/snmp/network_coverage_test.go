package snmp

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestStandardNetworkGroupsAreBoundedAndValid(t *testing.T) {
	groups := StandardNetworkOidGroups()
	if len(groups) != 7 {
		t.Fatalf("standard group count = %d, want 7", len(groups))
	}
	for _, group := range groups {
		if group.IntervalSeconds < 120 {
			t.Errorf("group %s interval = %d, want at least 120", group.Key, group.IntervalSeconds)
		}
	}
	raw, err := json.Marshal(groups)
	if err != nil {
		t.Fatal(err)
	}
	parsed, errs := ParseOidGroups(raw)
	if len(errs) != 0 || len(parsed) != len(groups) {
		t.Fatalf("ParseOidGroups = %d groups, %v errors", len(parsed), errs)
	}
}

func TestNetworkDeviceTypeFilter(t *testing.T) {
	for _, kind := range []string{"router", "switch", "firewall", "access_point"} {
		if !IsNetworkDeviceType(kind) {
			t.Errorf("%s should receive standard network groups", kind)
		}
	}
	for _, kind := range []string{"server", "printer", "other", ""} {
		if IsNetworkDeviceType(kind) {
			t.Errorf("%s should not receive standard network groups", kind)
		}
	}
}

func TestStandardGroupsDoNotDuplicateVendorCoverage(t *testing.T) {
	existing := []OidGroup{{
		Key: "vendor_bgp", Kind: "table",
		Metrics: []OidMetric{{Key: "vendor_bgp_state", OID: "1.3.6.1.2.1.15.3.1.2"}},
	}}
	merged := MergeStandardNetworkOidGroups(existing)
	for _, group := range merged {
		if group.Key == "std_bgp_peers" {
			t.Fatal("standard BGP duplicated vendor BGP coverage")
		}
	}
	if len(merged) != 7 { // existing + six remaining standard groups
		t.Fatalf("merged group count = %d, want 7", len(merged))
	}
}

func TestTemplateGroupCadence(t *testing.T) {
	c := NewCollector("test", nil)
	deviceID := uuid.New()
	group := &OidGroup{Key: "slow", IntervalSeconds: 120}
	now := time.Unix(1000, 0)
	if !c.templateGroupDue(deviceID, group, now) {
		t.Fatal("new group should be due")
	}
	c.markTemplateGroupPolled(deviceID, group, now)
	if c.templateGroupDue(deviceID, group, now.Add(119*time.Second)) {
		t.Fatal("group became due before its interval")
	}
	if !c.templateGroupDue(deviceID, group, now.Add(120*time.Second)) {
		t.Fatal("group was not due at its interval")
	}
}

func TestCanonicalNetworkVendorMetrics(t *testing.T) {
	samples := []MetricSample{
		{Key: "tpl_aruba_cx_cpu_5min_1", Value: 12},
		{Key: "tpl_aruba_cx_cpu_5min_2", Value: 72},
		{Key: "tpl_aruba_cx_memory_1", Value: 45},
		{Key: "tpl_aruba_cx_memory_2", Value: 68},
	}
	values := metricValues(canonicalVendorMetrics(samples))
	if values["cpu"] != 72 || values["memory"] != 68 {
		t.Fatalf("Aruba CX canonical values = cpu %v memory %v", values["cpu"], values["memory"])
	}

	const totalKB = 16 * 1024 * 1024
	const availableKB = 6 * 1024 * 1024
	values = metricValues(canonicalVendorMetrics([]MetricSample{
		{Key: "tpl_dell_total_mem_kb", Value: totalKB},
		{Key: "tpl_dell_available_mem_kb", Value: availableKB},
	}))
	if values["memory"] != 62.5 {
		t.Fatalf("Dell cache-aware memory = %v, want 62.5", values["memory"])
	}
}

func TestPrimaryCiscoMemoryPoolDoesNotSumDomains(t *testing.T) {
	pool, ok := primaryMemoryPool([]MetricSample{
		{Key: "tpl_cisco_mem_used_1", Value: 80},
		{Key: "tpl_cisco_mem_free_1", Value: 20}, // tiny, intentionally busy pool
		{Key: "tpl_cisco_mem_used_2", Value: 600},
		{Key: "tpl_cisco_mem_free_2", Value: 400},
	}, []string{"tpl_cisco_mem_used_"}, []string{"tpl_cisco_mem_free_"})
	if !ok || pool.pct != 60 || pool.used != 600 || pool.free != 400 {
		t.Fatalf("selected pool = %#v, ok=%v", pool, ok)
	}
}
