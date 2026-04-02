package store

import (
	"context"
	"fmt"
	"net"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
	"github.com/zenplus/poller/internal/config"
	"github.com/zenplus/poller/internal/pinger"
)

// ClickHouseStore handles metric writes to ClickHouse.
type ClickHouseStore struct {
	conn          driver.Conn
	batchSize     int
	flushInterval time.Duration
	buffer        chan *pinger.PingResult
	done          chan struct{}
}

// NewClickHouseStore connects to ClickHouse.
func NewClickHouseStore(cfg *config.Config) (*ClickHouseStore, error) {
	conn, err := clickhouse.Open(&clickhouse.Options{
		Addr: []string{fmt.Sprintf("%s:%d", cfg.ClickHouse.Host, cfg.ClickHouse.Port)},
		Auth: clickhouse.Auth{
			Database: cfg.ClickHouse.Database,
			Username: cfg.ClickHouse.User,
			Password: cfg.ClickHouse.Password,
		},
		Settings: clickhouse.Settings{
			"max_execution_time": 60,
		},
		Compression: &clickhouse.Compression{
			Method: clickhouse.CompressionLZ4,
		},
	})
	if err != nil {
		return nil, fmt.Errorf("connect to clickhouse: %w", err)
	}

	if err := conn.Ping(context.Background()); err != nil {
		return nil, fmt.Errorf("ping clickhouse: %w", err)
	}

	s := &ClickHouseStore{
		conn:          conn,
		batchSize:     cfg.ClickHouse.BatchSize,
		flushInterval: cfg.ClickHouse.FlushInterval,
		buffer:        make(chan *pinger.PingResult, cfg.ClickHouse.BatchSize*2),
		done:          make(chan struct{}),
	}

	return s, nil
}

// Close closes the ClickHouse connection.
func (s *ClickHouseStore) Close() {
	close(s.done)
	s.conn.Close()
}

// WriteResult queues a ping result for batch insertion.
func (s *ClickHouseStore) WriteResult(result *pinger.PingResult) {
	select {
	case s.buffer <- result:
	default:
		// Buffer full, drop oldest (shouldn't happen with proper sizing)
	}
}

// RunBatchWriter starts the background batch writer goroutine.
func (s *ClickHouseStore) RunBatchWriter(ctx context.Context) {
	ticker := time.NewTicker(s.flushInterval)
	defer ticker.Stop()

	batch := make([]*pinger.PingResult, 0, s.batchSize)

	flush := func() {
		if len(batch) == 0 {
			return
		}
		if err := s.insertBatch(ctx, batch); err != nil {
			fmt.Printf("ERROR: Failed to flush metrics batch: %v\n", err)
		}
		batch = batch[:0]
	}

	for {
		select {
		case <-ctx.Done():
			flush()
			return
		case <-s.done:
			flush()
			return
		case result := <-s.buffer:
			batch = append(batch, result)
			if len(batch) >= s.batchSize {
				flush()
			}
		case <-ticker.C:
			flush()
		}
	}
}

func (s *ClickHouseStore) insertBatch(ctx context.Context, results []*pinger.PingResult) error {
	batch, err := s.conn.PrepareBatch(ctx, `
		INSERT INTO ping_metrics (
			device_id, timestamp, is_up, rtt_ms, packet_loss, jitter_ms,
			min_rtt_ms, max_rtt_ms, packets_sent, packets_recv, poller_id, ip_address
		)
	`)
	if err != nil {
		return fmt.Errorf("prepare batch: %w", err)
	}

	for _, r := range results {
		isUp := uint8(0)
		if r.IsUp {
			isUp = 1
		}

		ipv4 := net.ParseIP(r.IPAddress.String()).To4()
		if ipv4 == nil {
			ipv4 = net.IPv4(0, 0, 0, 0).To4()
		}

		err := batch.Append(
			r.DeviceID,
			r.Timestamp,
			isUp,
			float64(r.RTT.Microseconds())/1000.0,
			r.PacketLoss,
			float64(r.Jitter.Microseconds())/1000.0,
			float64(r.MinRTT.Microseconds())/1000.0,
			float64(r.MaxRTT.Microseconds())/1000.0,
			uint16(r.Sent),
			uint16(r.Received),
			r.PollerID,
			ipv4,
		)
		if err != nil {
			return fmt.Errorf("append to batch: %w", err)
		}
	}

	return batch.Send()
}

// WriteStatusChange logs a status transition to ClickHouse.
func (s *ClickHouseStore) WriteStatusChange(ctx context.Context, sc *pinger.StatusChange, durationSec uint64) error {
	return s.conn.Exec(ctx, `
		INSERT INTO device_status_log (device_id, timestamp, old_status, new_status, reason, duration_sec)
		VALUES (?, ?, ?, ?, ?, ?)
	`, sc.DeviceID, sc.Timestamp, sc.OldStatus, sc.NewStatus, sc.Reason, durationSec)
}
