package pinger

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"sort"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/config"
	"github.com/zenplus/poller/internal/devicestatus"
	"go.uber.org/zap"
)

// Device status flow (DESIGN "Central writer"):
//
//  1. stepDevice runs devicestatus.Step for one ping result under e.mu. It
//     performs no I/O; it only decides whether the check must be persisted
//     and returns that as a statusJob.
//  2. The jobs of a ping cycle go, in check order, to ONE writer goroutine
//     (statusPipeline.run) through a FIFO channel. Ping cycles run one after
//     the other on the Run goroutine, so the writer persists every device's
//     checks in the order they happened, and the ping loop never waits for
//     PostgreSQL (it only blocks when the queue is full: back-pressure
//     instead of silently losing confirmed changes).
//  3. Only after SaveDeviceStatus has updated the devices row are the
//     check's events delivered: per device and per sink (ClickHouse status
//     log, alert engine, Redis) in a FIFO, so one device's events are never
//     reordered while a slow alert POST for one device does not hold up the
//     others. A save that updated no devices row (the device is no longer
//     centrally owned) drops the events.

// ownerCentral is the device_status_state owner of centrally polled devices.
const ownerCentral = "central"

const (
	// stateHeartbeat rewrites an unchanged state row at least this often;
	// stateHeartbeatFor shortens it for the device's interval.
	stateHeartbeat = 10 * time.Minute
	// stateGapFloor is the floor of the machine's gap rule (a silence longer
	// than max(3 x interval, 180 s) resets the streaks and the window).
	stateGapFloor = 180 * time.Second
	// statusQueueBatches bounds the writer queue (one batch per ping cycle).
	statusQueueBatches = 256
	// statusWriteTimeout bounds one SaveDeviceStatus / ReleaseDevices call.
	statusWriteTimeout = 30 * time.Second
	// maxConcurrentEffects bounds side effects running at the same time
	// across all devices.
	maxConcurrentEffects = 64

	statusLogTimeout = 30 * time.Second
	publishTimeout   = 10 * time.Second
	// alertTimeout is above the alert engine's worst case, so a slow
	// evaluation is not abandoned (and retried out of order) by the poller.
	alertTimeout       = 30 * time.Second
	alertEffectTimeout = alertTimeout + 5*time.Second

	// deviceAlertURL is the alert engine's device status endpoint.
	deviceAlertURL = "http://localhost:8000/api/v1/alert-engine/evaluate"
)

// AlertEvaluator delivers a confirmed device transition to the alert engine.
// The engine waits for each call before delivering the device's next event.
type AlertEvaluator interface {
	EvaluateDeviceStatus(ctx context.Context, ev DeviceAlertEvent) error
}

// DeviceAlertEvent is one alertable transition (StatusChangeEvent on the
// server).
type DeviceAlertEvent struct {
	DeviceID   uuid.UUID
	Hostname   string
	IPAddress  net.IP
	OldStatus  string
	NewStatus  string
	RTTMs      float64
	PacketLoss float32
	// Flapping: this transition's class is flapping (the server holds the
	// notification until the status has lasted flap_stable_minutes).
	Flapping bool
	// IsFlapping is the device-level flag.
	IsFlapping bool
}

// deviceAlertPayload is the JSON body of POST /alert-engine/evaluate.
func deviceAlertPayload(ev DeviceAlertEvent) map[string]any {
	return map[string]any{
		"device_id":   ev.DeviceID.String(),
		"hostname":    ev.Hostname,
		"ip_address":  ev.IPAddress.String(),
		"old_status":  ev.OldStatus,
		"new_status":  ev.NewStatus,
		"rtt_ms":      ev.RTTMs,
		"packet_loss": ev.PacketLoss,
		"flapping":    ev.Flapping,
		"is_flapping": ev.IsFlapping,
	}
}

// httpAlertEvaluator is the production AlertEvaluator (the local API).
type httpAlertEvaluator struct {
	url    string
	client *http.Client
	logger *zap.SugaredLogger
}

