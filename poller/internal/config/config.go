package config

import (
	"fmt"
	"net/url"
	"os"
	"strings"
	"time"
)

// AllowInsecureDefaultsEnv is the development escape hatch shared with the
// Python API (server/app/core/secret_policy.py): only the exact value "1"
// lets a service start with placeholder datastore passwords.
const AllowInsecureDefaultsEnv = "ZENPLUS_ALLOW_INSECURE_DEFAULTS"

// PlaceholderSecret is the published placeholder value refused everywhere.
const PlaceholderSecret = "changeme"

// Built-in development fallbacks, used only when the variable is unset or
// empty. Installed appliances always set real values in /opt/zenplus/.env
// (install.sh, bin/first-boot-init.sh, appliance/vmware/first-boot.py), which
// every service reads through EnvironmentFile=, so these never apply there.
const (
	DevPostgresPassword   = "zenplus_dev"
	DevClickHousePassword = "clickhouse_dev"
	DevRedisPassword      = "redis_dev"
)

// DatastorePasswordEnvs are the poller's datastore password settings.
var DatastorePasswordEnvs = []string{"POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_PASSWORD"}

var publishedDevPasswords = map[string]bool{
	DevPostgresPassword:   true,
	DevClickHousePassword: true,
	DevRedisPassword:      true,
}

// InsecureDefaultsAllowed reports whether the development escape hatch is on.
func InsecureDefaultsAllowed() bool {
	return os.Getenv(AllowInsecureDefaultsEnv) == "1"
}

// CheckSecrets refuses datastore passwords that would fall back to a built-in
// development default (variable unset, empty or blank) or that are the
// published placeholder "changeme", unless ZENPLUS_ALLOW_INSECURE_DEFAULTS=1.
//
// A variable explicitly set to one of the published development passwords
// (zenplus_dev, clickhouse_dev, redis_dev) only produces a warning: such a
// value works against a datastore that really uses it, and refusing it here
// without an OTA preflight check would stop a running appliance on update.
func CheckSecrets(envs ...string) (warnings []string, err error) {
	var bad, dev []string
	for _, name := range envs {
		v := os.Getenv(name)
		switch {
		case strings.TrimSpace(v) == "":
			bad = append(bad, name+" (unset or empty)")
		case v == PlaceholderSecret:
			bad = append(bad, name+" (placeholder \""+PlaceholderSecret+"\")")
		case publishedDevPasswords[v]:
			dev = append(dev, name)
		}
	}
	if len(bad) > 0 {
		if !InsecureDefaultsAllowed() {
			return nil, fmt.Errorf(
				"refusing to start with insecure datastore credentials: %s; "+
					"set the real values in /opt/zenplus/.env (development only: %s=1)",
				strings.Join(bad, ", "), AllowInsecureDefaultsEnv)
		}
		warnings = append(warnings, fmt.Sprintf(
			"running with insecure datastore credentials (%s) because %s=1",
			strings.Join(bad, ", "), AllowInsecureDefaultsEnv))
	}
	if len(dev) > 0 {
		warnings = append(warnings, fmt.Sprintf(
			"%s set to a published development password; rotate it",
			strings.Join(dev, ", ")))
	}
	return warnings, nil
}

type Config struct {
	Poller     PollerConfig
	Postgres   PostgresConfig
	ClickHouse ClickHouseConfig
	Redis      RedisConfig
	Health     HealthConfig
}

type PollerConfig struct {
	ID           string
	PingTimeout  time.Duration
	PingCount    int
	PingInterval time.Duration
	BatchSize    int
	TickRate     time.Duration
	// DownThreshold, DegradedRTTMs and DegradedLossPct are the fallbacks of
	// the device status policy (down_confirm_checks, degraded_rtt_ms,
	// degraded_loss_pct) when the admin settings (system_settings key
	// 'monitoring', reloaded every DeviceSyncInterval) do not set them. The
	// other policy keys use the built-in defaults of devicestatus.
	// DownThreshold is also the retry count of service checks that set none.
	DownThreshold      int
	DegradedRTTMs      float64
	DegradedLossPct    float64
	DeviceSyncInterval time.Duration
	Privileged         bool
}

