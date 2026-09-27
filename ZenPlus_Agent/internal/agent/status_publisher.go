package agent

import (
	"bytes"
	"encoding/json"
	"time"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/identity"
	"zenplus-agent/internal/model"
	"zenplus-agent/internal/runtime"
)

// statusRefreshInterval keeps the status files fresh while nothing changes.
// The dashboard reports the agent as stale after three minutes without one.
const statusRefreshInterval = time.Minute

// statusPublisher writes the local status file and the dashboard snapshot
// when their content changes, instead of after every loop event (every few
// seconds, each with an fsync and ACL checks on the snapshot).
type statusPublisher struct {
	last    []byte
	written time.Time
	now     func() time.Time
}

func (p *statusPublisher) publish(paths runtime.Paths, cfg config.Config, id identity.Identity, status model.Status) {
	now := time.Now
	if p.now != nil {
		now = p.now
	}
	snapshot := runtime.NewDashboardSnapshot(cfg, id, status)
	snapshot.GeneratedAt = time.Time{} // compare content, not the timestamp
	current, err := json.Marshal(struct {
		Status   model.Status
		Snapshot runtime.DashboardSnapshot
	}{status, snapshot})
	if err == nil && bytes.Equal(current, p.last) && now().Sub(p.written) < statusRefreshInterval {
		return
	}
	_ = writeStatus(paths.StatusFile, status)
	_ = runtime.WriteMachineDashboardSnapshot(cfg, id, status)
	p.last = current
	p.written = now()
}
