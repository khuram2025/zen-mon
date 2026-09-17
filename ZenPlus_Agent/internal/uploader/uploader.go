package uploader

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"hash/fnv"
	"net/http"
	"strconv"
	"strings"
	"time"

	"zenplus-agent/internal/client"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/spool"
)

type Uploader struct {
	client   *client.Client
	store    *spool.Store
	agentID  string
	serverID string
	now      func() time.Time
}

// The appliance can spend up to 5 seconds acquiring a batch claim and then
// 20 seconds processing it. Keep real response/network margin while still
// finishing before the 30-second client deadline and the 45-second nginx
// host-results deadline.
const (
	hostResultsRequestTimeout             = 29 * time.Second
	maxDeferredValidationFailuresPerDrain = 3
	validationFailureCircuitCooldown      = 5 * time.Minute
	validationFailureFairnessDelay        = 1 * time.Minute
)

func New(client *client.Client, store *spool.Store, agentID string, serverID string) *Uploader {
	return &Uploader{
		client: client, store: store, agentID: agentID, serverID: serverID,
		now: time.Now,
	}
}

// RetryableError carries the controller's requested (or locally computed)
// delay so the runtime can schedule the next drain without losing the cooldown
// when an unrelated heartbeat succeeds.
type RetryableError struct {
	Err        error
	RetryAfter time.Duration
}

func (e *RetryableError) Error() string {
	return fmt.Sprintf("%v; retry after %s", e.Err, e.RetryAfter.Round(time.Millisecond))
}

func (e *RetryableError) Unwrap() error { return e.Err }

// QuarantinedBatchError is a terminal, non-retryable local outcome. The
// records remain available through spool quarantine diagnostics, but no longer
// block active telemetry.
type QuarantinedBatchError struct {
	BatchIDs []string
	Reasons  []string
}

func (e *QuarantinedBatchError) Error() string {
	message := fmt.Sprintf("quarantined %d terminal batch(es): %s", len(e.BatchIDs), strings.Join(e.BatchIDs, ", "))
	if len(e.Reasons) > 0 {
		message += " (" + strings.Join(e.Reasons, "; ") + ")"
	}
	return message
}

func (e *QuarantinedBatchError) NonRetryable() bool { return true }

// Client exposes the underlying API client for auxiliary flows (self-update).
func (u *Uploader) Client() *client.Client {
	return u.client
}

func (u *Uploader) SendHeartbeat(ctx context.Context, hb model.Heartbeat) (model.HeartbeatResponse, error) {
	var out model.HeartbeatResponse
	resp, _, err := u.client.PostJSON(ctx, "/api/v1/agents/heartbeat", hb, &out)
	if err != nil {
		if resp != nil && resp.StatusCode == http.StatusNotFound {
			return out, fmt.Errorf("agent heartbeat endpoint is not available on this controller yet")
		}
		return out, err
	}
	return out, nil
}

func (u *Uploader) Drain(ctx context.Context, limit int) (int, error) {
	// Periodic, command-triggered, and background drains must never race the
	// same durable record into two simultaneous POSTs. The lease belongs to the
	// store so replacing an Uploader after auth/config refresh cannot bypass it.
	releaseReplay := u.store.AcquireReplayLease()
	defer releaseReplay()
	generation := u.store.CurrentReplayGeneration()
	return u.drainGeneration(ctx, limit, generation)
}

// DrainGeneration binds every durable queue mutation to the worker/client
// generation that initiated it. Replacing the runtime client invalidates an
// old request even when its HTTP response races with the replacement.
func (u *Uploader) DrainGeneration(ctx context.Context, limit int, generation uint64) (int, error) {
	releaseReplay := u.store.AcquireReplayLease()
	defer releaseReplay()
	if !u.store.IsReplayGenerationCurrent(generation) {
		return 0, spool.ErrStaleReplayGeneration
	}
	return u.drainGeneration(ctx, limit, generation)
}

