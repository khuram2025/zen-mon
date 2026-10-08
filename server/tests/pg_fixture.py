"""Self-provisioning private PostgreSQL for SQL-level tests.

The first test that asks for it builds a throwaway cluster of its own: initdb
into a temp dir, trust auth on a unix-domain socket in a private 0700 directory
(no TCP listener at all), role ``zenplus``, then every migration replayed once
(``scripts/run-migrations.py --include-init``) into a template database, about
8-10 s in total. Each test gets a database cloned from that template in a few
hundred milliseconds. The cluster is stopped and its directories deleted when
the session ends, including after a failure; on Linux the postmaster also dies
with the pytest process, so a killed run does not leave one behind.

Trust authentication is only safe because nothing else can reach the socket:
the superuser role can run programs and read files as the account that started
the cluster, so it is never exposed on a TCP port. The socket path
(``<dir>/.s.PGSQL.<port>``) must fit in sockaddr_un (108 bytes on Linux), so when
the temp root is too long the socket directory is made under /tmp instead.

It touches nothing else. It never reads ``DATABASE_URL`` (CI points that at a
closed port), refuses port 5432, passes explicit host/port/user/database on
every connection, and strips ``PG*`` variables from the environment of every
child process, so the appliance database on 5432 or any other cluster on the
machine cannot be reached by accident. The server bin directory it found is put
first on PATH for every child, so a server-only install without ``psql`` on
PATH still works (``run-migrations.py`` runs a bare ``psql``).

Needs the PostgreSQL server binaries (``initdb``, ``postgres``, ``pg_isready``)
and ``psql``. On Debian/Ubuntu runners they live in /usr/lib/postgresql/<n>/bin.
Without them, or when running as root (initdb refuses), dependent tests skip
with the reason; set ``ZENPLUS_REQUIRE_PG=1`` to turn that skip into a failure.
That is also the rule whenever ``CI=true`` (GitHub Actions sets it), so a runner
that loses PostgreSQL fails the job instead of silently skipping every trigger
and migration test.

Using it from a test module (sibling top-level import, the same style as
``from conftest import FakeDB``)::

    from pg_fixture import pg_cluster, pg_db, pg_db_module  # noqa: F401

    def test_something(pg_db):
        pg_db.execute("INSERT INTO servers (display_name) VALUES ($1)", "srv-1")
        assert pg_db.scalar("SELECT count(*) FROM servers") == 1

Import the fixtures into each module that uses them; every import resolves to
the same process-wide cluster, so there is still only one per session.

* ``pg_cluster``    session cluster (``PgCluster``)
* ``pg_db``         fresh database per test, cloned from the template, dropped after
* ``pg_db_module``  the same, shared by every test in a module
* ``PgDatabase.psql(sql)`` runs a script through psql (multi-statement, meta
  commands, ON_ERROR_STOP) and returns stdout; ``.run_psql(...)`` returns the
  CompletedProcess; ``.psql_file(path)`` applies a file.
* ``PgDatabase.query(sql, *args)`` runs ONE statement through asyncpg with
  ``$1`` parameters and returns a list of tuples in native Python types (uuid,
  datetime, jsonb already decoded); ``.scalar``/``.execute`` are the obvious
  companions. ``execute`` with no args accepts a multi-statement script.
* ``PgDatabase.url`` is ``postgresql+asyncpg://zen@/<db>?host=<socket dir>&port=<port>``
  for SQLAlchemy (``.replace("postgresql+asyncpg://", "postgresql://")`` is the
  matching asyncpg DSN); ``url_for("zenplus")`` connects as the application role.
* ``PgCluster.replay_migrations`` and ``copy_migration_set`` build a database
  from any subset of the migrations (for N-1 upgrade tests).
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import ctypes
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

SUPERUSER = "zen"
APP_ROLE = "zenplus"
TEMPLATE_DB = "zp_template"
HOST = "127.0.0.1"  # only used to find a free port number; nothing listens on it
APPLIANCE_PORT = 5432
# sun_path is 108 bytes on Linux (104 on macOS); leave a margin.
SOCKET_PATH_LIMIT = 100
_LONGEST_SOCKET_NAME = "/.s.PGSQL.65535"
_SAFE_SOCKET_DIR = re.compile(r"[A-Za-z0-9_./+-]+")


class PsqlError(RuntimeError):
    """psql exited non-zero; ``str(exc)`` carries stderr."""


def _clean_env(bin_dir: Path | None = None) -> dict[str, str]:
    """Environment for child processes with every libpq variable removed and,
    when ``bin_dir`` is given, that PostgreSQL bin directory first on PATH."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env["PGCONNECT_TIMEOUT"] = "10"
    if bin_dir is not None:
        env["PATH"] = os.pathsep.join([str(bin_dir), *filter(None, [env.get("PATH")])])
    return env


