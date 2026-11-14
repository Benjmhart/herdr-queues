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
import curses, json, os, re, shlex, socket, subprocess, sys, threading, time
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
HOST = socket.gethostname()
REMOTE_STATE = "~/.local/state/herdr/plugins/" + PLUGIN + "/queues.json"
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
            a["machine"], a["host"] = m, self.ms[m][0]
            a["workspace"] = self.names.get(m, {}).get(a["workspace_id"], a["workspace_id"])
        return ags

    def read(self, key, m, pane):
        s = " ".join(summary(screen(m, pane)).split())  # one line, runs of spaces (a status bar) collapsed
        with self.lock:
            self.summary[key] = s
            self.version += 1

    def run(self):
        while True:
            try:
                self.round()
            except Exception as e:  # a refresher that dies leaves the list frozen; log it and go again
                try:  # not stderr: it would draw over the list
                    with open(os.path.join(STATE_DIR, "errors.log"), "a") as f:
                        f.write(f"{time.strftime('%F %T')} refresh: {e!r}\n")
                except OSError:
                    pass
            time.sleep(2)

    def round(self):
        ags = [a for part in self.pool.map(self.fetch, self.ms) for a in part]
        with self.lock:
            self.agents, self.loaded = ags, True
            self.version += 1
            for a in ags:
                k = skey(a)
                if a.get("agent_status") in WAITING and k not in self.summary:
                    self.summary[k] = None  # asked for; read in parallel, shown when it lands
                    self.reads.submit(self.read, k, a["machine"], a["pane_id"])

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


