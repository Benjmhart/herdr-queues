#!/usr/bin/env python3
"""herdr-queues: your own queues (personal, work, ...) of the agents waiting on you, on every machine herdr knows,
each with one line of what it needs. Enter jumps to the agent, switching the herdr window to its machine.

  queues.py ui           the popup (the plugin's pane)
  queues.py open         opens the popup (the plugin's action)
  queues.py standalone   the same list outside herdr (a desktop key); jumps directly
  queues.py watch        run on the machine you sit at: carries out jumps asked for by a popup on any machine
  queues.py jump M P     switch the herdr window to machine M and focus pane P

Standard library only. Data loads in a background thread, so keys never wait on the network.
"""
import curses, json, os, re, socket, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

PLUGIN = "benjmhart.queues"
STATE_ROOT = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(STATE_ROOT, "herdr/plugins", PLUGIN)
STATE = os.path.join(STATE_DIR, "queues.json")
OLD_STATE = os.path.join(STATE_ROOT, "herdr/plugins/benjmhart.streams/streams.json")  # herdr-streams <= 0.3
REQUESTS = os.path.join(STATE_DIR, "jump")  # "<host> <pane>" lines, followed by `queues.py watch`
REMOTE_REQUESTS = "~/.local/state/herdr/plugins/" + PLUGIN + "/jump"
HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
STANDALONE = "HERDR_PLUGIN_ID" not in os.environ
ICON = {"working": "●", "blocked": "!", "done": "✓", "idle": "○"}
WAITING = ("blocked", "done")
UNSORTED = "unsorted"
PANE = re.compile(r"^[\w:.-]{1,64}$")


# --- herdr: this server plus every machine in `herdr machine list` ---------------------------------------------------

def herdr(machine, *args, timeout=5):
    cmd = [HERDR] + (["--machine", machine] if machine else []) + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError(f"{' '.join(cmd[1:])}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def machines():
    """{label: (hostname, ssh target)} of the enabled machines; "" is this server (no ssh target)."""
    out = {"": (socket.gethostname(), None)}
    try:
        for ln in herdr(None, "machine", "list").splitlines():
            f = ln.split("\t")
            if len(f) >= 5 and f[4] == "enabled":
                out[f[1]] = (f[2].split("@")[-1].split(".")[0], f[2])
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        pass
    return out


def screen(machine, pane):
    # visible: instant, and the only source herdr serves while an agent is blocked (recent needs it idle).
    try:
        return herdr(machine, "agent", "read", pane, "--source", "visible")
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return ""


class Live:
    """Agents of every machine, refreshed in a background thread. The UI only ever reads the last snapshot."""

    def __init__(self, ms):
        self.ms, self.agents, self.summary, self.version = ms, [], {}, 0
        self.loaded = False
        self.names, self.names_at, self.down = {}, {}, {}
        self.lock = threading.Lock()
        self.pool, self.reads = ThreadPoolExecutor(8), ThreadPoolExecutor(6)
        threading.Thread(target=self.run, daemon=True).start()

    def fetch(self, m):
        if time.time() - self.down.get(m, 0) < 60:  # an offline machine is retried once a minute, not every round
            return []
        try:
            ags = json.loads(herdr(m, "agent", "list", timeout=4))["result"]["agents"]
            if time.time() - self.names_at.get(m, 0) > 30:  # workspace names change rarely: half the calls
                ws = json.loads(herdr(m, "workspace", "list", timeout=4))["result"]["workspaces"]
                self.names[m] = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in ws}
                self.names_at[m] = time.time()
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            self.down[m] = time.time()
            return []
        for a in ags:
            a["machine"] = m
            a["workspace"] = self.names.get(m, {}).get(a["workspace_id"], a["workspace_id"])
        return ags

    def read(self, key, m, pane):
        s = summary(screen(m, pane))
        with self.lock:
            self.summary[key] = s
            self.version += 1

    def run(self):
        while True:
            ags = [a for part in self.pool.map(self.fetch, self.ms) for a in part]
            with self.lock:
                self.agents, self.loaded = ags, True
                self.version += 1
                for a in ags:
                    k = skey(a)
                    if a.get("agent_status") in WAITING and k not in self.summary:
                        self.summary[k] = None  # asked for; read in parallel, shown when it lands
                        self.reads.submit(self.read, k, a["machine"], a["pane_id"])
            time.sleep(2)

    def snapshot(self):
        with self.lock:
            return list(self.agents), dict(self.summary), self.version


