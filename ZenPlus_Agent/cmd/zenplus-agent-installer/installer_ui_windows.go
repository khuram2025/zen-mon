//go:build windows

package main

import (
	"bytes"
	"fmt"
	"image"
	"image/color"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/lxn/walk"
	. "github.com/lxn/walk/declarative"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
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

const installPolicyText = `ZenPlus Agent runs in the background to collect configured endpoint inventory, diagnostics, and activity logs for your organization.

Install ZenPlus Agent only on authorized devices. By continuing, you confirm that you are permitted to install the agent, allow it to start with Windows, and allow it to connect to the configured controller endpoint.`

type setupWindow struct {
	opts     options
	elevated bool

	window                *walk.MainWindow
	userScope             *walk.RadioButton
	machineScope          *walk.RadioButton
	controllerURL         *walk.LineEdit
	profileInfrastructure *walk.RadioButton
	profileAPM            *walk.RadioButton
	profileCombined       *walk.RadioButton
	noStartMenu           *walk.CheckBox
	launchAfter           *walk.CheckBox
	accept                *walk.CheckBox
	progress              *walk.ProgressBar
	status                *walk.Label
	logs                  *walk.TextEdit
	installButton         *walk.PushButton
	cancelButton          *walk.PushButton
	finished              bool
	working               bool
	resultErr             error
}

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

func runSetupUI(opts options, elevated bool) error {
	ui := &setupWindow{opts: opts, elevated: elevated}
	return ui.run()
}

func runUninstallUI(opts options, elevated bool) error {
	ui := &uninstallWindow{opts: opts, elevated: elevated}
	return ui.run()
}

func (u *setupWindow) run() error {
	u.hydrateExistingInstall()
	defaultMachine := u.opts.machine || (u.elevated && !u.opts.user)
	defaultUser := !defaultMachine
	icon := setupIcon()
	err := MainWindow{
		AssignTo:   &u.window,
		Title:      productName + " Setup",
		Icon:       icon,
		Background: SolidColorBrush{Color: setupBG},
		Size:       Size{Width: 670, Height: 730},
		MinSize:    Size{Width: 640, Height: 680},
		Layout:     VBox{MarginsZero: true, Spacing: 0},
		Children: []Widget{
			setupHeader("Install "+productName, "Version "+model.AgentVersion+" | Guided Windows setup"),
			ScrollView{
				Background:      SolidColorBrush{Color: setupBG},
				HorizontalFixed: true,
				Layout:          VBox{Margins: Margins{Left: 16, Top: 14, Right: 16, Bottom: 12}, Spacing: 10},
				Children: []Widget{
					u.scopePanel(defaultUser, defaultMachine),
					u.settingsPanel(),
					u.policyPanel(),
					u.progressPanel("Ready to install"),
				},
			},
			u.footer(),
		},
	}.Create()
	if err != nil {
		return err
	}
	u.window.Closing().Attach(func(canceled *bool, _ walk.CloseReason) {
		if u.working {
			*canceled = true
			u.setStatusDirect("Setup is applying changes and cannot be closed yet.", setupAmber)
		}
	})
	if u.userScope != nil {
		u.userScope.SetChecked(defaultUser)
	}
	if u.machineScope != nil {
		u.machineScope.SetChecked(defaultMachine)
	}
	if u.profileCombined != nil {
		u.profileCombined.SetChecked(u.opts.profile == "" || u.opts.profile == "combined")
	}
	if u.profileInfrastructure != nil {
		u.profileInfrastructure.SetChecked(u.opts.profile == "infrastructure")
	}
	if u.profileAPM != nil {
		u.profileAPM.SetChecked(u.opts.profile == "apm")
	}
	u.updateInstallEnabled()
	u.window.Run()
	return u.resultErr
}

func (u *setupWindow) hydrateExistingInstall() {
	if !u.opts.machine && !u.opts.user {
		switch {
		case machineInstallPresent():
			u.opts.machine = true
		case userInstallPresent():
			u.opts.user = true
		default:
			// Server monitoring and managed application instrumentation require
			// the Windows service, so make the professional machine install the
			// fresh-install default.
			u.opts.machine = true
		}
	}
	l, err := newLayout(u.opts, u.elevated)
	if err != nil {
		return
	}
	if _, err := os.Stat(l.ConfigPath); err != nil {
		return
	}
	cfg, err := config.LoadForEdit(l.ConfigPath)
	if err != nil {
		return
	}
	if strings.TrimSpace(u.opts.controllerURL) == "" {
		u.opts.controllerURL = cfg.ControllerURL
	}
	if strings.TrimSpace(u.opts.profile) == "" {
		u.opts.profile = cfg.APM.Profile
	}
}

func (u *setupWindow) scopePanel(defaultUser, defaultMachine bool) Widget {
	scopeNote := "The all-users Windows service is recommended for reliable server and managed APM monitoring. Current-user mode is intended for evaluation."
	if !u.elevated {
		scopeNote = "The all-users Windows service is recommended for server monitoring and requires administrator approval. Current-user mode is intended for evaluation only."
	}
	return GroupBox{
		Title:      "Installation Scope",
		Background: SolidColorBrush{Color: setupPanel},
		Layout:     VBox{Margins: Margins{Left: 12, Top: 8, Right: 12, Bottom: 10}, Spacing: 5},
		Children: []Widget{
			TextLabel{Text: scopeNote, MinSize: Size{Width: 560, Height: 34}, TextColor: setupMuted, Font: Font{Family: "Segoe UI", PointSize: 9}},
			RadioButton{AssignTo: &u.userScope, Text: "Current user - evaluation only (no Windows service)", OnClicked: func() {
				u.userScope.SetChecked(true)
				u.machineScope.SetChecked(false)
			}},
			RadioButton{AssignTo: &u.machineScope, Text: "All users - Windows service (recommended for servers)", OnClicked: func() {
				u.userScope.SetChecked(false)
				u.machineScope.SetChecked(true)
			}},
			CheckBox{AssignTo: &u.noStartMenu, Text: "Create Start Menu shortcut for ZenPlus Agent Dashboard", Checked: true},
			CheckBox{AssignTo: &u.launchAfter, Text: "Launch ZenPlus Agent after setup", Checked: true},
		},
	}
}

func (u *setupWindow) settingsPanel() Widget {
	controllerValue := strings.TrimSpace(u.opts.controllerURL)
	if controllerValue == "" {
		controllerValue = config.Default().ControllerURL
	}
	profile := u.opts.profile
	if profile == "" {
		if u.opts.apmMode == "disabled" {
			profile = "infrastructure"
		} else {
			profile = "combined"
		}
	}
	u.opts.profile = profile
	return GroupBox{
		Title:      "Connect to ZenPlus",
		Background: SolidColorBrush{Color: setupPanel},
		Layout:     VBox{Margins: Margins{Left: 12, Top: 10, Right: 12, Bottom: 10}, Spacing: 8},
		Children: []Widget{
			Label{Text: "Controller URL or IP address", TextColor: setupText, Font: Font{Family: "Segoe UI", PointSize: 9, Bold: true}},
			LineEdit{AssignTo: &u.controllerURL, CueBanner: "https://192.168.8.221", Text: controllerValue, Background: SolidColorBrush{Color: setupField}, TextColor: setupText, MinSize: Size{Height: 30}},
			Label{Text: "Monitoring profile", TextColor: setupText, Font: Font{Family: "Segoe UI", PointSize: 9, Bold: true}},
			RadioButton{AssignTo: &u.profileCombined, Text: "Server monitoring + APM (recommended)"},
			RadioButton{AssignTo: &u.profileInfrastructure, Text: "Server monitoring only"},
			RadioButton{AssignTo: &u.profileAPM, Text: "APM only (keeps agent health and inventory)"},
			TextLabel{Text: "Every package includes the managed OpenTelemetry gateway plus offline .NET, Java, Node.js, and Python assets. ZenPlus can manage IIS/.NET, Java, and Node Windows-service instrumentation; Python and other applications can send OTLP to 127.0.0.1:4317/4318.", MinSize: Size{Width: 560, Height: 48}, TextColor: setupMuted, Font: Font{Family: "Segoe UI", PointSize: 9}},
			TextLabel{Text: "During an upgrade or removal, Setup briefly stops only application pools and Windows services already managed by ZenPlus, then restores their prior running state. Windows itself is never restarted.", MinSize: Size{Width: 560, Height: 34}, TextColor: setupMuted, Font: Font{Family: "Segoe UI", PointSize: 9}},
			TextLabel{Text: "After installation, this computer registers with the appliance as Pending authorization. An administrator approves it in Agent Fleet; the appliance then issues and manages its protected credential and policy.", MinSize: Size{Width: 560, Height: 46}, TextColor: setupMuted, Font: Font{Family: "Segoe UI", PointSize: 9}},
		},
	}
}

func (u *setupWindow) policyPanel() Widget {
	return GroupBox{
		Title:      "Acceptance Policy",
		Background: SolidColorBrush{Color: setupPanel},
		Layout:     VBox{Margins: Margins{Left: 12, Top: 8, Right: 12, Bottom: 10}, Spacing: 7},
		Children: []Widget{
			TextEdit{
				Text:       strings.ReplaceAll(installPolicyText, "\n", "\r\n"),
				ReadOnly:   true,
				VScroll:    true,
				Background: SolidColorBrush{Color: setupField},
				TextColor:  setupText,
				Font:       Font{Family: "Segoe UI", PointSize: 9},
				MinSize:    Size{Height: 78},
			},
			CheckBox{AssignTo: &u.accept, Text: "I accept the ZenPlus Agent installation and data collection policy.", OnCheckedChanged: u.updateInstallEnabled},
		},
	}
}

func (u *setupWindow) progressPanel(initial string) Widget {
	return Composite{
		Background: SolidColorBrush{Color: setupPanel},
		Layout:     VBox{Margins: Margins{Left: 12, Top: 10, Right: 12, Bottom: 10}, Spacing: 7},
		Children: []Widget{
			Label{AssignTo: &u.status, Text: initial, TextColor: setupMuted, Font: Font{Family: "Segoe UI", PointSize: 9, Bold: true}},
			ProgressBar{AssignTo: &u.progress, MinValue: 0, MaxValue: 100, Value: 0, MinSize: Size{Height: 18}},
			TextEdit{
				AssignTo:   &u.logs,
				Text:       timestamped("Setup is ready."),
				ReadOnly:   true,
				VScroll:    true,
				Background: SolidColorBrush{Color: setupField},
				TextColor:  setupMuted,
				Font:       Font{Family: "Consolas", PointSize: 8},
				MinSize:    Size{Height: 70},
			},
		},
	}
}

func (u *setupWindow) footer() Widget {
	return Composite{
		Background: SolidColorBrush{Color: setupPanel},
		MinSize:    Size{Height: 58},
		MaxSize:    Size{Height: 58},
		Layout:     HBox{Margins: Margins{Left: 16, Top: 10, Right: 16, Bottom: 10}, Spacing: 8},
		Children: []Widget{
			TextLabel{Text: publisherName, TextColor: setupMuted, MinSize: Size{Width: 210, Height: 25}, Font: Font{Family: "Segoe UI", PointSize: 9}},
			HSpacer{},
			PushButton{AssignTo: &u.cancelButton, Text: "Cancel", MinSize: Size{Width: 88, Height: 30}, OnClicked: func() { _ = u.window.Close() }},
			PushButton{AssignTo: &u.installButton, Text: "Install", MinSize: Size{Width: 104, Height: 30}, OnClicked: func() {
				if u.finished {
					_ = u.window.Close()
					return
				}
				u.install()
			}},
		},
	}
}

func (u *setupWindow) install() {
	if u.accept == nil || !u.accept.Checked() {
		u.setStatus("Accept the policy to continue.", setupAmber)
		return
	}
	opts := u.optionsFromUI()
	normalizedController, err := config.NormalizeControllerURL(opts.controllerURL)
	if err != nil {
		u.setStatus("Enter a valid HTTPS controller URL before continuing.", setupRed)
		u.appendLog("Controller validation failed: " + err.Error())
		return
	}
	opts.controllerURL = normalizedController
	launchAfter := u.launchAfter != nil && u.launchAfter.Checked()
	u.setWorking(true)
	go func() {
		u.step("Preparing installation layout", 10)
		layout, err := newLayout(opts, u.elevated)
		if err != nil {
			u.fail(err)
			return
		}
		u.appendLog("Install location: " + layout.InstallDir)
		if layout.Scope == "machine" && !u.elevated {
			u.step("Opening administrator approval", 30)
			args := commandArgsFromOptions(opts, false, true)
			if err := relaunchElevated(args); err != nil {
				u.fail(err)
				return
			}
			if launchAfter {
				if err := launchDashboard(layout); err != nil {
					u.appendLog("Dashboard launch failed: " + err.Error())
				}
			}
			u.complete("ZenPlus Agent was installed successfully for all users.", true)
			return
		}
		u.step("Installing files and configuration", 45)
		runOpts := opts
		runOpts.quiet = true
		if err := install(layout, runOpts); err != nil {
			u.fail(err)
			return
		}
		u.step("Finalizing shortcuts and startup registration", 82)
		if launchAfter {
			if err := launchDashboard(layout); err != nil {
				u.appendLog("Dashboard launch failed: " + err.Error())
			} else {
				u.appendLog("ZenPlus Agent dashboard launched.")
			}
		}
		u.complete("ZenPlus Agent is installed. Its authorization request will appear in Agent Fleet.", true)
	}()
}

func (u *setupWindow) optionsFromUI() options {
	opts := u.opts
	opts.quiet = true
	opts.user = u.userScope != nil && u.userScope.Checked()
	opts.machine = !opts.user
	opts.noStartMenu = u.noStartMenu != nil && !u.noStartMenu.Checked()
	opts.controllerURL = strings.TrimSpace(textOf(u.controllerURL))
	switch {
	case u.profileInfrastructure != nil && u.profileInfrastructure.Checked():
		opts.profile, opts.apmMode = "infrastructure", "disabled"
	case u.profileAPM != nil && u.profileAPM.Checked():
		opts.profile, opts.apmMode = "apm", "enabled"
	default:
		opts.profile, opts.apmMode = "combined", "enabled"
	}
	return opts
}

func (u *setupWindow) updateInstallEnabled() {
	if u.installButton != nil {
		u.installButton.SetEnabled(u.accept != nil && u.accept.Checked())
	}
}

func (u *setupWindow) setWorking(working bool) {
	u.withUI(func() {
		u.working = working
		for _, w := range []interface{ SetEnabled(bool) }{
			u.userScope, u.machineScope, u.controllerURL, u.profileInfrastructure, u.profileAPM, u.profileCombined, u.noStartMenu, u.launchAfter, u.accept,
		} {
			if w != nil {
				w.SetEnabled(!working)
			}
		}
		if u.cancelButton != nil {
			u.cancelButton.SetEnabled(!working)
		}
		if u.installButton != nil {
			u.installButton.SetEnabled(false)
		}
	})
}

func (u *setupWindow) step(message string, progress int) {
	u.withUI(func() {
		u.setStatusDirect(message, setupBlue)
		if u.progress != nil {
			u.progress.SetValue(progress)
		}
		u.appendLogDirect(message)
	})
}

func (u *setupWindow) appendLog(message string) {
	u.withUI(func() {
		u.appendLogDirect(message)
	})
}

func (u *setupWindow) fail(err error) {
	u.withUI(func() {
		u.working = false
		u.resultErr = err
		u.setStatusDirect("Setup failed: "+err.Error(), setupRed)
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

func (u *setupWindow) complete(message string, installed bool) {
	u.withUI(func() {
		u.working = false
		u.setStatusDirect(message, setupGreen)
		if u.progress != nil {
			u.progress.SetValue(100)
		}
		if installed {
			u.appendLogDirect("Installation completed successfully.")
		} else {
			u.appendLogDirect(message)
		}
		if u.installButton != nil {
			_ = u.installButton.SetText("Finish")
			u.installButton.SetEnabled(true)
			u.finished = true
		}
		if u.cancelButton != nil {
			u.cancelButton.SetVisible(false)
		}
	})
}

func (u *setupWindow) setStatus(message string, color walk.Color) {
	u.withUI(func() { u.setStatusDirect(message, color) })
}

func (u *setupWindow) setStatusDirect(message string, color walk.Color) {
	if u.status != nil {
		_ = u.status.SetText(message)
		u.status.SetTextColor(color)
	}
}

func (u *setupWindow) appendLogDirect(message string) {
	if u.logs == nil {
		return
	}
	u.logs.AppendText("\r\n" + timestamped(message))
}

func (u *setupWindow) withUI(fn func()) {
	if u.window == nil || u.window.IsDisposed() {
		return
	}
	u.window.Synchronize(fn)
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
		Layout:     VBox{Margins: Margins{Left: 12, Top: 8, Right: 12, Bottom: 10}, Spacing: 7},
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
			if err := relaunchElevated(args); err != nil {
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

func setupHeader(title, subtitle string) Widget {
	return Composite{
		Background: SolidColorBrush{Color: setupAccent},
		MinSize:    Size{Height: 78},
		MaxSize:    Size{Height: 78},
		Layout:     HBox{Margins: Margins{Left: 18, Top: 13, Right: 18, Bottom: 12}, Spacing: 12},
		Children: []Widget{
			Label{
				Text:          "Z",
				MinSize:       Size{Width: 48, Height: 48},
				MaxSize:       Size{Width: 48, Height: 48},
				Background:    SolidColorBrush{Color: setupOverlay},
				TextColor:     setupAccent,
				Font:          Font{Family: "Segoe UI", PointSize: 22, Bold: true},
				TextAlignment: AlignCenter,
			},
			Composite{
				Background: SolidColorBrush{Color: setupAccent},
				Layout:     VBox{Margins: Margins{Left: 0, Top: 4, Right: 0, Bottom: 0}, Spacing: 3},
				Children: []Widget{
					Label{Text: title, TextColor: walk.RGB(255, 255, 255), Font: Font{Family: "Segoe UI", PointSize: 16, Bold: true}},
					TextLabel{Text: subtitle, TextColor: walk.RGB(226, 255, 252), Font: Font{Family: "Segoe UI", PointSize: 9}, MinSize: Size{Width: 430, Height: 22}},
				},
			},
		},
	}
}

func setupIcon() *walk.Icon {
	if icon, err := walk.NewIconFromResourceId(3); err == nil {
		return icon
	}
	icon, _ := walk.NewIconFromImage(makeSetupIcon(64))
	return icon
}

func makeSetupIcon(size int) image.Image {
	img := image.NewRGBA(image.Rect(0, 0, size, size))
	for y := 0; y < size; y++ {
		for x := 0; x < size; x++ {
			img.Set(x, y, color.RGBA{R: 0, G: 137, B: 129, A: 255})
		}
	}
	white := color.RGBA{R: 255, G: 255, B: 255, A: 255}
	thick := maxInt(2, size/7)
	left := size / 4
	right := size - left
	top := size / 4
	bottom := size - top - thick
	fillSetupRect(img, left, top, right, top+thick, white)
	fillSetupRect(img, left, bottom, right, bottom+thick, white)
	for i := 0; i < right-left; i++ {
		x := right - i - thick
		y := top + i*(bottom-top)/(right-left)
		fillSetupRect(img, x, y, x+thick, y+thick, white)
	}
	return img
}

func fillSetupRect(img *image.RGBA, left, top, right, bottom int, c color.RGBA) {
	bounds := img.Bounds()
	if left < bounds.Min.X {
		left = bounds.Min.X
	}
	if top < bounds.Min.Y {
		top = bounds.Min.Y
	}
	if right > bounds.Max.X {
		right = bounds.Max.X
	}
	if bottom > bounds.Max.Y {
		bottom = bounds.Max.Y
	}
	for y := top; y < bottom; y++ {
		for x := left; x < right; x++ {
			img.Set(x, y, c)
		}
	}
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
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
	if opts.apmMode == "enabled" {
		args = append(args, "APM_ENABLED=1")
	} else if opts.apmMode == "disabled" {
		args = append(args, "APM_ENABLED=0")
	}
	if opts.profile != "" {
		args = append(args, "INSTALL_PROFILE="+opts.profile)
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
