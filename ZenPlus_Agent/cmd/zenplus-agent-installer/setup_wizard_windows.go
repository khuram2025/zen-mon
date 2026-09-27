//go:build windows

package main

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/lxn/walk"
	. "github.com/lxn/walk/declarative"
	"github.com/lxn/win"
	"golang.org/x/sys/windows"

	"zenplus-agent/internal/brand"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
)

// The Setup wizard follows the page sequence of standard Windows installers:
// welcome, terms, options, connection, ready, progress and finish, with
// Back / Next / Cancel. Silent installs (/quiet) never open it.

const (
	pageWelcome = iota
	pageTerms
	pageOptions
	pageConnect
	pageReady
	pageInstalling
	pageFinish
	pageCount
)

var pageHeadings = [pageCount][2]string{
	{"Welcome", "ZenPlus Agent Setup"},
	{"Terms of installation", "Please read the following terms carefully"},
	{"Installation options", "Choose how ZenPlus Agent is installed on this computer"},
	{"Connect to ZenPlus", "Enter the address of your ZenPlus appliance"},
	{"Ready to install", "Setup has the information it needs"},
	{"Installing", "Please wait while Setup installs ZenPlus Agent"},
	{"Setup complete", "ZenPlus Agent Setup has finished"},
}

const installTerms = `ZENPLUS AGENT: TERMS OF INSTALLATION AND DATA COLLECTION

Your use of ZenPlus Agent is subject to your organization's agreement with Zentryc LLC. Please read these terms before you install the agent.

1. Authorized use
Install ZenPlus Agent only on computers you are authorized to manage. By installing it, you confirm that you have permission to do so on behalf of the owner of this computer.

2. What the agent collects
ZenPlus Agent runs in the background and starts with Windows. It collects performance metrics (processor, memory, disks and network), running processes and services, Windows event log summaries, and installed software and hardware inventory. When application performance monitoring (APM) is enabled, it also collects traces from the applications you choose to monitor.

3. Where the data goes
The agent sends this data only to the ZenPlus appliance you configure during Setup, over encrypted HTTPS connections. It opens no network ports to other computers; when APM is enabled, its trace receiver listens only on this computer.

4. Security
This computer receives its credential only after an administrator approves it in ZenPlus Agent Fleet. The credential is protected with Windows Data Protection. The agent installs updates only when they are signed by Zentryc LLC.

5. Control and removal
Your ZenPlus administrators control what is collected from the appliance. You can remove ZenPlus Agent at any time from Settings > Apps or Programs and Features. Removing it stops all collection.`

type setupWizard struct {
	opts      options
	elevated  bool
	elevation elevation
	installed string // version already installed, or ""

	window    *walk.MainWindow
	heading   *walk.Label
	subhead   *walk.Label
	pages     [pageCount]*walk.Composite
	page      int
	back      *walk.PushButton
	next      *walk.PushButton
	cancel    *walk.PushButton
	accept    *walk.RadioButton
	decline   *walk.RadioButton
	machine   *walk.RadioButton
	user      *walk.RadioButton
	scopeNote *walk.TextLabel
	combined  *walk.RadioButton
	apmOnly   *walk.RadioButton
	startMenu *walk.CheckBox

	controller *walk.LineEdit
	testButton *walk.PushButton
	testResult *walk.TextLabel

	summary [6]*walk.Label

	status   *walk.Label
	progress *walk.ProgressBar
	logs     *walk.TextEdit

	finishIcon    *walk.ImageView
	finishHeading *walk.Label
	finishText    *walk.TextLabel
	launchAfter   *walk.CheckBox
	failDetails   *walk.TextEdit

	working   bool
	succeeded bool
	layout    layout
	resultErr error
}

func runSetupUI(opts options, elevated bool) error {
	w := &setupWizard{opts: opts, elevated: elevated}
	return w.run()
}

var (
	textFont  = Font{Family: "Segoe UI", PointSize: 9}
	boldFont  = Font{Family: "Segoe UI", PointSize: 9, Bold: true}
	titleFont = Font{Family: "Segoe UI", PointSize: 14, Bold: true}
)

func para(text string, color walk.Color) TextLabel {
	return TextLabel{Text: text, TextColor: color, Font: textFont, MinSize: Size{Width: 540}}
}