func (u *Uploader) drainGeneration(ctx context.Context, limit int, generation uint64) (int, error) {
	records, err := u.store.PeekDue(limit, u.currentTime())
	if err != nil {
		return 0, err
	}
	if len(records) == 0 {
		return 0, nil
	}
	removed := 0
	quarantined := make([]string, 0)
	quarantineReasons := make([]string, 0)
	deferredValidationFailures := 0
	deferredValidationKeys := make([]uint64, 0, maxDeferredValidationFailuresPerDrain)
	validationCircuitDelay := validationFailureCircuitCooldown
	for _, rec := range records {
		if err := ctx.Err(); err != nil {
			return removed, err
		}
		if !u.store.IsReplayGenerationCurrent(generation) {
			return removed, spool.ErrStaleReplayGeneration
		}
		var batch model.Batch
		if err := json.Unmarshal(rec.Payload, &batch); err != nil {
			// A record already persisted locally cannot become valid on retry.
			// Isolate it for bounded diagnostics instead of silently discarding it.
			reason := "invalid persisted batch JSON: " + err.Error()
			if err := u.store.QuarantineReplay(generation, rec.Key, u.currentTime(), reason); err != nil {
				return removed, err
			}
			quarantined = append(quarantined, rec.BatchID)
			quarantineReasons = append(quarantineReasons, reason)
			continue
		}
		if !u.isUploadableBatch(batch) {
			reason := "persisted batch failed identity or metric validation"
			if err := u.store.QuarantineReplay(generation, rec.Key, u.currentTime(), reason); err != nil {
				return removed, err
			}
			quarantined = append(quarantined, rec.BatchID)
			quarantineReasons = append(quarantineReasons, reason)
			continue
		}
		batch.SentAt = u.currentTime()
		var out model.ResultsResponse
		requestCtx, cancel := context.WithTimeout(ctx, hostResultsRequestTimeout)
		resp, _, err := u.client.PostJSON(requestCtx, "/api/v1/agents/results/host", batch, &out)
		cancel()
		if parentErr := ctx.Err(); parentErr != nil {
			return removed, parentErr
		}
		if resp != nil && resp.StatusCode == http.StatusAccepted {
			// 202 only confirms that work was accepted for processing. It is not a
			// durable final acknowledgement, so retain the batch and try others.
			reason := "controller returned 202 Accepted before durable batch completion"
			if err != nil {
				reason += ": " + err.Error()
			}
			if err := u.deferRecord(generation, rec, resp, reason); err != nil {
				return removed, err
			}
			continue
		}
		if err != nil {
			if resp != nil && resp.StatusCode == http.StatusNotFound {
				return removed, fmt.Errorf("agent results endpoint is not available on this controller yet")
			}
			if isAuthFailure(err) {
				return removed, err
			}
			code, detail, hasControllerError := controllerErrorInfo(err)
			if hasControllerError && statusCode(err) == http.StatusConflict {
				normalizedCode := strings.ToLower(strings.TrimSpace(code))
				normalizedDetail := strings.ToLower(strings.TrimSpace(detail))
				switch {
				case normalizedCode == "host_results_in_progress" ||
					strings.Contains(normalizedDetail, "host-results batch is still being processed"):
					if err := u.deferRecord(generation, rec, resp, err.Error()); err != nil {
						return removed, err
					}
					continue
				case normalizedCode == "batch_id_payload_collision" ||
					normalizedDetail == "batch_id was already used for a different payload":
					if err := u.store.QuarantineReplay(generation, rec.Key, u.currentTime(), err.Error()); err != nil {
						return removed, err
					}
					quarantined = append(quarantined, rec.BatchID)
					quarantineReasons = append(quarantineReasons, err.Error())
					continue
				}
			}
			if statusCode(err) == http.StatusServiceUnavailable &&
				isPerBatchTransient(code, detail) {
				if err := u.deferRecord(generation, rec, resp, err.Error()); err != nil {
					return removed, err
				}
				continue
			}
			if statusCode(err) == http.StatusConflict || isRetryableFailure(err) {
				delay, deferErr := u.deferRecordWithDelay(generation, rec, resp, err.Error())
				if deferErr != nil {
					return removed, deferErr
				}
				return removed, &RetryableError{Err: err, RetryAfter: delay}
			}
			if isTerminalRecordFailure(err) {
				if err := u.store.QuarantineReplay(generation, rec.Key, u.currentTime(), err.Error()); err != nil {
					return removed, err
				}
				quarantined = append(quarantined, rec.BatchID)
				quarantineReasons = append(quarantineReasons, err.Error())
				continue
			}
			if isDeferredRecordFailure(err) {
				// FastAPI request-validation failures are normally specific to one
				// persisted batch. Preserve the payload for a future agent/controller
				// compatibility fix, but postpone it so newer valid telemetry drains.
				delay, deferErr := u.deferRecordWithDelay(generation, rec, resp, err.Error())
				if deferErr != nil {
					return removed, deferErr
				}
				deferredValidationFailures++
				deferredValidationKeys = append(deferredValidationKeys, rec.Key)
				if delay > validationCircuitDelay {
					validationCircuitDelay = delay
				}
				if deferredValidationFailures >= maxDeferredValidationFailuresPerDrain {
					// A controller-wide contract mismatch would otherwise walk a large
					// spool continuously because a different record is always due. Bound
					// that load while leaving each rejected payload available for a
					// compatible future release.
					// Keep the just-failed heads ineligible for a little longer than
					// the global wake. That durable gap lets previously unseen tail
					// records advance on the next cycle instead of selecting the same
					// three oldest keys forever.
					fairnessNotBefore := u.currentTime().Add(validationCircuitDelay + validationFailureFairnessDelay)
					for _, key := range deferredValidationKeys {
						if postponeErr := u.store.PostponeReplay(generation, key, fairnessNotBefore); postponeErr != nil {
							return removed, postponeErr
						}
					}
					return removed, &RetryableError{
						Err:        fmt.Errorf("controller returned %d request-validation failures in one drain: %w", deferredValidationFailures, err),
						RetryAfter: validationCircuitDelay,
					}
				}
				continue
			}
			return removed, err
		}
		if out.Rejected > 0 {
			// A successful HTTP response is a final decision. Preserve rejected
			// samples in bounded quarantine while allowing later telemetry through.
			reason := fmt.Sprintf("controller rejected %d metric sample(s): %v", out.Rejected, out.Errors)
			if err := u.store.QuarantineReplay(generation, rec.Key, u.currentTime(), reason); err != nil {
				return removed, err
			}
			quarantined = append(quarantined, rec.BatchID)
			quarantineReasons = append(quarantineReasons, reason)
			continue
		}
		if err := u.store.AckReplay(generation, rec.Key); err != nil {
			return removed, err
		}
		removed++
	}
	if len(quarantined) > 0 {
		return removed, &QuarantinedBatchError{BatchIDs: quarantined, Reasons: quarantineReasons}
	}
	return removed, nil
}

