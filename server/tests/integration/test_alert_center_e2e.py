"""Alert Center end-to-end contract (Alerts review A01–A27) against a real API.

Gated: set ZENPLUS_ALERTS_E2E_URL to a dev API (e.g. http://127.0.0.1:8093)
whose DATABASE_URL is a DISPOSABLE database built with
run-migrations.py --include-init and then loaded with
alert_center/seed.sql (replace __PASSWORD_HASH__ with
hash_password('Test12345'); set the admin password to admin123).
Never point this at production — it acknowledges, resolves and snoozes.

The read test must run before the action test (pytest keeps file order).
"""
import os
import urllib.error
import urllib.request

import pytest

if not os.environ.get("ZENPLUS_ALERTS_E2E_URL"):
    pytest.skip("Requires ZENPLUS_ALERTS_E2E_URL (disposable seeded API)", allow_module_level=True)

import json
BASE=os.environ.get("ZENPLUS_ALERTS_E2E_URL", "").rstrip("/") + "/api/v1"
_tok={}
def tok(u):
    if u not in _tok:
        pw = "admin123" if u=="admin" else "Test12345"
        r=urllib.request.urlopen(urllib.request.Request(BASE+"/auth/login",data=json.dumps({"username":u,"password":pw}).encode(),headers={"Content-Type":"application/json"}))
        _tok[u]=json.load(r)["access_token"]
    return _tok[u]
def call(u,m,p,body=None,raw=False):
    req=urllib.request.Request(BASE+p,method=m,data=json.dumps(body).encode() if body is not None else None,headers={"Authorization":"Bearer "+tok(u),"Content-Type":"application/json"})
    try:
        r=urllib.request.urlopen(req); data=r.read(); code=r.status
    except urllib.error.HTTPError as e:
        data=e.read(); code=e.code
    if raw: return code, data
    try: return code, json.loads(data)
    except Exception: return code, data[:300]
fails=0
def check(name, cond, info=""):
    global fails
    print(("PASS " if cond else "FAIL ")+name+("" if cond else f"  -> {info}"))
    if not cond: fails+=1