func (w *setupWizard) run() error {
	w.hydrateExistingInstall()
	w.elevation = currentElevation()
	w.installed = installedVersion()
	controllerValue := strings.TrimSpace(w.opts.controllerURL)
	if controllerValue == "" {
		controllerValue = config.Default().ControllerURL
	}

	err := MainWindow{
		AssignTo:   &w.window,
		Title:      productName + " Setup",
		Icon:       setupIcon(),
		Font:       textFont,
		Background: SolidColorBrush{Color: setupPanel},
		Size:       Size{Width: 640, Height: 500},
		MinSize:    Size{Width: 640, Height: 500},
		MaxSize:    Size{Width: 640, Height: 500},
		Layout:     VBox{MarginsZero: true, Spacing: 0},
		Children: []Widget{
			w.header(),
			Composite{
				Background: SolidColorBrush{Color: setupPanel},
				Layout:     VBox{Margins: Margins{Left: 28, Top: 20, Right: 28, Bottom: 12}},
				Children: []Widget{
					w.welcomePage(),
					w.termsPage(),
					w.optionsPage(),
					w.connectPage(controllerValue),
					w.readyPage(),
					w.installingPage(),
					w.finishPage(),
				},
			},
			Composite{Background: SolidColorBrush{Color: setupLine}, MinSize: Size{Height: 1}, MaxSize: Size{Height: 1}},
			w.footer(),
		},
	}.Create()
	if err != nil {
		return err
	}
	// A wizard has a fixed size: no maximize box or resizable frame.
	hwnd := w.window.Handle()
	style := win.GetWindowLong(hwnd, win.GWL_STYLE)
	win.SetWindowLong(hwnd, win.GWL_STYLE, style&^(win.WS_MAXIMIZEBOX|win.WS_THICKFRAME))

	w.window.Closing().Attach(func(canceled *bool, _ walk.CloseReason) {
		switch {
		case w.working:
			*canceled = true
		case w.page < pageInstalling && !w.confirmCancel():
			*canceled = true
		}
	})
	w.machine.SetChecked(w.opts.machine || !w.opts.user)
	w.user.SetChecked(!w.machine.Checked())
	if w.combined != nil {
		w.combined.SetChecked(w.opts.profile != "apm")
		w.apmOnly.SetChecked(w.opts.profile == "apm")
	}
	w.decline.SetChecked(true)
	w.show(pageWelcome)
	w.window.Run()
	return w.resultErr
}

// hydrateExistingInstall defaults to the installed scope and settings.
func (w *setupWizard) hydrateExistingInstall() {
	if !w.opts.machine && !w.opts.user {
		if !machineInstallPresent() && userInstallPresent() {
			w.opts.user = true
		} else {
			w.opts.machine = true
		}
	}
	l, err := newLayout(w.opts, w.elevated)
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
	if strings.TrimSpace(w.opts.controllerURL) == "" {
		w.opts.controllerURL = cfg.ControllerURL
	}
	if strings.TrimSpace(w.opts.profile) == "" {
		w.opts.profile = cfg.APM.Profile
	}
}

/* ── Frame ─────────────────────────────────────────────────────────── */

func (w *setupWizard) header() Widget {
	return Composite{
		Background: SolidColorBrush{Color: setupBanner},
		MinSize:    Size{Height: 76},
		MaxSize:    Size{Height: 76},
		Layout:     HBox{Margins: Margins{Left: 24, Top: 14, Right: 20, Bottom: 12}, Spacing: 12},
		Children: []Widget{
			Composite{
				Background: SolidColorBrush{Color: setupBanner},
				Layout:     VBox{MarginsZero: true, Spacing: 2},
				Children: []Widget{
					Label{AssignTo: &w.heading, Text: pageHeadings[0][0], TextColor: walk.RGB(255, 255, 255), Font: Font{Family: "Segoe UI", PointSize: 13, Bold: true}},
					Label{AssignTo: &w.subhead, Text: pageHeadings[0][1], TextColor: walk.RGB(191, 219, 254), Font: textFont, MinSize: Size{Width: 440}},
				},
			},
			HSpacer{},
			ImageView{
				Image:      setupBrandBitmap(48),
				Mode:       ImageViewModeIdeal,
				MinSize:    Size{Width: 48, Height: 48},
				MaxSize:    Size{Width: 48, Height: 48},
				Background: SolidColorBrush{Color: setupBanner},
			},
		},
	}
}