func newHTTPAlertEvaluator(logger *zap.SugaredLogger) *httpAlertEvaluator {
	return &httpAlertEvaluator{url: deviceAlertURL, client: &http.Client{Timeout: alertTimeout}, logger: logger}
}

func (h *httpAlertEvaluator) EvaluateDeviceStatus(ctx context.Context, ev DeviceAlertEvent) error {
	body, err := json.Marshal(deviceAlertPayload(ev))
	if err != nil {
		return fmt.Errorf("marshal alert payload: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, h.url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("create alert request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := h.client.Do(req)
	if err != nil {
		return fmt.Errorf("call alert engine: %w", err)
	}
	defer resp.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&out)
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return fmt.Errorf("alert engine answered %s", resp.Status)
	}
	h.logger.Infof("Alert evaluation: %s %s→%s, notifications sent: %v",
		ev.Hostname, ev.OldStatus, ev.NewStatus, out["notifications_sent"])
	return nil
}

// policyDefaults are the config fallbacks for the 'monitoring' policy, so
// deployments that tuned the poller config keep their limits.
func policyDefaults(cfg *config.Config) devicestatus.Defaults {
	return devicestatus.Defaults{
		DegradedRTTMs:     cfg.Poller.DegradedRTTMs,
		DegradedLossPct:   cfg.Poller.DegradedLossPct,
		DownConfirmChecks: cfg.Poller.DownThreshold,
	}
}

// describePolicy is the one-line log form of the effective policy.
func describePolicy(p devicestatus.Policy) string {
	flap := "off"
	if p.FlapDetectionEnabled {
		flap = fmt.Sprintf("%d changes in %d min (stop at %d, stable %d min)",
			p.FlapStartChanges, p.FlapWindowMinutes, p.FlapStopChanges, p.FlapStableMinutes)
	}
	return fmt.Sprintf("degraded when %d of %d checks exceed %s ms or %s%% (recover below %s ms / %s%% for %d checks); "+
		"down after %d failed checks (sensor %d); up after %d responding; flapping %s",
		p.DegradedConfirmChecks, p.DegradedWindowChecks,
		devicestatus.Fmt3(p.DegradedRTTMs), devicestatus.Fmt3(p.DegradedLossPct),
		devicestatus.Fmt3(p.DegradedRTTClearMs), devicestatus.Fmt3(p.DegradedLossClearPct), p.DegradedClearChecks,
		p.DownConfirmChecks, p.SensorDownConfirmChecks, p.UpConfirmChecks, flap)
}

// deviceCheck builds the machine input for one central ping result. The
// timestamp is cut to PostgreSQL's microsecond resolution, so the in-memory
// state and the persisted row hold identical times.
func deviceCheck(d *Device, r *PingResult, maintenance bool) devicestatus.Check {
	c := devicestatus.Check{
		TS:          r.Timestamp.UTC().Truncate(time.Microsecond),
		Responding:  r.IsUp,
		Loss:        float64(r.PacketLoss),
		IntervalS:   effectiveInterval(d.PingInterval, 60*time.Second).Seconds(),
		Maintenance: maintenance,
		Ordered:     true,
		Source:      ownerCentral,
	}
	if r.IsUp {
		c.RTTMs = float64(r.RTT.Microseconds()) / 1000.0
	}
	return c
}

// ensureSeeded gives a device without a machine state (one built outside the
// loader) a silent seed from its displayed status.
func ensureSeeded(d *Device) {
	if d.State.HardStatus == "" {
		d.State = devicestatus.Seed(d.Status, nil, ownerCentral)
		d.StateLoaded = false
	}
}

// initDeviceState prepares a device the engine sees for the first time. A
// state loaded from its row counts as already persisted; a seeded one is
// written by the first check.
func initDeviceState(d *Device) {
	ensureSeeded(d)
	d.Status = d.State.Displayed()
	d.persisted = nil
	d.stateWrittenAt = time.Time{}
	if d.StateLoaded {
		snapshot := d.State
		d.persisted = &snapshot
		if d.State.LastSampleAt != nil {
			d.stateWrittenAt = *d.State.LastSampleAt
		}
	}
}

