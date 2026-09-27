//go:build windows

package main

import (
	"bytes"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/lxn/walk"
	. "github.com/lxn/walk/declarative"
	"golang.org/x/sys/windows"

	"zenplus-agent/internal/brand"
)

var (
	setupBG      = walk.RGB(246, 248, 251)
	setupPanel   = walk.RGB(255, 255, 255)
	setupField   = walk.RGB(252, 253, 255)
	setupLine    = walk.RGB(226, 232, 240)
	setupText    = walk.RGB(15, 23, 42)
	setupMuted   = walk.RGB(83, 96, 116)
	setupAccent  = walk.RGB(0, 105, 98)
	setupBlue    = walk.RGB(12, 106, 228)
	setupGreen   = walk.RGB(22, 101, 52)
	setupAmber   = walk.RGB(146, 64, 14)
	setupRed     = walk.RGB(180, 35, 24)
	setupOverlay = walk.RGB(236, 250, 248)
)

type uninstallWindow struct {
	opts     options
	elevated bool

	window          *walk.MainWindow
	userScope       *walk.RadioButton
	machineScope    *walk.RadioButton
	purge           *walk.CheckBox
	progress        *walk.ProgressBar
	status          *walk.Label
	logs            *walk.TextEdit
	uninstallButton *walk.PushButton
	cancelButton    *walk.PushButton
	finished        bool
	working         bool
	resultErr       error
}

func runUninstallUI(opts options, elevated bool) error {
	ui := &uninstallWindow{opts: opts, elevated: elevated}
	return ui.run()
}

func (u *uninstallWindow) run() error {
	userPresent := userInstallPresent()
	machinePresent := machineInstallPresent()
	defaultMachine := u.opts.machine
	if !u.opts.user && !u.opts.machine {
		defaultMachine = !userPresent && machinePresent
	}
	defaultUser := !defaultMachine
	icon := setupIcon()
	err := MainWindow{
		AssignTo:   &u.window,
		Title:      productName + " Uninstall",
		Icon:       icon,
		Background: SolidColorBrush{Color: setupBG},
		Size:       Size{Width: 610, Height: 430},
		MinSize:    Size{Width: 580, Height: 390},
		Layout:     VBox{MarginsZero: true, Spacing: 0},
		Children: []Widget{
			setupHeader("Uninstall "+productName, "Remove the agent, shortcuts, and background components"),
			ScrollView{
				Background:      SolidColorBrush{Color: setupBG},
				HorizontalFixed: true,
				Layout:          VBox{Margins: Margins{Left: 16, Top: 14, Right: 16, Bottom: 12}, Spacing: 10},
				Children: []Widget{
					u.uninstallScopePanel(defaultUser, defaultMachine),
					u.uninstallProgressPanel(),
				},
			},
			u.uninstallFooter(),
		},
	}.Create()
	if err != nil {
		return err
	}
	u.window.Closing().Attach(func(canceled *bool, _ walk.CloseReason) {
		if u.working {
			*canceled = true
			u.setStatusDirect("Uninstall is applying changes and cannot be closed yet.", setupAmber)
		}
	})
	if u.userScope != nil {
		u.userScope.SetChecked(defaultUser)
	}
	if u.machineScope != nil {
		u.machineScope.SetChecked(defaultMachine)
	}
	if u.opts.autoUninstall {
		go func() {
			time.Sleep(250 * time.Millisecond)
			u.uninstall()
		}()
	}
	u.window.Run()
	return u.resultErr
}

