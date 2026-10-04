package netflow

import (
	"net/netip"
	"time"
)

// Template cache persistence. NetFlow v9 and IPFIX over UDP describe their
// records only in templates the exporter repeats periodically (Cisco's
// default template timeout is 30 minutes), and sampled exporters announce
// their rate in options data on the same schedule. A collector that forgets
// them on every restart (an upgrade, a configuration change) drops every
// data set until each exporter re-announces its templates, and stores sampled
// traffic unscaled until the sampler arrives again. The collector therefore
// saves a CacheSnapshot and restores it at start, as pmacct does with
// nfacctd_templates_file. Entries keep the time they were last announced, so
// the TTL still applies across the restart, and a template the exporter
// changed in the meantime is replaced by its next announcement exactly as in
// a collector that never stopped.

// CacheSnapshot is the persistable content of a V9TemplateCache.
type CacheSnapshot struct {
	Templates []TemplateSnapshot `json:"templates"`
	Options   []OptionsSnapshot  `json:"options"`
	Samplers  []SamplerSnapshot  `json:"samplers"`
	InitTimes []InitTimeSnapshot `json:"init_times"`
}

type TemplateSnapshot struct {
	Exporter    netip.Addr        `json:"exporter"`
	Domain      uint32            `json:"domain"`
	ID          uint16            `json:"id"`
	Fields      []V9TemplateField `json:"fields"`
	Length      int               `json:"length"`
	LearnedAt   time.Time         `json:"learned_at"`
	AnnouncedAt time.Time         `json:"announced_at"`
}

type OptionsSnapshot struct {
	Exporter    netip.Addr        `json:"exporter"`
	Domain      uint32            `json:"domain"`
	ID          uint16            `json:"id"`
	ScopeFields []V9TemplateField `json:"scope_fields"`
	Fields      []V9TemplateField `json:"fields"`
	Length      int               `json:"length"`
	LearnedAt   time.Time         `json:"learned_at"`
	AnnouncedAt time.Time         `json:"announced_at"`
}

// SamplerSnapshot is one learned sampler. Explicit marks the domain default
// (announced without a sampler ID), PerID a sampler announced with its ID.
// Snapshots written before X-08 have neither flag on per-ID samplers, and
// may hold a non-explicit ID-0 "default" copied from the last per-ID sampler;
// that copy is not restored (see Restore).
type SamplerSnapshot struct {
	Exporter netip.Addr `json:"exporter"`
	Domain   uint32     `json:"domain"`
	ID       uint64     `json:"id"`
	Interval uint32     `json:"interval"`
	Explicit bool       `json:"explicit"`
	PerID    bool       `json:"per_id,omitempty"`
}

type InitTimeSnapshot struct {
	Exporter netip.Addr `json:"exporter"`
	Domain   uint32     `json:"domain"`
	MS       uint64     `json:"ms"`
}

// Version reports a counter that changes whenever the cache content changes
// (not when a known template is merely re-announced).
func (c *V9TemplateCache) Version() uint64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.version
}

// Snapshot returns the live (unexpired) content of the cache.
func (c *V9TemplateCache) Snapshot() CacheSnapshot {
	now := c.now()
	c.mu.RLock()
	defer c.mu.RUnlock()
	s := CacheSnapshot{
		Templates: make([]TemplateSnapshot, 0, len(c.templates)),
		Options:   make([]OptionsSnapshot, 0, len(c.optionsTemplates)),
		Samplers:  make([]SamplerSnapshot, 0, len(c.samplers)),
		InitTimes: make([]InitTimeSnapshot, 0, len(c.initTimes)),
	}
	for k, e := range c.templates {
		if now.Sub(e.learnedAt) <= c.ttl {
			s.Templates = append(s.Templates, TemplateSnapshot{k.exporter, k.domain, k.id, e.tpl.Fields, e.tpl.Length, e.learnedAt, e.announcedAt})
		}
	}
	for k, e := range c.optionsTemplates {
		if now.Sub(e.learnedAt) <= c.ttl {
			s.Options = append(s.Options, OptionsSnapshot{k.exporter, k.domain, k.id, e.tpl.ScopeFields, e.tpl.Fields, e.tpl.Length, e.learnedAt, e.announcedAt})
		}
	}
	for k, e := range c.samplers {
		s.Samplers = append(s.Samplers, SamplerSnapshot{k.exporter, k.domain, k.id, e.interval, !k.byID, k.byID})
	}
	for k, ms := range c.initTimes {
		s.InitTimes = append(s.InitTimes, InitTimeSnapshot{k.Exporter, k.Domain, ms})
	}
	return s
}

