package snmp

import (
	"context"
	"fmt"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	g "github.com/gosnmp/gosnmp"
)

// Collector executes one full SNMP poll for a Device and returns a
// Result. It is stateless except for an in-memory cache of previous
// interface-counter snapshots, used to compute bps.
type Collector struct {
	sessions *SessionCache
	pollerID string

	mu       sync.Mutex
	prevIfs  map[uuid.UUID]map[uint32]ifSnapshot
}

type ifSnapshot struct {
	inOctets  uint64
	outOctets uint64
	at        time.Time
	hc        bool
}

func NewCollector(pollerID string, sessions *SessionCache) *Collector {
	return &Collector{
		sessions: sessions,
		pollerID: pollerID,
		prevIfs:  make(map[uuid.UUID]map[uint32]ifSnapshot),
	}
}

// Collect runs all enabled collectors for a device. Any partial
// success is returned — a single failing collector does not cause
// the entire poll to be discarded.
func (c *Collector) Collect(ctx context.Context, d *Device) *Result {
	start := time.Now()
	r := &Result{DeviceID: d.ID, Timestamp: start.UTC()}

	client, err := c.sessions.Acquire(d)
	if err != nil {
		r.Err = err
		r.Duration = time.Since(start)
		c.sessions.MarkFailure(d.ID)
		return r
	}

	// System info is cheap and tells us sysUpTime + sysObjectID for
	// classification. Always collect.
	sys, sysErr := c.collectSystem(ctx, client)
	if sysErr == nil {
		r.System = sys
	}

	// Host-Resources: CPU + memory on devices that implement it.
	// (Failures here are normal on routers/switches that don't.)
	scalars, _ := c.collectHostResources(ctx, client, d.ID, start)
	r.Scalars = append(r.Scalars, scalars...)

	// Interfaces are the big one: IF-MIB table walk.
	ifs, ifErr := c.collectInterfaces(ctx, client)
	if ifErr == nil {
		r.Interfaces = ifs
		r.IfSamples = c.diffInterfaces(d.ID, ifs, start)
	}

	// Entities (inventory) — once per poll is fine; cheap walk.
	ents, _ := c.collectEntities(ctx, client)
	r.Entities = ents

	// Sensors — needs scale+precision+value, returned as MetricSample.
	sensors, sensorScalars, _ := c.collectSensors(ctx, client, d.ID, start)
	r.Sensors = sensors
	r.Scalars = append(r.Scalars, sensorScalars...)

	if sysErr != nil && ifErr != nil {
		r.Err = fmt.Errorf("system+interfaces failed: sys=%v if=%v", sysErr, ifErr)
		c.sessions.MarkFailure(d.ID)
	} else {
		c.sessions.MarkSuccess(d.ID)
	}
	r.Duration = time.Since(start)
	return r
}

// --- individual collectors ---

func (c *Collector) collectSystem(ctx context.Context, s *g.GoSNMP) (*SystemInfo, error) {
	oids := []string{
		OIDSysDescr, OIDSysObjectID, OIDSysUpTime,
		OIDSysContact, OIDSysName, OIDSysLocation,
	}
	res, err := s.Get(oids)
	if err != nil {
		return nil, fmt.Errorf("system Get: %w", err)
	}
	out := &SystemInfo{}
	for _, v := range res.Variables {
		switch {
		case strings.HasPrefix(v.Name, "."+OIDSysDescr) || v.Name == "."+OIDSysDescr || v.Name == OIDSysDescr:
			out.SysDescr = asString(v)
		case matchOID(v.Name, OIDSysObjectID):
			out.SysObjectID = asString(v)
		case matchOID(v.Name, OIDSysUpTime):
			// sysUpTime is in hundredths of a second.
			ticks := asUint(v)
			out.SysUpTime = time.Duration(ticks) * 10 * time.Millisecond
		case matchOID(v.Name, OIDSysContact):
			out.SysContact = asString(v)
		case matchOID(v.Name, OIDSysName):
			out.SysName = asString(v)
		case matchOID(v.Name, OIDSysLocation):
			out.SysLocation = asString(v)
		}
	}
	return out, nil
}