def _die_with_parent() -> None:
    # PR_SET_PDEATHSIG = 1; SIGQUIT is postgres' immediate shutdown.
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGQUIT)
    except Exception:  # not Linux, or no libc.so.6: the finalizers still run
        pass


def _version_key(path: Path) -> tuple:
    try:
        return tuple(int(p) for p in path.parent.name.split("."))
    except ValueError:
        return (0,)


def _find_pg_bin() -> Path | None:
    candidates: list[Path] = []
    found = shutil.which("initdb")
    if found:
        candidates.append(Path(found).resolve().parent)
    candidates += sorted(Path("/usr/lib/postgresql").glob("*/bin"), key=_version_key, reverse=True)
    for directory in candidates:
        if (directory / "initdb").exists() and (directory / "postgres").exists():
            return directory
    return None


def _free_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((HOST, 0))
            port = sock.getsockname()[1]
        if port != APPLIANCE_PORT:
            return port


def _pg_required() -> bool:
    """A missing PostgreSQL is a failure, not a skip: explicitly, or on CI."""
    return (os.environ.get("ZENPLUS_REQUIRE_PG") == "1"
            or os.environ.get("CI", "").strip().lower() in ("true", "1"))


def _unavailable(reason: str):
    if _pg_required():
        raise RuntimeError(f"private PostgreSQL required but unavailable: {reason}")
    pytest.skip(reason)


def _make_socket_dir(root: Path) -> Path:
    """A private (0700) directory for the unix socket: ``<root>/sock`` when
    ``<dir>/.s.PGSQL.<port>`` fits in sockaddr_un and the path is plain enough
    for a postgres list setting, otherwise a fresh mkdtemp directory under /tmp."""
    inside = root / "sock"
    candidate = str(inside)
    if len(candidate + _LONGEST_SOCKET_NAME) <= SOCKET_PATH_LIMIT and _SAFE_SOCKET_DIR.fullmatch(candidate):
        inside.mkdir(mode=0o700)
        os.chmod(inside, 0o700)
        return inside
    outside = Path(tempfile.mkdtemp(prefix="zpg-", dir="/tmp"))  # mkdtemp creates it 0700
    os.chmod(outside, 0o700)
    return outside


