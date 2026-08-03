//go:build windows

package main

import (
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
)

const (
	productName   = "ZenPlus Agent"
	publisherName = "ZenPlus"
	serviceName   = "ZenPlusAgent"
)

type payloadFile struct {
	Name string
	Data []byte
	Mode fs.FileMode
}

type options struct {
	quiet           bool
	uninstall       bool
	purge           bool
	noStartMenu     bool
	noRestart       bool
	machine         bool
	user            bool
	fromTemp        bool
	autoUninstall   bool
	controllerURL   string
	enrollmentToken string
	siteID          string
	policyID        string
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "%s setup failed: %v\n", productName, err)
		if !hasQuietArg(os.Args[1:]) {
			showMessage(productName+" Setup", fmt.Sprintf("%s setup failed:\n\n%v", productName, err), true)
		}
		os.Exit(1)
	}
}

func run() error {
	opts, err := parseOptions(os.Args[1:])
	if err != nil {
		return err
	}
	elevated := isElevated()
	if !opts.quiet {
		if opts.uninstall {
			return runUninstallUI(opts, elevated)
		}
		return runSetupUI(opts, elevated)
	}
	layout, err := newLayout(opts, elevated)
	if err != nil {
		return err
	}
	if layout.Scope == "machine" && !elevated {
		return relaunchElevated(ensureArg(os.Args[1:], "/machine"))
	}
	if opts.uninstall {
		if !opts.fromTemp {
			if relaunched, err := relaunchUninstallFromTemp(layout, os.Args[1:]); err != nil {
				return err
			} else if relaunched {
				return nil
			}
		}
		return uninstall(layout, opts)
	}
	return install(layout, opts)
}

func parseOptions(args []string) (options, error) {
	var opts options
	normalized := make([]string, 0, len(args))
	for _, arg := range args {
		switch {
		case strings.HasPrefix(arg, "/"):
			normalized = append(normalized, "-"+strings.TrimLeft(arg, "/"))
		case strings.Contains(arg, "="):
			key, value, _ := strings.Cut(arg, "=")
			switch strings.ToUpper(strings.TrimSpace(key)) {
			case "CONTROLLER_URL":
				normalized = append(normalized, "-controller-url", value)
			case "ENROLLMENT_TOKEN":
				normalized = append(normalized, "-enrollment-token", value)
			case "SITE_ID":
				normalized = append(normalized, "-site-id", value)
			case "POLICY_ID":
				normalized = append(normalized, "-policy-id", value)
			case "PURGE":
				if value == "1" || strings.EqualFold(value, "true") {
					normalized = append(normalized, "-purge")
				}
			default:
				normalized = append(normalized, arg)
			}
		default:
			normalized = append(normalized, arg)
		}
	}

	fs := flag.NewFlagSet("setup", flag.ContinueOnError)
	fs.BoolVar(&opts.quiet, "quiet", false, "run without prompts")
	fs.BoolVar(&opts.quiet, "qn", false, "run without prompts")
	fs.BoolVar(&opts.uninstall, "uninstall", false, "uninstall ZenPlus Agent")
	fs.BoolVar(&opts.purge, "purge", false, "remove ProgramData state during uninstall")
	fs.BoolVar(&opts.noStartMenu, "no-start-menu", false, "skip Start Menu shortcut creation")
	fs.BoolVar(&opts.noRestart, "norestart", false, "accepted for deployment compatibility")
	fs.BoolVar(&opts.machine, "machine", false, "install for all users with the Windows service")
	fs.BoolVar(&opts.user, "user", false, "install for the current user without elevation")
	fs.BoolVar(&opts.fromTemp, "from-temp", false, "internal uninstall continuation")
	fs.BoolVar(&opts.autoUninstall, "auto-uninstall", false, "internal UI uninstall continuation")
	fs.StringVar(&opts.controllerURL, "controller-url", "", "controller URL")
	fs.StringVar(&opts.enrollmentToken, "enrollment-token", "", "enrollment token")
	fs.StringVar(&opts.siteID, "site-id", "", "site ID")
	fs.StringVar(&opts.policyID, "policy-id", "", "policy ID")
	fs.SetOutput(os.Stderr)
	if err := fs.Parse(normalized); err != nil {
		return opts, err
	}
	return opts, nil
}

type layout struct {
	Scope            string
	ProgramFiles     string
	ProgramData      string
	InstallDir       string
	DataDir          string
	ConfigDir        string
	ConfigPath       string
	StartMenuDir     string
	StartupDir       string
	UserStartMenuDir string
	UserStartupDir   string
	Uninstaller      string
}

