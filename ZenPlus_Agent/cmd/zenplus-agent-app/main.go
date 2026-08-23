package main

import (
	"context"
	"flag"
	"fmt"
	"image"
	"image/color"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync/atomic"
	"time"

	"github.com/lxn/walk"
	. "github.com/lxn/walk/declarative"
	"github.com/lxn/win"
	"golang.org/x/sys/windows"

	"zenplus-agent/internal/agent"
	"zenplus-agent/internal/appstate"
	"zenplus-agent/internal/client"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/runtime"
	"zenplus-agent/internal/selfupdate"
)

var (
	bg       = walk.RGB(246, 248, 251)
	surface  = walk.RGB(255, 255, 255)
	surface2 = walk.RGB(241, 245, 249)
	fieldBg  = walk.RGB(252, 253, 255)
	text     = walk.RGB(15, 23, 42)
	muted    = walk.RGB(82, 94, 113)
	faint    = walk.RGB(129, 140, 160)
	blue     = walk.RGB(12, 106, 228)
	teal     = walk.RGB(0, 137, 129)
	green    = walk.RGB(49, 181, 64)
	amber    = walk.RGB(204, 135, 25)
	red      = walk.RGB(210, 62, 62)
)

type appUI struct {
	configPath      string
	closing         bool
	refreshInFlight atomic.Bool
	updateInFlight  atomic.Bool
	notifiedVersion string

	window *walk.MainWindow
	tray   *walk.NotifyIcon

	statusBadge    *walk.Label
	topStatus      *walk.Label
	topConnection  *walk.Label
	hostName       *walk.Label
	agentID        *walk.Label
	policyID       *walk.Label
	serviceStatus  *walk.Label
	controllerURL  *walk.Label
	queueDepth     *walk.Label
	lastCollection *walk.Label
	apmService     *walk.Label
	apmActivity    *walk.Label
	apmEndpoint    *walk.Label
	apmQueue       *walk.Label
	versionStatus  *walk.Label
	updateStatus   *walk.Label
	actionStatus   *walk.Label
	collectButton  *walk.PushButton
	updateButton   *walk.PushButton
	settingsButton *walk.PushButton
	logsButton     *walk.PushButton
}

