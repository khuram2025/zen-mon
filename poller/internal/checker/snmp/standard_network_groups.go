package snmp

import "strings"

// IsNetworkDeviceType limits protocol-table walks to infrastructure devices.
// Servers and printers often expose partial bridge tables that are noisy and
// not operationally meaningful.
func IsNetworkDeviceType(deviceType string) bool {
	switch deviceType {
	case "router", "switch", "firewall", "access_point":
		return true
	default:
		return false
	}
}

// StandardNetworkOidGroups returns important vendor-neutral control-plane and
// layer-2 health tables. Unsupported MIBs are skipped by the template engine.
// These run less frequently than interface counters to keep poll cost bounded.
func StandardNetworkOidGroups() []OidGroup {
	return []OidGroup{
		{
			Key: "std_bgp_peers", Name: "BGP Peers", Kind: "table", IntervalSeconds: 120,
			Table: &OidGroupTable{LabelOID: "1.3.6.1.2.1.15.3.1.7"},
			Metrics: []OidMetric{
				{Key: "std_bgp_state", Name: "State", OID: "1.3.6.1.2.1.15.3.1.2", Type: "enum"},
				{Key: "std_bgp_admin", Name: "Administrative State", OID: "1.3.6.1.2.1.15.3.1.3", Type: "enum"},
				{Key: "std_bgp_remote_as", Name: "Remote AS", OID: "1.3.6.1.2.1.15.3.1.9", Type: "gauge", Unit: "asn"},
				{Key: "std_bgp_established_seconds", Name: "Established Time", OID: "1.3.6.1.2.1.15.3.1.16", Type: "gauge", Unit: "seconds"},
			},
		},
		{
			Key: "std_ospf_neighbors", Name: "OSPF Neighbors", Kind: "table", IntervalSeconds: 120,
			Table: &OidGroupTable{LabelOID: "1.3.6.1.2.1.14.10.1.1"},
			Metrics: []OidMetric{
				{Key: "std_ospf_neighbor_state", Name: "State", OID: "1.3.6.1.2.1.14.10.1.6", Type: "enum"},
				{Key: "std_ospf_neighbor_events", Name: "State Changes", OID: "1.3.6.1.2.1.14.10.1.7", Type: "gauge", Unit: "events"},
				{Key: "std_ospf_retrans_queue", Name: "Retransmit Queue", OID: "1.3.6.1.2.1.14.10.1.8", Type: "gauge", Unit: "LSAs"},
			},
		},
		{
			Key: "std_vrrp_routers", Name: "VRRP Routers", Kind: "table", IntervalSeconds: 120,
			Table: &OidGroupTable{LabelOID: "1.3.6.1.2.1.68.1.3.1.8"},
			Metrics: []OidMetric{
				{Key: "std_vrrp_state", Name: "State", OID: "1.3.6.1.2.1.68.1.3.1.4", Type: "enum"},
				{Key: "std_vrrp_admin_state", Name: "Administrative State", OID: "1.3.6.1.2.1.68.1.3.1.5", Type: "enum"},
				{Key: "std_vrrp_priority", Name: "Priority", OID: "1.3.6.1.2.1.68.1.3.1.6", Type: "gauge"},
				{Key: "std_vrrp_uptime", Name: "Master Uptime", OID: "1.3.6.1.2.1.68.1.3.1.14", Type: "gauge", Unit: "centiseconds"},
			},
		},
		{
			Key: "std_vrrpv3_routers", Name: "VRRPv3 Routers", Kind: "table", IntervalSeconds: 120,
			Table: &OidGroupTable{LabelOID: "1.3.6.1.2.1.207.1.1.1.1.3"},
			Metrics: []OidMetric{
				{Key: "std_vrrpv3_state", Name: "State", OID: "1.3.6.1.2.1.207.1.1.1.1.6", Type: "enum"},
				{Key: "std_vrrpv3_priority", Name: "Priority", OID: "1.3.6.1.2.1.207.1.1.1.1.7", Type: "gauge"},
				{Key: "std_vrrpv3_adv_interval", Name: "Advertisement Interval", OID: "1.3.6.1.2.1.207.1.1.1.1.9", Type: "gauge", Unit: "centiseconds"},
				{Key: "std_vrrpv3_preempt", Name: "Preempt Mode", OID: "1.3.6.1.2.1.207.1.1.1.1.10", Type: "enum"},
				{Key: "std_vrrpv3_uptime", Name: "Uptime", OID: "1.3.6.1.2.1.207.1.1.1.1.12", Type: "gauge", Unit: "centiseconds"},
			},
		},
		{
			Key: "std_stp_bridge", Name: "Spanning Tree", Kind: "scalar", IntervalSeconds: 120,
			Metrics: []OidMetric{
				{Key: "std_stp_time_since_change", Name: "Time Since Topology Change", OID: "1.3.6.1.2.1.17.2.3.0", Type: "gauge", Unit: "centiseconds"},
				{Key: "std_stp_topology_changes", Name: "Topology Changes", OID: "1.3.6.1.2.1.17.2.4.0", Type: "gauge", Unit: "changes"},
				{Key: "std_stp_root_cost", Name: "Root Path Cost", OID: "1.3.6.1.2.1.17.2.6.0", Type: "gauge"},
				{Key: "std_stp_root_port", Name: "Root Port", OID: "1.3.6.1.2.1.17.2.7.0", Type: "gauge", Unit: "bridge-port"},
			},
		},
		{
			Key: "std_stp_ports", Name: "Spanning Tree Ports", Kind: "table", IntervalSeconds: 300,
			Table: &OidGroupTable{LabelOID: "1.3.6.1.2.1.17.1.4.1.2"},
			Metrics: []OidMetric{
				{Key: "std_stp_port_state", Name: "State", OID: "1.3.6.1.2.1.17.2.15.1.3", Type: "enum"},
				{Key: "std_stp_port_enable", Name: "Enabled", OID: "1.3.6.1.2.1.17.2.15.1.4", Type: "enum"},
				{Key: "std_stp_port_path_cost", Name: "Path Cost", OID: "1.3.6.1.2.1.17.2.15.1.5", Type: "gauge"},
			},
		},
		{
			Key: "std_lacp_ports", Name: "LACP Ports", Kind: "table", IntervalSeconds: 300,
			Table: &OidGroupTable{LabelOID: "1.2.840.10006.300.43.1.2.1.1.1"},
			Metrics: []OidMetric{
				{Key: "std_lacp_actor_key", Name: "Actor Operational Key", OID: "1.2.840.10006.300.43.1.2.1.1.5", Type: "gauge"},
				{Key: "std_lacp_partner_key", Name: "Partner Operational Key", OID: "1.2.840.10006.300.43.1.2.1.1.11", Type: "gauge"},
				{Key: "std_lacp_selected_aggregator", Name: "Selected Aggregator", OID: "1.2.840.10006.300.43.1.2.1.1.12", Type: "gauge", Unit: "ifIndex"},
				{Key: "std_lacp_attached_aggregator", Name: "Attached Aggregator", OID: "1.2.840.10006.300.43.1.2.1.1.13", Type: "gauge", Unit: "ifIndex"},
			},
		},
	}
}

// MergeStandardNetworkOidGroups adds only capabilities not already supplied by
// a vendor profile. This prevents, for example, Juniper's existing BGP group
// and the standard BGP group from walking the same table twice.
func MergeStandardNetworkOidGroups(existing []OidGroup) []OidGroup {
	covered := make([]string, 0)
	for _, group := range existing {
		for _, metric := range group.Metrics {
			covered = append(covered, metric.OID)
		}
	}
	out := append([]OidGroup(nil), existing...)
	for _, standard := range StandardNetworkOidGroups() {
		overlaps := false
		for _, metric := range standard.Metrics {
			for _, oid := range covered {
				if oid == metric.OID || strings.HasPrefix(oid, metric.OID+".") || strings.HasPrefix(metric.OID, oid+".") {
					overlaps = true
					break
				}
			}
			if overlaps {
				break
			}
		}
		if !overlaps {
			out = append(out, standard)
		}
	}
	return out
}
