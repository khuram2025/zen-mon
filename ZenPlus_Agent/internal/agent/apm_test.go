package agent

import (
	"testing"

	"zenplus-agent/internal/config"
)

func TestProbeLocalAPMRespectsDisabledSetting(t *testing.T) {
	cfg := config.Default()
	cfg.APM.Enabled = false

	status := probeLocalAPM(cfg)
	if status.Enabled || status.Gateway.Listening {
		t.Fatalf("disabled APM must not probe or report a listener: %#v", status)
	}
	if status.Gateway.GRPCPort != 4317 || status.Gateway.HTTPPort != 4318 {
		t.Fatalf("standard OTLP ports missing from status: %#v", status.Gateway)
	}
}
