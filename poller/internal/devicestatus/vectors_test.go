package devicestatus

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"math"
	"os"
	"sort"
	"testing"
	"time"
)

// vectorsPath is the contract shared with the Python machine
// (scripts/dev/device-status-contract/build_vectors.py writes it), relative
// to this package.
const vectorsPath = "../../../scripts/dev/device-status-contract/vectors.json"

type vectorFile struct {
	Version     int            `json:"version"`
	GeneratedBy string         `json:"generated_by"`
	PolicyCases []policyVector `json:"policy_cases"`
	ReseedCases []reseedVector `json:"reseed_cases"`
	Cases       []caseVector   `json:"cases"`
}

type policyVector struct {
	Name            string         `json:"name"`
	Raw             any            `json:"raw"` // any JSON value: null and non-objects mean "no document"
	ExpectEffective map[string]any `json:"expect_effective"`
}

type reseedVector struct {
	Name            string         `json:"name"`
	Row             map[string]any `json:"row"`
	Owner           string         `json:"owner"`
	DevicesStatus   string         `json:"devices_status"`
	StatusChangedAt *string        `json:"status_changed_at"`
	ExpectReason    *string        `json:"expect_reason"`
	ExpectState     map[string]any `json:"expect_state"`
}

type seedVector struct {
	DevicesStatus   string  `json:"devices_status"`
	StatusChangedAt *string `json:"status_changed_at"`
	Owner           string  `json:"owner"`
}

type caseVector struct {
	Name      string           `json:"name"`
	PolicyRaw map[string]any   `json:"policy_raw"`
	Initial   map[string]any   `json:"initial"`
	Seed      *seedVector      `json:"seed"`
	Checks    []map[string]any `json:"checks"`
	Expect    []map[string]any `json:"expect"`
}

// stateFields are the persisted State keys every expectation states.
var stateFields = []string{
	"hard_status", "in_maintenance", "status_since", "fail_streak", "ok_streak", "clear_streak",
	"window_bits", "window_len", "reach_changes", "perf_changes", "reach_flapping", "perf_flapping",
	"flapping_since", "last_sample_at",
}

