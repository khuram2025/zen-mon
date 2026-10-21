package main

import (
	"os"
	"os/exec"
	"strings"
	"testing"

	"github.com/zenplus/poller/internal/config"
)

// The collector exits non-zero, before it listens, when CLICKHOUSE_PASSWORD
// is unset/empty or the "changeme" placeholder.
func TestMainRefusesInsecureClickHousePassword(t *testing.T) {
	if os.Getenv("ZENPLUS_NETFLOW_RUN_MAIN") == "1" {
		main()
		return
	}
	for _, pw := range []string{"", config.PlaceholderSecret} {
		cmd := exec.Command(os.Args[0], "-test.run=^TestMainRefusesInsecureClickHousePassword$")
		env := []string{"ZENPLUS_NETFLOW_RUN_MAIN=1", "CLICKHOUSE_PASSWORD=" + pw,
			"NETFLOW_LISTEN=127.0.0.1:0", "NETFLOW_SFLOW_LISTEN=off", "NETFLOW_HEALTH_LISTEN=127.0.0.1:0"}
		for _, kv := range os.Environ() {
			if !strings.HasPrefix(kv, "CLICKHOUSE_PASSWORD=") && !strings.HasPrefix(kv, config.AllowInsecureDefaultsEnv+"=") {
				env = append(env, kv)
			}
		}
		cmd.Env = env
		out, err := cmd.CombinedOutput()
		exit, ok := err.(*exec.ExitError)
		if !ok || exit.ExitCode() != 1 {
			t.Fatalf("password %q: want exit 1, got %v (%s)", pw, err, out)
		}
		if !strings.Contains(string(out), "refusing to start") || !strings.Contains(string(out), "CLICKHOUSE_PASSWORD") {
			t.Fatalf("password %q: unclear refusal message: %s", pw, out)
		}
	}
}