def write(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE + ".tmp", "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(STATE + ".tmp", STATE)


def save(state):
    """Here, and down the request channel: the watcher on your desk keeps every machine's copy the same."""
    write(state)
    request("state", json.dumps(state, separators=(",", ":"), sort_keys=True))


def valid(state):
    return (isinstance(state, dict) and isinstance(state.get("queues"), list) and isinstance(state.get("members"), dict)
            and all(isinstance(q, str) for q in state["queues"])
            and all(isinstance(k, str) and isinstance(v, str) for k, v in state["members"].items()))


def keys(agent):
    """Membership keys, the same whichever machine's popup reads them (the state is shared). Pane ids change on
    restart, so: the agent's session (globally unique), and its host + terminal (as herdr-folders)."""
    h = agent.get("host") or HOST
    k = []
    if (agent.get("agent_session") or {}).get("value"):
        k.append("s:" + agent["agent_session"]["value"])
    if agent.get("terminal_id"):
        k.append(f"{h}/t:" + agent["terminal_id"])
    return k or [f"{h}/p:" + agent["pane_id"]]


def legacy(agent):
    """0.3-0.4.1 keys, relative to the server the popup ran on (another machine's carried its label). Read, never
    written."""
    k = []
    if (agent.get("agent_session") or {}).get("value"):
        k.append("s:" + agent["agent_session"]["value"])
    if agent.get("terminal_id"):
        k.append("t:" + agent["terminal_id"])
    k = k or ["p:" + agent["pane_id"]]
    m = agent.get("machine")
    return [m + "/" + x for x in k] if m else k


def queue_of(state, agent):
    for k in keys(agent) + legacy(agent):
        q = state["members"].get(k)
        if q in state["queues"]:
            return q
    return UNSORTED


def put(state, agent, queue):
    # ponytail: members is never pruned; a few bytes per agent ever sorted, prune if the file ever matters.
    if queue and queue != UNSORTED and queue not in state["queues"]:
        state["queues"].append(queue)
    for k in legacy(agent):
        state["members"].pop(k, None)
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


# --- the sidebar's order (Ben 2026-11-14: "resortable by workspace order", "resortable by herdr-queues order") ----
# `s` in the popup flips every machine's workspace order between the queues' order and your own. Your own is saved
# (state["order"], per host, workspace ids) when it is left, and put back exactly; workspaces made since go last.
# herdr's agent panel (ui.agent_panel_sort = "spaces") follows the workspace order, so it moves too. workspace.move is
# a socket call the CLI lacks: run here, or over ssh on another machine (not one this one can't reach, e.g. micro).

MOVE = r'''
import json, os, socket, sys
P = os.environ.get("HERDR_SOCKET_PATH") or os.path.expanduser("~/.config/herdr/herdr.sock")
for i, w in enumerate(json.loads(sys.argv[1])):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(P)
        s.sendall((json.dumps({"id": "q", "method": "workspace.move", "params": {"workspace_id": w, "insert_index": i}}) + "\n").encode())
        s.makefile().readline()
'''


def by_queues(state, agents, ids):
    """Workspace ids in the queues' order: by the best (queue, status) of the agents in each; then the rest as they were."""
    rank = {"blocked": 0, "done": 1, "idle": 2, "working": 3}
    best = {}
    for a in agents:
        q = queue_of(state, a)
        k = (state["queues"].index(q) if q in state["queues"] else len(state["queues"]), rank.get(a.get("agent_status"), 4))
        w = a.get("workspace_id")
        if w in ids and k < best.get(w, (99, 99)):
            best[w] = k
    return sorted(ids, key=lambda w: (best.get(w, (99, 99)), ids.index(w)))


def restored(saved, ids):
    """Your saved order, then workspaces made since (in their current order)."""
    return [w for w in saved if w in ids] + [w for w in ids if w not in saved]


def workspace_ids(label):
    return [w["workspace_id"] for w in json.loads(herdr(label, "workspace", "list", timeout=5))["result"]["workspaces"]]


def apply_order(label, target, order):
    cmd = [sys.executable, "-c", MOVE, json.dumps(order)] if not target else [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target,
        "python3 -c " + shlex.quote(MOVE) + " " + shlex.quote(json.dumps(order))]
    subprocess.run(cmd, capture_output=True, timeout=20, check=True)


def resort(state, agents, ms):
    """Flip the sidebar on every machine; returns a message. Mutates state (save it after)."""
    to_queues = state.get("sort") != "queues"
    saved = state.setdefault("order", {})
    done, skipped = [], []
    for label, (host, target) in ms.items():
        try:
            ids = workspace_ids(label)
            if to_queues:
                saved[host] = ids  # your order, as it is now
                order = by_queues(state, [a for a in agents if a.get("host") == host], ids)
            else:
                order = restored(saved.get(host, ids), ids)
            if order != ids:
                apply_order(label, target, order)
            done.append(host)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
            skipped.append(host)
    state["sort"] = "queues" if to_queues else "workspaces"
    return (f"sidebar: {'queue' if to_queues else 'your workspace'} order on {len(done)} machine(s)"
            + (f"; not reached: {', '.join(skipped)}" if skipped else ""))


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


def request(*fields):
    """One line to the watcher: "<host> <pane>" (jump) or "state <json>" (the queues changed)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(REQUESTS, "a") as f:
        f.write(" ".join(fields) + "\n")
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


def at_desk(host):
    """Act only where you are: a watcher runs on every desktop, but only the one whose focused window is a herdr window
    showing the requesting machine, on an unlocked screen, carries out a jump (keys typed elsewhere could land in a
    lock screen's password box)."""
    try:
        if subprocess.run(["pgrep", "-x", "-u", str(os.getuid()), "xsecurelock|i3lock|slock|swaylock|xlock"],
                          capture_output=True).returncode == 0:
            return False
    except OSError:
        pass
    active = x11("getactivewindow").strip()
    return any(w == active for w, h in herdr_windows({host}))


SYNC = threading.Lock()


def ssh(target, *args, **kw):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, *args],
                          capture_output=True, text=True, timeout=30, **kw)


