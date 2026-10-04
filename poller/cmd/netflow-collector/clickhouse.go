package main

import (
	"context"
	"encoding/binary"
	"fmt"
	"net"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
	"github.com/zenplus/poller/internal/netflow"
)

// chStore is the ClickHouse implementation of flowStore plus the evidence
// tables (collector stats, template registry).
type chStore struct {
	conn        driver.Conn
	collectorID string
}

func (s *chStore) Ping(ctx context.Context) error { return s.conn.Ping(ctx) }

func (s *chStore) EnsureSchema(ctx context.Context) error {
	for _, stmt := range schemaStatements {
		if err := s.conn.Exec(ctx, stmt); err != nil {
			return fmt.Errorf("exec schema: %w", err)
		}
	}
	return nil
}

// insertSettings makes a retried batch idempotent: flow_records keeps a
// deduplication block log (non_replicated_deduplication_window), so a batch
// re-sent with the same token after an ambiguous failure is skipped, and a
// skipped block is not pushed to the materialized views either.
// max_partitions_per_insert_block is lifted because a batch trusted to
// exporter clocks (NETFLOW_CLOCK_POLICY=trust) may legitimately span many
// daily partitions; rejecting it would turn a clock problem into data loss.
func insertContext(ctx context.Context, token string) context.Context {
	return clickhouse.Context(ctx, clickhouse.WithSettings(clickhouse.Settings{
		"insert_deduplication_token":                         token,
		"deduplicate_blocks_in_dependent_materialized_views": 1,
		"max_partitions_per_insert_block":                    0,
	}))
}

const insertFlowsSQL = `INSERT INTO flow_records (
    timestamp, received_at, collector_id, exporter_ip, flow_version,
    flow_sequence, engine_type, engine_id, sampling_interval,
    src_addr, dst_addr, next_hop, input_snmp, output_snmp,
    packets, bytes, first_switched_ms, last_switched_ms,
    src_port, dst_port, tcp_flags, protocol, tos,
    src_as, dst_as, src_mask, dst_mask,
    input_if, output_if, ip_version, exporter_ip6, src_addr6, dst_addr6, next_hop6,
    fields_present, observation_domain, flow_direction, export_protocol, app_id, fw_event,
    xlate_src_addr6, xlate_dst_addr6, xlate_src_port, xlate_dst_port, flow_end_reason
)`

func (s *chStore) InsertFlows(ctx context.Context, token string, recs []netflow.Record) error {
	ctx = insertContext(ctx, token)
	batch, err := s.conn.PrepareBatch(ctx, insertFlowsSQL)
	if err != nil {
		return err
	}
	for i := range recs {
		r := &recs[i]
		if err := batch.Append(
			r.Timestamp, r.ReceivedAt, r.CollectorID, ipv4Col(r.ExporterIP), r.FlowVersion,
			r.FlowSequence, r.EngineType, r.EngineID, r.SamplingInterval,
			ipv4Col(r.SrcAddr), ipv4Col(r.DstAddr), ipv4Col(r.NextHop),
			netflow.LegacyIfIndex(r.InputSNMP), netflow.LegacyIfIndex(r.OutputSNMP),
			r.Packets, r.Bytes, r.FirstSwitchedMS, r.LastSwitchedMS,
			r.SrcPort, r.DstPort, r.TCPFlags, r.Protocol, r.TOS,
			r.SrcAS, r.DstAS, r.SrcMask, r.DstMask,
			r.InputSNMP, r.OutputSNMP, r.IPVersion, exporterIP6Col(r),
			ipv6Col(r.SrcAddr6), ipv6Col(r.DstAddr6), ipv6Col(r.NextHop6),
			r.FieldsPresent, r.ObservationDomain, r.FlowDirection, r.ExportProtocol, r.AppID, r.FwEvent,
			ipv6Col(r.XlateSrc), ipv6Col(r.XlateDst), r.XlateSrcPort, r.XlateDstPort, r.FlowEndReason,
		); err != nil {
			_ = batch.Abort()
			return &appendError{index: i, err: err}
		}
	}
	return batch.Send()
}

