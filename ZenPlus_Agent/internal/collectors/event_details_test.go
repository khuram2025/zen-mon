package collectors

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"unicode/utf8"

	"zenplus-agent/internal/config"
)

const sampleEventXML = `<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System><Provider Name="Service Control Manager"/><EventID>7031</EventID><Level>2</Level><TimeCreated SystemTime="2026-11-01T08:00:01.1234567Z"/><EventRecordID>99</EventRecordID><Computer>server-01</Computer></System><EventData><Data Name="ServiceName">SQL &amp; Reports</Data><Data>failure</Data></EventData></Event>`

func TestDecodeDetailedEventPreservesIdentityAndObject(t *testing.T) {
	one, err := decodeDetailedEvent("System", sampleEventXML, strings.Repeat("ش", 100), 31)
	if err != nil {
		t.Fatal(err)
	}
	two, err := decodeDetailedEvent("System", sampleEventXML, "different language", 100)
	if err != nil {
		t.Fatal(err)
	}
	if one["event_uid"] != two["event_uid"] {
		t.Fatal("identity depends on rendered message")
	}
	lower, err := decodeDetailedEvent("system", sampleEventXML, "", 100)
	if err != nil || lower["event_uid"] != one["event_uid"] {
		t.Fatal("channel casing changed event identity")
	}
	if one["fields"].(map[string]string)["ServiceName"] != "SQL & Reports" {
		t.Fatal(one)
	}
	if len(one["message"].(string)) > 31 || !utf8.ValidString(one["message"].(string)) {
		t.Fatal("invalid bounded UTF-8")
	}
	if one["message_truncated"] != true || one["record_id"] != uint64(99) {
		t.Fatal(one)
	}
}

func TestDetailedCollectionBudgetAndCheckpoints(t *testing.T) {
	original := readDetailedEvents
	defer func() { readDetailedEvents = original }()
	readDetailedEvents = func(ctx context.Context, q detailQuery, cursor string, limit, messageBytes int) (detailRead, error) {
		out := detailRead{More: true}
		for i := 0; i < limit; i++ {
			out.Events = append(out.Events, detailEvent{Bookmark: fmt.Sprintf("%s:%d", q.Key, i), Data: map[string]any{"event_uid": fmt.Sprintf("%s:%d", q.Key, i), "message": strings.Repeat("x", 1000)}})
		}
		return out, nil
	}
	cfg := config.EventLogConfig{MaxEvents: 50, MaxBytes: 16384}
	r := Result{Inventory: map[string]any{}, Errors: map[string]string{}}
	collectEventDetails(context.Background(), cfg, nil, &r)
	if len(r.Metrics) == 0 || len(r.Metrics) > 50 {
		t.Fatal(len(r.Metrics))
	}
	status := r.Inventory["event_collection"].(map[string]any)
	if status["bytes"].(int) > 16384 {
		t.Fatal("byte budget exceeded")
	}
	if len(r.EventCursors) == 0 {
		t.Fatal("accepted records have no checkpoint")
	}
	for _, s := range status["queries"].([]map[string]any) {
		if s["backlog"] != true {
			t.Fatal("storm hidden as clean")
		}
	}
}

func TestDetailedCollectionDoesNotAdvanceUnqueuedEvent(t *testing.T) {
	original := readDetailedEvents
	defer func() { readDetailedEvents = original }()
	readDetailedEvents = func(ctx context.Context, q detailQuery, cursor string, limit, messageBytes int) (detailRead, error) {
		return detailRead{Events: []detailEvent{{Bookmark: "new", Data: map[string]any{"event_uid": "one", "message": strings.Repeat("x", 20000)}}}}, nil
	}
	queries, _ := detailQueries(config.EventLogConfig{})
	r := Result{Inventory: map[string]any{}, Errors: map[string]string{}}
	collectEventDetails(context.Background(), config.EventLogConfig{MaxBytes: 16384}, map[string]string{queries[0].Key: "old"}, &r)
	if len(r.Metrics) != 0 || r.EventCursors[queries[0].Key] != "old" {
		t.Fatal("checkpoint skipped an event that did not fit")
	}
}

func TestOverlappingQueriesProduceOneOccurrence(t *testing.T) {
	original := readDetailedEvents
	defer func() { readDetailedEvents = original }()
	readDetailedEvents = func(ctx context.Context, q detailQuery, cursor string, limit, messageBytes int) (detailRead, error) {
		return detailRead{Events: []detailEvent{{Bookmark: q.Key, Data: map[string]any{"event_uid": "same-occurrence"}}}}, nil
	}
	r := Result{Inventory: map[string]any{}, Errors: map[string]string{}}
	collectEventDetails(context.Background(), config.EventLogConfig{}, nil, &r)
	if len(r.Metrics) != 1 || len(r.EventCursors) < 2 {
		t.Fatalf("metrics=%d cursors=%d", len(r.Metrics), len(r.EventCursors))
	}
}

func TestRoleAuditAndLifecycleQueries(t *testing.T) {
	filters := []config.EventLogFilter{{Channel: "Security", Levels: []string{"audit"}, IDs: []int{4625}}}
	queries, err := detailQueries(config.EventLogConfig{Filters: &filters})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(queries[0].XPath, "1074") || !strings.Contains(queries[1].XPath, "Level=0") {
		t.Fatal(queries)
	}
}