func main() {
	if err := run(); err != nil {
		_, _ = fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	fs := flag.NewFlagSet("zenplus-agent-app", flag.ExitOnError)
	configPath := fs.String("config", appstate.DefaultConfigPath(), "agent config path")
	startHidden := fs.Bool("start-hidden", false, "start minimized to tray")
	smoke := fs.Bool("smoke", false, "load state and exit")
	if err := fs.Parse(os.Args[1:]); err != nil {
		return err
	}
	if *smoke {
		if appstate.Load(context.Background(), *configPath).Config.ControllerURL == "" {
			return fmt.Errorf("agent config is not readable")
		}
		return nil
	}
	mutex, duplicate, err := acquireSingleInstance()
	if err != nil {
		return err
	}
	if duplicate {
		if mutex != 0 {
			windows.CloseHandle(mutex)
		}
		if !*startHidden {
			showExistingWindow()
		}
		return nil
	}
	defer windows.CloseHandle(mutex)

	ui := &appUI{configPath: *configPath}
	if err := ui.create(*startHidden); err != nil {
		return err
	}
	ui.refresh()
	go ui.refreshLoop()
	go ui.updateLoop()
	ui.window.Run()
	if ui.tray != nil {
		_ = ui.tray.Dispose()
	}
	return nil
}

func (a *appUI) create(startHidden bool) error {
	icon := loadAppIcon()
	err := MainWindow{
		AssignTo:   &a.window,
		Title:      "ZenPlus Agent",
		Icon:       icon,
		Background: SolidColorBrush{Color: bg},
		Size:       Size{Width: 780, Height: 465},
		MinSize:    Size{Width: 720, Height: 440},
		Layout:     VBox{Margins: Margins{Left: 12, Top: 10, Right: 12, Bottom: 12}, Spacing: 8},
		Children: []Widget{
			a.header(),
			a.statusCard(),
			a.detailsCard(),
		},
	}.Create()
	if err != nil {
		return err
	}
	a.window.Closing().Attach(func(canceled *bool, reason walk.CloseReason) {
		if !a.closing {
			*canceled = true
			a.window.Hide()
		}
	})
	if startHidden {
		a.window.Hide()
	}
	return a.setupTray(icon)
}

func loadAppIcon() *walk.Icon {
	if icon, err := walk.NewIconFromResourceId(3); err == nil {
		return icon
	}
	icon, _ := walk.NewIconFromImage(makeIcon(64))
	return icon
}

func acquireSingleInstance() (windows.Handle, bool, error) {
	handle, err := windows.CreateMutex(nil, false, windows.StringToUTF16Ptr(`Local\ZenPlusAgentDashboard`))
	if err == windows.ERROR_ALREADY_EXISTS {
		return handle, true, nil
	}
	if err != nil {
		return 0, false, fmt.Errorf("create dashboard instance lock: %w", err)
	}
	return handle, false, nil
}

func showExistingWindow() {
	hwnd := win.FindWindow(nil, windows.StringToUTF16Ptr("ZenPlus Agent"))
	if hwnd == 0 {
		return
	}
	win.ShowWindow(hwnd, win.SW_RESTORE)
	win.SetForegroundWindow(hwnd)
}

func (a *appUI) header() Widget {
	return Composite{
		Background: SolidColorBrush{Color: bg},
		MinSize:    Size{Height: 54},
		MaxSize:    Size{Height: 54},
		Layout:     HBox{Margins: Margins{Left: 2, Top: 2, Right: 2, Bottom: 2}, Spacing: 10},
		Children: []Widget{
			Label{
				Text:          "Z",
				MinSize:       Size{Width: 42, Height: 42},
				MaxSize:       Size{Width: 42, Height: 42},
				Background:    SolidColorBrush{Color: teal},
				TextColor:     walk.RGB(255, 255, 255),
				Font:          Font{Family: "Segoe UI", PointSize: 18, Bold: true},
				TextAlignment: AlignCenter,
			},
			Composite{
				Background:    SolidColorBrush{Color: bg},
				StretchFactor: 1,
				Layout:        VBox{Margins: Margins{Left: 2, Top: 3, Right: 0, Bottom: 1}, Spacing: 3},
				Children: []Widget{
					Composite{
						Background: SolidColorBrush{Color: bg},
						Layout:     HBox{MarginsZero: true, Spacing: 8},
						Children: []Widget{
							Label{Text: "ZenPlus Agent", TextColor: text, Font: Font{Family: "Segoe UI", PointSize: 14, Bold: true}},
							Label{Text: "v" + model.AgentVersion, TextColor: muted, Font: Font{Family: "Segoe UI", PointSize: 9, Bold: true}},
						},
					},
					Composite{
						Background: SolidColorBrush{Color: bg},
						Layout:     HBox{MarginsZero: true, Spacing: 8},
						Children: []Widget{
							Label{AssignTo: &a.topStatus, Text: "Checking agent", TextColor: amber, Font: Font{Family: "Segoe UI", PointSize: 10, Bold: true}},
							Label{Text: "|", TextColor: faint, Font: Font{Family: "Segoe UI", PointSize: 10}},
							Label{AssignTo: &a.topConnection, Text: "Controller pending", TextColor: muted, Font: Font{Family: "Segoe UI", PointSize: 10}},
						},
					},
				},
			},
			HSpacer{},
			Composite{
				Background: SolidColorBrush{Color: bg},
				Layout:     HBox{Margins: Margins{Left: 0, Top: 6, Right: 0, Bottom: 0}, Spacing: 5},
				Children: []Widget{
					PushButton{Text: "Refresh", MinSize: Size{Width: 68, Height: 28}, OnClicked: a.refresh},
					PushButton{AssignTo: &a.collectButton, Text: "Collect", MinSize: Size{Width: 66, Height: 28}, OnClicked: a.collectNow},
					PushButton{AssignTo: &a.updateButton, Text: "Updates", MinSize: Size{Width: 72, Height: 28}, OnClicked: func() { a.checkForUpdates(true) }},
					PushButton{AssignTo: &a.logsButton, Text: "Logs", MinSize: Size{Width: 56, Height: 28}, OnClicked: a.showLogs},
					PushButton{AssignTo: &a.settingsButton, Text: "Settings", MinSize: Size{Width: 72, Height: 28}, ToolTipText: "Settings", OnClicked: a.showSettings},
				},
			},
		},
	}
}

func (a *appUI) statusCard() Widget {
	return Composite{
		Background: SolidColorBrush{Color: surface},
		MinSize:    Size{Height: 92},
		MaxSize:    Size{Height: 96},
		Layout:     HBox{Margins: Margins{Left: 14, Top: 11, Right: 14, Bottom: 11}, Spacing: 14},
		Children: []Widget{
			Label{
				AssignTo:      &a.statusBadge,
				Text:          "OK",
				MinSize:       Size{Width: 54, Height: 54},
				MaxSize:       Size{Width: 54, Height: 54},
				Background:    SolidColorBrush{Color: surface2},
				TextColor:     green,
				Font:          Font{Family: "Segoe UI", PointSize: 14, Bold: true},
				TextAlignment: AlignCenter,
			},
			Composite{
				Background:    SolidColorBrush{Color: surface},
				StretchFactor: 1,
				Layout:        VBox{Margins: Margins{Left: 0, Top: 2, Right: 0, Bottom: 0}, Spacing: 6},
				Children: []Widget{
					Label{AssignTo: &a.hostName, Text: "Windows host", TextColor: text, Font: Font{Family: "Segoe UI", PointSize: 12, Bold: true}},
					Composite{
						Background: SolidColorBrush{Color: surface},
						Layout:     HBox{MarginsZero: true, Spacing: 8},
						Children: []Widget{
							Label{Text: "Background monitoring", TextColor: muted, Font: Font{Family: "Segoe UI", PointSize: 10, Bold: true}},
						},
					},
					Label{AssignTo: &a.actionStatus, Text: "Agent status is loading.", TextColor: muted, Font: Font{Family: "Segoe UI", PointSize: 9}},
				},
			},
		},
	}
}

func (a *appUI) detailsCard() Widget {
	return Composite{
		Background:    SolidColorBrush{Color: surface},
		StretchFactor: 1,
		Layout:        VBox{Margins: Margins{Left: 12, Top: 12, Right: 12, Bottom: 12}, Spacing: 8},
		Children: []Widget{
			Composite{
				Background: SolidColorBrush{Color: surface},
				Layout:     HBox{MarginsZero: true, Spacing: 8},
				Children: []Widget{
					a.detailTile("Agent ID", &a.agentID),
					a.detailTile("Controller", &a.controllerURL),
					a.detailTile("Version", &a.versionStatus),
					a.detailTile("Updates", &a.updateStatus),
				},
			},
			Composite{
				Background: SolidColorBrush{Color: surface},
				Layout:     HBox{MarginsZero: true, Spacing: 8},
				Children: []Widget{
					a.detailTile("Policy", &a.policyID),
					a.detailTile("Service", &a.serviceStatus),
					a.detailTile("Queue", &a.queueDepth),
					a.detailTile("Collection", &a.lastCollection),
				},
			},
			Composite{
				Background: SolidColorBrush{Color: surface},
				Layout:     HBox{MarginsZero: true, Spacing: 8},
				Children: []Widget{
					a.detailTile("Local APM", &a.apmService),
					a.detailTile("APM activity", &a.apmActivity),
					a.detailTile("Ingest endpoint", &a.apmEndpoint),
					a.detailTile("Appliance APM", &a.apmQueue),
				},
			},
		},
	}
}

func (a *appUI) detailTile(title string, value **walk.Label) Widget {
	return Composite{
		Background:    SolidColorBrush{Color: surface2},
		MinSize:       Size{Width: 150, Height: 54},
		StretchFactor: 1,
		Layout:        VBox{Margins: Margins{Left: 9, Top: 7, Right: 9, Bottom: 7}, Spacing: 3},
		Children: []Widget{
			Label{Text: title, TextColor: muted, Font: Font{Family: "Segoe UI", PointSize: 8}},
			Label{AssignTo: value, Text: "-", TextColor: text, Font: Font{Family: "Segoe UI", PointSize: 9, Bold: true}},
		},
	}
}

func (a *appUI) setupTray(icon *walk.Icon) error {
	ni, err := walk.NewNotifyIcon(a.window)
	if err != nil {
		return err
	}
	a.tray = ni
	if icon != nil {
		_ = ni.SetIcon(icon)
	}
	_ = ni.SetToolTip("ZenPlus Agent")
	openAction := walk.NewAction()
	_ = openAction.SetText("Open")
	openAction.Triggered().Attach(a.show)
	collectAction := walk.NewAction()
	_ = collectAction.SetText("Collect now")
	collectAction.Triggered().Attach(a.collectNow)
	updateAction := walk.NewAction()
	_ = updateAction.SetText("Check for updates")
	updateAction.Triggered().Attach(func() { a.checkForUpdates(true) })
	hideAction := walk.NewAction()
	_ = hideAction.SetText("Hide")
	hideAction.Triggered().Attach(func() { a.window.Hide() })
	quitAction := walk.NewAction()
	_ = quitAction.SetText("Quit")
	quitAction.Triggered().Attach(func() {
		a.closing = true
		_ = a.window.Close()
	})
	_ = ni.ContextMenu().Actions().Add(openAction)
	_ = ni.ContextMenu().Actions().Add(collectAction)
	_ = ni.ContextMenu().Actions().Add(updateAction)
	_ = ni.ContextMenu().Actions().Add(hideAction)
	_ = ni.ContextMenu().Actions().Add(walk.NewSeparatorAction())
	_ = ni.ContextMenu().Actions().Add(quitAction)
	ni.MouseDown().Attach(func(x, y int, button walk.MouseButton) {
		if button == walk.LeftButton {
			a.show()
		}
	})
	return ni.SetVisible(true)
}

func (a *appUI) show() {
	a.window.Show()
	_ = a.window.Activate()
	a.refresh()
}

func (a *appUI) refreshLoop() {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for range ticker.C {
		if a.window == nil || a.window.IsDisposed() {
			return
		}
		a.refresh()
	}
}

func (a *appUI) updateLoop() {
	timer := time.NewTimer(5 * time.Second)
	defer timer.Stop()
	for {
		select {
		case <-timer.C:
			if a.window == nil || a.window.IsDisposed() {
				return
			}
			a.checkForUpdates(false)
			timer.Reset(6 * time.Hour)
		}
	}
}

func (a *appUI) checkForUpdates(interactive bool) {
	if !a.updateInFlight.CompareAndSwap(false, true) {
		return
	}
	if a.updateButton != nil {
		a.updateButton.SetEnabled(false)
	}
	a.set(a.updateStatus, "Checking...")
	a.color(a.updateStatus, muted)
	go func() {
		defer a.updateInFlight.Store(false)
		cfg, err := config.Load(a.configPath)
		if err != nil {
			cfg = config.Default()
		}
		updateClient, clientErr := client.New(selfupdate.PublicUpdateBaseURL(), cfg.ProxyURL, true, "", "")
		var manifest selfupdate.Manifest
		if clientErr == nil {
			ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
			manifest, err = selfupdate.FetchPublicManifest(ctx, updateClient, cfg.UpdateRing)
			cancel()
		} else {
			err = clientErr
		}
		if a.window == nil || a.window.IsDisposed() {
			return
		}
		a.window.Synchronize(func() {
			if a.updateButton != nil {
				a.updateButton.SetEnabled(true)
			}
			if err != nil {
				a.set(a.updateStatus, "Unavailable")
				a.color(a.updateStatus, amber)
				if interactive {
					a.showUpdateResult(selfupdate.Manifest{}, err)
				}
				return
			}
			if selfupdate.IsNewer(manifest.LatestVersion, model.AgentVersion) {
				a.set(a.updateStatus, "v"+manifest.LatestVersion+" available")
				if strings.EqualFold(manifest.SignatureStatus, "Valid") {
					a.color(a.updateStatus, blue)
					if a.tray != nil && a.notifiedVersion != manifest.LatestVersion {
						_ = a.tray.ShowInfo("ZenPlus Agent update available", "Version "+manifest.LatestVersion+" is ready to download from Zentryc.")
						a.notifiedVersion = manifest.LatestVersion
					}
				} else {
					a.set(a.updateStatus, "v"+manifest.LatestVersion+" blocked")
					a.color(a.updateStatus, red)
				}
			} else if !strings.EqualFold(manifest.SignatureStatus, "Valid") {
				a.set(a.updateStatus, "Signing pending")
				a.color(a.updateStatus, red)
			} else {
				a.set(a.updateStatus, "Up to date")
				a.color(a.updateStatus, green)
			}
			if interactive {
				a.showUpdateResult(manifest, nil)
			}
		})
	}()
}

func (a *appUI) showUpdateResult(manifest selfupdate.Manifest, updateErr error) {
	var dlg *walk.Dialog
	var closeButton *walk.PushButton
	var downloadButton *walk.PushButton
	statusText := "The Zentryc update channel could not be reached.\r\n\r\n" + compactMiddle(updateErr.Error(), 180)
	canDownload := false
	if updateErr == nil {
		signed := strings.EqualFold(manifest.SignatureStatus, "Valid")
		signatureText := "Publisher signature: verified"
		if !signed {
			signatureText = "Publisher signature: not verified (automatic installation is blocked)"
		}
		if selfupdate.IsNewer(manifest.LatestVersion, model.AgentVersion) {
			channel := manifest.Channel
			if channel == "" {
				channel = "stable"
			}
			statusText = fmt.Sprintf("Version %s is available.\r\n\r\nCurrent version: %s\r\n%s\r\nChannel: %s", manifest.LatestVersion, model.AgentVersion, signatureText, channel)
			canDownload = signed && manifest.DownloadURL != ""
		} else {
			statusText = fmt.Sprintf("ZenPlus Agent is up to date.\r\n\r\nInstalled version: %s\r\nPublished version: %s\r\n%s", model.AgentVersion, manifest.LatestVersion, signatureText)
		}
	}
	err := Dialog{
		AssignTo:     &dlg,
		Title:        "ZenPlus Agent Updates",
		Icon:         loadAppIcon(),
		Background:   SolidColorBrush{Color: bg},
		Size:         Size{Width: 520, Height: 250},
		MinSize:      Size{Width: 500, Height: 230},
		CancelButton: &closeButton,
		Layout:       VBox{Margins: Margins{Left: 16, Top: 16, Right: 16, Bottom: 14}, Spacing: 12},
		Children: []Widget{
			Label{Text: "Software updates", TextColor: text, Font: Font{Family: "Segoe UI", PointSize: 14, Bold: true}},
			TextLabel{Text: statusText, TextColor: muted, MinSize: Size{Height: 100}},
			Composite{Background: SolidColorBrush{Color: bg}, Layout: HBox{MarginsZero: true, Spacing: 8}, Children: []Widget{
				HSpacer{},
				PushButton{AssignTo: &downloadButton, Text: "Download update", Enabled: canDownload, MinSize: Size{Width: 118, Height: 30}, OnClicked: func() {
					if err := openURL(manifest.DownloadURL); err != nil {
						a.set(a.actionStatus, "Unable to open update: "+compactMiddle(err.Error(), 90))
						a.color(a.actionStatus, red)
						return
					}
					dlg.Close(walk.DlgCmdOK)
				}},
				PushButton{AssignTo: &closeButton, Text: "Close", MinSize: Size{Width: 86, Height: 30}, OnClicked: func() { dlg.Close(walk.DlgCmdClose) }},
			}},
		},
	}.Create(a.window)
	if err == nil {
		dlg.Run()
	}
}

func openURL(raw string) error {
	if !strings.HasPrefix(strings.ToLower(raw), "https://") {
		return fmt.Errorf("update URL is not HTTPS")
	}
	return windows.ShellExecute(0, nil, windows.StringToUTF16Ptr(raw), nil, nil, windows.SW_SHOWNORMAL)
}

func (a *appUI) collectNow() {
	if a.collectButton != nil {
		a.collectButton.SetEnabled(false)
	}
	a.set(a.actionStatus, "Collecting now...")
	a.color(a.actionStatus, muted)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
		defer cancel()
		err := agent.CollectNow(ctx, a.configPath)
		a.window.Synchronize(func() {
			if a.collectButton != nil {
				a.collectButton.SetEnabled(true)
			}
			if err != nil {
				a.set(a.actionStatus, "Collection failed: "+compactMiddle(err.Error(), 90))
				a.color(a.actionStatus, red)
			} else {
				a.set(a.actionStatus, "Collection complete.")
				a.color(a.actionStatus, green)
			}
			a.refresh()
		})
	}()
}