// Restore loads a snapshot into the cache, as if each template had been
// announced at its saved time: entries past the TTL, undecodable templates and
// anything beyond the cache bounds are skipped, and nothing already in the
// cache is overwritten (a fresher announcement wins). It returns how many
// templates (data + options) and samplers were restored.
func (c *V9TemplateCache) Restore(s CacheSnapshot) (templates, samplers int) {
	now := c.now()
	c.mu.Lock()
	defer c.mu.Unlock()
	live := make(map[DomainKey]bool)
	for _, t := range s.Templates {
		key := templateKey{CanonicalAddr(t.Exporter), t.Domain, t.ID}
		if !key.exporter.IsValid() || t.ID < 256 || !decodable(t.Fields) || now.Sub(t.LearnedAt) > c.ttl {
			continue
		}
		if _, ok := c.templates[key]; ok {
			continue
		}
		if _, ok := c.optionsTemplates[key]; ok || !c.roomLocked(key.exporter) {
			continue
		}
		tpl := V9Template{ID: t.ID, Fields: t.Fields, Length: t.Length}
		tpl.dec = compileLayout(nil, tpl.Fields)
		c.templates[key] = &cachedTemplate{tpl: tpl, learnedAt: t.LearnedAt, announcedAt: t.AnnouncedAt}
		c.trackLocked(key, false)
		live[DomainKey{key.exporter, key.domain}] = true
		templates++
	}
	for _, t := range s.Options {
		key := templateKey{CanonicalAddr(t.Exporter), t.Domain, t.ID}
		if !key.exporter.IsValid() || t.ID < 256 || !(decodable(t.ScopeFields) || decodable(t.Fields)) || now.Sub(t.LearnedAt) > c.ttl {
			continue
		}
		if _, ok := c.optionsTemplates[key]; ok {
			continue
		}
		if _, ok := c.templates[key]; ok || !c.roomLocked(key.exporter) {
			continue
		}
		tpl := V9OptionsTemplate{ID: t.ID, ScopeFields: t.ScopeFields, Fields: t.Fields, Length: t.Length}
		tpl.dec = compileLayout(tpl.ScopeFields, tpl.Fields)
		c.optionsTemplates[key] = &cachedOptions{tpl: tpl, learnedAt: t.LearnedAt, announcedAt: t.AnnouncedAt}
		c.trackLocked(key, true)
		live[DomainKey{key.exporter, key.domain}] = true
		templates++
	}
	// Samplers and init times belong to a domain with live templates (Prune
	// drops them otherwise), and are restored only where none was learned yet.
	for _, sp := range s.Samplers {
		var k samplerKey
		switch exp := CanonicalAddr(sp.Exporter); {
		case sp.Explicit:
			k = samplerKey{exp, sp.Domain, 0, false}
		case sp.PerID || sp.ID != 0:
			k = samplerKey{exp, sp.Domain, sp.ID, true}
		default:
			// A pre-X-08 domain default copied from whichever per-ID sampler
			// was announced last: not a rate the exporter gave the domain.
			continue
		}
		if sp.Interval == 0 || !live[DomainKey{k.exporter, k.domain}] {
			continue
		}
		if _, ok := c.samplers[k]; ok {
			continue
		}
		before := len(c.samplers)
		c.putSamplerLocked(k, sp.Interval)
		if len(c.samplers) > before {
			samplers++
		}
	}
	for _, it := range s.InitTimes {
		k := DomainKey{CanonicalAddr(it.Exporter), it.Domain}
		if _, ok := c.initTimes[k]; ok || it.MS == 0 || !live[k] {
			continue
		}
		c.initTimes[k] = it.MS
		c.version++
	}
	return templates, samplers
}
