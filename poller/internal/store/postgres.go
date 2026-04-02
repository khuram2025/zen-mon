package store

import (
	"context"
	"fmt"
	"net"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
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
