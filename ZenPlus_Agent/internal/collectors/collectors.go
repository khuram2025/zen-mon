package collectors

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/shirou/gopsutil/v4/cpu"
	"github.com/shirou/gopsutil/v4/disk"
	"github.com/shirou/gopsutil/v4/host"
	"github.com/shirou/gopsutil/v4/mem"
	gnet "github.com/shirou/gopsutil/v4/net"
	"github.com/shirou/gopsutil/v4/process"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/netiface"
)

type Result struct {
	Metrics   []model.Metric
	Inventory map[string]any
	Events    []model.EventSummary
	Errors    map[string]string
}

var rateState = struct {
	sync.Mutex
	diskAt   time.Time
	disks    map[string]disk.IOCountersStat
	netAt    time.Time
	nets     map[string]gnet.IOCountersStat
	services map[string]string
}{
	disks:    map[string]disk.IOCountersStat{},
	nets:     map[string]gnet.IOCountersStat{},
	services: map[string]string{},
}

// lastRun tracks when each collector last executed so the per-collector
// interval_seconds in the policy is honoured. Without this every collector
// ran on the shortest (collect) tick, re-spawning PowerShell for the
// registry-wide software inventory once a minute instead of once per its
// configured 6-hour interval.
var lastRun = struct {
	sync.Mutex
	at map[string]time.Time
}{at: map[string]time.Time{}}

// procCPUKey identifies a process across samples. The creation time guards
// against PID reuse attributing a dead process's CPU time to a new one.
type procCPUKey struct {
	PID       int32
	CreatedMs int64
}

// procState holds the previous cumulative CPU time per process so usage can
// be reported as a rate over the sampling interval. gopsutil's
// Process.CPUPercent() is CPU time divided by process *lifetime*, which for
// a long-running process is a lifetime average, not current load — a process
// busy at boot and idle since would still rank at the top.
var procState = struct {
	sync.Mutex
	at     time.Time
	cpu    map[procCPUKey]float64
	numCPU int
}{cpu: map[procCPUKey]float64{}, numCPU: runtime.NumCPU()}

// due reports whether a collector configured for every interval seconds
// should run now, and records the run. interval <= 0 means "every tick".
func due(name string, interval int, now time.Time) bool {
	if interval <= 0 {
		return true
	}
	lastRun.Lock()
	defer lastRun.Unlock()
	last, seen := lastRun.at[name]
	// Fire on the first tick so a freshly started agent reports immediately.
	if seen && now.Sub(last) < time.Duration(interval)*time.Second {
		return false
	}
	lastRun.at[name] = now
	return true
}

// ResetSchedule clears collector run history. Used when the policy changes
// so new intervals take effect from the next tick.
func ResetSchedule() {
	lastRun.Lock()
	lastRun.at = map[string]time.Time{}
	lastRun.Unlock()
}

type serviceInfo struct {
	ServiceName string `json:"service_name"`
	DisplayName string `json:"display_name"`
	State       string `json:"state"`
	StartMode   string `json:"start_mode"`
	PID         int    `json:"pid"`
	ExitCode    int    `json:"exit_code"`
	Description string `json:"description"`
}

func Collect(ctx context.Context, cfg config.Config) Result {
	now := time.Now().UTC()
	r := Result{
		Metrics:   []model.Metric{},
		Inventory: map[string]any{},
		Events:    []model.EventSummary{},
		Errors:    map[string]string{},
	}
	add := func(kind string, data map[string]any) {
		r.Metrics = append(r.Metrics, model.Metric{
			Kind:      kind,
			Timestamp: now,
			Data:      data,
		})
	}
	if cfg.Collectors.CPU.Enabled && due("cpu", cfg.Collectors.CPU.IntervalSeconds, now) {
		collectCPU(ctx, add, r.Errors)
	}
	if cfg.Collectors.Memory.Enabled && due("memory", cfg.Collectors.Memory.IntervalSeconds, now) {
		collectMemory(ctx, add, r.Errors)
	}
	if cfg.Collectors.Filesystem.Enabled && due("filesystem", cfg.Collectors.Filesystem.IntervalSeconds, now) {
		collectFilesystem(ctx, cfg.DiskIgnore, add, r.Errors, r.Inventory)
	}
	if cfg.Collectors.DiskIO.Enabled && due("disk_io", cfg.Collectors.DiskIO.IntervalSeconds, now) {
		collectDiskIO(ctx, add, r.Errors)
	}
	if cfg.Collectors.Network.Enabled && due("network", cfg.Collectors.Network.IntervalSeconds, now) {
		collectNetwork(ctx, cfg.NetworkIgnore, add, r.Errors, r.Inventory)
	}
	if cfg.Collectors.Processes.Enabled && due("process", cfg.Collectors.Processes.IntervalSeconds, now) {
		topN := cfg.Collectors.Processes.TopN
		if cfg.Limits.MaxProcessCount > 0 && (topN <= 0 || topN > cfg.Limits.MaxProcessCount) {
			topN = cfg.Limits.MaxProcessCount
		}
		collectProcesses(ctx, topN, cfg.Collectors.Processes.Watchlist, add, r.Errors)
	}
	if cfg.Collectors.Services.Enabled && due("service_state", cfg.Collectors.Services.IntervalSeconds, now) {
		collectServices(ctx, cfg.Collectors.Services.Watchlist, add, r.Errors, r.Inventory)
	}
	if cfg.Collectors.EventLog.Enabled && due("event_log", cfg.Collectors.EventLog.IntervalSeconds, now) {
		collectEventLog(ctx, cfg.Collectors.EventLog, add, r.Errors)
	}
	if cfg.Collectors.Inventory.Enabled && due("inventory", cfg.Collectors.Inventory.IntervalSeconds, now) {
		collectInventory(ctx, &r)
	}
	return r
}

