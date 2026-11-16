"""python3 test_queues.py: headings, collapse, sort modes' labels, colours, marks and indent, git state, badges (no herdr needed)."""
import importlib.util, os, subprocess, sys, tempfile, tomllib

sys.argv = ["x"]
spec = importlib.util.spec_from_file_location("q", os.path.join(os.path.dirname(__file__), "queues.py"))
q = importlib.util.module_from_spec(spec); spec.loader.exec_module(q)
A = lambda p, ws, st: {"pane_id": p, "workspace": ws, "workspace_id": ws, "agent_status": st, "host": "micro",
                       "tokens": {"machine_icon": "🐭"}, "agent": "claude", "terminal_id": "t" + p}
ags = [A("p1", "a", "working"), A("p2", "b", "blocked"), A("p3", "c", "idle")]
st = {"queues": ["priority", "work"], "members": {}, "icons": {"priority": "🔥"}, "sort": "queues", "headings": "queue"}
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
assert t["p1"]["queues_label"] == q.SPIN[0] + " 🔥🐭 a"  # herdr indents it two; working: the spinner's first frame
assert t["p2"]["queues_label"] == P * 2 + "🔴 🔥🐭 b" and t["p2"]["queues_heading"] is None
assert t["p3"]["queues_label"] == "⚪ work🐭 c" and t["p3"]["queues_heading"] == "▾ work"
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
v = q.view(st3, "micro"); assert v["sort"][0]["field"] == {"token": "queues_rank"} and "micro" in str(v["filter"])
assert q.view(dict(st3, entries=False), "micro") == q.VIEW
st3["collapsed"] = ["priority"]
t3 = {r[1]["pane_id"]: r[1]["tokens"] for r in q.hiding(st3, [b1, b2, b3]) if r[0] == "pane.report_metadata"}
assert t3["p2"]["queues_inactive"] == "1" and t3["p3"]["queues_inactive"] == "1" and t3["p1"]["queues_inactive"] is None
assert q.hiding(st3, [b1], "carbon")[-1][1] == q.view(st3, "carbon")
print("ok")