func newLayout(opts options, elevated bool) (layout, error) {
	scope := "user"
	if opts.machine || (elevated && !opts.user) {
		scope = "machine"
	}
	programFiles := os.Getenv("ProgramFiles")
	if programFiles == "" {
		programFiles = `C:\Program Files`
	}
	programData := os.Getenv("ProgramData")
	if programData == "" {
		programData = `C:\ProgramData`
	}
	appData := os.Getenv("APPDATA")
	localAppData := os.Getenv("LOCALAPPDATA")
	if localAppData == "" {
		localAppData = filepath.Join(os.Getenv("USERPROFILE"), "AppData", "Local")
	}
	if appData == "" {
		appData = filepath.Join(os.Getenv("USERPROFILE"), "AppData", "Roaming")
	}
	installDir := filepath.Join(localAppData, "Programs", "ZenPlus", "Agent")
	dataDir := filepath.Join(localAppData, "ZenPlus", "Agent")
	userStartMenuDir := filepath.Join(appData, "Microsoft", "Windows", "Start Menu", "Programs", productName)
	startMenuDir := userStartMenuDir
	userStartupDir := filepath.Join(appData, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
	startupDir := userStartupDir
	if scope == "machine" {
		installDir = filepath.Join(programFiles, "ZenPlus", "Agent")
		dataDir = filepath.Join(programData, "ZenPlus", "Agent")
		startMenuDir = filepath.Join(programData, "Microsoft", "Windows", "Start Menu", "Programs", productName)
		startupDir = filepath.Join(programData, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
	}
	if opts.uninstall && !opts.user {
		if self, err := os.Executable(); err == nil {
			machineDir := filepath.Join(programFiles, "ZenPlus", "Agent")
			if pathWithin(machineDir, self) {
				scope = "machine"
				installDir = machineDir
				dataDir = filepath.Join(programData, "ZenPlus", "Agent")
				startMenuDir = filepath.Join(programData, "Microsoft", "Windows", "Start Menu", "Programs", productName)
				startupDir = filepath.Join(programData, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
			}
		}
	}
	return layout{
		Scope:            scope,
		ProgramFiles:     programFiles,
		ProgramData:      programData,
		InstallDir:       installDir,
		DataDir:          dataDir,
		ConfigDir:        filepath.Join(dataDir, "config"),
		ConfigPath:       filepath.Join(dataDir, "config", "agent.yaml"),
		StartMenuDir:     startMenuDir,
		StartupDir:       startupDir,
		UserStartMenuDir: userStartMenuDir,
		UserStartupDir:   userStartupDir,
		Uninstaller:      filepath.Join(installDir, "ZenPlusAgentSetup.exe"),
	}, nil
}

func pathWithin(parent string, child string) bool {
	rel, err := filepath.Rel(parent, child)
	if err != nil {
		return false
	}
	return rel == "." || (!strings.HasPrefix(rel, "..") && !filepath.IsAbs(rel))
}

func install(l layout, opts options) error {
	logStep(opts, "Installing %s %s (%s)", productName, model.AgentVersion, l.Scope)
	if l.Scope == "machine" {
		if err := uninstallService(l); err != nil {
			return err
		}
	}
	terminateZenPlusProcesses(15 * time.Second)
	removeLegacyRuntime(l)
	removeOppositeScopeShortcuts(l)
	if err := os.MkdirAll(l.InstallDir, 0o755); err != nil {
		return err
	}
	if err := os.MkdirAll(l.ConfigDir, 0o755); err != nil {
		return err
	}

	payloads, err := embeddedPayloads()
	if err != nil {
		return err
	}
	for _, payload := range payloads {
		target := filepath.Join(l.InstallDir, payload.Name)
		if err := os.WriteFile(target, payload.Data, payload.Mode); err != nil {
			return fmt.Errorf("write %s: %w", target, err)
		}
	}
	if err := copySelf(l.Uninstaller); err != nil {
		return err
	}
	if err := writeInstalledConfig(l, opts); err != nil {
		return err
	}
	if l.Scope == "machine" {
		if err := installService(l); err != nil {
			return err
		}
	} else if err := launchUserRunner(l); err != nil {
		return err
	}
	if opts.noStartMenu {
		removeStartMenuShortcuts(l)
	} else {
		if err := createShortcuts(l); err != nil {
			return err
		}
	}
	if err := createStartupShortcut(l); err != nil {
		return err
	}
	if err := writeUninstallRegistry(l); err != nil {
		return err
	}
	logStep(opts, "%s installed successfully.", productName)
	if !opts.quiet {
		showMessage(productName+" Setup", productName+" was installed successfully.", false)
	}
	return nil
}

func uninstall(l layout, opts options) error {
	logStep(opts, "Uninstalling %s (%s)", productName, l.Scope)
	if l.Scope == "machine" {
		if err := uninstallService(l); err != nil {
			return err
		}
	}
	terminateZenPlusProcesses(15 * time.Second)
	removeShortcuts(l)
	_ = removeUninstallRegistry(l)
	if err := removeAllWithRetry(l.InstallDir, 20, 500*time.Millisecond); err != nil {
		return fmt.Errorf("remove install directory: %w", err)
	}
	if opts.purge {
		if err := removeAllWithRetry(l.DataDir, 20, 500*time.Millisecond); err != nil {
			return fmt.Errorf("remove data directory: %w", err)
		}
	}
	logStep(opts, "%s uninstalled successfully.", productName)
	if !opts.quiet {
		showMessage(productName+" Setup", productName+" was uninstalled successfully.", false)
	}
	return nil
}

func terminateZenPlusProcesses(timeout time.Duration) {
	names := map[string]bool{
		"zenplus-agent-app.exe":  true,
		"zenplus-agent.exe":      true,
		"zenplus-agent-user.exe": true,
	}
	currentPID := uint32(os.Getpid())
	deadline := time.Now().Add(timeout)
	for {
		pids := findProcesses(names, currentPID)
		if len(pids) == 0 {
			return
		}
		for _, pid := range pids {
			terminateProcess(pid)
		}
		if time.Now().After(deadline) {
			return
		}
		time.Sleep(300 * time.Millisecond)
	}
}

func findProcesses(names map[string]bool, currentPID uint32) []uint32 {
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil
	}
	defer windows.CloseHandle(snapshot)
	var entry windows.ProcessEntry32
	entry.Size = uint32(unsafe.Sizeof(entry))
	if err := windows.Process32First(snapshot, &entry); err != nil {
		return nil
	}
	var pids []uint32
	for {
		name := strings.ToLower(windows.UTF16ToString(entry.ExeFile[:]))
		if entry.ProcessID != currentPID && names[name] {
			pids = append(pids, entry.ProcessID)
		}
		if err := windows.Process32Next(snapshot, &entry); err != nil {
			break
		}
	}
	return pids
}

func terminateProcess(pid uint32) {
	handle, err := windows.OpenProcess(windows.PROCESS_TERMINATE, false, pid)
	if err != nil {
		return
	}
	defer windows.CloseHandle(handle)
	_ = windows.TerminateProcess(handle, 0)
}

func removeLegacyRuntime(l layout) {
	_ = os.RemoveAll(filepath.Join(l.DataDir, "bin"))
}

func removeShortcuts(l layout) {
	_ = os.RemoveAll(l.StartMenuDir)
	_ = os.Remove(filepath.Join(l.StartupDir, "ZenPlus Agent.lnk"))
	_ = os.Remove(filepath.Join(l.StartupDir, "ZenPlus Agent Background.lnk"))
	_ = os.Remove(filepath.Join(l.StartupDir, "ZenPlusAgent.vbs"))
	_ = os.Remove(filepath.Join(l.StartupDir, "ZenPlusAgent.lnk"))
	if l.UserStartupDir != "" {
		_ = os.Remove(filepath.Join(l.UserStartupDir, "ZenPlus Agent.lnk"))
		_ = os.Remove(filepath.Join(l.UserStartupDir, "ZenPlus Agent Background.lnk"))
		_ = os.Remove(filepath.Join(l.UserStartupDir, "ZenPlusAgent.vbs"))
		_ = os.Remove(filepath.Join(l.UserStartupDir, "ZenPlusAgent.lnk"))
	}
}

func removeOppositeScopeShortcuts(l layout) {
	if l.Scope == "user" {
		machineStartMenu := filepath.Join(l.ProgramData, "Microsoft", "Windows", "Start Menu", "Programs", productName)
		machineStartup := filepath.Join(l.ProgramData, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
		_ = os.RemoveAll(machineStartMenu)
		_ = os.Remove(filepath.Join(machineStartup, "ZenPlus Agent.lnk"))
		_ = os.Remove(filepath.Join(machineStartup, "ZenPlus Agent Background.lnk"))
		_ = os.Remove(filepath.Join(machineStartup, "ZenPlusAgent.vbs"))
		_ = os.Remove(filepath.Join(machineStartup, "ZenPlusAgent.lnk"))
		return
	}
	if l.UserStartMenuDir != "" {
		_ = os.RemoveAll(l.UserStartMenuDir)
	}
	if l.UserStartupDir != "" {
		_ = os.Remove(filepath.Join(l.UserStartupDir, "ZenPlus Agent.lnk"))
		_ = os.Remove(filepath.Join(l.UserStartupDir, "ZenPlus Agent Background.lnk"))
		_ = os.Remove(filepath.Join(l.UserStartupDir, "ZenPlusAgent.vbs"))
		_ = os.Remove(filepath.Join(l.UserStartupDir, "ZenPlusAgent.lnk"))
	}
}

func writeInstalledConfig(l layout, opts options) error {
	cfg, err := config.LoadForEdit(l.ConfigPath)
	if err != nil {
		return err
	}
	cfg.DataDir = l.DataDir
	if opts.controllerURL != "" {
		normalized, err := config.NormalizeControllerURL(opts.controllerURL)
		if err != nil {
			return err
		}
		cfg.ControllerURL = normalized
	}
	// Discards the MSI's un-substituted placeholder, so a package pulled
	// outside the controller's download flow installs without a bogus token.
	if token := config.NormalizeEnrollmentToken(opts.enrollmentToken); token != "" {
		cfg.EnrollmentToken = token
	}
	if opts.siteID != "" {
		cfg.SiteID = opts.siteID
	}
	if opts.policyID != "" {
		cfg.PolicyID = opts.policyID
	}
	return config.Save(l.ConfigPath, cfg)
}

func installService(l layout) error {
	agent := filepath.Join(l.InstallDir, "zenplus-agent.exe")
	return runCommand(agent, "install-service", "--config", l.ConfigPath)
}

func launchUserRunner(l layout) error {
	runner := filepath.Join(l.InstallDir, "zenplus-agent-user.exe")
	cmd := exec.Command(runner, "--config", l.ConfigPath)
	cmd.SysProcAttr = detachedSysProcAttr()
	return cmd.Start()
}

func launchDashboard(l layout) error {
	dashboard := filepath.Join(l.InstallDir, "zenplus-agent-app.exe")
	cmd := exec.Command(dashboard, "--config", l.ConfigPath)
	cmd.SysProcAttr = detachedSysProcAttr()
	return cmd.Start()
}

func uninstallService(l layout) error {
	m, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect service manager: %w", err)
	}
	defer m.Disconnect()
	s, err := m.OpenService(serviceName)
	if err != nil {
		if serviceMissing(err) {
			return nil
		}
		return fmt.Errorf("open service: %w", err)
	}
	defer s.Close()

	status, err := s.Query()
	if err == nil && status.State != svc.Stopped {
		_, _ = s.Control(svc.Stop)
		waitForServiceStopped(s, 20*time.Second)
	}
	if err := s.Delete(); err != nil {
		if serviceMissing(err) {
			return nil
		}
		return fmt.Errorf("delete service: %w", err)
	}
	return nil
}

func waitForServiceStopped(s *mgr.Service, timeout time.Duration) {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		status, err := s.Query()
		if err != nil || status.State == svc.Stopped {
			return
		}
		time.Sleep(500 * time.Millisecond)
	}
}

func serviceMissing(err error) bool {
	if errors.Is(err, windows.ERROR_SERVICE_DOES_NOT_EXIST) {
		return true
	}
	text := strings.ToLower(err.Error())
	return strings.Contains(text, "service does not exist") ||
		strings.Contains(text, "specified service does not exist") ||
		strings.Contains(text, "open service")
}

func runCommand(name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.SysProcAttr = hiddenSysProcAttr()
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s %s failed: %w\n%s", filepath.Base(name), strings.Join(args, " "), err, strings.TrimSpace(string(output)))
	}
	return nil
}