// adoptDeviceState carries the engine-owned machine state of a device across
// a reload: the in-memory state is authoritative (single central writer), the
// reloaded row may lag behind writes still in the queue.
func adoptDeviceState(d, existing *Device) {
	d.State = existing.State
	d.StateLoaded = existing.StateLoaded
	d.SeedEndReason = existing.SeedEndReason
	d.Status = existing.Status
	d.persisted = existing.persisted
	d.stateWrittenAt = existing.stateWrittenAt
}

// sameState reports whether two states are equal apart from last_sample_at
// and the owner (a change of anything else must be persisted).
func sameState(a, b devicestatus.State) bool {
	return a.HardStatus == b.HardStatus &&
		a.InMaintenance == b.InMaintenance &&
		sameTime(a.StatusSince, b.StatusSince) &&
		a.FailStreak == b.FailStreak &&
		a.OkStreak == b.OkStreak &&
		a.ClearStreak == b.ClearStreak &&
		a.WindowBits == b.WindowBits &&
		a.WindowLen == b.WindowLen &&
		sameTimes(a.ReachChanges, b.ReachChanges) &&
		sameTimes(a.PerfChanges, b.PerfChanges) &&
		a.ReachFlapping == b.ReachFlapping &&
		a.PerfFlapping == b.PerfFlapping &&
		sameTime(a.FlappingSince, b.FlappingSince)
}

func sameTime(a, b *time.Time) bool {
	if a == nil || b == nil {
		return a == b
	}
	return a.Equal(*b)
}

func sameTimes(a, b []time.Time) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if !a[i].Equal(b[i]) {
			return false
		}
	}
	return true
}

// stateHeartbeatFor is the longest an unchanged state row goes unwritten for
// a device polled every interval: half the machine's gap, and at most
// stateHeartbeat. Readers take a last_sample_at older than the gap as a
// silent device (the status explanation reports its state as stale), so the
// row must stay inside it: the write comes with the first check at or past
// the heartbeat, so the row is never older than heartbeat + interval, which
// is at least 30 s (and half an interval) short of the gap.
func stateHeartbeatFor(interval time.Duration) time.Duration {
	gap := 3 * effectiveInterval(interval, 60*time.Second)
	if gap < stateGapFloor {
		gap = stateGapFloor
	}
	if gap/2 < stateHeartbeat {
		return gap / 2
	}
	return stateHeartbeat
}

// planPersist decides whether a check is written: the state row when the
// state (apart from last_sample_at) changed, was never written or is due for
// its heartbeat; the devices row alone when only last_seen moves.
func planPersist(d *Device, next devicestatus.State, responded bool, at time.Time) (write, writeState bool) {
	writeState = d.persisted == nil || !sameState(*d.persisted, next) ||
		at.Sub(d.stateWrittenAt) >= stateHeartbeatFor(d.PingInterval)
	return writeState || responded, writeState
}

// flapEndReason is the end_reason for closing an open flap episode on this
// check: the check's own flap_stop, else a pending reason (reseed, or a stop
// whose write failed), else 'stable'.
func flapEndReason(events []devicestatus.Event, pending string) string {
	for _, ev := range events {
		if ev.Type == devicestatus.EventFlapStop && ev.EndReason != "" {
			return ev.EndReason
		}
	}
	if pending != "" {
		return pending
	}
	return devicestatus.EndStable
}

// statusJob is one check to persist, with the events to deliver after it.
type statusJob struct {
	save       DeviceStatusSave
	events     []devicestatus.Event
	hostname   string
	ip         net.IP
	packetLoss float32
}