def skey(a):
    return (a["machine"], a["pane_id"], a.get("state_change_seq"))


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


# --- state: {"queues": [names], "members": {agent key: queue}} ----------------------------------------------------

def load():
    for path in (STATE, OLD_STATE):
        try:
            with open(path) as f:
                s = json.load(f)
            if "streams" in s:  # herdr-streams' file
                s = {"queues": s["streams"], "members": s["members"]}
            return s
        except (FileNotFoundError, ValueError):
            continue
    return {"queues": ["personal", "work"], "members": {}}


def save(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE + ".tmp", "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(STATE + ".tmp", STATE)


def keys(agent):
    """Pane ids change on restart, so membership is kept by the agent's session and its terminal (as herdr-folders).
    Another machine's ids can repeat this one's, so they carry the machine."""
    k = []
    if (agent.get("agent_session") or {}).get("value"):
        k.append("s:" + agent["agent_session"]["value"])
    if agent.get("terminal_id"):
        k.append("t:" + agent["terminal_id"])
    k = k or ["p:" + agent["pane_id"]]
    m = agent.get("machine")
    return [m + "/" + x for x in k] if m else k


def queue_of(state, agent):
    for k in keys(agent):
        q = state["members"].get(k)
        if q in state["queues"]:
            return q
    return UNSORTED


def put(state, agent, queue):
    # ponytail: members is never pruned; a few bytes per agent ever sorted, prune if the file ever matters.
    if queue and queue != UNSORTED and queue not in state["queues"]:
        state["queues"].append(queue)
    for k in keys(agent):
        if queue and queue != UNSORTED:
            state["members"][k] = queue
        else:
            state["members"].pop(k, None)


def rename(state, old, new):
    state["queues"] = [new if x == old else x for x in state["queues"]]
    state["members"] = {k: new if q == old else q for k, q in state["members"].items()}


def delete(state, name):
    """Its agents go back to unsorted."""
    state["queues"].remove(name)
    state["members"] = {k: q for k, q in state["members"].items() if q != name}


def shift(state, name, d):
    """Move a queue one place left (d=-1) or right (d=1) in the tab order."""
    t, i = state["queues"], state["queues"].index(name)
    j = max(0, min(len(t) - 1, i + d))
    t[i], t[j] = t[j], t[i]
    return j


def tabs(state, live):
    """Queue names in order, with unsorted last when an agent is in it."""
    t = list(state["queues"])
    if any(queue_of(state, a) == UNSORTED for a in live):
        t.append(UNSORTED)
    return t


def members(state, live, queue, everyone=False):
    """The queue's agents: blocked first, then done, then (with everyone) the rest. Unsorted always lists everyone:
    it is where agents are filed from."""
    rank = {"blocked": 0, "done": 1, "idle": 2, "working": 3}
    everyone = everyone or queue == UNSORTED
    mine = [a for a in live if queue_of(state, a) == queue and (everyone or a.get("agent_status") in WAITING)]
    return sorted(mine, key=lambda a: (rank.get(a.get("agent_status"), 4), a.get("machine", ""), a.get("workspace", "")))


# --- jumping: a plugin can't switch the window's machine (herdr #5904), so the window's Goto picker is driven -----
# The popup runs on the server the window shows, which may have no way to reach your display. So it appends a
# request to its own REQUESTS file, and `queues.py watch` on the machine you sit at follows that file on every
# machine (over ssh, as herdr itself reaches them) and drives the window that is showing the requesting machine.

def x11(*args):
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY") or ":0")
    try:
        return subprocess.run(["xdotool", *args], capture_output=True, text=True, env=env, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def herdr_windows(hosts):
    """[(window id, host it shows)] of herdr windows, the active one first. Title: "[herdr: ]<hostname>: <workspace>"."""
    out = []
    for w in dict.fromkeys(x11("getactivewindow").split() + x11("search", "--name", ": ").split()):
        h = x11("getwindowname", w).strip().removeprefix("herdr: ").split(": ", 1)[0]
        if h in hosts:
            out.append((w, h))
    return out


def jump(machine, pane, ms=None, shown_by=None):
    """Switch a herdr window to the machine (label; "" = this one) and focus the pane. shown_by: prefer the window
    showing that host (where the request was made)."""
    ms = ms or machines()
    hosts = {h for h, _ in ms.values()}
    target = ms[machine][0]
    wins = herdr_windows(hosts)
    win = next((w for w in wins if w[1] == shown_by), None) or (wins[0] if wins else None)
    if win and win[1] != target:
        x11("windowactivate", "--sync", win[0])
        x11("windowfocus", "--sync", win[0])  # input focus too: activate alone leaves it elsewhere without a WM
        # ctrl+b a: Goto; / search; the machine's label lists its panes and Enter takes the first, which switches.
        x11("key", "--clearmodifiers", "ctrl+b", "a", "slash")
        x11("type", "--delay", "8", machine or "local")
        x11("key", "Return")
        for _ in range(30):
            time.sleep(0.1)
            if x11("getwindowname", win[0]).strip().removeprefix("herdr: ").startswith(target + ": "):
                break
    herdr(machine, "agent", "focus", pane)


def request(host, pane):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(REQUESTS, "a") as f:
        f.write(f"{host} {pane}\n")
    try:
        if os.path.getsize(REQUESTS) > 64 * 1024:  # tail -F follows truncation
            open(REQUESTS, "w").close()
    except OSError:
        pass


def die_with_parent():
    """Linux: the ssh exits when the watcher does, instead of tailing on forever."""
    try:
        import ctypes, signal
        ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def follow(label, ms):
    """One machine's request file, forever: carry out each request in the window showing that machine."""
    shown_by, target = ms[label]
    path = os.path.expanduser(REQUESTS) if not target else REMOTE_REQUESTS
    sh = f'mkdir -p "$(dirname {path})" && touch {path} && exec tail -n0 -F {path}'
    cmd = ["sh", "-c", sh] if not target else [
        "ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ConnectTimeout=10", target, sh]
    by_host = {h: lab for lab, (h, _) in ms.items()}
    while True:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             text=True, preexec_fn=die_with_parent)
        for ln in p.stdout:
            f = ln.split()
            if len(f) == 2 and f[0] in by_host and PANE.match(f[1]):
                try:
                    jump(by_host[f[0]], f[1], ms, shown_by)
                except (RuntimeError, OSError, subprocess.TimeoutExpired, KeyError):
                    pass
        p.wait()
        time.sleep(10)  # machine down or rebooting: try again


def watch():
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(os.path.join(STATE_DIR, "watch.lock"), "w")
    try:  # one watcher per user: two would type into the window together
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("herdr-queues: a watcher is already running")
    ms = machines()
    for label in ms:
        threading.Thread(target=follow, args=(label, ms), daemon=True).start()
    while True:
        time.sleep(3600)


# --- the list ---------------------------------------------------------------------------------------------------

HELP = ("←→/h l/tab queue · ↑↓/j k · enter: go to agent · m: move to queue · 1-9: move to that queue · a: all/waiting\n"
        "queues: n new · r rename · d delete · </> reorder · q quit")
ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}
NAMED = {curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
         curses.KEY_ENTER: "enter", curses.KEY_BTAB: "left", curses.KEY_BACKSPACE: "backspace"}