func (w *setupWizard) footer() Widget {
	return Composite{
		Background: SolidColorBrush{Color: setupBG},
		MinSize:    Size{Height: 54},
		MaxSize:    Size{Height: 54},
		Layout:     HBox{Margins: Margins{Left: 20, Top: 11, Right: 16, Bottom: 11}, Spacing: 8},
		Children: []Widget{
			Label{Text: publisherName, TextColor: setupMuted, Font: textFont},
			HSpacer{},
			PushButton{AssignTo: &w.back, Text: "< Back", MinSize: Size{Width: 86, Height: 28}, OnClicked: w.goBack},
			PushButton{AssignTo: &w.next, Text: "Next >", MinSize: Size{Width: 96, Height: 28}, OnClicked: w.goNext},
			HSpacer{Size: 6},
			PushButton{AssignTo: &w.cancel, Text: "Cancel", MinSize: Size{Width: 86, Height: 28}, OnClicked: func() { _ = w.window.Close() }},
		},
	}
}

func (w *setupWizard) pageBox(index int, children ...Widget) Widget {
	return Composite{
		AssignTo:   &w.pages[index],
		Visible:    index == pageWelcome,
		Background: SolidColorBrush{Color: setupPanel},
		Layout:     VBox{MarginsZero: true, Spacing: 10, Alignment: AlignHNearVNear},
		Children:   append(children, VSpacer{}),
	}
}

/* ── Pages ─────────────────────────────────────────────────────────── */

func (w *setupWizard) welcomePage() Widget {
	what := "Monitors processor, memory, disks, network, processes, services and Windows event logs, and reports them to your ZenPlus appliance."
	if payloadIncludesAPM() {
		what = "Monitors this server and the performance of its applications (APM for .NET, Java, Node.js and Python), and reports them to your ZenPlus appliance."
	}
	children := []Widget{
		Label{Text: "Welcome to the ZenPlus Agent Setup Wizard", Font: titleFont, TextColor: setupText},
		para(fmt.Sprintf("This wizard installs ZenPlus Agent %s (%s) on this computer.", model.AgentVersion, flavorDisplayName()), setupText),
		para(what, setupMuted),
	}
	if w.installed != "" {
		note := fmt.Sprintf("ZenPlus Agent %s is already installed. Setup will upgrade it to %s and keep its settings, identity and approval.", w.installed, model.AgentVersion)
		if w.installed == model.AgentVersion {
			note = fmt.Sprintf("ZenPlus Agent %s is already installed. Setup will repair it and keep its settings, identity and approval.", w.installed)
		}
		children = append(children, w.infoBox(note, setupBlue))
	}
	children = append(children, VSpacer{Size: 8}, para("Click Next to continue, or Cancel to exit Setup.", setupMuted))
	return w.pageBox(pageWelcome, children...)
}

func (w *setupWizard) termsPage() Widget {
	return w.pageBox(pageTerms,
		para("You must accept these terms to install ZenPlus Agent.", setupText),
		TextEdit{
			Text:       strings.ReplaceAll(installTerms, "\n", "\r\n"),
			ReadOnly:   true,
			VScroll:    true,
			Background: SolidColorBrush{Color: setupField},
			TextColor:  setupText,
			Font:       textFont,
			MinSize:    Size{Height: 200},
		},
		Composite{
			Layout: VBox{MarginsZero: true, Spacing: 4, Alignment: AlignHNearVNear},
			Children: []Widget{
				RadioButton{AssignTo: &w.accept, Text: "I &accept the terms", OnClicked: w.refreshButtons},
				RadioButton{AssignTo: &w.decline, Text: "I &do not accept the terms", OnClicked: w.refreshButtons},
			},
		},
	)
}

