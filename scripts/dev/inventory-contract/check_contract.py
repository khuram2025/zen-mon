#!/usr/bin/env python3
"""Check Documentation/Inventory/05-P1-API-CONTRACT.md against the code and the repository.

Run from the repository root with the server's Python requirements installed (no database, no network)::

    python scripts/dev/inventory-contract/check_contract.py [path-to-another-copy-of-the-document]

It verifies, and exits 1 with a list when any of these fail:

1. routes      every route under /api/v1/inventory has a "### 3.N `METHOD /api/v1/inventory/...`" heading;
2. parameters  every query parameter of a route is named in the route's section (or in section 2 for the asset filters),
               and every name in the first column of a section's parameter table is a parameter of that route;
3. enums       the values the document lists for a Literal-typed query parameter are the route's values;
4. interfaces  every `interface X { ... }` of the document has exactly the fields of the pydantic response model of the same
               name (nested inline objects included), with the same nullability;
5. touchpoints the extra parameters of GET /devices and the extra fields of GET /servers/{id}/software exist;
6. paths       every repository path named in backticks exists, every bare file name exists somewhere in the tree, every
               `/inventory/...` page route is a route of the dashboard, every `/summary`-style API path is a route;
7. references  "section N", "doc 02 section N" and "doc 03 §N.M" point at headings that exist.

It does not replace test_inventory_fixtures.py (which proves every JSON example is a cut of a real response); it covers the
prose and the TypeScript.
"""

from __future__ import annotations

import re
import subprocess
import sys
import typing
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "server"))

DOC = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else REPO / "Documentation" / "Inventory" / "05-P1-API-CONTRACT.md"
API = "/api/v1/inventory"

problems: list[str] = []
notes: list[str] = []


def fail(area: str, msg: str) -> None:
    problems.append(f"[{area}] {msg}")


# ── the document ─────────────────────────────────────────────────────────────

text = DOC.read_text(encoding="utf-8")
lines = text.split("\n")


def sections() -> dict[str, str]:
    """'3.1' -> the text of that ### section (up to the next ##/###); '2' -> the text of the ## section."""
    out: dict[str, str] = {}
    cur, buf = None, []
    for line in lines + ["## end"]:
        m = re.match(r"^(#{2,3}) (\d+(?:\.\d+)?)[ .]", line)
        if m or line.startswith("## "):
            if cur is not None:
                out[cur] = "\n".join(buf)
            cur, buf = (m.group(2) if m else "end"), [line]
        else:
            buf.append(line)
    return out


SEC = sections()


def backticked(s: str) -> list[str]:
    return re.findall(r"`([^`\n]+)`", s)


# ── 1-3: routes, parameters, enums ───────────────────────────────────────────

def query_params(dependant) -> dict:
    out = {}
    for p in dependant.query_params:
        out[p.alias or p.name] = p
    for d in dependant.dependencies:
        out.update(query_params(d))
    return out


def literal_values(field) -> list[str] | None:
    ann = getattr(getattr(field, "field_info", None), "annotation", None) or getattr(field, "type_", None)
    stack = [ann]
    while stack:
        a = stack.pop()
        if a is None:
            continue
        if typing.get_origin(a) is typing.Literal:
            return [str(v) for v in typing.get_args(a)]
        stack.extend(typing.get_args(a))
    return None


def effective_routes():
    """Every API route with its final path (routers included under a prefix are wrapped by the application)."""
    from app.core.route_policy import _iter_effective_routes
    from app.main import app

    for r in _iter_effective_routes(app):
        yield getattr(r, "path_format", None) or r.path, r