// cpuState keeps the previous CPU time counters so each collection reports
// utilisation averaged over the interval since the last one. That needs no
// sampling sleep (the collector used to block for a second every cycle) and
// does not miss load between samples.
var cpuState struct {
	sync.Mutex
	perCore []cpu.TimesStat
	total   cpu.TimesStat
	taken   bool
}

// firstCPUSampleWindow is only used for the very first collection, when there
// is no previous reading to compare against.
const firstCPUSampleWindow = 500 * time.Millisecond

func collectCPU(ctx context.Context, add func(string, map[string]any), errs map[string]string) {
	perCore, total, err := readCPUTimes(ctx)
	if err != nil {
		errs["cpu"] = err.Error()
		return
	}
	cpuState.Lock()
	previousPerCore, previousTotal, havePrevious := cpuState.perCore, cpuState.total, cpuState.taken
	cpuState.perCore, cpuState.total, cpuState.taken = perCore, total, true
	cpuState.Unlock()
	if !havePrevious || len(previousPerCore) != len(perCore) {
		previousPerCore, previousTotal = perCore, total
		select {
		case <-ctx.Done():
			errs["cpu"] = ctx.Err().Error()
			return
		case <-time.After(firstCPUSampleWindow):
		}
		if perCore, total, err = readCPUTimes(ctx); err != nil {
			errs["cpu"] = err.Error()
			return
		}
		cpuState.Lock()
		cpuState.perCore, cpuState.total = perCore, total
		cpuState.Unlock()
	}

	per := make([]float64, len(perCore))
	for i := range perCore {
		per[i] = busyPct(previousPerCore[i], perCore[i])
	}
	totalPct := average(per)
	data := map[string]any{
		"cpu_total_pct": totalPct,
		"cpu_idle_pct":  clampPct(100 - totalPct),
	}
	if len(per) > 0 {
		data["per_core"] = per
	}
	if elapsed := total.Total() - previousTotal.Total(); elapsed > 0 {
		data["cpu_user_pct"] = clampPct((total.User - previousTotal.User) / elapsed * 100)
		data["cpu_system_pct"] = clampPct((total.System - previousTotal.System) / elapsed * 100)
	}
	if runtime.GOOS == "windows" {
		data["cpu_iowait_pct"] = 0.0
		if counters, err := readSystemCounters(); err == nil {
			if queue, ok := counters["load_1"]; ok {
				data["load_1"] = queue
			}
		}
	}
	add("cpu", data)
}

func readCPUTimes(ctx context.Context) ([]cpu.TimesStat, cpu.TimesStat, error) {
	perCore, err := cpu.TimesWithContext(ctx, true)
	if err != nil {
		return nil, cpu.TimesStat{}, err
	}
	totals, err := cpu.TimesWithContext(ctx, false)
	if err != nil || len(totals) == 0 {
		return nil, cpu.TimesStat{}, fmt.Errorf("read total CPU times: %v", err)
	}
	return perCore, totals[0], nil
}

// busyPct is the share of elapsed CPU time spent outside idle and I/O wait.
func busyPct(before, after cpu.TimesStat) float64 {
	elapsed := after.Total() - before.Total()
	if elapsed <= 0 {
		return 0
	}
	idle := (after.Idle + after.Iowait) - (before.Idle + before.Iowait)
	return clampPct((elapsed - idle) / elapsed * 100)
}

