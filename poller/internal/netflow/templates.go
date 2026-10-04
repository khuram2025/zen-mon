package netflow

import (
	"net/netip"
	"slices"
	"sync"
	"time"
)

// v9TemplateTTL bounds how long a learned template is trusted without being
// re-advertised. Routers re-send templates periodically; a generous TTL avoids
// dropping data between refreshes while still expiring templates from exporters
// that have gone away (BUG-15).
const v9TemplateTTL = 24 * time.Hour

// templateReannounce is how often an unchanged, still-advertised template is
// reported again for the zenplus.flow_templates registry, so its last_seen
// stays within a day even when the definition never changes.
const templateReannounce = 24 * time.Hour

// V9TemplateField is one field specifier of a v9 or IPFIX template. Type keeps
// the IPFIX enterprise bit (0x8000) so an enterprise-specific IE can never be
// mistaken for the standard IE with the same number; Enterprise is the private
// enterprise number that followed it (0 for standard IEs and for v9).
type V9TemplateField struct {
	Type       uint16
	Length     uint16 // 0xFFFF = IPFIX variable length
	Enterprise uint32
}

type V9Template struct {
	ID     uint16
	Fields []V9TemplateField
	Length int // sum of fixed field lengths (variable-length fields contribute 0)

	dec recordLayout // decode form, compiled when the template is stored (Put)
}

// V9OptionsTemplate describes a v9 Options Template (FlowSet 1) or an IPFIX
// Options Template (Set 3). Its data records carry exporter metadata (sampler
// definitions, system init time, application names), never traffic flows.
type V9OptionsTemplate struct {
	ID          uint16
	ScopeFields []V9TemplateField
	Fields      []V9TemplateField
	Length      int

	dec recordLayout // scope fields then option fields (PutOptions)
}

// recordLayout is the decode form of a template: its fields without the
// zero-length fixed ones, the shortest encoding of one record, and how many of
// the kept fields are options scope fields. A zero-length field occupies no
// bytes, so dropping it changes nothing about the record, while keeping it
// would let a template of thousands of such fields turn every byte of a data
// set into thousands of loop iterations. With every kept field consuming at
// least one byte, decoding a datagram costs O(its length) whatever the
// template says; compiling once per learned template keeps the per-data-set
// cost O(1) as well.
type recordLayout struct {
	fields []V9TemplateField
	minLen int
	scope  int
}

func compileLayout(scope, fields []V9TemplateField) recordLayout {
	l := recordLayout{fields: make([]V9TemplateField, 0, len(scope)+len(fields))}
	for i, f := range slices.Concat(scope, fields) {
		switch {
		case f.Length == 0:
			continue
		case f.Length == 0xFFFF: // variable length: at least its length byte
			l.minLen++
		default:
			l.minLen += int(f.Length)
		}
		l.fields = append(l.fields, f)
		if i < len(scope) {
			l.scope++
		}
	}
	return l
}

// decodable reports whether a field list can describe a record at all (some
// field occupies at least one byte).
func decodable(fields []V9TemplateField) bool {
	for _, f := range fields {
		if f.Length != 0 {
			return true
		}
	}
	return false
}

// templateKey identifies a template within one exporter's transport session:
// the full canonical exporter address (never a shared 0.0.0.0 fallback, which
// let two IPv6 exporters decode each other's data with the wrong layout), the
// observation domain / source ID and the template ID.
type templateKey struct {
	exporter netip.Addr
	domain   uint32
	id       uint16
}

// DomainKey is one exporter observation domain.
type DomainKey struct {
	Exporter netip.Addr
	Domain   uint32
}

// samplerKey identifies a sampler: exporters with several samplers (per
// interface or per monitor) tag each flow with the sampler / selector ID whose
// rate applies (byID). The rate an exporter announced without a sampler ID is
// the exporter-domain default (byID=false, id 0).
type samplerKey struct {
	exporter netip.Addr
	domain   uint32
	id       uint64
	byID     bool
}

type cachedTemplate struct {
	tpl         V9Template
	learnedAt   time.Time
	announcedAt time.Time
}

type cachedOptions struct {
	tpl         V9OptionsTemplate
	learnedAt   time.Time
	announcedAt time.Time
}

type samplerEntry struct {
	interval uint32
}

// SamplerState is what the learned samplers say about one record's rate.
type SamplerState uint8