def check_routes() -> None:
    routes = [(path, r) for path, r in effective_routes() if path.startswith(API)]
    if not routes:
        fail("routes", "no inventory routes found")
        return
    section2 = SEC["2"]
    headings = {}
    for k, body in SEC.items():
        m = re.match(r"### \d+\.\d+ `([A-Z]+) (/api/v1/inventory[^`]*)`( and `([A-Z]+) (/api/v1/inventory[^`]*)`)?", body.split("\n", 1)[0])
        if m:
            headings[(m.group(1), m.group(2))] = k
            if m.group(4):
                headings[(m.group(4), m.group(5))] = k
    for path, r in routes:
        for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
            doc_path = path.replace("{asset_id}", "{id}")
            key = headings.get((method, doc_path))
            if key is None:
                fail("routes", f"{method} {path} has no section heading in the document")
                continue
            body = SEC[key]
            params = query_params(r.dependant)
            names_in_body = set(backticked(body))
            names_in_body |= {t for chunk in backticked(body) for t in re.findall(r"[a-z_]+", chunk)}
            for name in params:
                if name not in names_in_body and f"`{name}`" not in section2:
                    fail("parameters", f"{method} {path}: query parameter {name!r} is not named in section {key} or section 2")
            # the first column of the section's parameter tables (tables whose header row starts with "Parameter")
            documented: set[str] = set()
            in_param_table = False
            for ln in body.split("\n"):
                if ln.startswith("| Parameter "):
                    in_param_table = True
                    continue
                if not ln.startswith("|"):
                    in_param_table = False
                    continue
                if in_param_table and not ln.startswith("|---"):
                    cell = ln.split(" | ", 1)[0]
                    documented.update(re.findall(r"`([a-z_]+)`", cell))
            for tok in sorted(documented):
                if tok not in params:
                    fail("parameters", f"section {key} documents the parameter {tok!r}, which {method} {path} does not take")
            # enums: a Literal-typed parameter's values are all named in section 2 / the section
            for name, field in params.items():
                vals = literal_values(field)
                if not vals:
                    continue
                scope_text = section2 if re.search(rf"^\| `{re.escape(name)}` \|", section2, re.M) else body
                row = next((ln for ln in scope_text.split("\n") if re.match(rf"^\| `{re.escape(name)}`", ln)), "")
                missing = [v for v in vals if f"`{v}`" not in row and v not in row]
                if row and missing:
                    fail("enums", f"{method} {path}: parameter {name!r} allows {vals}; the documented row lacks {missing}")


# ── 4: interfaces vs pydantic ────────────────────────────────────────────────

TOK = re.compile(r"\s*(?:(?P<nl>\n)|(?P<id>[A-Za-z_][A-Za-z0-9_]*)|(?P<str>'[^']*')|(?P<num>\d+)|(?P<p>[{}\[\]<>():;,|&?=]))")


def tokenize(src: str) -> list[tuple[str, str]]:
    src = re.sub(r"//[^\n]*", "", src)
    toks, pos = [], 0
    while pos < len(src):
        m = TOK.match(src, pos)
        if not m:
            if src[pos:].strip() == "":
                break
            raise ValueError(f"cannot tokenize near {src[pos:pos+40]!r}")
        pos = m.end()
        kind = m.lastgroup
        toks.append((kind, m.group(kind)))
    return toks


class TsParser:
    def __init__(self, toks):
        self.t, self.i = toks, 0

    def peek(self, skip_nl=True):
        j = self.i
        while skip_nl and j < len(self.t) and self.t[j][0] == "nl":
            j += 1
        return self.t[j] if j < len(self.t) else ("eof", "")

    def skip_nl(self):
        while self.i < len(self.t) and self.t[self.i][0] == "nl":
            self.i += 1

    def take(self, value=None):
        self.skip_nl()
        tok = self.t[self.i]
        if value is not None and tok[1] != value:
            raise ValueError(f"expected {value!r}, got {tok} at token {self.i}")
        self.i += 1
        return tok

    def type(self):
        parts = [self.inter()]
        while self.peek()[1] == "|":
            self.take("|")
            parts.append(self.inter())
        return parts[0] if len(parts) == 1 else ("union", parts)

    def inter(self):
        parts = [self.atom()]
        while self.peek()[1] == "&":
            self.take("&")
            parts.append(self.atom())
        return parts[0] if len(parts) == 1 else ("inter", parts)

    def atom(self):
        t = self.primary()
        while self.peek(skip_nl=False)[1] == "[":
            self.take("[")
            self.take("]")
            t = ("arr", t)
        return t

    def primary(self):
        kind, v = self.take()
        if v == "{":
            return self.obj()
        if v == "(":
            t = self.type()
            self.take(")")
            return t
        if kind == "str":
            return ("lit", v.strip("'"))
        if kind == "id":
            if v == "null":
                return ("null",)
            if v == "Record":
                self.take("<")
                k = self.type()
                self.take(",")
                val = self.type()
                self.take(">")
                return ("record", k, val)
            if v == "Pick":
                raise ValueError("Pick<> is not supported")
            return ("ref", v)
        raise ValueError(f"unexpected token {kind} {v!r}")

    def obj(self):
        members = {}
        while True:
            self.skip_nl()
            while self.peek()[1] in (";", ","):
                self.take()
                self.skip_nl()
            if self.peek()[1] == "}":
                self.take("}")
                return ("obj", members)
            kind, name = self.take()
            if name == "?":
                raise ValueError("stray ?")
            optional = False
            if self.peek(skip_nl=False)[1] == "?":
                self.take("?")
                optional = True
            self.take(":")
            members[name] = (self.type(), optional)