func collectMemory(ctx context.Context, add func(string, map[string]any), errs map[string]string) {
	vm, err := mem.VirtualMemoryWithContext(ctx)
	if err != nil {
		errs["memory"] = err.Error()
		return
	}
	used := vm.Used
	if used == 0 && vm.Total > vm.Available {
		used = vm.Total - vm.Available
	}
	data := map[string]any{
		"total_bytes":     int64(vm.Total),
		"used_bytes":      int64(used),
		"available_bytes": int64(vm.Available),
		"cached_bytes":    int64(vm.Cached),
		"used_pct":        vm.UsedPercent,
	}
	if runtime.GOOS == "windows" {
		if counters, err := readSystemCounters(); err == nil {
			for _, key := range []string{"cached_bytes", "committed_bytes"} {
				if value, ok := counters[key]; ok {
					data[key] = int64(value)
				}
			}
		}
	}
	if sw, err := mem.SwapMemoryWithContext(ctx); err == nil && sw != nil {
		data["swap_total_bytes"] = int64(sw.Total)
		data["swap_used_bytes"] = int64(sw.Used)
	}
	add("memory", data)
}

func collectFilesystem(ctx context.Context, ignore []string, add func(string, map[string]any), errs map[string]string, inv map[string]any) {
	parts, err := disk.PartitionsWithContext(ctx, true)
	if err != nil {
		errs["filesystem"] = err.Error()
		return
	}
	filesystems := []map[string]any{}
	for _, p := range parts {
		if ignored(ignore, p.Mountpoint, p.Device) {
			continue
		}
		usage, err := disk.UsageWithContext(ctx, p.Mountpoint)
		if err != nil {
			continue
		}
		data := map[string]any{
			"mount":       p.Mountpoint,
			"fs_type":     p.Fstype,
			"total_bytes": int64(usage.Total),
			"used_bytes":  int64(usage.Used),
			"free_bytes":  int64(usage.Free),
			"used_pct":    usage.UsedPercent,
		}
		add("filesystem", data)
		filesystems = append(filesystems, map[string]any{
			"mount":       p.Mountpoint,
			"fs_type":     p.Fstype,
			"device":      p.Device,
			"total_bytes": int64(usage.Total),
			"used_bytes":  int64(usage.Used),
			"free_bytes":  int64(usage.Free),
			"used_pct":    usage.UsedPercent,
		})
	}
	if len(filesystems) > 0 {
		inv["filesystems"] = filesystems
	}
}

func collectDiskIO(ctx context.Context, add func(string, map[string]any), errs map[string]string) {
	counters, err := disk.IOCountersWithContext(ctx)
	if err != nil {
		errs["disk_io"] = err.Error()
		return
	}
	now := time.Now().UTC()
	rateState.Lock()
	defer rateState.Unlock()
	dt := now.Sub(rateState.diskAt).Seconds()
	for name, c := range counters {
		data := map[string]any{
			"device":       name,
			"queue_length": float64(c.IopsInProgress),
		}
		if prev, ok := rateState.disks[name]; ok && dt > 0 {
			readOps := diff(c.ReadCount, prev.ReadCount)
			writeOps := diff(c.WriteCount, prev.WriteCount)
			data["read_bytes_ps"] = float64(diff(c.ReadBytes, prev.ReadBytes)) / dt
			data["write_bytes_ps"] = float64(diff(c.WriteBytes, prev.WriteBytes)) / dt
			data["read_iops"] = float64(readOps) / dt
			data["write_iops"] = float64(writeOps) / dt
			data["util_pct"] = clampPct(float64(diff(c.IoTime, prev.IoTime)) / (dt * 10))
			if readOps > 0 {
				data["avg_read_ms"] = float64(diff(c.ReadTime, prev.ReadTime)) / float64(readOps)
			}
			if writeOps > 0 {
				data["avg_write_ms"] = float64(diff(c.WriteTime, prev.WriteTime)) / float64(writeOps)
			}
		} else {
			data["read_bytes_ps"] = float64(0)
			data["write_bytes_ps"] = float64(0)
			data["read_iops"] = float64(0)
			data["write_iops"] = float64(0)
		}
		add("disk_io", data)
	}
	rateState.disks = cloneDiskCounters(counters)
	rateState.diskAt = now
}

