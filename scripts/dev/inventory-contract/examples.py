"""Builds the JSON examples of 05-P1-API-CONTRACT.md from the golden fixtures (real responses, trimmed).

Usage: python examples.py  -> prints a dict name -> json text (used by build_contract.py).
Trimming never edits a value: it only drops list items and says so in the surrounding prose.
"""
import copy
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FIX = REPO / "dashboard" / "src" / "pages" / "inventory" / "fixtures"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def golden_value(spec):
    """``<fixture>.<dotted path>`` read from a golden file as text. A numeric segment indexes a list; ``#len`` at the end
    gives the length of a list or object. Integers of four digits or more get thousands separators."""
    name, _, path = spec.partition(".")
    want_len = path.endswith("#len")
    if want_len:
        path = path[: -len("#len")]
    value = load(name)
    for part in [p for p in path.split(".") if p]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    if want_len:
        value = len(value)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"@@G:{spec}@@ is not a plain value")
    return f"{value:,}" if isinstance(value, int) and abs(value) >= 1000 else str(value)


def cut(obj, path, n, keep=None):
    """Keep the first n items (or those where keep(item) is true) of the list at dotted ``path`` ('*' = every element)."""
    parts = path.split(".")

    def walk(o, rest):
        if not rest:
            return
        key, tail = rest[0], rest[1:]
        if key == "*":
            for x in o:
                walk(x, tail)
            return
        if not tail:
            lst = o[key]
            if keep:
                lst[:] = [x for x in lst if keep(x)][:n]
            else:
                lst[:] = lst[:n]
            return
        walk(o[key], tail)

    walk(obj, parts)
    return obj


def pick(items, *names, key="name"):
    by = {x[key]: x for x in items}
    return [by[n] for n in names]


def dump(o, indent=0, width=150):
    """JSON, with every object or array that fits in ``width`` columns on one line (still valid JSON)."""
    flat = json.dumps(o, ensure_ascii=False)
    pad = "  " * indent
    if not isinstance(o, (dict, list)) or not o or len(flat) + len(pad) <= width:
        return flat
    if isinstance(o, list):
        inner = ",\n".join("  " * (indent + 1) + dump(x, indent + 1, width) for x in o)
        return "[\n" + inner + "\n" + pad + "]"
    inner = ",\n".join("  " * (indent + 1) + json.dumps(k, ensure_ascii=False) + ": " + dump(v, indent + 1, width) for k, v in o.items())
    return "{\n" + inner + "\n" + pad + "}"


def build():
    ex = {}
    s = copy.deepcopy(load("summary"))
    cut(s, "os.top", 2); cut(s, "software.top", 2); cut(s, "software.sprawl", 2); cut(s, "hardware.top_models", 2)
    ex["summary"] = dump(s)

    a = copy.deepcopy(load("assets"))
    fresh_server = next(x for x in a["items"] if x["kind"] == "server" and x["freshness"] == "fresh" and "agent" in x["sources"])
    a["items"] = pick(a["items"], "edge-unk-01", "edge-unk-02") + [fresh_server]
    ex["assets"] = dump(a)

    f = copy.deepcopy(load("facets"))
    for name in f["facets"]:
        f["facets"][name]["items"] = f["facets"][name]["items"][:3]
    ex["facets"] = dump(f)

    d = copy.deepcopy(load("asset-server"))
    d["identity"] = d["identity"][:3]
    cut(d, "server.disks", 2); cut(d, "server.software.top", 2); cut(d, "changes.items", 1)
    ex["asset-server"] = dump(d)

    d = copy.deepcopy(load("asset-device-ha"))
    d["identity"] = d["identity"][:3]
    ex["asset-device-ha"] = dump(d)
    d = copy.deepcopy(load("asset-device"))
    d["identity"] = d["identity"][:2]
    cut(d, "device.modules", 3); cut(d, "device.modules.*.items", 2); cut(d, "changes.items", 1)
    ex["asset-device"] = dump(d)

    o = copy.deepcopy(load("os"))
    o["families"] = [x for x in o["families"] if x["family"] in ("fortios", "unknown")]
    cut(o, "families.*.products.*.releases", 2)
    cut(o, "families.*.products.*.releases.*.versions", 2)
    ex["os"] = dump(o)

    h = copy.deepcopy(load("hardware"))
    cut(h, "network.vendors", 2); cut(h, "network.models", 2)
    cut(h, "matrix.releases", 4); cut(h, "matrix.models", 2); cut(h, "matrix.models.*.cells", 2)
    cut(h, "modules", 2)
    cut(h, "servers.cpu_models", 2); cut(h, "servers.memory", 3); cut(h, "servers.disks", 3)
    ex["hardware"] = dump(h)

    c = copy.deepcopy(load("changes"))
    items = c["items"]
    chosen = []
    seen = set()
    for it in items:
        tag = (it["category"], it["action"], it["label"], bool(it["pair"]), it["marker"], it["snapshot_truncated"])
        if tag not in seen and len(chosen) < 5:
            seen.add(tag)
            chosen.append(it)
    c["items"] = chosen[:4]
    c["series"] = c["series"][-2:]
    ex["changes"] = dump(c)

    sw = copy.deepcopy(load("software"))
    sw["items"] = pick(sw["items"], "crowdstrike-falcon-sensor", "google-chrome", "git", key="product_key")
    ex["software"] = dump(sw)
    pubs = copy.deepcopy(load("software-publishers"))
    pubs["items"] = pubs["items"][:3]
    ex["software-publishers"] = dump(pubs)
    ex["software-product"] = dump(load("software-product"))
    inst = copy.deepcopy(load("software-installations"))
    inst["items"] = inst["items"][:2]
    ex["software-installations"] = dump(inst)
    tr = copy.deepcopy(load("trends"))
    for series in tr["series"]:
        series["points"] = series["points"][-3:]
    ex["trends"] = dump(tr)
    ex["settings"] = dump(load("settings"))
    return ex


if __name__ == "__main__":
    ex = build()
    for k, v in ex.items():
        print("=====", k, len(v))
    if len(sys.argv) > 1:
        print(ex[sys.argv[1]])