func (a *appUI) showSettings() {
	cfg, err := config.LoadForEdit(a.configPath)
	if err != nil {
		a.set(a.actionStatus, "Unable to open settings: "+compactMiddle(err.Error(), 90))
		a.color(a.actionStatus, red)
		return
	}
	var dlg *walk.Dialog
	var remoteURL *walk.LineEdit
	var apmEnabled *walk.CheckBox
	var status *walk.Label
	var saveButton *walk.PushButton
	var cancelButton *walk.PushButton
	registrationLabel := "Starting registration"
	if current, readErr := agent.ReadStatus(a.configPath); readErr == nil {
		registrationLabel = registrationStateLabel(current.AuthState)
	}
	credentialLabel := storedCredentialState(cfg)
	saveSettings := func() (config.Config, error) {
		next := cfg
		normalized, err := config.NormalizeControllerURL(remoteURL.Text())
		if err != nil {
			return next, fmt.Errorf("invalid controller URL")
		}
		next.ControllerURL = normalized
		next.APM.Enabled = apmEnabled != nil && apmEnabled.Checked()
		if err := next.Validate(); err != nil {
			return next, fmt.Errorf("invalid settings")
		}
		if err := config.Save(a.configPath, next); err != nil {
			return next, fmt.Errorf("save failed")
		}
		return next, nil
	}
	err = Dialog{
		AssignTo:      &dlg,
		Title:         "ZenPlus Agent Settings",
		Icon:          loadAppIcon(),
		Background:    SolidColorBrush{Color: bg},
		Size:          Size{Width: 580, Height: 405},
		MinSize:       Size{Width: 550, Height: 385},
		DefaultButton: &saveButton,
		CancelButton:  &cancelButton,
		Layout:        VBox{Margins: Margins{Left: 14, Top: 14, Right: 14, Bottom: 12}, Spacing: 10},
		Children: []Widget{
			GroupBox{
				Title:      "ZenPlus Appliance",
				Background: SolidColorBrush{Color: surface},
				Layout:     Grid{Margins: Margins{Left: 12, Top: 10, Right: 12, Bottom: 10}, Spacing: 8, Columns: 2},
				Children: []Widget{
					Label{Text: "Controller URL or IP", TextColor: text, MinSize: Size{Width: 140}},
					LineEdit{AssignTo: &remoteURL, Text: cfg.ControllerURL, CueBanner: "https://192.168.8.221", Background: SolidColorBrush{Color: fieldBg}, TextColor: text, MinSize: Size{Height: 28}},
					Label{Text: "Authorization", TextColor: text},
					TextLabel{Text: registrationLabel, TextColor: muted, MinSize: Size{Height: 24}},
					Label{Text: "Appliance credential", TextColor: text},
					TextLabel{Text: credentialLabel, TextColor: muted, MinSize: Size{Height: 24}},
				},
			},
			GroupBox{
				Title:      "Managed Application Performance Monitoring",
				Background: SolidColorBrush{Color: surface},
				Layout:     Grid{Margins: Margins{Left: 12, Top: 8, Right: 12, Bottom: 9}, Spacing: 7, Columns: 2},
				Children: []Widget{
					Label{Text: "Local gateway", TextColor: text, MinSize: Size{Width: 140}},
					CheckBox{AssignTo: &apmEnabled, Text: "Enable APM on this server", Checked: cfg.APM.Enabled},
					Label{Text: "Install profile", TextColor: text},
					TextLabel{Text: friendlyProfile(cfg.APM.Profile), TextColor: muted, MinSize: Size{Height: 24}},
					Label{Text: "Local OTLP endpoints", TextColor: text},
					TextLabel{Text: "127.0.0.1:4317 (gRPC) · 127.0.0.1:4318 (HTTP)", TextColor: muted, MinSize: Size{Height: 24}},
					HSpacer{},
					TextLabel{Text: "The agent starts and monitors the bundled telemetry gateway. Its ingest credential is issued by the appliance after authorization and is never shown or stored in this window.", TextColor: muted, MinSize: Size{Width: 350, Height: 43}, Font: Font{Family: "Segoe UI", PointSize: 9}},
				},
			},
			TextLabel{Text: "New installations appear in Agent Fleet as Pending authorization. An appliance operator must approve them before monitoring data is accepted.", TextColor: muted, MinSize: Size{Width: 500, Height: 28}, Font: Font{Family: "Segoe UI", PointSize: 9}},
			Label{AssignTo: &status, Text: "", TextColor: muted, Font: Font{Family: "Segoe UI", PointSize: 9}},
			Composite{
				Background: SolidColorBrush{Color: bg},
				Layout:     HBox{MarginsZero: true, Spacing: 8},
				Children: []Widget{
					HSpacer{},
					PushButton{AssignTo: &cancelButton, Text: "Cancel", MinSize: Size{Width: 86, Height: 30}, OnClicked: func() { dlg.Close(walk.DlgCmdCancel) }},
					PushButton{AssignTo: &saveButton, Text: "Save", MinSize: Size{Width: 92, Height: 30}, OnClicked: func() {
						if _, err := saveSettings(); err != nil {
							_ = status.SetText(err.Error())
							status.SetTextColor(red)
							return
						}
						a.set(a.actionStatus, "Settings saved.")
						a.color(a.actionStatus, green)
						dlg.Close(walk.DlgCmdOK)
						a.refresh()
					}},
				},
			},
		},
	}.Create(a.window)
	if err != nil {
		a.set(a.actionStatus, "Unable to open settings: "+compactMiddle(err.Error(), 90))
		a.color(a.actionStatus, red)
		return
	}
	dlg.Run()
}

