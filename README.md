# herdr-streams

A [herdr](https://herdr.dev) plugin: one queue per **stream** (personal, work, anything you name) of the agents
waiting on you **on every machine herdr knows** (`herdr machine list`), each with one line saying what it needs.
Select one and herdr jumps to it, switching the window to that machine if it has to.

```
 personal 1   work 3   unsorted 2
tab/←→ stream · j/k · enter: go to agent · 1-9: file agent in that stream · s: file by name · a: all/waiting
streams: n new · r rename · d delete · </> move · q quit
! beast-arch · mlabs-marketing  Sales and marketing strategy for new customers
    Bash command: Save report, update nag, commit and push -- Do you want to proceed?
! carbon · castiron  next-story:bmad-pony loop
    9.11 scope: How should I proceed?
✓ nihongo-smash  Next-story:bmad-pony loop
    Stories 6.15 and 6.16 are merged and staging is green, which finishes all of Epic 6. The loop is paused until…
```

- **Waiting** means blocked (`!`, it asked you something) or done (`✓`, it finished). Blocked come first.
  `a` shows every agent in the stream.
- **The summary** is the agent's own `※ recap:` line when Claude Code printed one, else the question on its screen
  (a permission prompt is named by its tool and description), else its last line of output.
- **Filing agents**: every agent starts in `unsorted`, which always lists all of them. Select one and press the
  stream's number (`1` = the first tab) or `s` to type a stream name (a new name creates it).
- **Your own streams**: `n` new, `r` rename, `d` delete (its agents go back to unsorted), `<` / `>` move the tab.
  The defaults are personal and work. Membership follows the agent's session, so it survives a herdr restart.
- **Other machines**: agents on this server show bare; the rest as `machine · workspace`. A machine that doesn't
  answer is skipped for a minute.

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

## Jumping to another machine

A plugin can't switch the herdr window to another machine
([herdr #5904](https://github.com/herdrdev/herdr/issues/5904), not planned), so streams drives the window's own Goto
picker: `prefix+a`, `/`, the machine's label, Enter (which switches the window), then `herdr --machine M agent
focus`. That needs `xdotool` and the X display the herdr window is on, and the Goto picker on `prefix+a` with
`prefix` = ctrl+b. The window is found by its title, herdr's default `{hostname}: {workspace}` (an optional
`herdr: ` prefix is fine).

The popup runs on the server the window is showing. When that is the machine with the display, every jump works.
When the window is showing another machine, the popup can only jump within that machine. For that, run it from the
desktop instead:

```sh
python3 ~/.config/herdr/plugins/github-installations/benjmhart.streams-*/streams.py standalone
```

in a terminal bound to a desktop key; outside herdr it shares the plugin's state on that machine.

## Limits

- Streams (the names and who is filed where) are kept per machine: the popup on carbon and the one on beast-arch
  keep their own.
- Jumping is X11 only (xdotool). On Wayland the jump focuses the agent but cannot switch the window's machine.
- The summary scrapes the visible screen and is tuned on Claude Code; other agents show their last line.

## Test

`python3 test_streams.py` (no herdr needed).

MIT licence.