func (u *Uploader) currentTime() time.Time {
	if u.now == nil {
		return time.Now().UTC()
	}
	return u.now().UTC()
}

func (u *Uploader) deferRecord(generation uint64, rec spool.Record, resp *http.Response, reason string) error {
	_, err := u.deferRecordWithDelay(generation, rec, resp, reason)
	return err
}

func (u *Uploader) deferRecordWithDelay(generation uint64, rec spool.Record, resp *http.Response, reason string) (time.Duration, error) {
	now := u.currentTime()
	delay := retryDelay(resp, rec, now)
	if err := u.store.DeferReplay(generation, rec.Key, now.Add(delay), reason); err != nil {
		return 0, err
	}
	return delay, nil
}

func retryDelay(resp *http.Response, rec spool.Record, now time.Time) time.Duration {
	if resp != nil {
		if delay, ok := parseRetryAfter(resp.Header.Get("Retry-After"), now); ok {
			return delay
		}
	}
	return fallbackRetryDelay(rec)
}

func parseRetryAfter(value string, now time.Time) (time.Duration, bool) {
	const maxRetryAfter = 24 * time.Hour
	value = strings.TrimSpace(value)
	if value == "" {
		return 0, false
	}
	if isDecimal(value) {
		maxSeconds := uint64(maxRetryAfter / time.Second)
		seconds, err := strconv.ParseUint(value, 10, 64)
		if err != nil || seconds > maxSeconds {
			seconds = maxSeconds
		}
		return clampRetryDelay(time.Duration(seconds)*time.Second, maxRetryAfter), true
	}
	when, err := http.ParseTime(value)
	if err != nil {
		return 0, false
	}
	return clampRetryDelay(when.Sub(now), maxRetryAfter), true
}

func isDecimal(value string) bool {
	if value == "" {
		return false
	}
	for _, char := range value {
		if char < '0' || char > '9' {
			return false
		}
	}
	return true
}

func clampRetryDelay(delay, maximum time.Duration) time.Duration {
	if delay < time.Second {
		return time.Second
	}
	if maximum > 0 && delay > maximum {
		return maximum
	}
	return delay
}

func fallbackRetryDelay(rec spool.Record) time.Duration {
	const (
		base = 2 * time.Second
		max  = 5 * time.Minute
	)
	ceiling := base
	for attempt := 0; attempt < rec.Attempts && ceiling < max; attempt++ {
		if ceiling > max/2 {
			ceiling = max
			break
		}
		ceiling *= 2
	}
	if ceiling > max {
		ceiling = max
	}
	floor := ceiling / 2
	span := ceiling - floor
	h := fnv.New64a()
	_, _ = fmt.Fprintf(h, "%d:%s:%d", rec.Key, rec.BatchID, rec.Attempts)
	jitter := time.Duration(h.Sum64() % uint64(span+1))
	return clampRetryDelay(floor+jitter, max)
}

