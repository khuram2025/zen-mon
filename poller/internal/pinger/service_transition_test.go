package pinger

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
)

type durableServiceLoader struct {
	fakeServiceLoader
	queued  int
	fail    bool
	payload map[string]interface{}
}

func (f *durableServiceLoader) RecordServiceTransition(_ context.Context, _ *checker.ServiceCheck,
	_ *checker.ServiceCheckResult, _, _ string, body []byte) (bool, error) {
	if f.fail {
		return false, errors.New("injected PG failure")
	}
	f.queued++
	return true, json.Unmarshal(body, &f.payload)
}

func TestServiceTransitionWaitsForDurableCommit(t *testing.T) {
	e := testEngine()
	loader := &durableServiceLoader{fail: true}
	e.svcLoader = loader
	id := uuid.New()
	sc := &checker.ServiceCheck{ID: id, Name: "Durable HTTP", CheckType: "http", Status: "down", RetryCount: 3}
	e.serviceChecks[id] = sc
	r := &checker.ServiceCheckResult{ServiceCheckID: id, CheckType: "http", IsUp: true, Timestamp: time.Now()}
	e.processServiceStatusChange(context.Background(), r, map[uuid.UUID]struct{}{})
	if sc.Status != "down" || loader.queued != 0 {
		t.Fatal("uncommitted recovery became confirmed")
	}
	loader.fail = false
	e.processServiceStatusChange(context.Background(), r, map[uuid.UUID]struct{}{})
	if sc.Status != "up" || loader.queued != 1 || loader.payload["old_status"] != "down" || loader.payload["new_status"] != "up" {
		t.Fatalf("recovery not durably queued: %+v", loader)
	}
	if len(loader.updates) != 0 {
		t.Fatal("separate asynchronous state write raced the durable transition")
	}
}

func TestServiceMaintenanceDoesNotEnqueueTransition(t *testing.T) {
	e := testEngine()
	loader := &durableServiceLoader{}
	e.svcLoader = loader
	id := uuid.New()
	e.serviceChecks[id] = &checker.ServiceCheck{ID: id, CheckType: "http", Status: "down"}
	e.processServiceStatusChange(context.Background(), &checker.ServiceCheckResult{ServiceCheckID: id, IsUp: true, Timestamp: time.Now()}, map[uuid.UUID]struct{}{id: {}})
	if loader.queued != 0 || e.serviceChecks[id].Status != "up" {
		t.Fatal("maintenance semantics changed")
	}
}
