package main

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// The collector's embedded DDL must stay identical to the installer and the
// schema-v2 migration (contract §3: both paths produce the same schema on a
// fresh install). These tests read the SQL files from the repository, so a
// change on either side that is not mirrored fails here instead of shipping
// two different tables.

func readRepoSQL(t *testing.T, name string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "..", "scripts", name))
	if err != nil {
		t.Fatalf("read %s: %v", name, err)
	}
	return string(b)
}

var sqlComment = regexp.MustCompile(`(?m)--.*$`)

// normalizeSQL drops comments, the zenplus. database prefix (the collector
// writes to CLICKHOUSE_DB), the statement terminator and whitespace runs.
func normalizeSQL(s string) string {
	s = sqlComment.ReplaceAllString(s, "")
	s = strings.ReplaceAll(s, "zenplus.", "")
	s = strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(s), ";"))
	return strings.Join(strings.Fields(s), " ")
}

// statementStarting returns the statement of sql that begins with prefix
// (compared after normalisation, so line breaks do not matter).
func statementStarting(t *testing.T, sql, prefix string) string {
	t.Helper()
	prefix = normalizeSQL(prefix)
	for _, stmt := range strings.Split(sqlComment.ReplaceAllString(sql, ""), ";") {
		if n := normalizeSQL(stmt); strings.HasPrefix(n, prefix) {
			return n
		}
	}
	t.Fatalf("statement %q not found", prefix)
	return ""
}

func embeddedStatement(t *testing.T, prefix string) string {
	t.Helper()
	prefix = normalizeSQL(prefix)
	for _, s := range schemaStatements {
		if n := normalizeSQL(s); strings.HasPrefix(n, prefix) {
			return n
		}
	}
	t.Fatalf("collector schema has no statement starting %q", prefix)
	return ""
}

func TestSchemaFlowRecordsMatchesInstaller(t *testing.T) {
	want := statementStarting(t, readRepoSQL(t, "init-clickhouse.sql"), "CREATE TABLE IF NOT EXISTS zenplus.flow_records (")
	if got := embeddedStatement(t, "CREATE TABLE IF NOT EXISTS flow_records ("); got != want {
		t.Fatalf("flow_records DDL drifted from scripts/init-clickhouse.sql\ncollector: %s\ninstaller: %s", got, want)
	}
}

func TestSchemaMatchesMigration137(t *testing.T) {
	mig := readRepoSQL(t, "migrate-137-netflow-schema-v2-clickhouse.sql")
	for _, prefix := range []string{
		"ALTER TABLE zenplus.flow_records ADD COLUMN",
		"ALTER TABLE zenplus.flow_records MODIFY SETTING",
		"CREATE TABLE IF NOT EXISTS zenplus.flow_collector_stats (",
		"ALTER TABLE zenplus.flow_collector_stats ADD COLUMN",
		"CREATE TABLE IF NOT EXISTS zenplus.flow_templates (",
		"CREATE TABLE IF NOT EXISTS zenplus.flow_app_names (",
		"CREATE TABLE IF NOT EXISTS zenplus.flow_if_counters (",
	} {
		want := statementStarting(t, mig, prefix)
		if got := embeddedStatement(t, strings.ReplaceAll(prefix, "zenplus.", "")); got != want {
			t.Errorf("drifted from migrate-137:\ncollector: %s\nmigration: %s", got, want)
		}
	}
}

// The collector never creates or replaces materialized views (they belong to
// the migrations; the old embedded flow_traffic_5m_mv lacked the volume guard).
func TestSchemaHasNoViews(t *testing.T) {
	for _, s := range schemaStatements {
		if strings.Contains(strings.ToUpper(s), "VIEW") {
			t.Fatalf("collector DDL must not touch views: %s", normalizeSQL(s))
		}
	}
}
