//go:build windows

package main

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

// fileSnapshot preserves the exact pre-install configuration bytes. Installer
// profile changes are intentionally reversible until the new service and every
// managed application target have restarted successfully.
type fileSnapshot struct {
	path   string
	exists bool
	data   []byte
	mode   fs.FileMode
}

func captureFileSnapshot(path string) (fileSnapshot, error) {
	snapshot := fileSnapshot{path: path}
	info, err := os.Stat(path)
	if errors.Is(err, os.ErrNotExist) {
		return snapshot, nil
	}
	if err != nil {
		return fileSnapshot{}, fmt.Errorf("inspect existing file %q: %w", path, err)
	}
	if !info.Mode().IsRegular() {
		return fileSnapshot{}, fmt.Errorf("refusing to replace non-regular file %q", path)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return fileSnapshot{}, fmt.Errorf("read existing file %q: %w", path, err)
	}
	snapshot.exists = true
	snapshot.data = data
	snapshot.mode = info.Mode().Perm()
	return snapshot, nil
}

func (snapshot fileSnapshot) Restore() error {
	if strings.TrimSpace(snapshot.path) == "" {
		return fmt.Errorf("configuration snapshot path is empty")
	}
	if !snapshot.exists {
		if err := os.Remove(snapshot.path); err != nil && !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("remove newly-created configuration %q: %w", snapshot.path, err)
		}
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(snapshot.path), 0o755); err != nil {
		return fmt.Errorf("restore configuration directory: %w", err)
	}
	temp, err := os.CreateTemp(filepath.Dir(snapshot.path), ".zenplus-config-restore-*.tmp")
	if err != nil {
		return fmt.Errorf("create configuration restore file: %w", err)
	}
	tempPath := temp.Name()
	defer os.Remove(tempPath)
	if _, err := temp.Write(snapshot.data); err != nil {
		_ = temp.Close()
		return fmt.Errorf("restore configuration contents: %w", err)
	}
	if err := temp.Sync(); err != nil {
		_ = temp.Close()
		return fmt.Errorf("flush restored configuration: %w", err)
	}
	if err := temp.Close(); err != nil {
		return fmt.Errorf("close restored configuration: %w", err)
	}
	if err := os.Chmod(tempPath, snapshot.mode); err != nil {
		return fmt.Errorf("restore configuration mode: %w", err)
	}
	from, err := windows.UTF16PtrFromString(tempPath)
	if err != nil {
		return fmt.Errorf("encode configuration restore path: %w", err)
	}
	to, err := windows.UTF16PtrFromString(snapshot.path)
	if err != nil {
		return fmt.Errorf("encode configuration destination path: %w", err)
	}
	if err := windows.MoveFileEx(from, to, windows.MOVEFILE_REPLACE_EXISTING|windows.MOVEFILE_WRITE_THROUGH); err != nil {
		return fmt.Errorf("replace configuration with prior version: %w", err)
	}
	return nil
}

// stageInstallPayloads materializes the complete embedded product beside the
// final directory. Running services are not touched until every write succeeds.
func stageInstallPayloads(l layout, payloads []payloadFile) (string, error) {
	parent := filepath.Dir(filepath.Clean(l.InstallDir))
	if strings.TrimSpace(l.InstallDir) == "" || parent == "." || parent == filepath.Clean(l.InstallDir) {
		return "", fmt.Errorf("invalid installation directory %q", l.InstallDir)
	}
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return "", fmt.Errorf("create installation parent directory: %w", err)
	}
	stage, err := os.MkdirTemp(parent, ".zenplus-agent-stage-")
	if err != nil {
		return "", fmt.Errorf("create installation staging directory: %w", err)
	}
	keep := false
	defer func() {
		if !keep {
			_ = os.RemoveAll(stage)
		}
	}()
	for _, payload := range payloads {
		name, err := canonicalPayloadName(payload.Name)
		if err != nil {
			return "", err
		}
		target := filepath.Join(stage, filepath.FromSlash(name))
		if !pathWithin(stage, target) {
			return "", fmt.Errorf("installer payload %q escaped the staging directory", payload.Name)
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return "", fmt.Errorf("create staged payload directory for %q: %w", payload.Name, err)
		}
		if err := os.WriteFile(target, payload.Data, payload.Mode); err != nil {
			return "", fmt.Errorf("stage installer payload %q: %w", payload.Name, err)
		}
	}
	if err := copySelf(filepath.Join(stage, "ZenPlusAgentSetup.exe")); err != nil {
		return "", fmt.Errorf("stage uninstaller: %w", err)
	}
	keep = true
	return stage, nil
}

