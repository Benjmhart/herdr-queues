# herdr-streams

A [herdr](https://herdr.dev) plugin: one queue per **stream** (personal, work, anything you name) of the agents
waiting on you, each with one line saying what it needs. Select one and herdr jumps to it.

```
 personal 1   work 3   unsorted 2
tab/←→ stream · j/k · enter: go to agent · s: set stream · a: all/waiting · q quit
! mlabs-marketing  Sales and marketing strategy for new customers
    Bash command: Save report, update nag, commit and push -- Do you want to proceed?
! castiron  next-story:bmad-pony loop
    9.11 scope: How should I proceed?
✓ nihongo-smash  Next-story:bmad-pony loop
    Stories 6.15 and 6.16 are merged and staging is green, which finishes all of Epic 6. The loop is paused until…
```

- **Waiting** means blocked (`!`, it asked you something) or done (`✓`, it finished). Blocked come first.
  `a` shows every agent in the stream.
- **The summary** is the agent's own `※ recap:` line when Claude Code printed one, else the question on its screen
  (a permission prompt is named by its tool and description), else its last line of output.
- **Streams**: `s` puts the selected agent in a stream; a new name creates one. Agents not in a stream are in
  `unsorted`. Membership follows the agent's session, so it survives a herdr restart.

## Install

```sh
herdr plugin install Benjmhart/herdr-streams
```

Bind a key in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+shift+s"
type = "plugin_action"
command = "benjmhart.streams.open"
description = "agents waiting on me, by stream"
```

Python 3 standard library only.

## Limits

- Streams are per herdr server. A plugin cannot switch the client to another machine
  ([herdr #5904](https://github.com/herdrdev/herdr/issues/5904), closed as not planned 2026-11-09), so each machine
  has its own queues.
- The summary scrapes the visible screen and is tuned on Claude Code; other agents show their last line.

## Test

`python3 test_streams.py` (no herdr needed).

MIT licence.
