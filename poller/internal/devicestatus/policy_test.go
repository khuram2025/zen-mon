package devicestatus

import (
	"encoding/json"
	"strings"
	"testing"
)

func decodeSettings(t *testing.T, doc string, useNumber bool) map[string]any {
	t.Helper()
	dec := json.NewDecoder(strings.NewReader(doc))
	if useNumber {
		dec.UseNumber()
	}
	var raw map[string]any
	if err := dec.Decode(&raw); err != nil {
		t.Fatalf("decode %s: %v", doc, err)
	}
	return raw
}

func TestDefaultPolicy(t *testing.T) {
	want := Policy{
		DegradedRTTMs:           100,
		DegradedLossPct:         10,
		DegradedRTTClearMs:      80,
		DegradedLossClearPct:    8,
		DegradedConfirmChecks:   3,
		DegradedWindowChecks:    5,
		DegradedClearChecks:     3,
		DownConfirmChecks:       3,
		SensorDownConfirmChecks: 1,
		UpConfirmChecks:         1,
		FlapDetectionEnabled:    true,
		FlapWindowMinutes:       30,
		FlapStartChanges:        4,
		FlapStopChanges:         1,
		FlapStableMinutes:       10,
	}
	if got := DefaultPolicy(); got != want {
		t.Fatalf("default policy\n got %+v\nwant %+v", got, want)
	}
	if got := PolicyFromSettings(map[string]any{}, Defaults{}); got != want {
		t.Fatalf("empty document\n got %+v\nwant %+v", got, want)
	}
}

func TestPolicyFromSettingsLoaderRules(t *testing.T) {
	base := DefaultPolicy()
	for _, tc := range []struct {
		name     string
		doc      string
		defaults Defaults
		edit     func(p *Policy)
	}{
		{"config defaults feed unset keys", `{}`, Defaults{DegradedRTTMs: 250, DegradedLossPct: 20, DownConfirmChecks: 5}, func(p *Policy) {
			p.DegradedRTTMs, p.DegradedLossPct, p.DownConfirmChecks = 250, 20, 5
			p.DegradedRTTClearMs, p.DegradedLossClearPct = 200, 16
		}},
		{"stored values win over config defaults", `{"degraded_rtt_ms": 500, "degraded_loss_pct": 5, "down_confirm_checks": 2}`, Defaults{DegradedRTTMs: 250, DegradedLossPct: 20, DownConfirmChecks: 5}, func(p *Policy) {
			p.DegradedRTTMs, p.DegradedLossPct, p.DownConfirmChecks = 500, 5, 2
			p.DegradedRTTClearMs, p.DegradedLossClearPct = 400, 4
		}},
		{"out-of-range config defaults are clamped", `{}`, Defaults{DegradedRTTMs: 50000, DegradedLossPct: 0.5, DownConfirmChecks: 40}, func(p *Policy) {
			p.DegradedRTTMs, p.DegradedLossPct, p.DownConfirmChecks = 10000, 1, 10
			p.DegradedRTTClearMs, p.DegradedLossClearPct = 8000, 0.8
		}},
		{"derived clear limits rounded to 3 decimals", `{"degraded_rtt_ms": 123.4567, "degraded_loss_pct": 3.3333}`, Defaults{}, func(p *Policy) {
			p.DegradedRTTMs, p.DegradedLossPct = 123.4567, 3.3333
			p.DegradedRTTClearMs, p.DegradedLossClearPct = 98.765, 2.667
		}},
		{"null and wrong types fall back", `{"degraded_rtt_ms": null, "degraded_loss_pct": "20", "degraded_confirm_checks": true, "flap_detection_enabled": 0, "degraded_rtt_clear_ms": null}`, Defaults{}, func(p *Policy) {}},
		{"integer keys take only integral numbers", `{"degraded_confirm_checks": 2.5, "degraded_clear_checks": 4.0, "flap_window_minutes": 60}`, Defaults{}, func(p *Policy) {
			p.DegradedClearChecks, p.FlapWindowMinutes = 4, 60
		}},
		{"ranges clamp", `{"degraded_rtt_ms": 20000, "degraded_loss_pct": 0, "degraded_confirm_checks": 0, "degraded_window_checks": 99, "degraded_clear_checks": -3, "down_confirm_checks": 11, "sensor_down_confirm_checks": 0, "up_confirm_checks": 1e9, "flap_window_minutes": 1, "flap_start_changes": 100, "flap_stop_changes": -1, "flap_stable_minutes": 0}`, Defaults{}, func(p *Policy) {
			p.DegradedRTTMs, p.DegradedLossPct = 10000, 1
			p.DegradedRTTClearMs, p.DegradedLossClearPct = 8000, 0.8
			p.DegradedConfirmChecks, p.DegradedWindowChecks, p.DegradedClearChecks = 1, 20, 1
			p.DownConfirmChecks, p.SensorDownConfirmChecks, p.UpConfirmChecks = 10, 1, 10
			p.FlapWindowMinutes, p.FlapStartChanges, p.FlapStopChanges, p.FlapStableMinutes = 5, 64, 0, 1
		}},
		{"non-positive rtt means unset", `{"degraded_rtt_ms": 0, "degraded_rtt_clear_ms": -5}`, Defaults{DegradedRTTMs: 300}, func(p *Policy) {
			p.DegradedRTTMs, p.DegradedRTTClearMs = 300, 240
		}},
		{"clear limits never exceed the degraded limits", `{"degraded_rtt_ms": 200, "degraded_rtt_clear_ms": 500, "degraded_loss_pct": 5, "degraded_loss_clear_pct": 50}`, Defaults{}, func(p *Policy) {
			p.DegradedRTTMs, p.DegradedRTTClearMs, p.DegradedLossPct, p.DegradedLossClearPct = 200, 200, 5, 5
		}},
		{"explicit clear limits kept, loss clear may be zero", `{"degraded_rtt_clear_ms": 60.5, "degraded_loss_clear_pct": -2}`, Defaults{}, func(p *Policy) {
			p.DegradedRTTClearMs, p.DegradedLossClearPct = 60.5, 0
		}},
		{"window raised to confirm", `{"degraded_confirm_checks": 7, "degraded_window_checks": 4}`, Defaults{}, func(p *Policy) {
			p.DegradedConfirmChecks, p.DegradedWindowChecks = 7, 7
		}},
		{"stop below start", `{"flap_start_changes": 3, "flap_stop_changes": 5}`, Defaults{}, func(p *Policy) {
			p.FlapStartChanges, p.FlapStopChanges = 3, 2
		}},
		{"stable within the flap window", `{"flap_window_minutes": 8, "flap_stable_minutes": 20}`, Defaults{}, func(p *Policy) {
			p.FlapWindowMinutes, p.FlapStableMinutes = 8, 8
		}},
		{"legacy preset", `{"degraded_confirm_checks": 1, "degraded_window_checks": 1, "degraded_clear_checks": 1, "degraded_rtt_clear_ms": 100, "degraded_loss_clear_pct": 10, "flap_detection_enabled": false}`, Defaults{}, func(p *Policy) {
			p.DegradedConfirmChecks, p.DegradedWindowChecks, p.DegradedClearChecks = 1, 1, 1
			p.DegradedRTTClearMs, p.DegradedLossClearPct = 100, 10
			p.FlapDetectionEnabled = false
		}},
	} {
		for _, useNumber := range []bool{false, true} {
			name := tc.name
			if useNumber {
				name += "/json.Number"
			}
			t.Run(name, func(t *testing.T) {
				want := base
				if tc.defaults != (Defaults{}) {
					want = PolicyFromSettings(nil, tc.defaults)
				}
				tc.edit(&want)
				got := PolicyFromSettings(decodeSettings(t, tc.doc, useNumber), tc.defaults)
				if got != want {
					t.Fatalf("\n got %+v\nwant %+v", got, want)
				}
			})
		}
	}
}

