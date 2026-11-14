"""python3 test_streams.py: the stream logic and the summary heuristic, without a running herdr."""
import streams as S

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
s = {"streams": ["personal", "work"], "members": {}}
S.put(s, a, "work")
S.put(s, c, "work")
assert S.stream_of(s, a) == "work" and S.stream_of(s, b) == S.UNSORTED
# after a restart the pane id and terminal change but the session is the same
assert S.stream_of(s, dict(a, pane_id="w9:p1", terminal_id="term_new")) == "work"
assert S.tabs(s, [a, b]) == ["personal", "work", "unsorted"] and S.tabs(s, [a]) == ["personal", "work"]
assert S.queue(s, [c, a, b], "work") == [a]                      # only the ones waiting
assert S.queue(s, [c, a, b], "work", everyone=True) == [a, c]    # blocked first
S.put(s, b, "games")                                             # a new stream is created on first use
assert s["streams"] == ["personal", "work", "games"]
S.put(s, a, None)
assert S.stream_of(s, a) == S.UNSORTED
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