// stepDevice advances one device's machine for a ping result. It runs under
// e.mu and does no I/O; the returned job (nil when nothing must be written)
// goes to the status writer.
func (e *Engine) stepDevice(result *PingResult) *statusJob {
	e.mu.Lock()
	defer e.mu.Unlock()

	device, ok := e.devices[result.DeviceID]
	if !ok {
		return nil
	}
	ensureSeeded(device)
	_, inMaint := e.deviceMaint[result.DeviceID]
	check := deviceCheck(device, result, inMaint)
	next, events := devicestatus.Step(device.State, check, e.policy)

	device.State = next
	device.Status = next.Displayed()
	if check.Responding {
		device.LastSeen = check.TS
		device.LastRTT = check.RTTMs
	}

	write, writeState := planPersist(device, next, check.Responding, check.TS)
	if !write {
		// Unchanged state and no reply: nothing to write and, since every
		// event changes the state, nothing to deliver.
		return nil
	}
	job := &statusJob{
		save: DeviceStatusSave{
			DeviceID:          device.ID,
			At:                check.TS,
			Responded:         check.Responding,
			RTTMs:             check.RTTMs,
			State:             next,
			WriteState:        writeState,
			FlapChanges:       len(next.ReachChanges) + len(next.PerfChanges),
			FlapWindowMinutes: e.policy.FlapWindowMinutes,
		},
		events:     events,
		hostname:   device.Hostname,
		ip:         device.IPAddress,
		packetLoss: result.PacketLoss,
	}
	if writeState {
		job.save.EndReason = flapEndReason(events, device.SeedEndReason)
		device.SeedEndReason = ""
		snapshot := next
		device.persisted = &snapshot
		device.stateWrittenAt = check.TS
	}
	return job
}

// processStatusChange steps the device for one result and queues its write.
func (e *Engine) processStatusChange(ctx context.Context, result *PingResult) {
	if job := e.stepDevice(result); job != nil {
		e.statusPipeline().submit(ctx, statusWork{jobs: []*statusJob{job}})
	}
}

// statusPersistFailed makes the device's next check write its full state
// again and keeps the episode end reason the failed write carried.
func (e *Engine) statusPersistFailed(id uuid.UUID, endReason string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	d, ok := e.devices[id]
	if !ok {
		return
	}
	d.persisted = nil
	if d.SeedEndReason == "" && endReason != devicestatus.EndStable {
		d.SeedEndReason = endReason
	}
}

// statusPipeline returns the engine's status writer, starting it on first use.
func (e *Engine) statusPipeline() *statusPipeline {
	e.statusMu.Lock()
	defer e.statusMu.Unlock()
	if e.status == nil {
		e.status = newStatusPipeline(e)
	}
	return e.status
}

// statusPipelineIfStarted returns the writer, or nil when none was started.
func (e *Engine) statusPipelineIfStarted() *statusPipeline {
	e.statusMu.Lock()
	defer e.statusMu.Unlock()
	return e.status
}

// statusWork is one unit for the writer: a cycle's jobs in check order,
// devices that left central polling, and an optional barrier closed once
// everything before it has been written.
type statusWork struct {
	jobs    []*statusJob
	release []uuid.UUID
	barrier chan struct{}
}

type statusPipeline struct {
	e       *Engine
	work    chan statusWork
	ctx     context.Context
	cancel  context.CancelFunc
	effects effectQueue
}

func newStatusPipeline(e *Engine) *statusPipeline {
	ctx, cancel := context.WithCancel(context.Background())
	p := &statusPipeline{
		e:      e,
		work:   make(chan statusWork, statusQueueBatches),
		ctx:    ctx,
		cancel: cancel,
	}
	p.effects.init(maxConcurrentEffects, p.runEffect)
	go p.run()
	return p
}

// submit queues work. It only blocks while the queue is full; then a
// cancelled ctx (shutdown) drops the work instead of waiting.
func (p *statusPipeline) submit(ctx context.Context, w statusWork) bool {
	select {
	case p.work <- w:
		return true
	default:
	}
	select {
	case p.work <- w:
		return true
	case <-ctx.Done():
		p.e.logger.Warnf("Device status writer queue full at shutdown: %d status writes dropped", len(w.jobs))
		return false
	case <-p.ctx.Done():
		return false
	}
}

