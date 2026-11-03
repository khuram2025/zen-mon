package store

// The API recovery worker consumes this same bounded, typed journal format.
// Native poller queues are pre-admission buffers, not durable acknowledgements.
import (
	"bytes"
	"compress/zlib"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"reflect"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
)

const journalMaxRaw = 8 * 1024 * 1024

type analyticsBatch interface {
	Append(...any) error
	Send() error
	Abort() error
}

// AnalyticsBatch and AnalyticsBatchStager expose the bounded handoff to the
// native flow collector. They do not perform a ClickHouse write.
type AnalyticsBatch = analyticsBatch
type AnalyticsBatchStager = batchStager
type batchStager interface {
	StageAnalytics(context.Context, string, string, []byte) error
}
type batchStageFunc func(context.Context, string, string, []byte) error

func (f batchStageFunc) StageAnalytics(ctx context.Context, table, token string, payload []byte) error {
	return f(ctx, table, token, payload)
}

type journalBatch struct {
	ctx      context.Context
	table    string
	columns  []string
	rows     []any
	stage    batchStager
	aborted  bool
	identity string
}

var journalInsert = regexp.MustCompile(`(?is)^\s*INSERT\s+INTO\s+([a-z_][a-z0-9_]*)\s*\(([^)]+)\)\s*$`)
var journalColumn = regexp.MustCompile(`^[a-z_][a-z0-9_]*$`)

func (s *ClickHouseStore) EnableAnalyticsJournal(pg *PostgresStore) error {
	if pg == nil || s.batchSize < 1 || s.batchSize > 20000 || s.flushInterval <= 0 {
		return fmt.Errorf("invalid poller journal batching configuration")
	}
	s.journal = pg
	return nil
}
func (s *ClickHouseStore) prepareBatch(ctx context.Context, sql string) (analyticsBatch, error) {
	if s.journal == nil {
		return s.conn.PrepareBatch(ctx, sql)
	}
	return prepareJournalBatch(ctx, s.journal, sql, "", map[string]bool{"ping_metrics": true, "service_metrics": true, "snmp_metrics": true, "snmp_if_metrics": true, "snmp_traps": true, "device_status_log": true, "service_status_log": true})
}

// NewFlowJournalBatch accepts only the collector's five static insert targets.
// A supplied identity freezes the first payload even if a retry is reconstructed.
// Registries without a batch identity use their complete immutable content.
func NewFlowJournalBatch(ctx context.Context, stage AnalyticsBatchStager, sql, identity string) (AnalyticsBatch, error) {
	if stage == nil {
		return nil, fmt.Errorf("flow journal storage unavailable")
	}
	return prepareJournalBatch(ctx, stage, sql, identity, map[string]bool{"flow_records": true, "flow_collector_stats": true, "flow_templates": true, "flow_app_names": true, "flow_if_counters": true})
}

func prepareJournalBatch(ctx context.Context, stage batchStager, sql, identity string, allowed map[string]bool) (analyticsBatch, error) {
	match := journalInsert.FindStringSubmatch(sql)
	if match == nil {
		return nil, fmt.Errorf("unsupported journal insert")
	}
	if !allowed[match[1]] {
		return nil, fmt.Errorf("unsupported journal table")
	}
	columns := strings.Split(match[2], ",")
	for i, c := range columns {
		columns[i] = strings.TrimSpace(c)
		if !journalColumn.MatchString(columns[i]) {
			return nil, fmt.Errorf("invalid journal column")
		}
	}
	return &journalBatch{ctx: ctx, table: match[1], columns: columns, stage: stage, identity: identity}, nil
}

func journalValue(v any) (any, error) {
	if v == nil {
		return []any{"scalar", nil}, nil
	}
	switch x := v.(type) {
	case time.Time:
		return []any{"datetime", x.UTC().Format(time.RFC3339Nano)}, nil
	case uuid.UUID:
		return []any{"uuid", x.String()}, nil
	case net.IP:
		return []any{"scalar", x.String()}, nil
	}
	r := reflect.ValueOf(v)
	if r.Kind() == reflect.Pointer {
		if r.IsNil() {
			return []any{"scalar", nil}, nil
		}
		return journalValue(r.Elem().Interface())
	}
	switch r.Kind() {
	case reflect.Slice, reflect.Array:
		items := make([]any, r.Len())
		for i := 0; i < r.Len(); i++ {
			encoded, err := journalValue(r.Index(i).Interface())
			if err != nil {
				return nil, err
			}
			items[i] = encoded
		}
		return []any{"list", items}, nil
	case reflect.String, reflect.Bool, reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64, reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Float32, reflect.Float64:
		return []any{"scalar", v}, nil
	}
	return nil, fmt.Errorf("unsupported journal value type %T", v)
}
func (b *journalBatch) Append(values ...any) error {
	if b.aborted || len(b.rows) >= 20000 || len(values) != len(b.columns) {
		return fmt.Errorf("invalid journal row or row limit")
	}
	row := make([]any, len(values))
	for i, v := range values {
		encoded, err := journalValue(v)
		if err != nil {
			return err
		}
		row[i] = encoded
	}
	// Encoding now freezes pointer-backed values before the caller can reuse them.
	b.rows = append(b.rows, []any{"list", row})
	return nil
}
func (b *journalBatch) Abort() error { b.aborted = true; b.rows = nil; return nil }
func (b *journalBatch) Send() error {
	if b.aborted || len(b.rows) == 0 {
		return fmt.Errorf("empty or aborted journal batch")
	}
	columns := make([]any, len(b.columns))
	for i, c := range b.columns {
		columns[i] = []any{"scalar", c}
	}
	raw, err := json.Marshal([]any{"list", []any{[]any{"list", columns}, []any{"list", b.rows}}})
	if err != nil {
		return fmt.Errorf("encode journal batch: %w", err)
	}
	if len(raw) > journalMaxRaw {
		return fmt.Errorf("journal batch exceeds byte limit")
	}
	digest := sha256.Sum256(append(append([]byte(b.table), 0), raw...))
	token := "poller-journal-v1-" + hex.EncodeToString(digest[:])
	if b.identity != "" {
		identityHash := sha256.Sum256([]byte(b.table + "\x00" + b.identity))
		token = "flow-journal-v1-" + hex.EncodeToString(identityHash[:])
	}
	var compressed bytes.Buffer
	z := zlib.NewWriter(&compressed)
	if _, err = z.Write(raw); err != nil {
		return err
	}
	if err = z.Close(); err != nil {
		return err
	}
	// No ClickHouse write occurs here. Only the admitted recovery worker writes
	// the frozen block with this identity, including after a controller crash.
	return b.stage.StageAnalytics(b.ctx, b.table, token, compressed.Bytes())
}