const (
	// SamplerNone: no sampling rate was learned for the exporter domain.
	SamplerNone SamplerState = iota
	// SamplerKnown: the rate of the sampler the record names, the domain
	// default, or the one rate every sampler of the domain shares.
	SamplerKnown
	// SamplerAmbiguous: the record names no known sampler, there is no domain
	// default, and the domain's samplers have different rates. Picking one
	// would scale the record by an arbitrary factor (X-08), so it stays 1 and
	// is flagged (FieldSamplingAmbiguous).
	SamplerAmbiguous
)

// Cache bounds. The collector listens on an open UDP port, so whatever a
// datagram announces must not grow memory without limit: one source cycling
// through template IDs, observation domains or sampler IDs (a broken exporter,
// or packets with a forged source) is capped per exporter address, and the
// whole cache is capped for many sources. Real exporters stay far below: a
// router uses tens of templates per observation domain and a few samplers.
// A template or sampler refused at a bound is reported (TemplatesRefused) and
// its data waits as if the template had not arrived yet, so the refusal is
// visible as data_sets_waiting_template rather than as wrong data.
const (
	maxTemplatesPerExporter = 2048
	maxTemplates            = 65536
	maxSamplersPerExporter  = 1024
	maxSamplers             = 65536
)

// V9TemplateCache holds the templates, options templates, samplers and IPFIX
// system-init times learned from one protocol's exporters. It is safe for
// concurrent use (BUG-15), expires templates after ttl and is bounded (see
// maxTemplatesPerExporter).
type V9TemplateCache struct {
	mu               sync.RWMutex
	ttl              time.Duration
	now              func() time.Time
	templates        map[templateKey]*cachedTemplate
	optionsTemplates map[templateKey]*cachedOptions
	samplers         map[samplerKey]samplerEntry
	initTimes        map[DomainKey]uint64
	// idRates counts the per-ID samplers of each exporter domain by rate, so
	// a record that names no sampler resolves in O(1): one distinct rate is
	// unambiguous, several are not.
	idRates map[DomainKey]map[uint32]int

	// Entry counts per exporter address, for the bounds, and the template IDs
	// of each exporter domain per kind, so a withdrawal of all templates
	// touches only that domain (a datagram can carry 16,000 of them; scanning
	// the whole cache for each would make it a CPU amplifier).
	templatesPer map[netip.Addr]int
	samplersPer  map[netip.Addr]int
	dataIDs      map[DomainKey]map[uint16]struct{}
	optionIDs    map[DomainKey]map[uint16]struct{}

	// version counts changes (templates learned, changed or removed, samplers
	// and init times learned or changed), so the collector persists the cache
	// only when there is something new (see Snapshot).
	version uint64
}

func NewV9TemplateCache() *V9TemplateCache {
	return &V9TemplateCache{
		ttl:              v9TemplateTTL,
		now:              time.Now,
		templates:        make(map[templateKey]*cachedTemplate),
		optionsTemplates: make(map[templateKey]*cachedOptions),
		samplers:         make(map[samplerKey]samplerEntry),
		initTimes:        make(map[DomainKey]uint64),
		idRates:          make(map[DomainKey]map[uint32]int),
		templatesPer:     make(map[netip.Addr]int),
		samplersPer:      make(map[netip.Addr]int),
		dataIDs:          make(map[DomainKey]map[uint16]struct{}),
		optionIDs:        make(map[DomainKey]map[uint16]struct{}),
	}
}

// Get returns the live data template for (exporter, domain, id).
func (c *V9TemplateCache) Get(exporter netip.Addr, domain uint32, id uint16) (V9Template, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	e, ok := c.templates[templateKey{CanonicalAddr(exporter), domain, id}]
	if !ok || c.now().Sub(e.learnedAt) > c.ttl {
		return V9Template{}, false
	}
	return e.tpl, true
}

// roomLocked reports whether a new template key of this exporter fits.
func (c *V9TemplateCache) roomLocked(exporter netip.Addr) bool {
	return c.templatesPer[exporter] < maxTemplatesPerExporter && len(c.templates)+len(c.optionsTemplates) < maxTemplates
}

func (c *V9TemplateCache) idsLocked(options bool) map[DomainKey]map[uint16]struct{} {
	if options {
		return c.optionIDs
	}
	return c.dataIDs
}

// trackLocked accounts for a key just added to the templates (options=false)
// or optionsTemplates map.
func (c *V9TemplateCache) trackLocked(key templateKey, options bool) {
	c.version++
	c.templatesPer[key.exporter]++
	idx, dk := c.idsLocked(options), DomainKey{key.exporter, key.domain}
	ids := idx[dk]
	if ids == nil {
		ids = make(map[uint16]struct{})
		idx[dk] = ids
	}
	ids[key.id] = struct{}{}
}

