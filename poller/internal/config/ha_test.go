package config

import (
	"net/url"
	"testing"
)

func TestDatabaseURLQuotesSecretsAndVerifiesTLS(t *testing.T) {
	p := PostgresConfig{Host: "192.0.2.1", Port: 5432, User: "user@cell", Password: "a:b/@?%x", Database: "zenplus", SSLMode: "verify-full", SSLRootCert: "/etc/cell/ca.crt"}
	u, err := url.Parse(p.DSN())
	if err != nil {
		t.Fatal(err)
	}
	password, _ := u.User.Password()
	if password != p.Password || u.User.Username() != p.User || u.Query().Get("sslmode") != "verify-full" || u.Query().Get("sslrootcert") != p.SSLRootCert {
		t.Fatal("DSN lost credential or TLS configuration")
	}
}

func TestHARequiresVerifiedDatabaseTLS(t *testing.T) {
	setPasswords(t, "private-pg", "private-ch", "private-redis")
	t.Setenv("ZENPLUS_CONTROLLER_GRANT", "/run/zenplus-controller/grant.json")
	t.Setenv("POSTGRES_SSLMODE", "disable")
	if _, err := Load(); err == nil {
		t.Fatal("HA accepted plaintext database")
	}
	t.Setenv("POSTGRES_SSLMODE", "verify-full")
	t.Setenv("POSTGRES_SSLROOTCERT", "")
	if _, err := Load(); err == nil {
		t.Fatal("HA accepted missing CA")
	}
	t.Setenv("POSTGRES_SSLROOTCERT", "/etc/cell/ca.crt")
	if _, err := Load(); err != nil {
		t.Fatal(err)
	}
}