// TestSharedVectors replays the contract shared with Python: raw
// 'monitoring' documents to effective policies, the reseed rule, and check
// sequences to states and events (reason strings included). Every key is
// compared and an unknown key fails, so the contract cannot silently grow
// past the Go implementation.
func TestSharedVectors(t *testing.T) {
	data, err := os.ReadFile(vectorsPath)
	if errors.Is(err, fs.ErrNotExist) {
		t.Skipf("%s does not exist yet (written by the Python side of the contract); skipping the shared vectors", vectorsPath)
	}
	if err != nil {
		t.Fatal(err)
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber() // the loader's json.Number path, as a poller decoding with UseNumber would see it
	dec.DisallowUnknownFields()
	var vf vectorFile
	if err := dec.Decode(&vf); err != nil {
		t.Fatalf("decode %s: %v", vectorsPath, err)
	}
	if vf.Version != 1 {
		t.Fatalf("unsupported vectors version %d", vf.Version)
	}
	if len(vf.PolicyCases) == 0 || len(vf.ReseedCases) == 0 || len(vf.Cases) == 0 {
		t.Fatalf("empty vectors: %d policy, %d reseed, %d cases", len(vf.PolicyCases), len(vf.ReseedCases), len(vf.Cases))
	}

	for _, pc := range vf.PolicyCases {
		t.Run("policy/"+pc.Name, func(t *testing.T) {
			// The loader takes the decoded object; a document that is not an
			// object reaches it as no document at all.
			raw, _ := pc.Raw.(map[string]any)
			comparePolicy(t, PolicyFromSettings(raw, Defaults{}), pc.ExpectEffective)
		})
	}
	for _, rc := range vf.ReseedCases {
		t.Run("reseed/"+rc.Name, func(t *testing.T) {
			runReseedVector(t, rc)
		})
	}
	for _, vc := range vf.Cases {
		t.Run("case/"+vc.Name, func(t *testing.T) {
			runCaseVector(t, vc)
		})
	}
}

func runReseedVector(t *testing.T, rc reseedVector) {
	var row *State
	if rc.Row != nil {
		s := stateFromVector(t, "row", rc.Row)
		row = &s
	}
	reseed, reason := NeedsReseed(row, rc.DevicesStatus, rc.Owner)
	if rc.ExpectReason == nil {
		if reseed {
			t.Fatalf("NeedsReseed = true (%s), want keep", reason)
		}
		if rc.ExpectState != nil {
			t.Fatal("a kept row cannot expect a seeded state")
		}
		return
	}
	if !reseed || reason != *rc.ExpectReason {
		t.Fatalf("NeedsReseed = %v %q, want true %q", reseed, reason, *rc.ExpectReason)
	}
	seeded := Seed(rc.DevicesStatus, optTime(t, "status_changed_at", rc.StatusChangedAt), rc.Owner)
	compareStateVector(t, "seeded", seeded, rc.ExpectState, true)
}

func runCaseVector(t *testing.T, vc caseVector) {
	policy := PolicyFromSettings(vc.PolicyRaw, Defaults{})
	var s State
	switch {
	case vc.Initial != nil:
		s = stateFromVector(t, "initial", vc.Initial)
	case vc.Seed != nil:
		s = Seed(vc.Seed.DevicesStatus, optTime(t, "seed.status_changed_at", vc.Seed.StatusChangedAt), vc.Seed.Owner)
	default:
		t.Fatal("case without initial state or seed")
	}
	if len(vc.Checks) == 0 || len(vc.Checks) != len(vc.Expect) {
		t.Fatalf("%d checks, %d expectations", len(vc.Checks), len(vc.Expect))
	}
	for i, raw := range vc.Checks {
		label := fmt.Sprintf("check %d", i)
		c, override := checkFromVector(t, label, raw)
		if override != nil {
			policy = PolicyFromSettings(override, Defaults{})
		}
		var events []Event
		s, events = Step(s, c, policy)

		want := vc.Expect[i]
		compareStateVector(t, label, s, want, false)
		if got, exp := s.Displayed(), asString(t, label+" displayed", want["displayed"]); got != exp {
			t.Fatalf("%s displayed: got %q, want %q", label, got, exp)
		}
		comparePending(t, label, Pending(s, policy, c.Source), want["pending"])
		compareEvents(t, label, events, asList(t, label+" events", want["events"]))
	}
}

// checkFromVector decodes one check; policy_raw (if present) replaces the
// policy from this check on.
func checkFromVector(t *testing.T, label string, m map[string]any) (Check, map[string]any) {
	t.Helper()
	var c Check
	var override map[string]any
	for _, key := range keysOf(m) {
		v, l := m[key], label+"."+key
		switch key {
		case "ts":
			c.TS = asTime(t, l, v)
		case "responding":
			c.Responding = asBool(t, l, v)
		case "rtt_ms":
			if v != nil {
				c.RTTMs = asFloat(t, l, v)
			}
		case "loss":
			c.Loss = asFloat(t, l, v)
		case "interval_s":
			c.IntervalS = asFloat(t, l, v)
		case "maintenance":
			c.Maintenance = asBool(t, l, v)
		case "ordered":
			c.Ordered = asBool(t, l, v)
		case "source":
			c.Source = asString(t, l, v)
		case "policy_raw":
			override = asMap(t, l, v)
		default:
			t.Fatalf("%s: unknown check key", l)
		}
	}
	return c, override
}

func stateFromVector(t *testing.T, label string, m map[string]any) State {
	t.Helper()
	var s State
	for _, key := range keysOf(m) {
		v, l := m[key], label+"."+key
		switch key {
		case "owner":
			s.Owner = asString(t, l, v)
		case "hard_status":
			s.HardStatus = asString(t, l, v)
		case "in_maintenance":
			s.InMaintenance = asBool(t, l, v)
		case "status_since":
			s.StatusSince = asTimePtr(t, l, v)
		case "fail_streak":
			s.FailStreak = asInt(t, l, v)
		case "ok_streak":
			s.OkStreak = asInt(t, l, v)
		case "clear_streak":
			s.ClearStreak = asInt(t, l, v)
		case "window_bits":
			s.WindowBits = asInt(t, l, v)
		case "window_len":
			s.WindowLen = asInt(t, l, v)
		case "reach_changes":
			s.ReachChanges = asTimes(t, l, v)
		case "perf_changes":
			s.PerfChanges = asTimes(t, l, v)
		case "reach_flapping":
			s.ReachFlapping = asBool(t, l, v)
		case "perf_flapping":
			s.PerfFlapping = asBool(t, l, v)
		case "flapping_since":
			s.FlappingSince = asTimePtr(t, l, v)
		case "last_sample_at":
			s.LastSampleAt = asTimePtr(t, l, v)
		default:
			t.Fatalf("%s: unknown state key", l)
		}
	}
	return s
}

// compareStateVector compares every persisted field. want may also hold the
// per-check keys "displayed", "pending" and "events" (checked by the caller)
// and, for a seeded state, "owner".
func compareStateVector(t *testing.T, label string, s State, want map[string]any, withOwner bool) {
	t.Helper()
	known := map[string]bool{"displayed": true, "pending": true, "events": true}
	if withOwner {
		known["owner"] = true
		if got, exp := s.Owner, asString(t, label+" owner", want["owner"]); got != exp {
			t.Fatalf("%s owner: got %q, want %q", label, got, exp)
		}
	}
	for _, key := range stateFields {
		known[key] = true
		v, ok := want[key]
		if !ok {
			t.Fatalf("%s: expectation without %q", label, key)
		}
		l := label + " " + key
		var got, exp any
		switch key {
		case "hard_status":
			got, exp = s.HardStatus, asString(t, l, v)
		case "in_maintenance":
			got, exp = s.InMaintenance, asBool(t, l, v)
		case "fail_streak":
			got, exp = s.FailStreak, asInt(t, l, v)
		case "ok_streak":
			got, exp = s.OkStreak, asInt(t, l, v)
		case "clear_streak":
			got, exp = s.ClearStreak, asInt(t, l, v)
		case "window_bits":
			got, exp = s.WindowBits, asInt(t, l, v)
		case "window_len":
			got, exp = s.WindowLen, asInt(t, l, v)
		case "reach_flapping":
			got, exp = s.ReachFlapping, asBool(t, l, v)
		case "perf_flapping":
			got, exp = s.PerfFlapping, asBool(t, l, v)
		case "status_since":
			compareTimePtr(t, l, s.StatusSince, v)
			continue
		case "flapping_since":
			compareTimePtr(t, l, s.FlappingSince, v)
			continue
		case "last_sample_at":
			compareTimePtr(t, l, s.LastSampleAt, v)
			continue
		case "reach_changes":
			compareTimes(t, l, s.ReachChanges, asTimes(t, l, v))
			continue
		case "perf_changes":
			compareTimes(t, l, s.PerfChanges, asTimes(t, l, v))
			continue
		}
		if got != exp {
			t.Fatalf("%s: got %v, want %v (state %s)", l, got, exp, describe(s))
		}
	}
	for key := range want {
		if !known[key] {
			t.Fatalf("%s: unknown expectation key %q", label, key)
		}
	}
}

func compareEvents(t *testing.T, label string, got []Event, want []any) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s: got %d events %+v, want %d %v", label, len(got), got, len(want), want)
	}
	for i, raw := range want {
		l := fmt.Sprintf("%s event %d", label, i)
		w := asMap(t, l, raw)
		g := got[i]
		exp := Event{
			Type:      asString(t, l+" type", w["type"]),
			Old:       optString(t, l+" old", w["old"]),
			New:       optString(t, l+" new", w["new"]),
			Reason:    optString(t, l+" reason", w["reason"]),
			Alert:     asBool(t, l+" alert", w["alert"]),
			Flapping:  asBool(t, l+" flapping", w["flapping"]),
			Kind:      optString(t, l+" kind", w["kind"]),
			EndReason: optString(t, l+" end_reason", w["end_reason"]),
		}
		if len(w) != 8 {
			t.Fatalf("%s: unexpected event keys %v", l, keysOf(w))
		}
		if g != exp {
			t.Fatalf("%s:\n got %+v\nwant %+v", l, g, exp)
		}
	}
}