// flush waits until everything queued before it has been written and its
// side effects delivered, or ctx ends.
func (p *statusPipeline) flush(ctx context.Context) {
	barrier := make(chan struct{})
	if !p.submit(ctx, statusWork{barrier: barrier}) {
		return
	}
	select {
	case <-barrier:
	case <-ctx.Done():
		return
	}
	p.effects.wait(ctx)
}

// stop ends the writer; queued work is abandoned.
func (p *statusPipeline) stop() {
	p.cancel()
}

func (p *statusPipeline) run() {
	for {
		select {
		case <-p.ctx.Done():
			return
		case w := <-p.work:
			for _, job := range w.jobs {
				p.persist(job)
			}
			if len(w.release) > 0 {
				p.release(w.release)
			}
			if w.barrier != nil {
				close(w.barrier)
			}
		}
	}
}

func (p *statusPipeline) persist(job *statusJob) {
	e := p.e
	ctx, cancel := context.WithTimeout(p.ctx, statusWriteTimeout)
	saved, err := e.loader.SaveDeviceStatus(ctx, &job.save)
	cancel()
	switch {
	case err != nil:
		// The in-memory machine stays authoritative: the next check rewrites
		// the full state, and the confirmed changes are still delivered.
		e.logger.Errorf("Failed to save device status for %s (%s): %v", job.hostname, job.ip, err)
		e.statusPersistFailed(job.save.DeviceID, job.save.EndReason)
	case !saved.Device:
		if len(job.events) > 0 {
			e.logger.Infof("Device %s (%s) is no longer centrally polled; dropped %d status events",
				job.hostname, job.ip, len(job.events))
		}
		return
	case job.save.WriteState && !saved.State:
		e.statusPersistFailed(job.save.DeviceID, job.save.EndReason)
	}
	p.deliver(job)
}

func (p *statusPipeline) release(ids []uuid.UUID) {
	ctx, cancel := context.WithTimeout(p.ctx, statusWriteTimeout)
	defer cancel()
	if err := p.e.loader.ReleaseDevices(ctx, ids); err != nil {
		p.e.logger.Errorf("Failed to release %d devices that left central polling: %v", len(ids), err)
	}
}

// deliver hands a persisted check's transitions to the per-device sinks.
// Flap start/stop are only logged: the server sends flap notices from
// device_flap_episodes.
func (p *statusPipeline) deliver(job *statusJob) {
	e := p.e
	id := job.save.DeviceID
	isFlapping := job.save.State.IsFlapping()
	for _, ev := range job.events {
		switch ev.Type {
		case devicestatus.EventFlapStart:
			e.logger.Infof("Flapping started: %s (%s), %s changes", job.hostname, job.ip, ev.Kind)
			continue
		case devicestatus.EventFlapStop:
			e.logger.Infof("Flapping stopped: %s (%s), %s", job.hostname, job.ip, ev.EndReason)
			continue
		case devicestatus.EventTransition:
		default:
			continue
		}

		sc := &StatusChange{
			DeviceID:   id,
			OldStatus:  ev.Old,
			NewStatus:  ev.New,
			Reason:     ev.Reason,
			Timestamp:  job.save.At,
			Flapping:   ev.Flapping,
			IsFlapping: isFlapping,
		}
		note := ""
		switch {
		case !ev.Alert:
			note = " (alerting muted)"
		case ev.Flapping:
			note = " (flapping)"
		}
		e.logger.Infof("Status change: %s (%s) %s → %s: %s%s",
			job.hostname, job.ip, ev.Old, ev.New, ev.Reason, note)

		p.effects.push(effectKey{id, effectStatusLog}, deviceEffect{
			what:    "Failed to write status change to CH",
			timeout: statusLogTimeout,
			run: func(ctx context.Context) error {
				return e.writer.WriteStatusChange(ctx, sc, 0)
			},
		})
		if ev.Alert {
			alert := DeviceAlertEvent{
				DeviceID:   id,
				Hostname:   job.hostname,
				IPAddress:  job.ip,
				OldStatus:  ev.Old,
				NewStatus:  ev.New,
				RTTMs:      job.save.RTTMs,
				PacketLoss: job.packetLoss,
				Flapping:   ev.Flapping,
				IsFlapping: isFlapping,
			}
			p.effects.push(effectKey{id, effectAlert}, deviceEffect{
				what:    "Failed to evaluate device alert",
				timeout: alertEffectTimeout,
				run: func(ctx context.Context) error {
					if e.alerts == nil {
						return fmt.Errorf("no alert evaluator configured")
					}
					return e.alerts.EvaluateDeviceStatus(ctx, alert)
				},
			})
		}
		p.effects.push(effectKey{id, effectPublish}, deviceEffect{
			what:    "Failed to publish status change",
			timeout: publishTimeout,
			run: func(ctx context.Context) error {
				return e.publisher.PublishStatusChange(ctx, sc)
			},
		})
	}
}

