//go:build windows

package collectors

// Native Windows sources for the collectors that used to shell out to
// PowerShell every cycle. Each PowerShell launch cost roughly a second of CPU
// and a ~110 MB process, several times a minute. These call the underlying
// Windows APIs directly and keep long-lived handles where that is cheaper.

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
)

// ── Performance counters (PDH) ────────────────────────────────────────

var (
	modpdh                          = windows.NewLazySystemDLL("pdh.dll")
	procPdhOpenQueryW               = modpdh.NewProc("PdhOpenQueryW")
	procPdhAddEnglishCounterW       = modpdh.NewProc("PdhAddEnglishCounterW")
	procPdhCollectQueryData         = modpdh.NewProc("PdhCollectQueryData")
	procPdhGetFormattedCounterValue = modpdh.NewProc("PdhGetFormattedCounterValue")
)

const (
	pdhFmtDouble    = 0x00000200
	pdhFmtNoCap100  = 0x00008000
	pdhCStatusValid = 0x00000000
	pdhCStatusNew   = 0x00000001
)

// pdhFmtCounterValueDouble mirrors PDH_FMT_COUNTERVALUE with a double value.
type pdhFmtCounterValueDouble struct {
	CStatus uint32
	_       uint32
	Value   float64
}

// systemCounterPaths are instantaneous counters: one collection per read is
// enough. English names keep them working on localized Windows, where the
// previous Get-Counter script silently returned nothing.
var systemCounterPaths = map[string]string{
	"load_1":          `\System\Processor Queue Length`,
	"cached_bytes":    `\Memory\Cache Bytes`,
	"committed_bytes": `\Memory\Committed Bytes`,
}

var systemCounters struct {
	sync.Mutex
	query    uintptr
	counters map[string]uintptr
	openErr  error
}

// readSystemCounters returns the current value of each system counter,
// keeping one PDH query open for the life of the process.
func readSystemCounters() (map[string]float64, error) {
	systemCounters.Lock()
	defer systemCounters.Unlock()
	if systemCounters.query == 0 && systemCounters.openErr == nil {
		systemCounters.openErr = openSystemCounters()
	}
	if systemCounters.openErr != nil {
		return nil, systemCounters.openErr
	}
	if status, _, _ := procPdhCollectQueryData.Call(systemCounters.query); status != 0 {
		return nil, fmt.Errorf("PdhCollectQueryData: 0x%08x", uint32(status))
	}
	values := make(map[string]float64, len(systemCounters.counters))
	for key, counter := range systemCounters.counters {
		var value pdhFmtCounterValueDouble
		status, _, _ := procPdhGetFormattedCounterValue.Call(
			counter, pdhFmtDouble|pdhFmtNoCap100, 0, uintptr(unsafe.Pointer(&value)))
		if status != 0 || (value.CStatus != pdhCStatusValid && value.CStatus != pdhCStatusNew) {
			continue
		}
		values[key] = value.Value
	}
	return values, nil
}

func openSystemCounters() error {
	if err := procPdhOpenQueryW.Find(); err != nil {
		return err
	}
	var query uintptr
	if status, _, _ := procPdhOpenQueryW.Call(0, 0, uintptr(unsafe.Pointer(&query))); status != 0 {
		return fmt.Errorf("PdhOpenQuery: 0x%08x", uint32(status))
	}
	counters := make(map[string]uintptr, len(systemCounterPaths))
	for key, path := range systemCounterPaths {
		name, err := windows.UTF16PtrFromString(path)
		if err != nil {
			return err
		}
		var counter uintptr
		status, _, _ := procPdhAddEnglishCounterW.Call(query, uintptr(unsafe.Pointer(name)), 0, uintptr(unsafe.Pointer(&counter)))
		if status == 0 {
			counters[key] = counter
		}
	}
	if len(counters) == 0 {
		return errors.New("no Windows performance counters are available")
	}
	systemCounters.query = query
	systemCounters.counters = counters
	return nil
}

// ── Services (Service Control Manager) ────────────────────────────────

const serviceConfigTTL = 10 * time.Minute

type cachedServiceConfig struct {
	startMode   string
	description string
	fetched     time.Time
}

var serviceConfigs = struct {
	sync.Mutex
	byName map[string]cachedServiceConfig
}{byName: map[string]cachedServiceConfig{}}