func (a *appUI) showLogs() {
	cfg, err := config.Load(a.configPath)
	if err != nil {
		a.set(a.actionStatus, "Unable to read logs: "+compactMiddle(err.Error(), 90))
		a.color(a.actionStatus, red)
		return
	}
	paths := runtime.NewPaths(cfg.DataDir)
	lines := appstate.TailLines(paths.LogFile, 220)
	textValue := "No local log lines yet."
	if len(lines) > 0 {
		textValue = strings.Join(lines, "\r\n")
	}
	var dlg *walk.Dialog
	var logView *walk.TextEdit
	var closeButton *walk.PushButton
	err = Dialog{
		AssignTo:     &dlg,
		Title:        "ZenPlus Agent Logs",
		Icon:         loadAppIcon(),
		Background:   SolidColorBrush{Color: bg},
		Size:         Size{Width: 650, Height: 380},
		MinSize:      Size{Width: 600, Height: 340},
		CancelButton: &closeButton,
		Layout:       VBox{Margins: Margins{Left: 12, Top: 12, Right: 12, Bottom: 12}, Spacing: 8},
		Children: []Widget{
			TextEdit{AssignTo: &logView, Text: textValue, ReadOnly: true, VScroll: true, HScroll: true, Background: SolidColorBrush{Color: fieldBg}, TextColor: text, Font: Font{Family: "Consolas", PointSize: 9}, StretchFactor: 1},
			Composite{
				Background: SolidColorBrush{Color: bg},
				Layout:     HBox{MarginsZero: true, Spacing: 8},
				Children: []Widget{
					PushButton{Text: "Clear", MinSize: Size{Width: 82, Height: 30}, OnClicked: func() {
						if err := a.clearLogFile(); err != nil {
							_ = logView.SetText("Unable to clear logs: " + err.Error())
							return
						}
						_ = logView.SetText("")
						a.set(a.actionStatus, "Logs cleared.")
						a.color(a.actionStatus, muted)
					}},
					HSpacer{},
					PushButton{AssignTo: &closeButton, Text: "Close", MinSize: Size{Width: 86, Height: 30}, OnClicked: func() { dlg.Close(walk.DlgCmdClose) }},
				},
			},
		},
	}.Create(a.window)
	if err != nil {
		a.set(a.actionStatus, "Unable to open logs: "+compactMiddle(err.Error(), 90))
		a.color(a.actionStatus, red)
		return
	}
	dlg.Run()
}