func (w *setupWizard) optionsPage() Widget {
	scope := Composite{
		Layout: VBox{MarginsZero: true, Spacing: 2, Alignment: AlignHNearVNear},
		Children: []Widget{
			RadioButton{AssignTo: &w.machine, Text: "All &users (recommended)", Font: boldFont, OnClicked: w.refreshScope},
			Composite{Layout: VBox{Margins: Margins{Left: 20, Bottom: 8}, Alignment: AlignHNearVNear}, Children: []Widget{
				para("Installs the ZenPlus Agent Windows service, which monitors this computer even when no one is signed in. Requires administrator rights.", setupMuted),
			}},
			RadioButton{AssignTo: &w.user, Text: "Only for &me (evaluation)", Font: boldFont, OnClicked: w.refreshScope},
			Composite{Layout: VBox{Margins: Margins{Left: 20}, Alignment: AlignHNearVNear}, Children: []Widget{
				para("Runs only while you are signed in, without a Windows service. Not suitable for servers.", setupMuted),
			}},
		},
	}
	children := []Widget{
		Label{Text: "Install ZenPlus Agent for", Font: boldFont, TextColor: setupText},
		scope,
		TextLabel{AssignTo: &w.scopeNote, Font: textFont, TextColor: setupBlue, MinSize: Size{Width: 540}},
	}
	if payloadIncludesAPM() {
		children = append(children,
			Label{Text: "Features", Font: boldFont, TextColor: setupText},
			Composite{
				Layout: VBox{MarginsZero: true, Spacing: 2, Alignment: AlignHNearVNear},
				Children: []Widget{
					RadioButton{AssignTo: &w.combined, Text: "Server monitoring and APM (recommended)"},
					RadioButton{AssignTo: &w.apmOnly, Text: "APM only, without server metrics"},
				},
			},
		)
	}
	children = append(children,
		CheckBox{AssignTo: &w.startMenu, Text: "Create a &Start Menu shortcut", Checked: !w.opts.noStartMenu},
	)
	return w.pageBox(pageOptions, children...)
}

func (w *setupWizard) connectPage(controllerValue string) Widget {
	return w.pageBox(pageConnect,
		Label{Text: "ZenPlus appliance address", Font: boldFont, TextColor: setupText},
		Composite{
			Layout: HBox{MarginsZero: true, Spacing: 8},
			Children: []Widget{
				LineEdit{
					AssignTo: &w.controller, Text: controllerValue, CueBanner: "https://zenplus.example.com",
					MinSize: Size{Height: 26}, OnTextChanged: func() {
						if w.testResult != nil {
							_ = w.testResult.SetText("")
						}
						w.refreshButtons()
					},
				},
				PushButton{AssignTo: &w.testButton, Text: "&Test connection", MinSize: Size{Width: 118, Height: 26}, OnClicked: w.testConnection},
			},
		},
		para("For example https://zenplus.example.com or https://10.0.0.5.", setupMuted),
		TextLabel{AssignTo: &w.testResult, Font: textFont, MinSize: Size{Width: 540}},
		VSpacer{Size: 6},
		w.infoBox("After installation this computer appears in ZenPlus Agent Fleet as Pending authorization. It starts reporting once an administrator approves it.", setupMuted),
	)
}

func (w *setupWizard) readyPage() Widget {
	labels := []string{"Package", "Install for", "Location", "Appliance", "Features", "Start Menu shortcut"}
	grid := []Widget{}
	for i, label := range labels {
		if label == "Features" && !payloadIncludesAPM() {
			continue // the server-monitoring package has one feature set
		}
		grid = append(grid,
			Label{Text: label, TextColor: setupMuted, Font: textFont, MinSize: Size{Width: 140}},
			Label{AssignTo: &w.summary[i], TextColor: setupText, Font: boldFont, MinSize: Size{Width: 380}},
		)
	}
	return w.pageBox(pageReady,
		para("Setup will install ZenPlus Agent with these settings:", setupText),
		Composite{
			Background: SolidColorBrush{Color: setupField},
			Layout:     Grid{Columns: 2, Margins: Margins{Left: 14, Top: 12, Right: 14, Bottom: 12}, Spacing: 8},
			Children:   grid,
		},
		VSpacer{Size: 6},
		para("Click Install to begin, or Back to review or change settings.", setupMuted),
	)
}

func (w *setupWizard) installingPage() Widget {
	return w.pageBox(pageInstalling,
		Label{AssignTo: &w.status, Text: "Preparing", Font: boldFont, TextColor: setupText, MinSize: Size{Width: 540}},
		ProgressBar{AssignTo: &w.progress, MinValue: 0, MaxValue: 100, MinSize: Size{Height: 18}},
		VSpacer{Size: 6},
		Label{Text: "Details", Font: textFont, TextColor: setupMuted},
		TextEdit{
			AssignTo: &w.logs, ReadOnly: true, VScroll: true,
			Background: SolidColorBrush{Color: setupField}, TextColor: setupMuted,
			Font: Font{Family: "Consolas", PointSize: 8}, MinSize: Size{Height: 170},
		},
	)
}