// comparePending compares Pending (with the check's source) against the
// expectation: null, or {target, count, required}.
func comparePending(t *testing.T, label string, got *PendingChange, raw any) {
	t.Helper()
	l := label + " pending"
	if raw == nil {
		if got != nil {
			t.Fatalf("%s: got %+v, want null", l, *got)
		}
		return
	}
	w := asMap(t, l, raw)
	if len(w) != 3 {
		t.Fatalf("%s: unexpected keys %v", l, keysOf(w))
	}
	exp := PendingChange{
		Target:   asString(t, l+" target", w["target"]),
		Count:    asInt(t, l+" count", w["count"]),
		Required: asInt(t, l+" required", w["required"]),
	}
	if got == nil || *got != exp {
		t.Fatalf("%s: got %+v, want %+v", l, got, exp)
	}
}

func comparePolicy(t *testing.T, got Policy, want map[string]any) {
	t.Helper()
	encoded, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	var have map[string]any
	if err := json.Unmarshal(encoded, &have); err != nil {
		t.Fatal(err)
	}
	if len(have) != len(want) {
		t.Fatalf("effective policy keys: got %v, want %v", keysOf(have), keysOf(want))
	}
	for _, key := range keysOf(want) {
		h, ok := have[key]
		if !ok {
			t.Fatalf("unknown effective policy key %q", key)
		}
		if w, isBool := want[key].(bool); isBool {
			if h != w {
				t.Fatalf("effective %s: got %v, want %v", key, h, w)
			}
			continue
		}
		if hf, wf := h.(float64), asFloat(t, key, want[key]); hf != wf {
			t.Fatalf("effective %s: got %v, want %v (policy %+v)", key, hf, wf, got)
		}
	}
}

