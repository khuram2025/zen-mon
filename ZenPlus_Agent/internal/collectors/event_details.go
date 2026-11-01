package collectors

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"zenplus-agent/internal/config"
	"zenplus-agent/internal/model"
)

// A bookmark belongs to the exact query, not just the channel: adding a role
// or changing filters must not inherit a position from a narrower query.
type detailQuery struct{ Channel, XPath, Key string }
type detailEvent struct {
	Data     map[string]any
	Bookmark string
}
type detailRead struct {
	Events []detailEvent
	More   bool
	Gap    bool
}

type eventCursor struct {
	Bookmark string `json:"bookmark"`
	UID      string `json:"uid"`
}

func bounded(value, fallback, ceiling int) int {
	if value <= 0 {
		return fallback
	}
	return min(value, ceiling)
}

func detailQueries(cfg config.EventLogConfig) ([]detailQuery, error) {
	filters, err := validatedEventLogFilters(cfg)
	if err != nil {
		return nil, err
	}
	// A separate first query reserves collection capacity for lifecycle events.
	queries := []detailQuery{{Channel: "System", XPath: "*[System[(EventID=12 or EventID=13 or EventID=41 or EventID=1074 or EventID=6008)]]"}}
	for _, f := range filters {
		for _, level := range f.Levels {
			level = strings.ToLower(level)
			levelValue := eventLogLevelValues[level]
			if level == "audit" {
				levelValue = 0
			}
			base := fmt.Sprintf("Level=%d", levelValue)
			if len(f.IDs) == 0 {
				queries = append(queries, detailQuery{Channel: f.Channel, XPath: "*[System[" + base + "]]"})
			}
			for start := 0; start < len(f.IDs); start += eventIDsPerXPath {
				terms := []string{}
				for _, id := range f.IDs[start:min(start+eventIDsPerXPath, len(f.IDs))] {
					terms = append(terms, fmt.Sprintf("EventID=%d", id))
				}
				queries = append(queries, detailQuery{Channel: f.Channel, XPath: "*[System[" + base + " and (" + strings.Join(terms, " or ") + ")]]"})
			}
		}
	}
	for i := range queries {
		sum := sha256.Sum256([]byte(queries[i].Channel + "\x00" + queries[i].XPath))
		queries[i].Key = hex.EncodeToString(sum[:])
	}
	return queries, nil
}

// Kept as a seam for portable budget and replay tests. Production uses wevtapi.
var readDetailedEvents = readNativeDetailedEvents

func collectEventDetails(ctx context.Context, cfg config.EventLogConfig, cursors map[string]string, result *Result) {
	queries, err := detailQueries(cfg)
	if err != nil {
		result.Errors["event_log"] = err.Error()
		return
	}
	maxEvents := max(50, bounded(cfg.MaxEvents, 200, 1000))
	maxBytes := bounded(cfg.MaxBytes, 256*1024, 1024*1024)
	messageBytes := bounded(cfg.MaxMessageBytes, 2048, 8192)
	remaining := maxEvents
	usedBytes := 0
	seen := map[string]bool{}
	result.EventCursors = map[string]string{}
	statuses := []map[string]any{}
	for i, q := range queries {
		// Keep only active query keys; policy changes cannot grow state forever.
		if old := cursors[q.Key]; old != "" {
			result.EventCursors[q.Key] = old
		}
		status := map[string]any{"channel": q.Channel, "query": q.Key, "backlog": false, "gap": false, "collected": 0}
		quota := max(1, remaining/max(1, len(queries)-i))
		if i == 0 {
			quota = min(20, remaining)
		}
		if remaining <= 0 || usedBytes >= maxBytes {
			status["backlog"] = true
			statuses = append(statuses, status)
			continue
		}
		read, readErr := readDetailedEvents(ctx, q, cursors[q.Key], quota, messageBytes)
		status["backlog"] = read.More
		status["gap"] = read.Gap
		if read.Gap && readErr == nil && len(read.Events) == 0 {
			delete(result.EventCursors, q.Key)
		}
		if readErr != nil {
			status["error"] = truncateUTF8(readErr.Error(), 256)
			result.Errors["event_log:"+q.Channel] = readErr.Error()
		}
		accepted := 0
		for _, event := range read.Events {
			uid, _ := event.Data["event_uid"].(string)
			if seen[uid] {
				result.EventCursors[q.Key] = event.Bookmark
				continue
			}
			metric := model.Metric{Kind: "event_detail", Timestamp: time.Now().UTC(), Data: event.Data}
			encoded, err := json.Marshal(metric)
			if err != nil {
				status["error"] = "event serialization failed"
				break
			}
			if usedBytes+len(encoded) > maxBytes || remaining <= 0 {
				status["backlog"] = true
				break
			}
			seen[uid] = true
			usedBytes += len(encoded)
			remaining--
			accepted++
			result.Metrics = append(result.Metrics, metric)
			result.EventCursors[q.Key] = event.Bookmark
			status["last_event_at"] = event.Data["event_time"]
		}
		status["collected"] = accepted
		statuses = append(statuses, status)
	}
	result.Inventory["event_collection"] = map[string]any{"queries": statuses, "bytes": usedBytes, "events": maxEvents - remaining, "max_events": maxEvents, "max_bytes": maxBytes, "interval_s": max(60, cfg.IntervalSeconds), "checked_at": time.Now().UTC().Format(time.RFC3339)}
}