func collectNetwork(ctx context.Context, ignore []string, add func(string, map[string]any), errs map[string]string, inv map[string]any) {
	nativeCounters, nativeErr := netiface.Snapshot(ctx)
	nativeByName := make(map[string]netiface.Counter, len(nativeCounters))
	for _, counter := range nativeCounters {
		nativeByName[strings.ToLower(counter.Name)] = counter
	}
	ifaces, err := gnet.InterfacesWithContext(ctx)
	if err == nil {
		items := make([]map[string]any, 0, len(ifaces))
		for _, iface := range ifaces {
			if ignored(ignore, iface.Name) {
				continue
			}
			ips := make([]string, 0, len(iface.Addrs))
			for _, addr := range iface.Addrs {
				if addr.Addr != "" {
					ips = append(ips, addr.Addr)
				}
			}
			item := map[string]any{
				"if_name":      iface.Name,
				"mac_address":  iface.HardwareAddr,
				"ip_addresses": ips,
				"is_up":        hasFlag(iface.Flags, "up"),
				"mtu":          iface.MTU,
			}
			if native, ok := nativeByName[strings.ToLower(iface.Name)]; ok {
				linkSpeed := native.ReceiveLinkSpeedBPS
				if native.TransmitLinkSpeedBPS > linkSpeed {
					linkSpeed = native.TransmitLinkSpeedBPS
				}
				item["interface_index"] = native.InterfaceIndex
				item["interface_id"] = native.InterfaceID
				item["description"] = native.Description
				item["receive_link_speed_bps"] = native.ReceiveLinkSpeedBPS
				item["transmit_link_speed_bps"] = native.TransmitLinkSpeedBPS
				item["speed_mbps"] = netiface.SpeedMbps(linkSpeed)
				item["is_up"] = native.IsUp
			}
			items = append(items, item)
		}
		if len(items) > 0 {
			inv["network_interfaces"] = items
		}
	}
	if nativeErr != nil {
		errs["network_interface_details"] = nativeErr.Error()
	}
	counters, err := gnet.IOCountersWithContext(ctx, true)
	if err != nil {
		errs["network"] = err.Error()
		return
	}
	now := time.Now().UTC()
	rateState.Lock()
	defer rateState.Unlock()
	dt := now.Sub(rateState.netAt).Seconds()
	for _, c := range counters {
		if ignored(ignore, c.Name) {
			continue
		}
		data := map[string]any{
			"if_name": c.Name,
		}
		if prev, ok := rateState.nets[c.Name]; ok && dt > 0 {
			data["rx_bytes_ps"] = float64(diff(c.BytesRecv, prev.BytesRecv)) / dt
			data["tx_bytes_ps"] = float64(diff(c.BytesSent, prev.BytesSent)) / dt
			data["rx_packets_ps"] = float64(diff(c.PacketsRecv, prev.PacketsRecv)) / dt
			data["tx_packets_ps"] = float64(diff(c.PacketsSent, prev.PacketsSent)) / dt
			data["rx_errors_ps"] = float64(diff(c.Errin, prev.Errin)) / dt
			data["tx_errors_ps"] = float64(diff(c.Errout, prev.Errout)) / dt
			data["rx_dropped_ps"] = float64(diff(c.Dropin, prev.Dropin)) / dt
			data["tx_dropped_ps"] = float64(diff(c.Dropout, prev.Dropout)) / dt
		} else {
			data["rx_bytes_ps"] = float64(0)
			data["tx_bytes_ps"] = float64(0)
			data["rx_packets_ps"] = float64(0)
			data["tx_packets_ps"] = float64(0)
			data["rx_errors_ps"] = float64(0)
			data["tx_errors_ps"] = float64(0)
			data["rx_dropped_ps"] = float64(0)
			data["tx_dropped_ps"] = float64(0)
		}
		if ifaceUp, ok := interfaceUp(ifaces, c.Name); ok {
			data["is_up"] = ifaceUp
		}
		add("network", data)
	}
	rateState.nets = cloneNetCounters(counters)
	rateState.netAt = now
}

