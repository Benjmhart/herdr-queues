"""python3 test_queues.py: headings, collapse, sort modes' labels, colours, marks and indent, git state, badges (no herdr needed)."""
import importlib.util, os, subprocess, sys, tempfile, tomllib

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
assert t["p1"]["queues_heading"] == "▾ 🔥 priority" and t["p2"]["queues_heading"] is None and t["p3"]["queues_heading"] == "▾ work"
st["collapsed"] = ["priority"]; t = tok(st)  # the blocked one carries the collapsed queue
assert t["p1"]["queues_inactive"] == "1" and t["p2"]["queues_inactive"] is None and t["p2"]["queues_heading"] == "▸ 🔥 priority +1"
assert q.hiding(st, ags)[-1][0] == "agent.view.set"
st["sort"] = "workspaces"; t = tok(st)  # off: plain names, no headings
assert t["p2"]["queues_heading"] is None and t["p2"]["queues_label"] == "b"
st.update(sort="auto", headings="machine", collapsed=[]); t = tok(st)
assert t["p1"]["queues_heading"] == "🐭 micro" and t["p2"]["queues_heading"] is None
assert q.setting(st, "color", ["priority", "#ff5555"]) and not q.setting(st, "color", ["priority", "red"])
r = q.rows_toml(st); tomllib.loads(r); assert "$queues_heading_after" not in r and "state_icon" in r
st.update(mark="own", headings="queue", sort="queues", spinner=True); r = q.rows_toml(st); tomllib.loads(r)
assert "$queues_heading_after" not in r and "#f8f8f2" in r and "state_icon" not in r and q.WJ not in r  # escaped, not invisible
t = tok(st)  # own marks: every heading flush left on its group's first agent; names uncoloured under headings
P, W = q.PAD, q.WJ
assert t["p1"]["queues_heading"] == "▾ 🔥 priority" + W  # red = palette slot 1
assert t["p1"]["queues_label"] == q.SPIN[0] + "  🔥🐭 a" + q.marker("yellow")  # herdr indents it two; working: the spinner's first frame
assert t["p2"]["queues_label"] == P * 2 + "🔴 🔥🐭 b" + q.marker("red") and t["p2"]["queues_heading"] is None
assert t["p3"]["queues_label"] == P * 2 + " work🐭 c" and t["p3"]["queues_heading"] == "▾ work"
assert all(t[p]["queues_heading_after"] is None for p in t)
assert not q.setting(st, "host", ["micro", "icon", "🦊"]); t = tok(st); assert "🦊" in t["p1"]["queues_label"]
d = tempfile.mkdtemp(); subprocess.run(["git", "-C", d, "init", "-q"])
assert q.git_of(d) == "clean"; open(d + "/f", "w").write("x"); assert q.git_of(d) == "dirty"; assert q.git_of("/") is None
st["badges"] = {"gitstatus": {"dirty": "±"}}; assert q.info({"cwd": d}, st)["gitstatus"] == "±"
a = A("p1", "a", "idle"); a["tokens"]["usage"] = "62"; assert q.label(a, None, "{workspace} {usage_pct}") == "a 62%"
# heading entries: text, and fake agents filtered
st2 = {"queues": ["priority"], "icons": {"priority": "🔥"}}
assert q.heading_text(st2, "priority", 2, False) == "▾ 🔥 priority"
assert q.heading_text(st2, "priority", 2, True) == "▸ 🔥 priority +2"
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
with tempfile.TemporaryDirectory() as t:
    os.environ["XDG_CONFIG_HOME"] = t
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