func truncateUTF8(s string, limit int) string {
	if len(s) <= limit {
		return s
	}
	if limit <= 0 {
		return ""
	}
	s = s[:limit]
	for !utf8.ValidString(s) {
		s = s[:len(s)-1]
	}
	return s
}

type eventXML struct {
	System struct {
		Provider struct {
			Name string `xml:"Name,attr"`
		} `xml:"Provider"`
		ID       int    `xml:"EventID"`
		Level    int    `xml:"Level"`
		RecordID uint64 `xml:"EventRecordID"`
		Time     struct {
			Value string `xml:"SystemTime,attr"`
		} `xml:"TimeCreated"`
		Computer string `xml:"Computer"`
		Channel  string `xml:"Channel"`
	} `xml:"System"`
	Data []struct {
		Name  string `xml:"Name,attr"`
		Value string `xml:",chardata"`
	} `xml:"EventData>Data"`
}

func decodeDetailedEvent(channel, raw, message string, messageBytes int) (map[string]any, error) {
	var ev eventXML
	if err := xml.Unmarshal([]byte(raw), &ev); err != nil {
		return nil, err
	}
	if ev.System.Channel != "" {
		channel = ev.System.Channel
	}
	if ev.System.RecordID == 0 || ev.System.Time.Value == "" || ev.System.Provider.Name == "" {
		return nil, fmt.Errorf("event system fields missing")
	}
	fields := map[string]string{}
	for i, d := range ev.Data {
		if i >= 24 {
			break
		}
		name := d.Name
		if name == "" {
			name = fmt.Sprintf("param%d", i+1)
		}
		fields[truncateUTF8(name, 64)] = truncateUTF8(d.Value, 256)
	}
	if message == "" {
		parts := []string{}
		for _, d := range ev.Data {
			if len(parts) >= 12 {
				break
			}
			parts = append(parts, truncateUTF8(d.Value, 256))
		}
		message = strings.Join(parts, " · ")
	}
	level := map[int]string{0: "audit", 1: "critical", 2: "error", 3: "warning", 4: "information", 5: "verbose"}[ev.System.Level]
	if level == "" {
		level = "information"
	}
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s\x00%d\x00%s", strings.ToLower(channel), ev.System.RecordID, ev.System.Time.Value)))
	return map[string]any{"event_uid": hex.EncodeToString(sum[:]), "log_name": channel, "event_id": ev.System.ID, "record_id": ev.System.RecordID, "event_time": ev.System.Time.Value, "provider": truncateUTF8(ev.System.Provider.Name, 256), "level": level, "computer": truncateUTF8(ev.System.Computer, 255), "message": truncateUTF8(message, messageBytes), "message_truncated": len(message) > messageBytes, "fields": fields}, nil
}
