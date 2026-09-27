package agent

import (
	"context"
	"sync"

	"zenplus-agent/internal/client"
	"zenplus-agent/internal/config"
)

// apmReconciler is the part of the APM manager the worker drives.
type apmReconciler interface {
	Reconcile(ctx context.Context, cfg config.Config, agentID, serverID string, api *client.Client)
}

type apmReconcileRequest struct {
	cfg               config.Config
	agentID, serverID string
	api               *client.Client
}

// apmWorker runs APM reconciliation off the agent's main loop. A reconcile
// can wait on an unreachable appliance (credential enrollment), scan every
// process for runtime discovery, and start or stop the gateway; none of that
// may delay host collection or heartbeats. Requests coalesce: only the most
// recent config matters, so a slow pass is never followed by a backlog.
type apmWorker struct {
	requests chan apmReconcileRequest
	cancel   context.CancelFunc
	done     sync.WaitGroup
}

func startAPMWorker(parent context.Context, reconciler apmReconciler) *apmWorker {
	ctx, cancel := context.WithCancel(parent)
	w := &apmWorker{requests: make(chan apmReconcileRequest, 1), cancel: cancel}
	w.done.Add(1)
	go func() {
		defer w.done.Done()
		for {
			select {
			case <-ctx.Done():
				return
			case req := <-w.requests:
				reconciler.Reconcile(ctx, req.cfg, req.agentID, req.serverID, req.api)
			}
		}
	}()
	return w
}

// Submit queues a reconcile, replacing any request that has not started yet.
// It never blocks.
func (w *apmWorker) Submit(req apmReconcileRequest) {
	for {
		select {
		case w.requests <- req:
			return
		default:
		}
		select {
		case <-w.requests:
		default:
		}
	}
}

// Close stops the worker and waits for an in-flight reconcile to return.
func (w *apmWorker) Close() {
	w.cancel()
	w.done.Wait()
}
