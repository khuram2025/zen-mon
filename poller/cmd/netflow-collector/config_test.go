package main

import (
	"net/netip"
	"testing"
	"time"
)

// A typo in a numeric setting falls back to the default instead of, for
// example, a zero insert timeout that would fail every write forever.
func TestLoadConfigRejectsInvalidNumbers(t *testing.T) {
	t.Setenv("NETFLOW_INSERT_TIMEOUT_SECONDS", "0")
	t.Setenv("NETFLOW_SHUTDOWN_TIMEOUT_SECONDS", "-5")
	t.Setenv("NETFLOW_BATCH_SIZE", "lots")
	t.Setenv("NETFLOW_FLUSH_SECONDS", "0")
	t.Setenv("NETFLOW_SPOOL_MAX_MB", "0")
	t.Setenv("NETFLOW_STATS_INTERVAL_SECONDS", "1")
	t.Setenv("NETFLOW_QUEUE_MAX_RECORDS", "250000")
	cfg := loadConfig()
	if cfg.InsertTimeout != 30*time.Second || cfg.ShutdownTimeout != 15*time.Second || cfg.BatchSize != 1000 ||
		cfg.FlushInterval != 5*time.Second || cfg.SpoolMaxBytes != 1024<<20 || cfg.StatsInterval != 60*time.Second {
		t.Fatalf("invalid values not replaced by defaults: %+v", cfg)
	}
	if cfg.QueueMaxRecords != 250000 {
		t.Fatalf("valid value not applied: %d", cfg.QueueMaxRecords)
	}
}

// The allowlist is enforced whenever it is set: invalid entries are ignored,
// and a list without a single valid address accepts nothing rather than
// everything (BUG-07 must not fail open on a typo).
func TestAllowlistFailsClosed(t *testing.T) {
	if got := parseExporterAllowlist("  "); got != nil {
		t.Fatalf("blank allowlist = %v, want nil (allow all)", got)
	}
	c := newTestCollector(clockClamp)
	if !c.allowed(netip.MustParseAddr("192.0.2.1")) {
		t.Fatal("no allowlist must accept every exporter")
	}
	c.cfg.AllowedExporters = parseExporterAllowlist("10.0.0.300, router-1")
	if c.cfg.AllowedExporters == nil || c.allowed(netip.MustParseAddr("10.0.0.30")) {
		t.Fatal("an allowlist of typos must accept nothing")
	}
	c.cfg.AllowedExporters = parseExporterAllowlist("10.0.0.300, 192.0.2.7 ::ffff:192.0.2.8,2001:db8::1")
	for exporter, want := range map[string]bool{
		"192.0.2.7": true, "192.0.2.8": true, "::ffff:192.0.2.7": true, "2001:db8::1": true, "192.0.2.9": false,
	} {
		a := netip.MustParseAddr(exporter)
		if got := c.allowed(a.Unmap()); got != want {
			t.Errorf("allowed(%s) = %v, want %v", exporter, got, want)
		}
	}
}
