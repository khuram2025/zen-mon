package pinger

import (
	"context"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker/snmp"
)

// SNMPPollRecorder records whether each SNMP poll got an answer
// (device_snmp_poll_state, migration 202). The API's monitoring gap pass
// raises "SNMP unreachable" from it when a device answers ping while its SNMP
// polls keep failing (docs/monitoring-gap-alerts.md).
//
// It is optional: an SNMPLoader that also implements it is used, and a nil
// recorder records nothing (tests, and stores without the table).
type SNMPPollRecorder interface {
	RecordSNMPPoll(ctx context.Context, deviceID uuid.UUID, at time.Time, answered bool, errText string) error
}

// maxSNMPPollError bounds the stored error text.
const maxSNMPPollError = 500

// snmpPollOutcome decides what one finished poll means for the poll state.
//
//   - record=false when the engine itself is stopping (its context ended):
//     an interrupted poll says nothing about the device.
//   - answered=true when the device returned anything at all (system info,
//     interfaces, any scalar, entities, sensors, template values or UDT
//     data), or the poll ended without an error. A poll the per-device
//     budget cut short after the device replied is answered: the device
//     speaks SNMP, it is only slow.
//   - answered=false otherwise: no reply to anything (wrong community or
//     credentials, an ACL, the agent off, the session never opened).
func snmpPollOutcome(engineCtxErr error, r *snmp.Result) (record bool, answered bool, errText string) {
	if engineCtxErr != nil || r == nil {
		return false, false, ""
	}
	r.Mu.Lock()
	defer r.Mu.Unlock()
	replied := r.System != nil || len(r.Interfaces) > 0 || len(r.Scalars) > 0 ||
		len(r.Entities) > 0 || len(r.Sensors) > 0 || len(r.TplValues) > 0 || r.Udt != nil
	if r.Err == nil || replied {
		return true, true, ""
	}
	msg := r.Err.Error()
	if len(msg) > maxSNMPPollError {
		msg = msg[:maxSNMPPollError]
	}
	return true, false, msg
}

// recordSNMPPoll stores the outcome of one poll; failures are logged, never
// fatal (the poll's data is already handled).
func (e *Engine) recordSNMPPoll(ctx context.Context, d *snmp.Device, r *snmp.Result) {
	if e.snmpPolls == nil {
		return
	}
	record, answered, errText := snmpPollOutcome(ctx.Err(), r)
	if !record {
		return
	}
	at := time.Now().UTC()
	if err := e.snmpPolls.RecordSNMPPoll(ctx, d.ID, at, answered, errText); err != nil {
		e.logger.Warnf("RecordSNMPPoll %s: %v", d.Hostname, err)
	}
}
