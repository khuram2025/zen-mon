package devicestatus

import (
	"encoding/json"
	"math"
	"strconv"
)

// Policy is the effective device status policy (system_settings key
// 'monitoring'). Build it with PolicyFromSettings: Step assumes the loader's
// ranges and cross-field rules hold.
type Policy struct {
	DegradedRTTMs           float64 `json:"degraded_rtt_ms"`
	DegradedLossPct         float64 `json:"degraded_loss_pct"`
	DegradedRTTClearMs      float64 `json:"degraded_rtt_clear_ms"`
	DegradedLossClearPct    float64 `json:"degraded_loss_clear_pct"`
	DegradedConfirmChecks   int     `json:"degraded_confirm_checks"`
	DegradedWindowChecks    int     `json:"degraded_window_checks"`
	DegradedClearChecks     int     `json:"degraded_clear_checks"`
	DownConfirmChecks       int     `json:"down_confirm_checks"`
	SensorDownConfirmChecks int     `json:"sensor_down_confirm_checks"`
	UpConfirmChecks         int     `json:"up_confirm_checks"`
	FlapDetectionEnabled    bool    `json:"flap_detection_enabled"`
	FlapWindowMinutes       int     `json:"flap_window_minutes"`
	FlapStartChanges        int     `json:"flap_start_changes"`
	FlapStopChanges         int     `json:"flap_stop_changes"`
	FlapStableMinutes       int     `json:"flap_stable_minutes"`
}

// Defaults are the fallbacks used when the 'monitoring' document does not set
// a value: the poller passes its config (DegradedRTTMs, DegradedLossPct,
// DownThreshold) so deployments that tuned those keep working. Zero or
// negative fields fall back to the built-in defaults (100 ms, 10 %, 3).
type Defaults struct {
	DegradedRTTMs     float64
	DegradedLossPct   float64
	DownConfirmChecks int
}

// Built-in defaults and loader ranges (DESIGN.md "Policy").
const (
	defaultRTTMs           = 100.0
	defaultLossPct         = 10.0
	defaultConfirmChecks   = 3
	defaultWindowChecks    = 5
	defaultClearChecks     = 3
	defaultDownConfirm     = 3
	defaultSensorDown      = 1
	defaultUpConfirm       = 1
	defaultFlapWindowMin   = 30
	defaultFlapStart       = 4
	defaultFlapStop        = 1
	defaultFlapStableMin   = 10
	maxRTTMs               = 10000.0
	minLossPct, maxLossPct = 1.0, 100.0
	minCount, maxCount     = 1, 20
	maxConfirm             = 10
	minFlapWindowMin       = 5
	maxFlapWindowMin       = 1440
	minFlapStart           = 3
	maxFlapStart           = 64
	clearFactor            = 0.8
)

// SourceSensor marks a remote-sensor check (Check.Source); every other source
// uses the central down confirmation.
const SourceSensor = "sensor"

// DefaultPolicy is the effective policy for an empty 'monitoring' document
// and built-in defaults.
func DefaultPolicy() Policy {
	return PolicyFromSettings(nil, Defaults{})
}

// DownConfirm is the number of consecutive failed checks that confirm DOWN
// for a check from source.
func (p Policy) DownConfirm(source string) int {
	if source == SourceSensor {
		return p.SensorDownConfirmChecks
	}
	return p.DownConfirmChecks
}

