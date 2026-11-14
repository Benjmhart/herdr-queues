#!/usr/bin/env python3
"""herdr-streams: one queue per stream (personal, work, ...) of the agents waiting on you, each with what it needs.

`streams.py ui` is the popup: Tab/left/right switch stream, j/k move, Enter focuses the agent, s puts it in a stream,
a shows every agent in the stream (not only the ones waiting). `open` opens the popup. Standard library only.

Waiting = blocked (it asked you something) or done (it finished and wants a look). The summary is the agent's own
`※ recap:` line when Claude Code printed one, else the last question on its screen, else its last line of output.
Every machine in `herdr machine list` is shown; a jump to another machine drives the window's Goto picker (see README).
"""
import curses, json, os, socket, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

# Outside herdr (the desktop hotkey) it shares the plugin's state on this machine.
STATE = os.path.join(os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "herdr/plugins/benjmhart.streams"),
    "streams.json")
STANDALONE = "HERDR_PLUGIN_ID" not in os.environ
HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
ICON = {"working": "●", "blocked": "!", "done": "✓", "idle": "○"}
WAITING = ("blocked", "done")
UNSORTED = "unsorted"


# --- herdr: this server plus every machine it knows (`herdr machine list`) ---------------------------------------

def herdr(machine, *args, timeout=5):
    cmd = [HERDR] + (["--machine", machine] if machine else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"{' '.join(cmd[1:])}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def machines():
    """{label: host} of the enabled machines; "" is this server."""
    out = {"": socket.gethostname()}
    try:
        for ln in herdr(None, "machine", "list").splitlines():
            f = ln.split("\t")
            if len(f) >= 5 and f[4] == "enabled":
                out[f[1]] = f[2].split("@")[-1].split(".")[0]
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        pass
    return out


DOWN = {}  # machine -> when it last failed; skipped for a minute so one offline box doesn't stall the popup


def fetch(machine):
    if time.time() - DOWN.get(machine, 0) < 60:
        return []
    try:
        ags = json.loads(herdr(machine, "agent", "list", timeout=3))["result"]["agents"]
        ws = json.loads(herdr(machine, "workspace", "list", timeout=3))["result"]["workspaces"]
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        DOWN[machine] = time.time()
        return []
    names = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in ws}
    for a in ags:
        a["machine"], a["workspace"] = machine, names.get(a["workspace_id"], a["workspace_id"])
    return ags


def agents(ms):
    """Every agent on every machine, fetched in parallel (about half a second per remote call)."""
    with ThreadPoolExecutor(len(ms)) as ex:
        return [a for part in ex.map(fetch, ms) for a in part]


def screen(agent):
    # visible: instant, and the only source herdr serves while an agent is blocked (recent needs it idle).
    try:
        return herdr(agent["machine"], "agent", "read", agent["pane_id"], "--source", "visible")
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return ""


# --- jumping: a plugin can't switch the window's machine (herdr #5904), so drive the window's Goto picker -------------

def herdr_window(hosts):
    """(window id, host it shows) of a herdr window: title "[herdr: ]<hostname>: <workspace>". X11 via xdotool."""
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY") or ":0")
    try:
        active = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True, env=env).stdout.split()
        ids = subprocess.run(["xdotool", "search", "--name", ": "], capture_output=True, text=True, env=env).stdout.split()
    except OSError:
        return None, None
    for w in active + ids:
        t = subprocess.run(["xdotool", "getwindowname", w], capture_output=True, text=True, env=env).stdout
        h = t.removeprefix("herdr: ").split(": ", 1)[0]
        if h in hosts:
            return w, h
    return None, None