// listServices enumerates Win32 services with one SCM call. Start mode and
// description need a per-service query, so they are cached and refreshed
// every few minutes rather than on every collection.
func listServices() ([]serviceInfo, error) {
	scm, err := windows.OpenSCManager(nil, nil, windows.SC_MANAGER_CONNECT|windows.SC_MANAGER_ENUMERATE_SERVICE)
	if err != nil {
		return nil, fmt.Errorf("open service control manager: %w", err)
	}
	defer windows.CloseServiceHandle(scm)

	entries, err := enumServices(scm)
	if err != nil {
		return nil, err
	}
	now := time.Now()
	seen := make(map[string]bool, len(entries))
	services := make([]serviceInfo, 0, len(entries))
	serviceConfigs.Lock()
	defer serviceConfigs.Unlock()
	for _, entry := range entries {
		name := windows.UTF16PtrToString(entry.ServiceName)
		if name == "" {
			continue
		}
		key := strings.ToLower(name)
		seen[key] = true
		cfg, ok := serviceConfigs.byName[key]
		if !ok || now.Sub(cfg.fetched) > serviceConfigTTL {
			cfg = queryServiceConfig(scm, name)
			cfg.fetched = now
			serviceConfigs.byName[key] = cfg
		}
		status := entry.ServiceStatusProcess
		services = append(services, serviceInfo{
			ServiceName: name,
			DisplayName: windows.UTF16PtrToString(entry.DisplayName),
			State:       serviceStateName(status.CurrentState),
			StartMode:   cfg.startMode,
			PID:         int(status.ProcessId),
			ExitCode:    int(status.Win32ExitCode),
			Description: cfg.description,
		})
	}
	for key := range serviceConfigs.byName {
		if !seen[key] {
			delete(serviceConfigs.byName, key)
		}
	}
	return services, nil
}

func enumServices(scm windows.Handle) ([]windows.ENUM_SERVICE_STATUS_PROCESS, error) {
	var needed, returned, resume uint32
	buf := make([]byte, 64*1024)
	var entries []windows.ENUM_SERVICE_STATUS_PROCESS
	for {
		err := windows.EnumServicesStatusEx(scm, windows.SC_ENUM_PROCESS_INFO, windows.SERVICE_WIN32,
			windows.SERVICE_STATE_ALL, &buf[0], uint32(len(buf)), &needed, &returned, &resume, nil)
		if err != nil && err != windows.ERROR_MORE_DATA {
			return nil, fmt.Errorf("enumerate services: %w", err)
		}
		if returned > 0 {
			batch := unsafe.Slice((*windows.ENUM_SERVICE_STATUS_PROCESS)(unsafe.Pointer(&buf[0])), returned)
			// Names point into buf; copy them out before the buffer is reused.
			for _, entry := range batch {
				entries = append(entries, windows.ENUM_SERVICE_STATUS_PROCESS{
					ServiceName:          copyUTF16Ptr(entry.ServiceName),
					DisplayName:          copyUTF16Ptr(entry.DisplayName),
					ServiceStatusProcess: entry.ServiceStatusProcess,
				})
			}
		}
		if err == nil {
			return entries, nil
		}
		if returned == 0 {
			if needed <= uint32(len(buf)) {
				return nil, fmt.Errorf("enumerate services: no progress with a %d-byte buffer", len(buf))
			}
			buf = make([]byte, needed)
		}
	}
}

func copyUTF16Ptr(p *uint16) *uint16 {
	s, _ := windows.UTF16PtrFromString(windows.UTF16PtrToString(p))
	return s
}

func queryServiceConfig(scm windows.Handle, name string) cachedServiceConfig {
	namePtr, err := windows.UTF16PtrFromString(name)
	if err != nil {
		return cachedServiceConfig{}
	}
	svc, err := windows.OpenService(scm, namePtr, windows.SERVICE_QUERY_CONFIG)
	if err != nil {
		return cachedServiceConfig{}
	}
	defer windows.CloseServiceHandle(svc)
	var cfg cachedServiceConfig

	var needed uint32
	_ = windows.QueryServiceConfig(svc, nil, 0, &needed)
	if needed > 0 {
		buf := make([]byte, needed)
		sc := (*windows.QUERY_SERVICE_CONFIG)(unsafe.Pointer(&buf[0]))
		if windows.QueryServiceConfig(svc, sc, needed, &needed) == nil {
			cfg.startMode = serviceStartModeName(sc.StartType)
		}
	}

	needed = 0
	_ = windows.QueryServiceConfig2(svc, windows.SERVICE_CONFIG_DESCRIPTION, nil, 0, &needed)
	if needed > 0 {
		buf := make([]byte, needed)
		if windows.QueryServiceConfig2(svc, windows.SERVICE_CONFIG_DESCRIPTION, &buf[0], needed, &needed) == nil {
			desc := (*windows.SERVICE_DESCRIPTION)(unsafe.Pointer(&buf[0]))
			if desc.Description != nil {
				cfg.description = windows.UTF16PtrToString(desc.Description)
			}
		}
	}
	return cfg
}

