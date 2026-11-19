"""python3 test_queues.py: headings, collapse, sort modes' labels, colours, marks and indent, git state, badges (no herdr needed)."""
import importlib.util, os, subprocess, sys, tempfile
try:
    from tomllib import loads as toml_check
except ImportError:  # Python < 3.11 (macOS's own python3 is 3.9): the rows TOML goes unchecked
    toml_check = str

sys.argv = ["x"]
spec = importlib.util.spec_from_file_location("q", os.path.join(os.path.dirname(__file__), "queues.py"))
q = importlib.util.module_from_spec(spec); spec.loader.exec_module(q)
A = lambda p, ws, st: {"pane_id": p, "workspace": ws, "workspace_id": ws, "agent_status": st, "host": "micro",
                       "tokens": {"machine_icon": "🐭"}, "agent": "claude", "terminal_id": "t" + p}
ags = [A("p1", "a", "working"), A("p2", "b", "blocked"), A("p3", "c", "idle")]
st = {"queues": ["priority", "work"], "members": {}, "icons": {"priority": "🔥"}, "sort": "queues", "headings": "queue", "colors": {"priority": None, "work": None}}  # no colour (0.30 defaults: below)
for a, qn in zip(ags, ["priority", "priority", "work"]):
    q.put(st, a, qn)
