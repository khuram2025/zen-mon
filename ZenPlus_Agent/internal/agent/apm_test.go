package agent

import (
	"context"
	"strings"
	"testing"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/enroll"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/runtime"
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

func TestExecuteCommandRejectsInstrumentationWhenAPMDisabled(t *testing.T) {
	cfg := config.Default()
	if err := config.ApplyProfile(&cfg, "infrastructure"); err != nil {
		t.Fatal(err)
	}
	for _, command := range []string{"apm_instrument", "apm_restart_target"} {
		t.Run(command, func(t *testing.T) {
			result := executeCommand(
				context.Background(), model.Command{Command: command}, &cfg,
				nil, runtime.Paths{}, nil, nil, nil, enroll.Result{}, nil, func(string, ...any) {},
			)
			if result.Success || !strings.Contains(result.ErrorMessage, "APM disabled") {
				t.Fatalf("unexpected result: %#v", result)
			}
		})
	}
}

func TestCapabilitiesAdvertiseInstrumentationOnlyWhenUsable(t *testing.T) {
	cfg := config.Default()
	privileged := capabilitiesForConfig(cfg, true)
	if !containsCapability(privileged, "apm_iis_instrumentation_v1") || !containsCapability(privileged, "apm_windows_service_instrumentation_v1") {
		t.Fatalf("privileged combined profile is missing instrumentation capabilities: %v", privileged)
	}
	for name, capabilities := range map[string][]string{
		"unprivileged": capabilitiesForConfig(cfg, false),
		"disabled": func() []string {
			disabled := cfg
			_ = config.ApplyProfile(&disabled, "infrastructure")
			return capabilitiesForConfig(disabled, true)
		}(),
	} {
		if containsCapability(capabilities, "apm_iis_instrumentation_v1") || containsCapability(capabilities, "apm_windows_service_instrumentation_v1") {
			t.Fatalf("%s agent advertised unusable instrumentation: %v", name, capabilities)
		}
		if !containsCapability(capabilities, "apm_status_v1") {
			t.Fatalf("%s agent lost read-only APM status capability: %v", name, capabilities)
		}
	}
}

func containsCapability(capabilities []string, wanted string) bool {
	for _, capability := range capabilities {
		if capability == wanted {
			return true
		}
	}
	return false
}
