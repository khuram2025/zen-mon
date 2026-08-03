package agent

import (
	"encoding/json"
	"testing"

	"zenplus-agent/internal/config"
)

func TestPrintableConfigOmitsEnrollmentToken(t *testing.T) {
	cfg := config.Default()
	cfg.EnrollmentToken = "zpa_enr_do_not_print_this"

	printed := printableConfig(cfg)
	if printed.EnrollmentToken != "" {
		t.Fatal("printable config retained the enrollment token")
	}
	if cfg.EnrollmentToken == "" {
		t.Fatal("printableConfig mutated its input")
	}

	b, err := json.Marshal(printed)
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatal(err)
	}
	if _, exists := raw["enrollment_token"]; exists {
		t.Fatal("printable config JSON included enrollment_token")
	}
}
