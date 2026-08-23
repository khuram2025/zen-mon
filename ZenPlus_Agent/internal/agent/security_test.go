package agent

import (
	"encoding/json"
	"testing"

	"zenplus-agent/internal/config"
)

func TestPrintableConfigHasNoEnrollmentInputs(t *testing.T) {
	cfg := config.Default()

	printed := printableConfig(cfg)
	b, err := json.Marshal(printed)
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{"enrollment_token", "site_id"} {
		if _, exists := raw[forbidden]; exists {
			t.Fatalf("printable config JSON included %s", forbidden)
		}
	}
}