func (c *Collector) collectHostResources(
	ctx context.Context, s *g.GoSNMP, deviceID uuid.UUID, ts time.Time,
) ([]MetricSample, error) {
	var out []MetricSample

	// CPU: walk hrProcessorLoad, average across cores.
	cpuLoads, err := s.BulkWalkAll(OIDHrProcessorLoad)
	if err == nil && len(cpuLoads) > 0 {
		var sum, n float64
		for _, v := range cpuLoads {
			sum += float64(asInt(v))
			n++
		}
		if n > 0 {
			out = append(out, MetricSample{
				DeviceID: deviceID, Key: "cpu",
				Value: sum / n, Unit: "percent", Timestamp: ts, PollerID: c.pollerID,
			})
		}
	}

	// Memory: walk hrStorageTable, find entries with type=hrStorageRAM,
	// emit used/total/pct.
	types, _ := s.BulkWalkAll(OIDHrStorageType)
	units, _ := s.BulkWalkAll(OIDHrStorageAllocationU)
	sizes, _ := s.BulkWalkAll(OIDHrStorageSize)
	used, _ := s.BulkWalkAll(OIDHrStorageUsed)

	typeByIdx := indexMap(types)
	unitByIdx := indexMap(units)
	sizeByIdx := indexMap(sizes)
	usedByIdx := indexMap(used)

	for idx, typeVar := range typeByIdx {
		if asString(typeVar) != "."+OIDHrStorageRAM && !strings.HasSuffix(typeVar.Value.(string), OIDHrStorageRAM) && !strings.Contains(fmt.Sprint(typeVar.Value), OIDHrStorageRAM) {
			// Not RAM; skip. (Best-effort match; different agents report this differently.)
			continue
		}
		unit := int64(asInt(unitByIdx[idx]))
		sz := int64(asInt(sizeByIdx[idx]))
		us := int64(asInt(usedByIdx[idx]))
		if unit <= 0 || sz <= 0 {
			continue
		}
		totalBytes := float64(sz) * float64(unit)
		usedBytes := float64(us) * float64(unit)
		pct := 0.0
		if totalBytes > 0 {
			pct = usedBytes / totalBytes * 100.0
		}
		out = append(out,
			MetricSample{DeviceID: deviceID, Key: "memory_total_bytes", Value: totalBytes, Unit: "bytes", Timestamp: ts, PollerID: c.pollerID},
			MetricSample{DeviceID: deviceID, Key: "memory_used_bytes", Value: usedBytes, Unit: "bytes", Timestamp: ts, PollerID: c.pollerID},
			MetricSample{DeviceID: deviceID, Key: "memory", Value: pct, Unit: "percent", Timestamp: ts, PollerID: c.pollerID},
		)
		break // first RAM entry is enough
	}

	return out, nil
}