func (u *uninstallWindow) uninstallScopePanel(defaultUser, defaultMachine bool) Widget {
	scopeNote := "Choose the installed copy to remove. All-users removal requires administrator approval."
	if !machineInstallPresent() {
		scopeNote = "Choose the installed copy to remove. No all-users install was detected in Program Files."
	} else if userInstallPresent() {
		scopeNote = "Choose the installed copy to remove. A current-user install and an all-users install were detected."
	}
	return GroupBox{
		Title:      "Remove Installation",
		Background: SolidColorBrush{Color: setupPanel},
		Layout:     VBox{Alignment: AlignHNearVNear, Margins: Margins{Left: 12, Top: 8, Right: 12, Bottom: 10}, Spacing: 7},
		Children: []Widget{
			TextLabel{Text: scopeNote, MinSize: Size{Width: 530, Height: 34}, TextColor: setupMuted, Font: Font{Family: "Segoe UI", PointSize: 9}},
			RadioButton{AssignTo: &u.userScope, Text: "Current user installation", OnClicked: func() {
				u.userScope.SetChecked(true)
				u.machineScope.SetChecked(false)
			}},
			RadioButton{AssignTo: &u.machineScope, Text: "All-users installation in Program Files", OnClicked: func() {
				u.userScope.SetChecked(false)
				u.machineScope.SetChecked(true)
			}},
			CheckBox{AssignTo: &u.purge, Text: "Reset enrollment and remove credentials, buffered telemetry, settings, and logs", Checked: u.opts.purge},
			TextLabel{Text: "Leave this clear to retain the endpoint identity for a safe reinstall. Select it only when permanently decommissioning this agent; the server-side registration may also need removal in Agent Fleet.", MinSize: Size{Width: 530, Height: 38}, TextColor: setupMuted, Font: Font{Family: "Segoe UI", PointSize: 9}},
			TextLabel{Text: "Before removing runtime files, Setup restores every IIS pool and Windows service instrumented by ZenPlus and restarts only targets that were running.", MinSize: Size{Width: 530, Height: 34}, TextColor: setupMuted, Font: Font{Family: "Segoe UI", PointSize: 9}},
		},
	}
}

func (u *uninstallWindow) uninstallProgressPanel() Widget {
	return Composite{
		Background: SolidColorBrush{Color: setupPanel},
		Layout:     VBox{Margins: Margins{Left: 12, Top: 10, Right: 12, Bottom: 10}, Spacing: 7},
		Children: []Widget{
			Label{AssignTo: &u.status, Text: "Ready to uninstall", TextColor: setupMuted, Font: Font{Family: "Segoe UI", PointSize: 9, Bold: true}},
			ProgressBar{AssignTo: &u.progress, MinValue: 0, MaxValue: 100, Value: 0, MinSize: Size{Height: 18}},
			TextEdit{
				AssignTo:   &u.logs,
				Text:       timestamped("Uninstall is ready."),
				ReadOnly:   true,
				VScroll:    true,
				Background: SolidColorBrush{Color: setupField},
				TextColor:  setupMuted,
				Font:       Font{Family: "Consolas", PointSize: 8},
				MinSize:    Size{Height: 90},
			},
		},
	}
}

func (u *uninstallWindow) uninstallFooter() Widget {
	return Composite{
		Background: SolidColorBrush{Color: setupPanel},
		MinSize:    Size{Height: 58},
		MaxSize:    Size{Height: 58},
		Layout:     HBox{Margins: Margins{Left: 16, Top: 10, Right: 16, Bottom: 10}, Spacing: 8},
		Children: []Widget{
			TextLabel{Text: publisherName, TextColor: setupMuted, MinSize: Size{Width: 210, Height: 25}, Font: Font{Family: "Segoe UI", PointSize: 9}},
			HSpacer{},
			PushButton{AssignTo: &u.cancelButton, Text: "Cancel", MinSize: Size{Width: 88, Height: 30}, OnClicked: func() { _ = u.window.Close() }},
			PushButton{AssignTo: &u.uninstallButton, Text: "Uninstall", MinSize: Size{Width: 104, Height: 30}, OnClicked: func() {
				if u.finished {
					_ = u.window.Close()
					return
				}
				u.uninstall()
			}},
		},
	}
}

func (u *uninstallWindow) uninstall() {
	opts := u.optionsFromUI()
	u.setWorking(true)
	go func() {
		u.step("Preparing uninstall", 15)
		layout, err := newLayout(opts, u.elevated)
		if err != nil {
			u.fail(err)
			return
		}
		u.appendLog("Install location: " + layout.InstallDir)
		if layout.Scope == "machine" && !u.elevated {
			u.step("Opening administrator approval", 35)
			args := commandArgsFromOptions(opts, true, true)
			if err := relaunchElevated(args, windows.Handle(u.window.Handle())); err != nil {
				u.fail(err)
				return
			}
			u.complete("ZenPlus Agent was uninstalled successfully.", true)
			return
		}
		runOpts := opts
		runOpts.quiet = true
		if !runOpts.fromTemp && selfInside(layout.InstallDir) {
			u.step("Moving uninstaller to a temporary location", 45)
			if err := relaunchUninstallUIFromTemp(layout, opts); err != nil {
				u.fail(err)
				return
			}
			u.withUI(func() { _ = u.window.Close() })
			return
		}
		u.step("Stopping background components and removing files", 55)
		if err := uninstall(layout, runOpts); err != nil {
			u.fail(err)
			return
		}
		u.complete("ZenPlus Agent was uninstalled successfully.", true)
	}()
}