def test_reads_and_populations():
    global fails
    fails = 0
    c,s=call("admin","GET","/alerts/summary")
    check("summary 200", c==200, s)
    print(json.dumps({k:s[k] for k in ("open","window","diagnostics","filtered")}, default=str)[:900])
    c,t=call("admin","GET","/alerts/trend?hours=168")
    check("trend 200", c==200 and len(t["series"])==7, (c, str(t)[:300]))
    print(t["series"][-2:], t["top_rules"][:2], t["top_entities"][:2])
    c,st=call("admin","GET","/alerts/stats"); check("stats", c==200 and st["active"]>1000, st); print(st)
    # search
    for q,exp in (("10.9.0.11","server"),("WEB-01","server"),("Portal","service_check"),("T Device Down",None),("branch-sensor","sensor"),("Login journey","apm")):
        c,d=call("admin","GET","/alerts?view=open&limit=5&search="+urllib.request.quote(q))
        kinds={a["entity"]["kind"] for a in d["data"]}
        check(f"search {q!r}", c==200 and d["meta"]["total"]>0 and (exp is None or exp in kinds), (c, d.get("meta") if isinstance(d,dict) else d, kinds))
    c,d=call("admin","GET","/alerts?view=open&search=100%25&limit=5"); check("search % literal", c==200 and d["meta"]["total"]==0, d.get("meta"))
    # id prefix
    c,d=call("admin","GET","/alerts?view=active&limit=1"); aid=d["data"][0]["id"]
    c,d2=call("admin","GET","/alerts?view=all&search="+aid[:8]); check("search id prefix", c==200 and any(a["id"]==aid for a in d2["data"]), d2.get("meta"))
    # pagination reach oldest
    c,first=call("admin","GET","/alerts?view=active&limit=1"); tot=first["meta"]["total"]; tail_n=tot % 50 or 50
    c,last=call("admin","GET",f"/alerts?view=active&sort=newest&skip={tot-tail_n}&limit=50"); check("page to the end", c==200 and len(last["data"])==tail_n, (len(last["data"]), tail_n))
    # resolved basis
    c,d=call("admin","GET","/alerts?view=resolved&basis=resolved&hours=1&search=OLD%20incident"); check("old incident closed today in resolved-basis window", d["meta"]["total"]==1, d["meta"])
    c,d=call("admin","GET","/alerts?view=resolved&basis=triggered&hours=1&search=OLD%20incident"); check("excluded on triggered basis", d["meta"]["total"]==0, d["meta"])
    c,d=call("admin","GET","/alerts?view=resolved&hours=24&population=notices&limit=1"); check("notices population", c==200 and d["meta"]["total"]>0 and d["data"][0]["is_notice"], d.get("meta"))
    c,d=call("admin","GET","/alerts?view=resolved&hours=24&limit=200"); check("incidents default excludes notices", all(not a["is_notice"] for a in d["data"]), "")
    # legacy
    c,d=call("admin","GET","/alerts?status=active&limit=6"); check("legacy status=active keeps suppressed rows", c==200 and st["active"] <= d["meta"]["total"] <= st["active"]+st["suppressed"], (d.get("meta"), st))
    c,d=call("admin","GET","/alerts?limit=6"); c2,inc=call("admin","GET","/alerts?view=all&limit=1")
    check("legacy unfiltered incl notices", c==200 and d["meta"]["total"]>inc["meta"]["total"], (d.get("meta"), inc.get("meta")))
    # validation
    for p in ("/alerts?view=bogus","/alerts?sort=x","/alerts?severity=bad","/alerts?from=2026-09-01T00:00:00"):
        c,_=call("admin","GET",p); check(f"422 {p}", c==422, c)
    # server alert detail
    c,d=call("admin","GET","/alerts?view=open&entity_kind=server&limit=5")
    check("entity_kind server filter", d["meta"]["total"]==3, d["meta"])
    hm=[a for a in d["data"] if a["rule_name"]=="Host CPU high"][0]
    c,det=call("admin","GET","/alerts/"+hm["id"])
    check("server detail entity", det["entity"]["kind"]=="server" and det["entity"]["href"].startswith("/servers/"), det["entity"])
    check("server alert rule + channels linked (A10)", det["rule"] and det["rule"]["name"]=="Host CPU high" and det["channels"], (det["rule"] and det["rule"]["name"], det["channels"]))
    check("related all same server or same rule (A03)", all(r["server_id"]==det["server_id"] or r["relation"]=="same_rule_window" for r in det["related"]), [(r["relation"], r["server_id"]) for r in det["related"]])
    print("rule scope:", det["rule"]["scope"], det["rule"]["affected"], "events:", [e["kind"] for e in det["events"]], "evidence:", det["evidence"][:3])
    c,exp=call("admin","GET","/alerts/export?view=active&severity=warning",raw=True)
    lines=exp.decode().strip().splitlines(); check("export csv full population", c==200 and len(lines)-1==s["open"]["by_severity"]["warning"], len(lines))
    assert fails == 0, f"{fails} read checks failed"


