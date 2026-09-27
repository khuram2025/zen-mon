package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
	"zenplus-agent/internal/apm"
	"zenplus-agent/internal/config"
	agentruntime "zenplus-agent/internal/runtime"
)

// Save only the editable fields after rereading the actual configuration.
// Neither installer payloads nor service registration are touched.
func saveSettingsFile(path, controller, profile string) error {
	profile = strings.ToLower(strings.TrimSpace(profile))
	if isMachineConfigPath(path) && !windows.GetCurrentProcessToken().IsElevated() {
		return fmt.Errorf("administrator approval is required to save all-users settings")
	}
	if _, err := os.Stat(path); err != nil {
		return fmt.Errorf("read installed configuration: %w", err)
	}
	cfg, err := config.LoadForEdit(path)
	if err != nil {
		return err
	}
	controller, err = config.NormalizeControllerURL(controller)
	if err != nil {
		return err
	}
	if cfg.APM.Enabled && profile == "infrastructure" {
		resolved, err := config.Load(path)
		if err != nil {
			return err
		}
		count, err := apm.ManagedInstrumentationCount(agentruntime.NewPaths(resolved.DataDir).APMInstrumentationState)
		if err != nil {
			return fmt.Errorf("check managed APM targets: %w", err)
		}
		if count > 0 {
			return fmt.Errorf("%d application target(s) are instrumented; uninstrument them before disabling APM", count)
		}
	}
	cfg.ControllerURL = controller
	if err := config.ApplyProfile(&cfg, profile); err != nil {
		return err
	}
	if err := cfg.Validate(); err != nil {
		return err
	}
	// Same-directory replacement prevents the running service seeing half a
	// YAML file. Protected directory permissions are inherited by the new file.
	f, err := os.CreateTemp(filepath.Dir(path), ".settings-*.yaml")
	if err != nil {
		return err
	}
	temp := f.Name()
	_ = f.Close()
	defer os.Remove(temp)
	if err := config.Save(temp, cfg); err != nil {
		return err
	}
	return os.Rename(temp, path)
}

func settingsHelperArgs(path, controller, profile string) []string {
	return []string{"--apply-settings", "--config", path, "--controller", controller, "--profile", profile}
}

func openElevatedSettings(path string) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	params, err := settingsParameters([]string{"--settings", "--config", path})
	if err != nil {
		return err
	}
	file, err := windows.UTF16PtrFromString(exe)
	if err != nil {
		return err
	}
	return windows.ShellExecute(0, windows.StringToUTF16Ptr("runas"), file, params, nil, windows.SW_SHOWNORMAL)
}

func settingsParameters(args []string) (*uint16, error) {
	quoted := make([]string, len(args))
	for i, arg := range args {
		quoted[i] = windows.EscapeArg(arg)
	}
	return windows.UTF16PtrFromString(strings.Join(quoted, " "))
}

type settingsShellExecuteInfo struct {
	Size, Mask                        uint32
	Window                            windows.Handle
	Verb, File, Parameters, Directory *uint16
	Show                              int32
	Instance                          windows.Handle
	IDList                            uintptr
	Class                             *uint16
	ClassKey                          windows.Handle
	HotKey                            uint32
	IconOrMonitor, Process            windows.Handle
}

func saveElevatedSettings(path, controller, profile string) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	file, err := windows.UTF16PtrFromString(exe)
	if err != nil {
		return err
	}
	params, err := settingsParameters(settingsHelperArgs(path, controller, profile))
	if err != nil {
		return err
	}
	info := settingsShellExecuteInfo{Mask: 0x40, Verb: windows.StringToUTF16Ptr("runas"), File: file, Parameters: params, Show: windows.SW_HIDE}
	info.Size = uint32(unsafe.Sizeof(info))
	proc := windows.NewLazySystemDLL("shell32.dll").NewProc("ShellExecuteExW")
	ok, _, callErr := proc.Call(uintptr(unsafe.Pointer(&info)))
	if ok == 0 {
		return fmt.Errorf("administrator approval: %w", callErr)
	}
	if info.Process == 0 {
		return fmt.Errorf("settings helper did not start")
	}
	defer windows.CloseHandle(info.Process)
	result, err := windows.WaitForSingleObject(info.Process, windows.INFINITE)
	if err != nil {
		return err
	}
	if result != windows.WAIT_OBJECT_0 {
		return fmt.Errorf("could not wait for settings save")
	}
	var code uint32
	if err := windows.GetExitCodeProcess(info.Process, &code); err != nil {
		return err
	}
	if code != 0 {
		return fmt.Errorf("settings were not saved (helper exit %d); see the settings error dialog", code)
	}
	return nil
}