def key(scr):
    """One key as a name or a character; None on timeout. Decodes arrow sequences itself as well, for a terminal
    whose terminfo does not match what it sends (a lone escape is "esc")."""
    try:
        c = scr.get_wch()
    except curses.error:
        return None
    if c in NAMED:
        return NAMED[c]
    if c in ("\n", "\r"):
        return "enter"
    if c in ("\x7f", "\b"):
        return "backspace"
    if c == "\x1b":
        scr.nodelay(True)
        seq = ""
        try:
            while len(seq) < 8:
                n = scr.get_wch()
                seq += n if isinstance(n, str) else ""
                if seq[-1:].isalpha() or seq[-1:] == "~":
                    break
        except curses.error:
            pass
        finally:
            scr.nodelay(False)
            scr.timeout(250)
        return ARROWS.get(seq[-1:], "esc") if seq[:1] in ("[", "O") else "esc"
    return c


def prompt(scr, label, initial=""):
    h, w = scr.getmaxyx()
    curses.curs_set(1)
    s = initial
    scr.timeout(-1)
    while True:
        scr.move(h - 1, 0)
        scr.clrtoeol()
        scr.addstr(h - 1, 0, (label + s)[:w - 1])
        c = key(scr)
        if c == "enter":
            break
        if c == "esc":
            s = ""
            break
        if c == "backspace":
            s = s[:-1]
        elif isinstance(c, str) and len(c) == 1 and c.isprintable():
            s += c
    curses.curs_set(0)
    scr.timeout(250)
    return s.strip()[:30]


