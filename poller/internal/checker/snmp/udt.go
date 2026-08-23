package snmp

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	g "github.com/gosnmp/gosnmp"
)

// User Device Tracker collection: bridge FDBs (MAC -> port), ARP/ND
// (IP -> MAC), LLDP/CDP neighbors and VLAN inventory.
//
// Strategy per device:
//  1. dot1dBasePortIfIndex to map bridge ports to ifIndexes.
//  2. Q-BRIDGE-MIB dot1qTpFdbTable (VLAN-aware) first.
//  3. BRIDGE-MIB dot1dTpFdbTable when Q-BRIDGE is empty.
//  4. Cisco fallback: enumerate VLANs via VTP and walk BRIDGE-MIB once
//     per VLAN using community@vlan (v1/v2c) or the vlan-N context (v3).
//  5. ipNetToMediaTable + ipNetToPhysicalTable for IP bindings.
//  6. LLDP remTable (+ CDP cache on Cisco) for neighbors and uplink
//     detection.

const maxUdtVlans = 128 // safety cap for per-VLAN FDB walks

// fdbStatus values worth keeping: other(1) and learned(3).
// invalid(2), self(4) and mgmt(5) are dropped at collect time.
func fdbStatusKeep(status int) bool {
	return status == 1 || status == 3
}

// CollectUDT gathers UDT data for a device. The caller passes the
// connected session used for the main poll; per-VLAN Cisco walks open
// short-lived extra sessions. Partial output is returned on error —
// e.g. a switch with no ARP table still yields its FDB.
func (c *Collector) CollectUDT(ctx context.Context, d *Device, s *g.GoSNMP, ifs []Interface) (*UdtData, error) {
	u := &UdtData{
		Pvids:      make(map[int]int),
		TrunkPorts: make(map[int]bool),
		OwnMACs:    make(map[string]bool),
	}
	for _, i := range ifs {
		if i.MACAddress != "" {
			u.OwnMACs[i.MACAddress] = true
		}
	}

	// Bridge base info.
	if res, err := s.Get([]string{OIDDot1dBaseBridgeAddress}); err == nil && len(res.Variables) > 0 {
		if mac := macString(res.Variables[0]); mac != "" {
			u.BridgeMAC = mac
			u.OwnMACs[mac] = true
		}
	}
	portToIf := c.collectBasePortMap(s)

	isCisco := strings.HasPrefix(strings.TrimPrefix(firstNonEmpty(d.SysObjectID), "."), "1.3.6.1.4.1.9.")

	// VLAN inventory: Q-BRIDGE static names, Cisco VTP as fallback/merge.
	vlans := c.collectVlans(s, isCisco)
	u.Vlans = vlans

	// PVIDs (untagged VLAN per port) — indexed by bridge port.
	if pdus, err := s.BulkWalkAll(OIDDot1qPvid); err == nil {
		for _, p := range pdus {
			bport := lastIndex(p.Name)
			ifIdx, ok := portToIf[bport]
			if !ok {
				ifIdx = bport
			}
			u.Pvids[ifIdx] = int(asUint(p))
		}
	}

	// Cisco trunk status — indexed by ifIndex.
	if isCisco {
		if pdus, err := s.BulkWalkAll(OIDVlanTrunkPortDynamicStat); err == nil {
			for _, p := range pdus {
				if asInt(p) == 1 { // trunking
					u.TrunkPorts[lastIndex(p.Name)] = true
				}
			}
		}
	}

	if ctx.Err() != nil {
		return u, ctx.Err()
	}

	// FDB: Q-BRIDGE first, dot1d fallback, Cisco per-VLAN last.
	fdb := c.collectQBridgeFdb(s, portToIf)
	if len(fdb) == 0 {
		fdb = c.collectDot1dFdb(s, portToIf, 0)
	}
	if len(fdb) == 0 && isCisco && len(vlans) > 0 {
		fdb, u.FdbNote = c.collectCiscoPerVlanFdb(ctx, d, vlans)
	}
	if len(fdb) == 0 && u.FdbNote == "" && len(portToIf) > 0 {
		u.FdbNote = "bridge MIB returned no MACs (check the SNMP view exposes 1.3.6.1.2.1.17)"
	}
	u.Fdb = fdb

	if ctx.Err() != nil {
		return u, ctx.Err()
	}

	// ARP / ND.
	u.Arp = c.collectArp(s)

	// Neighbors.
	u.Neighbors = c.collectLldp(s, ifs, portToIf)
	if isCisco {
		u.Neighbors = append(u.Neighbors, c.collectCdp(s)...)
	}

	return u, nil
}

