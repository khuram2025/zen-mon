package main

import (
	"encoding/binary"
	"net/netip"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// withoutFirstFlowSet drops the first flowset of a v9 datagram (here: the
// template), leaving a data-only datagram as an exporter sends between
// template refreshes.
func withoutFirstFlowSet(d []byte) []byte {
	n := int(binary.BigEndian.Uint16(d[22:24]))
	return append(append([]byte(nil), d[:20]...), d[20+n:]...)
}

func newSpoolCollector(t *testing.T, dir string) *collector {
	t.Helper()
	c := newTestCollector(clockClamp)
	sp, err := openDiskSpool(dir, 64<<20)
	if err != nil {
		t.Fatal(err)
	}
	c.cfg.SpoolDir, c.spool = dir, sp
	return c
}

// A restart must not make the collector forget the exporters' templates:
// data sets arriving before the next template refresh (up to 30 minutes on
// Cisco) are decoded with the saved templates instead of being dropped.
func TestTemplateStatePersistsAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	exporter := netip.MustParseAddr("192.0.2.40")
	now := time.Now().UTC()
	full := v9Datagram(4, 1, uint32(now.Unix()), uint64(now.UnixMilli()))

	before := newSpoolCollector(t, dir)
	before.handleDatagram(full, exporter, now)
	before.saveTemplateState(now, false)
	if _, err := os.Stat(filepath.Join(dir, templateStateFile)); err != nil {
		t.Fatalf("template state not saved: %v", err)
	}

	// Without the saved state the data-only datagram waits for a template.
	fresh := newTestCollector(clockClamp)
	fresh.handleDatagram(withoutFirstFlowSet(full), exporter, now)
	if totals, _, _ := fresh.stats.snapshot(); totals.DataSetsWaitingTemplate != 1 || totals.RecordsDecoded != 0 {
		t.Fatalf("control: waiting=%d decoded=%d", totals.DataSetsWaitingTemplate, totals.RecordsDecoded)
	}

	after := newSpoolCollector(t, dir)
	after.loadTemplateState()
	after.handleDatagram(withoutFirstFlowSet(full), exporter, now.Add(time.Second))
	recs := drain(t, after)
	totals, _, _ := after.stats.snapshot()
	if len(recs) != 1 || recs[0].Bytes != 5000 || totals.DataSetsWaitingTemplate != 0 {
		t.Fatalf("after restart: records=%+v waiting=%d", recs, totals.DataSetsWaitingTemplate)
	}
}

// The file is rewritten only when a cache changed (or hourly), not on every
// stats interval; re-announcing a known template is not a change.
func TestTemplateStateSavedOnChangeOnly(t *testing.T) {
	dir := t.TempDir()
	c := newSpoolCollector(t, dir)
	exporter := netip.MustParseAddr("192.0.2.41")
	now := time.Now().UTC()
	c.handleDatagram(v9Datagram(1, 1, uint32(now.Unix()), uint64(now.UnixMilli())), exporter, now)
	c.saveTemplateState(now, false)
	path := filepath.Join(dir, templateStateFile)
	info1, _ := os.Stat(path)
	savedAt := c.tplSavedAt

	c.handleDatagram(v9Datagram(1, 2, uint32(now.Unix()), uint64(now.UnixMilli())), exporter, now) // same template again
	c.saveTemplateState(now.Add(time.Minute), false)
	if c.tplSavedAt != savedAt {
		t.Fatal("unchanged caches were saved again")
	}
	c.saveTemplateState(now.Add(templateStateRefresh+time.Minute), false)
	if !c.tplSavedAt.After(savedAt) {
		t.Fatal("the hourly refresh did not save")
	}
	c.handleDatagram(v9Datagram(2, 1, uint32(now.Unix()), uint64(now.UnixMilli())), exporter, now) // new domain
	before := c.tplSavedAt
	c.saveTemplateState(now.Add(templateStateRefresh+2*time.Minute), false)
	if !c.tplSavedAt.After(before) {
		t.Fatal("a new template did not trigger a save")
	}
	if info2, _ := os.Stat(path); info1 == nil || info2 == nil || info2.Mode().Perm() != 0o640 {
		t.Fatalf("state file: %v %v", info1, info2)
	}
	// A corrupt file is ignored, not fatal.
	if err := os.WriteFile(path, []byte("{not json"), 0o640); err != nil {
		t.Fatal(err)
	}
	newSpoolCollector(t, dir).loadTemplateState()
}
