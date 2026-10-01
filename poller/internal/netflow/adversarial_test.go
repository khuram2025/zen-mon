package netflow

import (
	"encoding/binary"
	"fmt"
	"runtime"
	"testing"
	"time"
)

// Adversarial input: the collector listens on an open UDP port, so the cost
// of decoding a datagram (memory and CPU) must be bounded by its length, and
// what datagrams announce (templates, samplers) must not grow memory without
// limit.

// allocatedBy returns the bytes allocated while f runs.
func allocatedBy(f func()) uint64 {
	var before, after runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&before)
	f()
	runtime.ReadMemStats(&after)
	return after.TotalAlloc - before.TotalAlloc
}

// A template record claiming 65535 fields in an 8-byte set used to allocate
// room for 65535 field specifiers before noticing the set was too short: a
// datagram of 8,000 such records allocated about 4 GB.
func TestIPFIXForgedFieldCountsDoNotAllocate(t *testing.T) {
	var sets []byte
	for len(sets)+8 <= 65000 {
		var c []byte
		c = binary.BigEndian.AppendUint16(c, 256)
		c = binary.BigEndian.AppendUint16(c, 0xFFFF)
		sets = append(sets, ipfixSet(2, c)...)
	}
	msg := ipfixMsg(1, 1, sets)
	cache := NewV9TemplateCache()
	n := allocatedBy(func() {
		if _, _, err := ParseIPFIX(msg, addr("192.0.2.9"), "t", testReceivedAt, cache); err != nil {
			t.Fatal(err)
		}
	})
	if n > 4<<20 {
		t.Fatalf("a %d-byte datagram allocated %d bytes", len(msg), n)
	}
	if active := cache.ActiveTemplates(); len(active) != 0 {
		t.Fatalf("truncated template records were stored: %v", active)
	}
}

// A template whose record is one byte long must not make a 64 KB data set
// reserve 65,000 records (28 MB) up front.
func TestTinyRecordTemplateDoesNotPreallocate(t *testing.T) {
	cache := NewV9TemplateCache()
	tpl := v9Packet(1, 1, v9TemplateSet(300, []V9TemplateField{{Type: ieProtocol, Length: 1}}))
	if _, _, err := ParseV9(tpl, addr("192.0.2.9"), "t", testReceivedAt, cache); err != nil {
		t.Fatal(err)
	}
	data := v9Packet(1, 2, v9Set(300, make([]byte, 65000)))
	var st V9Stats
	n := allocatedBy(func() {
		var err error
		if _, st, err = ParseV9(data, addr("192.0.2.9"), "t", testReceivedAt, cache); err != nil {
			t.Fatal(err)
		}
	})
	// No record has addresses: all are dropped as unsupported, none stored.
	if st.RecordsUnsupported != 65000 || st.RecordsDecoded != 0 {
		t.Fatalf("stats: %+v", st)
	}
	if n > 2<<20 {
		t.Fatalf("a %d-byte datagram allocated %d bytes", len(data), n)
	}
}

// Zero-length fields occupy no bytes. A template of thousands of them plus
// one real field used to cost thousands of loop iterations per data byte
// (about a billion per datagram); they are dropped from the decode layout,
// and records decode exactly as without them.
func TestZeroLengthFieldsDroppedFromLayout(t *testing.T) {
	rec := basic4("10.0.0.1", "10.0.0.2", 1500, 3, 6)
	fields := fieldsOf(rec)
	padded := make([]V9TemplateField, 0, 16000+len(fields))
	for i := 0; i < 16000; i++ {
		padded = append(padded, V9TemplateField{Type: ieTOS, Length: 0})
	}
	padded = append(padded, fields...)

	cache := NewV9TemplateCache()
	body := encodeRecords(t, rec, rec)
	pkt := ipfixMsg(5, 1, ipfixTemplate(256, padded), ipfixSet(256, body))
	start := time.Now()
	recs, st, err := ParseIPFIX(pkt, addr("192.0.2.9"), "t", testReceivedAt, cache)
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) != 2 || st.DataRecords != 2 || recs[0].Bytes != 1500 || recs[0].DstPort != 443 {
		t.Fatalf("records=%d stats=%+v first=%+v", len(recs), st, recs)
	}
	// A record decoded with the padded template carries no TOS presence: the
	// zero-length TOS fields sent nothing.
	if recs[0].FieldsPresent&FieldTOS != 0 {
		t.Fatalf("zero-length field reported as present: %032b", recs[0].FieldsPresent)
	}
	tpl, ok := cache.Get(addr("192.0.2.9"), 5, 256)
	if !ok || len(tpl.Fields) != len(padded) || len(tpl.dec.fields) != len(fields) || tpl.dec.minLen != 21 {
		t.Fatalf("layout: ok=%v fields=%d decode=%d minLen=%d", ok, len(tpl.Fields), len(tpl.dec.fields), tpl.dec.minLen)
	}

	// The worst case: one 1-byte field after 16,000 zero-length ones and a
	// 64 KB data set. Linear in the datagram, this is a few milliseconds.
	worst := append(append([]V9TemplateField(nil), padded[:16000]...), V9TemplateField{Type: ieProtocol, Length: 1})
	for _, msg := range [][]byte{
		ipfixMsg(5, 2, ipfixTemplate(257, worst)),
		ipfixMsg(5, 2, ipfixSet(257, make([]byte, 64000))),
	} {
		if _, _, err := ParseIPFIX(msg, addr("192.0.2.9"), "t", testReceivedAt, cache); err != nil {
			t.Fatal(err)
		}
	}
	if d := time.Since(start); d > 5*time.Second {
		t.Fatalf("decoding took %v", d)
	}
	if tpl, _ := cache.Get(addr("192.0.2.9"), 5, 257); len(tpl.dec.fields) != 1 {
		t.Fatalf("zero-length fields kept in the decode layout: %d", len(tpl.dec.fields))
	}
}

