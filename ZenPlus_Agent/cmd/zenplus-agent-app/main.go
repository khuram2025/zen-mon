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
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/runtime"
	"zenplus-agent/internal/secrets"
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
	actionStatus   *walk.Label
	collectButton  *walk.PushButton
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
		Size:       Size{Width: 600, Height: 330},
		MinSize:    Size{Width: 560, Height: 320},
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
					Label{Text: "ZenPlus Agent", TextColor: text, Font: Font{Family: "Segoe UI", PointSize: 14, Bold: true}},
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
					a.detailTile("Queue", &a.queueDepth),
				},
			},
			Composite{
				Background: SolidColorBrush{Color: surface},
				Layout:     HBox{MarginsZero: true, Spacing: 8},
				Children: []Widget{
					a.detailTile("Policy", &a.policyID),
					a.detailTile("Service", &a.serviceStatus),
					a.detailTile("Collection", &a.lastCollection),
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
	var siteID *walk.LineEdit
	var policyID *walk.LineEdit
	var enrollmentToken *walk.LineEdit
	var status *walk.Label
	var enrollButton *walk.PushButton
	var saveButton *walk.PushButton
	var cancelButton *walk.PushButton
	credentialCfg := cfg
	if resolved, err := config.Load(a.configPath); err == nil {
		credentialCfg = resolved
	}
	tokenLabel := enrollmentTokenLabel(cfg)
	credentialLabel := storedCredentialLabel(credentialCfg)
	saveSettings := func() (config.Config, error) {
		next := cfg
		normalized, err := config.NormalizeControllerURL(remoteURL.Text())
		if err != nil {
			return next, fmt.Errorf("invalid controller URL")
		}
		next.ControllerURL = normalized
		next.SiteID = strings.TrimSpace(siteID.Text())
		next.PolicyID = strings.TrimSpace(policyID.Text())
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
		Size:          Size{Width: 580, Height: 370},
		MinSize:       Size{Width: 550, Height: 350},
		DefaultButton: &saveButton,
		CancelButton:  &cancelButton,
		Layout:        VBox{Margins: Margins{Left: 14, Top: 14, Right: 14, Bottom: 12}, Spacing: 10},
		Children: []Widget{
			GroupBox{
				Title:      "Remote Server",
				Background: SolidColorBrush{Color: surface},
				Layout:     Grid{Margins: Margins{Left: 12, Top: 10, Right: 12, Bottom: 10}, Spacing: 8, Columns: 2},
				Children: []Widget{
					Label{Text: "Controller URL", TextColor: text, MinSize: Size{Width: 118}},
					LineEdit{AssignTo: &remoteURL, Text: cfg.ControllerURL, CueBanner: "http://192.168.8.152", Background: SolidColorBrush{Color: fieldBg}, TextColor: text, MinSize: Size{Height: 28}},
					Label{Text: "Site ID", TextColor: text},
					LineEdit{AssignTo: &siteID, Text: cfg.SiteID, CueBanner: "Optional", Background: SolidColorBrush{Color: fieldBg}, TextColor: text, MinSize: Size{Height: 28}},
					Label{Text: "Policy ID", TextColor: text},
					LineEdit{AssignTo: &policyID, Text: cfg.PolicyID, CueBanner: "Optional", Background: SolidColorBrush{Color: fieldBg}, TextColor: text, MinSize: Size{Height: 28}},
					Label{Text: "Current Token", TextColor: text},
					TextLabel{Text: tokenLabel, TextColor: muted, MinSize: Size{Height: 24}},
					Label{Text: "Stored API Key", TextColor: text},
					TextLabel{Text: credentialLabel, TextColor: muted, MinSize: Size{Height: 24}},
					Label{Text: "Enrollment Token", TextColor: text},
					LineEdit{AssignTo: &enrollmentToken, CueBanner: "Paste new token to enroll", PasswordMode: true, Background: SolidColorBrush{Color: fieldBg}, TextColor: text, MinSize: Size{Height: 28}},
				},
			},
			Label{AssignTo: &status, Text: "", TextColor: muted, Font: Font{Family: "Segoe UI", PointSize: 9}},
			Composite{
				Background: SolidColorBrush{Color: bg},
				Layout:     HBox{MarginsZero: true, Spacing: 8},
				Children: []Widget{
					PushButton{AssignTo: &enrollButton, Text: "Enroll", MinSize: Size{Width: 92, Height: 30}, OnClicked: func() {
						token := strings.TrimSpace(enrollmentToken.Text())
						if token == "" {
							_ = status.SetText("Enter enrollment token")
							status.SetTextColor(red)
							return
						}
						if _, err := saveSettings(); err != nil {
							_ = status.SetText(err.Error())
							status.SetTextColor(red)
							return
						}
						_ = status.SetText("Enrolling...")
						status.SetTextColor(muted)
						enrollButton.SetEnabled(false)
						saveButton.SetEnabled(false)
						cancelButton.SetEnabled(false)
						go func() {
							ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
							defer cancel()
							result, err := agent.EnrollNow(ctx, a.configPath, token)
							if a.window == nil || a.window.IsDisposed() {
								return
							}
							a.window.Synchronize(func() {
								enrollButton.SetEnabled(true)
								saveButton.SetEnabled(true)
								cancelButton.SetEnabled(true)
								if err != nil {
									_ = status.SetText("Enroll failed: " + compactMiddle(err.Error(), 80))
									status.SetTextColor(red)
									a.set(a.actionStatus, "Enrollment failed.")
									a.color(a.actionStatus, red)
									return
								}
								_ = enrollmentToken.SetText("")
								msg := fmt.Sprintf("Enrolled agent %s using %s", compactMiddle(result.Identity.AgentID, 30), secrets.Mask(result.APIKey))
								_ = status.SetText(msg)
								status.SetTextColor(green)
								a.set(a.actionStatus, msg)
								a.color(a.actionStatus, green)
								dlg.Close(walk.DlgCmdOK)
								a.refresh()
							})
						}()
					}},
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
	controllerText, controllerColor := controllerState(snap)

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

	a.set(a.statusBadge, badgeText(healthTone))
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
	a.set(a.actionStatus, fmt.Sprintf("Started %s | %s", started, compactMiddle(statusDetail, 90)))
	a.color(a.actionStatus, muted)
	if a.tray != nil {
		_ = a.tray.SetToolTip(compactMiddle("ZenPlus Agent - "+health+" - "+serviceLine, 120))
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

func controllerState(s appstate.Snapshot) (string, walk.Color) {
	if s.Controller.Reachable {
		return "Controller online", green
	}
	return "Controller offline", red
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

func badgeText(tone string) string {
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

func enrollmentTokenLabel(cfg config.Config) string {
	if cfg.EnrollmentToken == "" {
		return "Not stored"
	}
	return secrets.Mask(cfg.EnrollmentToken)
}

func storedCredentialLabel(cfg config.Config) string {
	paths := runtime.NewPaths(cfg.DataDir)
	if meta, err := secrets.ReadMetadata(paths.CredentialMeta); err == nil {
		if label := meta.Label(); label != "" {
			return label
		}
	}
	if plain, err := secrets.UnprotectFromFile(paths.CredentialFile); err == nil && len(plain) > 0 {
		return secrets.NewMetadata(plain).Label()
	}
	if _, err := os.Stat(paths.CredentialFile); err == nil {
		return "Stored, protected by another Windows account"
	}
	return "Not stored"
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
