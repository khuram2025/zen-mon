package netflow

import (
	"encoding/json"
	"testing"
	"time"
)

// A snapshot survives JSON and restores templates, options templates,
// samplers and init times, so sampled data decodes scaled and with the right
// layout right after a collector restart.
func TestCacheSnapshotRestore(t *testing.T) {
	rec := basic4("10.0.0.1", "10.0.0.2", 1500, 3, 6)
	exporter := "2001:db8::40"
	before := NewV9TemplateCache()
	opt := v9OptionsTemplateSet(300, []V9TemplateField{{Type: 1, Length: 4}}, []V9TemplateField{{Type: ieSamplingInterval, Length: 4}})
	optData := append(make([]byte, 4), 0, 0, 0, 100)
	pkt := v9Packet(7, 1, v9TemplateSet(256, fieldsOf(rec)), opt, v9Set(300, optData))
	if _, _, err := ParseV9(pkt, addr(exporter), "t", testReceivedAt, before); err != nil {
		t.Fatal(err)
	}
	before.PutInitTime(addr(exporter), 7, 1_699_000_000_000)
	v0 := before.Version()
	if _, _, err := ParseV9(pkt, addr(exporter), "t", testReceivedAt, before); err != nil {
		t.Fatal(err)
	}
	if before.Version() != v0 {
		t.Fatal("re-announcing known templates and the same sampler changed the version")
	}

	data, err := json.Marshal(before.Snapshot())
	if err != nil {
		t.Fatal(err)
	}
	var snap CacheSnapshot
	if err := json.Unmarshal(data, &snap); err != nil {
		t.Fatal(err)
	}
	after := NewV9TemplateCache()
	tpls, samplers := after.Restore(snap)
	if tpls != 2 || samplers != 1 {
		t.Fatalf("restored %d templates, %d samplers", tpls, samplers)
	}
	if ms, ok := after.InitTime(addr(exporter), 7); !ok || ms != 1_699_000_000_000 {
		t.Fatalf("init time: %d %v", ms, ok)
	}
	dataOnly := v9Packet(7, 2, v9Set(256, encodeRecords(t, rec)))
	recs, st, err := ParseV9(dataOnly, addr(exporter), "t", testReceivedAt, after)
	if err != nil || len(recs) != 1 || st.DataSetsWaiting != 0 {
		t.Fatalf("decode after restore: err=%v records=%d stats=%+v", err, len(recs), st)
	}
	if recs[0].SamplingInterval != 100 || recs[0].Bytes != 1500 || recs[0].DstPort != 443 {
		t.Fatalf("restored layout/sampler: %+v", recs[0])
	}
	if after.templatesPer[addr(exporter)] != 2 || len(after.dataIDs) != 1 || len(after.optionIDs) != 1 {
		t.Fatalf("bookkeeping not restored: %v %d %d", after.templatesPer, len(after.dataIDs), len(after.optionIDs))
	}
}

// Restore applies the TTL to the saved announcement times and never replaces
// what the cache already learned (a fresher announcement wins).
func TestCacheRestoreRespectsTTLAndExisting(t *testing.T) {
	fields := fieldsOf(basic4("10.0.0.1", "10.0.0.2", 1, 1, 6))
	old := NewV9TemplateCache()
	base := time.Now()
	old.now = func() time.Time { return base }
	old.Put(addr("192.0.2.1"), 1, V9Template{ID: 256, Fields: fields})
	old.Put(addr("192.0.2.2"), 1, V9Template{ID: 256, Fields: fields})
	snap := old.Snapshot()

	after := NewV9TemplateCache()
	after.now = func() time.Time { return base.Add(v9TemplateTTL + time.Minute) }
	if n, _ := after.Restore(snap); n != 0 {
		t.Fatalf("restored %d expired templates", n)
	}

	fresh := NewV9TemplateCache()
	other := []V9TemplateField{{Type: ieSrcIPv4, Length: 4}, {Type: ieDstIPv4, Length: 4}, {Type: ieBytes, Length: 8}}
	fresh.Put(addr("192.0.2.1"), 1, V9Template{ID: 256, Fields: other})
	if n, _ := fresh.Restore(snap); n != 1 {
		t.Fatalf("restored %d, want only the template not learned yet", n)
	}
	if tpl, _ := fresh.Get(addr("192.0.2.1"), 1, 256); len(tpl.Fields) != len(other) {
		t.Fatal("a saved template replaced a fresher announcement")
	}
}

// X-08: per-ID samplers survive a restart as per-ID samplers (still
// ambiguous for untagged records), and the ID-0 "default" that pre-X-08
// snapshots copied from the last per-ID sampler is not restored as a domain
// rate; an explicit default is.
func TestSnapshotKeepsPerIDSamplersAndDropsLegacyCopiedDefault(t *testing.T) {
	exp := addr("192.0.2.95")
	before := NewV9TemplateCache()
	before.Put(exp, 7, V9Template{ID: 256, Fields: fieldsOf(basic4("10.0.0.1", "10.0.0.2", 1, 1, 6))})
	before.PutSampler(exp, 7, 1, true, 100)
	before.PutSampler(exp, 7, 2, true, 1000)
	data, err := json.Marshal(before.Snapshot())
	if err != nil {
		t.Fatal(err)
	}
	var snap CacheSnapshot
	if err := json.Unmarshal(data, &snap); err != nil {
		t.Fatal(err)
	}
	after := NewV9TemplateCache()
	if _, n := after.Restore(snap); n != 2 {
		t.Fatalf("restored %d samplers, want 2", n)
	}
	if n, st := after.ResolveSampler(exp, 7, 2, true); st != SamplerKnown || n != 1000 {
		t.Fatalf("tagged after restore: %d %v", n, st)
	}
	if _, st := after.ResolveSampler(exp, 7, 0, false); st != SamplerAmbiguous {
		t.Fatalf("untagged after restore: %v", st)
	}

	// A pre-X-08 snapshot: per-ID samplers without per_id, plus the copied
	// default (ID 0, not explicit).
	legacy := CacheSnapshot{Templates: snap.Templates, Samplers: []SamplerSnapshot{
		{Exporter: exp, Domain: 7, ID: 1, Interval: 100},
		{Exporter: exp, Domain: 7, ID: 2, Interval: 1000},
		{Exporter: exp, Domain: 7, ID: 0, Interval: 1000},
	}}
	old := NewV9TemplateCache()
	if _, n := old.Restore(legacy); n != 2 {
		t.Fatalf("legacy restore kept %d samplers, want 2 (copied default dropped)", n)
	}
	if _, st := old.ResolveSampler(exp, 7, 0, false); st != SamplerAmbiguous {
		t.Fatalf("legacy copied default still applies: %v", st)
	}
	legacy.Samplers = append(legacy.Samplers[:2], SamplerSnapshot{Exporter: exp, Domain: 7, Interval: 10, Explicit: true})
	withDefault := NewV9TemplateCache()
	withDefault.Restore(legacy)
	if n, st := withDefault.ResolveSampler(exp, 7, 0, false); st != SamplerKnown || n != 10 {
		t.Fatalf("explicit default after restore: %d %v", n, st)
	}
	// Pruning the domain drops the per-ID rate index with the samplers.
	withDefault.now = func() time.Time { return time.Now().Add(48 * time.Hour) }
	withDefault.Prune()
	if len(withDefault.samplers) != 0 || len(withDefault.idRates) != 0 {
		t.Fatalf("prune left samplers=%d rate index=%v", len(withDefault.samplers), withDefault.idRates)
	}
}