tok = lambda st: {r[1]["pane_id"]: r[1]["tokens"] for r in q.hiding(st, ags) if r[0] == "pane.report_metadata"}
t = tok(st)
assert t["p1"]["queues_heading"] == "▾ 🔥 priority 2" and t["p2"]["queues_heading"] is None and t["p3"]["queues_heading"] == "▾ work 1"
st["collapsed"] = ["priority"]; t = tok(st)  # the blocked one carries the collapsed queue
assert t["p1"]["queues_inactive"] == "1" and t["p2"]["queues_inactive"] is None and t["p2"]["queues_heading"] == "▸ 🔥 priority 2"
assert q.hiding(st, ags)[-1][0] == "agent.view.set"
st["sort"] = "workspaces"; t = tok(st)  # off: plain names, no headings
assert t["p2"]["queues_heading"] is None and t["p2"]["queues_label"] == "b"
st.update(sort="auto", headings="machine", collapsed=[]); t = tok(st)
assert t["p1"]["queues_heading"] == "🐭 micro" and t["p2"]["queues_heading"] is None
assert q.setting(st, "color", ["priority", "#ff5555"]) and not q.setting(st, "color", ["priority", "red"])
r = q.rows_toml(st); toml_check(r); assert "$queues_heading_after" not in r and "state_icon" in r
st.update(mark="own", headings="queue", sort="queues", spinner=True); r = q.rows_toml(st); toml_check(r)
assert "$queues_heading_after" not in r and "#f8f8f2" in r and "state_icon" not in r and q.WJ not in r  # escaped, not invisible
t = tok(st)  # own marks: every heading flush left on its group's first agent; names uncoloured under headings
P, W = q.PAD, q.WJ
assert t["p1"]["queues_heading"] == "▾ 🔥 priority 2" + W  # red = palette slot 1
assert t["p1"]["queues_label"] == q.SPIN[0] + "  🔥🐭 a" + q.marker("yellow")  # herdr indents it two; working: the spinner's first frame
assert t["p2"]["queues_label"] == P * 2 + "🔴 🔥🐭 b" + q.marker("red") and t["p2"]["queues_heading"] is None
assert t["p3"]["queues_label"] == P * 2 + " work🐭 c" and t["p3"]["queues_heading"] == "▾ work 1"
assert all(t[p]["queues_heading_after"] is None for p in t)
assert not q.setting(st, "host", ["micro", "icon", "🦊"]); t = tok(st); assert "🦊" in t["p1"]["queues_label"]
d = tempfile.mkdtemp(); subprocess.run(["git", "-C", d, "init", "-q"])
assert q.git_of(d) == "clean"; open(d + "/f", "w").write("x"); assert q.git_of(d) == "dirty"; assert q.git_of("/") is None
st["badges"] = {"gitstatus": {"dirty": "±"}}; assert q.info({"cwd": d}, st)["gitstatus"] == "±"
a = A("p1", "a", "idle"); a["tokens"]["usage"] = "62"; assert q.label(a, None, "{workspace} {usage_pct}") == "a 62%"
# a newer machine's labels are left to it; unknown or older versions are not
v = q.VERSION.split("."); up = ".".join(v[:-1] + [str(int(v[-1]) + 1)])
assert q.newer({"versions": {"x": up}}, "x") and not q.newer({"versions": {"x": q.VERSION}}, "x")
assert not q.newer({"versions": {"x": "0.1.0"}}, "x") and not q.newer({}, "x") and not q.newer({"versions": {"x": "?"}}, "x")
assert q.newer({"versions": {"x": "1.0.0"}}, "x")
# lanes: by the agent's keys; shown after the workspace, in old formats too; empty clears
st = {}; a = A("p1", "a", "idle"); b = A("p2", "a", "idle")
q.set_lane(st, a, "urgent"); q.set_lane(st, b, "chores")
assert q.lane(st, a) == "urgent" and q.lane(st, b) == "chores"
assert q.label(a, None, "{workspace}", lane="urgent") == "[urgent] a" and q.label(a, None, "{workspace}") == "a"
assert q.label(a, None, "{lane} {workspace}", lane="urgent") == "[urgent] a" and q.label(a, None, "{lane} {workspace}") == "a"
assert q.label(a, None, "{agent}", lane="x").endswith(" [x]")
q.set_lane(st, a, ""); assert q.lane(st, a) == "" and q.lane(st, b) == "chores"
# heading entries: text, and fake agents filtered
st2 = {"queues": ["priority"], "icons": {"priority": "🔥"}}
assert q.heading_text(st2, "priority", {"active": 3, "hidden": 2}, False) == "▾ 🔥 priority 3"  # default: active
assert q.heading_text(st2, "priority", None, True) == "▸ 🔥 priority 0"
st2["heading"] = "{arrow} {name} {waiting}/{active} +{hidden}"
assert q.heading_text(st2, "priority", {"active": 3, "waiting": 1, "hidden": 3}, True) == "▸ priority 1/3 +3"
st2["heading"] = "{arrow} {name} {nope"  # broken: the default, not nothing
assert q.heading_text(st2, "priority", {"active": 1}, False) == "▾ 🔥 priority 1"
assert q.setting(st2, "heading", ["{arrow"]) and q.setting(st2, "heading", ["default"]) is None and "heading" not in st2
h = A("p9", "h", "idle"); h["tokens"]["queues_heading_for"] = "priority"
assert q.real([h, A("p1", "a", "idle")]) == [A("p1", "a", "idle")]
# only the token reports that change something are sent
ag = [{"pane_id": "p1", "tokens": {"queues_label": "x", "queues_heading": "h"}}]
rq = lambda tk: ["pane.report_metadata", {"pane_id": "p1", "source": "s", "tokens": tk}]
assert q.changed([rq({"queues_label": "x", "queues_heading_after": None}), ["agent.view.set", {}]], ag) == [["agent.view.set", {}]]
assert len(q.changed([rq({"queues_heading": None})], ag)) == 1 and len(q.changed([rq({"queues_label": "y"})], ag)) == 1
# heading entries (0.22): one list for every machine, sorted by queues_rank; each server shows its own headings
st3 = {"queues": ["priority", "work"], "members": {}, "sort": "queues", "headings": "queue", "entries": True}
b1, b2, b3 = A("p1", "a", "idle"), A("p2", "b", "blocked"), A("p3", "c", "idle")
b2["host"] = "beast-arch"
for a, qn in zip([b1, b2, b3], ["work", "priority", "priority"]):
    q.put(st3, a, qn)