def parse_interfaces() -> dict[str, tuple]:
    out = {}
    for block in re.findall(r"```ts\n(.*?)\n```", text, re.S):
        toks = tokenize(block)
        p = TsParser(toks)
        while p.i < len(toks):
            if p.peek()[0] == "eof":
                break
            kind, v = p.take()
            if v == "interface":
                name = p.take()[1]
                p.take("{")
                out[name] = p.obj()
            else:
                raise ValueError(f"unexpected top-level token {v!r}")
    return out


def unwrap_py(ann):
    """(annotation without Optional, nullable)"""
    nullable = False
    origin = typing.get_origin(ann)
    if origin is typing.Union or str(origin) == "<class 'types.UnionType'>":
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        nullable = len(args) != len(typing.get_args(ann))
        ann = args[0] if len(args) == 1 else typing.Union[tuple(args)]
    return ann, nullable


def ts_nullable(t) -> bool:
    if t[0] == "null":
        return True
    if t[0] == "union":
        return any(ts_nullable(x) for x in t[1])
    return False


def ts_strip_null(t):
    if t[0] == "union":
        rest = [x for x in t[1] if x[0] != "null"]
        return rest[0] if len(rest) == 1 else ("union", rest)
    return t


def compare(ts, ann, path, interfaces, health_keys):
    from pydantic import BaseModel

    ann, py_nullable = unwrap_py(ann)
    ts_null = ts_nullable(ts)
    t = ts_strip_null(ts)
    if t[0] == "null":
        return          # `lifecycle: null`
    if ts_null != py_nullable:
        fail("interfaces", f"{path}: nullable in {'TypeScript' if ts_null else 'the schema'} only")
    while t[0] == "ref" and t[1] in interfaces:
        t = interfaces[t[1]]
    if t[0] == "inter":
        # Record<HealthKey, number> & { ... }
        keys: dict = {}
        for part in t[1]:
            if part[0] == "record":
                for k in health_keys:
                    keys[k] = None
            elif part[0] == "obj":
                keys.update(part[1])
        t = ("obj", keys)
    origin = typing.get_origin(ann)
    if t[0] == "arr":
        if origin is not list:
            fail("interfaces", f"{path}: array in TypeScript, {ann} in the schema")
            return
        compare(t[1], typing.get_args(ann)[0], path + "[]", interfaces, health_keys)
    elif t[0] == "obj":
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            fields = {(f.alias or n): f for n, f in ann.model_fields.items()}
            ts_keys, py_keys = set(t[1]), set(fields)
            for k in sorted(ts_keys - py_keys):
                fail("interfaces", f"{path}.{k}: in the document, not in the schema {ann.__name__}")
            for k in sorted(py_keys - ts_keys):
                fail("interfaces", f"{path}.{k}: in the schema {ann.__name__}, not in the document")
            for k in sorted(ts_keys & py_keys):
                if t[1][k] is None:
                    continue
                compare(t[1][k][0], fields[k].annotation, f"{path}.{k}", interfaces, health_keys)
        elif origin is dict:
            pass        # a free-form object in the schema (paired, fresh_hours, ...): keys are not modelled
        else:
            fail("interfaces", f"{path}: object in TypeScript, {ann} in the schema")
    elif t[0] == "record":
        if origin is not dict and not (isinstance(ann, type) and issubclass(ann, BaseModel)):
            fail("interfaces", f"{path}: Record in TypeScript, {ann} in the schema")
        elif isinstance(ann, type) and issubclass(ann, BaseModel):
            # Record<'a' | 'b', X> over a model whose fields are exactly those literals
            keys = t[1]
            lits = [x[1] for x in (keys[1] if keys[0] == "union" else [keys]) if x[0] == "lit"]
            if lits and set(lits) != set(ann.model_fields):
                fail("interfaces", f"{path}: Record keys {sorted(lits)} differ from the schema fields {sorted(ann.model_fields)}")