func (u *uninstallWindow) optionsFromUI() options {
	opts := u.opts
	opts.quiet = true
	opts.uninstall = true
	opts.user = u.userScope != nil && u.userScope.Checked()
	opts.machine = !opts.user
	opts.purge = u.purge != nil && u.purge.Checked()
	return opts
}

func (u *uninstallWindow) setWorking(working bool) {
	u.withUI(func() {
		u.working = working
		for _, w := range []interface{ SetEnabled(bool) }{u.userScope, u.machineScope, u.purge} {
			if w != nil {
				w.SetEnabled(!working)
			}
		}
		if u.cancelButton != nil {
			u.cancelButton.SetEnabled(!working)
		}
		if u.uninstallButton != nil {
			u.uninstallButton.SetEnabled(false)
		}
	})
}

func (u *uninstallWindow) step(message string, progress int) {
	u.withUI(func() {
		u.setStatusDirect(message, setupBlue)
		if u.progress != nil {
			u.progress.SetValue(progress)
		}
		u.appendLogDirect(message)
	})
}

func (u *uninstallWindow) appendLog(message string) {
	u.withUI(func() { u.appendLogDirect(message) })
}

func (u *uninstallWindow) fail(err error) {
	u.withUI(func() {
		u.working = false
		u.resultErr = err
		errorShownInWindow = true
		u.setStatusDirect("Uninstall failed: "+err.Error(), setupRed)
		if u.progress != nil {
			u.progress.SetValue(100)
		}
		u.appendLogDirect("Error: " + err.Error())
		if u.cancelButton != nil {
			_ = u.cancelButton.SetText("Close")
			u.cancelButton.SetEnabled(true)
		}
	})
}

func (u *uninstallWindow) complete(message string, removed bool) {
	u.withUI(func() {
		u.working = false
		u.setStatusDirect(message, setupGreen)
		if u.progress != nil {
			u.progress.SetValue(100)
		}
		if removed {
			u.appendLogDirect("Uninstall completed successfully.")
		} else {
			u.appendLogDirect(message)
		}
		if u.uninstallButton != nil {
			_ = u.uninstallButton.SetText("Finish")
			u.uninstallButton.SetEnabled(true)
			u.finished = true
		}
		if u.cancelButton != nil {
			u.cancelButton.SetVisible(false)
		}
	})
}

func (u *uninstallWindow) setStatusDirect(message string, color walk.Color) {
	if u.status != nil {
		_ = u.status.SetText(message)
		u.status.SetTextColor(color)
	}
}

func (u *uninstallWindow) appendLogDirect(message string) {
	if u.logs == nil {
		return
	}
	u.logs.AppendText("\r\n" + timestamped(message))
}

func (u *uninstallWindow) withUI(fn func()) {
	if u.window == nil || u.window.IsDisposed() {
		return
	}
	u.window.Synchronize(fn)
}

// setupBanner matches the navy of the ZenPlus mark.
var setupBanner = walk.RGB(11, 31, 51)

func setupHeader(title, subtitle string) Widget {
	return Composite{
		Background: SolidColorBrush{Color: setupBanner},
		MinSize:    Size{Height: 78},
		MaxSize:    Size{Height: 78},
		Layout:     HBox{Margins: Margins{Left: 18, Top: 13, Right: 18, Bottom: 12}, Spacing: 12},
		Children: []Widget{
			ImageView{
				Image:      setupBrandBitmap(48),
				Mode:       ImageViewModeIdeal,
				MinSize:    Size{Width: 50, Height: 50},
				MaxSize:    Size{Width: 50, Height: 50},
				Background: SolidColorBrush{Color: setupBanner},
			},
			Composite{
				Background: SolidColorBrush{Color: setupBanner},
				Layout:     VBox{Margins: Margins{Left: 0, Top: 4, Right: 0, Bottom: 0}, Spacing: 3},
				Children: []Widget{
					Label{Text: title, TextColor: walk.RGB(255, 255, 255), Font: Font{Family: "Segoe UI", PointSize: 16, Bold: true}},
					TextLabel{Text: subtitle, TextColor: walk.RGB(191, 219, 254), Font: Font{Family: "Segoe UI", PointSize: 9}, MinSize: Size{Width: 430, Height: 22}},
				},
			},
		},
	}
}

func setupBrandBitmap(size int) walk.Image {
	bitmap, err := walk.NewBitmapFromImageForDPI(brand.Mark(size), 96)
	if err != nil {
		return nil
	}
	return bitmap
}