func runCommandAllowFailure(name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.SysProcAttr = hiddenSysProcAttr()
	_ = cmd.Run()
	return nil
}

func hiddenSysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{HideWindow: true, CreationFlags: windows.CREATE_NO_WINDOW}
}

func detachedSysProcAttr() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: windows.CREATE_NO_WINDOW | windows.CREATE_NEW_PROCESS_GROUP | windows.DETACHED_PROCESS,
	}
}

func relaunchUninstallFromTemp(l layout, args []string) (bool, error) {
	self, err := os.Executable()
	if err != nil {
		return false, err
	}
	rel, err := filepath.Rel(l.InstallDir, self)
	if err != nil || strings.HasPrefix(rel, "..") || filepath.IsAbs(rel) {
		return false, nil
	}
	tempDir := filepath.Join(os.TempDir(), fmt.Sprintf("ZenPlusAgentSetup-%d", time.Now().UnixNano()))
	if err := os.MkdirAll(tempDir, 0o755); err != nil {
		return false, err
	}
	tempExe := filepath.Join(tempDir, "ZenPlusAgentSetup.exe")
	data, err := os.ReadFile(self)
	if err != nil {
		return false, err
	}
	if err := os.WriteFile(tempExe, data, 0o755); err != nil {
		return false, err
	}
	cmd := exec.Command(tempExe, ensureArg(args, "/from-temp")...)
	cmd.SysProcAttr = hiddenSysProcAttr()
	if err := cmd.Start(); err != nil {
		return false, err
	}
	return true, nil
}

