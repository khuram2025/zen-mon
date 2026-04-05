package store

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/zenplus/poller/internal/checker"
	"github.com/zenplus/poller/internal/config"
	"github.com/zenplus/poller/internal/pinger"
)

// PostgresStore handles device data in PostgreSQL.
type PostgresStore struct {
	pool *pgxpool.Pool
}

// NewPostgresStore connects to PostgreSQL.
func NewPostgresStore(ctx context.Context, cfg *config.Config) (*PostgresStore, error) {
	poolCfg, err := pgxpool.ParseConfig(cfg.Postgres.DSN())
	if err != nil {
		return nil, fmt.Errorf("parse postgres config: %w", err)
	}
	poolCfg.MaxConns = 10

	pool, err := pgxpool.NewWithConfig(ctx, poolCfg)
	if err != nil {
		return nil, fmt.Errorf("connect to postgres: %w", err)
	}

	if err := pool.Ping(ctx); err != nil {
		return nil, fmt.Errorf("ping postgres: %w", err)
	}

	return &PostgresStore{pool: pool}, nil
}

// Close closes the connection pool.
func (s *PostgresStore) Close() {
	s.pool.Close()
}

// LoadDevices returns all ping-enabled devices.
func (s *PostgresStore) LoadDevices(ctx context.Context) ([]*pinger.Device, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, hostname, host(ip_address)::text, ping_interval, ping_enabled, status,
		       COALESCE(last_seen, '1970-01-01'::timestamptz),
		       COALESCE(last_rtt_ms, 0)
		FROM devices
		WHERE ping_enabled = TRUE
		ORDER BY hostname
	`)
	if err != nil {
		return nil, fmt.Errorf("query devices: %w", err)
	}
	defer rows.Close()

	var devices []*pinger.Device
	for rows.Next() {
		var d pinger.Device
		var ipStr string
		var intervalSec int

		err := rows.Scan(
			&d.ID, &d.Hostname, &ipStr, &intervalSec,
			&d.PingEnabled, &d.Status, &d.LastSeen, &d.LastRTT,
		)
		if err != nil {
			return nil, fmt.Errorf("scan device: %w", err)
		}

		d.IPAddress = net.ParseIP(ipStr)
		d.PingInterval = time.Duration(intervalSec) * time.Second
		devices = append(devices, &d)
	}

	return devices, rows.Err()
}

// UpdateDeviceStatus updates the device's current status in PostgreSQL.
func (s *PostgresStore) UpdateDeviceStatus(ctx context.Context, deviceID uuid.UUID, status string, lastSeen time.Time, rttMs float64) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE devices
		SET status = $1, last_seen = $2, last_rtt_ms = $3
		WHERE id = $4
	`, status, lastSeen, rttMs, deviceID)
	return err
}

// LoadServiceChecks returns all enabled service checks.
func (s *PostgresStore) LoadServiceChecks(ctx context.Context) ([]*checker.ServiceCheck, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, device_id, name, check_type, enabled,
		       target_host, COALESCE(target_port, 0), COALESCE(target_url, ''),
		       COALESCE(http_method, 'GET'), COALESCE(http_headers::text, '{}'),
		       COALESCE(http_body, ''), COALESCE(http_expected_status, 200),
		       COALESCE(http_content_match, ''), COALESCE(http_follow_redirects, true),
		       COALESCE(tls_warn_days, 30), COALESCE(tls_critical_days, 7),
		       check_interval, timeout, status
		FROM service_checks
		WHERE enabled = TRUE
		ORDER BY name
	`)
	if err != nil {
		return nil, fmt.Errorf("query service checks: %w", err)
	}
	defer rows.Close()

	var checks []*checker.ServiceCheck
	for rows.Next() {
		var sc checker.ServiceCheck
		var deviceID *uuid.UUID
		var intervalSec, timeoutSec int
		var headersJSON string

		err := rows.Scan(
			&sc.ID, &deviceID, &sc.Name, &sc.CheckType, &sc.Enabled,
			&sc.TargetHost, &sc.TargetPort, &sc.TargetURL,
			&sc.HTTPMethod, &headersJSON,
			&sc.HTTPBody, &sc.HTTPExpectedStatus,
			&sc.HTTPContentMatch, &sc.HTTPFollowRedirects,
			&sc.TLSWarnDays, &sc.TLSCriticalDays,
			&intervalSec, &timeoutSec, &sc.Status,
		)
		if err != nil {
			return nil, fmt.Errorf("scan service check: %w", err)
		}

		sc.DeviceID = deviceID
		sc.CheckInterval = time.Duration(intervalSec) * time.Second
		sc.Timeout = time.Duration(timeoutSec) * time.Second

		// Parse headers JSON
		sc.HTTPHeaders = make(map[string]string)
		if headersJSON != "" && headersJSON != "{}" {
			json.Unmarshal([]byte(headersJSON), &sc.HTTPHeaders)
		}

		checks = append(checks, &sc)
	}

	return checks, rows.Err()
}

// UpdateServiceCheckStatus updates the service check's current state in PostgreSQL.
func (s *PostgresStore) UpdateServiceCheckStatus(
	ctx context.Context,
	id uuid.UUID,
	status string,
	lastCheckAt time.Time,
	responseMs float64,
	lastError string,
	tlsExpiry *time.Time,
	tlsDaysRemaining *int,
	tlsIssuer string,
	tlsSubject string,
) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE service_checks
		SET status = $1, last_check_at = $2, last_response_ms = $3, last_error = $4,
		    tls_expiry_date = $5, tls_days_remaining = $6, tls_issuer = $7, tls_subject = $8
		WHERE id = $9
	`, status, lastCheckAt, responseMs, lastError,
		tlsExpiry, tlsDaysRemaining, tlsIssuer, tlsSubject, id)
	return err
}
