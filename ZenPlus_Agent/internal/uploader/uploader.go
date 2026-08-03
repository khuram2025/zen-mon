package uploader

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
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
}

func New(client *client.Client, store *spool.Store, agentID string, serverID string) *Uploader {
	return &Uploader{client: client, store: store, agentID: agentID, serverID: serverID}
}

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
	records, err := u.store.Peek(limit)
	if err != nil {
		return 0, err
	}
	if len(records) == 0 {
		return 0, nil
	}
	acked := make([]uint64, 0, len(records))
	for _, rec := range records {
		var batch model.Batch
		if err := json.Unmarshal(rec.Payload, &batch); err != nil {
			return len(acked), err
		}
		if !u.isUploadableBatch(batch) {
			acked = append(acked, rec.Key)
			continue
		}
		batch.SentAt = time.Now().UTC()
		var out model.ResultsResponse
		resp, _, err := u.client.PostJSON(ctx, "/api/v1/agents/results/host", batch, &out)
		if err != nil {
			if resp != nil && resp.StatusCode == http.StatusNotFound {
				return len(acked), fmt.Errorf("agent results endpoint is not available on this controller yet")
			}
			if len(acked) > 0 {
				_ = u.store.Ack(acked...)
			}
			return len(acked), err
		}
		if out.Rejected > 0 {
			return len(acked), fmt.Errorf("controller rejected %d metric sample(s): %v", out.Rejected, out.Errors)
		}
		acked = append(acked, rec.Key)
	}
	if err := u.store.Ack(acked...); err != nil {
		return len(acked), err
	}
	return len(acked), nil
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