def choose(scr, title, options):
    """A centred list; ↑↓/j k and Enter, a digit picks directly, Esc cancels. Returns the index or None."""
    h, w = scr.getmaxyx()
    bw = min(w - 4, max(len(title), *(len(o) for o in options)) + 10)
    bh = min(h - 2, len(options) + 4)
    win = curses.newwin(bh, bw, max(0, (h - bh) // 2), max(0, (w - bw) // 2))
    win.keypad(True)
    sel = 0
    while True:
        win.erase()
        win.box()
        win.addstr(0, 2, f" {title} "[:bw - 4], curses.A_BOLD)
        for i, o in enumerate(options[:bh - 4]):
            win.addstr(2 + i, 2, f"{i + 1 if i < 9 else ' '} {o}"[:bw - 4], curses.A_REVERSE if i == sel else 0)
        win.refresh()
        c = key(win)
        if c in ("up", "k"):
            sel = (sel - 1) % len(options)
        elif c in ("down", "j"):
            sel = (sel + 1) % len(options)
        elif c == "enter":
            return sel
        elif c in ("esc", "q"):
            return None
        elif isinstance(c, str) and c.isdigit() and 1 <= int(c) <= len(options):
            return int(c) - 1


def go(agent, ms):
    """Enter. Returns a message when the jump can't happen from here, else None (the list closes)."""
    if not agent["machine"] and not STANDALONE:  # this server, and the popup's window is showing it
        herdr(None, "agent", "focus", agent["pane_id"])
        return None
    if STANDALONE:  # on the desktop: drive the window directly, after this terminal has closed
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "jump", agent["machine"], agent["pane_id"]],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None
    request(ms[agent["machine"]][0], agent["pane_id"])  # `queues.py watch` on your desk carries it out
    return None


def ui(scr):
    os.environ.setdefault("ESCDELAY", "25")
    curses.curs_set(0)
    curses.use_default_colors()
    scr.timeout(250)
    ms = machines()
    live = Live(ms)
    state, cur, sel, everyone, msg, drawn = load(), None, 0, False, "", None

    while True:
        agents, sums, version = live.snapshot()
        qs = tabs(state, agents)
        if cur is None and live.loaded:  # open on the first queue with something waiting
            cur = next((i for i, s in enumerate(qs) if members(state, agents, s)), 0)
        if cur is not None:
            cur %= len(qs)
        q = members(state, agents, qs[cur or 0], everyone)
        sel = max(0, min(sel, len(q) - 1))
        if drawn != (version, cur, sel, everyone, msg, len(state["queues"])):
            drawn = (version, cur, sel, everyone, msg, len(state["queues"]))
            h, w = scr.getmaxyx()
            scr.erase()
            x = 0
            for i, s in enumerate(qs):
                n = len(members(state, agents, s))
                t = f" {s} {n} " if n else f" {s} "
                if x + len(t) < w:
                    scr.addstr(0, x, t, (curses.A_REVERSE if i == (cur or 0) else 0) | (curses.A_BOLD if n else curses.A_DIM))
                x += len(t) + 1
            for i, ln in enumerate(HELP.splitlines()):
                scr.addstr(1 + i, 0, ln[:w - 1], curses.A_DIM)
            row = 4
            if not live.loaded:
                scr.addstr(row, 2, f"loading {len(ms)} machines…"[:w - 3], curses.A_DIM)
            elif not q:
                scr.addstr(row, 2, ("no agents in this queue" if everyone else
                                    "nothing waiting on you here (a: show all its agents)")[:w - 3])
            for i, a in enumerate(q):
                if row + 1 >= h - 1:
                    break
                title = a.get("terminal_title_stripped") or os.path.basename(a.get("cwd", ""))
                where = f"{a['machine']} · {a['workspace']}" if a["machine"] else a["workspace"]
                scr.addstr(row, 0, f"{ICON.get(a.get('agent_status'), '?')} {where}  {title}"[:w - 1],
                           (curses.A_REVERSE if i == sel else 0) | curses.A_BOLD)
                s = sums.get(skey(a)) if a.get("agent_status") in WAITING else ""
                if a.get("agent_status") in WAITING:
                    scr.addstr(row + 1, 4, (s if s is not None else "…")[:w - 5], curses.A_DIM)
                    row += 1
                row += 2
            if msg:
                scr.addstr(h - 1, 0, msg[:w - 1], curses.A_DIM)
            scr.refresh()

        c = key(scr)
        if c is None:
            continue
        msg, cur = "", cur or 0
        if c in ("q", "esc"):
            return
        elif c in ("\t", "right", "l"):
            cur, sel = cur + 1, 0
        elif c in ("left", "h"):
            cur, sel = cur - 1, 0
        elif c in ("down", "j"):
            sel += 1
        elif c in ("up", "k"):
            sel -= 1
        elif c == "a":
            everyone = not everyone
        elif c == "enter" and q:
            msg = go(q[sel], ms)
            if not msg:
                return
        elif c == "m" and q:
            here = queue_of(state, q[sel])
            opts = [s for s in state["queues"] if s != here] + ([UNSORTED] if here != UNSORTED else []) + ["new queue…"]
            i = choose(scr, "move to", opts)
            if i is not None:
                name = opts[i] if i < len(opts) - 1 else prompt(scr, "new queue: ")
                if name:
                    put(state, q[sel], name)
                    save(state)
                    msg = f"moved to {name}"
            drawn = None
        elif isinstance(c, str) and c in "123456789" and q and int(c) <= len(state["queues"]):
            name = state["queues"][int(c) - 1]
            put(state, q[sel], name)
            save(state)
            msg = f"moved to {name}"
        elif c == "n":
            name = prompt(scr, "new queue: ")
            if name and name != UNSORTED and name not in state["queues"]:
                state["queues"].append(name)
                save(state)
                cur = len(state["queues"]) - 1
            drawn = None
        elif c == "r" and qs[cur] != UNSORTED:
            name = prompt(scr, "rename queue to: ", qs[cur])
            if name and name != UNSORTED and name not in state["queues"]:
                rename(state, qs[cur], name)
                save(state)
            drawn = None
        elif c == "d" and qs[cur] != UNSORTED:
            if prompt(scr, f"delete queue {qs[cur]}? its agents go to unsorted (y/N) ").lower() == "y":
                delete(state, qs[cur])
                save(state)
            drawn = None
        elif c in ("<", ">") and qs[cur] != UNSORTED:
            cur = shift(state, qs[cur], -1 if c == "<" else 1)
            save(state)
            drawn = None


def main(cmd):
    if cmd in ("ui", "standalone"):
        curses.wrapper(ui)
    elif cmd == "jump":
        time.sleep(0.3)  # let the list close first
        jump(*sys.argv[2:4])
    elif cmd == "watch":
        watch()
    elif cmd == "open":
        with socket.socket(socket.AF_UNIX) as sk:
            sk.connect(os.environ["HERDR_SOCKET_PATH"])
            sk.sendall((json.dumps({"id": "queues", "method": "plugin.pane.open", "params": {
                "plugin_id": os.environ["HERDR_PLUGIN_ID"], "entrypoint": "ui", "focus": True}}) + "\n").encode())
            resp = json.loads(sk.makefile().readline())
        if "error" in resp:
            raise RuntimeError(f"plugin.pane.open: {resp['error']}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "")
    except RuntimeError as e:
        sys.exit(f"herdr-queues: {e}")