def test_scope_permissions_lifecycle_and_suppression():
    global fails
    fails = 0
    # --- scope: webop sees only web-team entities
    c, d = call("webop", "GET", "/alerts?view=open&limit=200"); tot = d["meta"]["total"]
    kinds = {a["entity"]["kind"] for a in d["data"]}
    check("scoped list limited", c == 200 and 0 < tot < 1306, tot); print("webop open:", tot, kinds)
    c, all_ = call("admin", "GET", "/alerts?view=active&limit=200&sort=oldest")
    vis = {a["id"] for a in d["data"]}
    out = [a for a in all_["data"] if a["id"] not in vis and a["entity"]["kind"] == "device"]
    oos = out[0]["id"]
    for m, p, b in (("GET", f"/alerts/{oos}", None), ("POST", f"/alerts/{oos}/acknowledge", {}),
                    ("POST", f"/alerts/{oos}/resolve", {}), ("POST", f"/alerts/{oos}/snooze", {"minutes": 60}),
                    ("POST", f"/alerts/{oos}/unsnooze", None), ("POST", f"/alerts/{oos}/assign", {"user_id": None}),
                    ("POST", f"/alerts/{oos}/comments", {"text": "x"}), ("GET", f"/alerts/{oos}/snooze-preview", None)):
        c, r = call("webop", m, p, b); check(f"A02 out-of-scope {m} {p.split('/')[-1]} -> 404", c == 404, (c, r))
    c, r = call("admin", "GET", f"/alerts/{oos}"); check("admin sees it still active", r["status"] == "active", r.get("status"))
    mine_list = [a for a in d["data"] if a["entity"]["kind"] == "device" and a["status"] == "active"]
    c, b = call("webop", "POST", "/alerts/bulk", {"ids": [oos, mine_list[0]["id"]], "action": "acknowledge"})
    check("bulk mixed: out-of-scope not found, in-scope ok", c == 200 and b["results"][0]["error"] == "Not found" and b["results"][1]["ok"], b)
    mine = mine_list[1]["id"]
    c, det = call("webop", "GET", f"/alerts/{mine}")
    check("A03 related within scope", all(r["id"] in vis or r["status"] == "resolved" for r in det["related"]), [(r["relation"], r["entity"]["name"]) for r in det["related"]])
    check("scoped: fleet rule not editable", det["rule"] and det["rule"]["editable"] is False, det["rule"] and det["rule"]["editable"])
    # --- permissions
    c, r = call("ro1", "POST", f"/alerts/{mine}/acknowledge", {}); check("read_only cannot ack (403)", c == 403, c)
    c, r = call("viewer1", "GET", f"/alerts/{mine}"); check("viewer reads", c == 200, c)
    c, r = call("viewer1", "POST", f"/alerts/{mine}/snooze", {"minutes": None, "reason": "because"}); check("viewer indefinite snooze 403", c == 403, (c, r))
    # --- lifecycle
    tgt = all_["data"][5]["id"]
    c, r = call("admin", "POST", f"/alerts/{tgt}/acknowledge", {"note": "on it"})
    check("ack ok + auto-owner", c == 200 and r["status"] == "acknowledged" and r["changed"] and r["assigned_to"], (c, r.get("status"), r.get("assigned_to")))
    first_ack = r["acknowledged_at"]
    c, r = call("netop", "POST", f"/alerts/{tgt}/acknowledge", {})
    check("repeat ack idempotent keeps first actor", c == 200 and not r["changed"] and r["acknowledged_at"] == first_ack and r["acknowledged_by"]["name"] != "Net Operator", (c, r.get("acknowledged_by")))
    c, r = call("admin", "POST", f"/alerts/{tgt}/resolve", {"reason": "fixed cable"})
    check("resolve ok w/ closure manual", c == 200 and r["closure"]["reason"] == "manual", r.get("closure"))
    t1 = r["resolved_at"]
    c, r = call("admin", "POST", f"/alerts/{tgt}/resolve", {}); check("repeat resolve keeps time", c == 200 and not r["changed"] and r["resolved_at"] == t1, (c, r.get("resolved_at"), t1))
    c, r = call("admin", "POST", f"/alerts/{tgt}/acknowledge", {}); check("ack resolved -> 409", c == 409, (c, r))
    c, det = call("admin", "GET", f"/alerts/{tgt}")
    print("events:", [(e["kind"], e["source"], (e["actor"] or {}).get("name"), e["reason"]) for e in det["events"]])
    check("A23 timeline has actors+reason", any(e["kind"] == "resolved" and e["reason"] == "fixed cable" for e in det["events"]), det["events"])
    # --- snooze semantics
    sn = all_["data"][7]; sid = sn["id"]
    for mins in (-5, 0, 2, 50000):
        c, r = call("admin", "POST", f"/alerts/{sid}/snooze", {"minutes": mins}); check(f"A08 snooze minutes={mins} -> 422", c == 422, (c, r))
    c, r = call("admin", "POST", f"/alerts/{sid}/snooze", {"minutes": None}); check("indefinite without reason 422", c == 422, (c, r))
    c, pv = call("admin", "GET", f"/alerts/{sid}/snooze-preview"); check("snooze preview", c == 200 and pv["open_alerts"] >= 1 and pv["snoozable"], pv)
    c, r = call("admin", "POST", f"/alerts/{sid}/snooze", {"minutes": 60, "reason": "maintenance window"}); check("snooze ok", c == 200 and r["snoozed"], (c, r))
    c, r = call("admin", "GET", f"/alerts/{sid}")
    check("A07 snooze does not resolve", r["status"] == "active" and r["suppression"] and r["closure"] is None, (r["status"], r["suppression"]))
    c, v = call("admin", "GET", f"/alerts?view=suppressed&limit=5&search={sid}"); check("suppressed view lists it", any(a["id"] == sid for a in v["data"]), v["meta"])
    c, v = call("admin", "GET", "/alerts?view=active&limit=200&sort=oldest"); check("active view excludes it", all(a["id"] != sid for a in v["data"]), "")
    c, st = call("admin", "GET", "/alerts/stats"); check("stats suppressed count", st["suppressed"] >= 1, st)
    c, sl = call("admin", "GET", "/alerts/silences"); check("silences workspace", c == 200 and any(s["reason"] == "maintenance window" for s in sl["items"]), sl)
    c, slw = call("webop", "GET", "/alerts/silences")
    sil = [s for s in sl["items"] if s["reason"] == "maintenance window"][0]
    visible_to_webop = sil["id"] in {s["id"] for s in slw["items"]}
    c, r = call("webop", "DELETE", f"/alerts/silences/{sil['id']}")
    check("A04 scoped silence list/delete", (not visible_to_webop and c == 404) or (visible_to_webop and c == 200), (visible_to_webop, c))
    c, r = call("admin", "POST", f"/alerts/{sid}/unsnooze"); check("unsnooze", c == 200 and r["removed"] == 1, r)
    c, r = call("admin", "GET", f"/alerts/{sid}"); check("suppression cleared", r["suppression"] is None and r["status"] == "active", r["suppression"])
    print("events:", [e["kind"] for e in r["events"]])
    # --- assign / comment
    c, us = call("admin", "GET", "/alerts/assignees"); net = [u for u in us["items"] if u["username"] == "netop"][0]
    c, r = call("admin", "POST", f"/alerts/{sid}/assign", {"user_id": net["id"], "note": "handover"}); check("assign", c == 200 and r["assigned_to"]["name"] == "Net Operator", r.get("assigned_to"))
    c, r = call("admin", "POST", f"/alerts/{sid}/comments", {"text": "Checked the uplink"}); check("comment", c == 200, c)
    c, v = call("netop", "GET", "/alerts?view=open&assignee=me"); check("assignee=me filter", c == 200 and v["meta"]["total"] >= 1, v.get("meta"))
    # --- bulk
    ids = [a["id"] for a in all_["data"][10:15]]
    c, b = call("admin", "POST", "/alerts/bulk", {"ids": ids, "action": "resolve", "reason": "bulk cleanup"}); check("bulk resolve", c == 200 and b["summary"]["changed"] == 5, b["summary"])
    c, b = call("admin", "POST", "/alerts/bulk", {"ids": ids, "action": "acknowledge"}); check("bulk ack on resolved -> per-item 409", c == 200 and b["summary"]["failed"] == 5, b)
    c, b = call("admin", "POST", "/alerts/bulk", {"ids": [a["id"] for a in all_["data"][20:23]], "action": "snooze", "minutes": 30}); check("bulk snooze", c == 200 and b["summary"]["ok"] == 3, b)
    # --- summary with filter
    c, s = call("admin", "GET", "/alerts/summary?severity=critical")
    check("summary filtered label", s["filtered"] and "Severity: critical" in s["filters"], s["filters"])
    assert fails == 0, f"{fails} action checks failed"