func collectProcesses(ctx context.Context, topN int, watchlist []string, add func(string, map[string]any), errs map[string]string) {
	if topN <= 0 {
		topN = 10
	}
	procs, err := process.ProcessesWithContext(ctx)
	if err != nil {
		errs["process"] = err.Error()
		return
	}
	now := time.Now().UTC()
	procState.Lock()
	prevAt := procState.at
	prev := procState.cpu
	numCPU := procState.numCPU
	if numCPU <= 0 {
		numCPU = 1
	}
	dt := now.Sub(prevAt).Seconds()
	current := make(map[procCPUKey]float64, len(procs))

	items := make([]processSample, 0, len(procs))
	for _, p := range procs {
		name, _ := p.NameWithContext(ctx)
		name = truncateUTF8Bytes(strings.TrimSpace(name), maxProcessTextBytes)
		if name == "" {
			continue
		}
		memInfo, _ := p.MemoryInfoWithContext(ctx)
		threads, _ := p.NumThreadsWithContext(ctx)
		handles, _ := p.NumFDsWithContext(ctx)
		userName, _ := p.UsernameWithContext(ctx)
		var rss uint64
		if memInfo != nil {
			rss = memInfo.RSS
		}

		// CPU as a rate over the sampling interval, normalised to the whole
		// machine (0-100 across all cores) so it is comparable with
		// cpu_total_pct on the dashboard.
		var cpuPct float64
		createdMs, _ := p.CreateTimeWithContext(ctx)
		key := procCPUKey{PID: p.Pid, CreatedMs: createdMs}
		if times, err := p.TimesWithContext(ctx); err == nil && times != nil {
			busy := times.User + times.System
			current[key] = busy
			if before, ok := prev[key]; ok && dt > 0 && busy >= before {
				cpuPct = clampPct((busy - before) / dt / float64(numCPU) * 100)
			} else if !prevAt.IsZero() {
				cpuPct = 0
			} else {
				// First sample after start: no baseline yet, fall back to the
				// lifetime average so the very first report is not all zeros.
				lifetime, _ := p.CPUPercentWithContext(ctx)
				cpuPct = clampPct(lifetime / float64(numCPU))
			}
		}
		items = append(items, processSample{
			process: p, PID: p.Pid, Name: name, CPUPercent: cpuPct, RSS: rss,
			Threads: threads, Handles: handles,
			UserName:  truncateUTF8Bytes(strings.TrimSpace(userName), maxProcessTextBytes),
			CreatedMs: createdMs,
		})
	}
	procState.cpu = current
	procState.at = now
	procState.Unlock()
	selected := map[int32]processSample{}
	byCPU := append([]processSample(nil), items...)
	sort.Slice(byCPU, func(i, j int) bool {
		if byCPU[i].CPUPercent == byCPU[j].CPUPercent {
			if byCPU[i].RSS == byCPU[j].RSS {
				return byCPU[i].PID < byCPU[j].PID
			}
			return byCPU[i].RSS > byCPU[j].RSS
		}
		return byCPU[i].CPUPercent > byCPU[j].CPUPercent
	})
	for _, item := range firstN(byCPU, topN) {
		selected[item.PID] = item
	}
	byMem := append([]processSample(nil), items...)
	sort.Slice(byMem, func(i, j int) bool {
		if byMem[i].RSS == byMem[j].RSS {
			return byMem[i].PID < byMem[j].PID
		}
		return byMem[i].RSS > byMem[j].RSS
	})
	for _, item := range firstN(byMem, topN) {
		selected[item.PID] = item
	}
	watched := normalizedSet(watchlist)
	for _, item := range items {
		if watched[strings.ToLower(item.Name)] {
			selected[item.PID] = item
		}
	}
	ordered := make([]processSample, 0, len(selected))
	for _, item := range selected {
		ordered = append(ordered, item)
	}
	sort.Slice(ordered, func(i, j int) bool {
		if ordered[i].CPUPercent == ordered[j].CPUPercent {
			if ordered[i].RSS == ordered[j].RSS {
				if strings.EqualFold(ordered[i].Name, ordered[j].Name) {
					return ordered[i].PID < ordered[j].PID
				}
				return strings.ToLower(ordered[i].Name) < strings.ToLower(ordered[j].Name)
			}
			return ordered[i].RSS > ordered[j].RSS
		}
		return ordered[i].CPUPercent > ordered[j].CPUPercent
	})
	for _, item := range ordered {
		data := map[string]any{
			"process_name": item.Name,
			"pid":          int(item.PID),
			"cpu_pct":      item.CPUPercent,
			"memory_bytes": int64(item.RSS),
			"thread_count": int(item.Threads),
			"handle_count": int(item.Handles),
			"user_name":    item.UserName,
			"state":        "running",
			"running":      true,
			"watchlisted":  watched[strings.ToLower(item.Name)],
		}
		if startedAt := processStartedAt(item.CreatedMs); startedAt != "" {
			data["started_at"] = startedAt
		}
		if item.process != nil {
			// Never export raw argument values: the shape helper retains only a
			// small allowlist of option names and replaces every value/path.
			if argv, err := item.process.CmdlineSliceWithContext(ctx); err == nil {
				if commandShape := safeProcessCommandLine(item.Name, argv); commandShape != "" {
					data["cmdline"] = commandShape
				}
			}
		}
		add("process", data)
	}
	for _, name := range missingWatchedProcesses(watchlist, items) {
		add("process", map[string]any{
			"process_name": name,
			"pid":          0,
			"cpu_pct":      float64(0),
			"memory_bytes": int64(0),
			"thread_count": 0,
			"handle_count": 0,
			"user_name":    "",
			"state":        "not_running",
			"running":      false,
			"watchlisted":  true,
		})
	}
}