// ipv4Col converts an address for an IPv4 column. nil (not exported, or the
// record is IPv6) is written as 0.0.0.0; an IPv6 value can never reach an
// IPv4 column (the driver would reject or misconvert it).
func ipv4Col(ip net.IP) uint32 {
	if v4 := ip.To4(); v4 != nil {
		return binary.BigEndian.Uint32(v4)
	}
	return 0
}

// ipv6Col converts an address for an IPv6 column: nil is ::, an IPv4 address
// is stored IPv4-mapped (the convention of the NAT columns).
func ipv6Col(ip net.IP) [16]byte {
	var out [16]byte
	if len(ip) == 4 {
		copy(out[:], net.IPv4(ip[0], ip[1], ip[2], ip[3]).To16())
		return out
	}
	if len(ip) == 16 {
		copy(out[:], ip)
	}
	return out
}

func exporterIP6Col(r *netflow.Record) [16]byte {
	if !r.ExporterAddr.IsValid() || r.ExporterAddr.Is4() {
		return [16]byte{}
	}
	return r.ExporterAddr.As16()
}

const insertStatsSQL = `INSERT INTO flow_collector_stats (
    ts, collector_id, exporter, observation_domain, export_protocol,
    datagrams, datagram_bytes, records_decoded, records_written,
    records_dropped_implausible, records_dropped_queue, records_dropped_unknown_exporter,
    records_unsupported, parse_errors, data_sets_waiting_template, options_records,
    sequence_gaps, clock_offset_ms, clock_clamped_records, sampling_interval, templates_active,
    insert_failures, insert_retries, spool_records, spool_bytes, queue_len, queue_cap,
    kernel_rcvbuf_errors, sequence_units, sampling_ambiguous_records
)`

func (s *chStore) InsertStats(ctx context.Context, token string, rows []statsRow) error {
	ctx = insertContext(ctx, token)
	batch, err := s.conn.PrepareBatch(ctx, insertStatsSQL)
	if err != nil {
		return err
	}
	for _, r := range rows {
		c := r.C
		if err := batch.Append(
			r.TS, s.collectorID, r.Exporter, r.ObservationDomain, r.ExportProtocol,
			c.Datagrams, c.DatagramBytes, c.RecordsDecoded, c.RecordsWritten,
			c.RecordsDroppedImplausible, c.RecordsDroppedQueue, c.RecordsDroppedUnknownExporter,
			c.RecordsUnsupported, c.ParseErrors, c.DataSetsWaitingTemplate, c.OptionsRecords,
			c.SequenceGaps, r.ClockOffsetMS, c.ClockClampedRecords, r.SamplingInterval, r.TemplatesActive,
			c.InsertFailures, c.InsertRetries, r.G.SpoolRecords, r.G.SpoolBytes, r.G.QueueLen, r.G.QueueCap,
			c.KernelRcvbufErrors, c.SequenceUnits, c.SamplingAmbiguousRecords,
		); err != nil {
			_ = batch.Abort()
			return err
		}
	}
	return batch.Send()
}

const insertTemplatesSQL = `INSERT INTO flow_templates (
    exporter, observation_domain, export_protocol, template_id,
    field_ids, field_lengths, enterprise_ids, is_options,
    has_tcp_flags, has_ipv6, has_ifindex32, has_direction, has_app_id, has_sampling, has_nat, has_fw_event,
    first_seen, last_seen, collector_id
)`