// A template made only of zero-length fields cannot describe a record and is
// not stored (its data would loop on records of no length).
func TestAllZeroLengthTemplateIgnored(t *testing.T) {
	cache := NewV9TemplateCache()
	pkt := v9Packet(1, 1, v9TemplateSet(300, []V9TemplateField{{Type: ieSrcIPv4, Length: 0}, {Type: ieDstIPv4, Length: 0}}), v9Set(300, make([]byte, 40)))
	recs, st, err := ParseV9(pkt, addr("192.0.2.9"), "t", testReceivedAt, cache)
	if err != nil || len(recs) != 0 || st.TemplatesUpdated != 0 || st.DataSetsWaiting != 1 {
		t.Fatalf("err=%v records=%d stats=%+v", err, len(recs), st)
	}
}

// One exporter cycling through template IDs and observation domains is
// capped; refusals are reported, known templates keep updating, other
// exporters are unaffected, and freed room (withdrawal, expiry) is reusable.
func TestTemplateCacheBoundedPerExporter(t *testing.T) {
	cache := NewV9TemplateCache()
	fields := fieldsOf(basic4("10.0.0.1", "10.0.0.2", 1, 1, 6))
	noisy := addr("192.0.2.66")
	refused := 0
	for i := 0; i < maxTemplatesPerExporter+100; i++ {
		domain, id := uint32(i/1000), uint16(256+i%1000)
		if _, stored := cache.Put(noisy, domain, V9Template{ID: id, Fields: fields}); !stored {
			refused++
		}
	}
	if refused != 100 {
		t.Fatalf("refused %d new templates beyond the per-exporter bound, want 100", refused)
	}
	// A known template still refreshes (and may change layout) at the bound.
	changed := append(append([]V9TemplateField(nil), fields...), V9TemplateField{Type: ieTOS, Length: 1})
	if register, stored := cache.Put(noisy, 0, V9Template{ID: 256, Fields: changed}); !register || !stored {
		t.Fatalf("known template not updated at the bound: register=%v stored=%v", register, stored)
	}
	// An options template replacing a data template with the same ID fits.
	if _, stored := cache.PutOptions(noisy, 0, V9OptionsTemplate{ID: 257, ScopeFields: []V9TemplateField{{Type: 1, Length: 4}}, Fields: []V9TemplateField{{Type: ieSamplingInterval, Length: 4}}}); !stored {
		t.Fatal("data template could not be replaced by an options template at the bound")
	}
	// Other exporters are not affected by one exporter's bound.
	if _, stored := cache.Put(addr("192.0.2.1"), 0, V9Template{ID: 256, Fields: fields}); !stored {
		t.Fatal("another exporter was refused")
	}
	// Withdrawing frees room for a new template.
	cache.Withdraw(noisy, 0, 300)
	if _, stored := cache.Put(noisy, 99, V9Template{ID: 400, Fields: fields}); !stored {
		t.Fatal("room freed by a withdrawal was not reusable")
	}
	cache.WithdrawAll(noisy, 1, false)
	for i := 0; i < 1000; i++ {
		if _, stored := cache.Put(noisy, 100, V9Template{ID: uint16(256 + i), Fields: fields}); !stored {
			t.Fatalf("room freed by a withdraw-all was not reusable (template %d)", i)
		}
	}
	// Expiry frees everything, bookkeeping included.
	base := time.Now()
	cache.now = func() time.Time { return base.Add(v9TemplateTTL + time.Minute) }
	cache.Prune()
	if len(cache.templatesPer) != 0 || len(cache.templates)+len(cache.optionsTemplates) != 0 || len(cache.dataIDs)+len(cache.optionIDs) != 0 {
		t.Fatalf("prune left counts %v, %d templates, %d+%d domain indexes", cache.templatesPer,
			len(cache.templates)+len(cache.optionsTemplates), len(cache.dataIDs), len(cache.optionIDs))
	}
	if _, stored := cache.Put(noisy, 0, V9Template{ID: 256, Fields: fields}); !stored {
		t.Fatal("exporter still refused after its templates expired")
	}
}