func removeAllWithRetry(path string, attempts int, delay time.Duration) error {
	if path == "" {
		return nil
	}
	var lastErr error
	for i := 0; i < attempts; i++ {
		lastErr = os.RemoveAll(path)
		if _, err := os.Stat(path); errors.Is(err, os.ErrNotExist) {
			return nil
		}
		time.Sleep(delay)
	}
	scheduleDeleteOnReboot(path)
	return lastErr
}

func scheduleDeleteOnReboot(path string) {
	_ = filepath.WalkDir(path, func(p string, _ os.DirEntry, _ error) error {
		ptr, err := windows.UTF16PtrFromString(p)
		if err == nil {
			_ = windows.MoveFileEx(ptr, nil, windows.MOVEFILE_DELAY_UNTIL_REBOOT)
		}
		return nil
	})
	ptr, err := windows.UTF16PtrFromString(path)
	if err == nil {
		_ = windows.MoveFileEx(ptr, nil, windows.MOVEFILE_DELAY_UNTIL_REBOOT)
	}
}

func copySelf(target string) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	data, err := os.ReadFile(self)
	if err != nil {
		return err
	}
	return os.WriteFile(target, data, 0o755)
}

func createShortcuts(l layout) error {
	if err := os.MkdirAll(l.StartMenuDir, 0o755); err != nil {
		return err
	}
	removeDeprecatedStartMenuShortcuts(l)
	dashboard := filepath.Join(l.InstallDir, "zenplus-agent-app.exe")
	return createShortcut(filepath.Join(l.StartMenuDir, "ZenPlus Agent Dashboard.lnk"), dashboard, "--config \""+l.ConfigPath+"\"", l.InstallDir, productName)
}