func describe(s State) string {
	b, _ := json.Marshal(s)
	return string(b)
}

func compareTimePtr(t *testing.T, label string, got *time.Time, v any) {
	t.Helper()
	want := asTimePtr(t, label, v)
	switch {
	case got == nil && want == nil:
	case got == nil || want == nil:
		t.Fatalf("%s: got %v, want %v", label, got, want)
	case !got.Equal(*want):
		t.Fatalf("%s: got %s, want %s", label, got.UTC().Format(time.RFC3339Nano), want.UTC().Format(time.RFC3339Nano))
	}
}

func compareTimes(t *testing.T, label string, got, want []time.Time) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%s: got %d %v, want %d %v", label, len(got), got, len(want), want)
	}
	for i := range want {
		if !got[i].Equal(want[i]) {
			t.Fatalf("%s[%d]: got %v, want %v", label, i, got[i], want[i])
		}
	}
}

func keysOf(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func asMap(t *testing.T, label string, v any) map[string]any {
	t.Helper()
	m, ok := v.(map[string]any)
	if !ok {
		t.Fatalf("%s: want an object, got %T", label, v)
	}
	return m
}

func asList(t *testing.T, label string, v any) []any {
	t.Helper()
	l, ok := v.([]any)
	if !ok {
		t.Fatalf("%s: want a list, got %T", label, v)
	}
	return l
}

func asString(t *testing.T, label string, v any) string {
	t.Helper()
	s, ok := v.(string)
	if !ok {
		t.Fatalf("%s: want a string, got %T (%v)", label, v, v)
	}
	return s
}

// optString maps JSON null to "" (a field the event does not carry).
func optString(t *testing.T, label string, v any) string {
	t.Helper()
	if v == nil {
		return ""
	}
	return asString(t, label, v)
}

func asBool(t *testing.T, label string, v any) bool {
	t.Helper()
	b, ok := v.(bool)
	if !ok {
		t.Fatalf("%s: want a bool, got %T (%v)", label, v, v)
	}
	return b
}

func asFloat(t *testing.T, label string, v any) float64 {
	t.Helper()
	f, ok := numberOf(v)
	if !ok {
		t.Fatalf("%s: want a number, got %T (%v)", label, v, v)
	}
	return f
}

func asInt(t *testing.T, label string, v any) int {
	t.Helper()
	f := asFloat(t, label, v)
	if f != math.Trunc(f) {
		t.Fatalf("%s: want an integer, got %v", label, f)
	}
	return int(f)
}

// vectorTimeLayouts accept RFC 3339 with 'Z' or a numeric offset such as
// Python's '+00:00' (time.RFC3339Nano parses both), a space separator, and a
// value without a zone as UTC.
var vectorTimeLayouts = []string{
	time.RFC3339Nano,
	"2006-01-02 15:04:05.999999999Z07:00",
	"2006-01-02T15:04:05.999999999",
	"2006-01-02 15:04:05.999999999",
}

func asTime(t *testing.T, label string, v any) time.Time {
	t.Helper()
	s := asString(t, label, v)
	for _, layout := range vectorTimeLayouts {
		if ts, err := time.Parse(layout, s); err == nil {
			return ts
		}
	}
	t.Fatalf("%s: unparseable timestamp %q", label, s)
	return time.Time{}
}

func asTimePtr(t *testing.T, label string, v any) *time.Time {
	t.Helper()
	if v == nil {
		return nil
	}
	ts := asTime(t, label, v)
	return &ts
}

func optTime(t *testing.T, label string, v *string) *time.Time {
	t.Helper()
	if v == nil {
		return nil
	}
	return asTimePtr(t, label, *v)
}

func asTimes(t *testing.T, label string, v any) []time.Time {
	t.Helper()
	out := []time.Time{}
	for i, item := range asList(t, label, v) {
		out = append(out, asTime(t, fmt.Sprintf("%s[%d]", label, i), item))
	}
	return out
}

func TestVectorTimestampFormats(t *testing.T) {
	want := time.Date(2026, 10, 14, 10, 0, 0, 500000000, time.UTC)
	for _, s := range []string{
		"2026-10-14T10:00:00.5Z",
		"2026-10-14T10:00:00.500000+00:00",
		"2026-10-14 10:00:00.5+00:00",
		"2026-10-14T12:00:00.5+02:00",
		"2026-10-14T10:00:00.5",
	} {
		if got := asTime(t, s, s); !got.Equal(want) {
			t.Errorf("%s parsed as %v", s, got)
		}
	}
}
