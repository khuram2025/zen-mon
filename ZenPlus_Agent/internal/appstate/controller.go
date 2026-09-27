package appstate

import (
	"time"

	"zenplus-agent/internal/model"
)

// ControllerFromStatus reports controller connectivity as the agent itself
// observed it. The dashboard used to probe the controller with its own HTTP
// client, which trusts only the Windows certificate store. Appliances with an
// internal or self-signed certificate (trusted by the agent through its
// configured CA bundle, which the dashboard cannot read on an all-users
// install) were therefore always shown as unreachable while the agent was
// reporting normally. The agent's heartbeat is the authoritative signal.
func ControllerFromStatus(controllerURL string, status *model.Status, heartbeatIntervalSeconds int, now time.Time) ControllerSnapshot {
	out := ControllerSnapshot{URL: controllerURL}
	if status == nil || (status.LastHeartbeat == nil && status.LastHeartbeatError == "") {
		out.Status = "connecting"
		out.Message = "Waiting for the agent's first heartbeat"
		return out
	}
	out.Known = true
	fresh := status.LastHeartbeat != nil && now.Sub(*status.LastHeartbeat) <= heartbeatFreshness(heartbeatIntervalSeconds)
	if fresh && status.LastHeartbeatError == "" {
		out.Reachable = true
		out.Status = "reporting"
		return out
	}
	out.Status = "unreachable"
	out.Message = status.LastHeartbeatError
	if out.Message == "" {
		out.Message = "No heartbeat has succeeded recently"
	}
	return out
}

// heartbeatFreshness tolerates a few missed heartbeats before the dashboard
// calls the controller unreachable.
func heartbeatFreshness(intervalSeconds int) time.Duration {
	window := 3 * time.Duration(intervalSeconds) * time.Second
	if window < 2*time.Minute {
		window = 2 * time.Minute
	}
	return window
}