func (w *setupWizard) finishPage() Widget {
	return w.pageBox(pageFinish,
		Composite{
			Layout: HBox{MarginsZero: true, Spacing: 14},
			Children: []Widget{
				ImageView{AssignTo: &w.finishIcon, Mode: ImageViewModeIdeal, MinSize: Size{Width: 48, Height: 48}, MaxSize: Size{Width: 48, Height: 48}},
				Composite{
					Layout: VBox{MarginsZero: true, Spacing: 6},
					Children: []Widget{
						Label{AssignTo: &w.finishHeading, Font: titleFont, TextColor: setupText},
						TextLabel{AssignTo: &w.finishText, Font: textFont, TextColor: setupText, MinSize: Size{Width: 470}},
					},
				},
			},
		},
		CheckBox{AssignTo: &w.launchAfter, Text: "&Launch ZenPlus Agent", Checked: true},
		TextEdit{
			AssignTo: &w.failDetails, ReadOnly: true, VScroll: true, Visible: false,
			Background: SolidColorBrush{Color: setupField}, TextColor: setupMuted,
			Font: Font{Family: "Consolas", PointSize: 8}, MinSize: Size{Height: 150},
		},
	)
}

func (w *setupWizard) infoBox(text string, color walk.Color) Widget {
	return Composite{
		Background: SolidColorBrush{Color: setupField},
		Layout:     VBox{Margins: Margins{Left: 12, Top: 10, Right: 12, Bottom: 10}},
		Children:   []Widget{TextLabel{Text: text, TextColor: color, Font: textFont, MinSize: Size{Width: 510}}},
	}
}

/* ── Navigation ────────────────────────────────────────────────────── */

func (w *setupWizard) show(page int) {
	// Fill the page before it is shown: labels that receive text while
	// visible are not always laid out again.
	if page == pageReady {
		w.fillSummary()
	}
	for i, p := range w.pages {
		if p != nil {
			p.SetVisible(i == page)
		}
	}
	if p := w.pages[page]; p != nil {
		p.RequestLayout()
		p.Invalidate()
	}
	w.page = page
	_ = w.heading.SetText(pageHeadings[page][0])
	_ = w.subhead.SetText(pageHeadings[page][1])
	if page == pageOptions {
		w.refreshScope()
	}
	w.refreshButtons()
}

func (w *setupWizard) refreshButtons() {
	if w.next == nil {
		return
	}
	nextText, nextEnabled := "Next >", true
	switch w.page {
	case pageTerms:
		nextEnabled = w.accept != nil && w.accept.Checked()
	case pageOptions:
		nextEnabled = !(w.machine.Checked() && w.elevation == elevationUnavailable)
	case pageConnect:
		nextEnabled = strings.TrimSpace(w.controller.Text()) != ""
	case pageReady:
		nextText = "&Install"
	case pageInstalling:
		nextEnabled = false
	case pageFinish:
		nextText = "&Finish"
		if !w.succeeded {
			nextText = "&Close"
		}
	}
	_ = w.next.SetText(nextText)
	w.next.SetEnabled(nextEnabled)
	// The UAC shield marks the button that will ask for administrator approval.
	shield := uintptr(0)
	if w.page == pageReady && w.needsElevation() {
		shield = 1
	}
	win.SendMessage(w.next.Handle(), 0x160C /* BCM_SETSHIELD */, 0, shield)
	w.back.SetEnabled(w.page > pageWelcome && w.page < pageInstalling)
	w.back.SetVisible(w.page < pageInstalling)
	w.cancel.SetEnabled(!w.working)
	w.cancel.SetVisible(w.page != pageFinish)
}

func (w *setupWizard) refreshScope() {
	if w.scopeNote == nil {
		return
	}
	switch {
	case w.user.Checked() || w.elevation == elevationNotNeeded:
		_ = w.scopeNote.SetText("")
	case w.elevation == elevationPrompt:
		w.scopeNote.SetTextColor(setupBlue)
		_ = w.scopeNote.SetText("Windows will ask for administrator approval when you click Install.")
	default:
		w.scopeNote.SetTextColor(setupAmber)
		_ = w.scopeNote.SetText("This account cannot get administrator approval on this computer: User Account Control is turned off or set to deny requests from standard users. Sign in as an administrator, or ask one to run Setup.")
	}
	w.refreshButtons()
}