ranks = sorted([(q.rank_of(st3, a), a["pane_id"]) for a in (b1, b2, b3)] + [(q.rank_of(st3, q=g), g) for g in q.groups_of(st3)])
assert [p for _, p in ranks] == ["priority", "p2", "p3", "work", "p1", "unsorted"], ranks
st3["sort"] = "auto"; q.put(st3, b3, "priority")
assert q.rank_of(st3, b2) < q.rank_of(st3, b3)  # auto: blocked first
st3["rank"] = {"priority": [q.keys(b3)[0]]}; assert q.rank_of(st3, b2) < q.rank_of(st3, b3)  # even ahead of J/K order
st3["sort"] = "queues"; assert q.rank_of(st3, b3) < q.rank_of(st3, b2); st3.pop("rank")
v = q.view(st3, "micro"); assert v["sort"][0]["field"] == {"token": "queues_rank"} and "micro" in str(v["filter"])
assert q.view(dict(st3, entries=False), "micro") == q.VIEW
st3["collapsed"] = ["priority"]
t3 = {r[1]["pane_id"]: r[1]["tokens"] for r in q.hiding(st3, [b1, b2, b3]) if r[0] == "pane.report_metadata"}
assert t3["p2"]["queues_inactive"] == "1" and t3["p3"]["queues_inactive"] == "1" and t3["p1"]["queues_inactive"] is None
assert q.hiding(st3, [b1], "carbon")[-1][1] == q.view(st3, "carbon")
# finer marks (0.25): question vs job done, seen, background work
D = lambda st, **tk: {"pane_id": "p", "agent_status": st, "state_change_seq": 7, "tokens": tk}
assert q.need(D("done")) == "done" and q.need(D("done", queues_seen="7")) == "done_seen" and q.need(D("done", queues_seen="6")) == "done"
assert q.need(D("done", queues_asks="1")) == "asks" and q.need(D("idle", queues_asks="1")) == "asks_seen"
assert q.need(D("done", queues_bg="1")) == "background" and q.need(D("idle")) == "idle" and q.need(D("working")) == "working"
assert q.mark_of(D("done", queues_seen="7"), False) == "✓ " and q.mark_of(D("working"), True) == q.SPIN[0] + " "
assert q.asks_text("Done.\n\nShall I push it?") and not q.asks_text("Is it? Yes.\n\nAll pushed.")
assert q.BG.search("-- INSERT -- ⏵⏵ auto mode on · 1 shell · ← 2 agents") and not q.BG.search("· ← 2 agents")
st4 = {}
assert q.setting(st4, "status", ["bogus", "x"]) and not q.setting(st4, "status", ["done", "✓"]) and q.marks(st4)["done"] == "✓"
assert not q.setting(st4, "status", ["spin", "a", "b"]) and q.spin_frames(st4) == ["a", "b"]
assert not q.setting(st4, "status", ["working", "color", "none"]) and q.mark_colour(st4, "working") is None
assert not q.setting(st4, "status", ["done", "default"]) and q.marks(st4)["done"] == "✔"
print("ok")
# a request without a question mark is still an ask (confluence-consist 2026-11-16); a report is not
assert q.asks_text("Done.\n\nReply with pass or fail for each check.") and q.asks_text("Steps.\n\nThen run `$OPS/x.sh` and tell me what it prints.")
assert q.asks_text("All set.\n\nPlease run:\n\n    $OPS/a.sh") and q.asks_text("Fix ready.\n\nRun:\n\n```\n$OPS/a.sh\n```")
assert not q.asks_text("Pushed `abc123`.\n\nThe tests pass and the run finished.") and not q.asks_text("Ran the suite; it passed.")
# auto order (Ben 2026-11-16): blocked, unseen done, working, idle; a seen done is idle
assert q.NEED["blocked"] < q.NEED["done"] < q.NEED["working"] < q.NEED["idle"] == q.NEED["done_seen"] == q.NEED["asks_seen"]
print("ok")

