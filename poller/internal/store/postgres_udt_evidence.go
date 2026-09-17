package store

import (
	"context"
	"fmt"
	"net"
	"sort"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/zenplus/poller/internal/checker/snmp"
)

// Retain independently reported bindings. Interface indexes are local to a
// reporter, not a global VLAN/VRF identity. Missing entries in an incomplete
// ARP walk must not close another router's evidence or imply disconnection.
func upsertIPEvidence(ctx context.Context, tx pgx.Tx, deviceID uuid.UUID, arp []snmp.ArpEntry, endpointIDs map[string]uuid.UUID) error {
	type row struct {
		endpoint   uuid.UUID
		ip, source string
		ifIndex    int32
	}
	unique := make(map[string]row)
	for _, a := range arp {
		endpoint, ok := endpointIDs[a.MAC]
		ip := net.ParseIP(a.IP)
		if !ok || ip == nil {
			continue
		}
		ifIndex := a.IfIndex
		if ifIndex < 0 {
			ifIndex = 0
		}
		r := row{endpoint, ip.String(), a.Source, int32(ifIndex)}
		unique[fmt.Sprintf("%s|%s|%d|%s", endpoint, r.ip, ifIndex, a.Source)] = r
	}
	if len(unique) == 0 {
		return nil
	}
	keys := make([]string, 0, len(unique))
	for key := range unique {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	ids := make([]uuid.UUID, 0, len(keys))
	ips, sources := make([]string, 0, len(keys)), make([]string, 0, len(keys))
	interfaces := make([]int32, 0, len(keys))
	for _, key := range keys {
		r := unique[key]
		ids = append(ids, r.endpoint)
		ips = append(ips, r.ip)
		sources = append(sources, r.source)
		interfaces = append(interfaces, r.ifIndex)
	}
	_, err := tx.Exec(ctx, `
        INSERT INTO udt_ip_evidence AS e
            (endpoint_id, ip, reporting_device_id, if_index, source, first_seen, last_seen, observation_count, recent_sightings)
        SELECT t.endpoint_id, t.ip, $5, t.if_index, t.source, NOW(), NOW(), 1, ARRAY[NOW()]
        FROM unnest($1::uuid[], $2::inet[], $3::text[], $4::int[]) AS t(endpoint_id, ip, source, if_index)
        ORDER BY t.endpoint_id, t.ip, t.if_index, t.source
        ON CONFLICT (endpoint_id, ip, reporting_device_id, if_index, source)
        DO UPDATE SET last_seen = EXCLUDED.last_seen,
            observation_count = e.observation_count + 1,
            recent_sightings = ARRAY(SELECT sighting FROM unnest(e.recent_sightings || EXCLUDED.recent_sightings) sighting
                                    ORDER BY sighting DESC LIMIT 3)
        WHERE EXCLUDED.last_seen > e.last_seen
    `, ids, ips, sources, interfaces, deviceID)
	return err
}