func (c *Collector) collectInterfaces(ctx context.Context, s *g.GoSNMP) ([]Interface, error) {
	// Pull all IF-MIB columns we care about in parallel walks.
	// (gosnmp has no native parallel walk, so serial is fine; interfaces
	// are one bulk walk each — typically <10 PDUs.)
	walks := []struct {
		oid string
		dst map[int]*g.SnmpPDU
	}{}
	collect := func(oid string) map[int]*g.SnmpPDU {
		pdus, err := s.BulkWalkAll(oid)
		if err != nil {
			return nil
		}
		out := make(map[int]*g.SnmpPDU, len(pdus))
		for i := range pdus {
			p := pdus[i]
			idx := lastIndex(p.Name)
			out[idx] = &p
		}
		return out
	}
	_ = walks

	descr := collect(OIDIfDescr)
	if descr == nil {
		return nil, fmt.Errorf("ifDescr walk failed")
	}
	ifType := collect(OIDIfType)
	speed := collect(OIDIfSpeed)
	hspeed := collect(OIDIfHighSpeed)
	phys := collect(OIDIfPhysAddress)
	admin := collect(OIDIfAdminStatus)
	oper := collect(OIDIfOperStatus)
	inO := collect(OIDIfInOctets)
	outO := collect(OIDIfOutOctets)
	inP := collect(OIDIfInUcastPkts)
	outP := collect(OIDIfOutUcastPkts)
	inE := collect(OIDIfInErrors)
	outE := collect(OIDIfOutErrors)
	inD := collect(OIDIfInDiscards)
	outD := collect(OIDIfOutDiscards)

	// 64-bit HC counters — optional but preferred on > 1Gbps interfaces.
	hcInO := collect(OIDIfHCInOctets)
	hcOutO := collect(OIDIfHCOutOctets)
	hcInP := collect(OIDIfHCInUcastPkts)
	hcOutP := collect(OIDIfHCOutUcastPkts)

	name := collect(OIDIfName)
	alias := collect(OIDIfAlias)

	out := make([]Interface, 0, len(descr))
	for idx, d := range descr {
		iface := Interface{
			IfIndex: idx,
			IfDescr: asString(*d),
		}
		if t, ok := ifType[idx]; ok {
			iface.IfType = int(asInt(*t))
		}
		if v, ok := speed[idx]; ok {
			iface.IfSpeed = uint64(asInt(*v))
		}
		// ifHighSpeed is in Mbps; convert to bps and prefer if nonzero.
		if v, ok := hspeed[idx]; ok {
			hs := uint64(asInt(*v))
			if hs > 0 {
				iface.IfSpeed = hs * 1_000_000
			}
		}
		if v, ok := phys[idx]; ok {
			iface.MACAddress = macString(*v)
		}
		if v, ok := admin[idx]; ok {
			iface.AdminStatus = IfStatusNames[int(asInt(*v))]
		}
		if v, ok := oper[idx]; ok {
			iface.OperStatus = IfStatusNames[int(asInt(*v))]
		}

		// Prefer HC counters when present.
		if v, ok := hcInO[idx]; ok && asUint(*v) > 0 {
			iface.InOctets = asUint(*v)
			iface.HasHC = true
		} else if v, ok := inO[idx]; ok {
			iface.InOctets = asUint(*v)
		}
		if v, ok := hcOutO[idx]; ok && asUint(*v) > 0 {
			iface.OutOctets = asUint(*v)
			iface.HasHC = true
		} else if v, ok := outO[idx]; ok {
			iface.OutOctets = asUint(*v)
		}
		if v, ok := hcInP[idx]; ok && asUint(*v) > 0 {
			iface.InUcastPkts = asUint(*v)
		} else if v, ok := inP[idx]; ok {
			iface.InUcastPkts = asUint(*v)
		}
		if v, ok := hcOutP[idx]; ok && asUint(*v) > 0 {
			iface.OutUcastPkts = asUint(*v)
		} else if v, ok := outP[idx]; ok {
			iface.OutUcastPkts = asUint(*v)
		}
		if v, ok := inE[idx]; ok {
			iface.InErrors = asUint(*v)
		}
		if v, ok := outE[idx]; ok {
			iface.OutErrors = asUint(*v)
		}
		if v, ok := inD[idx]; ok {
			iface.InDiscards = asUint(*v)
		}
		if v, ok := outD[idx]; ok {
			iface.OutDiscards = asUint(*v)
		}
		if v, ok := name[idx]; ok {
			iface.IfName = asString(*v)
		} else {
			iface.IfName = iface.IfDescr
		}
		if v, ok := alias[idx]; ok {
			iface.IfAlias = asString(*v)
		}
		out = append(out, iface)
	}
	return out, nil
}

func (c *Collector) collectEntities(ctx context.Context, s *g.GoSNMP) ([]Entity, error) {
	descr, err := s.BulkWalkAll(OIDEntPhysicalDescr)
	if err != nil {
		return nil, err
	}
	contained, _ := s.BulkWalkAll(OIDEntPhysicalContained)
	class, _ := s.BulkWalkAll(OIDEntPhysicalClass)
	name, _ := s.BulkWalkAll(OIDEntPhysicalName)
	hw, _ := s.BulkWalkAll(OIDEntPhysicalHWRev)
	fw, _ := s.BulkWalkAll(OIDEntPhysicalFWRev)
	serial, _ := s.BulkWalkAll(OIDEntPhysicalSerialNum)
	model, _ := s.BulkWalkAll(OIDEntPhysicalModelName)

	containedByIdx := indexMap(contained)
	classByIdx := indexMap(class)
	nameByIdx := indexMap(name)
	hwByIdx := indexMap(hw)
	fwByIdx := indexMap(fw)
	serialByIdx := indexMap(serial)
	modelByIdx := indexMap(model)

	out := make([]Entity, 0, len(descr))
	for _, d := range descr {
		idx := lastIndex(d.Name)
		ent := Entity{
			EntIndex: idx,
			Name:     asString(d),
		}
		if v, ok := containedByIdx[idx]; ok {
			ent.ParentIndex = int(asInt(v))
		}
		if v, ok := classByIdx[idx]; ok {
			ent.Class = EntPhysicalClassNames[int(asInt(v))]
		}
		if v, ok := nameByIdx[idx]; ok {
			ent.Name = asString(v)
		}
		if v, ok := hwByIdx[idx]; ok {
			ent.HWRevision = asString(v)
		}
		if v, ok := fwByIdx[idx]; ok {
			ent.FWRevision = asString(v)
		}
		if v, ok := serialByIdx[idx]; ok {
			ent.SerialNumber = asString(v)
		}
		if v, ok := modelByIdx[idx]; ok {
			ent.ModelName = asString(v)
		}
		out = append(out, ent)
	}
	return out, nil
}