func collectServices(ctx context.Context, watchlist []string, add func(string, map[string]any), errs map[string]string, inv map[string]any) {
	services, err := listServices()
	if err != nil {
		errs["service_state"] = err.Error()
		return
	}
	services = appendMissingWatchedServices(services, watchlist)
	if len(services) > 0 {
		inv["services"] = serviceInventory(services)
	}
	watched := normalizedSet(watchlist)
	current := make(map[string]string, len(services))
	for _, svc := range services {
		if svc.ServiceName == "" {
			continue
		}
		current[strings.ToLower(svc.ServiceName)] = serviceSignature(svc)
	}
	rateState.Lock()
	firstSnapshot := len(rateState.services) == 0
	previous := cloneStringMap(rateState.services)
	rateState.services = current
	rateState.Unlock()

	for _, svc := range services {
		name := svc.ServiceName
		key := strings.ToLower(name)
		if name == "" {
			continue
		}
		if !firstSnapshot && !watched[key] && previous[key] == serviceSignature(svc) {
			continue
		}
		add("service_state", map[string]any{
			"service_name": name,
			"display_name": svc.DisplayName,
			"state":        normalizeState(svc.State),
			"start_mode":   normalizeStartMode(svc.StartMode),
			"pid":          svc.PID,
			"exit_code":    svc.ExitCode,
		})
	}
}

func appendMissingWatchedServices(services []serviceInfo, watchlist []string) []serviceInfo {
	seen := make(map[string]bool, len(services))
	for _, svc := range services {
		if svc.ServiceName != "" {
			seen[strings.ToLower(svc.ServiceName)] = true
		}
	}
	for _, name := range watchlist {
		name = strings.TrimSpace(name)
		if name == "" || seen[strings.ToLower(name)] {
			continue
		}
		services = append(services, serviceInfo{
			ServiceName: name,
			DisplayName: name,
			State:       "not_found",
		})
	}
	return services
}

func serviceInventory(services []serviceInfo) []map[string]any {
	items := make([]map[string]any, 0, len(services))
	for _, svc := range services {
		if svc.ServiceName == "" {
			continue
		}
		items = append(items, map[string]any{
			"service_name": svc.ServiceName,
			"display_name": svc.DisplayName,
			"state":        normalizeState(svc.State),
			"start_mode":   normalizeStartMode(svc.StartMode),
			"pid":          svc.PID,
			"exit_code":    svc.ExitCode,
			"description":  svc.Description,
		})
	}
	return items
}

func serviceSignature(svc serviceInfo) string {
	return strings.Join([]string{
		normalizeState(svc.State),
		normalizeStartMode(svc.StartMode),
		fmt.Sprintf("%d", svc.PID),
		fmt.Sprintf("%d", svc.ExitCode),
	}, "|")
}

func collectEventLog(ctx context.Context, cfg config.EventLogConfig, add func(string, map[string]any), errs map[string]string) {
	queries, err := eventLogQueries(cfg)
	if err != nil {
		errs["event_log"] = err.Error()
		return
	}
	for _, q := range queries {
		if ctx.Err() != nil {
			errs["event_log"] = ctx.Err().Error()
			return
		}
		count, sampleIDs, err := countEvents(q)
		if err != nil {
			errs["event_log:"+q.Channel+":"+q.Level] = err.Error()
		}
		if sampleIDs == nil {
			sampleIDs = []int{}
		}
		add("event_log", map[string]any{
			"log_name":    q.Channel,
			"level":       q.Level,
			"event_count": count,
			"sample_ids":  sampleIDs,
		})
	}
}

// eventLogQuery counts one channel/level pair of the policy. A long event-ID
// list is split across several XPath queries (the event log rejects very
// long expressions); an event has one ID, so the counts simply add up.
type eventLogQuery struct {
	Channel string
	Level   string
	XPaths  []string
}

var eventLogLevelValues = map[string]int{
	"critical": 1, "error": 2, "warning": 3, "information": 4, "verbose": 5,
}

const eventIDsPerXPath = 20

func eventLogQueries(cfg config.EventLogConfig) ([]eventLogQuery, error) {
	filters, err := validatedEventLogFilters(cfg)
	if err != nil {
		return nil, err
	}
	lookbackMinutes := cfg.LookbackMinutes
	if lookbackMinutes <= 0 {
		lookbackMinutes = 5
	}
	window := int64(lookbackMinutes) * 60 * 1000
	var queries []eventLogQuery
	for _, filter := range filters {
		for _, level := range filter.Levels {
			level = strings.ToLower(level)
			q := eventLogQuery{Channel: filter.Channel, Level: level}
			base := fmt.Sprintf("Level=%d and TimeCreated[timediff(@SystemTime) <= %d]", eventLogLevelValues[level], window)
			if len(filter.IDs) == 0 {
				q.XPaths = []string{fmt.Sprintf("*[System[%s]]", base)}
			}
			for start := 0; start < len(filter.IDs); start += eventIDsPerXPath {
				end := min(start+eventIDsPerXPath, len(filter.IDs))
				terms := make([]string, 0, end-start)
				for _, id := range filter.IDs[start:end] {
					terms = append(terms, fmt.Sprintf("EventID=%d", id))
				}
				q.XPaths = append(q.XPaths, fmt.Sprintf("*[System[%s and (%s)]]", base, strings.Join(terms, " or ")))
			}
			queries = append(queries, q)
		}
	}
	return queries, nil
}