func (a *appUI) clearLogs() {
	if err := a.clearLogFile(); err != nil {
		a.set(a.actionStatus, "Unable to clear logs: "+compactMiddle(err.Error(), 90))
		a.color(a.actionStatus, red)
		return
	}
	a.set(a.actionStatus, "Logs cleared.")
	a.color(a.actionStatus, muted)
}

func (a *appUI) clearLogFile() error {
	cfg, err := config.Load(a.configPath)
	if err != nil {
		return err
	}
	paths := runtime.NewPaths(cfg.DataDir)
	if err := os.MkdirAll(filepath.Dir(paths.LogFile), 0o700); err != nil {
		return err
	}
	if err := os.WriteFile(paths.LogFile, []byte{}, 0o600); err != nil {
		return err
	}
	return nil
}

func (a *appUI) refresh() {
	if !a.refreshInFlight.CompareAndSwap(false, true) {
		return
	}
	go func() {
		defer a.refreshInFlight.Store(false)
		ctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
		defer cancel()
		snap := appstate.Load(ctx, a.configPath)
		if a.window == nil || a.window.IsDisposed() {
			return
		}
		a.window.Synchronize(func() {
			if a.window == nil || a.window.IsDisposed() {
				return
			}
			a.applySnapshot(snap)
		})
	}()
}

