package netflow

import (
	"encoding/binary"
	"math/rand/v2"
	"net"
	"testing"
)

// Robustness: a collector listens on an open UDP port, so any byte sequence
// can arrive. Every decoder must return (records, error) without panicking,
// and whatever it returns must satisfy the storage invariants: both
// endpoints present in exactly one address family, never a fabricated one.

func checkRecords(t *testing.T, recs []Record) {
	t.Helper()
	for _, r := range recs {
		v4 := len(r.SrcAddr) == 4 && len(r.DstAddr) == 4 && r.SrcAddr6 == nil && r.DstAddr6 == nil
		v6 := len(r.SrcAddr6) == 16 && len(r.DstAddr6) == 16 && r.SrcAddr == nil && r.DstAddr == nil
		if !(v4 && r.IPVersion == 4) && !(v6 && r.IPVersion == 6) {
			t.Fatalf("record violates the address invariant: %+v", r)
		}
		for _, ip := range []net.IP{r.XlateSrc, r.XlateDst, r.NextHop, r.NextHop6} {
			if ip != nil && len(ip) != 4 && len(ip) != 16 {
				t.Fatalf("malformed address %v in %+v", ip, r)
			}
		}
		if !r.ExporterAddr.IsValid() || r.FieldsPresent&FieldTracked == 0 || r.SamplingInterval == 0 {
			t.Fatalf("identity/presence/sampling: %+v", r)
		}
	}
}

// v9Corpus is a datagram exercising most decoder paths: a data template, an
// options template with a sampler and an application name, NSEL fields and a
// session record that splits into two.
func v9Corpus(t testing.TB) []byte {
	rec := nselTeardown(int(FwEventDeleted), 500, 900, 5, 9)
	rec = append(rec, fv{ieApplicationID, 4, []byte{13, 0, 1, 0xC5}}, fv{ieSamplerID, 1, 1})
	opt := v9OptionsTemplateSet(300, []V9TemplateField{{Type: 1, Length: 4}},
		[]V9TemplateField{{Type: ieSamplerID, Length: 1}, {Type: ieSamplingInterval, Length: 4}, {Type: ieApplicationID, Length: 4}, {Type: ieApplicationName, Length: 8}})
	var optData []byte
	optData = binary.BigEndian.AppendUint32(optData, 0)
	optData = append(optData, 1, 0, 0, 0, 10, 13, 0, 1, 0xC5)
	optData = append(optData, fixedText("ssl", 8)...)
	return v9Packet(3, 9, v9TemplateSet(263, fieldsOf(rec)), opt, v9Set(300, optData), v9Set(263, encodeRecords(t, rec, rec)))
}

func ipfixCorpus(t testing.TB) []byte {
	rec := []fv{
		{ieSrcIPv6, 16, "2001:db8::1"}, {ieDstIPv6, 16, "2001:db8::2"}, {ieProtocol, 1, 58}, {ieICMPTypeCodeIPv6, 2, 0x8000},
		{ieBytes, 8, 100}, {iePackets, 8, 1}, {0x8000 | ieBytes, 8, 200}, {ieFlowEndMillis, 8, uint64(testExportSecs) * 1000},
	}
	fields := fieldsOf(rec)
	fields[6].Enterprise = penReverse
	fields = append(fields, V9TemplateField{Type: ieApplicationName, Length: 0xFFFF})
	body := append(encodeRecords(t, rec), 3, 'a', 'b', 'c')
	opts := ipfixOptionsTemplate(400, []V9TemplateField{{Type: ieApplicationID, Length: 4}}, []V9TemplateField{{Type: ieApplicationName, Length: 0xFFFF}})
	optData := pad4([]byte{13, 0, 0, 80, 4, 'h', 't', 't', 'p'})
	return ipfixMsg(8, 1, ipfixTemplate(256, fields), opts, ipfixSet(400, optData), ipfixSet(256, pad4(body)))
}

func TestDecodersSurviveTruncationAndCorruption(t *testing.T) {
	v5 := v5Packet(0x4064, testSysUptime, 99_000)
	corpora := []struct {
		name  string
		data  []byte
		parse func([]byte) ([]Record, error)
	}{
		{"v5", v5, func(b []byte) ([]Record, error) {
			p, err := ParseV5(b, addr("192.0.2.1"), "t", testReceivedAt)
			if err != nil {
				return nil, err
			}
			return p.Records, nil
		}},
		{"v9", v9Corpus(t), func(b []byte) ([]Record, error) {
			recs, _, err := ParseV9(b, addr("192.0.2.2"), "t", testReceivedAt, NewV9TemplateCache())
			return recs, err
		}},
		{"ipfix", ipfixCorpus(t), func(b []byte) ([]Record, error) {
			recs, _, err := ParseIPFIX(b, addr("2001:db8::3"), "t", testReceivedAt, NewV9TemplateCache())
			return recs, err
		}},
	}
	rng := rand.New(rand.NewPCG(1, 2))
	for _, c := range corpora {
		full, err := c.parse(c.data)
		if err != nil || len(full) == 0 {
			t.Fatalf("%s corpus does not decode: err=%v records=%d", c.name, err, len(full))
		}
		checkRecords(t, full)
		for cut := 0; cut < len(c.data); cut++ {
			recs, _ := c.parse(c.data[:cut])
			checkRecords(t, recs)
		}
		for i := 0; i < 3000; i++ {
			b := append([]byte(nil), c.data...)
			for n := 1 + rng.IntN(4); n > 0; n-- {
				b[rng.IntN(len(b))] = byte(rng.IntN(256))
			}
			recs, _ := c.parse(b)
			checkRecords(t, recs)
		}
		for i := 0; i < 500; i++ {
			b := make([]byte, rng.IntN(1500))
			for j := range b {
				b[j] = byte(rng.IntN(256))
			}
			if len(b) >= 2 { // keep the version so the body parsers are reached
				copy(b, c.data[:2])
			}
			recs, _ := c.parse(b)
			checkRecords(t, recs)
		}
	}
}

// Native fuzz targets (go test -fuzz=FuzzParseV9 ./internal/netflow); plain
// go test runs the seeds.
func FuzzParseV9(f *testing.F) {
	f.Add(v9Corpus(f))
	f.Fuzz(func(t *testing.T, b []byte) {
		recs, _, _ := ParseV9(b, addr("192.0.2.2"), "t", testReceivedAt, NewV9TemplateCache())
		checkRecords(t, recs)
	})
}

func FuzzParseIPFIX(f *testing.F) {
	f.Add(ipfixCorpus(f))
	f.Fuzz(func(t *testing.T, b []byte) {
		recs, _, _ := ParseIPFIX(b, addr("2001:db8::3"), "t", testReceivedAt, NewV9TemplateCache())
		checkRecords(t, recs)
	})
}

func FuzzParseV5(f *testing.F) {
	f.Add(v5Packet(0x4064, testSysUptime, 99_000))
	f.Fuzz(func(t *testing.T, b []byte) {
		if p, err := ParseV5(b, addr("192.0.2.1"), "t", testReceivedAt); err == nil {
			checkRecords(t, p.Records)
		}
	})
}
