package pinger

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
)

func maintSet(ids ...uuid.UUID) map[uuid.UUID]struct{} {
	out := make(map[uuid.UUID]struct{}, len(ids))
	for _, id := range ids {
		out[id] = struct{}{}
	}
	return out
}

func statusLogRows(w *fakeServiceMetricWriter) []*checker.ServiceStatusChange {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]*checker.ServiceStatusChange(nil), w.statusChanges...)
}

type sentServiceAlert struct{ old, new string }

func reconcileEngine(status string) (*Engine, uuid.UUID, *fakeServiceMetricWriter) {
	e := testEngine()
	e.serviceAlertFn = func(context.Context, *checker.ServiceCheck, string, string, *checker.ServiceCheckResult) {}
	id := uuid.New()
	e.serviceChecks[id] = &checker.ServiceCheck{ID: id, Name: "Website", CheckType: "http", Status: status, RetryCount: 1, Enabled: true}
	return e, id, e.svcWriter.(*fakeServiceMetricWriter)
}

func failInMaintenance(e *Engine, id uuid.UUID) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	e.processServiceStatusChange(ctx, &checker.ServiceCheckResult{
		ServiceCheckID: id, CheckType: "http", IsUp: false, Error: "connection refused", Timestamp: time.Now(),
	}, maintSet(id))
}

func TestMaintenanceEndReconcilesChangedStatusOnce(t *testing.T) {
	e, id, writer := reconcileEngine("up")
	ctx := context.Background()

	e.reconcileServiceMaintenance(ctx, id, maintSet())   // first seen outside maintenance: last logged = up
	e.reconcileServiceMaintenance(ctx, id, maintSet(id)) // window starts
	failInMaintenance(e, id)
	if got := e.serviceChecks[id].Status; got != "down" {
		t.Fatalf("expected in-memory down during maintenance, got %q", got)
	}
	e.reconcileServiceMaintenance(ctx, id, maintSet(id)) // still in maintenance
	if rows := statusLogRows(writer); len(rows) != 0 {
		t.Fatalf("expected no rows while still in maintenance, got %d", len(rows))
	}

	e.reconcileServiceMaintenance(ctx, id, maintSet()) // window ended
	e.reconcileServiceMaintenance(ctx, id, maintSet()) // repeated load stays idempotent

	rows := statusLogRows(writer)
	if len(rows) != 1 {
		t.Fatalf("expected exactly one reconciling row, got %d", len(rows))
	}
	r := rows[0]
	if r.ServiceCheckID != id || r.OldStatus != "up" || r.NewStatus != "down" || r.Reason != "maintenance ended" || r.CheckType != "http" {
		t.Fatalf("unexpected reconciling row: %+v", r)
	}
}

func TestMaintenanceEndWithUnchangedStatusWritesNothing(t *testing.T) {
	e, id, writer := reconcileEngine("up")
	ctx := context.Background()

	e.reconcileServiceMaintenance(ctx, id, maintSet())
	e.reconcileServiceMaintenance(ctx, id, maintSet(id))
	e.reconcileServiceMaintenance(ctx, id, maintSet())

	if rows := statusLogRows(writer); len(rows) != 0 {
		t.Fatalf("expected no rows when status is unchanged, got %d", len(rows))
	}
}

func TestStillInMaintenanceWritesNothing(t *testing.T) {
	e, id, writer := reconcileEngine("up")
	ctx := context.Background()

	e.reconcileServiceMaintenance(ctx, id, maintSet())
	e.reconcileServiceMaintenance(ctx, id, maintSet(id))
	failInMaintenance(e, id)
	for i := 0; i < 3; i++ {
		e.reconcileServiceMaintenance(ctx, id, maintSet(id))
	}

	if rows := statusLogRows(writer); len(rows) != 0 {
		t.Fatalf("expected no rows while the window is active, got %d", len(rows))
	}
}

func TestMaintenanceEndWithUnknownLastLoggedStatusWritesNothing(t *testing.T) {
	e, id, writer := reconcileEngine("up")
	ctx := context.Background()

	// First seen inside a window: the last logged status is unknown.
	e.reconcileServiceMaintenance(ctx, id, maintSet(id))
	failInMaintenance(e, id)
	e.reconcileServiceMaintenance(ctx, id, maintSet())

	if rows := statusLogRows(writer); len(rows) != 0 {
		t.Fatalf("expected no rows without a known last logged status, got %d", len(rows))
	}
}


func TestMaintenanceEndSendsTheStillFailingCheckToTheAlertEngine(t *testing.T) {
	e, id, _ := reconcileEngine("up")
	ctx := context.Background()
	sent := make(chan sentServiceAlert, 4)
	e.serviceAlertFn = func(_ context.Context, sc *checker.ServiceCheck, old, new string, r *checker.ServiceCheckResult) {
		if sc.ID != id || r.IsUp {
			t.Errorf("unexpected alert call: %+v %+v", sc, r)
		}
		sent <- sentServiceAlert{old, new}
	}

	e.reconcileServiceMaintenance(ctx, id, maintSet())
	e.reconcileServiceMaintenance(ctx, id, maintSet(id))
	failInMaintenance(e, id)
	e.reconcileServiceMaintenance(ctx, id, maintSet(id))
	select {
	case got := <-sent:
		t.Fatalf("nothing may be sent inside the window, got %+v", got)
	case <-time.After(50 * time.Millisecond):
	}
	e.reconcileServiceMaintenance(ctx, id, maintSet())
	select {
	case got := <-sent:
		if got.old != "up" || got.new != "down" {
			t.Fatalf("unexpected transition sent: %+v", got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the still-failing check was never sent to the alert engine")
	}
	e.reconcileServiceMaintenance(ctx, id, maintSet())
	select {
	case got := <-sent:
		t.Fatalf("sent twice: %+v", got)
	case <-time.After(50 * time.Millisecond):
	}
}
