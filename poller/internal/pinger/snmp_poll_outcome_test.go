package pinger

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker/snmp"
)

func TestSNMPPollOutcome(t *testing.T) {
	timeout := errors.New("request timeout (after 2 retries)")
	cases := []struct {
		name         string
		engineErr    error
		res          *snmp.Result
		wantRecord   bool
		wantAnswered bool
		wantErr      string
	}{
		{"clean poll", nil, &snmp.Result{System: &snmp.SystemInfo{SysName: "sw1"}}, true, true, ""},
		{"no error, nothing collected", nil, &snmp.Result{}, true, true, ""},
		{"no reply at all", nil, &snmp.Result{Err: timeout}, true, false, timeout.Error()},
		{"budget exceeded after the system reply", nil,
			&snmp.Result{Err: errors.New("device budget 20s exceeded"), System: &snmp.SystemInfo{}}, true, true, ""},
		{"interfaces only", nil,
			&snmp.Result{Err: errors.New("x"), Interfaces: []snmp.Interface{{IfIndex: 1}}}, true, true, ""},
		{"scalars only", nil,
			&snmp.Result{Err: errors.New("x"), Scalars: []snmp.MetricSample{{Key: "cpu"}}}, true, true, ""},
		{"engine stopping", context.Canceled, &snmp.Result{Err: context.Canceled}, false, false, ""},
		{"nil result", nil, nil, false, false, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			record, answered, errText := snmpPollOutcome(tc.engineErr, tc.res)
			if record != tc.wantRecord || answered != tc.wantAnswered || errText != tc.wantErr {
				t.Fatalf("got (%v, %v, %q), want (%v, %v, %q)", record, answered, errText,
					tc.wantRecord, tc.wantAnswered, tc.wantErr)
			}
		})
	}
}

func TestSNMPPollOutcomeBoundsErrorText(t *testing.T) {
	_, _, errText := snmpPollOutcome(nil, &snmp.Result{Err: errors.New(strings.Repeat("e", 2000))})
	if len(errText) != maxSNMPPollError {
		t.Fatalf("error text is %d bytes, want %d", len(errText), maxSNMPPollError)
	}
}

type pollCall struct {
	id       uuid.UUID
	answered bool
	errText  string
}

type fakePollRecorder struct {
	mu    sync.Mutex
	calls []pollCall
}

func (f *fakePollRecorder) RecordSNMPPoll(_ context.Context, id uuid.UUID, _ time.Time, answered bool, errText string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, pollCall{id, answered, errText})
	return nil
}

func TestHandleSNMPResultRecordsPollOutcome(t *testing.T) {
	rec := &fakePollRecorder{}
	e := snmpTestEngine(&fakeSNMPLoader{})
	e.snmpPolls = rec
	dev := &snmp.Device{ID: uuid.New(), Hostname: "core-1"}

	// A poll nobody answered: recorded as a failure with its error.
	e.handleSNMPResult(context.Background(), dev, &snmp.Result{Err: errors.New("request timeout")})
	// The engine stopping mid-poll records nothing.
	stopped, cancel := context.WithCancel(context.Background())
	cancel()
	e.handleSNMPResult(stopped, dev, &snmp.Result{Err: context.Canceled})

	if len(rec.calls) != 1 {
		t.Fatalf("recorded %d polls, want 1: %+v", len(rec.calls), rec.calls)
	}
	got := rec.calls[0]
	if got.id != dev.ID || got.answered || got.errText != "request timeout" {
		t.Fatalf("recorded %+v", got)
	}
}

func TestHandleSNMPResultWithoutRecorder(t *testing.T) {
	// No recorder (a store without migration 202, or tests): nothing panics.
	e := snmpTestEngine(&fakeSNMPLoader{})
	e.handleSNMPResult(context.Background(), &snmp.Device{ID: uuid.New()}, &snmp.Result{Err: errors.New("x")})
}

func TestNewEngineUsesLoaderAsPollRecorder(t *testing.T) {
	type loaderWithPolls struct {
		*fakeSNMPLoader
		*fakePollRecorder
	}
	l := loaderWithPolls{&fakeSNMPLoader{}, &fakePollRecorder{}}
	if _, ok := SNMPLoader(l).(SNMPPollRecorder); !ok {
		t.Fatal("a loader that records polls must satisfy SNMPPollRecorder")
	}
	if _, ok := SNMPLoader(&fakeSNMPLoader{}).(SNMPPollRecorder); ok {
		t.Fatal("the plain fake loader must not be taken for a recorder")
	}
}
