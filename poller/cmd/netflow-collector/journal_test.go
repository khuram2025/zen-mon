package main

import (
	"bytes"
	"compress/zlib"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/netip"
	"net/url"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/zenplus/poller/internal/config"
	"github.com/zenplus/poller/internal/netflow"
	"github.com/zenplus/poller/internal/sflow"
	"github.com/zenplus/poller/internal/store"
)

type flowStaged struct {
	table, token string
	payload      []byte
}
type flowCapture struct {
	calls   []flowStaged
	failure bool
}

func (f *flowCapture) StageAnalytics(_ context.Context, table, token string, payload []byte) error {
	f.calls = append(f.calls, flowStaged{table, token, append([]byte(nil), payload...)})
	if f.failure {
		return errors.New("fixture unavailable")
	}
	return nil
}
func flowJournalFixture(ctx context.Context, s *chStore) error {
	now := time.Date(2026, 11, 1, 0, 0, 0, 123000000, time.UTC)
	flows := []netflow.Record{{Timestamp: now, ReceivedAt: now, CollectorID: "journal-fixture", ExporterAddr: netip.MustParseAddr("2001:db8::62"),
		SrcAddr6: net.ParseIP("2001:db8::10"), DstAddr6: net.ParseIP("2001:db8::20"), IPVersion: 6, InputSNMP: 70000,
		Bytes: ^uint64(0), Packets: 7, ExportProtocol: netflow.ProtocolIPFIX, FlowVersion: 10}}
	if err := s.InsertFlows(ctx, "flow-fixture-v1", flows); err != nil {
		return err
	}
	if err := s.InsertStats(ctx, "stats-fixture-v1", []statsRow{{TS: now, Exporter: "2001:db8::62", ClockOffsetMS: -1500, C: counters{Datagrams: 3}}}); err != nil {
		return err
	}
	info := netflow.TemplateInfo{Exporter: netip.MustParseAddr("2001:db8::62"), Domain: 6, Protocol: netflow.ProtocolIPFIX, ID: 256,
		Fields: []netflow.V9TemplateField{{Type: 27, Length: 16}, {Type: 28, Length: 16}, {Type: 0x8000 | 12, Length: 4, Enterprise: 9}}}
	if err := s.InsertTemplates(ctx, []templateRow{{info: info, firstSeen: now, lastSeen: now}}); err != nil {
		return err
	}
	if err := s.InsertAppNames(ctx, []appNameRow{{AppID: 13<<32 | 453, Name: "lab \u0627", Description: "fixture", Category: "test", Exporter: "2001:db8::62", UpdatedAt: now}}); err != nil {
		return err
	}
	return s.InsertIfCounters(ctx, "counter-fixture-v1", []ifCounterRow{{TS: now, Exporter: netip.MustParseAddr("2001:db8::90"), C: sflow.IfCounters{IfIndex: 70001, Speed: 10000000000, InOctets: ^uint64(0)}}})
}
func TestFlowJournalUsesAllFiveProductionInsertPaths(t *testing.T) {
	f := &flowCapture{}
	s := &chStore{journal: f, collectorID: "journal-fixture"}
	// nil native connection proves journal mode neither writes nor adopts DDL.
	if err := s.Ping(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := s.EnsureSchema(context.Background()); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if err := flowJournalFixture(context.Background(), s); err != nil {
			t.Fatal(err)
		}
	}
	if len(f.calls) != 10 {
		t.Fatal(len(f.calls))
	}
	for i := 0; i < 5; i++ {
		if f.calls[i].token != f.calls[i+5].token || !bytes.Equal(f.calls[i].payload, f.calls[i+5].payload) {
			t.Fatal("retry regrouped")
		}
	}
	z, err := zlib.NewReader(bytes.NewReader(f.calls[0].payload))
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := io.ReadAll(z)
	_ = z.Close()
	if !bytes.Contains(raw, []byte("18446744073709551615")) || !bytes.Contains(raw, []byte("2001:db8::10")) {
		t.Fatal("counter or IPv6 lost")
	}
}
func TestFlowJournalPreservesLogicalIdentityAndFreezesArrays(t *testing.T) {
	f := &flowCapture{}
	ctx := context.Background()
	makeBatch := func(token string, fields []uint16) {
		b, e := store.NewFlowJournalBatch(ctx, f, "INSERT INTO flow_templates (field_ids)", token)
		if e != nil {
			t.Fatal(e)
		}
		if e = b.Append(fields); e != nil {
			t.Fatal(e)
		}
		fields[0] = 99
		if e = b.Send(); e != nil {
			t.Fatal(e)
		}
	}
	makeBatch("one", []uint16{27, 28})
	makeBatch("two", []uint16{27, 28})
	makeBatch("one", []uint16{12, 28})
	if f.calls[0].token == f.calls[1].token || f.calls[0].token != f.calls[2].token {
		t.Fatal("logical identity replaced by content")
	}
	z, _ := zlib.NewReader(bytes.NewReader(f.calls[0].payload))
	raw, _ := io.ReadAll(z)
	_ = z.Close()
	var decoded any
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	if e := d.Decode(&decoded); e != nil {
		t.Fatal(e)
	}
	if bytes.Contains(raw, []byte("99")) || !bytes.Contains(raw, []byte("27")) {
		t.Fatal("caller mutation changed frozen array")
	}
	if _, e := store.NewFlowJournalBatch(ctx, f, "INSERT INTO users (password)", "one"); e == nil {
		t.Fatal("unsafe target admitted")
	}
	f.failure = true
	b, _ := store.NewFlowJournalBatch(ctx, f, "INSERT INTO flow_records (bytes)", "fail")
	_ = b.Append(uint64(1))
	if b.Send() == nil {
		t.Fatal("failed handoff acknowledged")
	}
}
func TestFlowJournalRequiresCommissionedMode(t *testing.T) {
	t.Setenv("HOST_INSERT_JOURNAL_ENABLED", "1")
	t.Setenv("ZENPLUS_CONTROLLER_GRANT", "")
	t.Setenv("NETFLOW_COLLECTOR_ID", "cell")
	c := &collector{cfg: collectorConfig{BatchSize: 1000}}
	if c.enableJournal(context.Background()) == nil {
		t.Fatal("uncommissioned journal accepted")
	}
}
func TestFlowJournalPrivatePostgres(t *testing.T) {
	dsn := os.Getenv("ZENPLUS_FLOW_JOURNAL_TEST_DSN")
	if dsn == "" {
		t.Skip("private PostgreSQL fixture required")
	}
	u, e := url.Parse(dsn)
	if e != nil {
		t.Fatal(e)
	}
	q := u.Query()
	port, e := strconv.Atoi(q.Get("port"))
	if e != nil || port == 5432 || !strings.HasPrefix(q.Get("host"), "/tmp/") || !strings.HasPrefix(strings.TrimPrefix(u.Path, "/"), "zp_t_") || u.User.Username() != "zenplus" {
		t.Fatal("disposable fixture required")
	}
	if _, hasPassword := u.User.Password(); hasPassword {
		t.Fatal("password not allowed for fixture")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	pg, e := store.NewPostgresStore(ctx, &config.Config{Postgres: config.PostgresConfig{Host: q.Get("host"), Port: port, Database: strings.TrimPrefix(u.Path, "/"), User: "zenplus", SSLMode: "disable"}})
	if e != nil {
		t.Fatal(e)
	}
	defer pg.Close()
	s := &chStore{journal: pg, collectorID: "journal-fixture"}
	e = flowJournalFixture(ctx, s)
	if os.Getenv("ZENPLUS_FLOW_JOURNAL_EXPECT_FAILURE") == "1" {
		if e == nil {
			t.Fatal("failed transaction acknowledged")
		}
		return
	}
	if e != nil {
		t.Fatal(e)
	}
	if e = flowJournalFixture(ctx, s); e != nil {
		t.Fatal(e)
	}
}