func removeStartMenuShortcuts(l layout) {
	_ = os.RemoveAll(l.StartMenuDir)
}

func removeDeprecatedStartMenuShortcuts(l layout) {
	_ = os.Remove(filepath.Join(l.StartMenuDir, "ZenPlus Agent Status.lnk"))
	_ = os.Remove(filepath.Join(l.StartMenuDir, "Uninstall ZenPlus Agent.lnk"))
}

func createStartupShortcut(l layout) error {
	if err := os.MkdirAll(l.StartupDir, 0o755); err != nil {
		return err
	}
	dashboard := filepath.Join(l.InstallDir, "zenplus-agent-app.exe")
	args := "--config " + quote(l.ConfigPath) + " --start-hidden"
	if err := createShortcut(filepath.Join(l.StartupDir, "ZenPlus Agent.lnk"), dashboard, args, l.InstallDir, productName); err != nil {
		return err
	}
	if l.Scope == "user" {
		runner := filepath.Join(l.InstallDir, "zenplus-agent-user.exe")
		return createShortcut(filepath.Join(l.StartupDir, "ZenPlus Agent Background.lnk"), runner, "--config "+quote(l.ConfigPath), l.InstallDir, productName+" Background")
	}
	return nil
}

func createShortcut(path, target, arguments, workingDir, description string) error {
	script := strings.Join([]string{
		"$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + psQuote(path) + ");",
		"$s.TargetPath=" + psQuote(target) + ";",
		"$s.Arguments=" + psQuote(arguments) + ";",
		"$s.WorkingDirectory=" + psQuote(workingDir) + ";",
		"$s.Description=" + psQuote(description) + ";",
		"$s.IconLocation=" + psQuote(target+",0") + ";",
		"$s.Save();",
	}, "")
	return runCommand("powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", script)
}

func psQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "''") + "'"
}

func writeUninstallRegistry(l layout) error {
	root := registry.CURRENT_USER
	if l.Scope == "machine" {
		root = registry.LOCAL_MACHINE
	}
	key, _, err := registry.CreateKey(root, `Software\Microsoft\Windows\CurrentVersion\Uninstall\ZenPlus Agent`, registry.SET_VALUE)
	if err != nil {
		return err
	}
	defer key.Close()
	uninstallCommand := quote(l.Uninstaller) + " /uninstall /quiet " + scopeArg(l)
	values := map[string]string{
		"DisplayName":          productName,
		"DisplayVersion":       model.AgentVersion,
		"Publisher":            publisherName,
		"InstallLocation":      l.InstallDir,
		"DisplayIcon":          filepath.Join(l.InstallDir, "zenplus-agent-app.exe"),
		"UninstallString":      uninstallCommand,
		"QuietUninstallString": uninstallCommand,
		"InstallScope":         l.Scope,
	}
	for name, value := range values {
		if err := key.SetStringValue(name, value); err != nil {
			return err
		}
	}
	if err := key.SetDWordValue("NoModify", 1); err != nil {
		return err
	}
	if err := key.SetDWordValue("NoRepair", 1); err != nil {
		return err
	}
	if size, err := installedSizeKB(l.InstallDir); err == nil {
		_ = key.SetDWordValue("EstimatedSize", uint32(size))
	}
	return nil
}

func removeUninstallRegistry(l layout) error {
	root := registry.CURRENT_USER
	if l.Scope == "machine" {
		root = registry.LOCAL_MACHINE
	}
	return registry.DeleteKey(root, `Software\Microsoft\Windows\CurrentVersion\Uninstall\ZenPlus Agent`)
}

func installedSizeKB(path string) (int, error) {
	var total int64
	err := filepath.WalkDir(path, func(_ string, entry os.DirEntry, err error) error {
		if err != nil || entry.IsDir() {
			return err
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		total += info.Size()
		return nil
	})
	return int(total / 1024), err
}

func quote(value string) string {
	return `"` + strings.ReplaceAll(value, `"`, `\"`) + `"`
}

func scopeArg(l layout) string {
	if l.Scope == "machine" {
		return "/machine"
	}
	return "/user"
}

func isElevated() bool {
	token := windows.GetCurrentProcessToken()
	return token.IsElevated()
}

func relaunchElevated(args []string) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	argv := make([]string, len(args))
	for i, arg := range args {
		argv[i] = quote(arg)
	}
	err = windows.ShellExecute(0, windows.StringToUTF16Ptr("runas"), windows.StringToUTF16Ptr(exe), windows.StringToUTF16Ptr(strings.Join(argv, " ")), nil, windows.SW_SHOWNORMAL)
	if err != nil {
		return fmt.Errorf("administrator approval is required: %w", err)
	}
	return nil
}

func ensureArg(args []string, required string) []string {
	requiredNorm := strings.TrimLeft(strings.ToLower(required), "-/")
	for _, arg := range args {
		if strings.TrimLeft(strings.ToLower(arg), "-/") == requiredNorm {
			return args
		}
	}
	out := make([]string, 0, len(args)+1)
	out = append(out, args...)
	out = append(out, required)
	return out
}

func logStep(opts options, format string, args ...any) {
	if opts.quiet {
		return
	}
	fmt.Printf("[%s] %s\n", time.Now().Format("15:04:05"), fmt.Sprintf(format, args...))
}

func hasQuietArg(args []string) bool {
	for _, arg := range args {
		arg = strings.TrimLeft(strings.ToLower(strings.TrimSpace(arg)), "-/")
		if arg == "quiet" || arg == "qn" {
			return true
		}
	}
	return false
}

func showMessage(title, message string, isError bool) {
	flags := uint32(windows.MB_OK | windows.MB_ICONINFORMATION)
	if isError {
		flags = windows.MB_OK | windows.MB_ICONERROR
	}
	_, _ = windows.MessageBox(0, windows.StringToUTF16Ptr(message), windows.StringToUTF16Ptr(title), flags)
}
