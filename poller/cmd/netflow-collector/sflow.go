package main

import (
	"net/netip"
	"time"

	"github.com/zenplus/poller/internal/netflow"
	"github.com/zenplus/poller/internal/sflow"
)

// handleSFlowDatagram decodes one sFlow v5 datagram (CR-03) on the sFlow
// socket's reader goroutine. Flow samples join the NetFlow records in the
// same queue (export_protocol 'sflow5', flow_version 0); generic interface
// counter samples go to flow_if_counters.
//
// The exporter identity is the agent address the datagram declares, the one
// sFlow agents are configured and inventoried by, so the allowlist and every
// statistic use it; the UDP source stands in only when the agent sent none.
// Statistics per (agent, sub-agent): datagram sequence gaps are lost
// datagrams, and options_records counts the counter samples (the stream's
// non-flow records).
func (c *collector) handleSFlowDatagram(data []byte, from netip.Addr, receivedAt time.Time) {
	from = netflow.CanonicalAddr(from)
	dg, err := sflow.Decode(data, from, c.cfg.ID, receivedAt)
	exporter := from
	if dg != nil {
		exporter = dg.Exporter
	}
	obs := datagramObservation{
		key:        statsKey{exporter: exporter, protocol: netflow.ProtocolSFlow5},
		bytes:      len(data),
		receivedAt: receivedAt,
	}
	if !c.allowed(exporter) {
		obs.unknownDrops = true
		c.stats.observeDatagram(obs)
		return
	}
	if err != nil {
		obs.parseError = true
		c.logf(&c.lastParseLog, "WARN: dropped malformed sFlow data from %s: %v", exporter, err)
	}
	if dg == nil {
		c.stats.observeDatagram(obs)
		return
	}
	obs.key.domain = dg.SubAgentID
	obs.hasSequence, obs.sequence, obs.seqUnits = true, dg.Sequence, 1
	obs.decoded = len(dg.Records)
	obs.unsupported = dg.Unsupported
	obs.options = dg.CounterSamples
	c.ifCounters.add(dg, receivedAt)
	c.ingest(&obs, dg.Records)
}
