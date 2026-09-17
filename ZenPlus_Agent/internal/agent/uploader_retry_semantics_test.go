package agent

import (
	"errors"
	"net/http"
	"testing"
	"time"

	"zenplus-agent/internal/client"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/enroll"
	"zenplus-agent/internal/identity"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/spool"
	"zenplus-agent/internal/uploader"
)

func TestSupervisorHonorsUploaderRetryAfter(t *testing.T) {
	t.Parallel()

	sup := newSupervisor()
	status := model.Status{}
	const requested = 45 * time.Second
	before := time.Now().UTC()
	sup.onError(
		&uploader.RetryableError{Err: errors.New("controller backpressure"), RetryAfter: requested},
		&status,
		func(string, ...any) {},
	)
	after := time.Now().UTC()

	earliest := before.Add(requested)
	latest := after.Add(requested + time.Second)
	if sup.notBefore.Before(earliest) || sup.notBefore.After(latest) {
		t.Fatalf("supervisor retry deadline = %v, want between %v and %v", sup.notBefore, earliest, latest)
	}
	if status.NextRetryAt == nil || !status.NextRetryAt.Equal(sup.notBefore) {
		t.Fatalf("status retry deadline = %v, supervisor = %v", status.NextRetryAt, sup.notBefore)
	}
}

func TestSupervisorDoesNotBackOffAfterTerminalQuarantine(t *testing.T) {
	t.Parallel()

	sup := newSupervisor()
	status := model.Status{}
	sup.onError(
		&uploader.QuarantinedBatchError{BatchIDs: []string{"collision"}},
		&status,
		func(string, ...any) {},
	)

	if !sup.notBefore.IsZero() || status.NextRetryAt != nil {
		t.Fatalf(
			"terminal quarantine incorrectly marked controller unavailable: notBefore=%v next=%v",
			sup.notBefore,
			status.NextRetryAt,
		)
	}
	if attempts := sup.comm.Attempts(); attempts != 0 {
		t.Fatalf("terminal quarantine advanced outage backoff attempts to %d", attempts)
	}
}

func TestApplyUploadOutcomeIgnoresStaleUnauthorizedClient(t *testing.T) {
	sup := newSupervisor()
	status := model.Status{AuthState: "ok"}
	applyUploadOutcome(uploadOutcome{
		Generation: 1,
		Err: &client.StatusError{
			Code: http.StatusUnauthorized, Status: "401 Unauthorized",
			Method: http.MethodPost, URL: "https://old-controller/api/v1/agents/results/host",
		},
		Queue: spool.Stats{Depth: 2, Bytes: 200},
	}, 2, &status, sup, func(string, ...any) {})

	if sup.authFailed || status.AuthState != "ok" || status.LastUploadError != "" {
		t.Fatalf("stale unauthorized outcome changed active auth state: supervisor=%#v status=%#v", sup, status)
	}
	if status.QueueDepth != 2 || status.SpoolBytes != 200 {
		t.Fatalf("stale outcome did not refresh safe spool stats: %#v", status)
	}
}

func TestRuntimeClientRefreshClearsOldControllerCooldown(t *testing.T) {
	t.Parallel()

	sup := newSupervisor()
	_ = sup.comm.Next()
	_ = sup.comm.Next()
	sup.notBefore = time.Now().UTC().Add(10 * time.Minute)
	next := sup.notBefore
	status := model.Status{NextRetryAt: &next}

	resetSupervisorAfterRuntimeClientRefresh(sup, &status, enroll.Result{Enrolled: true})

	if !sup.notBefore.IsZero() || status.NextRetryAt != nil {
		t.Fatalf("old controller cooldown survived client refresh: notBefore=%v next=%v", sup.notBefore, status.NextRetryAt)
	}
	if attempts := sup.comm.Attempts(); attempts != 0 {
		t.Fatalf("old controller backoff attempts survived client refresh: %d", attempts)
	}
}

func TestRuntimeConnectionFingerprintIgnoresAPMOnlyChanges(t *testing.T) {
	t.Parallel()

	cfg := config.Default()
	enrollment := enroll.Result{
		Enrolled: true,
		APIKey:   "credential-one",
		Identity: identity.Identity{AgentID: "agent-1", ServerID: "server-1"},
	}
	baseline := runtimeConnectionFingerprint(cfg, enrollment)

	cfg.APM.Enabled = !cfg.APM.Enabled
	if got := runtimeConnectionFingerprint(cfg, enrollment); got != baseline {
		t.Fatal("APM-only change altered runtime connection fingerprint")
	}
	cfg.ControllerURL = "https://replacement.example"
	if got := runtimeConnectionFingerprint(cfg, enrollment); got == baseline {
		t.Fatal("controller replacement did not alter runtime connection fingerprint")
	}
	cfg.ControllerURL = config.Default().ControllerURL
	enrollment.APIKey = "credential-two"
	if got := runtimeConnectionFingerprint(cfg, enrollment); got == baseline {
		t.Fatal("credential replacement did not alter runtime connection fingerprint")
	}
}
