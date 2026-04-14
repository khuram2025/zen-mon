package store

import (
	"context"
	"fmt"
	"net"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
	"github.com/zenplus/poller/internal/checker/snmp"
	"github.com/zenplus/poller/internal/config"
	"github.com/zenplus/poller/internal/pinger"
)

// ClickHouseStore handles metric writes to ClickHouse.
type ClickHouseStore struct {
	conn            driver.Conn
	batchSize       int
	flushInterval   time.Duration
	buffer          chan *pinger.PingResult
	serviceBuffer   chan *checker.ServiceCheckResult
	snmpBuffer      chan snmp.MetricSample
	snmpIfBuffer    chan snmp.InterfaceSample
	trapBuffer      chan snmp.TrapRecord
	done            chan struct{}
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
		serviceBuffer: make(chan *checker.ServiceCheckResult, cfg.ClickHouse.BatchSize*2),
		snmpBuffer:    make(chan snmp.MetricSample, cfg.ClickHouse.BatchSize*4),
		snmpIfBuffer:  make(chan snmp.InterfaceSample, cfg.ClickHouse.BatchSize*4),
		trapBuffer:    make(chan snmp.TrapRecord, 1024),
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
	}
}

// RunBatchWriter starts the background batch writer goroutine for ping metrics.
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

// --- Service Check Methods ---

// WriteServiceResult queues a service check result for batch insertion.
func (s *ClickHouseStore) WriteServiceResult(result *checker.ServiceCheckResult) {
	select {
	case s.serviceBuffer <- result:
	default:
	}
}

// RunServiceBatchWriter starts the background batch writer for service metrics.
func (s *ClickHouseStore) RunServiceBatchWriter(ctx context.Context) {
	ticker := time.NewTicker(s.flushInterval)
	defer ticker.Stop()

	batch := make([]*checker.ServiceCheckResult, 0, s.batchSize)

	flush := func() {
		if len(batch) == 0 {
			return
		}
		if err := s.insertServiceBatch(ctx, batch); err != nil {
			fmt.Printf("ERROR: Failed to flush service metrics batch: %v\n", err)
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
		case result := <-s.serviceBuffer:
			batch = append(batch, result)
			if len(batch) >= s.batchSize {
				flush()
			}
		case <-ticker.C:
			flush()
		}
	}
}

func (s *ClickHouseStore) insertServiceBatch(ctx context.Context, results []*checker.ServiceCheckResult) error {
	batch, err := s.conn.PrepareBatch(ctx, `
		INSERT INTO service_metrics (
			service_check_id, device_id, timestamp, check_type, is_up,
			response_ms, status_code, tls_days_remaining, tls_valid,
			content_matched, error_message, poller_id
		)
	`)
	if err != nil {
		return fmt.Errorf("prepare service batch: %w", err)
	}

	for _, r := range results {
		isUp := uint8(0)
		if r.IsUp {
			isUp = 1
		}

		deviceID := &uuid.Nil
		if r.DeviceID != nil {
			deviceID = r.DeviceID
		}

		var statusCode *uint16
		if r.StatusCode > 0 {
			sc := uint16(r.StatusCode)
			statusCode = &sc
		}

		var tlsDays *int32
		if r.TLSDaysRemaining != nil {
			d := int32(*r.TLSDaysRemaining)
			tlsDays = &d
		}

		var tlsValid *uint8
		if r.TLSValid != nil {
			v := uint8(0)
			if *r.TLSValid {
				v = 1
			}
			tlsValid = &v
		}

		var contentMatched *uint8
		if r.ContentMatched != nil {
			v := uint8(0)
			if *r.ContentMatched {
				v = 1
			}
			contentMatched = &v
		}

		var errMsg *string
		if r.Error != "" {
			errMsg = &r.Error
		}

		err := batch.Append(
			r.ServiceCheckID,
			deviceID,
			r.Timestamp,
			r.CheckType,
			isUp,
			float64(r.ResponseTime.Microseconds())/1000.0,
			statusCode,
			tlsDays,
			tlsValid,
			contentMatched,
			errMsg,
			r.PollerID,
		)
		if err != nil {
			return fmt.Errorf("append to service batch: %w", err)
		}
	}

	return batch.Send()
}

// --- SNMP Methods ---

// WriteSNMPMetric queues a scalar SNMP metric (CPU, memory, temperature…).
// Non-blocking: drops the sample if the buffer is full. Under normal load
// the buffer capacity is batchSize*4, so this only matters during an
// outage when CH writes are stuck.
func (s *ClickHouseStore) WriteSNMPMetric(m snmp.MetricSample) {
	select {
	case s.snmpBuffer <- m:
	default:
	}
}

// WriteSNMPIfMetric queues an interface-counter snapshot.
func (s *ClickHouseStore) WriteSNMPIfMetric(m snmp.InterfaceSample) {
	select {
	case s.snmpIfBuffer <- m:
	default:
	}
}

