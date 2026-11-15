"""python3 test_queues.py: the queue logic and the summary heuristic, without a running herdr."""
import queues as S

recap = """  Next: answer (a), (b) or (c), then run /loop /bmad-pony.

✻ Brewed for 22s · done 9:58 PM

※ recap: Stories 6.15 and 6.16 are merged and staging is green, which finishes all of Epic 6. The loop is paused until
  you choose: add review-history sync as story 6.17 next, or close Epic 6. (disable recaps in /config)

────────────────────────
❯
────────────────────────
  INSERT · Opus 5.5 · ctx 72% · medium · 7d 18%
  -- INSERT -- ⏵⏵ auto mode on (shift+tab to cycle) · ← 1 agent
"""
assert S.summary(recap).startswith("Stories 6.15 and 6.16 are merged")
assert S.summary(recap).endswith("or close Epic 6.")
question = "I changed the config.\n  Should I also push it to main?\n\n────\n❯ \n────\n  -- INSERT --\n"
assert S.summary(question) == "Should I also push it to main?"
assert S.summary("built ok\nall tests pass\n────\n❯\n") == "all tests pass"
assert S.summary("") == ""

a = {"pane_id": "w1:p1", "terminal_id": "term_a", "agent_session": {"value": "sess-a"}, "agent_status": "blocked"}
b = {"pane_id": "w2:p1", "terminal_id": "term_b", "agent_status": "done"}
c = {"pane_id": "w3:p1", "terminal_id": "term_c", "agent_status": "working"}
s = {"queues": ["personal", "work"], "members": {}}
S.put(s, a, "work")
S.put(s, c, "work")
assert S.queue_of(s, a) == "work" and S.queue_of(s, b) == S.UNSORTED
# after a restart the pane id and terminal change but the session is the same
assert S.queue_of(s, dict(a, pane_id="w9:p1", terminal_id="term_new")) == "work"
assert S.tabs(s, [a, b]) == ["personal", "work", "unsorted"] and S.tabs(s, [a]) == ["personal", "work"]
assert S.members(s, [c, a, b], "work") == [a]                      # only the ones waiting
assert S.members(s, [c, a, b], "work", everyone=True) == [a, c]    # blocked first
S.put(s, b, "games")                                             # a new queue is created on first use
assert s["queues"] == ["personal", "work", "games"]
S.put(s, a, None)
assert S.queue_of(s, a) == S.UNSORTED
print("ok")

perm = """────────────────────────────────
 Bash command
 Save report, update nag, commit and push
╌╌╌╌╌╌╌╌╌╌╌╌
 │ git add x && git commit -qm "msg?"
╌╌╌╌╌╌╌╌╌╌╌╌
 Ask rule Bash(git push:*) overrides auto mode for this command.
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for: ls *
   3. No
 Esc to cancel · Tab to amend
"""
assert S.summary(perm) == "Bash command: Save report, update nag, commit and push -- Do you want to proceed?", S.summary(perm)
print("ok perm")

ask = """ ☐ 9.11 scope
│ Story 9.11's backup test caught a real bug.
│ migrate.rs is outside 9.11's scope. How should I proceed?
❯ 1. Extend 9.11 (Recommended)
     Add migrate.rs to 9.11's scope.
  4. Type something.
────────────────
  5. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel
"""
assert S.summary(ask) == "9.11 scope: How should I proceed?", S.summary(ask)
print("ok ask")

s = {"queues": ["personal", "work", "games"], "members": {}}
S.put(s, a, "work")
S.rename(s, "work", "job")
assert s["queues"] == ["personal", "job", "games"] and S.queue_of(s, a) == "job"
assert S.shift(s, "games", -1) == 1 and s["queues"] == ["personal", "games", "job"]
assert S.shift(s, "personal", -1) == 0 and s["queues"][0] == "personal"   # already first: stays
S.delete(s, "job")
assert s["queues"] == ["personal", "games"] and S.queue_of(s, a) == S.UNSORTED
idle = dict(c, agent_status="idle")
assert S.members(s, [idle], S.UNSORTED) == [idle]                 # unsorted lists everyone, waiting or not
print("ok queues editing")

# keys are the same whichever machine's popup reads them: session as is, terminal under its host
r = dict(c, machine="carbon", host="carbon")
assert S.keys(r) == ["carbon/t:term_c"] and S.keys(dict(c, host="micro")) == ["micro/t:term_c"]
assert S.keys(dict(a, host="x"))[0] == "s:sess-a"
s = {"queues": ["work"], "members": {}}
S.put(s, r, "work")
assert S.queue_of(s, r) == "work" and S.queue_of(s, dict(c, host="micro")) == S.UNSORTED
# 0.4.1 state, written by micro's popup: carbon's agent under its label, micro's own bare
old = {"queues": ["work", "home"], "members": {"carbon/t:term_c": "work", "t:term_b": "home"}}
assert S.queue_of(old, dict(r)) == "work"
assert S.queue_of(old, dict(b, machine="", host="micro")) == "home"
S.put(old, r, "home")                       # re-filing writes the new key and drops the old one
assert old["members"] == {"carbon/t:term_c": "home", "t:term_b": "home"} or "carbon/t:term_c" in old["members"]
assert S.valid(old) and not S.valid({"queues": "x", "members": {}}) and not S.valid({"queues": [], "members": {"a": 1}})
print("ok machines")

