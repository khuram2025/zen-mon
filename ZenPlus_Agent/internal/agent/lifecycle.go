package agent

import (
	"context"
	"encoding/json"
	"time"

	"zenplus-agent/internal/collectors"
	"zenplus-agent/internal/config"
	"zenplus-agent/internal/enroll"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/spool"
	"zenplus-agent/internal/uploader"
)

// shutdownUploadTimeout bounds the attempt to deliver shutdown evidence
// before the agent exits. systemd stops the agent before the network (the
// unit is ordered after network-online.target), so the controller is usually
// still reachable; otherwise the batch stays spooled for the next start.
const shutdownUploadTimeout = 8 * time.Second

// recordStop is called once when the agent stops. On Linux it records how
// the agent left this boot and, when the system itself is shutting down,
// spools the shutdown evidence and tries to deliver it straight away.
func recordStop(cfg config.Config, store *spool.Store, up *uploader.Uploader, enrollment enroll.Result, logf func(string, ...any)) {
	ctx, cancel := context.WithTimeout(context.Background(), shutdownUploadTimeout)
	defer cancel()
	metrics := collectors.StopLifecycle(ctx)
	if len(metrics) == 0 {
		return
	}
	if err := enqueueMetrics(cfg, store, enrollment, metrics); err != nil {
		logf("shutdown evidence not spooled: %v", err)
		return
	}
	logf("system shutdown recorded")
	if !enrollment.Enrolled || up == nil {
		return
	}
	if n, err := up.Drain(ctx, 20); err != nil {
		logf("shutdown evidence stays queued for the next start: %v", err)
	} else if n > 0 {
		logf("uploaded %d batch(es) before stopping", n)
	}
}

// enqueueMetrics spools a batch that carries only the given metrics.
func enqueueMetrics(cfg config.Config, store *spool.Store, enrollment enroll.Result, metrics []model.Metric) error {
	now := time.Now().UTC()
	seq := uint64(now.UnixNano())
	st, _ := store.Stats()
	batch := model.Batch{
		AgentID:       enrollment.Identity.AgentID,
		ServerID:      enrollment.Identity.ServerID,
		BatchID:       randomUUID(),
		SequenceStart: seq,
		SequenceEnd:   seq + uint64(len(metrics)) - 1,
		ConfigHash:    config.Hash(cfg),
		AgentVersion:  model.AgentVersion,
		CollectedAt:   now,
		Metrics:       metrics,
		Health:        model.Health{Status: "ok", QueueDepth: st.Depth, SpoolBytes: st.Bytes},
	}
	payload, err := json.Marshal(batch)
	if err != nil {
		return err
	}
	_, err = store.Enqueue(batch.BatchID, payload, cfg.Spool.MaxBytes)
	return err
}