func (a *appUI) applySnapshot(snap appstate.Snapshot) {
	health, healthTone := appstate.HealthText(snap)
	healthColor := toneColor(healthTone)
	controllerText, controllerColor := connectionState(snap)

	hostname := "Windows host"
	if snap.Identity != nil && snap.Identity.Hostname != "" {
		hostname = snap.Identity.Hostname
	}
	agentID := "-"
	if snap.Status != nil && snap.Status.AgentID != "" {
		agentID = snap.Status.AgentID
	} else if snap.Identity != nil && snap.Identity.AgentID != "" {
		agentID = snap.Identity.AgentID
	}

	started := "-"
	lastCollection := "-"
	if snap.Status != nil {
		started = formatTime(snap.Status.StartedAt)
		lastCollection = appstate.TimeAgo(snap.Status.LastCollection)
	}

	queue := fmt.Sprintf("%d batch%s, %s queued", snap.Spool.Depth, plural(snap.Spool.Depth), appstate.Bytes(snap.Spool.Bytes))
	controllerLine := compactMiddle(snap.Config.ControllerURL, 52)
	policyLine := value(snap.Config.PolicyID)
	if policyLine == "-" {
		policyLine = "Default"
	}
	statusDetail := snap.Controller.Status
	if statusDetail == "" || statusDetail == "unknown" {
		statusDetail = snap.Controller.Message
	}
	if statusDetail == "" {
		statusDetail = "Waiting for controller response"
	}
	serviceLine := serviceText(snap.Service)

	a.set(a.statusBadge, badgeText(health, healthTone))
	a.color(a.statusBadge, healthColor)
	a.set(a.topStatus, health)
	a.color(a.topStatus, healthColor)
	a.set(a.topConnection, controllerText)
	a.color(a.topConnection, controllerColor)
	a.set(a.hostName, hostname)
	a.set(a.agentID, compactMiddle(agentID, 36))
	a.set(a.policyID, compactMiddle(policyLine, 36))
	a.set(a.serviceStatus, serviceLine)
	a.set(a.controllerURL, controllerLine)
	a.set(a.queueDepth, queue)
	a.set(a.lastCollection, lastCollection)
	apmService, apmActivity, apmEndpoint, apmQueue, apmColor := apmSummary(snap.Status)
	if strings.HasPrefix(apmEndpoint, "/") && snap.Config.ControllerURL != "" {
		apmEndpoint = strings.TrimRight(snap.Config.ControllerURL, "/") + apmEndpoint
	}
	a.set(a.apmService, apmService)
	a.color(a.apmService, apmColor)
	a.set(a.apmActivity, apmActivity)
	a.set(a.apmEndpoint, compactMiddle(apmEndpoint, 32))
	a.set(a.apmQueue, apmQueue)
	if snap.Status != nil && snap.Status.APM != nil {
		switch snap.Status.APM.State {
		case "active":
			a.color(a.apmQueue, green)
		case "starting", "degraded":
			a.color(a.apmQueue, amber)
		default:
			a.color(a.apmQueue, red)
		}
	}
	a.set(a.versionStatus, "v"+model.AgentVersion+" stable")
	if a.updateStatus != nil && (a.updateStatus.Text() == "" || a.updateStatus.Text() == "-") {
		a.set(a.updateStatus, "Not checked")
		a.color(a.updateStatus, muted)
	}
	a.set(a.actionStatus, statusSummary(snap, started, statusDetail))
	a.color(a.actionStatus, muted)
	if a.tray != nil {
		_ = a.tray.SetToolTip(compactMiddle("ZenPlus Agent - "+health+" - "+serviceLine, 120))
	}
}