def check_interfaces() -> None:
    from app.schemas import inventory as schemas
    from app.services import inventory_query as iq

    try:
        interfaces = parse_interfaces()
    except ValueError as exc:
        fail("interfaces", f"the TypeScript of the document does not parse: {exc}")
        return
    checked = 0
    for name, ts in interfaces.items():
        model = getattr(schemas, name, None)
        if model is None:
            notes.append(f"interface {name}: no pydantic model of that name (not compared)")
            continue
        compare(ts, model, name, interfaces, iq.HEALTH_KEYS)
        checked += 1
    # enumerations the TypeScript spells out
    expected = {
        ("AssetRow", "freshness"): iq.FRESHNESS,
        ("AssetRow", "kind"): iq.KINDS,
        ("ChangeRow", "category"): iq.CHANGE_CATEGORIES,
        ("ChangeRow", "action"): iq.CHANGE_ACTIONS,
    }
    for (iface, field), values in expected.items():
        t = interfaces.get(iface)
        if not t or field not in t[1]:
            fail("interfaces", f"{iface}.{field} is missing")
            continue
        ft = ts_strip_null(t[1][field][0])
        lits = [x[1] for x in (ft[1] if ft[0] == "union" else [ft]) if x[0] == "lit"]
        if set(lits) != set(values):
            fail("interfaces", f"{iface}.{field}: the document lists {sorted(lits)}, the code has {sorted(values)}")
    notes.append(f"{checked} interfaces compared with their schema models")


# ── 5: touchpoints ───────────────────────────────────────────────────────────

def check_touchpoints() -> None:
    by = {(m, path): r for path, r in effective_routes() for m in r.methods}
    dev = by.get(("GET", "/api/v1/devices"))
    if dev is None:
        fail("touchpoints", "GET /api/v1/devices not found")
    else:
        names = set(query_params(dev.dependant))
        for p in ("vendor", "model", "os_version", "facets"):
            if p not in names:
                fail("touchpoints", f"GET /api/v1/devices has no query parameter {p!r}")
        if "os_version_display" not in SEC["3.15"]:
            fail("touchpoints", "section 3.15 does not name os_version_display")
    if ("GET", "/api/v1/servers/{server_id}/software") not in by:
        fail("touchpoints", "GET /api/v1/servers/{server_id}/software not found")
    for token in ("product_key", "agent_truncated", "Server not found"):
        if token not in SEC["3.15"]:
            fail("touchpoints", f"section 3.15 does not mention {token}")
    # the fields exist in the source (the tests prove the behaviour)
    src = (REPO / "server/app/api/v1/servers.py").read_text(encoding="utf-8")
    for token in ("agent_truncated", "name_key AS product_key", "Server not found"):
        if token not in src:
            fail("touchpoints", f"servers.py no longer contains {token!r}")
    dsrc = (REPO / "server/app/api/v1/devices.py").read_text(encoding="utf-8")
    if "os_version_display" not in dsrc:
        fail("touchpoints", "devices.py no longer contains os_version_display")


# ── 6-7: paths and references ────────────────────────────────────────────────

def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=REPO, capture_output=True, text=True, check=True)
    return out.stdout.split("\n")


