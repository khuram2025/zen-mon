package snmp

import (
	"math"
	"testing"
	"time"
)

func TestF5SystemMemoryUsesSystemPair(t *testing.T) {
	s := []MetricSample{{Key: "tpl_f5_system_mem_used", Value: 10787326592}, {Key: "tpl_f5_system_mem_total", Value: 16826568704}, {Key: "tpl_f5_other_mem_used", Value: 9358487552}, {Key: "tpl_f5_other_mem_total", Value: 10325397504}, {Key: "tpl_f5_swap_used", Value: 990171136}, {Key: "tpl_f5_swap_total", Value: 1048571904}}
	v := metricValues(canonicalVendorMetrics(s))
	assertClose(t, "system", v["memory"], 10787326592.0/16826568704*100)
	assertClose(t, "non-TMM", v["f5_host_memory_pct"], 9358487552.0/10325397504*100)
	assertClose(t, "swap", v["f5_swap_memory_pct"], 990171136.0/1048571904*100)
	if v["memory"] == v["f5_host_memory_pct"] {
		t.Fatal("domain was used as overall RAM")
	}
	s[0].Timestamp = time.Now()
	if _, ok := metricValues(canonicalVendorMetrics(s))["f5_system_memory_pct"]; ok {
		t.Fatal("mixed timestamps used")
	}
}
func TestF5MemoryInvalidReadings(t *testing.T) {
	for _, p := range [][2]float64{{1, 0}, {-1, 100}, {101, 100}, {math.NaN(), 100}, {1, math.Inf(1)}} {
		if utilizationPct(p[0], p[1]) >= 0 {
			t.Fatal("invalid RAM ratio accepted", p)
		}
	}
	if utilizationPct(0, 100) != 0 {
		t.Fatal("valid zero lost")
	}
}
