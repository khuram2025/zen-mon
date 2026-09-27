// Command winres writes the Windows resources (icon, application manifest and
// version information) that the Go linker embeds from rsrc.syso, so Explorer,
// Task Manager and Programs and Features show the agent as a Zentryc LLC
// product with its version. scripts\build.ps1 runs it for every release.
package main

import (
	"bytes"
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"time"

	"github.com/tc-hib/winres"
	"github.com/tc-hib/winres/version"
)

const (
	company = "Zentryc LLC"
	product = "ZenPlus Agent"
)

// target is one executable and the resources it gets.
type target struct {
	dir, exe, description, manifest string
}

var targets = []target{
	{dir: "cmd/zenplus-agent", exe: "zenplus-agent.exe", description: "ZenPlus Agent Service"},
	{dir: "cmd/zenplus-agent-app", exe: "zenplus-agent-app.exe", description: "ZenPlus Agent",
		manifest: "cmd/zenplus-agent-app/zenplus-agent-app.manifest"},
	{dir: "cmd/zenplus-agent-installer", exe: "ZenPlusAgentSetup.exe", description: "ZenPlus Agent Setup",
		manifest: "cmd/zenplus-agent-installer/zenplus-agent-installer.manifest"},
}

func main() {
	root := flag.String("root", ".", "agent source root")
	flag.Parse()
	ver, err := agentVersion(filepath.Join(*root, "internal", "model", "model.go"))
	if err != nil {
		log.Fatal(err)
	}
	icon, err := os.ReadFile(filepath.Join(*root, "assets", "zenplus-agent.ico"))
	if err != nil {
		log.Fatal(err)
	}
	for _, t := range targets {
		if err := write(*root, t, ver, icon); err != nil {
			log.Fatalf("%s: %v", t.exe, err)
		}
		fmt.Printf("wrote %s/rsrc.syso (%s %s)\n", t.dir, t.description, ver)
	}
}

func agentVersion(modelPath string) (string, error) {
	src, err := os.ReadFile(modelPath)
	if err != nil {
		return "", err
	}
	m := regexp.MustCompile(`AgentVersion\s*=\s*"(\d+\.\d+\.\d+)"`).FindSubmatch(src)
	if m == nil {
		return "", fmt.Errorf("AgentVersion not found in %s", modelPath)
	}
	return string(m[1]), nil
}

func write(root string, t target, ver string, icoData []byte) error {
	rs := winres.ResourceSet{}
	icon, err := winres.LoadICO(bytes.NewReader(icoData))
	if err != nil {
		return fmt.Errorf("load icon: %w", err)
	}
	if err := rs.SetIcon(winres.ID(1), icon); err != nil {
		return err
	}
	if t.manifest != "" {
		manifest, err := os.ReadFile(filepath.Join(root, t.manifest))
		if err != nil {
			return err
		}
		if err := rs.Set(winres.RT_MANIFEST, winres.ID(1), winres.LCIDDefault, manifest); err != nil {
			return err
		}
	}
	var vi version.Info
	vi.SetFileVersion(ver + ".0")
	vi.SetProductVersion(ver + ".0")
	for key, value := range map[string]string{
		version.CompanyName:      company,
		version.ProductName:      product,
		version.FileDescription:  t.description,
		version.FileVersion:      ver,
		version.ProductVersion:   ver,
		version.InternalName:     t.exe,
		version.OriginalFilename: t.exe,
		version.LegalCopyright:   fmt.Sprintf("© %d %s", time.Now().Year(), company),
	} {
		if err := vi.Set(version.LangDefault, key, value); err != nil {
			return err
		}
	}
	rs.SetVersionInfo(vi)

	var out bytes.Buffer
	if err := rs.WriteObject(&out, winres.ArchAMD64); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(root, t.dir, "rsrc.syso"), out.Bytes(), 0o644)
}