// collectBasePortMap walks dot1dBasePortIfIndex: bridge port -> ifIndex.
func (c *Collector) collectBasePortMap(s *g.GoSNMP) map[int]int {
	out := make(map[int]int)
	pdus, err := s.BulkWalkAll(OIDDot1dBasePortIfIndex)
	if err != nil {
		return out
	}
	for _, p := range pdus {
		out[lastIndex(p.Name)] = int(asInt(p))
	}
	return out
}

func (c *Collector) collectVlans(s *g.GoSNMP, isCisco bool) []VlanInfo {
	byID := make(map[int]string)
	if pdus, err := s.BulkWalkAll(OIDDot1qVlanStaticName); err == nil {
		for _, p := range pdus {
			byID[lastIndex(p.Name)] = asString(p)
		}
	}
	if isCisco {
		// vtpVlanState index is mgmtDomain.vlan — vlan is the last sub-id.
		states, _ := s.BulkWalkAll(OIDVtpVlanState)
		names, _ := s.BulkWalkAll(OIDVtpVlanName)
		nameByVlan := make(map[int]string, len(names))
		for _, p := range names {
			nameByVlan[lastIndex(p.Name)] = asString(p)
		}
		for _, p := range states {
			if asInt(p) != 1 { // operational
				continue
			}
			vlan := lastIndex(p.Name)
			if _, ok := byID[vlan]; !ok {
				byID[vlan] = nameByVlan[vlan]
			}
		}
	}
	out := make([]VlanInfo, 0, len(byID))
	for id, name := range byID {
		// 1002-1005 are legacy FDDI/TR defaults on Cisco — skip.
		if id >= 1002 && id <= 1005 {
			continue
		}
		out = append(out, VlanInfo{ID: id, Name: name})
	}
	return out
}

// collectQBridgeFdb walks dot1qTpFdbTable. The OID suffix is
// <fdbId>.<6 MAC bytes>; fdbId equals the VLAN ID on practically all
// implementations.
func (c *Collector) collectQBridgeFdb(s *g.GoSNMP, portToIf map[int]int) []FdbEntry {
	ports, err := s.BulkWalkAll(OIDDot1qTpFdbPort)
	if err != nil || len(ports) == 0 {
		return nil
	}
	status := make(map[string]int)
	if sts, err := s.BulkWalkAll(OIDDot1qTpFdbStatus); err == nil {
		for _, p := range sts {
			status[oidSuffix(p.Name, OIDDot1qTpFdbStatus)] = int(asInt(p))
		}
	}

	out := make([]FdbEntry, 0, len(ports))
	for _, p := range ports {
		suffix := oidSuffix(p.Name, OIDDot1qTpFdbPort)
		parts := strings.Split(suffix, ".")
		if len(parts) != 7 {
			continue
		}
		vlan, _ := strconv.Atoi(parts[0])
		mac := macFromOIDParts(parts[1:])
		if mac == "" {
			continue
		}
		st, ok := status[suffix]
		if !ok {
			st = 3
		}
		if !fdbStatusKeep(st) {
			continue
		}
		bport := int(asInt(p))
		if bport <= 0 {
			continue
		}
		ifIdx, ok := portToIf[bport]
		if !ok {
			ifIdx = bport
		}
		out = append(out, FdbEntry{VlanID: vlan, MAC: mac, IfIndex: ifIdx, Status: st})
	}
	return out
}

// collectDot1dFdb walks the classic dot1dTpFdbTable on session s,
// attributing entries to the given VLAN (0 = unknown).
func (c *Collector) collectDot1dFdb(s *g.GoSNMP, portToIf map[int]int, vlan int) []FdbEntry {
	out, _ := c.walkDot1dFdb(s, portToIf, vlan)
	return out
}

