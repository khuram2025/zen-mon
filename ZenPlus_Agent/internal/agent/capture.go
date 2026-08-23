package agent

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"time"

	"zenplus-agent/internal/model"
	"zenplus-agent/internal/netcapture"
)

type captureSender interface {
	SendNetworkCapture(context.Context, model.NetworkCaptureUpload) error
}

type captureRunFunc func(context.Context, string, netcapture.Options, captureSender, func(string, ...any)) string

type activeCapture struct {
	id     string
	cancel context.CancelFunc
	done   chan struct{}
}

type captureManager struct {
	mu          sync.Mutex
	active      *activeCapture
	states      map[string]string
	history     []string
	historySize int
	stopTimeout time.Duration
	run         captureRunFunc
}

type captureStartResult struct {
	CaptureID string
	Status    string
	Duplicate bool
}

type captureStopResult struct {
	CaptureID string
	Status    string
	Duplicate bool
}

func newCaptureManager(run captureRunFunc) *captureManager {
	return &captureManager{
		states:      map[string]string{},
		historySize: 64,
		stopTimeout: 35 * time.Second,
		run:         run,
	}
}

var networkCaptures = newCaptureManager(runNetworkCapture)

func (m *captureManager) Start(parent context.Context, captureID string, opts netcapture.Options, sender captureSender, logf func(string, ...any)) (captureStartResult, error) {
	captureID = strings.TrimSpace(captureID)
	if captureID == "" {
		return captureStartResult{}, fmt.Errorf("capture_id is required")
	}
	m.mu.Lock()
	if state, ok := m.states[captureID]; ok {
		m.mu.Unlock()
		return captureStartResult{CaptureID: captureID, Status: state, Duplicate: true}, nil
	}
	if m.active != nil {
		activeID := m.active.id
		m.mu.Unlock()
		return captureStartResult{}, fmt.Errorf("network capture %s is already running on this host", activeID)
	}
	ctx, cancel := context.WithCancel(parent)
	m.active = &activeCapture{id: captureID, cancel: cancel, done: make(chan struct{})}
	m.states[captureID] = "running"
	m.mu.Unlock()

	go func() {
		state := m.run(ctx, captureID, opts, sender, logf)
		if state == "" {
			state = "failed"
		}
		m.finish(captureID, state)
	}()
	return captureStartResult{CaptureID: captureID, Status: "running"}, nil
}

func (m *captureManager) Stop(ctx context.Context, captureID string) (captureStopResult, error) {
	captureID = strings.TrimSpace(captureID)
	m.mu.Lock()
	if captureID != "" {
		if state, ok := m.states[captureID]; ok && (m.active == nil || m.active.id != captureID) {
			m.mu.Unlock()
			return captureStopResult{CaptureID: captureID, Status: state, Duplicate: true}, nil
		}
	}
	if m.active == nil {
		m.mu.Unlock()
		if captureID == "" {
			return captureStopResult{}, fmt.Errorf("no network capture is running")
		}
		return captureStopResult{}, fmt.Errorf("network capture %s is not running", captureID)
	}
	if captureID != "" && captureID != m.active.id {
		activeID := m.active.id
		m.mu.Unlock()
		return captureStopResult{}, fmt.Errorf("network capture %s is running, not %s", activeID, captureID)
	}
	captureID = m.active.id
	duplicate := m.states[captureID] == "cancelling"
	done := m.active.done
	if !duplicate {
		m.states[captureID] = "cancelling"
		m.active.cancel()
	}
	stopTimeout := m.stopTimeout
	m.mu.Unlock()

	timer := time.NewTimer(stopTimeout)
	defer timer.Stop()
	select {
	case <-done:
		state, _ := m.Status(captureID)
		return captureStopResult{CaptureID: captureID, Status: state, Duplicate: duplicate}, nil
	case <-ctx.Done():
		return captureStopResult{}, fmt.Errorf("waiting for network capture %s to stop: %w", captureID, ctx.Err())
	case <-timer.C:
		return captureStopResult{}, fmt.Errorf("timed out after %s waiting for network capture %s to stop", stopTimeout, captureID)
	}
}

func (m *captureManager) finish(captureID, state string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.active != nil && m.active.id == captureID {
		m.active.cancel()
		close(m.active.done)
		m.active = nil
	}
	m.states[captureID] = state
	m.history = append(m.history, captureID)
	for len(m.history) > m.historySize {
		oldest := m.history[0]
		m.history = m.history[1:]
		if m.active == nil || m.active.id != oldest {
			delete(m.states, oldest)
		}
	}
}

func (m *captureManager) Status(captureID string) (string, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	state, ok := m.states[captureID]
	return state, ok
}