func (s *chStore) InsertTemplates(ctx context.Context, rows []templateRow) error {
	batch, err := s.conn.PrepareBatch(ctx, insertTemplatesSQL)
	if err != nil {
		return err
	}
	for _, t := range rows {
		c := t.info.Capabilities()
		ids := make([]uint16, len(t.info.Fields))
		lens := make([]uint16, len(t.info.Fields))
		ents := make([]uint32, len(t.info.Fields))
		for i, f := range t.info.Fields {
			ids[i], lens[i], ents[i] = f.Type, f.Length, f.Enterprise
			if f.Enterprise != 0 {
				ids[i] &^= 0x8000 // the enterprise bit is carried by enterprise_ids
			}
		}
		if err := batch.Append(
			t.info.Exporter.String(), t.info.Domain, t.info.Protocol, t.info.ID,
			ids, lens, ents, b2u(t.info.IsOptions),
			b2u(c.TCPFlags), b2u(c.IPv6), b2u(c.IfIndex32), b2u(c.Direction), b2u(c.AppID), b2u(c.Sampling), b2u(c.NAT), b2u(c.FwEvent),
			t.firstSeen, t.lastSeen, s.collectorID,
		); err != nil {
			_ = batch.Abort()
			return err
		}
	}
	return batch.Send()
}

const insertAppNamesSQL = `INSERT INTO flow_app_names (app_id, name, description, category, exporter, updated_at)`

// InsertAppNames writes application definitions. flow_app_names is a
// ReplacingMergeTree(updated_at) keyed by app_id, so a repeated row simply
// replaces the previous one and needs no deduplication token.
func (s *chStore) InsertAppNames(ctx context.Context, rows []appNameRow) error {
	batch, err := s.conn.PrepareBatch(ctx, insertAppNamesSQL)
	if err != nil {
		return err
	}
	for _, r := range rows {
		if err := batch.Append(r.AppID, r.Name, r.Description, r.Category, r.Exporter, r.UpdatedAt); err != nil {
			_ = batch.Abort()
			return err
		}
	}
	return batch.Send()
}

const insertIfCountersSQL = `INSERT INTO flow_if_counters (
    ts, collector_id, exporter_ip, exporter_ip6, ifindex, if_speed, if_direction, oper_up,
    in_octets, out_octets, in_ucast, out_ucast, in_errors, out_errors, in_discards, out_discards,
    sample_interval_s
)`

// InsertIfCounters writes sFlow interface counter samples under the batch's
// deduplication token (flow_if_counters keeps a block log), so a retry after
// an ambiguous failure does not duplicate them.
func (s *chStore) InsertIfCounters(ctx context.Context, token string, rows []ifCounterRow) error {
	ctx = insertContext(ctx, token)
	batch, err := s.conn.PrepareBatch(ctx, insertIfCountersSQL)
	if err != nil {
		return err
	}
	for _, r := range rows {
		c := r.C
		var ip6 [16]byte // :: for an IPv4 agent, as in flow_records
		if r.Exporter.Is6() {
			ip6 = r.Exporter.As16()
		}
		if err := batch.Append(
			r.TS, s.collectorID, ipv4Col(netflow.ExporterStorageIP(r.Exporter)), ip6, c.IfIndex, c.Speed,
			uint8(min(c.Direction, 255)), b2u(c.OperUp),
			c.InOctets, c.OutOctets, uint64(c.InUcast), uint64(c.OutUcast), uint64(c.InErrors), uint64(c.OutErrors),
			uint64(c.InDiscards), uint64(c.OutDiscards), r.SampleIntervalS,
		); err != nil {
			_ = batch.Abort()
			return err
		}
	}
	return batch.Send()
}

func b2u(b bool) uint8 {
	if b {
		return 1
	}
	return 0
}

// openClickHouse builds the connection pool. clickhouse.Open does not dial,
// so an unreachable server does not stop the collector from listening.
func openClickHouse(opts *clickhouse.Options) (driver.Conn, error) {
	if opts.DialTimeout == 0 {
		opts.DialTimeout = 5 * time.Second
	}
	return clickhouse.Open(opts)
}