func apmSummary(status *model.Status) (local, activity, endpoint, appliance string, localColor walk.Color) {
	local = "Checking local host"
	localColor = muted
	if status != nil && status.LocalAPM != nil {
		profile := friendlyProfile(status.LocalAPM.Profile)
		switch {
		case !status.LocalAPM.Enabled:
			local = "Disabled · " + profile
		case status.LocalAPM.State == "waiting_authorization":
			local = "Waiting for approval"
			localColor = amber
		case status.LocalAPM.Gateway.Managed && status.LocalAPM.Gateway.Healthy:
			local = "Active · managed gateway"
			localColor = green
		case status.LocalAPM.State == "failed" || status.LocalAPM.State == "credential_error" || status.LocalAPM.State == "configuration_error":
			local = "APM needs attention"
			localColor = red
		case status.LocalAPM.Gateway.Listening:
			local = "Starting · OTLP listening"
			localColor = amber
		default:
			local = "Starting managed gateway"
			localColor = amber
		}
	}
	if status == nil || status.APM == nil {
		return local, "No status received", "/v1/traces", "Waiting for appliance", localColor
	}
	apm := status.APM
	endpoint = apm.IngestPath
	if endpoint == "" {
		endpoint = "/v1/traces"
	}
	if apm.LastReceivedAt != nil {
		activity = fmt.Sprintf("%s ago · %d spans", appstate.TimeAgo(apm.LastReceivedAt), apm.AcceptedSpansTotal)
	} else if apm.AcceptedSpansTotal > 0 {
		activity = fmt.Sprintf("%d spans accepted", apm.AcceptedSpansTotal)
	} else {
		activity = "No telemetry received"
	}
	switch apm.State {
	case "active":
		appliance = fmt.Sprintf("Active · queue %d/%d", apm.QueueDepth, apm.QueueCapacity)
	case "starting":
		appliance = "Starting"
	case "degraded":
		appliance = "Degraded"
	default:
		appliance = "Unavailable"
	}
	return local, activity, endpoint, appliance, localColor
}

func friendlyProfile(profile string) string {
	switch strings.ToLower(strings.TrimSpace(profile)) {
	case "apm":
		return "APM only"
	case "infrastructure":
		return "Server monitoring only"
	default:
		return "Server monitoring + APM"
	}
}

func (a *appUI) set(label *walk.Label, text string) {
	if label != nil {
		_ = label.SetText(text)
	}
}

func (a *appUI) color(label *walk.Label, c walk.Color) {
	if label != nil {
		label.SetTextColor(c)
	}
}

func (a *appUI) setTextEdit(edit *walk.TextEdit, text string) {
	if edit == nil {
		return
	}
	if edit.Text() == text {
		return
	}
	_ = edit.SetText(text)
	edit.SendMessage(win.WM_VSCROLL, uintptr(win.SB_BOTTOM), 0)
	edit.SendMessage(win.WM_HSCROLL, uintptr(win.SB_LEFT), 0)
}

