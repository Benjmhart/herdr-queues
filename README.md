# herdr-queues

A [herdr](https://herdr.dev) plugin: your own **queues** (personal, work, anything you name) of the agents waiting on
you, **on every machine herdr knows**, each with one line saying what it needs. Press Enter and the herdr window
switches to that agent's machine and focuses it.

```
 personal 1   work 3   unsorted 2
←→/h l/tab queue · ↑↓/j k · enter: go to agent · m: move to queue · 1-9: move to that queue · a: all/waiting
queues: n new · r rename · d delete · </> reorder · q quit

! beast-arch · mlabs-marketing  Sales and marketing strategy for new customers
    Bash command: Save report, update nag, commit and push -- Do you want to proceed?

! carbon · castiron  next-story:bmad-pony loop
    9.11 scope: How should I proceed?

✓ nihongo-smash  Next-story:bmad-pony loop
    Stories 6.15 and 6.16 are merged and staging is green, which finishes all of Epic 6. The loop is paused until…
```

- **A queue lists all its agents**, the waiting ones first: blocked (`!`, it asked you something), then done (`✓`, it
  finished). **Your own order:** `J` / `K` (or the ▲ ▼ buttons) move the selected agent down or up, and from then on
  the queue keeps your order; agents you never placed follow it, by status. Inactive agents sit at the bottom. The number on a tab counts the waiting ones. `a` narrows the list to the waiting ones.
- **The summary** is the agent's own `※ recap:` line when Claude Code printed one, else the question on its screen
  (a permission prompt is named by its tool and description), else its last line of output.
- **Every machine**: the server you are on plus every enabled machine in `herdr machine list`. Agents on this
  server show bare; the rest as `machine · workspace`. A machine that doesn't answer is skipped for a minute.
- **One line per agent**, in the Dracula palette; the list scrolls with the selection (↑↓, PgUp/PgDn, `g`/`G`),
  and the popup opens at 98 % × 96 % of the window.
- **Fast**: the list opens at once and fills in the background (about two seconds for five machines); keys never
  wait on the network.
- **Filing agents**: every agent starts in `unsorted`, which always lists all of them. `m` opens a list of the
  other queues (or a new one) to move the selected agent into; `1`-`9` moves it straight to that queue.
- **The mouse**: a row of buttons under the help (go, ▲, ▼, move, waiting/all, inactive, alerts, sort, new, rename, delete,
  `<` `>`, quit) does what its key does. Click a tab to open it, a line to select it, the selected line again to go
  to it; the wheel scrolls the list. In the move box, click a queue (outside the box cancels).
- **Your own queues**: `n` new, `r` rename, `d` delete (its agents go back to unsorted), `<` / `>` reorder.
  Membership follows the agent's session, so it survives a herdr restart.

## Install

```sh
herdr plugin install Benjmhart/herdr-queues
```

Bind a key in `~/.config/herdr/config.toml`:

```toml
[[keys.command]]
key = "prefix+u"
type = "plugin_action"
command = "benjmhart.queues.open"
description = "agents waiting on me, by queue"
```

Python 3 standard library only. Install it on every machine you connect to with `herdr machine`, since the popup
runs on whichever machine the window is showing.

## The sidebar in queue order

`s` in the popup sorts herdr's sidebar on every machine by your queues: workspaces holding agents of the first queue
first (blocked, then done, idle, working), then the next queue, then the rest as they were. `s` again puts back your
own order, saved when you left it; workspaces made since go last. herdr's agent panel follows the workspace order
(`ui.agent_panel_sort = "spaces"`, the default), so it reorders too. Another machine is reordered over ssh
(`workspace.move` is a socket call the CLI lacks), so one this machine cannot ssh to keeps its order.

## Inactive agents: off herdr's sidebar, still here

`i` in the popup marks the selected agent inactive (`i` again undoes it): herdr's sidebar stops listing it, on every
machine, while the popup keeps it in its queue, marked "· inactive", and the watcher stops notifying about it. It is
herdr's own agent view (`agent.view.set`, the sidebar header reads "filtered"): the agent's pane carries the token
`queues_inactive` on its own server, and every server gets a view hiding panes with that token. A token rather than a
pane id, because a saved-machine client applies the selected server's view to every machine's agents, and pane ids
repeat across machines. Views and tokens don't survive a herdr restart, so the watcher puts them back once a minute;
the mark itself is kept by the agent's session and terminal, like queue membership. `herdr agent list` is unchanged.

## Jumping to another machine: run the watcher where you sit

A plugin can't switch the herdr window to another machine
([herdr #5904](https://github.com/herdrdev/herdr/issues/5904), not planned), and the popup runs on the server the
window is showing, which may have no way back to your screen. So:

1. Enter in the popup writes a one-line request (`<host> <pane>`) to the plugin's state on that server.
2. `queues.py watch`, running on the machine you sit at, follows that file on every machine (`ssh <machine> tail
   -F`, the same ssh herdr uses) and drives the window showing the requesting machine: `prefix+a` (Goto), `/`, the
   machine's label, Enter (which switches the window), then `herdr --machine M agent focus P`. About 0.6 s.

Start the watcher with your desktop session, e.g. in xmonad's startup hook or i3/sway `exec`:

```sh
python3 "$(ls -dt ~/.config/herdr/plugins/github-installations/benjmhart.queues-*/*/checkout/queues.py | head -1)" watch
```

It needs `xdotool` and an X11 display, the Goto picker on `prefix+a` with `prefix` = ctrl+b, and herdr's window
title in its default shape, `{hostname}: {workspace}` (a `herdr: ` prefix is fine). Without a watcher, Enter on
this server's own agents still works; on another machine's it does nothing visible.

`queues.py standalone` runs the same list outside herdr (bind it to a desktop key); it drives the window itself.

## One set of queues on every machine

Each save also goes down the same request channel, and the watcher keeps the master copy on your desk and writes it
to every machine, so the popup on any machine opens with the same queues. A watcher only carries out a jump when its
focused window is a herdr window showing the requesting machine and the screen is not locked, so watchers on other
desks (locked, or showing something else) stay still. Without a watcher, each machine keeps its own queues.

## Agents left waiting: a notification

The watcher also checks every machine's agents once a minute and sends a desktop notification (`notify-send`) when one
has waited on you too long: **blocked** (it asked a permission or a question) for 10 minutes, or **done** (it finished
and you haven't looked) for 65 minutes. done waits longer because a `/loop` agent sleeping until its next wakeup (at
most an hour) shows as done too. Each wait is told once; looking at the agent ends it. Agents already waiting when the
watcher starts are left out. Every desk's watcher notifies on its own screen.

`b` in the popup (or the **alerts** button) turns notifications off for the selected agent, and on again; it shows
"· no alerts" and is kept by the agent's session, like inactive. To turn them off on a desk altogether, start the
watcher with `watch --no-notify` (it still keeps the inactive marks up).

It also reads each idle agent's prompt: a reply typed but never sent (still there, unchanged, after 5 minutes) gets
its own notification. A dimmed suggested prompt isn't counted.

## Limits

- X11 only for the machine switch (xdotool). On Wayland the jump can focus the agent but not switch machines.
- The summary scrapes the visible screen and is tuned on Claude Code; other agents show their last line.

## Upgrading from herdr-streams

The plugin id changed (`benjmhart.streams` → `benjmhart.queues`). Your streams are read once from the old state
file; then `herdr plugin uninstall benjmhart.streams`.

## Test

`python3 test_queues.py` (no herdr needed).

MIT licence.
