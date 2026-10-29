package store

import (
	"context"
	"fmt"
	"time"

	"github.com/zenplus/poller/internal/checker"
)

// RecordServiceTransition commits the confirmed state and its alert work
// together. An API restart cannot strand a state change between these writes.
func (s *PostgresStore) RecordServiceTransition(ctx context.Context, sc *checker.ServiceCheck,
	result *checker.ServiceCheckResult, oldStatus, newStatus string, payload []byte) (bool, error) {
	if len(payload) > 65536 {
		return false, fmt.Errorf("service transition payload exceeds 64 KiB")
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer tx.Rollback(context.Background())
	responseMs := float64(result.ResponseTime.Microseconds()) / 1000.0
	changed, err := tx.Exec(ctx, `
		UPDATE service_checks SET status=$1,last_check_at=$2,last_response_ms=$3,last_error=$4,
		 tls_expiry_date=$5,tls_days_remaining=$6,tls_issuer=$7,tls_subject=$8,verification_attempt=0
		WHERE id=$9 AND status=$10 AND (last_check_at IS NULL OR last_check_at<=$2)
		 AND EXISTS (SELECT 1 FROM service_monitoring_vantages v
		             WHERE v.service_check_id=service_checks.id AND v.poller_id='central')`,
		newStatus, result.Timestamp, responseMs, result.Error, result.TLSExpiry,
		result.TLSDaysRemaining, result.TLSIssuer, result.TLSSubject, sc.ID, oldStatus)
	if err != nil || changed.RowsAffected() == 0 {
		return false, err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO service_transition_inbox(service_check_id,observed_at,payload)
		VALUES($1,$2,$3::jsonb)`, sc.ID, result.Timestamp, payload); err != nil {
		return false, err
	}
	if err = tx.Commit(ctx); err != nil {
		return false, err
	}
	return true, nil
}
