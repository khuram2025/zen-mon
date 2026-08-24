//go:build windows

package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/registry"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"

	apmruntime "zenplus-agent/internal/apm"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
	agentruntime "zenplus-agent/internal/runtime"
)

const (
	productName   = "ZenPlus Agent"
	publisherName = "Zentryc"
	serviceName   = "ZenPlusAgent"
)

type payloadFile struct {
	Name string
	Data []byte
	Mode fs.FileMode
}

type options struct {
	quiet         bool
	verifyPayload bool
	uninstall     bool
	purge         bool
	noStartMenu   bool
	noRestart     bool
	machine       bool
	user          bool
	fromTemp      bool
	autoUninstall bool
	managedByMSI  bool
	controllerURL string
	apmMode       string
	profile       string
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
	if opts.verifyPayload {
		return verifyEmbeddedInstallerPayload()
	}
	elevated := isElevated()
	if !opts.uninstall && !opts.fromTemp {
		if relaunched, err := relaunchSetupFromTempIfInstalled(elevated, os.Args[1:], opts.quiet); err != nil {
			return err
		} else if relaunched {
			return nil
		}
	}
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

func relaunchSetupFromTempIfInstalled(elevated bool, args []string, wait bool) (bool, error) {
	for _, scoped := range []options{{machine: true}, {user: true}} {
		l, err := newLayout(scoped, elevated)
		if err != nil {
			return false, err
		}
		if !selfInside(l.InstallDir) {
			continue
		}
		tempExe, err := copySelfToTemp()
		if err != nil {
			return false, err
		}
		cmd := exec.Command(tempExe, ensureArg(args, "/from-temp")...)
		if !wait {
			if err := cmd.Start(); err != nil {
				_ = os.RemoveAll(filepath.Dir(tempExe))
				return false, err
			}
			return true, nil
		}
		defer os.RemoveAll(filepath.Dir(tempExe))
		cmd.SysProcAttr = hiddenSysProcAttr()
		var stderr bytes.Buffer
		cmd.Stderr = &stderr
		if err := cmd.Run(); err != nil {
			detail := strings.TrimSpace(stderr.String())
			if detail != "" {
				return true, fmt.Errorf("temporary setup failed: %w: %s", err, detail)
			}
			return true, fmt.Errorf("temporary setup failed: %w", err)
		}
		return true, nil
	}
	return false, nil
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
			case "APM_ENABLED":
				normalized = append(normalized, "-apm-enabled", value)
			case "INSTALL_PROFILE":
				normalized = append(normalized, "-profile", value)
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
	fs.BoolVar(&opts.verifyPayload, "verify-payload", false, "verify the embedded installer payload and exit")
	fs.BoolVar(&opts.uninstall, "uninstall", false, "uninstall ZenPlus Agent")
	fs.BoolVar(&opts.purge, "purge", false, "remove ProgramData state during uninstall")
	fs.BoolVar(&opts.noStartMenu, "no-start-menu", false, "skip Start Menu shortcut creation")
	fs.BoolVar(&opts.noRestart, "norestart", false, "accepted for deployment compatibility")
	fs.BoolVar(&opts.machine, "machine", false, "install for all users with the Windows service")
	fs.BoolVar(&opts.user, "user", false, "install for the current user without elevation")
	fs.BoolVar(&opts.fromTemp, "from-temp", false, "internal uninstall continuation")
	fs.BoolVar(&opts.autoUninstall, "auto-uninstall", false, "internal UI uninstall continuation")
	fs.BoolVar(&opts.managedByMSI, "managed-by-msi", false, "let Windows Installer own Apps and Features registration")
	fs.StringVar(&opts.controllerURL, "controller-url", "", "controller URL")
	fs.StringVar(&opts.apmMode, "apm-enabled", "", "enable or disable local APM monitoring")
	fs.StringVar(&opts.profile, "profile", "", "installation profile: infrastructure, apm, or combined")
	fs.SetOutput(os.Stderr)
	if err := fs.Parse(normalized); err != nil {
		return opts, err
	}
	if opts.apmMode != "" {
		switch strings.ToLower(strings.TrimSpace(opts.apmMode)) {
		case "1", "true", "yes", "enabled":
			opts.apmMode = "enabled"
		case "0", "false", "no", "disabled":
			opts.apmMode = "disabled"
		default:
			return opts, fmt.Errorf("apm-enabled must be true or false")
		}
	}
	if opts.profile != "" {
		opts.profile = strings.ToLower(strings.TrimSpace(opts.profile))
		if opts.profile != "infrastructure" && opts.profile != "apm" && opts.profile != "combined" {
			return opts, fmt.Errorf("profile must be infrastructure, apm, or combined")
		}
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

func preflightInstall(opts options) ([]payloadFile, error) {
	candidate := config.Default()
	if opts.controllerURL != "" {
		normalized, err := config.NormalizeControllerURL(opts.controllerURL)
		if err != nil {
			return nil, fmt.Errorf("validate controller URL: %w", err)
		}
		candidate.ControllerURL = normalized
	}
	profile := opts.profile
	if profile == "" && opts.apmMode != "" {
		if opts.apmMode == "disabled" {
			profile = "infrastructure"
		} else {
			profile = "combined"
		}
	}
	if profile != "" {
		if err := config.ApplyProfile(&candidate, profile); err != nil {
			return nil, fmt.Errorf("validate monitoring profile: %w", err)
		}
	}
	if err := candidate.Validate(); err != nil {
		return nil, fmt.Errorf("validate installation configuration: %w", err)
	}
	payloads, err := verifiedEmbeddedInstallerPayloads()
	if err != nil {
		return nil, err
	}
	return payloads, nil
}

func verifyEmbeddedInstallerPayload() error {
	_, err := verifiedEmbeddedInstallerPayloads()
	return err
}

func verifiedEmbeddedInstallerPayloads() ([]payloadFile, error) {
	payloads, err := embeddedPayloads()
	if err != nil {
		return nil, err
	}
	if err := validateEmbeddedPayloads(payloads); err != nil {
		return nil, err
	}
	return payloads, nil
}

func validateEmbeddedPayloads(payloads []payloadFile) error {
	byName := make(map[string][]byte, len(payloads))
	for _, payload := range payloads {
		name, err := canonicalPayloadName(payload.Name)
		if err != nil {
			return err
		}
		// NuGet packages legitimately use zero-byte `_._` placeholder files.
		// APM files are accepted here only provisionally: the signed bundle
		// manifest below must list every one with its exact size and SHA-256.
		if len(payload.Data) == 0 && !strings.HasPrefix(name, "apm/") {
			return fmt.Errorf("installer payload %q is empty", payload.Name)
		}
		if _, duplicate := byName[name]; duplicate {
			return fmt.Errorf("installer payload %q is duplicated", payload.Name)
		}
		byName[name] = payload.Data
	}
	requiredFiles := []string{
		"zenplus-agent.exe",
		"zenplus-agentctl.exe",
		"zenplus-agent-app.exe",
		"zenplus-agent-user.exe",
		"apm/bundle-manifest.json",
		"apm/gateway/zenplus-telemetry-gateway.exe",
		"apm/instrumentation/dotnet/net/opentelemetry.autoinstrumentation.startuphook.dll",
		"apm/instrumentation/dotnet/win-x64/opentelemetry.autoinstrumentation.native.dll",
		"apm/instrumentation/java/opentelemetry-javaagent.jar",
		"apm/instrumentation/node/bootstrap.js",
		"apm/instrumentation/node/node_modules/@opentelemetry/auto-instrumentations-node/package.json",
		"apm/instrumentation/python/wheelhouse/opentelemetry_distro-0.65b0-py3-none-any.whl",
		"apm/instrumentation/python/wheelhouse/opentelemetry_instrumentation_flask-0.65b0-py3-none-any.whl",
		"apm/instrumentation/python/wheelhouse/opentelemetry_instrumentation_requests-0.65b0-py3-none-any.whl",
		"apm/instrumentation/python/install-zenpluspythontracing.ps1",
		"apm/instrumentation/python/constraints.txt",
		"apm/instrumentation/python/readme.txt",
	}
	for _, name := range requiredFiles {
		if len(byName[name]) == 0 {
			return fmt.Errorf("installer APM/server payload is incomplete: required file %q is missing", name)
		}
	}
	var manifest struct {
		Components []struct {
			Name string `json:"name"`
		} `json:"components"`
		Files []struct {
			Path   string `json:"path"`
			Size   int64  `json:"size"`
			SHA256 string `json:"sha256"`
		} `json:"files"`
	}
	if err := json.Unmarshal(byName["apm/bundle-manifest.json"], &manifest); err != nil {
		return fmt.Errorf("parse embedded APM bundle manifest: %w", err)
	}
	components := make(map[string]bool, len(manifest.Components))
	for _, component := range manifest.Components {
		name := strings.ToLower(strings.TrimSpace(component.Name))
		if name == "" {
			return fmt.Errorf("installer APM bundle manifest contains an unnamed component")
		}
		if components[name] {
			return fmt.Errorf("installer APM bundle manifest duplicates component %q", component.Name)
		}
		components[name] = true
	}
	for _, name := range []string{
		"zenplus-telemetry-gateway",
		"opentelemetry-dotnet-auto",
		"opentelemetry-javaagent",
		"opentelemetry-node-auto",
		"opentelemetry-python-auto",
	} {
		if !components[name] {
			return fmt.Errorf("installer APM bundle is incomplete: component %q is missing", name)
		}
	}
	if len(manifest.Files) == 0 {
		return fmt.Errorf("installer APM bundle manifest has no file inventory")
	}
	seenManifestFiles := make(map[string]bool, len(manifest.Files))
	for _, file := range manifest.Files {
		manifestName, err := canonicalManifestFileName(file.Path)
		if err != nil {
			return err
		}
		name := "apm/" + manifestName
		if seenManifestFiles[name] {
			return fmt.Errorf("installer APM bundle manifest duplicates file %q", file.Path)
		}
		seenManifestFiles[name] = true
		data, found := byName[name]
		if !found {
			return fmt.Errorf("installer APM bundle manifest file %q is missing", file.Path)
		}
		if file.Size < 0 || int64(len(data)) != file.Size {
			return fmt.Errorf("installer APM bundle manifest file %q has size %d, expected %d", file.Path, len(data), file.Size)
		}
		digest := sha256.Sum256(data)
		want := strings.TrimSpace(file.SHA256)
		if len(want) != sha256.Size*2 || !strings.EqualFold(hex.EncodeToString(digest[:]), want) {
			return fmt.Errorf("installer APM bundle manifest file %q failed SHA-256 verification", file.Path)
		}
	}
	for name := range byName {
		if name == "apm/bundle-manifest.json" || !strings.HasPrefix(name, "apm/") {
			continue
		}
		if !seenManifestFiles[name] {
			return fmt.Errorf("installer APM payload %q is not listed in the bundle manifest", name)
		}
	}
	return nil
}

func canonicalPayloadName(name string) (string, error) {
	name = strings.TrimSpace(filepath.ToSlash(name))
	if name == "" {
		return "", fmt.Errorf("installer payload name is empty")
	}
	if strings.HasPrefix(name, "/") || filepath.IsAbs(filepath.FromSlash(name)) {
		return "", fmt.Errorf("installer payload %q uses an absolute path", name)
	}
	clean := filepath.ToSlash(filepath.Clean(filepath.FromSlash(name)))
	if clean == "." || clean == ".." || strings.HasPrefix(clean, "../") {
		return "", fmt.Errorf("installer payload %q escapes the installation directory", name)
	}
	return strings.ToLower(clean), nil
}

func canonicalManifestFileName(name string) (string, error) {
	clean, err := canonicalPayloadName(name)
	if err != nil {
		return "", fmt.Errorf("invalid APM bundle manifest path %q: %w", name, err)
	}
	if clean == "bundle-manifest.json" || strings.HasPrefix(clean, "apm/") {
		return "", fmt.Errorf("invalid APM bundle manifest path %q", name)
	}
	return clean, nil
}

func stopManagedWindowsServices(targets []string) ([]string, error) {
	if len(targets) == 0 {
		return nil, nil
	}
	manager, err := mgr.Connect()
	if err != nil {
		return nil, fmt.Errorf("connect to Windows Service Control Manager for application upgrade: %w", err)
	}
	defer manager.Disconnect()
	stopped := make([]string, 0, len(targets))
	for _, target := range targets {
		if strings.HasPrefix(strings.ToLower(strings.TrimSpace(target)), "zenplus") {
			_ = startManagedWindowsServices(stopped)
			return nil, fmt.Errorf("refusing to stop protected service %q from instrumentation state", target)
		}
		service, err := manager.OpenService(target)
		if err != nil {
			_ = startManagedWindowsServices(stopped)
			return nil, fmt.Errorf("open managed application service %q: %w", target, err)
		}
		status, queryErr := service.Query()
		if queryErr == nil && status.State == svc.Running {
			_, queryErr = service.Control(svc.Stop)
			if queryErr == nil {
				queryErr = waitForInstallerServiceState(service, svc.Stopped, 45*time.Second)
			}
			if queryErr == nil {
				stopped = append(stopped, target)
			}
		} else if queryErr == nil && status.State != svc.Stopped {
			queryErr = fmt.Errorf("service is in state %d", status.State)
		}
		_ = service.Close()
		if queryErr != nil {
			_ = startManagedWindowsServices(stopped)
			return nil, fmt.Errorf("stop managed application service %q for profiler upgrade: %w", target, queryErr)
		}
	}
	return stopped, nil
}

func startManagedWindowsServices(targets []string) error {
	if len(targets) == 0 {
		return nil
	}
	manager, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect to Windows Service Control Manager to restore applications: %w", err)
	}
	defer manager.Disconnect()
	var startErrors []error
	for _, target := range targets {
		service, err := manager.OpenService(target)
		if err == nil {
			status, queryErr := service.Query()
			err = queryErr
			if err == nil && status.State != svc.Running {
				err = service.Start()
				if err == nil {
					err = waitForInstallerServiceState(service, svc.Running, 45*time.Second)
				}
			}
			_ = service.Close()
		}
		if err != nil {
			startErrors = append(startErrors, fmt.Errorf("start managed application service %q: %w", target, err))
		}
	}
	return errors.Join(startErrors...)
}

func waitForInstallerServiceState(service *mgr.Service, wanted svc.State, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		status, err := service.Query()
		if err != nil {
			return err
		}
		if status.State == wanted {
			return nil
		}
		time.Sleep(500 * time.Millisecond)
	}
	return fmt.Errorf("timed out waiting for service state %d", wanted)
}

func stopManagedIISPools(targets []string) ([]string, error) {
	stopped := make([]string, 0, len(targets))
	for _, target := range targets {
		state, err := managedIISPoolState(target)
		if err != nil {
			_ = startManagedIISPools(stopped)
			return nil, err
		}
		if strings.EqualFold(state, "Stopped") {
			continue
		}
		if !strings.EqualFold(state, "Started") {
			_ = startManagedIISPools(stopped)
			return nil, fmt.Errorf("IIS application pool %q is in state %q; retry after it reaches Started or Stopped", target, state)
		}
		if err := runIISAppCmd("stop", "apppool", target); err != nil {
			_ = startManagedIISPools(stopped)
			return nil, fmt.Errorf("stop managed IIS application pool %q for profiler upgrade: %w", target, err)
		}
		stopped = append(stopped, target)
	}
	return stopped, nil
}

func startManagedIISPools(targets []string) error {
	var startErrors []error
	for _, target := range targets {
		if err := runIISAppCmd("start", "apppool", target); err != nil {
			startErrors = append(startErrors, fmt.Errorf("start IIS application pool %q: %w", target, err))
		}
	}
	return errors.Join(startErrors...)
}

func managedIISPoolState(target string) (string, error) {
	output, err := runIISAppCmdOutput("list", "apppool", target, "/text:state")
	if err != nil {
		return "", fmt.Errorf("read IIS application pool %q state: %w", target, err)
	}
	state := strings.TrimSpace(output)
	if state == "" {
		return "", fmt.Errorf("IIS application pool %q returned an empty state", target)
	}
	return state, nil
}

func runIISAppCmd(args ...string) error {
	_, err := runIISAppCmdOutput(args...)
	return err
}

func runIISAppCmdOutput(args ...string) (string, error) {
	windowsDir := os.Getenv("WINDIR")
	if windowsDir == "" {
		windowsDir = `C:\Windows`
	}
	appcmd := filepath.Join(windowsDir, "System32", "inetsrv", "appcmd.exe")
	cmd := exec.Command(appcmd, args...)
	cmd.SysProcAttr = hiddenSysProcAttr()
	output, err := cmd.CombinedOutput()
	if err != nil {
		detail := strings.TrimSpace(string(output))
		if detail != "" {
			return detail, fmt.Errorf("%w: %s", err, detail)
		}
		return "", err
	}
	return string(output), nil
}

func install(l layout, opts options) (returnErr error) {
	logStep(opts, "Installing %s %s (%s)", productName, model.AgentVersion, l.Scope)
	payloads, err := preflightInstall(opts)
	if err != nil {
		return err
	}
	// Validate the requested configuration while the old installation is still
	// fully available. It is rebuilt after quiescing the agent so a policy
	// update that lands during payload staging is not overwritten.
	if _, err := buildInstalledConfig(l, opts); err != nil {
		return fmt.Errorf("prepare installed configuration: %w", err)
	}
	stageDir, err := stageInstallPayloads(l, payloads)
	if err != nil {
		return err
	}
	stageNeedsCleanup := true
	defer func() {
		if stageNeedsCleanup {
			_ = os.RemoveAll(stageDir)
		}
	}()

	apmPaths := agentruntime.NewPaths(l.DataDir)
	managedPools, err := apmruntime.ManagedIISTargets(apmPaths.APMInstrumentationState)
	if err != nil {
		return fmt.Errorf("inspect managed IIS instrumentation before upgrade: %w", err)
	}
	managedServices, err := apmruntime.ManagedWindowsServiceTargets(apmPaths.APMInstrumentationState)
	if err != nil {
		return fmt.Errorf("inspect managed Windows-service instrumentation before upgrade: %w", err)
	}

	var serviceBefore agentServiceState
	var configBefore fileSnapshot
	var nextConfig config.Config
	var transaction *installDirectoryTransaction
	var stoppedServices, stoppedPools []string
	serviceTouched := false
	configChanged := false
	transactionComplete := false
	defer func() {
		if transactionComplete {
			return
		}
		var recoveryErrors []error
		if transaction != nil {
			if err := quiesceManagedTargetsForRollback(managedServices, managedPools); err != nil {
				recoveryErrors = append(recoveryErrors, fmt.Errorf("quiesce managed applications for install rollback: %w", err))
			}
		}
		if l.Scope == "machine" && (serviceTouched || transaction != nil) {
			if _, err := quiesceAgentService(); err != nil {
				recoveryErrors = append(recoveryErrors, fmt.Errorf("quiesce agent for install rollback: %w", err))
			}
		}
		if transaction != nil {
			terminateZenPlusProcesses(l, 15*time.Second)
			if err := transaction.Rollback(); err != nil {
				recoveryErrors = append(recoveryErrors, err)
			}
		}
		if configChanged {
			if err := configBefore.Restore(); err != nil {
				recoveryErrors = append(recoveryErrors, err)
			}
		}
		if l.Scope == "machine" && serviceTouched {
			if serviceBefore.Exists {
				if err := resumeExistingAgentService(serviceBefore); err != nil {
					recoveryErrors = append(recoveryErrors, fmt.Errorf("restore prior agent service state: %w", err))
				}
			} else if err := uninstallService(l); err != nil {
				recoveryErrors = append(recoveryErrors, fmt.Errorf("remove service created by failed install: %w", err))
			}
		} else if l.Scope == "user" && transaction != nil && transaction.HadInstall {
			if err := launchUserRunner(l); err != nil {
				recoveryErrors = append(recoveryErrors, fmt.Errorf("restore prior user agent: %w", err))
			}
		}
		if err := startManagedIISPools(stoppedPools); err != nil {
			recoveryErrors = append(recoveryErrors, fmt.Errorf("restore managed IIS pools after install rollback: %w", err))
		}
		if err := startManagedWindowsServices(stoppedServices); err != nil {
			recoveryErrors = append(recoveryErrors, fmt.Errorf("restore managed services after install rollback: %w", err))
		}
		returnErr = errors.Join(returnErr, errors.Join(recoveryErrors...))
	}()

	if l.Scope == "machine" {
		serviceBefore, err = quiesceAgentService()
		serviceTouched = serviceBefore.Exists
		if err != nil {
			return err
		}
	}
	terminateZenPlusProcesses(l, 15*time.Second)
	// Re-read both the instrumentation state and configuration only after the
	// agent is stopped. Remote policy/config commands can otherwise race the
	// potentially long embedded-payload staging step.
	managedPools, err = apmruntime.ManagedIISTargets(apmPaths.APMInstrumentationState)
	if err != nil {
		return fmt.Errorf("inspect quiesced IIS instrumentation before upgrade: %w", err)
	}
	managedServices, err = apmruntime.ManagedWindowsServiceTargets(apmPaths.APMInstrumentationState)
	if err != nil {
		return fmt.Errorf("inspect quiesced Windows-service instrumentation before upgrade: %w", err)
	}
	configBefore, err = captureFileSnapshot(l.ConfigPath)
	if err != nil {
		return err
	}
	nextConfig, err = buildInstalledConfig(l, opts)
	if err != nil {
		return fmt.Errorf("prepare quiesced installed configuration: %w", err)
	}
	stoppedServices, err = stopManagedWindowsServices(managedServices)
	if err != nil {
		return err
	}
	stoppedPools, err = stopManagedIISPools(managedPools)
	if err != nil {
		return err
	}
	transaction, err = activateStagedInstall(stageDir, l.InstallDir)
	if err != nil {
		return err
	}
	stageNeedsCleanup = false
	if err := os.MkdirAll(l.ConfigDir, 0o755); err != nil {
		return err
	}
	if l.Scope == "machine" {
		if err := hardenMachineDataTree(l.DataDir, serviceName); err != nil {
			return fmt.Errorf("secure machine data directory: %w", err)
		}
	}
	configChanged = true
	if err := config.Save(l.ConfigPath, nextConfig); err != nil {
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
	if opts.managedByMSI {
		// Remove the legacy EXE registration when upgrading into the MSI-owned
		// product so Apps & Features shows one authoritative entry.
		_ = removeUninstallRegistry(l)
	} else {
		if err := writeUninstallRegistry(l); err != nil {
			return err
		}
	}
	if l.Scope == "machine" {
		serviceTouched = true
		if serviceBefore.Exists {
			if err := resumeExistingAgentService(serviceBefore); err != nil {
				return err
			}
		} else if err := installService(l); err != nil {
			return err
		}
	} else if err := launchUserRunner(l); err != nil {
		return err
	}
	if err := startManagedIISPools(stoppedPools); err != nil {
		return fmt.Errorf("restart managed IIS application pools after profiler upgrade: %w", err)
	}
	if err := startManagedWindowsServices(stoppedServices); err != nil {
		return fmt.Errorf("restart managed application services after profiler upgrade: %w", err)
	}
	stoppedPools = nil
	stoppedServices = nil
	transactionComplete = true
	removeLegacyRuntime(l)
	removeOppositeScopeShortcuts(l)
	if err := transaction.CleanupBackup(); err != nil {
		logStep(opts, "Installation completed, but cleanup of the prior payload was deferred: %v", err)
	}
	logStep(opts, "%s installed successfully.", productName)
	if !opts.quiet {
		showMessage(productName+" Setup", productName+" was installed successfully.", false)
	}
	return nil
}

func uninstall(l layout, opts options) (returnErr error) {
	logStep(opts, "Uninstalling %s (%s)", productName, l.Scope)
	var serviceBefore agentServiceState
	serviceQuiesced := false
	serviceDeleted := false
	defer func() {
		if returnErr == nil || l.Scope != "machine" || !serviceQuiesced || serviceDeleted {
			return
		}
		if err := resumeExistingAgentService(serviceBefore); err != nil {
			returnErr = errors.Join(returnErr, fmt.Errorf("restart agent after failed uninstall: %w", err))
		}
	}()
	if l.Scope == "machine" {
		var err error
		serviceBefore, err = quiesceAgentService()
		serviceQuiesced = serviceBefore.Exists
		if err != nil {
			return err
		}
	}
	terminateZenPlusProcesses(l, 15*time.Second)
	rollbackCtx, cancelRollback := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancelRollback()
	apmPaths := agentruntime.NewPaths(l.DataDir)
	if err := apmruntime.RollbackAll(rollbackCtx, apmPaths.APMInstrumentationState, l.InstallDir, true); err != nil {
		return fmt.Errorf("restore managed application instrumentation before uninstall: %w", err)
	}
	if l.Scope == "machine" {
		if err := uninstallService(l); err != nil {
			return err
		}
		serviceDeleted = true
	}
	_ = agentruntime.RemoveMachineDashboardSnapshot(l.DataDir)
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

func terminateZenPlusProcesses(l layout, timeout time.Duration) {
	names := map[string]bool{
		"zenplus-agent-app.exe":         true,
		"zenplus-agent.exe":             true,
		"zenplus-agent-user.exe":        true,
		"zenplus-telemetry-gateway.exe": true,
	}
	roots := []string{l.InstallDir, filepath.Join(l.DataDir, "bin")}
	currentPID := uint32(os.Getpid())
	deadline := time.Now().Add(timeout)
	for {
		pids := findProcesses(names, roots, currentPID)
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

func findProcesses(names map[string]bool, roots []string, currentPID uint32) []uint32 {
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
		if entry.ProcessID != currentPID && names[name] && processInsideAnyRoot(entry.ProcessID, roots) {
			pids = append(pids, entry.ProcessID)
		}
		if err := windows.Process32Next(snapshot, &entry); err != nil {
			break
		}
	}
	return pids
}

func processInsideAnyRoot(pid uint32, roots []string) bool {
	handle, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil {
		return false
	}
	defer windows.CloseHandle(handle)
	buffer := make([]uint16, 32768)
	size := uint32(len(buffer))
	if err := windows.QueryFullProcessImageName(handle, 0, &buffer[0], &size); err != nil || size == 0 {
		return false
	}
	imagePath := windows.UTF16ToString(buffer[:size])
	for _, root := range roots {
		if strings.TrimSpace(root) != "" && pathWithin(root, imagePath) {
			return true
		}
	}
	return false
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

func buildInstalledConfig(l layout, opts options) (config.Config, error) {
	cfg, err := config.LoadForEdit(l.ConfigPath)
	if err != nil {
		return config.Config{}, err
	}
	cfg.DataDir = l.DataDir
	if opts.controllerURL != "" {
		normalized, err := config.NormalizeControllerURL(opts.controllerURL)
		if err != nil {
			return config.Config{}, err
		}
		cfg.ControllerURL = normalized
	}
	if opts.profile != "" {
		if err := config.ApplyProfile(&cfg, opts.profile); err != nil {
			return config.Config{}, err
		}
	} else if opts.apmMode != "" {
		profile := cfg.APM.Profile
		if opts.apmMode == "disabled" {
			profile = "infrastructure"
		} else if profile == "" || profile == "infrastructure" {
			profile = "combined"
		}
		if err := config.ApplyProfile(&cfg, profile); err != nil {
			return config.Config{}, err
		}
	} else if cfg.APM.Profile != "apm" && config.InfrastructureCollectorsDisabled(cfg) {
		profile := cfg.APM.Profile
		if profile == "" {
			profile = "combined"
		}
		if err := config.ApplyProfile(&cfg, profile); err != nil {
			return config.Config{}, err
		}
	}
	// Authorization, site placement, and policy assignment are controlled by
	// the appliance. Clear legacy bootstrap values during upgrades so the
	// endpoint stores only its controller connection setting.
	cfg.PolicyID = ""
	if err := cfg.Validate(); err != nil {
		return config.Config{}, err
	}
	return cfg, nil

}

func writeInstalledConfig(l layout, opts options) error {
	cfg, err := buildInstalledConfig(l, opts)
	if err != nil {
		return err
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
		strings.Contains(text, "specified service does not exist")
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
	defer os.RemoveAll(tempDir)
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
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		detail := strings.TrimSpace(stderr.String())
		if detail != "" {
			return true, fmt.Errorf("temporary uninstaller failed: %w: %s", err, detail)
		}
		return true, fmt.Errorf("temporary uninstaller failed: %w", err)
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
	if err := scheduleDeleteOnReboot(path); err != nil {
		return errors.Join(lastErr, err)
	}
	return nil
}

func scheduleDeleteOnReboot(path string) error {
	paths := make([]string, 0, 8)
	if err := filepath.WalkDir(path, func(p string, _ os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		paths = append(paths, p)
		return nil
	}); err != nil {
		return fmt.Errorf("enumerate deferred-delete paths: %w", err)
	}
	sort.SliceStable(paths, func(i, j int) bool {
		return strings.Count(paths[i], string(filepath.Separator)) > strings.Count(paths[j], string(filepath.Separator))
	})
	var deleteErrors []error
	for _, target := range paths {
		ptr, err := windows.UTF16PtrFromString(target)
		if err == nil {
			err = windows.MoveFileEx(ptr, nil, windows.MOVEFILE_DELAY_UNTIL_REBOOT)
		}
		if err != nil {
			deleteErrors = append(deleteErrors, fmt.Errorf("schedule deletion of %q: %w", target, err))
		}
	}
	return errors.Join(deleteErrors...)
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
		argv[i] = windows.EscapeArg(arg)
	}
	verb, err := windows.UTF16PtrFromString("runas")
	if err != nil {
		return err
	}
	file, err := windows.UTF16PtrFromString(exe)
	if err != nil {
		return err
	}
	parameters, err := windows.UTF16PtrFromString(strings.Join(argv, " "))
	if err != nil {
		return err
	}
	info := shellExecuteInfo{
		Mask:       seeMaskNoCloseProcess,
		Verb:       verb,
		File:       file,
		Parameters: parameters,
		Show:       int32(windows.SW_SHOWNORMAL),
	}
	info.Size = uint32(unsafe.Sizeof(info))
	ok, _, callErr := shellExecuteExW.Call(uintptr(unsafe.Pointer(&info)))
	if ok == 0 {
		return fmt.Errorf("administrator approval is required: %w", callErr)
	}
	if info.Process == 0 {
		return fmt.Errorf("elevated setup did not return a process handle")
	}
	defer windows.CloseHandle(info.Process)
	waitResult, err := windows.WaitForSingleObject(info.Process, windows.INFINITE)
	if err != nil {
		return fmt.Errorf("wait for elevated setup: %w", err)
	}
	if waitResult != windows.WAIT_OBJECT_0 {
		return fmt.Errorf("wait for elevated setup returned status 0x%x", waitResult)
	}
	var exitCode uint32
	if err := windows.GetExitCodeProcess(info.Process, &exitCode); err != nil {
		return fmt.Errorf("read elevated setup result: %w", err)
	}
	if exitCode != 0 {
		return fmt.Errorf("elevated setup failed with exit code %d", exitCode)
	}
	return nil
}

const seeMaskNoCloseProcess = 0x00000040

var shellExecuteExW = windows.NewLazySystemDLL("shell32.dll").NewProc("ShellExecuteExW")

// shellExecuteInfo mirrors SHELLEXECUTEINFOW. The process handle is requested
// so the unelevated wizard can report the real result of its elevated child.
type shellExecuteInfo struct {
	Size          uint32
	Mask          uint32
	Window        windows.Handle
	Verb          *uint16
	File          *uint16
	Parameters    *uint16
	Directory     *uint16
	Show          int32
	Instance      windows.Handle
	IDList        uintptr
	Class         *uint16
	ClassKey      windows.Handle
	HotKey        uint32
	IconOrMonitor windows.Handle
	Process       windows.Handle
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