// RunSNMPBatchWriter starts the SNMP metric batch writer. Runs until
// the context is cancelled or Close() is called.
func (s *ClickHouseStore) RunSNMPBatchWriter(ctx context.Context) {
	ticker := time.NewTicker(s.flushInterval)
	defer ticker.Stop()

	scalarBatch := make([]snmp.MetricSample, 0, s.batchSize)
	ifBatch := make([]snmp.InterfaceSample, 0, s.batchSize)

	flushScalars := func() {
		if len(scalarBatch) == 0 {
			return
		}
		if err := s.insertSNMPBatch(ctx, scalarBatch); err != nil {
			fmt.Printf("ERROR: Failed to flush snmp_metrics batch: %v\n", err)
		}
		scalarBatch = scalarBatch[:0]
	}
	flushIf := func() {
		if len(ifBatch) == 0 {
			return
		}
		if err := s.insertSNMPIfBatch(ctx, ifBatch); err != nil {
			fmt.Printf("ERROR: Failed to flush snmp_if_metrics batch: %v\n", err)
		}
		ifBatch = ifBatch[:0]
	}

	for {
		select {
		case <-ctx.Done():
			flushScalars()
			flushIf()
			return
		case <-s.done:
			flushScalars()
			flushIf()
			return
		case m := <-s.snmpBuffer:
			scalarBatch = append(scalarBatch, m)
			if len(scalarBatch) >= s.batchSize {
				flushScalars()
			}
		case m := <-s.snmpIfBuffer:
			ifBatch = append(ifBatch, m)
			if len(ifBatch) >= s.batchSize {
				flushIf()
			}
		case <-ticker.C:
			flushScalars()
			flushIf()
		}
	}
}

func (s *ClickHouseStore) insertSNMPBatch(ctx context.Context, samples []snmp.MetricSample) error {
	batch, err := s.conn.PrepareBatch(ctx, `
		INSERT INTO snmp_metrics (device_id, metric_key, value, unit, timestamp, poller_id)
	`)
	if err != nil {
		return fmt.Errorf("prepare snmp batch: %w", err)
	}
	for _, m := range samples {
		if err := batch.Append(m.DeviceID, m.Key, m.Value, m.Unit, m.Timestamp, m.PollerID); err != nil {
			return fmt.Errorf("append snmp sample: %w", err)
		}
	}
	return batch.Send()
}

func (s *ClickHouseStore) insertSNMPIfBatch(ctx context.Context, samples []snmp.InterfaceSample) error {
	batch, err := s.conn.PrepareBatch(ctx, `
		INSERT INTO snmp_if_metrics (
		    device_id, if_index, timestamp,
		    in_octets, out_octets, in_errors, out_errors, in_discards, out_discards,
		    in_ucast_pkts, out_ucast_pkts, oper_status, in_bps, out_bps, poller_id
		)
	`)
	if err != nil {
		return fmt.Errorf("prepare snmp_if batch: %w", err)
	}
	for _, m := range samples {
		err := batch.Append(
			m.DeviceID, m.IfIndex, m.Timestamp,
			m.InOctets, m.OutOctets, m.InErrors, m.OutErrors, m.InDiscards, m.OutDiscards,
			m.InUcastPkts, m.OutUcastPkts, m.OperStatus, m.InBps, m.OutBps, m.PollerID,
		)
		if err != nil {
			return fmt.Errorf("append snmp_if sample: %w", err)
		}
	}
	return batch.Send()
}

// WriteTrap queues a decoded SNMP trap for batched insertion. This
// implements the snmp.TrapSink interface.
func (s *ClickHouseStore) WriteTrap(t snmp.TrapRecord) {
	select {
	case s.trapBuffer <- t:
	default:
	}
}

// RunTrapBatchWriter drains trapBuffer into zenplus.snmp_traps.
// Traps are bursty (linkDown storms, authFailure floods), so the
// batch size is modest and the flush interval tight.
func (s *ClickHouseStore) RunTrapBatchWriter(ctx context.Context) {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()

	batch := make([]snmp.TrapRecord, 0, 256)
	flush := func() {
		if len(batch) == 0 {
			return
		}
		if err := s.insertTrapBatch(ctx, batch); err != nil {
			fmt.Printf("ERROR: Failed to flush snmp_traps batch: %v\n", err)
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
		case t := <-s.trapBuffer:
			batch = append(batch, t)
			if len(batch) >= 256 {
				flush()
			}
		case <-ticker.C:
			flush()
		}
	}
}

func (s *ClickHouseStore) insertTrapBatch(ctx context.Context, traps []snmp.TrapRecord) error {
	batch, err := s.conn.PrepareBatch(ctx, `
		INSERT INTO snmp_traps (device_id, source_ip, trap_oid, trap_name, bindings, severity, message, timestamp, poller_id)
	`)
	if err != nil {
		return fmt.Errorf("prepare trap batch: %w", err)
	}
	for _, t := range traps {
		var devID *uuid.UUID
		if t.DeviceID != nil {
			devID = t.DeviceID
		}
		ipv4 := t.SourceIP.To4()
		if ipv4 == nil {
			ipv4 = net.IPv4(0, 0, 0, 0).To4()
		}
		err := batch.Append(
			devID, ipv4, t.TrapOID, t.TrapName, t.Bindings, t.Severity, t.Message, t.Timestamp, t.PollerID,
		)
		if err != nil {
			return fmt.Errorf("append trap: %w", err)
		}
	}
	return batch.Send()
}

// WriteServiceStatusChange logs a service check status transition.
func (s *ClickHouseStore) WriteServiceStatusChange(ctx context.Context, sc *checker.ServiceStatusChange, durationSec uint64) error {
	deviceIDStr := uuid.Nil.String()
	if sc.DeviceID != nil {
		deviceIDStr = sc.DeviceID.String()
	}

	query := fmt.Sprintf(`INSERT INTO service_status_log (service_check_id, device_id, timestamp, check_type, old_status, new_status, reason, duration_sec) VALUES ('%s', '%s', now(), '%s', '%s', '%s', '%s', %d)`,
		sc.ServiceCheckID.String(), deviceIDStr, sc.CheckType, sc.OldStatus, sc.NewStatus, sc.Reason, durationSec)
	return s.conn.Exec(ctx, query)
}