// dropLocked removes a key of one kind, with its accounting; a key that is
// not present is a no-op.
func (c *V9TemplateCache) dropLocked(key templateKey, options bool) {
	if options {
		if _, ok := c.optionsTemplates[key]; !ok {
			return
		}
		delete(c.optionsTemplates, key)
	} else {
		if _, ok := c.templates[key]; !ok {
			return
		}
		delete(c.templates, key)
	}
	c.version++
	if n := c.templatesPer[key.exporter] - 1; n > 0 {
		c.templatesPer[key.exporter] = n
	} else {
		delete(c.templatesPer, key.exporter)
	}
	idx, dk := c.idsLocked(options), DomainKey{key.exporter, key.domain}
	if ids := idx[dk]; ids != nil {
		delete(ids, key.id)
		if len(ids) == 0 {
			delete(idx, dk)
		}
	}
}

// Put stores a data template. A data template and an options template can
// never share an ID within one domain, so learning one evicts the other: an
// exporter that re-uses a former data-template ID for options (after a reboot
// or reconfiguration) must not have its options records decoded as traffic
// with the stale layout (X-12). register reports whether the template should
// be (re)registered: new, changed, or not reported for templateReannounce.
// stored=false means the cache bound refused a new template.
func (c *V9TemplateCache) Put(exporter netip.Addr, domain uint32, tpl V9Template) (register, stored bool) {
	key := templateKey{CanonicalAddr(exporter), domain, tpl.ID}
	tpl.dec = compileLayout(nil, tpl.Fields)
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.templates[key]
	if ok && slices.Equal(e.tpl.Fields, tpl.Fields) {
		e.learnedAt = now
		if now.Sub(e.announcedAt) < templateReannounce {
			return false, true
		}
		e.announcedAt = now
		return true, true
	}
	c.dropLocked(key, true)
	if !ok && !c.roomLocked(key.exporter) {
		return false, false
	}
	c.templates[key] = &cachedTemplate{tpl: tpl, learnedAt: now, announcedAt: now}
	if ok {
		c.version++ // a known ID with a new layout
	} else {
		c.trackLocked(key, false)
	}
	return true, true
}

// GetOptions reports whether id refers to a live Options Template for this
// exporter domain (i.e. its data records are metadata, not flows).
func (c *V9TemplateCache) GetOptions(exporter netip.Addr, domain uint32, id uint16) (V9OptionsTemplate, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	e, ok := c.optionsTemplates[templateKey{CanonicalAddr(exporter), domain, id}]
	if !ok || c.now().Sub(e.learnedAt) > c.ttl {
		return V9OptionsTemplate{}, false
	}
	return e.tpl, true
}

// PutOptions stores an options template, evicting a data template with the
// same ID (see Put). The return values have the same meaning as Put's.
func (c *V9TemplateCache) PutOptions(exporter netip.Addr, domain uint32, tpl V9OptionsTemplate) (register, stored bool) {
	key := templateKey{CanonicalAddr(exporter), domain, tpl.ID}
	tpl.dec = compileLayout(tpl.ScopeFields, tpl.Fields)
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.optionsTemplates[key]
	if ok && slices.Equal(e.tpl.ScopeFields, tpl.ScopeFields) && slices.Equal(e.tpl.Fields, tpl.Fields) {
		e.learnedAt = now
		if now.Sub(e.announcedAt) < templateReannounce {
			return false, true
		}
		e.announcedAt = now
		return true, true
	}
	c.dropLocked(key, false)
	if !ok && !c.roomLocked(key.exporter) {
		return false, false
	}
	c.optionsTemplates[key] = &cachedOptions{tpl: tpl, learnedAt: now, announcedAt: now}
	if ok {
		c.version++
	} else {
		c.trackLocked(key, true)
	}
	return true, true
}

// Withdraw removes one template of either kind (IPFIX template withdrawal).
func (c *V9TemplateCache) Withdraw(exporter netip.Addr, domain uint32, id uint16) {
	key := templateKey{CanonicalAddr(exporter), domain, id}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.dropLocked(key, false)
	c.dropLocked(key, true)
}

// WithdrawAll removes every data template (options=false) or every options
// template (options=true) of an exporter domain (IPFIX "all templates"
// withdrawal). Its cost is the number of templates of that domain.
func (c *V9TemplateCache) WithdrawAll(exporter netip.Addr, domain uint32, options bool) {
	exporter = CanonicalAddr(exporter)
	c.mu.Lock()
	defer c.mu.Unlock()
	for id := range c.idsLocked(options)[DomainKey{exporter, domain}] {
		c.dropLocked(templateKey{exporter, domain, id}, options)
	}
}