// Refused templates are counted by the decoder and their data waits, rather
// than being decoded with a guessed layout.
func TestRefusedTemplateCountedAndDataWaits(t *testing.T) {
	cache := NewV9TemplateCache()
	exporter := addr("192.0.2.66")
	fields := fieldsOf(basic4("10.0.0.1", "10.0.0.2", 1, 1, 6))
	for i := 0; i < maxTemplatesPerExporter; i++ {
		cache.Put(exporter, uint32(1000+i), V9Template{ID: 256, Fields: fields})
	}
	recs, st := decodeV9(t, cache, "192.0.2.66", 256, basic4("10.0.0.1", "10.0.0.2", 100, 1, 6))
	if st.TemplatesRefused != 1 || st.DataSetsWaiting != 1 || len(recs) != 0 {
		t.Fatalf("records=%d stats=%+v", len(recs), st)
	}
}

// Samplers are bounded per exporter as well; existing samplers keep updating.
func TestSamplerCacheBounded(t *testing.T) {
	cache := NewV9TemplateCache()
	exporter := addr("192.0.2.66")
	for i := 0; i < maxSamplersPerExporter+50; i++ {
		cache.PutSampler(exporter, 1, uint64(i+1), true, 100)
	}
	// Per-ID samplers only: none of them became a domain default (X-08).
	if got := cache.samplersPer[exporter]; got != maxSamplersPerExporter || len(cache.samplers) != maxSamplersPerExporter {
		t.Fatalf("samplers held = %d (%d in the map), want %d", got, len(cache.samplers), maxSamplersPerExporter)
	}
	cache.PutSampler(exporter, 1, 5, true, 1000)
	if n, ok := cache.Sampler(exporter, 1, 5, true); !ok || n != 1000 {
		t.Fatalf("known sampler not updated at the bound: %d %v", n, ok)
	}
	refusedID := uint64(maxSamplersPerExporter + 40)
	if _, kept := cache.samplers[samplerKey{exporter, 1, refusedID, true}]; kept {
		t.Fatal("sampler beyond the bound was stored")
	}
	// A flow naming the refused sampler has no rate of its own and no domain
	// default; the domain's samplers disagree (100 and 1000), so no rate is
	// picked for it (X-08).
	if n, state := cache.ResolveSampler(exporter, 1, refusedID, true); state != SamplerAmbiguous || n != 0 {
		t.Fatalf("refused sampler resolved to %d %v", n, state)
	}
	cache.PutSampler(addr("192.0.2.1"), 1, 0, false, 64)
	if n, ok := cache.Sampler(addr("192.0.2.1"), 1, 0, false); !ok || n != 64 {
		t.Fatalf("another exporter's sampler refused: %d %v", n, ok)
	}
	// Samplers of a domain without live templates are pruned with their counts.
	cache.Prune()
	if len(cache.samplers) != 0 || len(cache.samplersPer) != 0 {
		t.Fatalf("prune left %d samplers, counts %v", len(cache.samplers), cache.samplersPer)
	}
}

// Many template records in one datagram, each a new definition: the cache
// stays within its global bound whatever the number of source addresses.
func TestTemplateCacheGlobalBound(t *testing.T) {
	cache := NewV9TemplateCache()
	fields := []V9TemplateField{{Type: ieProtocol, Length: 1}}
	stored := 0
	for e := 0; e < maxTemplates/maxTemplatesPerExporter+2; e++ {
		exporter := addr(fmt.Sprintf("10.%d.%d.1", e/250, e%250))
		for i := 0; i < maxTemplatesPerExporter; i++ {
			if _, ok := cache.Put(exporter, uint32(i/1000), V9Template{ID: uint16(256 + i%1000), Fields: fields}); ok {
				stored++
			}
		}
	}
	if stored != maxTemplates || len(cache.templates) != maxTemplates {
		t.Fatalf("stored %d (map %d), want the global bound %d", stored, len(cache.templates), maxTemplates)
	}
}

