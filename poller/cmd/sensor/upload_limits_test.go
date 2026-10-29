package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"
	"time"

	"github.com/zenplus/poller/internal/sensorspool"
	"go.uber.org/zap"
)

func TestDrainEventBacklogDoesNotSendOversizeBatches(t *testing.T) {
	spool, err := sensorspool.Open(filepath.Join(t.TempDir(), "wal"), sensorspool.Options{})
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 501; i++ {
		ok, err := spool.Enqueue(sensorspool.Item{ID: fmt.Sprint(i), Path: "/api/v1/sensor/events", Payload: json.RawMessage(`{"type":"sensor_event"}`)})
		if err != nil || !ok {
			t.Fatalf("enqueue: %v %v", ok, err)
		}
	}
	accepted, rejected, requests := 0, 0, 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Items []json.RawMessage `json:"items"`
			Key   string            `json:"idempotency_key"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			w.WriteHeader(400)
			return
		}
		requests++
		if r.URL.Path != "/api/v1/sensor/events" || body.Key == "" {
			t.Error("missing endpoint or retry key")
		}
		if len(body.Items) > 200 {
			rejected++
			w.WriteHeader(422)
			return
		}
		accepted += len(body.Items)
		w.WriteHeader(200)
	}))
	defer server.Close()
	c := &client{baseURL: server.URL, httpClient: server.Client(), state: &state{SensorID: "fixture", APIKey: "fixture"}}
	if _, err := drainSpool(context.Background(), time.Minute, c, spool, zap.NewNop().Sugar()); err != nil {
		t.Fatal(err)
	}
	if accepted != 501 || rejected != 0 || requests != 3 || spool.Stats().Depth != 0 || spool.Stats().Dropped != 0 {
		t.Fatalf("accepted=%d rejected=%d requests=%d stats=%+v", accepted, rejected, requests, spool.Stats())
	}
}
