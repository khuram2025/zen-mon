package main

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
	"github.com/zenplus/poller/internal/netflow"
)

type collectorConfig struct {
	ID            string
	Listen        string
	HealthListen  string
	ClickHouseDSN clickhouse.Options
	BatchSize     int
	FlushInterval time.Duration
}

func main() {
	cfg := loadConfig()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	conn, err := clickhouse.Open(&cfg.ClickHouseDSN)
	if err != nil {
		fatalf("connect clickhouse: %v", err)
	}
	if err := conn.Ping(ctx); err != nil {
		fatalf("ping clickhouse: %v", err)
	}
	defer conn.Close()

	if err := ensureSchema(ctx, conn); err != nil {
		fmt.Printf("WARN: ensure clickhouse schema: %v\n", err)
	}

	c := &collector{
		cfg:         cfg,
		conn:        conn,
		records:     make(chan netflow.Record, cfg.BatchSize*4),
		v9Templates: netflow.NewV9TemplateCache(),
	}
	go c.runHealth(ctx)
	go c.runWriter(ctx)

	if err := c.runUDP(ctx); err != nil && ctx.Err() == nil {
		fatalf("collector stopped: %v", err)
	}
}

type collector struct {
	cfg         collectorConfig
	conn        driver.Conn
	records     chan netflow.Record
	v9Templates *netflow.V9TemplateCache
}

func (c *collector) runUDP(ctx context.Context) error {
	addr, err := net.ResolveUDPAddr("udp", c.cfg.Listen)
	if err != nil {
		return err
	}
	udp, err := net.ListenUDP("udp", addr)
	if err != nil {
		return err
	}
	defer udp.Close()

	fmt.Printf("ZenPlus NetFlow collector listening on %s\n", c.cfg.Listen)
	go func() {
		<-ctx.Done()
		_ = udp.Close()
	}()

	buf := make([]byte, 64*1024)
	for {
		n, remote, err := udp.ReadFromUDP(buf)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			fmt.Printf("WARN: udp read failed: %v\n", err)
			continue
		}
		receivedAt := time.Now().UTC()
		records, err := c.parsePacket(buf[:n], remote.IP, receivedAt)
		if err != nil {
			fmt.Printf("WARN: dropped flow packet from %s: %v\n", remote.IP, err)
			continue
		}
		for _, record := range records {
			select {
			case c.records <- record:
			default:
				fmt.Println("WARN: netflow record queue full, dropping record")
			}
		}
	}
}

func (c *collector) parsePacket(data []byte, exporter net.IP, receivedAt time.Time) ([]netflow.Record, error) {
	if len(data) < 2 {
		return nil, fmt.Errorf("packet too short")
	}
	version := uint16(data[0])<<8 | uint16(data[1])
	switch version {
	case 5:
		pkt, err := netflow.ParseV5(data, exporter, c.cfg.ID, receivedAt)
		if err != nil {
			return nil, err
		}
		return pkt.Records, nil
	case 9:
		records, stats, err := netflow.ParseV9(data, exporter, c.cfg.ID, receivedAt, c.v9Templates)
		if err != nil {
			return nil, err
		}
		if stats.TemplatesUpdated > 0 {
			fmt.Printf("INFO: netflow v9 templates updated from %s: %d\n", exporter, stats.TemplatesUpdated)
		}
		if stats.DataSetsWaiting > 0 && stats.RecordsDecoded == 0 {
			fmt.Printf("INFO: netflow v9 data from %s waiting for template\n", exporter)
		}
		return records, nil
	default:
		return nil, fmt.Errorf("unsupported netflow version %d", version)
	}
}

func (c *collector) runWriter(ctx context.Context) {
	ticker := time.NewTicker(c.cfg.FlushInterval)
	defer ticker.Stop()
	batch := make([]netflow.Record, 0, c.cfg.BatchSize)

	flush := func() {
		if len(batch) == 0 {
			return
		}
		if err := c.insert(ctx, batch); err != nil {
			fmt.Printf("ERROR: netflow insert failed: %v\n", err)
		}
		batch = batch[:0]
	}

	for {
		select {
		case <-ctx.Done():
			flush()
			return
		case r := <-c.records:
			batch = append(batch, r)
			if len(batch) >= c.cfg.BatchSize {
				flush()
			}
		case <-ticker.C:
			flush()
		}
	}
}