type installDirectoryTransaction struct {
	InstallDir string
	StageDir   string
	BackupDir  string
	HadInstall bool
}

func activateStagedInstall(stageDir, installDir string) (*installDirectoryTransaction, error) {
	stageDir = filepath.Clean(stageDir)
	installDir = filepath.Clean(installDir)
	parent := filepath.Dir(installDir)
	if !strings.EqualFold(filepath.Dir(stageDir), parent) || !strings.HasPrefix(strings.ToLower(filepath.Base(stageDir)), ".zenplus-agent-stage-") {
		return nil, fmt.Errorf("staging directory %q is not a generated sibling of %q", stageDir, installDir)
	}
	stageInfo, err := os.Stat(stageDir)
	if err != nil || !stageInfo.IsDir() {
		return nil, fmt.Errorf("installation staging directory is unavailable: %w", err)
	}
	transaction := &installDirectoryTransaction{
		InstallDir: installDir,
		StageDir:   stageDir,
		BackupDir:  stageDir + ".previous",
	}
	if _, err := os.Stat(transaction.BackupDir); err == nil {
		return nil, fmt.Errorf("installation backup path already exists: %s", transaction.BackupDir)
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("inspect installation backup path: %w", err)
	}
	if info, err := os.Lstat(installDir); err == nil {
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return nil, fmt.Errorf("refusing to replace non-directory installation path %q", installDir)
		}
		transaction.HadInstall = true
		if err := os.Rename(installDir, transaction.BackupDir); err != nil {
			return nil, fmt.Errorf("preserve prior installation: %w", err)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("inspect prior installation: %w", err)
	}
	if err := os.Rename(stageDir, installDir); err != nil {
		var restoreErr error
		if transaction.HadInstall {
			restoreErr = os.Rename(transaction.BackupDir, installDir)
		}
		return nil, errors.Join(
			fmt.Errorf("activate staged installation: %w", err),
			wrapInstallerRecoveryError("restore prior installation after activation failure", restoreErr),
		)
	}
	return transaction, nil
}