def push(state, ms, skip=None):
    """The queues to this machine and every other one (except skip, which sent them), in parallel."""
    text = json.dumps(state, indent=1, sort_keys=True)
    with SYNC:
        if skip != "":
            write(state)
        sh = f'mkdir -p "$(dirname {REMOTE_STATE})" && cat > {REMOTE_STATE}.tmp && mv {REMOTE_STATE}.tmp {REMOTE_STATE}'
        targets = [t for lab, (_, t) in ms.items() if t and lab != skip]
        with ThreadPoolExecutor(max(1, len(targets))) as ex:
            list(ex.map(lambda t: ssh(t, sh, input=text), targets))


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
            if ln.startswith("state "):
                try:
                    state = json.loads(ln[6:])
                    if valid(state):
                        push(state, ms, skip=label)
                except (ValueError, OSError, subprocess.TimeoutExpired):
                    pass
                continue
            f = ln.split()
            if len(f) == 2 and f[0] in by_host and PANE.match(f[1]) and at_desk(shown_by):
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
    if os.path.exists(STATE):  # this desk's queues are the master copy: every machine starts from them
        threading.Thread(target=push, args=(load(), ms, ""), daemon=True).start()
    for label in ms:
        threading.Thread(target=follow, args=(label, ms), daemon=True).start()
    threading.Thread(target=nag, args=(ms,), daemon=True).start()
    while True:
        time.sleep(3600)


# How long an agent may wait on you before a desktop notification says so. blocked = it asked something (a permission
# or a question). done = it finished its turn and you haven't looked: a /loop sleeping until its next wakeup is "done"
# too, for up to an hour (ScheduleWakeup's limit), so done waits longer than that. Viewing the agent (idle) ends it.
NAG = {"blocked": 10 * 60, "done": 65 * 60}


def nag_due(seen, ags, now):
    """One round: update seen {key: [status, since, told]} from ags [(key, status, name)], return the names now due.
    An agent already waiting when first seen isn't told about (an old finished session isn't news); each wait is told
    once."""
    due = []
    for k, st, name in ags:
        old = seen.get(k)
        if old is None:
            seen[k] = [st, now, True]
        elif old[0] != st:
            seen[k] = [st, now, False]
        elif not old[2] and st in NAG and now - old[1] >= NAG[st]:
            old[2] = True
            due.append(f"{name}: {st} {int((now - old[1]) // 60)} min")
    return due


UNSENT_AFTER = 5 * 60  # a reply typed into an idle agent's prompt and never sent (twice on 2026-11-14)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def typed(screen_ansi):
    """The text sitting in a Claude Code prompt, "" if none. Its prompt is "❯" + a no-break space (a shell prompt has a
    plain space); a suggested prompt is drawn dim (ESC[2m) and isn't anything typed."""
    for line in reversed(screen_ansi.splitlines()):
        if "❯\u00a0" not in line:
            continue
        rest = line.split("❯\u00a0", 1)[1].rstrip("\r")
        if rest.replace("\x1b[0m", "").startswith("\x1b[2m"):
            return ""
        return ANSI.sub("", rest).strip()
    return ""


def unsent_due(seen, rows, now):
    """rows [(key, text, name)]: the same text left in a prompt UNSENT_AFTER is due, once. Text that changes (still
    being typed) starts the clock again; an empty prompt clears it."""
    due = []
    for k, text, name in rows:
        old = seen.get(k)
        if not text:
            seen.pop(k, None)
        elif old is None or old[0] != text:
            seen[k] = [text, now, False]
        elif not old[2] and now - old[1] >= UNSENT_AFTER:
            old[2] = True
            due.append(f"{name}: unsent reply: {text[:60]}")
    return due


