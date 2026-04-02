package config

import (
	"fmt"
	"os"
	"time"
)

type Config struct {
	Poller     PollerConfig
	Postgres   PostgresConfig
	ClickHouse ClickHouseConfig
	Redis      RedisConfig
	Health     HealthConfig
}

type PollerConfig struct {
	ID                 string
	PingTimeout        time.Duration
	PingCount          int
	PingInterval       time.Duration
	BatchSize          int
	TickRate           time.Duration
	DownThreshold      int
	DegradedRTTMs      float64
	DegradedLossPct    float64
	DeviceSyncInterval time.Duration
	Privileged         bool
}

type PostgresConfig struct {
	Host     string
	Port     int
	Database string
	User     string
	Password string
}

type ClickHouseConfig struct {
	Host       string
	Port       int
	Database   string
	User       string
	Password   string
	BatchSize  int
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
	return fmt.Sprintf("postgres://%s:%s@%s:%d/%s?sslmode=disable",
		p.User, p.Password, p.Host, p.Port, p.Database)
}

func Load() (*Config, error) {
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
			Host:     getEnv("POSTGRES_HOST", "localhost"),
			Port:     getEnvInt("POSTGRES_PORT", 5432),
			Database: getEnv("POSTGRES_DB", "zenplus"),
			User:     getEnv("POSTGRES_USER", "zenplus"),
			Password: getEnv("POSTGRES_PASSWORD", "zenplus_dev"),
		},
		ClickHouse: ClickHouseConfig{
			Host:          getEnv("CLICKHOUSE_HOST", "localhost"),
			Port:          getEnvInt("CLICKHOUSE_PORT", 9000),
			Database:      getEnv("CLICKHOUSE_DB", "zenplus"),
			User:          getEnv("CLICKHOUSE_USER", "default"),
			Password:      getEnv("CLICKHOUSE_PASSWORD", "clickhouse_dev"),
			BatchSize:     1000,
			FlushInterval: 5 * time.Second,
		},
		Redis: RedisConfig{
			Host:     getEnv("REDIS_HOST", "localhost"),
			Port:     getEnvInt("REDIS_PORT", 6379),
			Password: getEnv("REDIS_PASSWORD", "redis_dev"),
			DB:       0,
		},
		Health: HealthConfig{
			Port: getEnvInt("HEALTH_PORT", 8081),
		},
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