// The reverse half of an ICMP session carries no type/code and no ports,
// whichever field carried the request's type: v9 exporters often put
// type*256+code in IE 11 (dst port) instead of IE 32, and swapping the ports
// moved the echo-request type into the reply's src_port.
func TestBiflowReverseICMPHasNoPorts(t *testing.T) {
	rec := []fv{
		{ieSrcIPv4, 4, "10.0.0.1"}, {ieDstIPv4, 4, "10.0.0.2"}, {ieProtocol, 1, 1},
		{ieSrcPort, 2, 0}, {ieDstPort, 2, 0x0800},
		{ieBytes, 4, 840}, {iePackets, 4, 10}, {ieOutBytes, 4, 420}, {ieOutPackets, 4, 5},
	}
	out, _ := decodeV9(t, NewV9TemplateCache(), "192.0.2.75", 292, rec)
	if len(out) != 2 {
		t.Fatalf("records = %d, want forward + reverse", len(out))
	}
	fwd, rev := out[0], out[1]
	if fwd.DstPort != 0x0800 || fwd.FieldsPresent&FieldL4Ports == 0 {
		t.Fatalf("forward: %+v", fwd)
	}
	if rev.SrcPort != 0 || rev.DstPort != 0 || rev.FieldsPresent&(FieldL4Ports|FieldICMPType) != 0 || rev.Bytes != 420 {
		t.Fatalf("reverse ICMP: ports %d/%d fields %032b bytes %d", rev.SrcPort, rev.DstPort, rev.FieldsPresent, rev.Bytes)
	}
	// TCP keeps its swapped ports.
	tcp := basic4("10.0.0.1", "10.0.0.2", 1000, 10, 6)
	tcp = append(tcp, fv{ieOutBytes, 4, 9000}, fv{ieOutPackets, 4, 12})
	out, _ = decodeV9(t, NewV9TemplateCache(), "192.0.2.75", 293, tcp)
	if len(out) != 2 || out[1].SrcPort != 443 || out[1].DstPort != 40000 || out[1].FieldsPresent&FieldL4Ports == 0 {
		t.Fatalf("reverse TCP: %+v", out)
	}
}

// An IPFIX "withdraw all templates" record is 4 bytes, so one datagram can
// carry 16,000 of them. Each must cost the templates of its own domain, not a
// scan of the whole cache (16,000 x 65,536 map visits per datagram).
func TestWithdrawAllFloodIsCheap(t *testing.T) {
	cache := NewV9TemplateCache()
	fields := []V9TemplateField{{Type: ieProtocol, Length: 1}}
	others := maxTemplates/maxTemplatesPerExporter - 1 // nearly full, with room for the attacker
	for e := 0; e < others; e++ {
		exporter := addr(fmt.Sprintf("10.1.%d.1", e))
		for i := 0; i < maxTemplatesPerExporter; i++ {
			cache.Put(exporter, uint32(i/1000), V9Template{ID: uint16(256 + i%1000), Fields: fields})
		}
	}
	attacker := addr("192.0.2.66")
	cache.Put(attacker, 1, V9Template{ID: 256, Fields: fields})
	cache.PutOptions(attacker, 1, V9OptionsTemplate{ID: 257, ScopeFields: []V9TemplateField{{Type: ieSelectorID, Length: 4}}, Fields: []V9TemplateField{{Type: iePsampInterval, Length: 4}}})
	var flood []byte
	for i := 0; i < 8000; i++ {
		flood = binary.BigEndian.AppendUint16(flood, 2) // template ID 2 + field count 0: withdraw all
		flood = binary.BigEndian.AppendUint16(flood, 0)
	}
	msg := ipfixMsg(1, 1, ipfixSet(2, flood), ipfixSet(3, flood))
	start := time.Now()
	if _, _, err := ParseIPFIX(msg, attacker, "t", testReceivedAt, cache); err != nil {
		t.Fatal(err)
	}
	if d := time.Since(start); d > time.Second {
		t.Fatalf("a withdrawal flood took %v", d)
	}
	if _, ok := cache.Get(attacker, 1, 256); ok {
		t.Fatal("data templates not withdrawn")
	}
	if _, ok := cache.GetOptions(attacker, 1, 257); !ok {
		t.Fatal("a data-template withdrawal removed an options template") // set 3 withdraws ID 3 only
	}
	if len(cache.templates) != others*maxTemplatesPerExporter || cache.templatesPer[attacker] != 1 {
		t.Fatalf("other exporters affected: %d templates, attacker count %d", len(cache.templates), cache.templatesPer[attacker])
	}
}