# The Goto guard (0.29.0): Return only when the label did not land in the pane.
assert not q.typed_into_pane("micro", "$ ls\n", "$ ls\n")
assert q.typed_into_pane("micro", "$ ls\n", "$ ls\n$ a/micro")
assert q.typed_into_pane("micro", None, "x") and q.typed_into_pane("micro", "x", None)
# Key names per tool.
sent = []
q.tool, real_tool = (lambda *c, env=None: sent.append(c) or ""), q.tool
q.press("gnome", "ctrl+b", "Return", "shift+g")
q.press("sway", "ctrl+u")
assert sent == [("ydotool", "key", "29:1", "48:1", "48:0", "29:0"), ("ydotool", "key", "28:1", "28:0"), ("ydotool", "key", "42:1", "34:1", "34:0", "42:0"),
                ("wtype", "-M", "ctrl", "-k", "u", "-m", "ctrl")], sent
q.tool = real_tool
print("ok")
import os, tempfile
if sys.version_info < (3, 11):  # no tomllib (macOS's own python3 is 3.9): no Goto keys, so no blind keys
    assert q.goto_keys() is None
else:
    with tempfile.TemporaryDirectory() as t:
        os.environ["APPDATA" if q.WIN else "XDG_CONFIG_HOME"] = t  # where goto_keys reads herdr's config
        assert q.goto_keys() == ["ctrl+b", "g"]  # no config: herdr's defaults
        os.makedirs(f"{t}/herdr")
        open(f"{t}/herdr/config.toml", "w").write('[keys]\nprefix = "ctrl+a"\ngoto = ["prefix+a", "prefix+g"]\n')
        assert q.goto_keys() == ["ctrl+a", "a"]
        open(f"{t}/herdr/config.toml", "w").write('[keys\n')
        assert q.goto_keys() is None
print("ok")

# 0.31.0: membership survives a restart into a new pane followed by /clear (micro, 2026-11-16: lost to unsorted)
S = lambda sid, t, cwd: {"pane_id": "w1:p1", "host": "micro", "agent_session": {"value": sid} if sid else None,
                         "terminal_id": t, "cwd": cwd}
sm = {"queues": ["priority", "work"], "members": {"micro/t:term_old": "priority"}}  # filed before 0.31: terminal only
old, new = S(None, "term_old", "/p/station"), S("s2", "term_new", "/p/station")
assert q.queue_of(sm, new) == q.UNSORTED  # the 0.30 failure
assert q.learn(sm, [old]) and not q.learn(sm, [old])  # backfills the folder key once, then nothing to save
assert q.queue_of(sm, new) == "priority" and q.learn(sm, [new]) and sm["members"]["s:s2"] == "priority"
# two agents in one folder, different queues: each keeps its own, and learn settles (no save every pass)
other = S("s3", "term_3", "/p/station"); q.put(sm, other, "work")
assert q.queue_of(sm, new) == "priority" and q.queue_of(sm, other) == "work" and not q.learn(sm, [new, other])
assert q.folder(other) not in [q.keys(new)] and "micro/d:/p/station" not in q.keys(other)  # quiet/inactive/rank: not shared
assert q.queue_colour({"queues": ["priority", "work"]}, "priority") == "red"  # 0.30 default colours
assert q.queue_colour({"queues": ["priority"], "colors": {"priority": None}}, "priority") is None
print("ok")

# 0.32.0 (micro's multi-host review): collapsed counts, real hostnames, version skew
sc = {"queues": ["priority"], "members": {"micro/t:a": "priority"}, "collapsed": ["unsorted", "priority"]}
la = [{"pane_id": "p1", "host": "micro", "terminal_id": "a"}, {"pane_id": "p2", "host": "micro", "terminal_id": "b"},
      {"pane_id": "p3", "host": "micro", "terminal_id": "c"}]
sc["inactive"] = q.keys(la[2])
la[0].update(agent_status="blocked")
assert q.head_counts(sc, la) == {"priority": {"active": 1, "waiting": 1, "hidden": 1},
                                 "unsorted": {"active": 1, "waiting": 0, "hidden": 1}}  # inactive ones aren't counted