// walkDot1dFdb is collectDot1dFdb with the walk error preserved, so a
// per-VLAN caller can tell "denied" apart from "empty".
func (c *Collector) walkDot1dFdb(s *g.GoSNMP, portToIf map[int]int, vlan int) ([]FdbEntry, error) {
	ports, err := s.BulkWalkAll(OIDDot1dTpFdbPort)
	if err != nil {
		return nil, err
	}
	if len(ports) == 0 {
		return nil, nil
	}
	status := make(map[string]int)
	if sts, err := s.BulkWalkAll(OIDDot1dTpFdbStatus); err == nil {
		for _, p := range sts {
			status[oidSuffix(p.Name, OIDDot1dTpFdbStatus)] = int(asInt(p))
		}
	}

	out := make([]FdbEntry, 0, len(ports))
	for _, p := range ports {
		suffix := oidSuffix(p.Name, OIDDot1dTpFdbPort)
		parts := strings.Split(suffix, ".")
		if len(parts) != 6 {
			continue
		}
		mac := macFromOIDParts(parts)
		if mac == "" {
			continue
		}
		st, ok := status[suffix]
		if !ok {
			st = 3
		}
		if !fdbStatusKeep(st) {
			continue
		}
		bport := int(asInt(p))
		if bport <= 0 {
			continue
		}
		ifIdx, ok := portToIf[bport]
		if !ok {
			ifIdx = bport
		}
		out = append(out, FdbEntry{VlanID: vlan, MAC: mac, IfIndex: ifIdx, Status: st})
	}
	return out, nil
}

// collectCiscoPerVlanFdb opens one short-lived session per VLAN using
// Cisco community-string indexing (community@vlan) or, for SNMPv3, the
// vlan-N context, and walks BRIDGE-MIB inside it. The bridge-port map
// is VLAN-specific, so it is re-walked per context.
// It also returns a note describing why the walk came up empty, so an
// unconfigured switch is distinguishable from a genuinely idle one.
func (c *Collector) collectCiscoPerVlanFdb(ctx context.Context, d *Device, vlans []VlanInfo) ([]FdbEntry, string) {
	var out []FdbEntry
	count, denied, failed := 0, 0, 0
	for _, v := range vlans {
		if ctx.Err() != nil {
			break
		}
		if v.ID <= 0 || v.ID > 4094 {
			continue
		}
		count++
		if count > maxUdtVlans {
			break
		}

		vd := *d // shallow copy: adjust credentials for the VLAN context
		if d.Version == "3" {
			vd.V3Context = fmt.Sprintf("vlan-%d", v.ID)
		} else {
			vd.Community = fmt.Sprintf("%s@%d", d.Community, v.ID)
		}
		vs, err := NewSession(&vd)
		if err != nil {
			failed++
			continue
		}
		if err := vs.Connect(); err != nil {
			failed++
			continue
		}
		portToIf := c.collectBasePortMap(vs)
		entries, err := c.walkDot1dFdb(vs, portToIf, v.ID)
		_ = vs.Conn.Close()
		switch {
		case isAuthzError(err):
			denied++
		case err != nil:
			failed++
		}
		out = append(out, entries...)
	}

	if len(out) > 0 || count == 0 {
		return out, ""
	}
	switch {
	case denied > 0 && d.Version == "3":
		return nil, fmt.Sprintf("SNMPv3 user not authorized for the vlan-N contexts "+
			"(%d/%d VLANs denied); add 'snmp-server group <group> v3 priv context vlan- match prefix' on the switch", denied, count)
	case denied > 0:
		return nil, fmt.Sprintf("community@vlan indexing denied on %d/%d VLANs", denied, count)
	case failed == count:
		return nil, fmt.Sprintf("per-VLAN BRIDGE-MIB walk failed on all %d VLANs", count)
	}
	return nil, ""
}

// isAuthzError reports whether an SNMP error is the agent refusing access
// to a context/view, as opposed to a timeout or transport failure.
func isAuthzError(err error) bool {
	if err == nil {
		return false
	}
	s := strings.ToLower(err.Error())
	return strings.Contains(s, "authorizationerror") ||
		strings.Contains(s, "access denied") ||
		strings.Contains(s, "unknown context") ||
		strings.Contains(s, "bad context") ||
		strings.Contains(s, "noaccess") ||
		strings.Contains(s, "no access")
}

