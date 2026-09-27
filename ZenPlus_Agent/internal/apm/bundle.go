package apm

import (
	"os"
	"path/filepath"
)

// NotInstalledMessage explains an APM profile on a server-monitoring-only
// installation, which has no gateway or instrumentation components.
const NotInstalledMessage = "APM is enabled for this server, but its APM components are not installed. Install the ZenPlus Agent package with APM to use it."

// BundleInstalled reports whether installDir carries the APM bundle that the
// full ("with APM") package installs next to the agent.
func BundleInstalled(installDir string) bool {
	_, err := os.Stat(filepath.Join(installDir, "apm", "bundle-manifest.json"))
	return err == nil
}

// BundleInstalledWithAgent reports whether the running agent's installation
// includes the APM bundle.
func BundleInstalledWithAgent() bool {
	exe, err := os.Executable()
	if err != nil {
		return false
	}
	return BundleInstalled(filepath.Dir(exe))
}
