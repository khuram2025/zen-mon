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

	mu      sync.Mutex
	prevIfs map[uuid.UUID]map[uint32]ifSnapshot
	prevTpl map[uuid.UUID]map[string]tplSnap
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
		prevTpl:  make(map[uuid.UUID]map[string]tplSnap),
	}
}

// Collect runs all enabled collectors for a device and writes results
// progressively into r so that partial output is preserved even if the
// caller races this against a timeout. Any partial success is kept —
// a single failing collector does not cause the entire poll to be
// discarded.
func (c *Collector) Collect(ctx context.Context, d *Device, r *Result) {
	start := time.Now()

	client, err := c.sessions.Acquire(d)
	if err != nil {
		r.Mu.Lock()
		r.Err = err
		r.Duration = time.Since(start)
		r.Mu.Unlock()
		c.sessions.MarkFailure(d.ID)
		return
	}

	// Bail early if the caller cancelled before we even started.
	if ctx.Err() != nil {
		r.Mu.Lock()
		r.Err = ctx.Err()
		r.Duration = time.Since(start)
		r.Mu.Unlock()
		return
	}

	// 1) System info — fast (single GET with 6 OIDs). Always collect
	// first so that even if every subsequent walk times out we've still
	// persisted the device's identity (sysObjectID → vendor/model).
	sys, sysErr := c.collectSystem(ctx, client)
	if sysErr == nil {
		r.Mu.Lock()
		r.System = sys
		if sys.SysUpTime > 0 {
			r.Scalars = append(r.Scalars, MetricSample{
				DeviceID: d.ID, Key: "uptime",
				Value: sys.SysUpTime.Seconds(), Unit: "seconds", Timestamp: start, PollerID: c.pollerID,
			})
		}
		r.Mu.Unlock()
	}

	sysOID := d.SysObjectID
	if sys != nil && sys.SysObjectID != "" {
		sysOID = sys.SysObjectID
	}

	// 2) Host-Resources: CPU + memory
	if ctx.Err() == nil {
		scalars, _ := c.collectHostResources(ctx, client, d.ID, start, sysOID)
		r.Mu.Lock()
		r.Scalars = append(r.Scalars, scalars...)
		r.Mu.Unlock()
	}

	// 2.5) Monitoring template — vendor-specific OID groups declared by
	// the device's profile. Runs early so the high-value vendor insights
	// survive a budget kill during the (much heavier) interface walk.
	if len(d.OidGroups) > 0 && ctx.Err() == nil {
		tplSamples, tplValues, tplGroups := c.collectTemplateMetrics(ctx, client, d, start)
		if len(tplValues) > 0 || len(tplGroups) > 0 {
			r.Mu.Lock()
			r.Scalars = append(r.Scalars, tplSamples...)
			r.TplValues = append(r.TplValues, tplValues...)
			r.TplGroups = append(r.TplGroups, tplGroups...)
			r.Mu.Unlock()
		}
	}

	// 3) Interfaces — the big one. Skip if we're already cancelled.
	var ifErr error
	if ctx.Err() == nil {
		var ifs []Interface
		ifs, ifErr = c.collectInterfaces(ctx, client)
		if ifErr == nil {
			samples := c.diffInterfaces(d.ID, ifs, start)
			r.Mu.Lock()
			r.Interfaces = ifs
			r.IfSamples = samples
			r.Mu.Unlock()
		}
	}

	// 4) Entities — small walk, cheap.
	if ctx.Err() == nil {
		ents, _ := c.collectEntities(ctx, client)
		if len(ents) > 0 {
			r.Mu.Lock()
			r.Entities = ents
			r.Mu.Unlock()
		}
	}

	// 5) Sensors — scale+precision+value, returned as MetricSample.
	if ctx.Err() == nil {
		sensors, sensorScalars, _ := c.collectSensors(ctx, client, d.ID, start)
		if len(sensors) > 0 || len(sensorScalars) > 0 {
			r.Mu.Lock()
			r.Sensors = sensors
			r.Scalars = append(r.Scalars, sensorScalars...)
			r.Mu.Unlock()
		}
	}

	// 6) UDT — bridge FDB, ARP/ND, LLDP/CDP, VLANs. Runs on its own
	// cadence (engine sets WantUdt); partial output is still persisted.
	r.Mu.Lock()
	wantUdt := r.WantUdt
	ifsForUdt := r.Interfaces
	r.Mu.Unlock()
	if wantUdt && ctx.Err() == nil {
		d2 := *d
		if sysOID != "" {
			d2.SysObjectID = sysOID
		}
		udt, _ := c.CollectUDT(ctx, &d2, client, ifsForUdt)
		if udt != nil {
			r.Mu.Lock()
			r.Udt = udt
			r.Scalars = append(r.Scalars, MetricSample{
				DeviceID: d.ID, Key: "udt_mac_count",
				Value: float64(len(udt.Fdb)), Unit: "count", Timestamp: start, PollerID: c.pollerID,
			})
			r.Mu.Unlock()
		}
	}

	r.Mu.Lock()
	if sysErr != nil && ifErr != nil {
		r.Err = fmt.Errorf("system+interfaces failed: sys=%v if=%v", sysErr, ifErr)
		c.sessions.MarkFailure(d.ID)
	} else {
		c.sessions.MarkSuccess(d.ID)
	}
	r.Duration = time.Since(start)
	r.Mu.Unlock()
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
	ctx context.Context, s *g.GoSNMP, deviceID uuid.UUID, ts time.Time, sysObjectID string,
) ([]MetricSample, error) {
	var out []MetricSample
	hasCPU := false
	hasMem := false

	// 1) Standard HOST-RESOURCES-MIB — CPU
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
			hasCPU = true
		}
	}

	// 2) Standard HOST-RESOURCES-MIB — Memory
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
		hasMem = true
		break
	}

	// 3) Vendor-specific fallbacks when standard MIBs return nothing.
	cleanOID := strings.TrimPrefix(sysObjectID, ".")
	if !hasCPU || !hasMem {
		vendorMetrics := c.collectVendorMetrics(s, deviceID, ts, cleanOID, hasCPU, hasMem)
		out = append(out, vendorMetrics...)
	}

	return out, nil
}

