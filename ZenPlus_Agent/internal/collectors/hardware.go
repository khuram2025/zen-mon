package collectors

import (
	"context"
	"sort"
	"strings"

	"github.com/shirou/gopsutil/v4/cpu"
	"github.com/shirou/gopsutil/v4/mem"
)

const (
	maxPhysicalDiskInventory = 64
	maxHardwareTextBytes     = 255
)

func collectHardwareInventory(ctx context.Context, r *Result) {
	hardware := make(map[string]any, 3)

	info, infoErr := cpu.InfoWithContext(ctx)
	logical, logicalErr := cpu.CountsWithContext(ctx, true)
	physical, physicalErr := cpu.CountsWithContext(ctx, false)
	cpuInfo := buildCPUInventory(info, logical, physical)
	if len(cpuInfo) > 0 {
		hardware["cpu"] = cpuInfo
	}
	if infoErr != nil {
		r.Errors["cpu_inventory_model"] = infoErr.Error()
	}
	if logicalErr != nil {
		r.Errors["cpu_inventory_logical_count"] = logicalErr.Error()
	}
	if physicalErr != nil {
		r.Errors["cpu_inventory_physical_count"] = physicalErr.Error()
	}

	memory, err := mem.VirtualMemoryWithContext(ctx)
	if err != nil {
		r.Errors["memory_inventory"] = err.Error()
	} else if memory != nil && memory.Total > 0 {
		hardware["memory"] = map[string]any{"total_physical_bytes": memory.Total}
	}

	physicalDisks, err := collectPhysicalDiskInventory(ctx)
	if err != nil {
		r.Errors["physical_disk_inventory"] = err.Error()
	} else if len(physicalDisks) > 0 {
		hardware["physical_disks"] = physicalDisks
	}

	if len(hardware) > 0 {
		r.Inventory["hardware"] = hardware
	}
}

func buildCPUInventory(info []cpu.InfoStat, logical, physical int) map[string]any {
	out := make(map[string]any, 3)
	models := make(map[string]struct{})
	for _, item := range info {
		model := normalizeHardwareText(item.ModelName, maxHardwareTextBytes)
		if model == "" {
			model = normalizeHardwareText(item.Model, maxHardwareTextBytes)
		}
		if model != "" {
			models[model] = struct{}{}
		}
	}
	if len(models) > 0 {
		ordered := make([]string, 0, len(models))
		for model := range models {
			ordered = append(ordered, model)
		}
		sort.Strings(ordered)
		out["model"] = truncateUTF8Bytes(strings.Join(ordered, "; "), maxHardwareTextBytes)
	}
	if logical > 0 {
		out["logical_count"] = logical
	}
	if physical > 0 {
		out["physical_count"] = physical
	}
	return out
}

func normalizeHardwareText(value string, maxBytes int) string {
	return truncateUTF8Bytes(strings.Join(strings.Fields(value), " "), maxBytes)
}