def nag(ms):
    """Tell you, on this desk, about agents on any machine left waiting on you (station-maintenance fleet 30)."""
    seen, unsent = {}, {}
    while True:
        ags, prompts = [], []
        for m in ms:
            try:
                for a in json.loads(herdr(m, "agent", "list", timeout=4))["result"]["agents"]:
                    name = f"{ms[m][0]} {a.get('terminal_title_stripped') or a['pane_id']}"
                    ags.append(((m, a["pane_id"]), a.get("agent_status"), name))
                    if a.get("agent_status") in ("idle", "done"):
                        try:
                            scr = herdr(m, "agent", "read", a["pane_id"], "--source", "visible", "--ansi", timeout=4)
                        except (RuntimeError, OSError, subprocess.TimeoutExpired):
                            scr = ""
                        prompts.append(((m, a["pane_id"]), typed(scr), name))
            except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                pass
        now = time.time()
        due = nag_due(seen, ags, now) + unsent_due(unsent, prompts, now)
        if due:
            try:
                subprocess.run(["notify-send", "-a", "herdr-queues", "Agents waiting on you (mod+shift+s)", "\n".join(due)],
                               timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass
        time.sleep(60)


# --- the list ---------------------------------------------------------------------------------------------------

HELP = ("←→/h l/tab queue · ↑↓/j k pgup pgdn g G · enter: go to agent · m: move to queue · 1-9: move to that queue · a: waiting only/all\n"
        "queues: n new · r rename · d delete · </> reorder · s: sidebar by queues / your order · q quit")
ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}
NAMED = {curses.KEY_NPAGE: "pgdn", curses.KEY_PPAGE: "pgup", curses.KEY_HOME: "home", curses.KEY_END: "end",
         curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
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
        if seq[:1] in ("[", "O"):
            return {"5~": "pgup", "6~": "pgdn", "H": "home", "F": "end", "1~": "home", "4~": "end"}.get(
                seq[1:], ARROWS.get(seq[-1:], "esc"))
        return "esc"
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
    request(agent["host"], agent["pane_id"])  # `queues.py watch` on your desk carries it out
    return None


# Dracula, the fleet's palette (on 256 colours; plain ANSI otherwise). No A_DIM: unreadable on a translucent terminal.
PALETTE = {"blocked": (210, 1), "done": (84, 2), "working": (228, 3), "idle": (103, 8), "machine": (141, 5),
           "workspace": (117, 6), "summary": (188, 7), "help": (103, 8), "tab": (236, 0), "tabbg": (141, 5),
           "select": (231, 7), "selectbg": (239, 4)}
C = {}


def colours():
    curses.use_default_colors()
    if not curses.has_colors():
        return
    big = curses.COLORS >= 256
    pick = lambda name: PALETTE[name][0 if big else 1]
    pairs = {"blocked": ("blocked", -1), "done": ("done", -1), "working": ("working", -1), "idle": ("idle", -1),
             "machine": ("machine", -1), "workspace": ("workspace", -1), "summary": ("summary", -1),
             "help": ("help", -1), "tab": ("tab", "tabbg"), "select": ("select", "selectbg")}
    for n, (name, (fg, bg)) in enumerate(pairs.items(), 1):
        try:
            curses.init_pair(n, pick(fg), -1 if bg == -1 else pick(bg))
            C[name] = curses.color_pair(n)
        except curses.error:
            pass


def put_str(scr, y, x, text, attr=0):
    """addstr that clips to the screen and never raises at the last cell."""
    h, w = scr.getmaxyx()
    if 0 <= y < h and x < w - 1:
        try:
            scr.addstr(y, x, text[:w - 1 - x], attr)
        except curses.error:
            pass
    return x + len(text)


def ui(scr):
    os.environ.setdefault("ESCDELAY", "25")
    curses.curs_set(0)
    colours()
    scr.timeout(250)
    ms = machines()
    live = Live(ms)
    # Every agent in the queue, waiting ones first (Ben 2026-11-14: a queue is worked through, so all of it shows);
    # `a` narrows it to the ones waiting on you. The number on a tab counts the waiting ones.
    state, cur, sel, everyone, msg, drawn, top = load(), None, 0, True, "", None, 0

    while True:
        agents, sums, version = live.snapshot()
        qs = tabs(state, agents)
        if cur is None and live.loaded:  # open on the first queue with something waiting
            cur = next((i for i, s in enumerate(qs) if members(state, agents, s)), 0)
        if cur is not None:
            cur %= len(qs)
        q = members(state, agents, qs[cur or 0], everyone)
        sel = max(0, min(sel, len(q) - 1))
        h, w = scr.getmaxyx()
        rows = max(1, h - 5)  # tabs, two help lines, a blank, the list, the message line
        top = min(max(top, sel - rows + 1), sel, max(0, len(q) - rows))  # keep the selection in view
        top = max(0, top)
        if drawn != (version, cur, sel, everyone, msg, len(state["queues"]), top, h, w):
            drawn = (version, cur, sel, everyone, msg, len(state["queues"]), top, h, w)
            scr.erase()
            x = 0
            for i, s in enumerate(qs):
                n = len(members(state, agents, s))
                t = f" {s} {n} " if n else f" {s} "
                x = put_str(scr, 0, x, t, (C.get("tab", curses.A_REVERSE) if i == (cur or 0) else 0)
                            | (curses.A_BOLD if n else 0)) + 1
            for i, ln in enumerate(HELP.splitlines()):
                put_str(scr, 1 + i, 0, ln, C.get("help", 0))
            if not live.loaded:
                put_str(scr, 4, 2, f"loading {len(ms)} machines…", C.get("help", 0))
            elif not q:
                put_str(scr, 4, 2, "no agents in this queue (m on an agent in another queue moves it here)" if everyone
                        else "nothing waiting on you here (a: show all its agents)")
            for i, a in enumerate(q[top:top + rows]):
                row, st = 4 + i, a.get("agent_status")
                title = a.get("terminal_title_stripped") or os.path.basename(a.get("cwd", ""))
                s = sums.get(skey(a)) if st in WAITING else ""
                if i + top == sel:  # the selected line: one solid bar
                    line = f" {ICON.get(st, '?')} {(a['machine'] + ' · ') if a['machine'] else ''}{a['workspace']}  {title}"
                    line += f"  — {s if s is not None else '…'}" if st in WAITING else ""
                    put_str(scr, row, 0, line.ljust(w - 1), C.get("select", curses.A_REVERSE) | curses.A_BOLD)
                    continue
                x = put_str(scr, row, 1, ICON.get(st, "?"), C.get(st, 0) | curses.A_BOLD) + 1
                if a["machine"]:
                    x = put_str(scr, row, x, a["machine"], C.get("machine", 0)) 
                    x = put_str(scr, row, x, " · ", C.get("help", 0))
                x = put_str(scr, row, x, a["workspace"], C.get("workspace", 0) | curses.A_BOLD) + 2
                x = put_str(scr, row, x, title, curses.A_BOLD if st in WAITING else 0)
                if st in WAITING:
                    put_str(scr, row, x + 2, "— " + (s if s is not None else "…"), C.get("summary", 0))
            if len(q) > rows:  # where you are in a long list
                put_str(scr, h - 1, max(0, w - 12), f"{top + 1}-{min(len(q), top + rows)}/{len(q)}".rjust(11),
                        C.get("help", 0))
            if msg:
                put_str(scr, h - 1, 0, msg, C.get("summary", 0))
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
        elif c in ("pgdn", "pgup"):
            sel += (1 if c == "pgdn" else -1) * max(1, scr.getmaxyx()[0] - 6)
        elif c in ("home", "g"):
            sel = 0
        elif c in ("end", "G"):
            sel = len(q) - 1
        elif c == "a":
            everyone = not everyone
        elif c == "s":
            msg = "sorting the sidebar…"
            put_str(scr, scr.getmaxyx()[0] - 1, 0, msg)
            scr.refresh()
            msg = resort(state, agents, ms)
            save(state)
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
                "plugin_id": os.environ["HERDR_PLUGIN_ID"], "entrypoint": "ui", "focus": True,
                "width": "98%", "height": "96%"}}) + "\n").encode())
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