func (c *collector) insert(ctx context.Context, records []netflow.Record) error {
	batch, err := c.conn.PrepareBatch(ctx, `
		INSERT INTO flow_records (
			timestamp, received_at, collector_id, exporter_ip, flow_version,
			flow_sequence, engine_type, engine_id, sampling_interval,
			src_addr, dst_addr, next_hop, input_snmp, output_snmp,
			packets, bytes, first_switched_ms, last_switched_ms,
			src_port, dst_port, tcp_flags, protocol, tos,
			src_as, dst_as, src_mask, dst_mask
		)
	`)
	if err != nil {
		return err
	}
	for _, r := range records {
		if err := batch.Append(
			r.Timestamp, r.ReceivedAt, r.CollectorID, r.ExporterIP, r.FlowVersion,
			r.FlowSequence, r.EngineType, r.EngineID, r.SamplingInterval,
			r.SrcAddr, r.DstAddr, r.NextHop, r.InputSNMP, r.OutputSNMP,
			r.Packets, r.Bytes, r.FirstSwitchedMS, r.LastSwitchedMS,
			r.SrcPort, r.DstPort, r.TCPFlags, r.Protocol, r.TOS,
			r.SrcAS, r.DstAS, r.SrcMask, r.DstMask,
		); err != nil {
			return err
		}
	}
	return batch.Send()
}

// schemaStatements are the ClickHouse objects the collector writes to. They are
// applied with CREATE ... IF NOT EXISTS on startup so flow ingestion works on a
// fresh appliance without a clickhouse-client binary or a separate migration.
var schemaStatements = []string{
	`CREATE TABLE IF NOT EXISTS flow_records (
    timestamp          DateTime64(3, 'UTC'),
    received_at        DateTime64(3, 'UTC'),
    collector_id       LowCardinality(String),
    exporter_ip        IPv4,
    flow_version       UInt8,
    flow_sequence      UInt32,
    engine_type        UInt8,
    engine_id          UInt8,
    sampling_interval  UInt32,
    src_addr           IPv4,
    dst_addr           IPv4,
    next_hop           IPv4,
    input_snmp         UInt16,
    output_snmp        UInt16,
    packets            UInt64,
    bytes              UInt64,
    first_switched_ms  UInt64,
    last_switched_ms   UInt64,
    src_port           UInt16,
    dst_port           UInt16,
    tcp_flags          UInt8,
    protocol           UInt8,
    tos                UInt8,
    src_as             UInt32,
    dst_as             UInt32,
    src_mask           UInt8,
    dst_mask           UInt8
)
ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(timestamp)
ORDER BY (timestamp, exporter_ip, src_addr, dst_addr, protocol, dst_port)
TTL toDateTime(timestamp) + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192`,
	`CREATE TABLE IF NOT EXISTS flow_traffic_5m (
    timestamp     DateTime64(3, 'UTC'),
    exporter_ip   IPv4,
    protocol      UInt8,
    dst_port      UInt16,
    bytes         UInt64,
    packets       UInt64,
    flow_count    UInt64
)
ENGINE = SummingMergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (timestamp, exporter_ip, protocol, dst_port)
TTL toDateTime(timestamp) + INTERVAL 90 DAY DELETE`,
	`CREATE MATERIALIZED VIEW IF NOT EXISTS flow_traffic_5m_mv
TO flow_traffic_5m
AS SELECT
    toStartOfFiveMinutes(timestamp) AS timestamp,
    exporter_ip,
    protocol,
    dst_port,
    sum(bytes) AS bytes,
    sum(packets) AS packets,
    count() AS flow_count
FROM flow_records
GROUP BY timestamp, exporter_ip, protocol, dst_port`,
}

// ensureSchema creates the ClickHouse tables the collector depends on if they
// are missing. On an appliance where they already exist these statements are
// no-ops; errors are returned so the caller can log and continue.
func ensureSchema(ctx context.Context, conn driver.Conn) error {
	for _, stmt := range schemaStatements {
		if err := conn.Exec(ctx, stmt); err != nil {
			return fmt.Errorf("exec schema: %w", err)
		}
	}
	return nil
}

func (c *collector) runHealth(ctx context.Context) {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok","service":"zenplus-netflow-collector"}`))
	})
	srv := &http.Server{Addr: c.cfg.HealthListen, Handler: mux, ReadHeaderTimeout: 3 * time.Second}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fmt.Printf("WARN: health server stopped: %v\n", err)
	}
}

func loadConfig() collectorConfig {
	return collectorConfig{
		ID:            env("NETFLOW_COLLECTOR_ID", "netflow-01"),
		Listen:        env("NETFLOW_LISTEN", ":2055"),
		HealthListen:  env("NETFLOW_HEALTH_LISTEN", ":8091"),
		BatchSize:     envInt("NETFLOW_BATCH_SIZE", 1000),
		FlushInterval: time.Duration(envInt("NETFLOW_FLUSH_SECONDS", 5)) * time.Second,
		ClickHouseDSN: clickhouse.Options{
			Addr: []string{fmt.Sprintf("%s:%d", env("CLICKHOUSE_HOST", "localhost"), envInt("CLICKHOUSE_PORT", 9000))},
			Auth: clickhouse.Auth{
				Database: env("CLICKHOUSE_DB", "zenplus"),
				Username: env("CLICKHOUSE_USER", "default"),
				Password: env("CLICKHOUSE_PASSWORD", "clickhouse_dev"),
			},
			Settings: clickhouse.Settings{"max_execution_time": 60},
			Compression: &clickhouse.Compression{Method: clickhouse.CompressionLZ4},
		},
	}
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}
