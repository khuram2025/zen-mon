package pinger

import (
	"os"
	"testing"

	"github.com/zenplus/poller/internal/config"
)

// config.Load refuses unset datastore passwords on a real appliance. Unit
// tests run without /opt/zenplus/.env, so they use the development escape
// hatch exactly as a developer would.
func TestMain(m *testing.M) {
	if os.Getenv(config.AllowInsecureDefaultsEnv) == "" {
		os.Setenv(config.AllowInsecureDefaultsEnv, "1")
	}
	os.Exit(m.Run())
}
