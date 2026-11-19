package store

import (
	"context"
	"errors"
	"fmt"
	"sync/atomic"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"
)

// recordSNMPPollSQL keeps one row per device (device_snmp_poll_state,
// migration 202): when it was last polled, last answered, and since when its
// polls have gone unanswered ($1 device, $2 poll time, $3 answered, $4 error).
// An older poll never overwrites a newer one.
const recordSNMPPollSQL = `
	INSERT INTO device_snmp_poll_state AS s
	       (device_id, last_attempt_at, last_ok_at, failing_since, last_failure_at,
	        last_error, consecutive_failures, updated_at)
	VALUES ($1, $2::timestamptz,
	        CASE WHEN $3::boolean THEN $2::timestamptz END,
	        CASE WHEN $3::boolean THEN NULL ELSE $2::timestamptz END,
	        CASE WHEN $3::boolean THEN NULL ELSE $2::timestamptz END,
	        CASE WHEN $3::boolean THEN NULL ELSE $4::text END,
	        CASE WHEN $3::boolean THEN 0 ELSE 1 END,
	        now())
	ON CONFLICT (device_id) DO UPDATE SET
	       last_attempt_at      = EXCLUDED.last_attempt_at,
	       last_ok_at           = COALESCE(EXCLUDED.last_ok_at, s.last_ok_at),
	       failing_since        = CASE WHEN $3::boolean THEN NULL
	                                   ELSE COALESCE(s.failing_since, EXCLUDED.failing_since) END,
	       last_failure_at      = COALESCE(EXCLUDED.last_failure_at, s.last_failure_at),
	       last_error           = CASE WHEN $3::boolean THEN s.last_error ELSE EXCLUDED.last_error END,
	       consecutive_failures = CASE WHEN $3::boolean THEN 0 ELSE s.consecutive_failures + 1 END,
	       updated_at           = now()
	 WHERE s.last_attempt_at <= EXCLUDED.last_attempt_at`

// snmpPollRetry is how long RecordSNMPPoll stays quiet after it found the
// table missing (migration 202 not applied yet) before trying again.
const snmpPollRetry = 10 * time.Minute

var snmpPollSkipUntil atomic.Int64 // unix nanoseconds; zero = table assumed present

// RecordSNMPPoll implements pinger.SNMPPollRecorder.
func (s *PostgresStore) RecordSNMPPoll(ctx context.Context, deviceID uuid.UUID, at time.Time, answered bool, errText string) error {
	now := time.Now()
	if now.UnixNano() < snmpPollSkipUntil.Load() {
		return nil
	}
	_, err := s.pool.Exec(ctx, recordSNMPPollSQL, deviceID, at.UTC(), answered, errText)
	return snmpPollWriteResult(err, now)
}

// snmpPollWriteResult turns the expected failures into silence: the table
// not existing yet (warned once per process, retried every snmpPollRetry) and the device having been
// deleted while it was being polled (foreign key violation).
func snmpPollWriteResult(err error, now time.Time) error {
	if err == nil {
		return nil
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23503" {
		return nil
	}
	if isUndefinedTable(err) {
		if snmpPollSkipUntil.Swap(now.Add(snmpPollRetry).UnixNano()) == 0 {
			fmt.Printf("WARN: device_snmp_poll_state is missing (%v): migration 202 "+
				"(migrate-202-monitoring-gap-alerts.sql) has not been applied. SNMP poll "+
				"outcomes are not recorded, so SNMP unreachable alerts cannot fire.\n", err)
		}
		return nil
	}
	return err
}