// PutSampler records a learned 1-in-N sampling interval. samplerID is the
// sampler/selector the options record described; hasID=false means the record
// described the exporter domain as a whole (the domain default). A per-ID
// sampler applies only to records naming its ID: it never becomes the domain
// default, so the rate of a record that names no sampler cannot depend on
// which sampler the exporter happened to announce last (X-08). A new sampler
// beyond the cache bounds is ignored.
func (c *V9TemplateCache) PutSampler(exporter netip.Addr, domain uint32, samplerID uint64, hasID bool, interval uint32) {
	if interval == 0 {
		return
	}
	exporter = CanonicalAddr(exporter)
	c.mu.Lock()
	defer c.mu.Unlock()
	if !hasID {
		c.putSamplerLocked(samplerKey{exporter, domain, 0, false}, interval)
		return
	}
	c.putSamplerLocked(samplerKey{exporter, domain, samplerID, true}, interval)
}

func (c *V9TemplateCache) putSamplerLocked(k samplerKey, interval uint32) {
	cur, ok := c.samplers[k]
	if ok && cur.interval == interval {
		return
	}
	if !ok {
		if c.samplersPer[k.exporter] >= maxSamplersPerExporter || len(c.samplers) >= maxSamplers {
			return
		}
		c.samplersPer[k.exporter]++
	} else {
		c.unindexSamplerLocked(k, cur.interval)
	}
	c.samplers[k] = samplerEntry{interval: interval}
	if k.byID {
		dk := DomainKey{k.exporter, k.domain}
		rates := c.idRates[dk]
		if rates == nil {
			rates = make(map[uint32]int)
			c.idRates[dk] = rates
		}
		rates[interval]++
	}
	c.version++
}

func (c *V9TemplateCache) deleteSamplerLocked(k samplerKey) {
	e, ok := c.samplers[k]
	if !ok {
		return
	}
	delete(c.samplers, k)
	c.unindexSamplerLocked(k, e.interval)
	c.version++
	if c.samplersPer[k.exporter]--; c.samplersPer[k.exporter] <= 0 {
		delete(c.samplersPer, k.exporter)
	}
}

func (c *V9TemplateCache) unindexSamplerLocked(k samplerKey, interval uint32) {
	if !k.byID {
		return
	}
	dk := DomainKey{k.exporter, k.domain}
	rates := c.idRates[dk]
	if rates[interval]--; rates[interval] <= 0 {
		delete(rates, interval)
	}
	if len(rates) == 0 {
		delete(c.idRates, dk)
	}
}

// ResolveSampler returns the sampling interval for a record of the exporter
// domain: the rate of the sampler the record names (hasID), else the domain
// default announced without a sampler ID, else the rate all of the domain's
// samplers share. When they disagree the state is SamplerAmbiguous (no
// interval); when nothing was learned it is SamplerNone.
func (c *V9TemplateCache) ResolveSampler(exporter netip.Addr, domain uint32, samplerID uint64, hasID bool) (uint32, SamplerState) {
	exporter = CanonicalAddr(exporter)
	c.mu.RLock()
	defer c.mu.RUnlock()
	if hasID {
		if e, ok := c.samplers[samplerKey{exporter, domain, samplerID, true}]; ok {
			return e.interval, SamplerKnown
		}
	}
	if e, ok := c.samplers[samplerKey{exporter, domain, 0, false}]; ok {
		return e.interval, SamplerKnown
	}
	rates := c.idRates[DomainKey{exporter, domain}]
	if len(rates) == 1 {
		for n := range rates {
			return n, SamplerKnown
		}
	}
	if len(rates) > 1 {
		return 0, SamplerAmbiguous
	}
	return 0, SamplerNone
}

// Sampler is ResolveSampler reduced to "known rate or not" (ok=false when no
// rate applies: nothing learned, or several conflicting samplers).
func (c *V9TemplateCache) Sampler(exporter netip.Addr, domain uint32, samplerID uint64, hasID bool) (uint32, bool) {
	n, state := c.ResolveSampler(exporter, domain, samplerID, hasID)
	return n, state == SamplerKnown
}

// PutInitTime / InitTime keep the IPFIX systemInitTimeMilliseconds (IE 160)
// an exporter announced in options data, needed to turn flowStart/EndSysUpTime
// (IE 22/21) into wall-clock times.
func (c *V9TemplateCache) PutInitTime(exporter netip.Addr, domain uint32, ms uint64) {
	k := DomainKey{CanonicalAddr(exporter), domain}
	c.mu.Lock()
	defer c.mu.Unlock()
	if cur, ok := c.initTimes[k]; !ok || cur != ms {
		c.version++
	}
	c.initTimes[k] = ms
}