def _run_async(fn):
    """Run ``fn()`` (an awaitable factory) on a private loop in a worker thread,
    so it works whether or not the calling test is itself inside an event loop."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(fn())).result()


class PgDatabase:
    """One database on the private cluster."""

    def __init__(self, cluster: "PgCluster", name: str):
        self.cluster = cluster
        self.name = name

    # ---- connection strings
    def url_for(self, user: str = SUPERUSER) -> str:
        return f"postgresql+asyncpg://{self.cluster.conn_tail(user, self.name)}"

    @property
    def url(self) -> str:
        return self.url_for(SUPERUSER)

    def _dsn(self, user: str = SUPERUSER) -> str:
        return f"postgresql://{self.cluster.conn_tail(user, self.name)}"

    # ---- psql
    def run_psql(self, sql: str | None = None, *, file: Path | str | None = None,
                 check: bool = True) -> subprocess.CompletedProcess:
        return self.cluster.run_psql(self.name, sql, file=file, check=check)

    def psql(self, sql: str) -> str:
        return self.run_psql(sql).stdout

    def psql_file(self, path: Path | str) -> str:
        return self.run_psql(file=path).stdout

    # ---- asyncpg
    def _with_conn(self, work, user: str = SUPERUSER):
        import asyncpg

        async def go():
            conn = await asyncpg.connect(self._dsn(user), timeout=15)
            try:
                for kind in ("json", "jsonb"):
                    await conn.set_type_codec(kind, encoder=json.dumps, decoder=json.loads,
                                              schema="pg_catalog")
                return await work(conn)
            finally:
                await conn.close()

        return _run_async(go)

    def query(self, sql: str, *args, user: str = SUPERUSER) -> list[tuple]:
        async def work(conn):
            return [tuple(r) for r in await conn.fetch(sql, *args)]
        return self._with_conn(work, user)

    def scalar(self, sql: str, *args, user: str = SUPERUSER):
        rows = self.query(sql, *args, user=user)
        return rows[0][0] if rows else None

    def execute(self, sql: str, *args, user: str = SUPERUSER) -> str:
        async def work(conn):
            return await conn.execute(sql, *args)
        return self._with_conn(work, user)

    def drop(self) -> None:
        self.cluster.drop_database(self.name)


class PgCluster:
    def __init__(self, bin_dir: Path, psql: Path, root: Path, port: int, sock_dir: Path | None = None):
        if port == APPLIANCE_PORT:
            raise RuntimeError("refusing to use port 5432: that is the appliance database")
        self.bin_dir, self._psql, self.root, self.port = bin_dir, psql, root, port
        # Where the postmaster listens. There is no TCP listener, so this
        # directory's 0700 mode is the whole access control.
        self.sock_dir = sock_dir if sock_dir is not None else root / "sock"
        self.data = root / "data"
        self.log = root / "postgres.log"
        self.proc: subprocess.Popen | None = None
        self.template: PgDatabase | None = None
        self._stopped = False

    def _env(self) -> dict[str, str]:
        return _clean_env(self.bin_dir)

    def conn_tail(self, user: str, database: str) -> str:
        """``user@/database?host=<socket dir>&port=<port>``: the part of a
        connection URL after the scheme, for SQLAlchemy and asyncpg alike."""
        return f"{user}@/{database}?host={quote(str(self.sock_dir), safe='/')}&port={self.port}"

    # ---- lifecycle
    def _start(self) -> None:
        log = open(self.log, "ab")
        self.proc = subprocess.Popen(
            [str(self.bin_dir / "postgres"), "-D", str(self.data),
             "-p", str(self.port), "-c", "listen_addresses=",
             "-c", f"unix_socket_directories={self.sock_dir}",
             "-c", "unix_socket_permissions=0700",
             "-c", "fsync=off", "-c", "synchronous_commit=off",
             "-c", "full_page_writes=off", "-c", "max_connections=50",
             "-c", "shared_buffers=64MB", "-c", "log_min_messages=warning"],
            stdout=log, stderr=log, env=self._env(), cwd=str(self.root),
            preexec_fn=_die_with_parent if sys.platform.startswith("linux") else None,
        )
        log.close()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"postgres exited during start (rc={self.proc.returncode})")
            ready = subprocess.run(
                [str(self.bin_dir / "pg_isready"), "-h", str(self.sock_dir), "-p", str(self.port), "-q"],
                env=self._env(), capture_output=True,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("postgres did not accept connections within 60 s")
        # Belt and braces: whatever answers on our private socket must be our data directory.
        where = self.run_psql("postgres", "SHOW data_directory").stdout.strip()
        if Path(where).resolve() != self.data.resolve():
            raise RuntimeError(f"connected to a foreign cluster at port {self.port} ({where})")

    def _kill_process(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        proc = self.proc
        try:
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGINT)  # fast shutdown
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=30)
        finally:
            shutil.rmtree(self.root, ignore_errors=True)
            # The socket directory lives outside root only when _make_socket_dir made it
            # under /tmp as zpg-*; never remove a directory this module did not create.
            if self.root not in self.sock_dir.parents and self.sock_dir.name.startswith("zpg-"):
                shutil.rmtree(self.sock_dir, ignore_errors=True)

    # ---- running SQL
    def run_psql(self, database: str, sql: str | None = None, *, file: Path | str | None = None,
                 check: bool = True) -> subprocess.CompletedProcess:
        cmd = [str(self._psql), "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
               "-h", str(self.sock_dir), "-p", str(self.port), "-U", SUPERUSER, "-d", database]
        if file is not None:
            cmd += ["-f", str(file)]
        result = subprocess.run(cmd, input=None if file is not None else sql, text=True,
                                capture_output=True, env=self._env(), timeout=600)
        if check and result.returncode != 0:
            raise PsqlError(f"psql exited {result.returncode}: {result.stderr.strip()}")
        return result

    def create_database(self, name: str | None = None, *, template: str | None = TEMPLATE_DB) -> PgDatabase:
        name = name or f"zp_t_{uuid.uuid4().hex[:10]}"
        clause = f' TEMPLATE "{template}"' if template else ""
        self.run_psql("postgres", f'CREATE DATABASE "{name}"{clause}')
        return PgDatabase(self, name)

    def drop_database(self, name: str) -> None:
        self.run_psql("postgres", f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    # ---- migrations
    def replay_migrations(self, database: str, *, scripts_dir: Path | str | None = None,
                          lock: Path | str | None = None, include_init: bool = True) -> str:
        """Run scripts/run-migrations.py against ``database`` (default: the repo's
        full set). Returns its stdout; raises on a non-zero exit."""
        runner = SCRIPTS_DIR / "run-migrations.py"
        cmd = [sys.executable, str(runner), "--database", database, "--user", SUPERUSER,
               "--host", str(self.sock_dir), "--port", str(self.port),
               "--scripts-dir", str(scripts_dir or SCRIPTS_DIR)]
        if lock is not None:
            cmd += ["--lock", str(lock)]
        if include_init:
            cmd.append("--include-init")
        result = subprocess.run(cmd, text=True, capture_output=True, env=self._env(), timeout=600)
        if result.returncode != 0:
            raise RuntimeError(
                f"run-migrations failed ({result.returncode}):\n{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
        return result.stdout

    def _build_template(self) -> None:
        self.run_psql("postgres", f"CREATE ROLE {APP_ROLE} LOGIN")
        self.run_psql("postgres", f'CREATE DATABASE "{TEMPLATE_DB}"')
        self.replay_migrations(TEMPLATE_DB)
        self.run_psql("postgres",
                      f'ALTER DATABASE "{TEMPLATE_DB}" WITH IS_TEMPLATE true ALLOW_CONNECTIONS false')
        self.template = PgDatabase(self, TEMPLATE_DB)


def copy_migration_set(dest: Path, *, exclude: Iterable[str] = ()) -> tuple[Path, Path]:
    """Copy init/seed SQL, every migrate-*.sql and migrations.lock into ``dest``,
    leaving out the named migrations (and their lock lines). Returns
    ``(scripts_dir, lock_path)`` for ``replay_migrations``."""
    skip = set(exclude)
    scripts = dest / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for path in SCRIPTS_DIR.glob("*.sql"):
        if path.name not in skip:
            shutil.copy2(path, scripts / path.name)
    lock_lines = [
        line for line in (SCRIPTS_DIR / "migrations.lock").read_text().splitlines()
        if not any(line.strip().endswith(name) for name in skip)
    ]
    lock = scripts / "migrations.lock"
    lock.write_text("\n".join(lock_lines) + "\n")
    return scripts, lock


# ---------------------------------------------------------------- singleton
#
# Fixtures imported into several modules are registered once per importing
# module, so a session-scoped fixture body can run once per module. The cluster
# therefore lives here, once per process, and the fixtures only fetch it.

_lock = threading.Lock()
_cluster: PgCluster | None = None
_failure: BaseException | None = None


def _provision() -> PgCluster:
    if os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0):
        _unavailable("private PostgreSQL needs a non-root POSIX user (initdb refuses to run as root)")
    bin_dir = _find_pg_bin()
    if bin_dir is None:
        _unavailable("PostgreSQL server binaries (initdb/postgres) not found on PATH or in /usr/lib/postgresql/*/bin")
    psql = bin_dir / "psql"
    if not psql.exists():
        found = shutil.which("psql")
        if found is None:
            _unavailable("psql not found on PATH")
        psql = Path(found)

    root = Path(tempfile.mkdtemp(prefix="zenplus-inventory-pg-"))
    sock_dir: Path | None = None
    cluster: PgCluster | None = None
    try:
        sock_dir = _make_socket_dir(root)
        init = subprocess.run(
            [str(bin_dir / "initdb"), "-D", str(root / "data"), "-U", SUPERUSER,
             "--auth-local=trust", "--auth-host=reject",  # nothing listens on TCP; reject if it ever did
             "-E", "UTF8", "--no-locale", "--no-sync"],
            capture_output=True, text=True, env=_clean_env(bin_dir), timeout=120)
        if init.returncode != 0:
            raise RuntimeError(f"initdb failed: {init.stderr.strip()}")
        last: Exception | None = None
        for _ in range(3):  # the port only names the socket file, but keep the retry for odd failures
            cluster = PgCluster(bin_dir, psql, root, _free_port(), sock_dir)
            try:
                cluster._start()
                break
            except RuntimeError as exc:
                last = exc
                cluster._kill_process()
                cluster = None
        if cluster is None:
            raise RuntimeError(f"could not start a private PostgreSQL: {last}")
        cluster._build_template()
        return cluster
    except BaseException:
        if cluster is not None:
            cluster.stop()
        else:
            for directory in (root, sock_dir):
                if directory is not None:
                    shutil.rmtree(directory, ignore_errors=True)
        raise


def get_cluster() -> PgCluster:
    global _cluster, _failure
    with _lock:
        if _cluster is not None:
            return _cluster
        if _failure is not None:
            raise RuntimeError(f"private PostgreSQL provisioning already failed: {_failure}")
        try:
            _cluster = _provision()
        except pytest.skip.Exception:
            raise
        except BaseException as exc:
            _failure = exc
            raise
        atexit.register(shutdown_cluster)
        return _cluster


def shutdown_cluster() -> None:
    global _cluster
    with _lock:
        cluster, _cluster = _cluster, None
    if cluster is not None:
        cluster.stop()


@pytest.fixture(scope="session")
def pg_cluster(request):
    cluster = get_cluster()
    request.addfinalizer(shutdown_cluster)
    return cluster


@pytest.fixture
def pg_db(pg_cluster):
    db = pg_cluster.create_database()
    try:
        yield db
    finally:
        db.drop()


@pytest.fixture(scope="module")
def pg_db_module(pg_cluster):
    db = pg_cluster.create_database()
    try:
        yield db
    finally:
        db.drop()