// serviceStateName uses the Win32_Service strings the collector always
// reported, so normalizeState output is unchanged.
func serviceStateName(state uint32) string {
	switch state {
	case windows.SERVICE_RUNNING:
		return "Running"
	case windows.SERVICE_STOPPED:
		return "Stopped"
	case windows.SERVICE_PAUSED:
		return "Paused"
	case windows.SERVICE_START_PENDING:
		return "Start Pending"
	case windows.SERVICE_STOP_PENDING:
		return "Stop Pending"
	case windows.SERVICE_CONTINUE_PENDING:
		return "Continue Pending"
	case windows.SERVICE_PAUSE_PENDING:
		return "Pause Pending"
	default:
		return "Unknown"
	}
}

func serviceStartModeName(startType uint32) string {
	switch startType {
	case windows.SERVICE_BOOT_START:
		return "Boot"
	case windows.SERVICE_SYSTEM_START:
		return "System"
	case windows.SERVICE_AUTO_START:
		return "Auto"
	case windows.SERVICE_DEMAND_START:
		return "Manual"
	case windows.SERVICE_DISABLED:
		return "Disabled"
	default:
		return ""
	}
}

// ── Event log (wevtapi) ───────────────────────────────────────────────

var (
	modwevtapi                 = windows.NewLazySystemDLL("wevtapi.dll")
	procEvtQuery               = modwevtapi.NewProc("EvtQuery")
	procEvtNext                = modwevtapi.NewProc("EvtNext")
	procEvtClose               = modwevtapi.NewProc("EvtClose")
	procEvtCreateRenderContext = modwevtapi.NewProc("EvtCreateRenderContext")
	procEvtRender              = modwevtapi.NewProc("EvtRender")
)

const (
	evtQueryChannelPath      = 0x1
	evtQueryReverseDirection = 0x200
	evtRenderContextValues   = 0
	evtRenderEventValues     = 0
	evtVarTypeUInt16         = 6
	evtNextBatch             = 128
	errorEvtChannelNotFound  = windows.Errno(15007)
	errorNoMoreItems         = windows.Errno(259)
	eventLogSampleIDs        = 50
)

// evtVariant mirrors EVT_VARIANT: an 8-byte value, a count and a type.
type evtVariant struct {
	Value uint64
	Count uint32
	Type  uint32
}

// countEvents counts the events matching one query and returns the IDs of
// the newest few. Events are enumerated as handles only; nothing is
// formatted, which is where Get-WinEvent spent most of its time.
func countEvents(q eventLogQuery) (int, []int, error) {
	if err := procEvtQuery.Find(); err != nil {
		return 0, nil, err
	}
	renderContext, err := eventIDRenderContext()
	if err != nil {
		return 0, nil, err
	}
	defer procEvtClose.Call(renderContext)

	count := 0
	var sample []int
	for _, xpath := range q.XPaths {
		channel, err := windows.UTF16PtrFromString(q.Channel)
		if err != nil {
			return count, sample, err
		}
		query, err := windows.UTF16PtrFromString(xpath)
		if err != nil {
			return count, sample, err
		}
		results, _, callErr := procEvtQuery.Call(0, uintptr(unsafe.Pointer(channel)), uintptr(unsafe.Pointer(query)),
			evtQueryChannelPath|evtQueryReverseDirection)
		if results == 0 {
			if errors.Is(callErr, errorEvtChannelNotFound) {
				// Get-WinEvent -ErrorAction SilentlyContinue reported an
				// absent channel as zero events; keep that behaviour.
				return 0, nil, nil
			}
			return count, sample, fmt.Errorf("query %s: %w", q.Channel, callErr)
		}
		n, ids, err := drainEvents(results, renderContext, eventLogSampleIDs-len(sample))
		procEvtClose.Call(results)
		count += n
		sample = append(sample, ids...)
		if err != nil {
			return count, sample, err
		}
	}
	return count, sample, nil
}

func drainEvents(results, renderContext uintptr, wantIDs int) (int, []int, error) {
	handles := make([]uintptr, evtNextBatch)
	count := 0
	var ids []int
	for {
		var returned uint32
		ok, _, callErr := procEvtNext.Call(results, evtNextBatch, uintptr(unsafe.Pointer(&handles[0])),
			windows.INFINITE, 0, uintptr(unsafe.Pointer(&returned)))
		if ok == 0 {
			if errors.Is(callErr, errorNoMoreItems) {
				return count, ids, nil
			}
			return count, ids, fmt.Errorf("read events: %w", callErr)
		}
		for _, event := range handles[:returned] {
			if len(ids) < wantIDs {
				if id, ok := renderEventID(renderContext, event); ok {
					ids = append(ids, id)
				}
			}
			procEvtClose.Call(event)
		}
		count += int(returned)
	}
}