func TestPolicyFromSettingsGoNumbers(t *testing.T) {
	got := PolicyFromSettings(map[string]any{
		"degraded_rtt_ms":         int64(250),
		"degraded_confirm_checks": int32(2),
		"degraded_window_checks":  float32(4),
		"degraded_clear_checks":   uint8(2),
		"flap_stable_minutes":     json.Number("1e400"),
	}, Defaults{})
	if got.DegradedRTTMs != 250 || got.DegradedConfirmChecks != 2 || got.DegradedWindowChecks != 4 || got.DegradedClearChecks != 2 {
		t.Fatalf("Go numeric types not accepted: %+v", got)
	}
	if got.FlapStableMinutes != 10 {
		t.Fatalf("a JSON number beyond float64 must fall back to the default, got %d", got.FlapStableMinutes)
	}
}

func TestDownConfirmBySource(t *testing.T) {
	p := PolicyFromSettings(map[string]any{"down_confirm_checks": 4.0, "sensor_down_confirm_checks": 2.0}, Defaults{})
	if p.DownConfirm(SourceSensor) != 2 || p.DownConfirm("central") != 4 || p.DownConfirm("") != 4 {
		t.Fatalf("down confirm by source: %+v", p)
	}
}

func TestRound3MatchesPythonRound(t *testing.T) {
	for _, tc := range []struct {
		in, want float64
	}{
		{80.00000000000001, 80},
		{0.8 * 3.3333, 2.667},
		{2.675, 2.675},
		{1.0005, 1.0},   // binary value is just below the tie
		{0.0625, 0.062}, // exact binary tie rounds to even
		{0.1875, 0.188}, // exact binary tie rounds to even
		{98.76536, 98.765},
	} {
		if got := round3(tc.in); got != tc.want {
			t.Errorf("round3(%v) = %v, want %v", tc.in, got, tc.want)
		}
	}
}