type journalRefusal string

func (e journalRefusal) Error() string { return string(e) }

func (s *PostgresStore) StageAnalytics(ctx context.Context, table, token string, payload []byte) error {
	return s.stageAnalytics(ctx, table, token, payload, nil)
}

// Trap incidents and analytics share the same acknowledgement. Either the
// whole batch survives promotion or the producer retries with the same ID.
func (s *PostgresStore) stageAnalytics(ctx context.Context, table, token string, payload, traps []byte) (err error) {
	defer func() {
		if err != nil {
			if _, safe := err.(journalRefusal); !safe {
				err = fmt.Errorf("analytics journal storage unavailable (%T)", err)
			}
		}
	}()
	if len(payload) == 0 || len(payload) > journalMaxRaw+1024 {
		return fmt.Errorf("invalid journal payload size")
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = tx.Rollback(cleanup)
	}()
	tableHash := sha256.Sum256([]byte(table))
	tokenHash := sha256.Sum256([]byte(token))
	var locked bool
	if err = tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock($1,$2)`, int32(17524001), int32(binary.BigEndian.Uint32(tableHash[:4]))).Scan(&locked); err != nil {
		return err
	}
	if !locked {
		return journalRefusal("analytics journal table busy")
	}
	var exists bool
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM host_ingest_receipts WHERE table_name=$1 AND token_hash=$2)`, table, tokenHash[:]).Scan(&exists); err != nil {
		return err
	}
	if exists {
		return tx.Commit(ctx)
	}
	var pending, receipts int64
	if err = tx.QueryRow(ctx, `SELECT payload_bytes,receipts FROM host_ingest_capacity WHERE singleton FOR UPDATE`).Scan(&pending, &receipts); err != nil {
		return err
	}
	if pending+int64(len(payload)) > 512*1024*1024 || receipts >= 10000000 {
		return journalRefusal("analytics journal capacity exhausted")
	}
	var id int64
	if err = tx.QueryRow(ctx, `INSERT INTO host_ingest_batches(table_name,write_token,payload) VALUES($1,$2,$3) RETURNING id`, table, token, payload).Scan(&id); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `INSERT INTO host_ingest_receipts(table_name,token_hash,batch_id) VALUES($1,$2,$3)`, table, tokenHash[:], id); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `UPDATE host_ingest_capacity SET payload_bytes=payload_bytes+$1,receipts=receipts+1 WHERE singleton`, len(payload)); err != nil {
		return err
	}
	if len(traps) != 0 {
		var admitted bool
		if err = tx.QueryRow(ctx, `
			WITH staged AS (
				INSERT INTO snmp_trap_incident_inbox(event_id,source_key,observed_at,payload)
				SELECT (event->>'event_id')::uuid,
				       COALESCE(NULLIF(event->>'device_id',''),event->>'source_ip'),
				       (event->>'observed_at')::timestamptz,event - 'observed_at'
				FROM jsonb_array_elements($1::jsonb) AS event
				ON CONFLICT(event_id) DO NOTHING RETURNING payload_bytes
			), bytes AS (SELECT COALESCE(sum(payload_bytes),0) AS used FROM staged),
			admitted AS (
				UPDATE host_ingest_capacity SET trap_bytes=trap_bytes+used FROM bytes
				WHERE singleton AND trap_bytes+used<=33554432 RETURNING 1
			)
			SELECT EXISTS(SELECT 1 FROM admitted)
		`, string(traps)).Scan(&admitted); err != nil {
			return err
		}
		if !admitted {
			return journalRefusal("trap incident inbox capacity exhausted")
		}
	}
	return tx.Commit(ctx)
}