def jump(machine, pane):
    """Run detached after the popup closed: switch the herdr window to the machine, then focus the pane."""
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY") or ":0")
    ms = machines()
    win, shown = herdr_window(set(ms.values()))
    if win and shown != ms.get(machine):
        x = lambda *a: subprocess.run(["xdotool", *a], env=env, capture_output=True)
        x("windowactivate", "--sync", win)
        x("windowfocus", "--sync", win)  # input focus too: activate alone leaves it elsewhere without a WM
        # ctrl+b a: Goto; / search; the machine's label lists its panes and Enter takes the first, which switches.
        x("key", "--clearmodifiers", "ctrl+b", "a", "slash")
        x("type", "--delay", "20", machine or "local")
        x("key", "Return")
        for _ in range(30):
            time.sleep(0.1)
            if herdr_window({ms.get(machine)})[0]:
                break
    herdr(machine, "agent", "focus", pane)


# --- what an agent needs ------------------------------------------------------------------------------------------

def chrome(line):
    """Claude Code's own furniture: rules, the input box, the status lines, the spinner."""
    s = line.strip()
    return (not s or s[0] in "─━╌╭╰│❯⏵✻✳※" or s.startswith(("-- INSERT", "INSERT ·", "NORMAL", "? for shortcuts"))
            or "esc to interrupt" in s or "shift+tab to cycle" in s or "Esc to cancel" in s)


def summary(text):
    """One line: the recap, else the last question, else the last line of output. ponytail: a screen-scraping
    heuristic tuned on Claude Code; other agents fall through to their last line."""
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].lstrip().startswith("※ recap:"):
            out = [lines[i].split("recap:", 1)[1].strip()]
            for nxt in lines[i + 1:]:
                if not nxt.strip() or chrome(nxt):
                    break
                out.append(nxt.strip())
            return " ".join(out).replace(" (disable recaps in /config)", "")
    for i in range(len(lines) - 1, -1, -1):
        q = lines[i].strip().lstrip("│").strip()  # a question dialog's text sits in │ lines
        if not q.endswith("?"):
            continue
        head = next((l.strip()[1:].strip() for l in reversed(lines[:i]) if l.strip()[:1] in ("☐", "☒")), None)
        if head:  # a question dialog: its header, then the question's last sentence
            return head + ": " + q.rsplit(". ", 1)[-1]
        if q.startswith("Do you want to"):  # a permission prompt: name the tool and what it is for
            rule = max((j for j in range(i) if lines[j].lstrip().startswith(("─", "━"))), default=-1)
            what = [l.strip() for l in lines[rule + 1:i] if not chrome(l)][:2]
            if what:
                return ": ".join(what) + " -- " + q
        return q
    body = [l.strip() for l in lines if not chrome(l)]
    return body[-1] if body else ""


# --- state: {"streams": [names], "members": {agent key: stream}} -------------------------------------------------

def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"streams": ["personal", "work"], "members": {}}