assert q.heading_text(sc, "unsorted", {"active": 2, "hidden": 2}, True) == "▸ unsorted 2"
import json as _j, tempfile as _t
q.HOSTS = os.path.join(_t.mkdtemp(), "hosts.json")
q.herdr = lambda m, *a, **k: "id\tdesk\tben@100.64.0.5\tdefault\tenabled\nid\tcarbon\tcarbon\tdefault\tenabled\n"
assert q.machines()["desk"][0] == "100"  # the old guess, until learned
q.hostname_of = lambda t: {"ben@100.64.0.5": "beast-arch"}.get(t)  # carbon unreachable: not cached, guessed
ms = q.machines(learn=True)
assert ms["desk"] == ("beast-arch", "ben@100.64.0.5") and ms["carbon"][0] == "carbon"
assert _j.load(open(q.HOSTS)) == {"ben@100.64.0.5": "beast-arch"} and q.machines()["desk"][0] == "beast-arch"
sv = {"versions": {q.HOST: q.VERSION, "beast-arch": q.VERSION}}
assert q.skew(sv, ms) == "" or "carbon ?" in q.skew(sv, ms)
assert "carbon ?" in q.skew(sv, ms) and "beast-arch" not in q.skew(sv, ms)
sv["versions"]["carbon"] = "0.31.0"; assert "carbon 0.31.0" in q.skew(sv, ms)
sv["versions"]["carbon"] = q.VERSION; assert q.skew(sv, ms) == ""
print("ok")

# 0.33.0: the other machines' agents for this machine's headings, from the watcher's cache
import time as _tm
q.OTHERS = os.path.join(_t.mkdtemp(), "others.json")
assert q.others() == []  # no cache yet
_j.dump({"at": _tm.time(), "agents": [{"host": "carbon", "pane_id": "p9"}, {"host": q.HOST, "pane_id": "p1"}]}, open(q.OTHERS, "w"))
assert [a["host"] for a in q.others()] == ["carbon"]  # this machine's own come from herdr, not the cache
_j.dump({"at": _tm.time() - 600, "agents": [{"host": "carbon", "pane_id": "p9"}]}, open(q.OTHERS, "w"))
assert q.others() == []  # stale: the watcher stopped
print("ok")

# Windows / WSL (fleet 37): SendKeys text for the jump's keys
assert q.sendkeys(names=["ctrl+b", "a", "slash"]) == "^ba/" and q.sendkeys(names=["Return"]) == "{ENTER}"
assert q.sendkeys("a+b (x)~") == "a{+}b {(}x{)}{~}"

# Windows titles herdr's window with the upper-case computer name (fleet 37, 2026-11-17)
_w, q.windows = q.windows, lambda d=None: [("1", "WIN11VM: System32", True), ("2", "herdr: beast-arch: x", False), ("3", "Notes", False)]
assert q.herdr_windows({"win11vm", "beast-arch"}) == [("1", "win11vm"), ("2", "beast-arch")]
q.windows = _w
st = {}                                                                   # per-host spinner speed
assert q.setting(st, "host", [q.HOST, "spin", "1"]) is None and q.spin_rate(st) == 1.0
assert q.setting(st, "host", [q.HOST, "spin", "off"]) is None and q.spin_rate(st) == 0.0
assert q.setting(st, "host", [q.HOST, "spin", "fast"]) and q.setting(st, "host", [q.HOST, "spin", "0"])  # refused
assert q.setting(st, "host", [q.HOST, "spin", "default"]) is None and q.spin_rate(st) == 1.0
assert q.setting(st, "host", ["other", "spin", "0.5"]) is None and q.spin_rate(st) == 1.0  # not this host's
print("ok")

# search (/): every word, case-blind, in machine / lane / queue / workspace / title / folder
_s = {"queues": ["work"], "members": {"s:x": "work"}, "lanes": {"s:x": "chores"}}
_a = {"machine": "carbon", "workspace": "api-server", "terminal_title_stripped": "Fix login", "cwd": "/home/u/api",
      "agent_session": {"value": "x"}, "pane_id": "w1:p1"}