// collectVendorMetrics tries vendor-specific OIDs for CPU and memory
// based on the device's sysObjectID prefix.
func (c *Collector) collectVendorMetrics(
	s *g.GoSNMP, deviceID uuid.UUID, ts time.Time,
	sysOID string, hasCPU, hasMem bool,
) []MetricSample {
	var out []MetricSample

	// Detect vendor from sysObjectID prefix
	isCisco := strings.HasPrefix(sysOID, "1.3.6.1.4.1.9.")
	isForti := strings.HasPrefix(sysOID, "1.3.6.1.4.1.12356.")
	isPAN := strings.HasPrefix(sysOID, "1.3.6.1.4.1.25461.")
	isJuniper := strings.HasPrefix(sysOID, "1.3.6.1.4.1.2636.")
	isAruba := strings.HasPrefix(sysOID, "1.3.6.1.4.1.14823.")

	mk := func(key string, val float64, unit string) MetricSample {
		return MetricSample{DeviceID: deviceID, Key: key, Value: val, Unit: unit, Timestamp: ts, PollerID: c.pollerID}
	}

	// ── Cisco ──
	if isCisco {
		if !hasCPU {
			// Try CISCO-PROCESS-MIB first (cpmCPUTotal5minRev)
			if v := getScalar(s, OIDCiscoCPU5min); v >= 0 {
				out = append(out, mk("cpu", v, "percent"))
			} else if v := getScalar(s, OIDCiscoCPU1min); v >= 0 {
				out = append(out, mk("cpu", v, "percent"))
			} else if v := getScalar(s, OIDCiscoCPUBusy); v >= 0 {
				// OLD-CISCO-CPU-MIB fallback
				out = append(out, mk("cpu", v, "percent"))
			}
		}
		if !hasMem {
			// CISCO-MEMORY-POOL-MIB — walk used+free, sum processor pools
			usedVals, _ := s.BulkWalkAll(OIDCiscoMemPoolUsed)
			freeVals, _ := s.BulkWalkAll(OIDCiscoMemPoolFree)
			if len(usedVals) > 0 && len(freeVals) > 0 {
				var totalUsed, totalFree float64
				for _, v := range usedVals {
					totalUsed += float64(asUint(v))
				}
				for _, v := range freeVals {
					totalFree += float64(asUint(v))
				}
				total := totalUsed + totalFree
				if total > 0 {
					pct := totalUsed / total * 100.0
					out = append(out,
						mk("memory_total_bytes", total, "bytes"),
						mk("memory_used_bytes", totalUsed, "bytes"),
						mk("memory", pct, "percent"),
					)
				}
			}
		}
	}

	// ── Fortinet ──
	if isForti {
		if !hasCPU {
			if v := getScalar(s, OIDFortiCPU); v >= 0 {
				out = append(out, mk("cpu", v, "percent"))
			}
		}
		if !hasMem {
			if v := getScalar(s, OIDFortiMem); v >= 0 {
				out = append(out, mk("memory", v, "percent"))
			}
		}
		// Bonus: session count as extra metric
		if v := getScalar(s, OIDFortiSesCount); v >= 0 {
			out = append(out, mk("sessions", v, "count"))
		}
	}

	// ── Palo Alto ──
	if isPAN {
		if !hasCPU {
			if v := getScalar(s, OIDPanSysCPU); v >= 0 {
				out = append(out, mk("cpu", v, "percent"))
			} else if v := getScalar(s, OIDPanCPU); v >= 0 {
				out = append(out, mk("cpu", v, "percent"))
			}
		}
		if !hasMem {
			if v := getScalar(s, OIDPanSysMem); v >= 0 {
				out = append(out, mk("memory", v, "percent"))
			}
		}
	}

	// ── Juniper ──
	if isJuniper {
		if !hasCPU {
			// Walk jnxOperatingCPU, average across routing engines
			cpuVals, _ := s.BulkWalkAll(OIDJnxCPU)
			if len(cpuVals) > 0 {
				var sum float64
				for _, v := range cpuVals {
					sum += float64(asInt(v))
				}
				out = append(out, mk("cpu", sum/float64(len(cpuVals)), "percent"))
			}
		}
		if !hasMem {
			memVals, _ := s.BulkWalkAll(OIDJnxMem)
			if len(memVals) > 0 {
				var sum float64
				for _, v := range memVals {
					sum += float64(asInt(v))
				}
				out = append(out, mk("memory", sum/float64(len(memVals)), "percent"))
			}
		}
	}

	// ── Aruba / HPE ──
	if isAruba {
		if !hasCPU {
			if v := getScalar(s, OIDArubaAPCPU); v >= 0 {
				out = append(out, mk("cpu", v, "percent"))
			}
		}
	}

	return out
}

// getScalar does a single SNMP GET and returns the numeric value, or -1 on failure.
func getScalar(s *g.GoSNMP, oid string) float64 {
	res, err := s.Get([]string{oid})
	if err != nil || len(res.Variables) == 0 {
		return -1
	}
	v := res.Variables[0]
	switch v.Type {
	case g.NoSuchObject, g.NoSuchInstance, g.EndOfMibView:
		return -1
	}
	val := float64(asInt(v))
	if val == 0 {
		// Could be genuine 0% or a non-numeric — check unsigned too
		uval := float64(asUint(v))
		if uval > 0 {
			return uval
		}
	}
	return val
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