func (c *V9TemplateCache) InitTime(exporter netip.Addr, domain uint32) (uint64, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	ms, ok := c.initTimes[DomainKey{CanonicalAddr(exporter), domain}]
	return ms, ok
}

// ActiveTemplates counts the live data + options templates per exporter
// domain (flow_collector_stats.templates_active).
func (c *V9TemplateCache) ActiveTemplates() map[DomainKey]int {
	now := c.now()
	out := make(map[DomainKey]int)
	c.mu.RLock()
	defer c.mu.RUnlock()
	for k, e := range c.templates {
		if now.Sub(e.learnedAt) <= c.ttl {
			out[DomainKey{k.exporter, k.domain}]++
		}
	}
	for k, e := range c.optionsTemplates {
		if now.Sub(e.learnedAt) <= c.ttl {
			out[DomainKey{k.exporter, k.domain}]++
		}
	}
	return out
}

// Prune drops expired templates so exporters that went away do not keep
// memory forever. Samplers and init times of domains with no live template
// left are dropped with them.
func (c *V9TemplateCache) Prune() {
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	live := make(map[DomainKey]bool)
	for k, e := range c.templates {
		if now.Sub(e.learnedAt) > c.ttl {
			c.dropLocked(k, false)
			continue
		}
		live[DomainKey{k.exporter, k.domain}] = true
	}
	for k, e := range c.optionsTemplates {
		if now.Sub(e.learnedAt) > c.ttl {
			c.dropLocked(k, true)
			continue
		}
		live[DomainKey{k.exporter, k.domain}] = true
	}
	for k := range c.samplers {
		if !live[DomainKey{k.exporter, k.domain}] {
			c.deleteSamplerLocked(k)
		}
	}
	for k := range c.initTimes {
		if !live[k] {
			c.version++
			delete(c.initTimes, k)
		}
	}
}

// TemplateInfo describes one template for the zenplus.flow_templates
// registry: which fields an exporter actually sends is the evidence behind
// every "TCP flags not exported" / "IPv6 present" capability shown to users.
type TemplateInfo struct {
	Exporter   netip.Addr
	Domain     uint32
	Protocol   string
	ID         uint16
	IsOptions  bool
	ScopeCount int               // options templates: the first ScopeCount entries of Fields are scope fields
	Fields     []V9TemplateField // scope fields first for options templates
}

// TemplateCapabilities are the has_* columns of zenplus.flow_templates.
type TemplateCapabilities struct {
	TCPFlags, IPv6, IfIndex32, Direction, AppID, Sampling, NAT, FwEvent bool
}

// Capabilities derives the registry flags from the field list. v9 options
// scope fields use their own type space (1 System, 2 Interface, ...), so only
// IPFIX scope fields are interpreted as information elements.
func (t TemplateInfo) Capabilities() TemplateCapabilities {
	var caps TemplateCapabilities
	for i, f := range t.Fields {
		if f.Enterprise != 0 || (t.IsOptions && i < t.ScopeCount && t.Protocol == ProtocolNetFlow9) {
			continue
		}
		switch f.Type {
		case ieTCPFlags:
			caps.TCPFlags = true
		case ieSrcIPv6, ieDstIPv6:
			caps.IPv6 = true
		case ieInputIf, ieOutputIf:
			if f.Length >= 4 {
				caps.IfIndex32 = true
			}
		case ieIngressPhysIf, ieEgressPhysIf:
			caps.IfIndex32 = true
		case ieDirection:
			caps.Direction = true
		case ieApplicationID:
			caps.AppID = true
		case ieSamplingInterval, ieSamplingAlgorithm, ieSamplerID, ieSamplerMode, ieSamplerRandomInterval, ieSelectorID,
			iePsampInterval, iePsampSpace, ieSamplingSize, ieSamplingPopulation, ieSamplingProbability:
			caps.Sampling = true
		case ieXlateSrcIPv4, ieXlateDstIPv4, ieXlateSrcPort, ieXlateDstPort, ieXlateSrcIPv6, ieXlateDstIPv6:
			caps.NAT = true
		case ieFirewallEvent:
			caps.FwEvent = true
		case ieNSELXlateSrc84, ieNSELXlateDst84, ieNSELXlateSrcPort84, ieNSELXlateDstPort84:
			caps.NAT = caps.NAT || t.Protocol == ProtocolNetFlow9
		case ieNSELFwEvent84:
			caps.FwEvent = caps.FwEvent || t.Protocol == ProtocolNetFlow9
		}
	}
	return caps
}