func (w *setupWizard) needsElevation() bool {
	return w.machine != nil && w.machine.Checked() && w.elevation != elevationNotNeeded
}

func (w *setupWizard) goBack() {
	if w.page > pageWelcome && w.page < pageInstalling {
		w.show(w.page - 1)
	}
}

func (w *setupWizard) goNext() {
	switch w.page {
	case pageConnect:
		if _, err := config.NormalizeControllerURL(w.controller.Text()); err != nil {
			w.testResult.SetTextColor(setupRed)
			_ = w.testResult.SetText("Enter a valid appliance address, for example https://zenplus.example.com.")
			return
		}
		w.show(pageReady)
	case pageReady:
		w.install()
	case pageFinish:
		if w.succeeded && w.launchAfter.Checked() {
			if err := launchDashboard(w.layout); err != nil {
				walk.MsgBox(w.window, productName+" Setup", "ZenPlus Agent is installed, but its dashboard could not be started: "+err.Error(), walk.MsgBoxIconWarning)
			}
		}
		_ = w.window.Close()
	default:
		w.show(w.page + 1)
	}
}

func (w *setupWizard) confirmCancel() bool {
	return walk.MsgBox(w.window, productName+" Setup",
		"Are you sure you want to cancel ZenPlus Agent Setup?", walk.MsgBoxYesNo|walk.MsgBoxIconQuestion) == walk.DlgCmdYes
}

/* ── Actions ───────────────────────────────────────────────────────── */

func (w *setupWizard) optionsFromUI() options {
	opts := w.opts
	opts.quiet = true
	opts.user = w.user.Checked()
	opts.machine = !opts.user
	opts.noStartMenu = !w.startMenu.Checked()
	opts.controllerURL, _ = config.NormalizeControllerURL(w.controller.Text())
	switch {
	case !payloadIncludesAPM():
		opts.profile, opts.apmMode = "infrastructure", "disabled"
	case w.apmOnly != nil && w.apmOnly.Checked():
		opts.profile, opts.apmMode = "apm", "enabled"
	default:
		opts.profile, opts.apmMode = "combined", "enabled"
	}
	return opts
}

func (w *setupWizard) fillSummary() {
	opts := w.optionsFromUI()
	l, _ := newLayout(opts, w.elevated)
	scope := "All users (Windows service)"
	if opts.user {
		scope = "Only for me (evaluation)"
	}
	features := "Server monitoring"
	switch opts.profile {
	case "combined":
		features = "Server monitoring and APM"
	case "apm":
		features = "APM only"
	}
	shortcut := "Yes"
	if opts.noStartMenu {
		shortcut = "No"
	}
	values := []string{
		fmt.Sprintf("ZenPlus Agent %s (%s)", model.AgentVersion, flavorDisplayName()),
		scope, l.InstallDir, opts.controllerURL, features, shortcut,
	}
	for i, v := range values {
		if w.summary[i] != nil {
			_ = w.summary[i].SetText(v)
		}
	}
}

func (w *setupWizard) testConnection() {
	target, err := config.NormalizeControllerURL(w.controller.Text())
	if err != nil {
		w.testResult.SetTextColor(setupRed)
		_ = w.testResult.SetText("Enter a valid appliance address, for example https://zenplus.example.com.")
		return
	}
	parsed, err := url.Parse(target)
	if err != nil {
		w.testResult.SetTextColor(setupRed)
		_ = w.testResult.SetText("Enter a valid appliance address, for example https://zenplus.example.com.")
		return
	}
	address := parsed.Host
	if parsed.Port() == "" {
		address = net.JoinHostPort(parsed.Hostname(), "443")
	}
	w.testButton.SetEnabled(false)
	w.testResult.SetTextColor(setupMuted)
	_ = w.testResult.SetText("Connecting to " + address + "...")
	go func() {
		// A TCP connection proves the address resolves and the appliance
		// accepts connections; the agent verifies its certificate itself.
		// An HTTP client would add megabytes to Setup for no extra signal.
		conn, err := net.DialTimeout("tcp", address, 8*time.Second)
		if conn != nil {
			conn.Close()
		}
		w.window.Synchronize(func() {
			w.testButton.SetEnabled(true)
			if err != nil {
				w.testResult.SetTextColor(setupRed)
				_ = w.testResult.SetText("Could not reach the appliance: " + friendlyNetError(err))
				return
			}
			w.testResult.SetTextColor(setupGreen)
			_ = w.testResult.SetText("Connected. The appliance at " + address + " accepts connections.")
		})
	}()
}

