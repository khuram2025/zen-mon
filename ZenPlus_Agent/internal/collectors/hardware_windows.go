//go:build windows

package collectors

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

type physicalDiskInfo struct {
	Index        int    `json:"index"`
	DeviceID     string `json:"device_id"`
	Model        string `json:"model"`
	Manufacturer string `json:"manufacturer"`
	Interface    string `json:"interface_type"`
	MediaType    string `json:"media_type"`
	SizeBytes    uint64 `json:"size_bytes"`
	Status       string `json:"status"`
}

func collectPhysicalDiskInventory(ctx context.Context) ([]map[string]any, error) {
	out, err := runPowerShellJSON(ctx, physicalDiskScript())
	if err != nil {
		return nil, err
	}
	var payload struct {
		PhysicalDisks []physicalDiskInfo `json:"physical_disks"`
		Error         string             `json:"error"`
	}
	if err := json.Unmarshal(out, &payload); err != nil {
		return nil, fmt.Errorf("decode physical disk inventory: %w", err)
	}
	if message := normalizeHardwareText(payload.Error, 512); message != "" {
		return nil, fmt.Errorf("query Win32_DiskDrive: %s", message)
	}
	return normalizePhysicalDisks(payload.PhysicalDisks), nil
}

func normalizePhysicalDisks(disks []physicalDiskInfo) []map[string]any {
	sort.SliceStable(disks, func(i, j int) bool {
		if disks[i].Index == disks[j].Index {
			return strings.ToLower(disks[i].DeviceID) < strings.ToLower(disks[j].DeviceID)
		}
		return disks[i].Index < disks[j].Index
	})
	if len(disks) > maxPhysicalDiskInventory {
		disks = disks[:maxPhysicalDiskInventory]
	}
	out := make([]map[string]any, 0, len(disks))
	for _, item := range disks {
		deviceID := normalizeHardwareText(item.DeviceID, 128)
		model := normalizeHardwareText(item.Model, maxHardwareTextBytes)
		if deviceID == "" && model == "" {
			continue
		}
		disk := map[string]any{
			"index": item.Index,
		}
		if deviceID != "" {
			disk["device_id"] = deviceID
		}
		if model != "" {
			disk["model"] = model
		}
		if value := normalizeHardwareText(item.Manufacturer, maxHardwareTextBytes); value != "" {
			disk["manufacturer"] = value
		}
		if value := normalizeHardwareText(item.Interface, 64); value != "" {
			disk["interface_type"] = value
		}
		if value := normalizeHardwareText(item.MediaType, 64); value != "" {
			disk["media_type"] = value
		}
		if item.SizeBytes > 0 {
			disk["size_bytes"] = item.SizeBytes
		}
		if value := normalizeHardwareText(item.Status, 64); value != "" {
			disk["status"] = strings.ToLower(value)
		}
		out = append(out, disk)
	}
	return out
}

func physicalDiskScript() string {
	return `
$items = @()
$errorMessage = ''
try {
  $disks = @(Get-CimInstance Win32_DiskDrive -ErrorAction Stop | Sort-Object Index, DeviceID | Select-Object -First 64)
  foreach ($disk in $disks) {
    if ($null -eq $disk) { continue }
    $sizeBytes = [uint64]0
    try { $sizeBytes = [uint64]$disk.Size } catch {}
    $items += [pscustomobject]@{
      index          = [int]$disk.Index
      device_id      = [string]$disk.DeviceID
      model          = [string]$disk.Model
      manufacturer   = [string]$disk.Manufacturer
      interface_type = [string]$disk.InterfaceType
      media_type     = [string]$disk.MediaType
      size_bytes     = $sizeBytes
      status         = [string]$disk.Status
    }
  }
} catch {
  $errorMessage = [string]$_.Exception.Message
}
[pscustomobject]@{ physical_disks = @($items); error = $errorMessage } | ConvertTo-Json -Depth 4 -Compress
`
}