def check_paths() -> None:
    files = [f for f in tracked_files() if f]
    basenames = {Path(f).name for f in files}
    roots = ("server/", "dashboard/", "Documentation/", "scripts/", ".github/")
    for tok in sorted(set(backticked(text))):
        t = tok.strip()
        if "<" in t or "*" in t or " " in t.split("(")[0].strip() and not t.startswith(roots):
            pass
        if t.startswith(roots) and "<" not in t and "*" not in t and " " not in t:
            p = REPO / t.rstrip("/")
            if not p.exists():
                fail("paths", f"`{t}` does not exist")
        elif re.fullmatch(r"[A-Za-z0-9_.\-]+\.(py|ts|tsx|md|json|csv|sql|mjs|yml)", t) and "<" not in t:
            if t not in basenames and not t.startswith("zenplus-"):
                fail("paths", f"file `{t}` does not exist in the repository")

    # dashboard page routes
    nav = (REPO / "dashboard/src/components/layout/navigation.ts").read_text(encoding="utf-8")
    pages = {m for m in re.findall(r"to: '(/inventory[^']*)'", nav)}
    pages.add("/inventory/software/<x>")
    api_routes = {path[len(API):] for path, _r in effective_routes() if path.startswith(API)}
    api_norm = {re.sub(r"\{[^}]*\}", "{}", p) for p in api_routes}
    scan = text.replace(SEC.get("7", ""), "")      # section 7 names endpoints of later phases on purpose
    for tok in sorted(set(backticked(scan))):
        t = tok.strip()
        if t.startswith(("GET ", "PUT ", "POST ")):
            t = t.split(" ", 1)[1]
        if t.startswith("/inventory") and re.sub(r"\{[^}]*\}|<[^>]*>", "{}", t[len("/inventory"):].split("?")[0]) in api_norm:
            continue            # an API path written without its /api/v1 prefix
        if t.startswith("/inventory"):
            page = re.sub(r"\?.*$", "", t)
            page = re.sub(r"[:<][A-Za-z_]+>?$", "<x>", page) if page.count("/") >= 3 else page
            page = page.replace("/<product_key>", "/<x>").replace("/:productKey", "/<x>")
            if page.rstrip("/") not in pages and page != "/inventory":
                fail("paths", f"`{tok}` is not a page route of the dashboard ({sorted(pages)})")
        elif t.startswith(API):
            rel = re.sub(r"\{[^}]*\}", "{}", t[len(API):].split("?")[0]).replace("<kind>", "{}").replace("<id>", "{}")
            rel = rel or "/"
            if t.startswith(API + "/…") or rel == "/…":
                continue
            if rel not in api_norm and rel != "/":
                fail("paths", f"`{tok}` is not an inventory API route")
        elif re.fullmatch(r"/(summary|assets|os|hardware|changes|software|trends|settings|export)(/[A-Za-z{}_<>/]*)?(\?[^`]*)?", t):
            rel = re.sub(r"\{[^}]*\}|<[^>]*>", "{}", t.split("?")[0])
            if rel not in api_norm and rel not in pages and ("/inventory" + rel) not in pages:
                if not any(re.fullmatch(re.escape(a).replace(r"\{\}", r"[^/]+"), rel) for a in api_norm):
                    fail("paths", f"`{tok}` is neither an inventory API route nor a page route")


def check_references() -> None:
    headings = set(SEC)
    for m in re.finditer(r"(?<![0-9A-Za-z])section (\d+(?:\.\d+)?)(?! of)", text):
        if "doc 0" in text[max(0, m.start() - 8):m.start()]:
            continue
        if m.group(1) not in headings:
            fail("references", f"'section {m.group(1)}' has no heading in the document")
    docs = {"01": "01-RESEARCH-AND-GAP-ANALYSIS.md", "02": "02-ARCHITECTURE-DATA-MODEL-API.md", "03": "03-UI-UX-SPEC.md",
            "04": "04-ROADMAP-PHASES-AND-TASKS.md", "00": "00-INDEX.md"}
    heads: dict[str, set[str]] = {}
    for n, fname in docs.items():
        body = (REPO / "Documentation" / "Inventory" / fname).read_text(encoding="utf-8")
        heads[n] = set(re.findall(r"^#{2,3} (\d+(?:\.\d+)?)[ .]", body, re.M))
    for m in re.finditer(r"doc (0\d) (?:§|section )(\d+(?:\.\d+)?)", text):
        if m.group(2) not in heads.get(m.group(1), set()):
            fail("references", f"'{m.group(0)}' has no such heading in {docs.get(m.group(1))}")


def main() -> int:
    for fn in (check_routes, check_interfaces, check_touchpoints, check_paths, check_references):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a checker that crashes must say so, not pass
            fail(fn.__name__, f"crashed: {type(exc).__name__}: {exc}")
    for n in notes:
        print("note:", n)
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print(" -", p)
        return 1
    print(f"{DOC.relative_to(REPO)}: routes, parameters, enums, interfaces, touchpoints, paths and references all check out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
