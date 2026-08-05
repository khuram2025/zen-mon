package store

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/zenplus/poller/internal/checker/snmp"
)

// UDT persistence: turns one device's UdtData snapshot into endpoint
// identities, sessionized port attachments, IP bindings, per-port
// state (with automatic uplink classification) and activity events.
//
// Uplink classification order (first match wins; manual override
// always wins via the ON CONFLICT clause):
//   1. LLDP/CDP neighbor that is network infrastructure
//   2. Cisco VTP trunk status
//   3. MAC count >= udtUplinkMacThreshold
const udtUplinkMacThreshold = 8

// infraSysDescrRe matches neighbor system descriptions/names that
// indicate switching/routing gear (vs. phones, APs, cameras).
var infraSysDescrRe = regexp.MustCompile(`(?i)\b(switch|router|routing|bridge|firewall|ios|nx-os|junos|eos|vyos|routeros)\b`)

type udtSessionRow struct {
	endpointID uuid.UUID
	ifIndex    int32
	vlanID     int32 // -1 = unknown
	isDirect   bool
}

// UpsertUdtData persists a UDT snapshot for one device inside a single
// transaction.
func (s *PostgresStore) UpsertUdtData(ctx context.Context, deviceID uuid.UUID, u *snmp.UdtData) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("udt begin: %w", err)
	}
	defer tx.Rollback(ctx)

	// ---- reference data -------------------------------------------------
	deviceHostnames, deviceMacs, err := loadInfraSets(ctx, tx)
	if err != nil {
		return err
	}

	// ---- topology links (LLDP/CDP) --------------------------------------
	if err := upsertTopologyLinks(ctx, tx, deviceID, u.Neighbors, deviceHostnames, deviceMacs); err != nil {
		return fmt.Errorf("udt topology: %w", err)
	}

	// ---- per-port rollup + uplink classification ------------------------
	ownMacs := make(map[string]bool, len(u.OwnMACs)+1)
	for m := range u.OwnMACs {
		ownMacs[m] = true
	}
	if u.BridgeMAC != "" {
		ownMacs[u.BridgeMAC] = true
	}

	// Dedupe FDB entries and drop the device's own MACs.
	type fdbKey struct {
		mac  string
		ifIx int
		vlan int
	}
	fdbSeen := make(map[fdbKey]bool, len(u.Fdb))
	fdb := make([]snmp.FdbEntry, 0, len(u.Fdb))
	macsPerPort := make(map[int]map[string]bool)
	vlansPerPort := make(map[int]map[int]bool)
	for _, f := range u.Fdb {
		if f.MAC == "" || f.IfIndex <= 0 || ownMacs[f.MAC] {
			continue
		}
		k := fdbKey{f.MAC, f.IfIndex, f.VlanID}
		if fdbSeen[k] {
			continue
		}
		fdbSeen[k] = true
		fdb = append(fdb, f)
		if macsPerPort[f.IfIndex] == nil {
			macsPerPort[f.IfIndex] = make(map[string]bool)
		}
		macsPerPort[f.IfIndex][f.MAC] = true
		if f.VlanID > 0 {
			if vlansPerPort[f.IfIndex] == nil {
				vlansPerPort[f.IfIndex] = make(map[int]bool)
			}
			vlansPerPort[f.IfIndex][f.VlanID] = true
		}
	}

	// Neighbor-derived uplink evidence.
	uplinkReason := make(map[int]string)
	for _, n := range u.Neighbors {
		if n.LocalIfIndex <= 0 {
			continue
		}
		infra := deviceHostnames[strings.ToLower(n.SysName)] ||
			(n.ChassisID != "" && deviceMacs[n.ChassisID] != uuid.Nil) ||
			infraSysDescrRe.MatchString(n.SysDescr) ||
			infraSysDescrRe.MatchString(n.SysName)
		if infra {
			if _, ok := uplinkReason[n.LocalIfIndex]; !ok {
				uplinkReason[n.LocalIfIndex] = n.Protocol // "lldp" | "cdp"
			}
		}
	}
	for ifIdx := range u.TrunkPorts {
		if _, ok := uplinkReason[ifIdx]; !ok {
			uplinkReason[ifIdx] = "trunk"
		}
	}
	for ifIdx, macs := range macsPerPort {
		if len(macs) >= udtUplinkMacThreshold {
			if _, ok := uplinkReason[ifIdx]; !ok {
				uplinkReason[ifIdx] = "mac_count"
			}
		}
	}

	// Union of ports we know something about.
	portSet := make(map[int]bool)
	for p := range macsPerPort {
		portSet[p] = true
	}
	for p := range u.Pvids {
		portSet[p] = true
	}
	for p := range uplinkReason {
		portSet[p] = true
	}

	finalUplink, err := upsertPortState(ctx, tx, deviceID, portSet, uplinkReason, macsPerPort, vlansPerPort, u.Pvids)
	if err != nil {
		return fmt.Errorf("udt port state: %w", err)
	}

	// ---- VLAN inventory -------------------------------------------------
	if err := upsertVlans(ctx, tx, deviceID, u.Vlans); err != nil {
		return fmt.Errorf("udt vlans: %w", err)
	}

	// ---- endpoints ------------------------------------------------------
	// Collect every observable MAC: FDB plus ARP (ARP-only endpoints are
	// real — e.g. hosts behind non-bridging gear).
	macSet := make(map[string]bool, len(fdb))
	for _, f := range fdb {
		macSet[f.MAC] = true
	}
	arp := make([]snmp.ArpEntry, 0, len(u.Arp))
	for _, a := range u.Arp {
		if a.MAC == "" || a.IP == "" || ownMacs[a.MAC] {
			continue
		}
		arp = append(arp, a)
		macSet[a.MAC] = true
	}
	if len(macSet) == 0 {
		return tx.Commit(ctx)
	}

	endpointIDs, newMacs, err := upsertEndpoints(ctx, tx, macSet)
	if err != nil {
		return fmt.Errorf("udt endpoints: %w", err)
	}

	// ---- sessions -------------------------------------------------------
	sessions := make([]udtSessionRow, 0, len(fdb))
	sessKey := make(map[string]bool, len(fdb))
	for _, f := range fdb {
		epID, ok := endpointIDs[f.MAC]
		if !ok {
			continue
		}
		vlan := int32(-1)
		if f.VlanID > 0 {
			vlan = int32(f.VlanID)
		}
		k := fmt.Sprintf("%s|%d|%d", epID, f.IfIndex, vlan)
		if sessKey[k] {
			continue
		}
		sessKey[k] = true
		sessions = append(sessions, udtSessionRow{
			endpointID: epID,
			ifIndex:    int32(f.IfIndex),
			vlanID:     vlan,
			isDirect:   !finalUplink[f.IfIndex],
		})
	}
	moveEvents, err := upsertSessions(ctx, tx, deviceID, sessions)
	if err != nil {
		return fmt.Errorf("udt sessions: %w", err)
	}

	// ---- IP bindings ----------------------------------------------------
	if err := upsertIPHistory(ctx, tx, deviceID, arp, endpointIDs); err != nil {
		return fmt.Errorf("udt ip history: %w", err)
	}

	// ---- link endpoints that are themselves monitored devices -----------
	if _, err := tx.Exec(ctx, `
		UPDATE udt_endpoints e SET device_id = sub.did, updated_at = NOW()
		FROM (
			SELECT DISTINCT ON (di.mac_address) di.mac_address AS mac, di.device_id AS did
			FROM device_interfaces di WHERE di.mac_address IS NOT NULL
			ORDER BY di.mac_address, di.device_id
		) sub
		WHERE e.device_id IS NULL AND e.mac = sub.mac
	`); err != nil {
		return fmt.Errorf("udt device link by mac: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		UPDATE udt_endpoints e SET device_id = d.id, updated_at = NOW()
		FROM devices d
		WHERE e.device_id IS NULL AND e.ip_address IS NOT NULL AND e.ip_address = d.ip_address
	`); err != nil {
		return fmt.Errorf("udt device link by ip: %w", err)
	}

	// ---- events ---------------------------------------------------------
	if err := insertUdtEvents(ctx, tx, deviceID, fdb, endpointIDs, newMacs, moveEvents); err != nil {
		return fmt.Errorf("udt events: %w", err)
	}

	return tx.Commit(ctx)
}

// loadInfraSets returns (lowercased hostnames of monitored devices,
// interface MAC -> device id for all monitored devices).
func loadInfraSets(ctx context.Context, tx pgx.Tx) (map[string]bool, map[string]uuid.UUID, error) {
	hostnames := make(map[string]bool)
	rows, err := tx.Query(ctx, `SELECT LOWER(hostname) FROM devices`)
	if err != nil {
		return nil, nil, fmt.Errorf("udt load hostnames: %w", err)
	}
	for rows.Next() {
		var h string
		if err := rows.Scan(&h); err != nil {
			rows.Close()
			return nil, nil, err
		}
		hostnames[h] = true
	}
	rows.Close()
	if rows.Err() != nil {
		return nil, nil, rows.Err()
	}

	macs := make(map[string]uuid.UUID)
	rows, err = tx.Query(ctx, `
		SELECT di.mac_address::text, di.device_id
		FROM device_interfaces di WHERE di.mac_address IS NOT NULL
	`)
	if err != nil {
		return nil, nil, fmt.Errorf("udt load device macs: %w", err)
	}
	for rows.Next() {
		var m string
		var id uuid.UUID
		if err := rows.Scan(&m, &id); err != nil {
			rows.Close()
			return nil, nil, err
		}
		macs[m] = id
	}
	rows.Close()
	return hostnames, macs, rows.Err()
}

func upsertTopologyLinks(
	ctx context.Context, tx pgx.Tx, deviceID uuid.UUID,
	neighbors []snmp.LldpNeighbor,
	deviceHostnames map[string]bool, deviceMacs map[string]uuid.UUID,
) error {
	if len(neighbors) == 0 {
		return nil
	}
	batch := &pgx.Batch{}
	n := 0
	for _, nb := range neighbors {
		if nb.LocalIfIndex <= 0 {
			continue
		}
		meta, _ := json.Marshal(map[string]any{
			"sys_descr":          nb.SysDescr,
			"chassis_id_subtype": nb.ChassisIDSubtype,
		})
		batch.Queue(`
			WITH local_if AS (
				SELECT if_name FROM device_interfaces WHERE device_id = $1 AND if_index = $2
			), remote_dev AS (
				SELECT id FROM devices WHERE LOWER(hostname) = LOWER(NULLIF($3, '')) LIMIT 1
			), remote_dev_mac AS (
				SELECT device_id AS id FROM device_interfaces WHERE mac_address::text = NULLIF($4, '') LIMIT 1
			), updated AS (
				UPDATE topology_links SET
					last_seen_at = NOW(), updated_at = NOW(),
					local_if_name = COALESCE((SELECT if_name FROM local_if), local_if_name),
					remote_device_id = COALESCE((SELECT id FROM remote_dev), (SELECT id FROM remote_dev_mac), remote_device_id),
					remote_if_name = COALESCE(NULLIF($5, ''), remote_if_name),
					confidence = GREATEST(confidence, $6),
					metadata = $7::jsonb
				WHERE local_device_id = $1
				  AND COALESCE(local_if_index, -1) = $2
				  AND protocol = $8
				  AND COALESCE(remote_chassis_id, '') = COALESCE($4, '')
				  AND COALESCE(remote_port_id, '') = COALESCE($5, '')
				  AND COALESCE(remote_hostname, '') = COALESCE($3, '')
				RETURNING id
			)
			INSERT INTO topology_links (
				local_device_id, local_if_index, local_if_name,
				remote_device_id, remote_chassis_id, remote_port_id,
				remote_hostname, remote_if_name, protocol, confidence,
				source, metadata
			)
			SELECT $1, $2, (SELECT if_name FROM local_if),
			       COALESCE((SELECT id FROM remote_dev), (SELECT id FROM remote_dev_mac)),
			       NULLIF($4, ''), NULLIF($5, ''), NULLIF($3, ''), NULLIF($5, ''),
			       $8, $6, 'udt-poller', $7::jsonb
			WHERE NOT EXISTS (SELECT 1 FROM updated)
		`,
			deviceID, nb.LocalIfIndex, nb.SysName, nb.ChassisID, nb.PortID,
			90, string(meta), nb.Protocol,
		)
		n++
	}
	br := tx.SendBatch(ctx, batch)
	defer br.Close()
	for i := 0; i < n; i++ {
		if _, err := br.Exec(); err != nil {
			return err
		}
	}
	return br.Close()
}

func upsertPortState(
	ctx context.Context, tx pgx.Tx, deviceID uuid.UUID,
	portSet map[int]bool, uplinkReason map[int]string,
	macsPerPort map[int]map[string]bool, vlansPerPort map[int]map[int]bool,
	pvids map[int]int,
) (map[int]bool, error) {
	finalUplink := make(map[int]bool)
	if len(portSet) == 0 {
		return finalUplink, nil
	}

	ifIdx := make([]int32, 0, len(portSet))
	isUp := make([]bool, 0, len(portSet))
	reasons := make([]string, 0, len(portSet))
	macCounts := make([]int32, 0, len(portSet))
	vlanJSONs := make([]string, 0, len(portSet))
	pvidArr := make([]int32, 0, len(portSet))

	ports := make([]int, 0, len(portSet))
	for p := range portSet {
		ports = append(ports, p)
	}
	sort.Ints(ports)

	for _, p := range ports {
		reason := uplinkReason[p]
		vlans := make([]int, 0, len(vlansPerPort[p]))
		for v := range vlansPerPort[p] {
			vlans = append(vlans, v)
		}
		sort.Ints(vlans)
		vj, _ := json.Marshal(vlans)

		ifIdx = append(ifIdx, int32(p))
		isUp = append(isUp, reason != "")
		reasons = append(reasons, reason)
		macCounts = append(macCounts, int32(len(macsPerPort[p])))
		vlanJSONs = append(vlanJSONs, string(vj))
		pvidArr = append(pvidArr, int32(pvids[p]))
	}

	rows, err := tx.Query(ctx, `
		INSERT INTO udt_port_state (device_id, if_index, is_uplink, uplink_reason, mac_count, vlan_ids, pvid, updated_at)
		SELECT $1, t.if_index, t.is_uplink, NULLIF(t.reason, ''), t.mac_count, t.vlan_ids::jsonb, NULLIF(t.pvid, 0), NOW()
		FROM unnest($2::int[], $3::bool[], $4::text[], $5::int[], $6::text[], $7::int[])
		     AS t(if_index, is_uplink, reason, mac_count, vlan_ids, pvid)
		ON CONFLICT (device_id, if_index) DO UPDATE SET
			is_uplink = CASE WHEN udt_port_state.uplink_override = 'uplink' THEN TRUE
			                 WHEN udt_port_state.uplink_override = 'access' THEN FALSE
			                 ELSE EXCLUDED.is_uplink END,
			uplink_reason = CASE WHEN udt_port_state.uplink_override IS NOT NULL THEN 'manual'
			                     ELSE EXCLUDED.uplink_reason END,
			mac_count = EXCLUDED.mac_count,
			vlan_ids = EXCLUDED.vlan_ids,
			pvid = COALESCE(EXCLUDED.pvid, udt_port_state.pvid),
			updated_at = NOW()
		RETURNING if_index, is_uplink
	`, deviceID, ifIdx, isUp, reasons, macCounts, vlanJSONs, pvidArr)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var idx int32
		var up bool
		if err := rows.Scan(&idx, &up); err != nil {
			rows.Close()
			return nil, err
		}
		finalUplink[int(idx)] = up
	}
	rows.Close()
	return finalUplink, rows.Err()
}

func upsertVlans(ctx context.Context, tx pgx.Tx, deviceID uuid.UUID, vlans []snmp.VlanInfo) error {
	if len(vlans) == 0 {
		return nil
	}
	ids := make([]int32, 0, len(vlans))
	names := make([]string, 0, len(vlans))
	seen := make(map[int]bool, len(vlans))
	for _, v := range vlans {
		if v.ID <= 0 || seen[v.ID] {
			continue
		}
		seen[v.ID] = true
		ids = append(ids, int32(v.ID))
		names = append(names, v.Name)
	}
	_, err := tx.Exec(ctx, `
		INSERT INTO udt_vlans (device_id, vlan_id, name, first_seen, last_seen)
		SELECT $1, t.vlan_id, NULLIF(t.name, ''), NOW(), NOW()
		FROM unnest($2::int[], $3::text[]) AS t(vlan_id, name)
		ON CONFLICT (device_id, vlan_id) DO UPDATE SET
			name = COALESCE(NULLIF(EXCLUDED.name, ''), udt_vlans.name),
			last_seen = NOW()
	`, deviceID, ids, names)
	return err
}

// upsertEndpoints inserts/refreshes endpoint identities and returns
// (mac -> endpoint id, set of newly created macs).
func upsertEndpoints(ctx context.Context, tx pgx.Tx, macSet map[string]bool) (map[string]uuid.UUID, map[string]bool, error) {
	macs := make([]string, 0, len(macSet))
	randomized := make([]bool, 0, len(macSet))
	for m := range macSet {
		macs = append(macs, m)
		randomized = append(randomized, isLocallyAdministered(m))
	}

	rows, err := tx.Query(ctx, `
		INSERT INTO udt_endpoints (mac, vendor, is_randomized, first_seen, last_seen)
		SELECT t.mac::macaddr,
		       o.vendor,
		       t.randomized,
		       NOW(), NOW()
		FROM unnest($1::text[], $2::bool[]) AS t(mac, randomized)
		LEFT JOIN udt_oui o ON o.prefix = replace(substring(t.mac, 1, 8), ':', '')
		ON CONFLICT (mac) DO UPDATE SET
			last_seen = NOW(),
			vendor = COALESCE(udt_endpoints.vendor, EXCLUDED.vendor),
			updated_at = NOW()
		RETURNING mac::text, id, (xmax = 0) AS is_new
	`, macs, randomized)
	if err != nil {
		return nil, nil, err
	}
	ids := make(map[string]uuid.UUID, len(macs))
	newMacs := make(map[string]bool)
	for rows.Next() {
		var mac string
		var id uuid.UUID
		var isNew bool
		if err := rows.Scan(&mac, &id, &isNew); err != nil {
			rows.Close()
			return nil, nil, err
		}
		ids[mac] = id
		if isNew {
			newMacs[mac] = true
		}
	}
	rows.Close()
	return ids, newMacs, rows.Err()
}

type udtMoveEvent struct {
	endpointID uuid.UUID
	oldIfIndex int32
	newIfIndex int32
}

func upsertSessions(ctx context.Context, tx pgx.Tx, deviceID uuid.UUID, sessions []udtSessionRow) ([]udtMoveEvent, error) {
	if len(sessions) == 0 {
		return nil, nil
	}
	epIDs := make([]uuid.UUID, len(sessions))
	ifIdx := make([]int32, len(sessions))
	vlans := make([]int32, len(sessions))
	direct := make([]bool, len(sessions))
	for i, r := range sessions {
		epIDs[i] = r.endpointID
		ifIdx[i] = r.ifIndex
		vlans[i] = r.vlanID
		direct[i] = r.isDirect
	}

	// Detect moves: an active DIRECT session for the same endpoint on
	// this device but a different port gets closed when a new direct
	// observation arrives elsewhere.
	var moves []udtMoveEvent
	rows, err := tx.Query(ctx, `
		UPDATE udt_endpoint_locations l
		SET active = FALSE, closed_at = NOW()
		FROM (
			SELECT DISTINCT t.endpoint_id, t.if_index
			FROM unnest($2::uuid[], $3::int[], $4::bool[]) AS t(endpoint_id, if_index, is_direct)
			WHERE t.is_direct
		) v
		WHERE l.device_id = $1 AND l.active AND l.is_direct
		  AND l.endpoint_id = v.endpoint_id AND l.if_index <> v.if_index
		  -- Don't close a session whose own port is still a current direct
		  -- observation: an endpoint legitimately on two ports (e.g. one MAC
		  -- learned in two VLANs) must not perpetually close+reopen itself.
		  AND NOT EXISTS (
		    SELECT 1 FROM unnest($2::uuid[], $3::int[], $4::bool[]) AS v2(endpoint_id, if_index, is_direct)
		    WHERE v2.is_direct AND v2.endpoint_id = l.endpoint_id AND v2.if_index = l.if_index
		  )
		RETURNING l.endpoint_id, l.if_index, v.if_index
	`, deviceID, epIDs, ifIdx, direct)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var m udtMoveEvent
		if err := rows.Scan(&m.endpointID, &m.oldIfIndex, &m.newIfIndex); err != nil {
			rows.Close()
			return nil, err
		}
		moves = append(moves, m)
	}
	rows.Close()
	if rows.Err() != nil {
		return nil, rows.Err()
	}

	if _, err := tx.Exec(ctx, `
		INSERT INTO udt_endpoint_locations (endpoint_id, device_id, if_index, vlan_id, is_direct, active, first_seen, last_seen)
		SELECT t.endpoint_id, $1, t.if_index, NULLIF(t.vlan_id, -1), t.is_direct, TRUE, NOW(), NOW()
		FROM unnest($2::uuid[], $3::int[], $4::int[], $5::bool[]) AS t(endpoint_id, if_index, vlan_id, is_direct)
		ON CONFLICT (endpoint_id, device_id, if_index, COALESCE(vlan_id, -1)) WHERE active
		DO UPDATE SET last_seen = NOW(), is_direct = EXCLUDED.is_direct
	`, deviceID, epIDs, ifIdx, vlans, direct); err != nil {
		return nil, err
	}

	// Refresh per-port last-endpoint timestamps for direct sessions.
	if _, err := tx.Exec(ctx, `
		UPDATE udt_port_state p SET last_endpoint_seen = NOW()
		FROM (
			SELECT DISTINCT t.if_index
			FROM unnest($2::int[], $3::bool[]) AS t(if_index, is_direct)
			WHERE t.is_direct
		) v
		WHERE p.device_id = $1 AND p.if_index = v.if_index
	`, deviceID, ifIdx, direct); err != nil {
		return nil, err
	}
	return moves, nil
}

func upsertIPHistory(ctx context.Context, tx pgx.Tx, deviceID uuid.UUID, arp []snmp.ArpEntry, endpointIDs map[string]uuid.UUID) error {
	if len(arp) == 0 {
		return nil
	}
	type ipRow struct {
		epID   uuid.UUID
		ip     string
		source string
		isV6   bool
	}
	rowsIn := make([]ipRow, 0, len(arp))
	seen := make(map[string]bool, len(arp))
	for _, a := range arp {
		epID, ok := endpointIDs[a.MAC]
		if !ok {
			continue
		}
		k := epID.String() + "|" + a.IP
		if seen[k] {
			continue
		}
		seen[k] = true
		rowsIn = append(rowsIn, ipRow{epID, a.IP, a.Source, a.IsIPv6})
	}
	if len(rowsIn) == 0 {
		return nil
	}
	epIDs := make([]uuid.UUID, len(rowsIn))
	ips := make([]string, len(rowsIn))
	sources := make([]string, len(rowsIn))
	for i, r := range rowsIn {
		epIDs[i] = r.epID
		ips[i] = r.ip
		sources[i] = r.source
	}

	// An IP now seen on a different MAC closes the old binding.
	if _, err := tx.Exec(ctx, `
		UPDATE udt_ip_history h SET active = FALSE
		FROM unnest($1::uuid[], $2::inet[]) AS t(endpoint_id, ip)
		WHERE h.active AND h.ip = t.ip AND h.endpoint_id <> t.endpoint_id
	`, epIDs, ips); err != nil {
		return err
	}

	if _, err := tx.Exec(ctx, `
		INSERT INTO udt_ip_history (endpoint_id, ip, source, reporting_device_id, active, first_seen, last_seen)
		SELECT t.endpoint_id, t.ip, t.source, $4, TRUE, NOW(), NOW()
		FROM unnest($1::uuid[], $2::inet[], $3::text[]) AS t(endpoint_id, ip, source)
		ON CONFLICT (endpoint_id, ip) WHERE active
		DO UPDATE SET last_seen = NOW(), reporting_device_id = EXCLUDED.reporting_device_id
	`, epIDs, ips, sources, deviceID); err != nil {
		return err
	}

	// Latest-IP on the endpoint row; IPv4 wins over IPv6.
	for _, pass := range []bool{true, false} { // first IPv6, then IPv4 overwrites
		var pEp []uuid.UUID
		var pIP []string
		for i, r := range rowsIn {
			if r.isV6 == pass {
				pEp = append(pEp, epIDs[i])
				pIP = append(pIP, r.ip)
			}
		}
		if len(pEp) == 0 {
			continue
		}
		if _, err := tx.Exec(ctx, `
			UPDATE udt_endpoints e SET ip_address = t.ip, last_seen = NOW(), updated_at = NOW()
			FROM (
				SELECT DISTINCT ON (endpoint_id) endpoint_id, ip
				FROM unnest($1::uuid[], $2::inet[]) AS x(endpoint_id, ip)
			) t
			WHERE e.id = t.endpoint_id
		`, pEp, pIP); err != nil {
			return err
		}
	}
	return nil
}

func insertUdtEvents(
	ctx context.Context, tx pgx.Tx, deviceID uuid.UUID,
	fdb []snmp.FdbEntry, endpointIDs map[string]uuid.UUID,
	newMacs map[string]bool, moves []udtMoveEvent,
) error {
	// Location of each new MAC (first direct FDB sighting preferred).
	locByMac := make(map[string]snmp.FdbEntry)
	for _, f := range fdb {
		if _, ok := locByMac[f.MAC]; !ok {
			locByMac[f.MAC] = f
		}
	}

	batch := &pgx.Batch{}
	n := 0
	for mac := range newMacs {
		epID, ok := endpointIDs[mac]
		if !ok {
			continue
		}
		var ifIdx any
		details := map[string]any{"mac": mac}
		if f, ok := locByMac[mac]; ok {
			ifIdx = f.IfIndex
			if f.VlanID > 0 {
				details["vlan"] = f.VlanID
			}
		}
		dj, _ := json.Marshal(details)
		batch.Queue(`
			INSERT INTO udt_events (event_type, endpoint_id, device_id, if_index, details)
			VALUES ('new_endpoint', $1, $2, $3, $4::jsonb)
		`, epID, deviceID, ifIdx, string(dj))
		n++
	}
	for _, m := range moves {
		dj, _ := json.Marshal(map[string]any{
			"from_if_index": m.oldIfIndex,
			"to_if_index":   m.newIfIndex,
		})
		batch.Queue(`
			INSERT INTO udt_events (event_type, endpoint_id, device_id, if_index, details)
			VALUES ('endpoint_moved', $1, $2, $3, $4::jsonb)
		`, m.endpointID, deviceID, m.newIfIndex, string(dj))
		n++
	}
	if n == 0 {
		return nil
	}
	br := tx.SendBatch(ctx, batch)
	defer br.Close()
	for i := 0; i < n; i++ {
		if _, err := br.Exec(); err != nil {
			return err
		}
	}
	return br.Close()
}

// isLocallyAdministered reports whether the MAC has the locally
// administered bit set (randomized/private MACs on modern clients).
func isLocallyAdministered(mac string) bool {
	if len(mac) < 2 {
		return false
	}
	var b byte
	_, err := fmt.Sscanf(mac[:2], "%02x", &b)
	if err != nil {
		return false
	}
	return b&0x02 != 0
}
