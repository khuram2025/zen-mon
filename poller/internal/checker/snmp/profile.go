package snmp

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"

	"github.com/google/uuid"
)

// Profile is the poller-side representation of a row in
// `device_profiles`. JSON files under data/profiles/*.json are
// loaded at startup and upserted into the DB so operators can see
// them in the UI (phase 4).
type Profile struct {
	ID          uuid.UUID    `json:"-"` // filled by the DB upsert
	Name        string       `json:"name"`
	Vendor      string       `json:"vendor,omitempty"`
	Version     int          `json:"version"`
	Builtin     bool         `json:"builtin"`
	Description string       `json:"description,omitempty"`
	Match       MatchRules   `json:"match_rules"`
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
	// sysObjectID, this profile matches. Longest prefix wins when
	// multiple profiles match.
	SysObjectIDPrefixes []string `json:"sys_object_id_prefixes,omitempty"`

	// SysDescrRegex — single regex (Go/RE2). If set, tried as a
	// fallback when no sysObjectID prefix matched, OR as a tie-breaker.
	SysDescrRegex string `json:"sys_descr_regex,omitempty"`

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
	profile    *Profile
	descrRE    *regexp.Regexp
	vendorRE   *regexp.Regexp
	modelRE    *regexp.Regexp
	versionRE  *regexp.Regexp
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
		cp, err := compile(p)
		if err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", p.Name, err))
			continue
		}
		c.profiles = append(c.profiles, cp)
	}
	c.mu.Unlock()

	return loaded, errs
}

// Match returns the best profile for the given sysObjectID + sysDescr.
// Returns nil if nothing matches. Priority:
//
//  1. Longest sysObjectIDPrefix that is a prefix of the device's sysObjectID
//  2. Fallback: first profile whose sysDescrRegex matches
func (c *Classifier) Match(sysObjectID, sysDescr string) *Profile {
	c.mu.RLock()
	defer c.mu.RUnlock()

	sysObjectID = strings.TrimPrefix(sysObjectID, ".")

	type hit struct {
		cp       compiledProfile
		prefixLen int
	}
	var hits []hit
	for _, cp := range c.profiles {
		for _, pref := range cp.profile.Match.SysObjectIDPrefixes {
			pref = strings.TrimPrefix(pref, ".")
			if sysObjectID != "" && strings.HasPrefix(sysObjectID, pref) {
				hits = append(hits, hit{cp, len(pref)})
				break
			}
		}
	}
	if len(hits) > 0 {
		sort.SliceStable(hits, func(i, j int) bool {
			return hits[i].prefixLen > hits[j].prefixLen
		})
		return hits[0].cp.profile
	}

	// sysDescr fallback.
	for _, cp := range c.profiles {
		if cp.descrRE != nil && cp.descrRE.MatchString(sysDescr) {
			return cp.profile
		}
	}
	return nil
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

func compile(p *Profile) (compiledProfile, error) {
	cp := compiledProfile{profile: p}
	var err error
	if p.Match.SysDescrRegex != "" {
		if cp.descrRE, err = regexp.Compile(p.Match.SysDescrRegex); err != nil {
			return cp, fmt.Errorf("sys_descr_regex: %w", err)
		}
	}
	if p.Match.ExtractVendor != "" {
		if cp.vendorRE, err = regexp.Compile(p.Match.ExtractVendor); err != nil {
			return cp, fmt.Errorf("extract_vendor: %w", err)
		}
	}
	if p.Match.ExtractModel != "" {
		if cp.modelRE, err = regexp.Compile(p.Match.ExtractModel); err != nil {
			return cp, fmt.Errorf("extract_model: %w", err)
		}
	}
	if p.Match.ExtractOSVersion != "" {
		if cp.versionRE, err = regexp.Compile(p.Match.ExtractOSVersion); err != nil {
			return cp, fmt.Errorf("extract_os_version: %w", err)
		}
	}
	return cp, nil
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