def save(state):
    with open(STATE + ".tmp", "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(STATE + ".tmp", STATE)


def keys(agent):
    """Pane ids change on restart, so membership is kept by the agent's session and its terminal (as herdr-folders)."""
    k = []
    if (agent.get("agent_session") or {}).get("value"):
        k.append("s:" + agent["agent_session"]["value"])
    if agent.get("terminal_id"):
        k.append("t:" + agent["terminal_id"])
    k = k or ["p:" + agent["pane_id"]]
    m = agent.get("machine")
    return [m + "/" + x for x in k] if m else k  # another machine's ids can repeat this one's


def stream_of(state, agent):
    for k in keys(agent):
        s = state["members"].get(k)
        if s in state["streams"]:
            return s
    return UNSORTED


def put(state, agent, stream):
    # ponytail: members is never pruned; a few bytes per agent ever sorted, prune if the file ever matters.
    if stream and stream != UNSORTED and stream not in state["streams"]:
        state["streams"].append(stream)
    for k in keys(agent):
        if stream and stream != UNSORTED:
            state["members"][k] = stream
        else:
            state["members"].pop(k, None)


def rename(state, old, new):
    state["streams"] = [new if x == old else x for x in state["streams"]]
    state["members"] = {k: new if f == old else f for k, f in state["members"].items()}


def delete(state, name):
    """Its agents go back to unsorted."""
    state["streams"].remove(name)
    state["members"] = {k: f for k, f in state["members"].items() if f != name}


def shift(state, name, d):
    """Move a stream one place left (d=-1) or right (d=1) in the tab order."""
    t, i = state["streams"], state["streams"].index(name)
    j = max(0, min(len(t) - 1, i + d))
    t[i], t[j] = t[j], t[i]
    return j


def tabs(state, live):
    """Stream names in order, with unsorted last when an agent is in it."""
    t = list(state["streams"])
    if any(stream_of(state, a) == UNSORTED for a in live):
        t.append(UNSORTED)
    return t


def queue(state, live, stream, everyone=False):
    """The stream's agents: blocked first, then done, then (with everyone) the rest. Unsorted always lists everyone:
    it is where agents are filed from."""
    rank = {"blocked": 0, "done": 1, "idle": 2, "working": 3}
    everyone = everyone or stream == UNSORTED
    mine = [a for a in live if stream_of(state, a) == stream and (everyone or a.get("agent_status") in WAITING)]
    return sorted(mine, key=lambda a: rank.get(a.get("agent_status"), 4))


# --- the popup ----------------------------------------------------------------------------------------------------

HELP = ("tab/←→ stream · j/k · enter: go to agent · 1-9: file agent in that stream · s: file by name · a: all/waiting\n"
        "streams: n new · r rename · d delete · </> move · q quit")


def prompt(scr, label, initial=""):
    h, w = scr.getmaxyx()
    curses.curs_set(1)
    s = initial
    while True:
        scr.move(h - 1, 0)
        scr.clrtoeol()
        scr.addstr(h - 1, 0, (label + s)[:w - 1])
        c = scr.get_wch()
        if c in ("\n", "\r", curses.KEY_ENTER):
            break
        if c == "\x1b":
            s = ""
            break
        if c in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            s = s[:-1]
        elif isinstance(c, str) and c.isprintable():
            s += c
    curses.curs_set(0)
    return s.strip()[:30]


def ui(scr):
    curses.curs_set(0)
    curses.use_default_colors()
    scr.timeout(3000)  # pick up status changes
    state, cur, sel, everyone, msg = load(), None, 0, False, ""
    ms = machines()
    cache = {}  # (pane, status change) -> summary, so the screen is read once per change

    while True:
        live = agents(ms)
        streams = tabs(state, live)
        if cur is None:  # open on the first stream with something waiting
            cur = next((i for i, s in enumerate(streams) if queue(state, live, s)), 0)
        cur %= len(streams)
        q = queue(state, live, streams[cur], everyone)
        sel = max(0, min(sel, len(q) - 1))
        h, w = scr.getmaxyx()
        scr.erase()
        x = 0
        for i, s in enumerate(streams):
            n = len(queue(state, live, s))
            t = f" {s} {n} " if n else f" {s} "
            if x + len(t) < w:
                scr.addstr(0, x, t, (curses.A_REVERSE if i == cur else 0) | (curses.A_BOLD if n else curses.A_DIM))
            x += len(t) + 1
        for i, ln in enumerate(HELP.splitlines()):
            scr.addstr(1 + i, 0, ln[:w - 1], curses.A_DIM)
        row = 4
        if not q:
            scr.addstr(row, 2, ("no agents in this stream" if everyone else
                                "nothing waiting on you here (a: show all its agents)")[:w - 3])
        for i, a in enumerate(q):
            if row + 1 >= h - 1:
                break
            key = (a["machine"], a["pane_id"], a.get("state_change_seq"))
            if key not in cache:
                cache[key] = summary(screen(a)) if a.get("agent_status") in WAITING else ""
            title = a.get("terminal_title_stripped") or os.path.basename(a.get("cwd", ""))
            where = f"{a['machine']} · {a['workspace']}" if a["machine"] else a["workspace"]
            head = f"{ICON.get(a.get('agent_status'), '?')} {where}  {title}"
            attr = curses.A_REVERSE if i == sel else 0
            scr.addstr(row, 0, head[:w - 1], attr | curses.A_BOLD)
            if cache[key]:
                scr.addstr(row + 1, 4, cache[key][:w - 5], curses.A_DIM)
            row += 2 if cache[key] else 1
            row += 1
        if msg:
            scr.addstr(h - 1, 0, msg[:w - 1], curses.A_DIM)
        scr.refresh()

        try:
            c = scr.get_wch()
        except curses.error:
            continue  # timeout: refresh
        msg = ""
        if c in ("q", "\x1b"):
            return
        elif c in ("\t", curses.KEY_RIGHT, "l"):
            cur, sel = cur + 1, 0
        elif c in (curses.KEY_BTAB, curses.KEY_LEFT, "h"):
            cur, sel = cur - 1, 0
        elif c in ("j", curses.KEY_DOWN):
            sel += 1
        elif c in ("k", curses.KEY_UP):
            sel -= 1
        elif c == "a":
            everyone = not everyone
        elif c in ("\n", "\r", curses.KEY_ENTER) and q:
            a = q[sel]
            if not a["machine"] and not STANDALONE:  # this server, and the popup's window is showing it
                herdr(None, "agent", "focus", a["pane_id"])
                return
            if not herdr_window(set(ms.values()))[0]:
                msg = f"can't switch this window to {a['machine']} from here: use mod+shift+s on the desktop"
                continue
            subprocess.Popen([sys.executable, __file__, "jump", a["machine"], a["pane_id"]], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        elif c == "s" and q:
            name = prompt(scr, f"stream ({', '.join(state['streams'])}; new ok; empty = unsorted): ",
                          stream_of(state, q[sel]) if stream_of(state, q[sel]) != UNSORTED else "")
            put(state, q[sel], name or None)
            save(state)
            msg = f"moved to {name or UNSORTED}"
        elif isinstance(c, str) and c in "123456789" and q and int(c) <= len(state["streams"]):
            name = state["streams"][int(c) - 1]
            put(state, q[sel], name)
            save(state)
            msg = f"moved to {name}"
        elif c == "n":
            name = prompt(scr, "new stream: ")
            if name and name != UNSORTED and name not in state["streams"]:
                state["streams"].append(name)
                save(state)
                cur = len(state["streams"]) - 1
        elif c == "r" and streams[cur] != UNSORTED:
            name = prompt(scr, "rename stream to: ", streams[cur])
            if name and name != UNSORTED and name not in state["streams"]:
                rename(state, streams[cur], name)
                save(state)
        elif c == "d" and streams[cur] != UNSORTED:
            if prompt(scr, f"delete stream {streams[cur]}? its agents go to unsorted (y/N) ").lower() == "y":
                delete(state, streams[cur])
                save(state)
        elif c in ("<", ">") and streams[cur] != UNSORTED:
            cur = shift(state, streams[cur], -1 if c == "<" else 1)
            save(state)


def main(cmd):
    if cmd in ("ui", "standalone"):
        curses.wrapper(ui)
    elif cmd == "jump":
        time.sleep(0.3)  # let the popup close first
        jump(*sys.argv[2:4])
    elif cmd == "open":
        with socket.socket(socket.AF_UNIX) as sk:  # the socket call 0.1/0.2 used
            sk.connect(os.environ["HERDR_SOCKET_PATH"])
            sk.sendall((json.dumps({"id": "streams", "method": "plugin.pane.open", "params": {
                "plugin_id": os.environ["HERDR_PLUGIN_ID"], "entrypoint": "ui", "focus": True}}) + "\n").encode())
            resp = json.loads(sk.makefile().readline())
        if "error" in resp:
            raise RuntimeError(f"plugin.pane.open: {resp['error']}")
    else:
        sys.exit("usage: streams.py ui|open|jump <machine> <pane>")


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "")
    except RuntimeError as e:
        sys.exit(f"herdr-streams: {e}")