func collectInventory(ctx context.Context, r *Result) {
	info, err := host.InfoWithContext(ctx)
	if err == nil && info != nil {
		osInfo := map[string]any{
			"name":            info.Platform,
			"version":         info.PlatformVersion,
			"kernel_or_build": info.KernelVersion,
			"architecture":    info.KernelArch,
			"fqdn":            info.Hostname,
		}
		if info.BootTime > 0 {
			bootTime := time.Unix(int64(info.BootTime), 0).UTC()
			osInfo["boot_time"] = bootTime.Format(time.RFC3339)
			osInfo["uptime_seconds"] = int64(time.Since(bootTime).Seconds())
		}
		r.Inventory["os"] = osInfo
	}
	collectHardwareInventory(ctx, r)
	if runtime.GOOS == "windows" {
		software, err := collectInstalledSoftware(ctx)
		if err != nil {
			r.Errors["software_inventory"] = err.Error()
		} else if len(software) > 0 {
			r.Inventory["software"] = software
		}
	}
}

func collectInstalledSoftware(ctx context.Context) ([]map[string]any, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return installedSoftware()
}

func runPowerShellJSON(ctx context.Context, script string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script)
	cmd.SysProcAttr = hiddenPowerShellSysProcAttr()
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg != "" {
			return nil, fmt.Errorf("%w: %s", err, msg)
		}
		return nil, err
	}
	return out, nil
}

const (
	maxEventLogFilters         = 32
	maxEventLogLevelsPerFilter = 5
	maxEventLogIDsPerFilter    = 64
	maxEventLogTotalLevels     = 128
	maxEventLogTotalIDs        = 512
	maxEventLogChannelBytes    = 256
	maxEventLogLevelBytes      = 32
	maxWindowsEventIdentifier  = 2147483647
)

var supportedEventLogLevels = map[string]bool{
	"critical":    true,
	"error":       true,
	"warning":     true,
	"information": true,
	"verbose":     true,
}

func effectiveEventLogFilters(cfg config.EventLogConfig) []config.EventLogFilter {
	if cfg.Filters != nil {
		return *cfg.Filters
	}
	channels := cfg.Channels
	if len(channels) == 0 {
		channels = []string{"System", "Application"}
	}
	levels := cfg.Levels
	if len(levels) == 0 {
		levels = []string{"critical", "error", "warning"}
	}
	filters := make([]config.EventLogFilter, 0, len(channels))
	for _, channel := range channels {
		filters = append(filters, config.EventLogFilter{
			Channel: channel,
			Levels:  append([]string(nil), levels...),
		})
	}
	return filters
}