// collectArp walks ipNetToMediaTable (IPv4) and ipNetToPhysicalTable
// (IPv4 + IPv6 neighbor discovery).
func (c *Collector) collectArp(s *g.GoSNMP) []ArpEntry {
	var out []ArpEntry
	seen := make(map[string]bool)

	// Classic IPv4 ARP: suffix = ifIndex.a.b.c.d
	types := make(map[string]int)
	if pdus, err := s.BulkWalkAll(OIDIpNetToMediaType); err == nil {
		for _, p := range pdus {
			types[oidSuffix(p.Name, OIDIpNetToMediaType)] = int(asInt(p))
		}
	}
	if pdus, err := s.BulkWalkAll(OIDIpNetToMediaPhysAddress); err == nil {
		for _, p := range pdus {
			suffix := oidSuffix(p.Name, OIDIpNetToMediaPhysAddress)
			parts := strings.Split(suffix, ".")
			if len(parts) != 5 {
				continue
			}
			if t, ok := types[suffix]; ok && t == 2 { // invalid
				continue
			}
			mac := macString(p)
			if mac == "" {
				continue
			}
			ifIdx, _ := strconv.Atoi(parts[0])
			ip := ipv4FromOIDParts(parts[1:])
			if ip == "" {
				continue
			}
			key := ip + "|" + mac
			if seen[key] {
				continue
			}
			seen[key] = true
			out = append(out, ArpEntry{IfIndex: ifIdx, IP: ip, MAC: mac, Source: "arp"})
		}
	}

	// RFC 4293 ipNetToPhysicalTable: suffix = ifIndex.addrType.addrLen.addr...
	if pdus, err := s.BulkWalkAll(OIDIpNetToPhysicalPhysAddr); err == nil {
		for _, p := range pdus {
			suffix := oidSuffix(p.Name, OIDIpNetToPhysicalPhysAddr)
			parts := strings.Split(suffix, ".")
			if len(parts) < 4 {
				continue
			}
			ifIdx, _ := strconv.Atoi(parts[0])
			addrType, _ := strconv.Atoi(parts[1])
			addrLen, _ := strconv.Atoi(parts[2])
			addrParts := parts[3:]
			if len(addrParts) != addrLen {
				continue
			}
			mac := macString(p)
			if mac == "" {
				continue
			}
			var ip string
			var isV6 bool
			switch {
			case addrType == 1 && addrLen == 4:
				ip = ipv4FromOIDParts(addrParts)
			case addrType == 2 && addrLen == 16:
				ip = ipv6FromOIDParts(addrParts)
				isV6 = true
			default:
				continue
			}
			if ip == "" {
				continue
			}
			key := ip + "|" + mac
			if seen[key] {
				continue
			}
			seen[key] = true
			out = append(out, ArpEntry{IfIndex: ifIdx, IP: ip, MAC: mac, IsIPv6: isV6, Source: "nd"})
		}
	}
	return out
}

