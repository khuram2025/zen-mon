package main

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

// appNameRefresh is how often an application definition that an exporter
// keeps announcing is written again although it did not change, so
// flow_app_names keeps current rows (and is repopulated if it was emptied).
const appNameRefresh = 24 * time.Hour

// maxAppNames bounds the registry. An NBAR2 protocol pack defines about 1,500
// applications; the bound only stops a misbehaving exporter from growing
// memory without limit.
const maxAppNames = 100_000

// appNameRow is one zenplus.flow_app_names row.
type appNameRow struct {
	AppID       uint64
	Name        string
	Description string
	Category    string
	Exporter    string
	UpdatedAt   time.Time
}

type appNameState struct {
	name, description, category string
	exporter                    string    // the exporter that last described it
	written                     time.Time // last successful write
	dirty                       bool
}

// appNameRegistry turns the application definitions exporters send in
// options data (CR-12) into zenplus.flow_app_names rows. Cisco sends the
// name/description and the category in separate option templates, and
// flow_app_names keeps one row per app_id (ReplacingMergeTree), so the parts
// are merged here and every row written carries the complete definition: a
// category-only row must never replace a known name. Rows are written when a
// definition is new or changed and at least daily while it is announced;
// definitions without a name are held until the name arrives. Rows that fail
// to be written stay pending, so an outage delays the table, not loses it.
type appNameRegistry struct {
	mu   sync.Mutex
	apps map[uint64]*appNameState
}

func newAppNameRegistry() *appNameRegistry {
	return &appNameRegistry{apps: make(map[uint64]*appNameState)}
}

func (a *appNameRegistry) observe(names []netflow.AppName, at time.Time) {
	if len(names) == 0 {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, n := range names {
		st := a.apps[n.AppID]
		if st == nil {
			if len(a.apps) >= maxAppNames {
				continue
			}
			st = &appNameState{}
			a.apps[n.AppID] = st
		}
		changed := false
		for _, f := range []struct {
			dst *string
			v   string
		}{{&st.name, n.Name}, {&st.description, n.Description}, {&st.category, n.Category}} {
			if f.v != "" && *f.dst != f.v {
				*f.dst, changed = f.v, true
			}
		}
		if changed || at.Sub(st.written) >= appNameRefresh {
			st.exporter, st.dirty = n.Exporter.String(), true
		}
	}
}

// flush writes the pending definitions that have a name.
func (a *appNameRegistry) flush(ctx context.Context, now time.Time, insert func(context.Context, []appNameRow) error) {
	a.mu.Lock()
	var rows []appNameRow
	for id, st := range a.apps {
		if st.dirty && st.name != "" {
			rows = append(rows, appNameRow{AppID: id, Name: st.name, Description: st.description, Category: st.category, Exporter: st.exporter, UpdatedAt: now})
			st.dirty = false
		}
	}
	a.mu.Unlock()
	if len(rows) == 0 {
		return
	}
	err := insert(ctx, rows)
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, r := range rows {
		st := a.apps[r.AppID]
		switch {
		case st == nil:
		case err != nil:
			st.dirty = true
		default:
			st.written = now
		}
	}
	if err != nil {
		fmt.Printf("WARN: netflow application names write failed (%s); will retry\n", errorLabel(err))
	}
}

// counts reports the known and still-unwritten definitions for /health.
func (a *appNameRegistry) counts() (known, pending int) {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, st := range a.apps {
		if st.name == "" {
			continue
		}
		known++
		if st.dirty {
			pending++
		}
	}
	return known, pending
}
