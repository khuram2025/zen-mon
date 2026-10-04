package main

import (
	"context"
	"fmt"
	"net/netip"
	"slices"
	"sync"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

type templateRowKey struct {
	exporter netip.Addr
	domain   uint32
	protocol string
	id       uint16
}

type templateRow struct {
	info      netflow.TemplateInfo
	firstSeen time.Time
	lastSeen  time.Time
}

// templateRegistry writes the definition of every template an exporter
// announces to zenplus.flow_templates when it is learned or changes, and
// again at least daily while the exporter keeps announcing it (the decoder's
// cache decides when). Rows wait here until an insert succeeds, so a
// ClickHouse outage delays the registry instead of losing entries.
type templateRegistry struct {
	mu        sync.Mutex
	pending   map[templateRowKey]templateRow
	firstSeen map[templateRowKey]templateRow // definition + when this process first saw it
}

func newTemplateRegistry() *templateRegistry {
	return &templateRegistry{pending: make(map[templateRowKey]templateRow), firstSeen: make(map[templateRowKey]templateRow)}
}

// observe queues templates reported by a decoded datagram. first_seen is the
// first time this collector process saw the current definition; a changed
// layout starts a new first_seen.
func (t *templateRegistry) observe(infos []netflow.TemplateInfo, at time.Time) {
	if len(infos) == 0 {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	for _, info := range infos {
		k := templateRowKey{info.Exporter, info.Domain, info.Protocol, info.ID}
		first := at
		if prev, ok := t.firstSeen[k]; ok && prev.info.IsOptions == info.IsOptions && slices.Equal(prev.info.Fields, info.Fields) {
			first = prev.firstSeen
		}
		t.firstSeen[k] = templateRow{info: info, firstSeen: first, lastSeen: at}
		t.pending[k] = templateRow{info: info, firstSeen: first, lastSeen: at}
	}
}

// flush writes pending rows; on failure they stay pending (unless a newer
// observation replaced them meanwhile).
func (t *templateRegistry) flush(ctx context.Context, insert func(context.Context, []templateRow) error) {
	t.mu.Lock()
	if len(t.pending) == 0 {
		t.mu.Unlock()
		return
	}
	rows := make([]templateRow, 0, len(t.pending))
	keys := make([]templateRowKey, 0, len(t.pending))
	for k, r := range t.pending {
		rows = append(rows, r)
		keys = append(keys, k)
	}
	t.pending = make(map[templateRowKey]templateRow)
	// Templates are re-announced at least daily while in use; forget the
	// first_seen of those that stopped two days ago.
	for k, r := range t.firstSeen {
		if time.Since(r.lastSeen) > 48*time.Hour {
			delete(t.firstSeen, k)
		}
	}
	t.mu.Unlock()

	if err := insert(ctx, rows); err != nil {
		fmt.Printf("WARN: netflow template registry write failed (%s); will retry\n", errorLabel(err))
		t.mu.Lock()
		for i, k := range keys {
			if _, newer := t.pending[k]; !newer {
				t.pending[k] = rows[i]
			}
		}
		t.mu.Unlock()
	}
}