func setupIcon() *walk.Icon {
	if icon, err := walk.NewIconFromResourceId(3); err == nil {
		return icon
	}
	icon, _ := walk.NewIconFromImageForDPI(brand.Mark(64), 96)
	return icon
}

func commandArgsFromOptions(opts options, uninstall bool, quiet bool) []string {
	args := make([]string, 0, 12)
	if uninstall {
		args = append(args, "/uninstall")
	}
	if quiet {
		args = append(args, "/quiet")
	}
	if opts.machine {
		args = append(args, "/machine")
	} else if opts.user {
		args = append(args, "/user")
	}
	if opts.purge {
		args = append(args, "/purge")
	}
	if opts.noStartMenu {
		args = append(args, "/no-start-menu")
	}
	if opts.noRestart {
		args = append(args, "/norestart")
	}
	if opts.managedByMSI {
		args = append(args, "/managed-by-msi")
	}
	if opts.fromTemp {
		args = append(args, "/from-temp")
	}
	if opts.autoUninstall {
		args = append(args, "/auto-uninstall")
	}
	if opts.controllerURL != "" {
		args = append(args, "CONTROLLER_URL="+opts.controllerURL)
	}
	if opts.controllerCA != "" {
		args = append(args, "CONTROLLER_CA_FILE="+opts.controllerCA)
	}
	if opts.apmMode == "enabled" {
		args = append(args, "APM_ENABLED=1")
	} else if opts.apmMode == "disabled" {
		args = append(args, "APM_ENABLED=0")
	}
	if opts.profile != "" {
		args = append(args, "INSTALL_PROFILE="+opts.profile)
	}
	if opts.logPath != "" {
		args = append(args, "LOG="+opts.logPath)
	}
	return args
}

func relaunchUninstallUIFromTemp(l layout, opts options) error {
	tempExe, err := copySelfToTemp()
	if err != nil {
		return err
	}
	opts.quiet = false
	opts.fromTemp = true
	opts.autoUninstall = true
	args := commandArgsFromOptions(opts, true, false)
	cmd := exec.Command(tempExe, args...)
	return cmd.Start()
}

func relaunchUninstallFromTempQuiet(l layout, opts options) error {
	tempExe, err := copySelfToTemp()
	if err != nil {
		return err
	}
	opts.quiet = true
	opts.fromTemp = true
	args := commandArgsFromOptions(opts, true, true)
	cmd := exec.Command(tempExe, args...)
	cmd.SysProcAttr = hiddenSysProcAttr()
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		detail := strings.TrimSpace(stderr.String())
		if detail != "" {
			return fmt.Errorf("%w: %s", err, detail)
		}
		return err
	}
	return nil
}

func copySelfToTemp() (string, error) {
	self, err := os.Executable()
	if err != nil {
		return "", err
	}
	tempDir := filepath.Join(os.TempDir(), fmt.Sprintf("ZenPlusAgentSetup-%d", time.Now().UnixNano()))
	if err := os.MkdirAll(tempDir, 0o755); err != nil {
		return "", err
	}
	tempExe := filepath.Join(tempDir, "ZenPlusAgentSetup.exe")
	data, err := os.ReadFile(self)
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(tempExe, data, 0o755); err != nil {
		return "", err
	}
	return tempExe, nil
}

func userInstallPresent() bool {
	localAppData := os.Getenv("LOCALAPPDATA")
	if localAppData == "" {
		return false
	}
	return installPathPresent(filepath.Join(localAppData, "Programs", "ZenPlus", "Agent"))
}

func machineInstallPresent() bool {
	programFiles := os.Getenv("ProgramFiles")
	if programFiles == "" {
		programFiles = `C:\Program Files`
	}
	return installPathPresent(filepath.Join(programFiles, "ZenPlus", "Agent"))
}

func installPathPresent(path string) bool {
	if _, err := os.Stat(filepath.Join(path, "zenplus-agent-app.exe")); err == nil {
		return true
	}
	if _, err := os.Stat(filepath.Join(path, "ZenPlusAgentSetup.exe")); err == nil {
		return true
	}
	return false
}

func selfInside(path string) bool {
	self, err := os.Executable()
	if err != nil {
		return false
	}
	return pathWithin(path, self)
}

func textOf(edit *walk.LineEdit) string {
	if edit == nil {
		return ""
	}
	return edit.Text()
}

func timestamped(message string) string {
	return "[" + time.Now().Format("15:04:05") + "] " + message
}