func (c *Collector) collectSensors(
	ctx context.Context, s *g.GoSNMP, deviceID uuid.UUID, ts time.Time,
) ([]Sensor, []MetricSample, error) {
	typeV, err := s.BulkWalkAll(OIDEntPhySensorType)
	if err != nil {
		return nil, nil, err
	}
	scaleV, _ := s.BulkWalkAll(OIDEntPhySensorScale)
	precV, _ := s.BulkWalkAll(OIDEntPhySensorPrecision)
	valueV, _ := s.BulkWalkAll(OIDEntPhySensorValue)
	unitV, _ := s.BulkWalkAll(OIDEntPhySensorUnitsDisplay)

	scaleByIdx := indexMap(scaleV)
	precByIdx := indexMap(precV)
	valueByIdx := indexMap(valueV)
	unitByIdx := indexMap(unitV)

	sensors := make([]Sensor, 0, len(typeV))
	samples := make([]MetricSample, 0, len(typeV))
	for _, t := range typeV {
		idx := lastIndex(t.Name)
		tCode := int(asInt(t))
		typeName := EntPhySensorTypeNames[tCode]
		raw := float64(asInt(valueByIdx[idx]))
		scale := int(asInt(scaleByIdx[idx]))
		prec := int(asInt(precByIdx[idx]))
		val := applyScale(raw, scale, prec)
		unit := asString(unitByIdx[idx])
		if unit == "" {
			unit = typeName
		}

		sensors = append(sensors, Sensor{
			SensorIndex: idx,
			SensorType:  typeName,
			Unit:        unit,
			Value:       val,
		})

		// Emit a normalized scalar for thresholding. Only types with
		// a clear meaning get mapped; others are left out.
		key := sensorKey(typeName, idx)
		if key != "" {
			samples = append(samples, MetricSample{
				DeviceID: deviceID, Key: key, Value: val,
				Unit: unit, Timestamp: ts, PollerID: c.pollerID,
			})
		}
	}
	return sensors, samples, nil
}

// diffInterfaces converts interface counter snapshots into
// InterfaceSample with computed bps. Handles 32-bit counter wraps.
func (c *Collector) diffInterfaces(deviceID uuid.UUID, ifs []Interface, ts time.Time) []InterfaceSample {
	c.mu.Lock()
	defer c.mu.Unlock()

	prev := c.prevIfs[deviceID]
	if prev == nil {
		prev = make(map[uint32]ifSnapshot)
	}
	cur := make(map[uint32]ifSnapshot, len(ifs))
	samples := make([]InterfaceSample, 0, len(ifs))

	for i := range ifs {
		iface := ifs[i]
		idx := uint32(iface.IfIndex)
		snap := ifSnapshot{
			inOctets:  iface.InOctets,
			outOctets: iface.OutOctets,
			at:        ts,
			hc:        iface.HasHC,
		}
		cur[idx] = snap

		inBps, outBps := 0.0, 0.0
		if p, ok := prev[idx]; ok {
			dt := ts.Sub(p.at).Seconds()
			if dt > 0 {
				inBps = rateBps(iface.InOctets, p.inOctets, dt, iface.HasHC)
				outBps = rateBps(iface.OutOctets, p.outOctets, dt, iface.HasHC)
			}
		}

		opStatus := uint8(0)
		if iface.OperStatus == "up" {
			opStatus = 1
		}

		samples = append(samples, InterfaceSample{
			DeviceID:     deviceID,
			IfIndex:      idx,
			InOctets:     iface.InOctets,
			OutOctets:    iface.OutOctets,
			InUcastPkts:  iface.InUcastPkts,
			OutUcastPkts: iface.OutUcastPkts,
			InErrors:     iface.InErrors,
			OutErrors:    iface.OutErrors,
			InDiscards:   iface.InDiscards,
			OutDiscards:  iface.OutDiscards,
			OperStatus:   opStatus,
			InBps:        inBps,
			OutBps:       outBps,
			Timestamp:    ts,
			PollerID:     c.pollerID,
		})
	}
	c.prevIfs[deviceID] = cur
	return samples
}

