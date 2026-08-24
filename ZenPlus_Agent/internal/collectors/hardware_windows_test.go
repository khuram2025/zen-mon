//go:build windows

package collectors

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"
)

func TestNormalizePhysicalDisksIsBoundedSortedAndOmitsSerials(t *testing.T) {
	disks := make([]physicalDiskInfo, 0, maxPhysicalDiskInventory+8)
	for index := maxPhysicalDiskInventory + 7; index >= 0; index-- {
		disks = append(disks, physicalDiskInfo{
			Index: index, DeviceID: fmt.Sprintf(`\\.\PHYSICALDRIVE%d`, index),
			Model: "  Example   Disk  ", SizeBytes: uint64(index+1) * 1_000_000,
			Status: " OK ",
		})
	}
	got := normalizePhysicalDisks(disks)
	if len(got) != maxPhysicalDiskInventory {
		t.Fatalf("physical disk count = %d, max %d", len(got), maxPhysicalDiskInventory)
	}
	for index, item := range got {
		if item["index"] != index {
			t.Fatalf("physical disks are not sorted: index %d item %#v", index, item)
		}
		if item["model"] != "Example Disk" || item["status"] != "ok" {
			t.Fatalf("physical disk fields were not normalized: %#v", item)
		}
		if _, exists := item["serial_number"]; exists {
			t.Fatalf("physical disk serial number must not be collected: %#v", item)
		}
	}
}

func TestPhysicalDiskScriptDoesNotRequestSerialNumber(t *testing.T) {
	if strings.Contains(strings.ToLower(physicalDiskScript()), "serialnumber") {
		t.Fatal("physical disk inventory script requests a device serial number")
	}
}

func TestCollectHardwareInventoryIncludesWindowsCPUAndMemory(t *testing.T) {
	result := Result{Inventory: map[string]any{}, Errors: map[string]string{}}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	collectHardwareInventory(ctx, &result)
	physicalDiskUnavailable := false
	for name, message := range result.Errors {
		normalizedMessage := strings.ToLower(message)
		if name == "physical_disk_inventory" && strings.Contains(normalizedMessage, "access") && strings.Contains(normalizedMessage, "denied") {
			// Some application-control test sandboxes deny CIM access to an
			// unelevated generated test binary. The production machine service
			// runs under SYSTEM; keep CPU/memory coverage useful on those hosts.
			physicalDiskUnavailable = true
			continue
		}
		if strings.Contains(name, "inventory") {
			t.Fatalf("%s: %s", name, message)
		}
	}
	hardware, ok := result.Inventory["hardware"].(map[string]any)
	if !ok {
		t.Fatalf("hardware inventory missing: %#v", result.Inventory)
	}
	cpuInfo, ok := hardware["cpu"].(map[string]any)
	if !ok || cpuInfo["model"] == "" || cpuInfo["logical_count"] == nil || cpuInfo["physical_count"] == nil {
		t.Fatalf("CPU inventory incomplete: %#v", hardware["cpu"])
	}
	memory, ok := hardware["memory"].(map[string]any)
	if !ok || memory["total_physical_bytes"] == nil {
		t.Fatalf("memory inventory incomplete: %#v", hardware["memory"])
	}
	if physicalDiskUnavailable {
		t.Log("physical disk CIM inventory unavailable to the unelevated test process")
		return
	}
	disks, ok := hardware["physical_disks"].([]map[string]any)
	if !ok || len(disks) == 0 {
		t.Fatalf("physical disk inventory incomplete: %#v", hardware["physical_disks"])
	}
}