assert q.matches(_s, _a, "API carbon")
assert q.matches(_s, _a, "chores work login")
assert not q.matches(_s, _a, "api docs")
assert q.matches(_s, _a, "")
print("ok")

# a non-claude agent's unseen idle is done (pi goes straight from working to idle); claude's idle and a seen one stay idle
_r = q.real([{"pane_id": "a", "agent": "pi", "agent_status": "idle", "state_change_seq": 22, "tokens": {"queues_seen": "4"}},
             {"pane_id": "b", "agent": "pi", "agent_status": "idle", "state_change_seq": 22, "tokens": {"queues_seen": "22"}},
             {"pane_id": "c", "agent": "claude", "agent_status": "idle", "state_change_seq": 9, "tokens": {}}])
assert [a["agent_status"] for a in _r] == ["done", "idle", "idle"], _r
print("ok")

# a draft: text in the box between the last two rules; a dim (SGR 2) suggestion is not one
_rule = "─" * 20
for _box, _want in (("❯\xa0", False), ("❯\xa0\x1b[2mTry \"fix lint\"\x1b[0m", False), ("❯\xa0\x1b[38;2;1;2;3m/loop x\x1b[0m", True)):
    q.herdr = lambda *a, _b=_box, **k: "\n".join(["out", _rule, _b, _rule, "  INSERT"])
    assert q.draft("w1:p1") is _want, _box
_a = {"agent_status": "done", "tokens": {"queues_draft": "1", "queues_asks": "1"}}
assert q.need(_a) == "draft"
print("ok")

# flags (🔖 revisit, 📌 pin, 🪫 low context): set by session key, own glyphs, at the label's end, searchable
_s = {"queues": [], "members": {}, "low_context": 60}
_a = {"agent_session": {"value": "z"}, "pane_id": "w1:p1", "workspace": "api", "tokens": {"usage": "45"}}
assert q.context_left(_a) == 55 and q.low_context(_s, _a) and not q.low_context({"low_context": 50}, _a)
q.set_flag(_s, _a, "revisit", True); q.set_flag(_s, _a, "pin", True)
assert [n for n, _, _ in q.flags_of(_s, _a)] == ["revisit", "pin", "lowctx"]
assert q.matches(_s, _a, "revisit") and q.matches(_s, _a, "pin")
assert q.setting(_s, "status", ["pin", "⭐"]) is None and q.glyph(_s, "pin") == "⭐"
assert q.label(_a, "", "{workspace}", flags=" ".join(g for _, g, _ in q.flags_of(_s, _a))) == "api 🔖 ⭐ 🪫"
q.set_flag(_s, _a, "revisit", False); assert not q.revisit(_s, _a) and q.flagged(_s, _a, "pin")
assert q.setting(_s, "context", ["off"]) is None and _s["low_context"] is None
print("ok")

# mark_seen: a stopped agent is marked seen on its own server, local or by ssh; a working one is left alone
_calls = []
_call, _ret = q.call, q.retoken
q.call = lambda target, reqs: _calls.append((target, reqs))
q.retoken = lambda *a: True
try:
    _a = {"agent_status": "idle", "state_change_seq": 22, "pane_id": "w2:p4", "machine": "media-center", "tokens": {}}
    assert q.mark_seen({}, [_a], _a, {"media-center": ("media-center", "media-center")})
    assert _calls[-1][0] == "media-center" and _calls[-1][1][0][1]["tokens"] == {"queues_seen": "22"} and _a["tokens"]["queues_seen"] == "22"
    _l = {"agent_status": "done", "state_change_seq": 5, "pane_id": "w1:p1", "machine": "", "tokens": {}}
    assert q.mark_seen({}, [_l], _l, {}) and _calls[-1][0] is None
    _n = len(_calls)
    assert q.mark_seen({}, [], {"agent_status": "working", "pane_id": "w1:p2", "machine": ""}, {}) and len(_calls) == _n
    assert not q.mark_seen({}, [], dict(_a, machine="nowhere"), {})   # unreachable: says so
finally:
    q.call, q.retoken = _call, _ret
print("ok")