// rateBps computes bits-per-second from two byte counters, handling
// the 32-bit wrap case. If the new value is less than the old, we
// assume either a wrap (for 32-bit) or a counter reset (for 64-bit).
// In the reset case we return 0 rather than a negative or absurd
// rate.
func rateBps(cur, prev uint64, dtSec float64, hc bool) float64 {
	var delta uint64
	switch {
	case cur >= prev:
		delta = cur - prev
	case !hc && prev < math.MaxUint32:
		// 32-bit wrap
		delta = (math.MaxUint32 - prev) + cur + 1
	default:
		// Counter reset on 64-bit or out-of-range: drop the sample.
		return 0
	}
	return float64(delta) * 8.0 / dtSec
}

// --- small helpers ---

func asString(v g.SnmpPDU) string {
	switch x := v.Value.(type) {
	case string:
		return x
	case []byte:
		return string(x)
	case nil:
		return ""
	default:
		return fmt.Sprint(x)
	}
}

func asInt(v g.SnmpPDU) int64 {
	switch x := v.Value.(type) {
	case int:
		return int64(x)
	case int32:
		return int64(x)
	case int64:
		return x
	case uint:
		return int64(x)
	case uint32:
		return int64(x)
	case uint64:
		return int64(x)
	case string:
		n, _ := strconv.ParseInt(x, 10, 64)
		return n
	case nil:
		return 0
	default:
		n, _ := strconv.ParseInt(fmt.Sprint(x), 10, 64)
		return n
	}
}

func asUint(v g.SnmpPDU) uint64 {
	switch x := v.Value.(type) {
	case uint:
		return uint64(x)
	case uint32:
		return uint64(x)
	case uint64:
		return x
	case int:
		if x < 0 {
			return 0
		}
		return uint64(x)
	case int64:
		if x < 0 {
			return 0
		}
		return uint64(x)
	case int32:
		if x < 0 {
			return 0
		}
		return uint64(x)
	case nil:
		return 0
	default:
		n, _ := strconv.ParseUint(fmt.Sprint(x), 10, 64)
		return n
	}
}

func macString(v g.SnmpPDU) string {
	b, ok := v.Value.([]byte)
	if !ok || len(b) != 6 {
		return ""
	}
	return fmt.Sprintf("%02x:%02x:%02x:%02x:%02x:%02x", b[0], b[1], b[2], b[3], b[4], b[5])
}

func lastIndex(oid string) int {
	i := strings.LastIndex(oid, ".")
	if i < 0 || i == len(oid)-1 {
		return 0
	}
	n, _ := strconv.Atoi(oid[i+1:])
	return n
}

func indexMap(pdus []g.SnmpPDU) map[int]g.SnmpPDU {
	out := make(map[int]g.SnmpPDU, len(pdus))
	for i := range pdus {
		out[lastIndex(pdus[i].Name)] = pdus[i]
	}
	return out
}

// matchOID compares gosnmp-returned names (often prefixed with ".")
// against OID constants without the leading dot.
func matchOID(full, want string) bool {
	return strings.TrimPrefix(full, ".") == want ||
		strings.HasPrefix(strings.TrimPrefix(full, "."), want+".")
}

// applyScale converts ENTITY-SENSOR-MIB scale+precision into a float.
// scale: 1=yocto … 9=units … 17=yotta (RFC 3433). precision is a power
// of ten applied after scale.
func applyScale(raw float64, scale, prec int) float64 {
	mult := 1.0
	switch {
	case scale == 0 || scale == 9: // units
		// no change
	case scale >= 1 && scale <= 8: // sub-unit
		mult = math.Pow10(-3 * (9 - scale))
	case scale >= 10 && scale <= 17: // super-unit
		mult = math.Pow10(3 * (scale - 9))
	}
	v := raw * mult
	if prec != 0 {
		v = v * math.Pow10(-prec)
	}
	return v
}

func sensorKey(sensorType string, idx int) string {
	switch sensorType {
	case "celsius":
		return fmt.Sprintf("temperature_%d", idx)
	case "rpm":
		return fmt.Sprintf("fan_%d", idx)
	case "voltsDC", "voltsAC":
		return fmt.Sprintf("voltage_%d", idx)
	case "amperes":
		return fmt.Sprintf("amperage_%d", idx)
	case "watts":
		return fmt.Sprintf("power_%d", idx)
	case "percentRH":
		return fmt.Sprintf("humidity_%d", idx)
	}
	return ""
}