func (transaction *installDirectoryTransaction) Rollback() error {
	if transaction == nil {
		return nil
	}
	failedDir := transaction.StageDir + ".failed"
	if err := os.RemoveAll(failedDir); err != nil {
		return fmt.Errorf("clear failed-install quarantine: %w", err)
	}
	if _, err := os.Stat(transaction.InstallDir); err == nil {
		if err := os.Rename(transaction.InstallDir, failedDir); err != nil {
			return fmt.Errorf("quarantine failed installation: %w", err)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("inspect failed installation: %w", err)
	}
	if transaction.HadInstall {
		if err := os.Rename(transaction.BackupDir, transaction.InstallDir); err != nil {
			recoverErr := os.Rename(failedDir, transaction.InstallDir)
			return errors.Join(
				fmt.Errorf("restore prior installation: %w", err),
				wrapInstallerRecoveryError("put failed installation back after restore failure", recoverErr),
			)
		}
	}
	if err := os.RemoveAll(failedDir); err != nil {
		return fmt.Errorf("remove failed installation quarantine: %w", err)
	}
	return nil
}

func (transaction *installDirectoryTransaction) CleanupBackup() error {
	if transaction == nil || !transaction.HadInstall {
		return nil
	}
	if err := removeAllWithRetry(transaction.BackupDir, 20, 250*time.Millisecond); err != nil {
		return fmt.Errorf("remove prior installation backup: %w", err)
	}
	return nil
}

type agentServiceState struct {
	Exists         bool
	InitialState   svc.State
	Config         mgr.Config
	ConfigCaptured bool
}

func quiesceAgentService() (agentServiceState, error) {
	manager, err := mgr.Connect()
	if err != nil {
		return agentServiceState{}, fmt.Errorf("connect service manager: %w", err)
	}
	defer manager.Disconnect()
	service, err := manager.OpenService(serviceName)
	if err != nil {
		if serviceMissing(err) {
			return agentServiceState{}, nil
		}
		return agentServiceState{}, fmt.Errorf("open service: %w", err)
	}
	defer service.Close()
	status, err := service.Query()
	if err != nil {
		return agentServiceState{}, fmt.Errorf("query service: %w", err)
	}
	serviceConfig, err := service.Config()
	if err != nil {
		return agentServiceState{}, fmt.Errorf("read service configuration: %w", err)
	}
	state := agentServiceState{Exists: true, InitialState: status.State, Config: serviceConfig, ConfigCaptured: true}
	if err := stopOpenedService(service, 45*time.Second); err != nil {
		return state, fmt.Errorf("stop service safely: %w", err)
	}
	return state, nil
}

func stopOpenedService(service *mgr.Service, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	stopRequested := false
	for time.Now().Before(deadline) {
		status, err := service.Query()
		if err != nil {
			return err
		}
		switch status.State {
		case svc.Stopped:
			return nil
		case svc.StopPending:
			stopRequested = true
		case svc.Running, svc.Paused:
			if !stopRequested {
				if _, err := service.Control(svc.Stop); err != nil && !errors.Is(err, windows.ERROR_SERVICE_NOT_ACTIVE) {
					return err
				}
				stopRequested = true
			}
		}
		time.Sleep(250 * time.Millisecond)
	}
	return fmt.Errorf("timed out waiting for service to stop")
}

func resumeExistingAgentService(state agentServiceState) error {
	if !state.Exists || state.InitialState == svc.Stopped || state.InitialState == svc.StopPending {
		return nil
	}
	manager, err := mgr.Connect()
	if err != nil {
		return fmt.Errorf("connect service manager: %w", err)
	}
	defer manager.Disconnect()
	service, err := manager.OpenService(serviceName)
	if err != nil {
		return fmt.Errorf("open preserved service: %w", err)
	}
	defer service.Close()
	status, err := service.Query()
	if err != nil {
		return fmt.Errorf("query preserved service: %w", err)
	}
	if state.ConfigCaptured {
		if err := service.UpdateConfig(state.Config); err != nil {
			return fmt.Errorf("restore preserved service configuration: %w", err)
		}
	}
	if status.State != svc.Running && status.State != svc.Paused {
		if err := service.Start(); err != nil {
			return fmt.Errorf("restart preserved service: %w", err)
		}
		if err := waitForInstallerServiceState(service, svc.Running, 45*time.Second); err != nil {
			return fmt.Errorf("wait for preserved service: %w", err)
		}
	}
	if state.InitialState == svc.Paused {
		if _, err := service.Control(svc.Pause); err != nil {
			return fmt.Errorf("restore paused service state: %w", err)
		}
		if err := waitForInstallerServiceState(service, svc.Paused, 45*time.Second); err != nil {
			return fmt.Errorf("wait for paused service state: %w", err)
		}
	}
	return nil
}

func serviceStateForInstalledPayload(state agentServiceState, l layout) agentServiceState {
	if !state.Exists || !state.ConfigCaptured {
		return state
	}
	next := state
	next.Config.BinaryPathName = strings.Join([]string{
		syscall.EscapeArg(filepath.Join(l.InstallDir, "zenplus-agent.exe")),
		"service",
		"--config",
		syscall.EscapeArg(l.ConfigPath),
	}, " ")
	next.Config.StartType = mgr.StartAutomatic
	next.Config.DisplayName = productName
	next.Config.Description = "Collects Windows host telemetry and uploads it to the ZenPlus controller."
	return next
}

func quiesceManagedTargetsForRollback(serviceTargets, poolTargets []string) error {
	var recoveryErrors []error
	for _, target := range serviceTargets {
		if _, err := stopManagedWindowsServices([]string{target}); err != nil {
			recoveryErrors = append(recoveryErrors, err)
		}
	}
	for _, target := range poolTargets {
		if _, err := stopManagedIISPools([]string{target}); err != nil {
			recoveryErrors = append(recoveryErrors, err)
		}
	}
	return errors.Join(recoveryErrors...)
}

func wrapInstallerRecoveryError(operation string, err error) error {
	if err == nil {
		return nil
	}
	return fmt.Errorf("%s: %w", operation, err)
}
