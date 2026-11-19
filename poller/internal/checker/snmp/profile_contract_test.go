package snmp

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// templateMatchContract is scripts/dev/template-match-contract/vectors.json:
// the classification cases the Go classifier and the server's classify.py
// must both decide identically.
type templateMatchContract struct {
	Description string `json:"description"`
	Templates   []struct {
		ID            string     `json:"id"`
		Name          string     `json:"name"`
		MatchPriority int        `json:"match_priority"`
		MatchRules    MatchRules `json:"match_rules"`
	} `json:"templates"`
	Cases []struct {
		Name        string  `json:"name"`
		SysObjectID string  `json:"sys_object_id"`
		SysDescr    string  `json:"sys_descr"`
		Expect      *string `json:"expect"`
	} `json:"cases"`
}

func TestTemplateMatchContract(t *testing.T) {
	path := filepath.Join("..", "..", "..", "..", "scripts", "dev", "template-match-contract", "vectors.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read contract: %v", err)
	}
	var contract templateMatchContract
	if err := json.Unmarshal(raw, &contract); err != nil {
		t.Fatalf("parse contract: %v", err)
	}
	if len(contract.Templates) == 0 || len(contract.Cases) == 0 {
		t.Fatal("contract has no templates or cases")
	}

	idOf := map[*Profile]string{}
	profiles := make([]*Profile, 0, len(contract.Templates))
	for _, tpl := range contract.Templates {
		p := &Profile{Name: tpl.Name, MatchPriority: tpl.MatchPriority, Match: tpl.MatchRules}
		idOf[p] = tpl.ID
		profiles = append(profiles, p)
	}

	c := NewClassifier()
	// The broken regex is reported, but its template is kept (and never
	// matches); every other template compiles.
	errs := c.SetProfiles(profiles)
	if len(errs) != 1 {
		t.Fatalf("SetProfiles errors = %v, want exactly the broken regex", errs)
	}
	if len(c.profiles) != len(profiles) {
		t.Fatalf("classifier kept %d of %d templates", len(c.profiles), len(profiles))
	}

	// Load order must not decide anything: run the cases forwards and with
	// the templates reversed.
	reversed := make([]*Profile, len(profiles))
	for i, p := range profiles {
		reversed[len(profiles)-1-i] = p
	}
	rc := NewClassifier()
	rc.SetProfiles(reversed)

	for _, tc := range contract.Cases {
		want := "<none>"
		if tc.Expect != nil {
			want = *tc.Expect
		}
		for label, cl := range map[string]*Classifier{"forward": c, "reversed": rc} {
			got := "<none>"
			if p := cl.Match(tc.SysObjectID, tc.SysDescr); p != nil {
				got = idOf[p]
			}
			if got != want {
				t.Errorf("%s (%s): Match(%q, %q) = %s, want %s",
					tc.Name, label, tc.SysObjectID, tc.SysDescr, got, want)
			}
		}
	}
}