// collectLldp walks the LLDP remote table. Index is
// timeMark.localPortNum.remIndex; localPortNum maps to an ifIndex via
// lldpLocPortId (matched against ifName/ifDescr) with a fall-through
// to treating localPortNum as the ifIndex directly (the common case).
func (c *Collector) collectLldp(s *g.GoSNMP, ifs []Interface, portToIf map[int]int) []LldpNeighbor {
	sysNames, err := s.BulkWalkAll(OIDLldpRemSysName)
	if err != nil || len(sysNames) == 0 {
		return nil
	}

	walk := func(oid string) map[string]g.SnmpPDU {
		out := make(map[string]g.SnmpPDU)
		if pdus, err := s.BulkWalkAll(oid); err == nil {
			for i := range pdus {
				out[oidSuffix(pdus[i].Name, oid)] = pdus[i]
			}
		}
		return out
	}
	chassisSub := walk(OIDLldpRemChassisIDSubtype)
	chassisID := walk(OIDLldpRemChassisID)
	portSub := walk(OIDLldpRemPortIDSubtype)
	portID := walk(OIDLldpRemPortID)
	portDesc := walk(OIDLldpRemPortDesc)
	sysDesc := walk(OIDLldpRemSysDesc)

	// lldpLocPortId -> ifIndex resolution table.
	ifByName := make(map[string]int, len(ifs))
	ifIdxSet := make(map[int]bool, len(ifs))
	for _, i := range ifs {
		ifByName[strings.ToLower(i.IfName)] = i.IfIndex
		ifByName[strings.ToLower(i.IfDescr)] = i.IfIndex
		ifIdxSet[i.IfIndex] = true
	}
	locPortToIf := make(map[int]int)
	if pdus, err := s.BulkWalkAll(OIDLldpLocPortID); err == nil {
		for _, p := range pdus {
			locPort := lastIndex(p.Name)
			if idx, ok := ifByName[strings.ToLower(asString(p))]; ok {
				locPortToIf[locPort] = idx
			}
		}
	}

	resolveLocal := func(locPort int) int {
		if idx, ok := locPortToIf[locPort]; ok {
			return idx
		}
		if ifIdxSet[locPort] {
			return locPort
		}
		if idx, ok := portToIf[locPort]; ok {
			return idx
		}
		return locPort
	}

	out := make([]LldpNeighbor, 0, len(sysNames))
	for _, p := range sysNames {
		suffix := oidSuffix(p.Name, OIDLldpRemSysName)
		parts := strings.Split(suffix, ".")
		if len(parts) != 3 {
			continue
		}
		locPort, _ := strconv.Atoi(parts[1])
		n := LldpNeighbor{
			LocalIfIndex: resolveLocal(locPort),
			Protocol:     "lldp",
			SysName:      asString(p),
		}
		if v, ok := chassisSub[suffix]; ok {
			n.ChassisIDSubtype = int(asInt(v))
		}
		if v, ok := chassisID[suffix]; ok {
			// chassisId subtypes: 4 = macAddress, 5 = networkAddress.
			n.ChassisID = decodeLldpID(v, n.ChassisIDSubtype, 4, 5)
		}
		if v, ok := portID[suffix]; ok {
			sub := 0
			if sv, ok := portSub[suffix]; ok {
				sub = int(asInt(sv))
			}
			// portId subtypes: 3 = macAddress, 4 = networkAddress.
			n.PortID = decodeLldpID(v, sub, 3, 4)
		}
		if v, ok := portDesc[suffix]; ok {
			n.PortDesc = asString(v)
		}
		if v, ok := sysDesc[suffix]; ok {
			n.SysDescr = asString(v)
		}
		out = append(out, n)
	}
	return out
}

// collectCdp walks the Cisco CDP cache. Index is ifIndex.deviceIndex.
func (c *Collector) collectCdp(s *g.GoSNMP) []LldpNeighbor {
	devIDs, err := s.BulkWalkAll(OIDCdpCacheDeviceID)
	if err != nil || len(devIDs) == 0 {
		return nil
	}
	walk := func(oid string) map[string]g.SnmpPDU {
		out := make(map[string]g.SnmpPDU)
		if pdus, err := s.BulkWalkAll(oid); err == nil {
			for i := range pdus {
				out[oidSuffix(pdus[i].Name, oid)] = pdus[i]
			}
		}
		return out
	}
	ports := walk(OIDCdpCacheDevicePort)
	platforms := walk(OIDCdpCachePlatform)

	out := make([]LldpNeighbor, 0, len(devIDs))
	for _, p := range devIDs {
		suffix := oidSuffix(p.Name, OIDCdpCacheDeviceID)
		parts := strings.Split(suffix, ".")
		if len(parts) != 2 {
			continue
		}
		ifIdx, _ := strconv.Atoi(parts[0])
		n := LldpNeighbor{
			LocalIfIndex: ifIdx,
			Protocol:     "cdp",
			SysName:      asString(p),
		}
		if v, ok := ports[suffix]; ok {
			n.PortID = asString(v)
		}
		if v, ok := platforms[suffix]; ok {
			n.SysDescr = asString(v)
		}
		out = append(out, n)
	}
	return out
}

// --- helpers ---

