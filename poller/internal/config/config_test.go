package config

import (
	"strings"
	"testing"
)

func setPasswords(t *testing.T, pg, ch, rd string) {
	t.Helper()
	t.Setenv(AllowInsecureDefaultsEnv, "")
	t.Setenv("POSTGRES_PASSWORD", pg)
	t.Setenv("CLICKHOUSE_PASSWORD", ch)
	t.Setenv("REDIS_PASSWORD", rd)
}

// An installed appliance has random secrets in /opt/zenplus/.env; the poller
// must start with them and use them unchanged.
func TestLoadAcceptsGeneratedSecrets(t *testing.T) {
	pg, ch, rd := "3f9c0a1b2c3d4e5f60718293a4b5c6d7", "a1b2c3d4e5f60718293a4b5c6d7e8f90", "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
	setPasswords(t, pg, ch, rd)
	cfg, err := Load()
	if err != nil {
		t.Fatalf("generated secrets refused: %v", err)
	}
	if cfg.Postgres.Password != pg || cfg.ClickHouse.Password != ch || cfg.Redis.Password != rd {
		t.Fatal("configured passwords not used")
	}
	if w, err := CheckSecrets(DatastorePasswordEnvs...); err != nil || len(w) != 0 {
		t.Fatalf("unexpected warnings %v / error %v", w, err)
	}
}

func TestLoadRefusesUnsetEmptyAndPlaceholderPasswords(t *testing.T) {
	cases := []struct {
		name       string
		pg, ch, rd string
		want       []string
	}{
		{"all unset", "", "", "", []string{"POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_PASSWORD"}},
		{"placeholder clickhouse", "pg-real-secret-1", PlaceholderSecret, "rd-real-secret-1", []string{"CLICKHOUSE_PASSWORD"}},
		{"placeholder postgres", PlaceholderSecret, "ch-real-secret-1", "rd-real-secret-1", []string{"POSTGRES_PASSWORD"}},
		{"blank redis", "pg-real-secret-1", "ch-real-secret-1", "   ", []string{"REDIS_PASSWORD"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			setPasswords(t, c.pg, c.ch, c.rd)
			cfg, err := Load()
			if err == nil || cfg != nil {
				t.Fatalf("Load() = %v, %v; want refusal", cfg, err)
			}
			msg := err.Error()
			for _, name := range c.want {
				if !strings.Contains(msg, name) {
					t.Fatalf("error %q does not name %s", msg, name)
				}
			}
			if !strings.Contains(msg, AllowInsecureDefaultsEnv) || !strings.Contains(msg, "/opt/zenplus/.env") {
				t.Fatalf("error %q does not say how to fix it", msg)
			}
			// Real secrets configured next to the bad one are never echoed.
			for _, secret := range []string{"pg-real-secret-1", "ch-real-secret-1", "rd-real-secret-1"} {
				if strings.Contains(msg, secret) {
					t.Fatalf("error leaks a configured secret: %q", msg)
				}
			}
		})
	}
}

func TestEscapeHatchAllowsDevelopmentDefaults(t *testing.T) {
	setPasswords(t, "", "", "")
	t.Setenv(AllowInsecureDefaultsEnv, "1")
	cfg, err := Load()
	if err != nil {
		t.Fatalf("escape hatch ignored: %v", err)
	}
	if cfg.Postgres.Password != DevPostgresPassword || cfg.ClickHouse.Password != DevClickHousePassword ||
		cfg.Redis.Password != DevRedisPassword {
		t.Fatal("development fallbacks changed")
	}
	w, err := CheckSecrets(DatastorePasswordEnvs...)
	if err != nil || len(w) != 1 || !strings.Contains(w[0], AllowInsecureDefaultsEnv) {
		t.Fatalf("expected one escape-hatch warning, got %v / %v", w, err)
	}
}

// Same contract as the Python API: only the exact value "1" opens the hatch.
func TestEscapeHatchRequiresExactlyOne(t *testing.T) {
	for _, v := range []string{"true", "yes", "0", " 1", "on"} {
		setPasswords(t, "", "", "")
		t.Setenv(AllowInsecureDefaultsEnv, v)
		if _, err := Load(); err == nil {
			t.Fatalf("%s=%q must not allow insecure defaults", AllowInsecureDefaultsEnv, v)
		}
	}
}

// A published development password set explicitly (for example .env copied
// from .env.example) still starts, with a warning, so an OTA update never
// stops a poller whose datastores really use that value.
func TestExplicitDevPasswordWarnsButStarts(t *testing.T) {
	setPasswords(t, DevPostgresPassword, "ch-real-secret-1", "rd-real-secret-1")
	if _, err := Load(); err != nil {
		t.Fatalf("explicit development password refused: %v", err)
	}
	w, err := CheckSecrets(DatastorePasswordEnvs...)
	if err != nil || len(w) != 1 || !strings.Contains(w[0], "POSTGRES_PASSWORD") || strings.Contains(w[0], DevPostgresPassword) {
		t.Fatalf("expected a rotation warning naming POSTGRES_PASSWORD only, got %v / %v", w, err)
	}
}