func friendlyNetError(err error) string {
	msg := err.Error()
	var netErr net.Error
	switch {
	case errors.As(err, &netErr) && netErr.Timeout():
		return "the connection timed out. Check the address and that this computer can reach the appliance on port 443."
	case strings.Contains(msg, "no such host"):
		return "the name could not be resolved. Check the address or use the appliance's IP address."
	case strings.Contains(msg, "refused"):
		return "the connection was refused. Check the address and port."
	}
	return msg
}

func (w *setupWizard) install() {
	opts := w.optionsFromUI()
	w.working = true
	w.show(pageInstalling)
	w.logLine("Installing ZenPlus Agent " + model.AgentVersion)
	_ = w.progress.SetMarqueeMode(true)
	owner := windows.Handle(w.window.Handle())
	go func() {
		l, err := newLayout(opts, w.elevated)
		if err != nil {
			w.finish(err, l)
			return
		}
		w.setStatus("Installing to " + l.InstallDir)
		w.logLine("Install location: " + l.InstallDir)
		if l.Scope == "machine" && !w.elevated {
			w.setStatus("Waiting for administrator approval")
			w.logLine("Requesting administrator approval")
			err = relaunchElevated(commandArgsFromOptions(opts, false, true), owner)
			w.finish(err, l)
			return
		}
		w.setStatus("Installing files, service and configuration")
		err = install(l, opts)
		w.finish(err, l)
	}()
}

func (w *setupWizard) setStatus(text string) {
	w.window.Synchronize(func() { _ = w.status.SetText(text) })
}

func (w *setupWizard) logLine(text string) {
	w.window.Synchronize(func() { w.logLineDirect(text) })
}

// logLineDirect appends to the details; call it on the UI thread.
func (w *setupWizard) logLineDirect(text string) {
	if w.logs.TextLength() > 0 {
		w.logs.AppendText("\r\n")
	}
	w.logs.AppendText(timestamped(text))
}

func (w *setupWizard) finish(err error, l layout) {
	w.window.Synchronize(func() {
		w.working = false
		w.layout = l
		_ = w.progress.SetMarqueeMode(false)
		w.progress.SetValue(100)
		if err == nil {
			w.succeeded = true
			w.logLineDirect("Installation completed")
			w.setFinishIcon(brand.GlyphCheck)
			_ = w.finishHeading.SetText("ZenPlus Agent is installed")
			_ = w.finishText.SetText(fmt.Sprintf("ZenPlus Agent %s is running on this computer.\r\n\r\nIt appears in ZenPlus Agent Fleet as Pending authorization and starts reporting once an administrator approves it.", model.AgentVersion))
			w.launchAfter.SetVisible(true)
		} else {
			w.resultErr = err
			errorShownInWindow = true
			w.logLineDirect("Setup failed: " + err.Error())
			w.setFinishIcon(brand.GlyphCross)
			heading := "Setup could not install ZenPlus Agent"
			text := capitalize(err.Error()) + "."
			if errors.Is(err, errElevationCancelled) {
				heading = "Setup was cancelled"
			} else {
				text += "\r\n\r\nAny changes Setup made were rolled back. Details are below."
			}
			_ = w.finishHeading.SetText(heading)
			_ = w.finishText.SetText(text)
			w.launchAfter.SetVisible(false)
			_ = w.failDetails.SetText(w.logs.Text())
			w.failDetails.SetVisible(!errors.Is(err, errElevationCancelled))
		}
		w.show(pageFinish)
		if err != nil {
			_ = w.heading.SetText("Setup failed")
			_ = w.subhead.SetText("ZenPlus Agent was not installed")
		}
	})
}

func (w *setupWizard) setFinishIcon(g brand.Glyph) {
	if bitmap, err := walk.NewBitmapFromImageForDPI(brand.StatusGlyph(48, g), 96); err == nil {
		_ = w.finishIcon.SetImage(bitmap)
	}
}

func capitalize(s string) string {
	s = strings.TrimSuffix(strings.TrimSpace(s), ".")
	if s == "" {
		return s
	}
	return strings.ToUpper(s[:1]) + s[1:]
}