# herdr-streams' state file is read when there is no queues.json yet
import json, os, subprocess, sys, tempfile
d = tempfile.mkdtemp()
S.STATE, S.OLD_STATE = os.path.join(d, "queues.json"), os.path.join(d, "streams.json")
json.dump({"streams": ["home"], "members": {"t:x": "home"}}, open(S.OLD_STATE, "w"))
assert S.load() == {"queues": ["home"], "members": {"t:x": "home"}}
# the sidebar's order: by the best (queue, status) per workspace, the rest as they were; restoring puts new ones last
st = {"queues": ["personal", "work"], "members": {}}
ag = [{"workspace_id": "w3", "agent_status": "idle", "terminal_id": "t3", "host": "h"},
      {"workspace_id": "w1", "agent_status": "blocked", "terminal_id": "t1", "host": "h"},
      {"workspace_id": "w2", "agent_status": "done", "terminal_id": "t2", "host": "h"}]
S.put(st, ag[0], "personal"); S.put(st, ag[1], "work"); S.put(st, ag[2], "work")
assert S.by_queues(st, ag, ["w1", "w2", "w3", "w4", "w5"]) == ["w3", "w1", "w2", "w4", "w5"]
assert S.restored(["w5", "w1", "w9"], ["w1", "w2", "w5"]) == ["w5", "w1", "w2"]
print("ok migrate")

# inactive: marked by the agent's keys (survives a pane id change), cleared again; the view is set only while any is
st = {"queues": ["work"], "members": {}}
x = {"pane_id": "w1:p1", "terminal_id": "tx", "host": "carbon", "agent_status": "idle"}
y = {"pane_id": "w1:p1", "terminal_id": "ty", "host": "micro", "agent_status": "done"}  # same pane id, other machine
S.set_inactive(st, x, True)
assert S.inactive(st, dict(x, pane_id="w9:p4")) and not S.inactive(st, y)
r = S.hiding(st, [x, y])
assert r[0] == ["pane.report_metadata", {"pane_id": "w1:p1", "source": S.SOURCE, "tokens": {S.TOKEN: "1"}}]
assert r[1][1]["tokens"] == {S.TOKEN: None} and r[2] == ["agent.view.set", S.VIEW]
S.set_inactive(st, x, False)
assert st["inactive"] == [] and S.hiding(st, [])[-1] == ["agent.view.clear", {"source": S.SOURCE}]
print("ok inactive")

q = {"queues": ["work"], "members": {}}
S.set_quiet(q, a, True)
assert S.quiet(q, a) and not S.quiet(q, b)
assert S.quiet(q, dict(a, pane_id="w9:p1"))           # follows the session, like inactive
S.set_quiet(q, a, False)
assert not S.quiet(q, a) and q["quiet"] == []
print("ok quiet")

# order within a queue (J/K), inactive last
x = {"pane_id": "p1", "terminal_id": "tx", "agent_status": "working"}
y = {"pane_id": "p2", "terminal_id": "ty", "agent_status": "blocked"}
z = {"pane_id": "p3", "terminal_id": "tz", "agent_status": "done"}
o = {"queues": ["w"], "members": {}}
for g in (x, y, z):
    S.put(o, g, "w")
L = [x, y, z]
assert S.members(o, L, "w", True) == [y, z, x]                 # by status until you order them
S.set_inactive(o, y, True)
assert S.members(o, L, "w", True) == [z, x, y]                 # inactive sinks
S.set_inactive(o, y, False)
assert S.move(o, L, "w", x, -1) == 1                           # x up past z
assert S.members(o, L, "w", True) == [y, x, z]
y["agent_status"] = "working"                                  # status no longer reorders a placed queue
assert S.members(o, L, "w", True) == [y, x, z]
S.set_inactive(o, y, True)
assert S.members(o, L, "w", True) == [x, z, y]                 # inactive still sinks below your order
S.set_inactive(o, y, False)
# waiting-only view: z (done) moves up past y? y is working now, so only z waits; move is a no-op there
assert S.move(o, L, "w", z, -1, everyone=False) == 0
S.rename(o, "w", "v")
assert S.members(o, L, "v", True) == [y, x, z] and "w" not in o["rank"]
S.delete(o, "v")
assert "v" not in o["rank"]
print("ok order")

# newest copy wins: a stale copy (a watcher starting, a popup left open) never replaces a newer one (2026-11-15)
S.write({"queues": ["a"], "members": {}, "saved": 200.0})
S.write({"queues": ["stale"], "members": {}, "saved": 100.0})
assert S.load()["queues"] == ["a"]
S.write({"queues": ["b"], "members": {}, "saved": 300.0})
assert S.load()["queues"] == ["b"]
r = subprocess.run([sys.executable, "-c", S.KEEP_NEWEST, S.STATE], input='{"queues": ["old"], "members": {}}', text=True)
assert r.returncode == 0 and S.load()["queues"] == ["b"]  # no stamp = oldest
print("ok newest")

# relayed agents: a machine this one can't reach (micro) sent its list; shown unless reachable, or older than a minute
S.RELAY = os.path.join(d, "relay"); os.makedirs(S.RELAY)
json.dump([{"pane_id": "w1:p1", "agent_status": "done", "summary": "hi"}], open(os.path.join(S.RELAY, "micro.json"), "w"))
json.dump([{"pane_id": "w1:p1"}], open(os.path.join(S.RELAY, "carbon.json"), "w"))
json.dump([{"pane_id": "w1:p1"}], open(os.path.join(S.RELAY, "old.json"), "w"))
os.utime(os.path.join(S.RELAY, "old.json"), (0, 0))
got = S.relayed({"": ("beast-arch", None), "carbon": ("carbon", "carbon")})
assert [(a["host"], a["machine"], a["summary"]) for a in got] == [("micro", "micro", "hi")]
sink = subprocess.run([sys.executable, "-c", S.RELAY_SINK], input='{"host": "../x", "agents": []}\n', text=True,
                      env=dict(os.environ, HOME=d), capture_output=True)
assert not os.path.exists(os.path.join(d, ".local/state/herdr/plugins/x.json"))  # a host name can't climb out
print("ok relay")
