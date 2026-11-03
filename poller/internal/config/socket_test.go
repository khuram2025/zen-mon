package config

import (
	"github.com/jackc/pgx/v5/pgxpool"
	"testing"
)

func TestPostgresSocketDSN(t *testing.T) {
	c := PostgresConfig{Host: "/tmp/private socket", Port: 35432, Database: "zp_t_fixture", User: "zenplus", SSLMode: "disable"}
	p, e := pgxpool.ParseConfig(c.DSN())
	if e != nil {
		t.Fatal(e)
	}
	if p.ConnConfig.Host != c.Host || int(p.ConnConfig.Port) != c.Port || p.ConnConfig.Database != c.Database || p.ConnConfig.TLSConfig != nil {
		t.Fatal("socket DSN changed connection target")
	}
}
