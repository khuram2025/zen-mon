package store

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

// Opt-in private PostgreSQL for the SYSINFO tests, so they run locally with
// one variable and no cluster to prepare:
//
//	SYSINFO_TEST_PG=1 go test ./internal/store/ -run 'SystemInfo' -v
//
// What it needs:
//   - the PostgreSQL server binaries initdb, postgres and pg_ctl, found on PATH
//     or under /usr/lib/postgresql/<version>/bin (Debian, Ubuntu and the
//     GitHub-hosted ubuntu runners);
//   - a non-root account (initdb refuses to run as root).
//
// TestUpsertSystemInfoMigratedSchema additionally needs python3 and psql on
// PATH (or beside the server binaries), because it replays the repository's
// real migrations (scripts/run-migrations.py --include-init) into a database
// of the private cluster, which takes several seconds. Without them it skips
// with the reason. Use -run to pick one test.
//
// The cluster is created in a private 0700 directory, listens on a Unix
// socket only (no TCP listener), is never given the default port, and is
// stopped and deleted when the test binary exits normally. A run that is
// killed (SIGKILL, or go test -timeout expiring) leaves its postgres process
// and the /tmp/zpsysinfo-* directory behind: stop the process and delete the
// directory by hand. Every PG* variable is
// removed from the environment of the child processes and the DSNs name host,
// port and user explicitly, so neither the appliance database nor any other
// cluster on the machine can be reached by accident. SYSINFO_TEST_DSN, when
// set, still wins over SYSINFO_TEST_PG.

const sysInfoSuperuser = "zen"

// errMigrationPrereq marks a missing tool that the migrated-schema test needs,
// as opposed to a migration that failed.
var errMigrationPrereq = errors.New("migrated-schema prerequisite missing")

type sysInfoCluster struct {
	root    string // private directory: data, log and the socket
	binDir  string
	port    int
	baseDSN string // database "postgres"
}

var (
	sysInfoClusterOnce sync.Once
	sysInfoClusterVal  *sysInfoCluster
	sysInfoClusterErr  error
)

// TestMain only exists to stop the opt-in private cluster after the last test.
func TestMain(m *testing.M) {
	code := m.Run()
	if sysInfoClusterVal != nil {
		sysInfoClusterVal.stop()
	}
	os.Exit(code)
}

// privateSysInfoCluster starts the cluster on first use. The error is sticky.
func privateSysInfoCluster() (*sysInfoCluster, error) {
	sysInfoClusterOnce.Do(func() {
		sysInfoClusterVal, sysInfoClusterErr = startSysInfoCluster()
	})
	return sysInfoClusterVal, sysInfoClusterErr
}

func findPGBin() (string, error) {
	var candidates []string
	if p, err := exec.LookPath("initdb"); err == nil {
		if real, err := filepath.EvalSymlinks(p); err == nil {
			p = real
		}
		candidates = append(candidates, filepath.Dir(p))
	}
	globbed, _ := filepath.Glob("/usr/lib/postgresql/*/bin")
	sort.Slice(globbed, func(i, j int) bool { return versionKey(globbed[i]) > versionKey(globbed[j]) })
	candidates = append(candidates, globbed...)
	for _, dir := range candidates {
		ok := true
		for _, bin := range []string{"initdb", "postgres", "pg_ctl"} {
			if _, err := os.Stat(filepath.Join(dir, bin)); err != nil {
				ok = false
			}
		}
		if ok {
			return dir, nil
		}
	}
	return "", fmt.Errorf("PostgreSQL server binaries (initdb, postgres, pg_ctl) not found on PATH or in /usr/lib/postgresql/*/bin")
}

// versionKey orders /usr/lib/postgresql/<major>/bin directories numerically.
func versionKey(binDir string) int {
	n, err := strconv.Atoi(filepath.Base(filepath.Dir(binDir)))
	if err != nil {
		return 0
	}
	return n
}

// cleanPGEnv is os.Environ without libpq variables, with binDir first on PATH.
func cleanPGEnv(binDir string) []string {
	var env []string
	for _, kv := range os.Environ() {
		if strings.HasPrefix(kv, "PG") || strings.HasPrefix(kv, "PATH=") {
			continue
		}
		env = append(env, kv)
	}
	return append(env, "PATH="+binDir+string(os.PathListSeparator)+os.Getenv("PATH"))
}

