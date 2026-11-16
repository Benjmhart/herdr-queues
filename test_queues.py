"""python3 test_queues.py: headings, collapse, sort modes' labels, colours, git state, badges (no herdr needed)."""
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
st.update(colors={"priority": "#ff5555"}); r = q.rows_toml(st)
assert 'contains = "🔥", fg = "#ff5555"' in r; tomllib.loads(r)
d = tempfile.mkdtemp(); subprocess.run(["git", "-C", d, "init", "-q"])
assert q.git_of(d) == "clean"; open(d + "/f", "w").write("x"); assert q.git_of(d) == "dirty"; assert q.git_of("/") is None
st["badges"] = {"gitstatus": {"dirty": "±"}}; assert q.info({"cwd": d}, st)["gitstatus"] == "±"
a = A("p1", "a", "idle"); a["tokens"]["usage"] = "62"; assert q.label(a, None, "{workspace} {usage_pct}") == "a 62%"
print("ok")
