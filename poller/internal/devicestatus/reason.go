package devicestatus

import (
	"fmt"
	"strconv"
	"strings"
)

// Class is the classification of one check against the policy limits.
type Class string

const (
	// ClassFail is a check without any reply.
	ClassFail Class = "fail"
	// ClassBreach is a responding check over a degraded limit.
	ClassBreach Class = "breach"
	// ClassClear is a responding check inside both recovery limits.
	ClassClear Class = "clear"
	// ClassMarginal is a responding check inside the hysteresis band (over a
	// recovery limit but not over a degraded limit).
	ClassMarginal Class = "marginal"
)

// ExceedsMs is the canonical latency predicate shared with Python. The
// tolerance absorbs representation error at the limit.
func ExceedsMs(v, limit float64) bool {
	return (v - limit) > 1e-6
}

// ExceedsPct is the canonical loss predicate shared with Python: lossRatio is
// a fraction (0..1), limitPct a percentage. The explicit float64 conversion
// stops the compiler from fusing the multiply and subtract into an FMA, which
// would round differently from Python. The tolerance keeps float32(.05) at 5 %
// and float32(.1) at 10 % within limits, as the poller always treated them.
func ExceedsPct(lossRatio, limitPct float64) bool {
	return (float64(lossRatio*100) - limitPct) > 1e-4
}

// Classify classifies one check (ClassFail when it got no reply).
func Classify(c Check, p Policy) Class {
	switch {
	case !c.Responding:
		return ClassFail
	case ExceedsMs(c.RTTMs, p.DegradedRTTMs) || ExceedsPct(c.Loss, p.DegradedLossPct):
		return ClassBreach
	case !ExceedsMs(c.RTTMs, p.DegradedRTTClearMs) && !ExceedsPct(c.Loss, p.DegradedLossClearPct):
		return ClassClear
	default:
		return ClassMarginal
	}
}

// Fmt3 formats a configured number with 3 fixed decimals and trims trailing
// zeros and the dot: 500, 5.6, 1234.568.
func Fmt3(x float64) string {
	s := strconv.FormatFloat(x, 'f', 3, 64)
	s = strings.TrimRight(s, "0")
	return strings.TrimRight(s, ".")
}

// DegradedReason explains why one sample is over the degraded limits. loss
// is a fraction (0..1); rttLimit is in ms, lossLimitPct a percentage.
func DegradedReason(rttMs, loss, rttLimit, lossLimitPct float64) string {
	parts := make([]string, 0, 2)
	if ExceedsMs(rttMs, rttLimit) {
		parts = append(parts, fmt.Sprintf("Latency %.3f ms exceeds %s ms by %.3f ms",
			rttMs, Fmt3(rttLimit), rttMs-rttLimit))
	}
	if ExceedsPct(loss, lossLimitPct) {
		lossPct := float64(loss * 100)
		parts = append(parts, fmt.Sprintf("Packet loss %.2f%% exceeds %s%% by %.2f percentage points",
			lossPct, Fmt3(lossLimitPct), lossPct-lossLimitPct))
	}
	if len(parts) == 0 {
		return "High latency or packet loss"
	}
	return strings.Join(parts, "; ")
}

// Contracted reason texts (DESIGN.md "Reasons").
const (
	reasonMaintenanceActive = "Maintenance window active"
	reasonUnknownUp         = "Device responding normally"
	reasonRespondingAgain   = "Responding again"
	reasonMaintenanceUp     = "Maintenance ended; device responding normally"
	reasonMaintenanceDeg    = "Maintenance ended; latency or loss above limits"
)

func reasonDown(failStreak int) string {
	return fmt.Sprintf("No response for %d consecutive checks", failStreak)
}

func reasonDownRecovered(p Policy, okStreak int) string {
	if p.UpConfirmChecks == 1 {
		return reasonRespondingAgain
	}
	return fmt.Sprintf("Responding for %d consecutive checks", okStreak)
}

func reasonWindowDegraded(c Check, p Policy, breach bool, bad, eff int) string {
	evidence := fmt.Sprintf("%d of the last %d checks over limits or unanswered", bad, eff)
	if breach {
		return checkDegradedReason(c, p) + "; " + evidence
	}
	return evidence + "; latest check within limits"
}

func reasonClearStreak(p Policy, clearStreak int) string {
	return fmt.Sprintf("Within recovery limits (%s ms, %s%%) for %d consecutive checks",
		Fmt3(p.DegradedRTTClearMs), Fmt3(p.DegradedLossClearPct), clearStreak)
}

func reasonCleanWindow(eff int) string {
	return fmt.Sprintf("No slow, lossy or unanswered checks in the last %d checks", eff)
}

// reasonMaintenanceDown explains a maintenance exit into DOWN. A device that
// answers but has not confirmed its recovery yet (up_confirm_checks > 1) has
// no failure streak, so its progress is reported instead.
func reasonMaintenanceDown(p Policy, failStreak, okStreak int) string {
	if failStreak > 0 {
		return fmt.Sprintf("Maintenance ended; no response for %d consecutive checks", failStreak)
	}
	return fmt.Sprintf("Maintenance ended; responding for %d of %d checks needed to confirm recovery",
		okStreak, p.UpConfirmChecks)
}

func checkDegradedReason(c Check, p Policy) string {
	return DegradedReason(c.RTTMs, c.Loss, p.DegradedRTTMs, p.DegradedLossPct)
}