func startSysInfoCluster() (*sysInfoCluster, error) {
	if os.Geteuid() == 0 {
		return nil, fmt.Errorf("initdb refuses to run as root; run the tests as an ordinary user")
	}
	binDir, err := findPGBin()
	if err != nil {
		return nil, err
	}
	// sun_path is 108 bytes on Linux; keep the socket path well inside it.
	root, err := os.MkdirTemp("", "zpsysinfo-")
	if err != nil {
		return nil, err
	}
	if len(root)+len("/.s.PGSQL.65535") > 100 {
		os.RemoveAll(root)
		if root, err = os.MkdirTemp("/tmp", "zpsysinfo-"); err != nil {
			return nil, err
		}
	}
	c := &sysInfoCluster{root: root, binDir: binDir}
	fail := func(format string, args ...any) (*sysInfoCluster, error) {
		c.stop()
		return nil, fmt.Errorf(format, args...)
	}

	// A free port number only names the socket file; nothing listens on TCP.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return fail("pick a port: %v", err)
	}
	c.port = ln.Addr().(*net.TCPAddr).Port
	ln.Close()
	if c.port == 5432 {
		return fail("refusing the default PostgreSQL port")
	}

	data := filepath.Join(root, "data")
	if out, err := c.run(filepath.Join(binDir, "initdb"), "-D", data, "-U", sysInfoSuperuser,
		"--auth=trust", "-E", "UTF8", "--no-locale"); err != nil {
		return fail("initdb: %v\n%s", err, out)
	}
	conf := fmt.Sprintf("\nlisten_addresses = ''\nunix_socket_directories = '%s'\nport = %d\n"+
		"fsync = off\nsynchronous_commit = off\nfull_page_writes = off\nmax_connections = 30\n"+
		"shared_buffers = 16MB\n", root, c.port)
	f, err := os.OpenFile(filepath.Join(data, "postgresql.conf"), os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return fail("open postgresql.conf: %v", err)
	}
	_, werr := f.WriteString(conf)
	f.Close()
	if werr != nil {
		return fail("write postgresql.conf: %v", werr)
	}
	if out, err := c.run(filepath.Join(binDir, "pg_ctl"), "-D", data, "-l", filepath.Join(root, "log"),
		"-w", "-t", "60", "start"); err != nil {
		return fail("pg_ctl start: %v\n%s", err, out)
	}
	c.baseDSN = c.dsn("postgres")
	return c, nil
}

func (c *sysInfoCluster) run(name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	cmd.Env = cleanPGEnv(c.binDir)
	out, err := cmd.CombinedOutput()
	return string(out), err
}

// dsn names the database over the cluster's Unix socket.
func (c *sysInfoCluster) dsn(database string) string {
	q := url.Values{"host": {c.root}, "port": {strconv.Itoa(c.port)}, "sslmode": {"disable"}}
	return "postgres://" + sysInfoSuperuser + "@/" + url.PathEscape(database) + "?" + q.Encode()
}

func (c *sysInfoCluster) stop() {
	if c.root == "" {
		return
	}
	data := filepath.Join(c.root, "data")
	if _, err := os.Stat(filepath.Join(data, "postmaster.pid")); err == nil {
		c.run(filepath.Join(c.binDir, "pg_ctl"), "-D", data, "-m", "immediate", "-w", "-t", "30", "stop")
	}
	os.RemoveAll(c.root)
}

// createMigratedDatabase creates database name in the cluster and replays the
// repository's migrations into it (init-postgres.sql plus every migrate-*.sql,
// the same runner the updater uses). The application role the migrations
// grant to must exist first.
func (c *sysInfoCluster) createMigratedDatabase(name string) error {
	python, err := exec.LookPath("python3")
	if err != nil {
		return fmt.Errorf("%w: python3 not found on PATH", errMigrationPrereq)
	}
	psqlDir := c.binDir
	if _, err := os.Stat(filepath.Join(psqlDir, "psql")); err != nil {
		p, lerr := exec.LookPath("psql")
		if lerr != nil {
			return fmt.Errorf("%w: psql not found on PATH or in %s", errMigrationPrereq, c.binDir)
		}
		psqlDir = filepath.Dir(p)
	}
	runner, err := filepath.Abs(filepath.Join("..", "..", "..", "scripts", "run-migrations.py"))
	if err != nil {
		return err
	}
	if _, err := os.Stat(runner); err != nil {
		return fmt.Errorf("%w: migration runner not found: %v", errMigrationPrereq, err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	conn, err := pgx.Connect(ctx, c.baseDSN)
	if err != nil {
		return err
	}
	defer conn.Close(ctx)
	for _, stmt := range []string{
		`CREATE ROLE zenplus LOGIN`,
		`CREATE DATABASE ` + pgx.Identifier{name}.Sanitize(),
	} {
		if _, err := conn.Exec(ctx, stmt); err != nil {
			return fmt.Errorf("%s: %w", stmt, err)
		}
	}

	cmd := exec.CommandContext(ctx, python, runner, "--database", name, "--user", sysInfoSuperuser,
		"--host", c.root, "--port", strconv.Itoa(c.port), "--include-init")
	cmd.Env = cleanPGEnv(psqlDir)
	if out, err := cmd.CombinedOutput(); err != nil {
		tail := string(out)
		if len(tail) > 2000 {
			tail = tail[len(tail)-2000:]
		}
		return fmt.Errorf("run-migrations.py: %v\n%s", err, tail)
	}
	return nil
}