type PostgresConfig struct {
	Host        string
	Port        int
	Database    string
	User        string
	Password    string
	SSLMode     string
	SSLRootCert string
}

type ClickHouseConfig struct {
	Host          string
	Port          int
	Database      string
	User          string
	Password      string
	BatchSize     int
	FlushInterval time.Duration
}

type RedisConfig struct {
	Host     string
	Port     int
	Password string
	DB       int
}

type HealthConfig struct {
	Port int
}

func (p PostgresConfig) DSN() string {
	mode := p.SSLMode
	if mode == "" {
		mode = "disable"
	}
	u := url.URL{Scheme: "postgres", User: url.UserPassword(p.User, p.Password), Host: fmt.Sprintf("%s:%d", p.Host, p.Port), Path: "/" + p.Database}
	q := url.Values{"sslmode": {mode}}
	if p.SSLRootCert != "" {
		q.Set("sslrootcert", p.SSLRootCert)
	}
	u.RawQuery = q.Encode()
	return u.String()
}

// Load reads the poller configuration from the environment. It refuses
// (returns an error) when a datastore password is unset/empty or the
// placeholder "changeme", unless ZENPLUS_ALLOW_INSECURE_DEFAULTS=1.
func Load() (*Config, error) {
	warnings, err := CheckSecrets(DatastorePasswordEnvs...)
	if err != nil {
		return nil, err
	}
	for _, w := range warnings {
		fmt.Fprintln(os.Stderr, "WARN: "+w)
	}
	cfg := &Config{
		Poller: PollerConfig{
			ID:                 getEnv("POLLER_ID", "poller-01"),
			PingTimeout:        3 * time.Second,
			PingCount:          3,
			PingInterval:       500 * time.Millisecond,
			BatchSize:          500,
			TickRate:           50 * time.Millisecond,
			DownThreshold:      3,
			DegradedRTTMs:      100.0,
			DegradedLossPct:    10.0,
			DeviceSyncInterval: 60 * time.Second,
			Privileged:         getEnv("POLLER_PRIVILEGED", "false") == "true",
		},
		Postgres: PostgresConfig{
			Host:        getEnv("POSTGRES_HOST", "localhost"),
			Port:        getEnvInt("POSTGRES_PORT", 5432),
			Database:    getEnv("POSTGRES_DB", "zenplus"),
			User:        getEnv("POSTGRES_USER", "zenplus"),
			Password:    getEnv("POSTGRES_PASSWORD", DevPostgresPassword),
			SSLMode:     getEnv("POSTGRES_SSLMODE", "disable"),
			SSLRootCert: getEnv("POSTGRES_SSLROOTCERT", ""),
		},
		ClickHouse: ClickHouseConfig{
			Host:          getEnv("CLICKHOUSE_HOST", "localhost"),
			Port:          getEnvInt("CLICKHOUSE_PORT", 9000),
			Database:      getEnv("CLICKHOUSE_DB", "zenplus"),
			User:          getEnv("CLICKHOUSE_USER", "default"),
			Password:      getEnv("CLICKHOUSE_PASSWORD", DevClickHousePassword),
			BatchSize:     1000,
			FlushInterval: 5 * time.Second,
		},
		Redis: RedisConfig{
			Host:     getEnv("REDIS_HOST", "localhost"),
			Port:     getEnvInt("REDIS_PORT", 6379),
			Password: getEnv("REDIS_PASSWORD", DevRedisPassword),
			DB:       0,
		},
		Health: HealthConfig{
			Port: getEnvInt("HEALTH_PORT", 8081),
		},
	}

	if os.Getenv("ZENPLUS_CONTROLLER_GRANT") != "" && (cfg.Postgres.SSLMode != "verify-full" || cfg.Postgres.SSLRootCert == "") {
		return nil, fmt.Errorf("HA poller requires POSTGRES_SSLMODE=verify-full and POSTGRES_SSLROOTCERT")
	}
	return cfg, nil
}

func getEnv(key, defaultVal string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return defaultVal
}

func getEnvInt(key string, defaultVal int) int {
	val := os.Getenv(key)
	if val == "" {
		return defaultVal
	}
	var result int
	_, err := fmt.Sscanf(val, "%d", &result)
	if err != nil {
		return defaultVal
	}
	return result
}
