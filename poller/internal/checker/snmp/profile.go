package snmp

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"

	"github.com/google/uuid"
)

// Profile is the poller-side representation of a row in
// `device_profiles`. JSON files under data/profiles/*.json are
// loaded at startup and upserted into the DB so operators can see
// them in the UI (phase 4).
type Profile struct {
	ID          uuid.UUID  `json:"-"` // filled by the DB upsert
	Name        string     `json:"name"`
	Vendor      string     `json:"vendor,omitempty"`
	Version     int        `json:"version"`
	Builtin     bool       `json:"builtin"`
	Description string     `json:"description,omitempty"`
	Match       MatchRules `json:"match_rules"`
	// MatchPriority ranks templates that match the same device (higher
	// wins). The DB default is 100; 0 is a valid value and is kept as
	// stored (a bootstrap JSON file without the field yields 0).
	MatchPriority int `json:"match_priority"`
	// DeviceType is the device_types key a device on this template is
	// (devices.device_type is set from it only while the device is 'other').
	DeviceType string `json:"device_type,omitempty"`
	// OidGroups is reserved for Phase 3 (vendor-specific metric
	// collection driven by the profile, not hard-coded Go collectors).
	// Kept as raw JSON so profiles can evolve without schema churn.
	OidGroups json.RawMessage `json:"oid_groups,omitempty"`
}

// MatchRules describes how a device is matched to this profile.
// Any rule may be empty; a profile with no match rules matches
// nothing (except as an explicit operator override).
type MatchRules struct {
	// SysObjectIDPrefixes — if any prefix is a prefix of the device's
	// sysObjectID on a dot boundary, this profile matches. Among equal
	// match priorities the longest prefix wins.
	SysObjectIDPrefixes []string `json:"sys_object_id_prefixes,omitempty"`

	// SysDescrRegex — single regex (Go/RE2). Tried as a fallback when
	// no sysObjectID prefix matched (see Classifier.Match).
	SysDescrRegex string `json:"sys_descr_regex,omitempty"`

	// RequireSysDescr — a sysObjectID prefix hit counts only when
	// SysDescrRegex also matches (a model-specific template under a
	// vendor-wide prefix).
	RequireSysDescr bool `json:"require_sys_descr,omitempty"`

	// Vendor / Model / OSVersion extraction patterns — regexes with
	// named groups (?P<vendor>…) or positional groups 1/2/3. Applied
	// to sysDescr on a successful match.
	ExtractVendor    string `json:"extract_vendor,omitempty"`
	ExtractModel     string `json:"extract_model,omitempty"`
	ExtractOSVersion string `json:"extract_os_version,omitempty"`

	// Static fallbacks used when the extract regexes don't fire.
	DefaultVendor string `json:"default_vendor,omitempty"`
	DefaultModel  string `json:"default_model,omitempty"`
}

// compiledProfile caches regex compilations so we don't re-parse on
// every classification call.
type compiledProfile struct {
	profile   *Profile
	descrRE   *regexp.Regexp
	vendorRE  *regexp.Regexp
	modelRE   *regexp.Regexp
	versionRE *regexp.Regexp
}

// Classifier loads profile definitions from disk and assigns them
// to polled devices. It is safe for concurrent use.
type Classifier struct {
	mu       sync.RWMutex
	profiles []compiledProfile
}

func NewClassifier() *Classifier {
	return &Classifier{}
}

// LoadFromDir reads every *.json file under the given directory and
// compiles it into the classifier. Files that fail to parse are
// skipped with a returned error list (the rest still load).
func (c *Classifier) LoadFromDir(dir string) ([]*Profile, []error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, []error{fmt.Errorf("read profile dir %s: %w", dir, err)}
	}
	var loaded []*Profile
	var errs []error
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		path := filepath.Join(dir, e.Name())
		raw, err := os.ReadFile(path)
		if err != nil {
			errs = append(errs, fmt.Errorf("read %s: %w", e.Name(), err))
			continue
		}
		var p Profile
		if err := json.Unmarshal(raw, &p); err != nil {
			errs = append(errs, fmt.Errorf("parse %s: %w", e.Name(), err))
			continue
		}
		if p.Name == "" {
			errs = append(errs, fmt.Errorf("%s: missing name", e.Name()))
			continue
		}
		if p.Version == 0 {
			p.Version = 1
		}
		loaded = append(loaded, &p)
	}

	c.mu.Lock()
	c.profiles = c.profiles[:0]
	for _, p := range loaded {
		cp, cerrs := compile(p)
		for _, err := range cerrs {
			errs = append(errs, fmt.Errorf("%s: %w", p.Name, err))
		}
		c.profiles = append(c.profiles, cp)
	}
	c.mu.Unlock()

	return loaded, errs
}

// SetProfiles replaces the classifier's profile set from an
// already-loaded slice (the DB-driven path — device_profiles is the
// source of truth; JSON files under data/profiles are only an optional
// bootstrap). A regex that fails to compile is reported and disabled; the
// profile itself is kept.
func (c *Classifier) SetProfiles(profiles []*Profile) []error {
	var errs []error
	compiled := make([]compiledProfile, 0, len(profiles))
	for _, p := range profiles {
		cp, cerrs := compile(p)
		for _, err := range cerrs {
			errs = append(errs, fmt.Errorf("%s: %w", p.Name, err))
		}
		compiled = append(compiled, cp)
	}
	c.mu.Lock()
	c.profiles = compiled
	c.mu.Unlock()
	return errs
}