func validatedEventLogFilters(cfg config.EventLogConfig) ([]config.EventLogFilter, error) {
	filters := effectiveEventLogFilters(cfg)
	if len(filters) > maxEventLogFilters {
		return nil, fmt.Errorf("event-log policy has %d filters; maximum is %d", len(filters), maxEventLogFilters)
	}
	totalLevels := 0
	totalIDs := 0
	for i, filter := range filters {
		channel := strings.TrimSpace(filter.Channel)
		if channel == "" {
			return nil, fmt.Errorf("event-log filter %d has an empty channel", i+1)
		}
		if channel != filter.Channel {
			return nil, fmt.Errorf("event-log filter %d channel has leading or trailing whitespace", i+1)
		}
		if len(filter.Channel) > maxEventLogChannelBytes {
			return nil, fmt.Errorf("event-log filter %d channel is %d bytes; maximum is %d", i+1, len(filter.Channel), maxEventLogChannelBytes)
		}
		if len(filter.Levels) == 0 {
			return nil, fmt.Errorf("event-log filter %d has no levels", i+1)
		}
		if len(filter.Levels) > maxEventLogLevelsPerFilter {
			return nil, fmt.Errorf("event-log filter %d has %d levels; maximum is %d", i+1, len(filter.Levels), maxEventLogLevelsPerFilter)
		}
		totalLevels += len(filter.Levels)
		if totalLevels > maxEventLogTotalLevels {
			return nil, fmt.Errorf("event-log policy has %d total levels; maximum is %d", totalLevels, maxEventLogTotalLevels)
		}
		for j, level := range filter.Levels {
			trimmed := strings.TrimSpace(level)
			if trimmed != level {
				return nil, fmt.Errorf("event-log filter %d level %d has leading or trailing whitespace", i+1, j+1)
			}
			if len(level) > maxEventLogLevelBytes {
				return nil, fmt.Errorf("event-log filter %d level %d is %d bytes; maximum is %d", i+1, j+1, len(level), maxEventLogLevelBytes)
			}
			if !supportedEventLogLevels[strings.ToLower(level)] {
				return nil, fmt.Errorf("event-log filter %d level %q is unsupported", i+1, level)
			}
		}
		if len(filter.IDs) > maxEventLogIDsPerFilter {
			return nil, fmt.Errorf("event-log filter %d has %d event IDs; maximum is %d", i+1, len(filter.IDs), maxEventLogIDsPerFilter)
		}
		totalIDs += len(filter.IDs)
		if totalIDs > maxEventLogTotalIDs {
			return nil, fmt.Errorf("event-log policy has %d total event IDs; maximum is %d", totalIDs, maxEventLogTotalIDs)
		}
		for j, id := range filter.IDs {
			if id <= 0 || id > maxWindowsEventIdentifier {
				return nil, fmt.Errorf("event-log filter %d event ID %d at position %d must be between 1 and %d", i+1, id, j+1, maxWindowsEventIdentifier)
			}
		}
	}
	return filters, nil
}

func average(values []float64) float64 {
	if len(values) == 0 {
		return 0
	}
	var total float64
	for _, value := range values {
		total += value
	}
	return total / float64(len(values))
}

func clampPct(value float64) float64 {
	if value < 0 {
		return 0
	}
	if value > 100 {
		return 100
	}
	return value
}

func diff(current uint64, previous uint64) uint64 {
	if current < previous {
		return 0
	}
	return current - previous
}

func cloneDiskCounters(in map[string]disk.IOCountersStat) map[string]disk.IOCountersStat {
	out := make(map[string]disk.IOCountersStat, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func cloneNetCounters(in []gnet.IOCountersStat) map[string]gnet.IOCountersStat {
	out := make(map[string]gnet.IOCountersStat, len(in))
	for _, v := range in {
		out[v.Name] = v
	}
	return out
}

func cloneStringMap(in map[string]string) map[string]string {
	out := make(map[string]string, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func ignored(patterns []string, values ...string) bool {
	for _, pattern := range patterns {
		pattern = strings.TrimSpace(strings.ToLower(pattern))
		if pattern == "" {
			continue
		}
		for _, value := range values {
			value = strings.ToLower(value)
			if value == pattern || strings.Contains(value, pattern) {
				return true
			}
		}
	}
	return false
}

func hasFlag(flags []string, want string) bool {
	want = strings.ToLower(want)
	for _, flag := range flags {
		if strings.ToLower(flag) == want {
			return true
		}
	}
	return false
}

func interfaceUp(ifaces []gnet.InterfaceStat, name string) (bool, bool) {
	for _, iface := range ifaces {
		if iface.Name == name {
			return hasFlag(iface.Flags, "up"), true
		}
	}
	return false, false
}

func normalizedSet(values []string) map[string]bool {
	out := make(map[string]bool, len(values))
	for _, value := range values {
		value = strings.TrimSpace(strings.ToLower(value))
		if value != "" {
			out[value] = true
		}
	}
	return out
}

func firstN[T any](items []T, n int) []T {
	if n > len(items) {
		n = len(items)
	}
	if n < 0 {
		n = 0
	}
	return items[:n]
}

func stringValue(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func intValue(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case int32:
		return int(n)
	case int64:
		return int(n)
	case float64:
		return int(n)
	default:
		return 0
	}
}

func normalizeState(state string) string {
	switch strings.ToLower(strings.ReplaceAll(state, " ", "_")) {
	case "running":
		return "running"
	case "stopped":
		return "stopped"
	case "paused":
		return "paused"
	case "start_pending", "startpending":
		return "start_pending"
	case "stop_pending", "stoppending":
		return "stop_pending"
	case "not_found":
		return "not_found"
	default:
		if state == "" {
			return "unknown"
		}
		return strings.ToLower(state)
	}
}

func normalizeStartMode(mode string) string {
	switch strings.ToLower(mode) {
	case "auto", "automatic":
		return "auto"
	case "manual":
		return "manual"
	case "disabled":
		return "disabled"
	default:
		return strings.ToLower(mode)
	}
}