// PolicyFromSettings is the loader shared with Python: each key is parsed on
// its own (only JSON numbers count, integer keys only integral numbers; any
// other value means "use the default"), out-of-range values are clamped, and
// the cross-field rules are applied in the contract order. raw may come from
// encoding/json with or without UseNumber.
func PolicyFromSettings(raw map[string]any, defaults Defaults) Policy {
	var p Policy

	rttDefault := defaultRTTMs
	if isFinite(defaults.DegradedRTTMs) && defaults.DegradedRTTMs > 0 {
		rttDefault = defaults.DegradedRTTMs
	}
	lossDefault := defaultLossPct
	if isFinite(defaults.DegradedLossPct) && defaults.DegradedLossPct > 0 {
		lossDefault = defaults.DegradedLossPct
	}
	downDefault := defaultDownConfirm
	if defaults.DownConfirmChecks > 0 {
		downDefault = defaults.DownConfirmChecks
	}

	// rtt: (0, 10000]. The open lower bound cannot be clamped to, so a
	// non-positive value means "unset" (as the poller has always treated it).
	p.DegradedRTTMs = math.Min(positiveField(raw, "degraded_rtt_ms", rttDefault), maxRTTMs)
	// loss: [1, 100].
	p.DegradedLossPct = clampFloat(numberField(raw, "degraded_loss_pct", lossDefault), minLossPct, maxLossPct)
	// rtt_clear: (0, rtt]; unset derives 0.8 x rtt rounded to 3 decimals.
	rttClear := positiveField(raw, "degraded_rtt_clear_ms", round3(float64(clearFactor*p.DegradedRTTMs)))
	p.DegradedRTTClearMs = math.Min(math.Min(rttClear, maxRTTMs), p.DegradedRTTMs)
	// loss_clear: [0, loss]; unset derives 0.8 x loss rounded to 3 decimals.
	lossClear := numberField(raw, "degraded_loss_clear_pct", round3(float64(clearFactor*p.DegradedLossPct)))
	p.DegradedLossClearPct = math.Min(clampFloat(lossClear, 0, maxLossPct), p.DegradedLossPct)

	p.DegradedConfirmChecks = intField(raw, "degraded_confirm_checks", defaultConfirmChecks, minCount, maxCount)
	p.DegradedWindowChecks = max(intField(raw, "degraded_window_checks", defaultWindowChecks, minCount, maxCount), p.DegradedConfirmChecks)
	p.DegradedClearChecks = intField(raw, "degraded_clear_checks", defaultClearChecks, minCount, maxCount)
	p.DownConfirmChecks = intField(raw, "down_confirm_checks", downDefault, 1, maxConfirm)
	p.SensorDownConfirmChecks = intField(raw, "sensor_down_confirm_checks", defaultSensorDown, 1, maxConfirm)
	p.UpConfirmChecks = intField(raw, "up_confirm_checks", defaultUpConfirm, 1, maxConfirm)

	p.FlapDetectionEnabled = true
	if v, ok := raw["flap_detection_enabled"].(bool); ok {
		p.FlapDetectionEnabled = v
	}
	p.FlapWindowMinutes = intField(raw, "flap_window_minutes", defaultFlapWindowMin, minFlapWindowMin, maxFlapWindowMin)
	p.FlapStartChanges = intField(raw, "flap_start_changes", defaultFlapStart, minFlapStart, maxFlapStart)
	p.FlapStopChanges = min(intField(raw, "flap_stop_changes", defaultFlapStop, 0, maxFlapStart-1), p.FlapStartChanges-1)
	p.FlapStableMinutes = min(intField(raw, "flap_stable_minutes", defaultFlapStableMin, 1, maxFlapWindowMin), p.FlapWindowMinutes)
	return p
}

// numberOf returns v as a float64 when it is a finite JSON number. Booleans,
// strings and null are not numbers.
func numberOf(v any) (float64, bool) {
	var f float64
	switch n := v.(type) {
	case float64:
		f = n
	case float32:
		f = float64(n)
	case int:
		f = float64(n)
	case int8:
		f = float64(n)
	case int16:
		f = float64(n)
	case int32:
		f = float64(n)
	case int64:
		f = float64(n)
	case uint:
		f = float64(n)
	case uint8:
		f = float64(n)
	case uint16:
		f = float64(n)
	case uint32:
		f = float64(n)
	case uint64:
		f = float64(n)
	case json.Number:
		parsed, err := n.Float64()
		if err != nil {
			return 0, false
		}
		f = parsed
	default:
		return 0, false
	}
	if !isFinite(f) {
		return 0, false
	}
	return f, true
}

// numberField returns raw[key] when it is a JSON number, else def.
func numberField(raw map[string]any, key string, def float64) float64 {
	if f, ok := numberOf(raw[key]); ok {
		return f
	}
	return def
}

// positiveField is numberField for keys whose range is open at zero: a value
// <= 0 cannot be clamped into (0, max] and falls back to def.
func positiveField(raw map[string]any, key string, def float64) float64 {
	if f, ok := numberOf(raw[key]); ok && f > 0 {
		return f
	}
	return def
}

// intField returns raw[key] clamped to [lo, hi] when it is an integral JSON
// number; otherwise def (also clamped, so config fallbacks obey the range).
func intField(raw map[string]any, key string, def, lo, hi int) int {
	f, ok := numberOf(raw[key])
	if !ok || f != math.Trunc(f) {
		return min(max(def, lo), hi)
	}
	// Clamp in float64 first: an integral value beyond the int range must
	// not overflow the conversion.
	return int(clampFloat(f, float64(lo), float64(hi)))
}

func clampFloat(f, lo, hi float64) float64 {
	return math.Min(math.Max(f, lo), hi)
}

func isFinite(f float64) bool {
	return !math.IsNaN(f) && !math.IsInf(f, 0)
}

// round3 rounds to 3 decimals exactly like Python's round(x, 3): the
// correctly rounded decimal of the binary value (ties to even), not
// math.Round(x*1000)/1000, whose scaling adds its own rounding error.
func round3(x float64) float64 {
	f, err := strconv.ParseFloat(strconv.FormatFloat(x, 'f', 3, 64), 64)
	if err != nil {
		return x
	}
	return f
}