func connectionState(s appstate.Snapshot) (string, walk.Color) {
	if s.Status != nil {
		switch s.Status.AuthState {
		case "pending", "unenrolled":
			return "Awaiting appliance approval", amber
		case "revoked", "unauthorized":
			return "Authorization required", red
		}
	}
	if !s.Controller.Reachable {
		return "Controller unreachable", red
	}
	if s.Status != nil && s.Status.LastHeartbeat != nil && s.Status.LastHeartbeatError == "" {
		return "Connected and reporting", green
	}
	return "Controller reachable", blue
}

func statusSummary(s appstate.Snapshot, started string, controllerDetail string) string {
	if s.Status != nil {
		switch s.Status.AuthState {
		case "pending", "unenrolled":
			return "Waiting for approval in ZenPlus Agent Fleet. Monitoring data is being buffered locally."
		case "revoked":
			return "Authorization was revoked by the appliance. Monitoring data is being buffered locally."
		case "unauthorized":
			return "The appliance rejected this credential. Approve the agent again in Agent Fleet."
		}
		if s.Status.LastHeartbeat != nil && s.Status.LastHeartbeatError == "" {
			return fmt.Sprintf("Monitoring active | Last heartbeat %s | Started %s", appstate.TimeAgo(s.Status.LastHeartbeat), started)
		}
		if s.Status.LastHeartbeatError != "" {
			return "Connection problem: " + compactMiddle(s.Status.LastHeartbeatError, 92)
		}
	}
	if !s.Controller.Reachable {
		if s.Controller.Message != "" {
			return "Cannot reach the controller: " + compactMiddle(s.Controller.Message, 88)
		}
		return "Cannot reach the configured ZenPlus controller."
	}
	return fmt.Sprintf("Starting secure registration | %s", compactMiddle(controllerDetail, 80))
}

func toneColor(tone string) walk.Color {
	switch tone {
	case "ok":
		return green
	case "warn":
		return amber
	default:
		return red
	}
}

func badgeText(health string, tone string) string {
	switch health {
	case "Pending authorization":
		return "WAIT"
	case "Authorization revoked", "Authorization required":
		return "AUTH"
	case "Connecting":
		return "SYNC"
	}
	switch tone {
	case "ok":
		return "OK"
	case "warn":
		return "WARN"
	default:
		return "ERR"
	}
}

func serviceText(service appstate.ServiceSnapshot) string {
	if !service.Installed {
		if service.Error != "" {
			return "Service unavailable"
		}
		return "Service not installed"
	}
	if service.StartMode != "" {
		return "Service " + strings.ToLower(service.State) + " (" + service.StartMode + ")"
	}
	return "Service " + strings.ToLower(service.State)
}

func collectorLine(values map[string]bool) string {
	keys := make([]string, 0, len(values))
	for k := range values {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		if values[k] {
			parts = append(parts, strings.ReplaceAll(k, "_", " "))
		}
	}
	if len(parts) == 0 {
		return "Collectors: none"
	}
	return "Collectors: " + strings.Join(parts, " | ")
}

func makeIcon(size int) image.Image {
	img := image.NewRGBA(image.Rect(0, 0, size, size))
	for y := 0; y < size; y++ {
		for x := 0; x < size; x++ {
			img.Set(x, y, color.RGBA{R: 0, G: 137, B: 129, A: 255})
		}
	}
	white := color.RGBA{R: 255, G: 255, B: 255, A: 255}
	thick := max(2, size/7)
	left := size / 4
	right := size - left
	top := size / 4
	bottom := size - top - thick
	fillRect(img, left, top, right, top+thick, white)
	fillRect(img, left, bottom, right, bottom+thick, white)
	for i := 0; i < right-left; i++ {
		x := right - i - thick
		y := top + i*(bottom-top)/(right-left)
		fillRect(img, x, y, x+thick, y+thick, white)
	}
	return img
}

func fillRect(img *image.RGBA, left, top, right, bottom int, c color.RGBA) {
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

func max(a int, b int) int {
	if a > b {
		return a
	}
	return b
}

func value(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

func storedCredentialState(cfg config.Config) string {
	paths := runtime.NewPaths(cfg.DataDir)
	if _, err := os.Stat(paths.CredentialFile); err == nil {
		return "Issued by appliance and protected by Windows"
	}
	return "Not issued yet"
}

func registrationStateLabel(state string) string {
	switch state {
	case "ok":
		return "Authorized"
	case "pending", "unenrolled":
		return "Pending appliance approval"
	case "revoked":
		return "Revoked by appliance"
	case "unauthorized":
		return "Authorization required"
	default:
		return "Starting registration"
	}
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "es"
}

func compactMiddle(s string, n int) string {
	if s == "" || len(s) <= n {
		return value(s)
	}
	if n < 8 {
		return s[:n]
	}
	head := (n - 3) / 2
	tail := n - 3 - head
	return s[:head] + "..." + s[len(s)-tail:]
}

func formatTime(t time.Time) string {
	if t.IsZero() {
		return "-"
	}
	return t.Local().Format("Jan 02, 2006 03:04 PM")
}