// controllerErrorInfo accepts the current FastAPI string detail and stable
// machine-code envelopes that newer appliances can return. Terminal behavior
// never depends on a substring: only an exact legacy collision string or a
// dedicated machine code can quarantine data.
func controllerErrorInfo(err error) (string, string, bool) {
	var statusErr *client.StatusError
	if !errors.As(err, &statusErr) {
		return "", "", false
	}
	var envelope struct {
		Code   string          `json:"code"`
		Detail json.RawMessage `json:"detail"`
	}
	if json.Unmarshal([]byte(statusErr.Body), &envelope) != nil {
		return "", "", false
	}
	code := envelope.Code
	detail := ""
	if len(envelope.Detail) > 0 {
		if json.Unmarshal(envelope.Detail, &detail) != nil {
			var nested struct {
				Code    string `json:"code"`
				Message string `json:"message"`
				Detail  string `json:"detail"`
			}
			if json.Unmarshal(envelope.Detail, &nested) == nil {
				if code == "" {
					code = nested.Code
				}
				detail = nested.Message
				if detail == "" {
					detail = nested.Detail
				}
			}
		}
	}
	if code == "" && detail == "" {
		return "", "", false
	}
	return code, detail, true
}

func statusCode(err error) int {
	var statusErr *client.StatusError
	if errors.As(err, &statusErr) {
		return statusErr.Code
	}
	return 0
}

func isAuthFailure(err error) bool {
	return client.IsStatus(err, http.StatusUnauthorized, http.StatusForbidden)
}

func isRetryableFailure(err error) bool {
	code := statusCode(err)
	if code == 0 {
		// Transport failures, timeouts, and malformed success responses are safe
		// to replay because batch IDs are idempotent on the controller.
		return true
	}
	return code == http.StatusRequestTimeout || code == 425 || code == http.StatusTooManyRequests || code >= 500
}

func isTerminalRecordFailure(err error) bool {
	switch statusCode(err) {
	case http.StatusRequestEntityTooLarge:
		return true
	case http.StatusBadRequest, http.StatusUnprocessableEntity:
		code, detail, ok := controllerErrorInfo(err)
		if !ok {
			return false
		}
		normalizedCode := strings.ToLower(strings.TrimSpace(code))
		normalizedDetail := strings.ToLower(strings.TrimSpace(detail))
		return normalizedCode == "invalid_host_results_batch" ||
			(statusCode(err) == http.StatusBadRequest &&
				normalizedDetail == "batch_id must be 1-255 non-whitespace characters")
	default:
		return false
	}
}

func isDeferredRecordFailure(err error) bool {
	return statusCode(err) == http.StatusUnprocessableEntity
}

func isPerBatchTransient(code, detail string) bool {
	normalizedCode := strings.ToLower(strings.TrimSpace(code))
	normalizedDetail := strings.ToLower(strings.TrimSpace(detail))
	switch normalizedCode {
	case "host_results_claim_busy", "host_results_processing_timeout":
		return true
	}
	return normalizedDetail == "host-results batch claim is busy; retry the same batch" ||
		normalizedDetail == "host telemetry processing timed out; retry the same batch"
}

func (u *Uploader) isUploadableBatch(batch model.Batch) bool {
	if batch.AgentID == "" || batch.ServerID == "" || batch.BatchID == "" || len(batch.Metrics) == 0 {
		return false
	}
	if u.agentID != "" && batch.AgentID != u.agentID {
		return false
	}
	if u.serverID != "" && batch.ServerID != u.serverID {
		return false
	}
	for _, metric := range batch.Metrics {
		if metric.Kind == "" || metric.Timestamp.IsZero() || metric.Data == nil {
			return false
		}
	}
	return true
}

func (u *Uploader) PollCommands(ctx context.Context) ([]model.Command, error) {
	var out model.CommandPoll
	resp, _, err := u.client.PostNoBody(ctx, "/api/v1/agents/commands/poll", &out)
	if err != nil {
		if resp != nil && resp.StatusCode == http.StatusNotFound {
			return nil, fmt.Errorf("agent command endpoint is not available on this controller yet")
		}
		return nil, err
	}
	return out.Commands, nil
}

func (u *Uploader) SendCommandResult(ctx context.Context, commandID string, result model.CommandResult) error {
	endpoint := fmt.Sprintf("/api/v1/agents/commands/%s/result", commandID)
	_, _, err := u.client.PostJSON(ctx, endpoint, result, nil)
	return err
}

// SendNetworkCapture streams capture progress and flows to the controller.
func (u *Uploader) SendNetworkCapture(ctx context.Context, up model.NetworkCaptureUpload) error {
	up.AgentID = u.agentID
	up.ServerID = u.serverID
	_, _, err := u.client.PostJSON(ctx, "/api/v1/agents/network-capture", up, nil)
	return err
}

func (u *Uploader) RegisterDiagnostics(ctx context.Context, req model.DiagnosticsRequest) error {
	_, _, err := u.client.PostJSON(ctx, "/api/v1/agents/diagnostics", req, nil)
	return err
}