// NormalizeOID puts a sysObjectID into bare numeric dotted form so the
// net-snmp symbolic "iso.3.6..." and the leading-dot ".1.3.6..." forms
// compare equal to "1.3.6...".
func NormalizeOID(oid string) string {
	oid = strings.TrimSpace(oid)
	if strings.HasPrefix(oid, "iso.") || oid == "iso" {
		oid = "1" + strings.TrimPrefix(oid, "iso")
	}
	return strings.TrimPrefix(oid, ".")
}

// Match returns the best profile for the given sysObjectID + sysDescr,
// or nil if nothing matches. The rules are the shared classification
// contract (scripts/dev/template-match-contract/vectors.json, also
// implemented by the server's classify.py):
//
//   - OIDs are normalised (NormalizeOID) and a prefix matches only on a dot
//     boundary: the sysObjectID equals the prefix or continues with ".".
//   - A template matches by prefix (strength 2) or, when none of its
//     prefixes matched, by sys_descr_regex (strength 1). With
//     require_sys_descr a prefix hit counts only when the regex also
//     matches; the descr fallback needs the regex anyway, so such a template
//     does not match at all without it.
//   - Candidates rank by match_priority (higher first), then strength, then
//     the longest matching prefix, then name in byte order.
//   - An invalid regex never matches (compile leaves it nil).
func (c *Classifier) Match(sysObjectID, sysDescr string) *Profile {
	c.mu.RLock()
	defer c.mu.RUnlock()

	sysObjectID = NormalizeOID(sysObjectID)

	var best *Profile
	var bestStrength, bestPrefixLen int
	for i := range c.profiles {
		cp := &c.profiles[i]
		strength, prefixLen := cp.matchStrength(sysObjectID, sysDescr)
		if strength == 0 {
			continue
		}
		if best == nil || outranks(cp.profile, strength, prefixLen, best, bestStrength, bestPrefixLen) {
			best, bestStrength, bestPrefixLen = cp.profile, strength, prefixLen
		}
	}
	return best
}

// matchStrength reports how a template matches: 2 by sysObjectID prefix
// (with the longest matching prefix length), 1 by sysDescr regex, 0 not at
// all. sysObjectID must already be normalised.
func (cp *compiledProfile) matchStrength(sysObjectID, sysDescr string) (strength, prefixLen int) {
	descrOK := cp.descrRE != nil && cp.descrRE.MatchString(sysDescr)
	longest := -1
	if sysObjectID != "" {
		for _, pref := range cp.profile.Match.SysObjectIDPrefixes {
			pref = NormalizeOID(pref)
			if pref == "" || len(pref) <= longest {
				continue
			}
			if sysObjectID == pref || strings.HasPrefix(sysObjectID, pref+".") {
				longest = len(pref)
			}
		}
	}
	if longest >= 0 && (!cp.profile.Match.RequireSysDescr || descrOK) {
		return 2, longest
	}
	if descrOK {
		return 1, 0
	}
	return 0, 0
}

// outranks reports whether candidate a beats candidate b.
func outranks(a *Profile, aStrength, aPrefixLen int, b *Profile, bStrength, bPrefixLen int) bool {
	if a.MatchPriority != b.MatchPriority {
		return a.MatchPriority > b.MatchPriority
	}
	if aStrength != bStrength {
		return aStrength > bStrength
	}
	if aPrefixLen != bPrefixLen {
		return aPrefixLen > bPrefixLen
	}
	return a.Name < b.Name
}

// Extract runs the profile's vendor/model/os_version regexes against
// sysDescr and returns the resolved fields. Empty fields fall back to
// the profile's static defaults.
func (c *Classifier) Extract(p *Profile, sysDescr string) (vendor, model, osVersion string) {
	c.mu.RLock()
	defer c.mu.RUnlock()

	var cp *compiledProfile
	for i := range c.profiles {
		if c.profiles[i].profile == p {
			cp = &c.profiles[i]
			break
		}
	}
	if cp == nil {
		return p.Match.DefaultVendor, p.Match.DefaultModel, ""
	}
	vendor = firstGroup(cp.vendorRE, sysDescr)
	if vendor == "" {
		vendor = p.Match.DefaultVendor
	}
	model = firstGroup(cp.modelRE, sysDescr)
	if model == "" {
		model = p.Match.DefaultModel
	}
	osVersion = firstGroup(cp.versionRE, sysDescr)
	return
}

// compile caches a profile's regexes. A pattern that does not compile is
// reported and left nil (it then never matches or extracts) instead of
// dropping the whole profile: devices already on the template keep their
// classification, and its other rules keep working.
func compile(p *Profile) (compiledProfile, []error) {
	cp := compiledProfile{profile: p}
	var errs []error
	re := func(field, pattern string) *regexp.Regexp {
		if pattern == "" {
			return nil
		}
		r, err := regexp.Compile(pattern)
		if err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", field, err))
			return nil
		}
		return r
	}
	cp.descrRE = re("sys_descr_regex", p.Match.SysDescrRegex)
	cp.vendorRE = re("extract_vendor", p.Match.ExtractVendor)
	cp.modelRE = re("extract_model", p.Match.ExtractModel)
	cp.versionRE = re("extract_os_version", p.Match.ExtractOSVersion)
	return cp, errs
}

// firstGroup returns the first non-empty capture group from the
// regex. Named group "v" / "vendor" / "value" is preferred if present.
func firstGroup(re *regexp.Regexp, s string) string {
	if re == nil || s == "" {
		return ""
	}
	m := re.FindStringSubmatch(s)
	if m == nil {
		return ""
	}
	// Prefer a named group if any are defined.
	for i, name := range re.SubexpNames() {
		if name == "" || i == 0 {
			continue
		}
		if i < len(m) && m[i] != "" {
			return strings.TrimSpace(m[i])
		}
	}
	// Fall back to group 1.
	if len(m) > 1 {
		return strings.TrimSpace(m[1])
	}
	return ""
}