// decodeLldpID renders an LLDP chassis or port identifier as text.
// Both are OctetStrings whose meaning is given by a companion subtype
// column, so the raw bytes are frequently not text at all — a bare MAC,
// an address-family-prefixed IP, or a NUL-padded fixed-width field
// (ArubaOS-CX reports chassisId "17 00 00 00 00 00" for Cisco phones).
// macSub/addrSub carry the subtype values that mean macAddress and
// networkAddress for this particular column.
//
// Anything that survives as neither is cleaned to printable text, and
// falls back to hex when nothing printable remains — an opaque but
// stable identifier beats an empty string, which would make every
// neighbor on the port collapse into one topology link.
func decodeLldpID(v g.SnmpPDU, subtype, macSub, addrSub int) string {
	b := bytesOf(v)
	declaredBinary := false
	switch {
	case subtype == macSub:
		declaredBinary = true
		if len(b) == 6 {
			return macOf(b)
		}
	case subtype == addrSub:
		declaredBinary = true
		if s := lldpNetworkAddr(b); s != "" {
			return s
		}
	}
	if s := asString(v); s != "" {
		return s
	}
	// Only guess at a bare MAC when the agent did not already declare the
	// value to be something else; a malformed networkAddress rendered as a
	// MAC would read as a real endpoint identity.
	if len(b) == 6 && !declaredBinary {
		return macOf(b)
	}
	if len(b) > 0 {
		return fmt.Sprintf("%x", b)
	}
	return ""
}

// lldpNetworkAddr decodes an LLDP networkAddress: a one-octet IANA
// address family followed by the address itself.
func lldpNetworkAddr(b []byte) string {
	switch {
	case b[0] == 1 && len(b) == 5: // IPv4
		return fmt.Sprintf("%d.%d.%d.%d", b[1], b[2], b[3], b[4])
	case b[0] == 2 && len(b) == 17: // IPv6
		parts := make([]string, 0, 8)
		for i := 1; i < 17; i += 2 {
			parts = append(parts, fmt.Sprintf("%x", uint16(b[i])<<8|uint16(b[i+1])))
		}
		return strings.Join(parts, ":")
	}
	return ""
}

// oidSuffix strips the base OID (with or without leading dot) and the
// separating dot from a returned PDU name.
func oidSuffix(full, base string) string {
	t := strings.TrimPrefix(full, ".")
	if strings.HasPrefix(t, base+".") {
		return t[len(base)+1:]
	}
	return ""
}

// macFromOIDParts converts 6 dotted-decimal OID sub-ids to a MAC string.
// Multicast/broadcast MACs (group bit set) return "".
func macFromOIDParts(parts []string) string {
	if len(parts) != 6 {
		return ""
	}
	b := make([]byte, 6)
	for i, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil || n < 0 || n > 255 {
			return ""
		}
		b[i] = byte(n)
	}
	if b[0]&0x01 != 0 { // multicast / broadcast
		return ""
	}
	return fmt.Sprintf("%02x:%02x:%02x:%02x:%02x:%02x", b[0], b[1], b[2], b[3], b[4], b[5])
}

// ipv4FromOIDParts converts 4 dotted-decimal OID sub-ids to an IPv4
// string, validating each octet is 0-255. Returns "" on malformed input
// so one bad SNMP value can't poison the enclosing DB transaction.
func ipv4FromOIDParts(parts []string) string {
	if len(parts) != 4 {
		return ""
	}
	for _, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil || n < 0 || n > 255 {
			return ""
		}
	}
	return strings.Join(parts, ".")
}

// ipv6FromOIDParts converts 16 dotted-decimal sub-ids to an IPv6 string.
func ipv6FromOIDParts(parts []string) string {
	if len(parts) != 16 {
		return ""
	}
	b := make([]byte, 16)
	for i, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil || n < 0 || n > 255 {
			return ""
		}
		b[i] = byte(n)
	}
	var sb strings.Builder
	for i := 0; i < 16; i += 2 {
		if i > 0 {
			sb.WriteByte(':')
		}
		fmt.Fprintf(&sb, "%x", uint16(b[i])<<8|uint16(b[i+1]))
	}
	return sb.String()
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}