func (p *statusPipeline) runEffect(fx deviceEffect) {
	ctx, cancel := context.WithTimeout(p.ctx, fx.timeout)
	defer cancel()
	if err := fx.run(ctx); err != nil {
		p.e.logger.Errorf("%s: %v", fx.what, err)
	}
}

// Side-effect sinks. Each (device, sink) pair is its own FIFO.
type effectKind int

const (
	effectStatusLog effectKind = iota
	effectAlert
	effectPublish
)

type effectKey struct {
	device uuid.UUID
	kind   effectKind
}

type deviceEffect struct {
	what    string
	timeout time.Duration
	run     func(context.Context) error
}

// effectQueue runs effects in FIFO order per key, keys in parallel (at most
// cap(slots) effects at once). A key is in pending exactly while its drain
// goroutine runs.
type effectQueue struct {
	mu      sync.Mutex
	pending map[effectKey][]deviceEffect
	idle    chan struct{} // closed while no drain goroutine runs
	slots   chan struct{}
	exec    func(deviceEffect)
}

func (q *effectQueue) init(concurrency int, exec func(deviceEffect)) {
	q.pending = make(map[effectKey][]deviceEffect)
	q.idle = make(chan struct{})
	close(q.idle)
	q.slots = make(chan struct{}, concurrency)
	q.exec = exec
}

func (q *effectQueue) push(key effectKey, fx deviceEffect) {
	q.mu.Lock()
	defer q.mu.Unlock()
	queued, running := q.pending[key]
	q.pending[key] = append(queued, fx)
	if running {
		return
	}
	if len(q.pending) == 1 {
		q.idle = make(chan struct{})
	}
	go q.drain(key)
}

func (q *effectQueue) drain(key effectKey) {
	for {
		q.mu.Lock()
		items := q.pending[key]
		if len(items) == 0 {
			delete(q.pending, key)
			if len(q.pending) == 0 {
				close(q.idle)
			}
			q.mu.Unlock()
			return
		}
		q.pending[key] = nil
		q.mu.Unlock()
		for _, fx := range items {
			q.slots <- struct{}{}
			q.exec(fx)
			<-q.slots
		}
	}
}

// wait returns once no effect is queued or running, or ctx ends.
func (q *effectQueue) wait(ctx context.Context) {
	q.mu.Lock()
	idle := q.idle
	q.mu.Unlock()
	select {
	case <-idle:
	case <-ctx.Done():
	}
}

// sortedIDs orders device IDs for deterministic release statements.
func sortedIDs(ids []uuid.UUID) []uuid.UUID {
	sort.Slice(ids, func(i, j int) bool { return bytes.Compare(ids[i][:], ids[j][:]) < 0 })
	return ids
}