func eventIDRenderContext() (uintptr, error) {
	path, err := windows.UTF16PtrFromString("Event/System/EventID")
	if err != nil {
		return 0, err
	}
	paths := []*uint16{path}
	ctx, _, callErr := procEvtCreateRenderContext.Call(1, uintptr(unsafe.Pointer(&paths[0])), evtRenderContextValues)
	if ctx == 0 {
		return 0, fmt.Errorf("create event render context: %w", callErr)
	}
	return ctx, nil
}

func renderEventID(renderContext, event uintptr) (int, bool) {
	var value [4]evtVariant // one value; spare room for the API's bookkeeping
	var used, count uint32
	ok, _, _ := procEvtRender.Call(renderContext, event, evtRenderEventValues,
		unsafe.Sizeof(value), uintptr(unsafe.Pointer(&value[0])),
		uintptr(unsafe.Pointer(&used)), uintptr(unsafe.Pointer(&count)))
	if ok == 0 || count < 1 || value[0].Type != evtVarTypeUInt16 {
		return 0, false
	}
	return int(uint16(value[0].Value)), true
}

// ── Installed software (registry) ─────────────────────────────────────

type uninstallRoot struct {
	key          registry.Key
	path         string
	architecture string
	source       string
}

var uninstallRoots = []uninstallRoot{
	{registry.LOCAL_MACHINE, `Software\Microsoft\Windows\CurrentVersion\Uninstall`, "x64", "hklm"},
	{registry.LOCAL_MACHINE, `Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall`, "x86", "hklm_wow6432"},
	{registry.CURRENT_USER, `Software\Microsoft\Windows\CurrentVersion\Uninstall`, "user", "hkcu"},
}

const maxInstalledSoftware = 1000

// installedSoftware reads the Programs and Features entries from the
// registry, applying the same filters as the previous PowerShell script:
// hidden system components and updates are skipped and duplicates of the
// same name, version and publisher are reported once.
func installedSoftware() ([]map[string]any, error) {
	seen := map[string]bool{}
	var items []map[string]any
	for _, root := range uninstallRoots {
		parent, err := registry.OpenKey(root.key, root.path, registry.ENUMERATE_SUB_KEYS)
		if err != nil {
			continue
		}
		names, err := parent.ReadSubKeyNames(-1)
		parent.Close()
		if err != nil {
			continue
		}
		for _, sub := range names {
			item, key, ok := readUninstallEntry(root, sub)
			if !ok || seen[key] {
				continue
			}
			seen[key] = true
			items = append(items, item)
		}
	}
	sort.SliceStable(items, func(i, j int) bool {
		return strings.ToLower(items[i]["package_name"].(string)) < strings.ToLower(items[j]["package_name"].(string))
	})
	if len(items) > maxInstalledSoftware {
		items = items[:maxInstalledSoftware]
	}
	return items, nil
}

func readUninstallEntry(root uninstallRoot, sub string) (map[string]any, string, bool) {
	k, err := registry.OpenKey(root.key, root.path+`\`+sub, registry.QUERY_VALUE)
	if err != nil {
		return nil, "", false
	}
	defer k.Close()
	str := func(name string) string {
		value, _, err := k.GetStringValue(name)
		if err != nil {
			return ""
		}
		return strings.TrimSpace(value)
	}
	name := str("DisplayName")
	if name == "" {
		return nil, "", false
	}
	if component, _, err := k.GetIntegerValue("SystemComponent"); err == nil && component == 1 {
		return nil, "", false
	}
	switch str("ReleaseType") {
	case "Update", "Hotfix", "Security Update":
		return nil, "", false
	}
	version := str("DisplayVersion")
	vendor := str("Publisher")
	var installDate any
	if raw := str("InstallDate"); len(raw) == 8 {
		if t, err := time.ParseInLocation("20060102", raw, time.Local); err == nil {
			installDate = t.UTC().Format("2006-01-02T15:04:05.0000000Z")
		}
	}
	key := strings.ToLower(name) + "|" + strings.ToLower(version) + "|" + strings.ToLower(vendor)
	return map[string]any{
		"package_name":     name,
		"name":             name,
		"version":          version,
		"vendor":           vendor,
		"publisher":        vendor,
		"install_date":     installDate,
		"install_location": str("InstallLocation"),
		"architecture":     root.architecture,
		"source":           root.source,
	}, key, true
}
