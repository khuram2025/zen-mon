package collectors

import (
	"strings"
	"testing"

	"github.com/shirou/gopsutil/v4/cpu"
)

func TestBuildCPUInventoryIncludesModelAndCounts(t *testing.T) {
	info := []cpu.InfoStat{
		{ModelName: "  Zen CPU 9000  "},
		{ModelName: "Zen   CPU 9000"},
	}
	got := buildCPUInventory(info, 16, 8)
	if got["model"] != "Zen CPU 9000" {
		t.Fatalf("CPU model = %#v", got["model"])
	}
	if got["logical_count"] != 16 || got["physical_count"] != 8 {
		t.Fatalf("CPU counts = %#v", got)
	}
}

func TestHardwareTextIsNormalizedAndBounded(t *testing.T) {
	got := normalizeHardwareText("  Model\r\n"+strings.Repeat("x", 400), 64)
	if strings.ContainsAny(got, "\r\n") {
		t.Fatalf("hardware text contains a line break: %q", got)
	}
	if len(got) > 64 {
		t.Fatalf("hardware text length = %d, max 64", len(got))
	}
}
