#!/usr/bin/env python3
"""herdr-queues: your own queues (personal, work, ...) of the agents waiting on you, on every machine herdr knows,
each with one line of what it needs. Enter jumps to the agent, switching the herdr window to its machine.

  queues.py ui           the popup (the plugin's pane)
  queues.py open         opens the popup (the plugin's action)
  queues.py open --search  opens it in search mode (the search action)
  queues.py standalone   the same list outside herdr (a desktop key); jumps directly
  queues.py watch        run on the machine you sit at: carries out jumps asked for by a popup on any machine
  queues.py jump M P     switch the herdr window to machine M and focus pane P
  queues.py icon Q [E]   icon E for queue Q in herdr's sidebar ($queues_queue); no E clears it
  queues.py refresh      this machine's sidebar tokens now (herdr runs it on rename and new agents)
  queues.py color Q C    queue Q's colour (red orange yellow green cyan purple pink grey, none); colorby queue|machine
  queues.py host H icon E | host H color C   a machine's emoji (overrides herdr-machine-icons) and colour
  queues.py host H spin N|off|default         machine H's spinner frames a second (default 1)
  queues.py headings queue|machine|off;  collapse Q;  mark own|herdr;  spinner on|off;  badge model|gitstatus V [E]
  queues.py status NAME GLYPH|default;  status NAME color COLOUR|none;  status spin 'F1 F2 ...'|default   (NAME: blocked draft asks asks_seen done done_seen background idle working unknown revisit pin lowctx)
  queues.py context N|off    flag (🪫) an agent with less than N % of its context left; off by default
  queues.py revisit|pin PANE|here on|off   🔖 come back to it (cleared when you focus it; v) · 📌 stays (V)
  queues.py rows         herdr's [ui.sidebar.agents] for the mark style (colours are settings: it never changes)
  queues.py spin         the spinner (herdr starts it with the server)
  queues.py entries on|off   experimental: each queue heading an entry of its own; clicking it collapses the queue
  queues.py lane PANE NAME|none   tag one agent (PANE "here": this pane), shown before its workspace
  queues.py label [F]    the sidebar's $queues_label template, e.g. "{queue}{machine} {workspace} {folder}" (any pane
                         token by name); "default" resets

Standard library only. Data loads in a background thread, so keys never wait on the network.
"""
import glob, json, os, re, shlex, socket, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
try:
    import curses
except ImportError:  # Windows Python ships none: the popup needs `py -m pip install windows-curses`, the rest runs
    curses = None

WIN = os.name == "nt"  # native Windows: herdr's socket is a named pipe; no fcntl, sh, tail or getuid
UID = os.getuid() if hasattr(os, "getuid") else 0

# Windows before Python 3.15 reads files and subprocess output as cp1252, and herdr's JSON is full of emoji: run again in
# UTF-8 mode (a child, not exec: herdr waits on this process).
if os.name == "nt" and not sys.flags.utf8_mode and __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-X", "utf8", *sys.argv]))

if WIN:  # no console window flashing up for each child of a windowless (pythonw) desk process; none needs one
    _popen = subprocess.Popen.__init__
    subprocess.Popen.__init__ = lambda self, *a, creationflags=0, **k: _popen(
        self, *a, creationflags=creationflags | subprocess.CREATE_NO_WINDOW, **k)

PLUGIN = "benjmhart.queues"
STATE_ROOT = (os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")) if os.name != "nt" else \
    os.environ["LOCALAPPDATA"]  # where herdr puts plugin state on Windows (HERDR_PLUGIN_STATE_DIR, herdr 0.9.5)
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.join(STATE_ROOT, "herdr/plugins", PLUGIN)
STATE = os.path.join(STATE_DIR, "queues.json")
OLD_STATE = os.path.join(STATE_ROOT, "herdr/plugins/benjmhart.streams/streams.json")  # herdr-streams <= 0.3
REQUESTS = os.path.join(STATE_DIR, "jump")  # "<host> <pane>" lines, followed by `queues.py watch`
REMOTE_REQUESTS = "~/.local/state/herdr/plugins/" + PLUGIN + "/jump"
HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
STANDALONE = "HERDR_PLUGIN_ID" not in os.environ
HOST = socket.gethostname()
REMOTE_STATE = "~/.local/state/herdr/plugins/" + PLUGIN + "/queues.json"
try:  # a regex, not tomllib: macOS's own python3 is 3.9, and tomllib is 3.11+
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "herdr-plugin.toml"), encoding="utf-8") as _f:
        VERSION = re.search(r'^version = "([^"]+)"', _f.read(), re.M).group(1)
except (OSError, AttributeError):
    VERSION = "?"
HOSTS = os.path.join(STATE_DIR, "hosts.json")  # {ssh target: that machine's hostname}, learned by the watcher
RELAY = os.path.join(STATE_DIR, "relay")  # <host>.json: agents of a machine this one can't reach (micro), sent by it
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


def machines(learn=False):
    """{label: (hostname, ssh target)} of the enabled machines; "" is this server (no ssh target). The hostname is what
    that machine's own HOST is, so membership keys and jump requests agree whichever machine wrote them: asked once
    over ssh (`uname -n`, by the watcher: learn=True) and cached in HOSTS. Until then it is guessed from the target
    ("user@beast-arch.lan" -> "beast-arch"), wrong for an alias or an IP (micro's review, 2026-11-16).
    ponytail: cached for good; delete hosts.json after renaming a machine."""
    out, targets = {"": (HOST, None)}, {}
    try:
        for ln in herdr(None, "machine", "list").splitlines():
            f = ln.split("\t")
            if len(f) >= 5 and f[4] == "enabled":
                targets[f[1]] = f[2]
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        pass
    try:
        with open(HOSTS) as fh:
            known = json.load(fh)
    except (OSError, ValueError):
        known = {}
    new = [t for t in set(targets.values()) if t not in known] if learn else []
    if new:
        with ThreadPoolExecutor(len(new)) as ex:
            for t, name in zip(new, ex.map(hostname_of, new)):
                if name:
                    known[t] = name
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(HOSTS + ".tmp", "w") as fh:
            json.dump(known, fh, indent=1, sort_keys=True)
        os.replace(HOSTS + ".tmp", HOSTS)
    for lab, t in targets.items():
        out[lab] = (known.get(t) or t.split("@")[-1].split(".")[0], t)
    return out


def hostname_of(target):
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, "uname -n"],
                           capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = r.stdout.strip()
    return name if r.returncode == 0 and re.fullmatch(r"[\w.-]{1,64}", name) else None


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
            ags = real(json.loads(herdr(m, "agent", "list", timeout=4))["result"]["agents"])
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
        ags = [a for part in self.pool.map(self.fetch, self.ms) for a in part] + relayed(self.ms)
        with self.lock:
            self.agents, self.loaded = ags, True
            self.version += 1
            for a in ags:
                k = skey(a)
                if "summary" in a:  # relayed: read on its own machine
                    self.summary[k] = a["summary"]
                elif a.get("agent_status") in WAITING and k not in self.summary:
                    self.summary[k] = None  # asked for; read in parallel, shown when it lands
                    self.reads.submit(self.read, k, a["machine"], a["pane_id"])

    def snapshot(self):
        with self.lock:
            return list(self.agents), dict(self.summary), self.version


def relayed(ms):
    """Agents of machines this server can't reach that sent their list here (fresh in the last minute): micro is
    outbound-only, so only its own watcher can tell the others what runs there (Ben 2026-11-15)."""
    known, out = {h for h, _ in ms.values()}, []
    try:
        names = os.listdir(RELAY)
    except OSError:
        return out
    for n in names:
        p = os.path.join(RELAY, n)
        try:
            if n[:-5] in known or not n.endswith(".json") or time.time() - os.path.getmtime(p) > 60:
                continue
            with open(p) as f:
                for a in json.load(f):
                    a["machine"] = a["host"] = n[:-5]
                    out.append(a)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return out


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
        for _ in range(10):  # an unreadable copy is retried, never taken for "no state": the default has no headings,
            try:             # so one bad read closed every heading entry (beast-arch 2026-11-22 09:36)
                with open(path) as f:
                    s = json.load(f)
                if "streams" in s:  # herdr-streams' file
                    s = {"queues": s["streams"], "members": s["members"]}
                return s
            except FileNotFoundError:
                break
            except ValueError:
                time.sleep(0.1)
        else:
            raise RuntimeError("unreadable state: " + path)
    return {"queues": ["personal", "work"], "members": {}}


# Every copy carries the time it was saved, and no copy is replaced by an older one (Ben 2026-11-15: J/K order lost).
# Without it the last writer won: a watcher starting pushes its own copy to every machine, and the four fpush restarted
# together went out in a race; a popup left open saved what it read when it opened.
KEEP_NEWEST = r'''
import json, os, sys
p = os.path.expanduser(sys.argv[1]); new = sys.stdin.read()
try:
    old = json.load(open(p)).get("saved", 0)
except (OSError, ValueError, AttributeError):
    old = 0
if json.loads(new).get("saved", 0) >= old:
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".%d.tmp" % os.getpid()  # one name per writer: a shared one let two writers swap in a half-written file
    open(tmp, "w").write(new)
    os.replace(tmp, p)
'''


def write(state):
    subprocess.run([sys.executable, "-c", KEEP_NEWEST, STATE], input=json.dumps(state, indent=1, sort_keys=True),
                   text=True, timeout=10)


def save(state):
    """Here, and down the request channel: the watcher on your desk keeps every machine's copy the same."""
    state["saved"] = time.time()
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


def folder(agent):
    """Queue membership only, tried last: host + folder outlives a restart into a new pane followed by /clear, which
    changes both keys (micro, 2026-11-16: lost to unsorted). Not identity: two agents can share a folder (quiet, inactive
    and J/K order stay on keys())."""
    return [f"{agent.get('host') or HOST}/d:" + agent["cwd"]] if agent.get("cwd") else []


def learn(state, agents):
    """A filed agent's keys that aren't stored yet (a new session after /clear or a resume, a new pane, the folder key
    for agents filed before it existed), so the next change still finds one. Only absent keys: two agents in one folder
    in different queues would otherwise take the folder key back and forth, a save each time. True if any were added.
    ponytail: the folder goes to whichever was filed there first; filing one by hand (put) takes it."""
    m, new = state["members"], False
    for a in agents:
        qn = queue_of(state, a)
        if qn != UNSORTED:
            for k in keys(a) + folder(a):
                if k not in m:
                    m[k], new = qn, True
    return new


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
    for k in keys(agent) + legacy(agent) + folder(agent):
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
    for k in keys(agent) + folder(agent):
        if queue and queue != UNSORTED:
            state["members"][k] = queue
        else:
            state["members"].pop(k, None)


def rename(state, old, new):
    state["queues"] = [new if x == old else x for x in state["queues"]]
    state["members"] = {k: new if q == old else q for k, q in state["members"].items()}
    if old in state.get("rank", {}):
        state["rank"][new] = state["rank"].pop(old)


def delete(state, name):
    """Its agents go back to unsorted."""
    state["queues"].remove(name)
    state["members"] = {k: q for k, q in state["members"].items() if q != name}
    state.get("rank", {}).pop(name, None)


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


def position(state, queue, agent):
    """Where you put the agent in the queue (J/K), None if you haven't."""
    order = state.get("rank", {}).get(queue, [])
    return min((order.index(k) for k in keys(agent) if k in order), default=None)


def members(state, live, queue, everyone=False):
    """The queue's agents: your order first (J/K), then the rest blocked, done, idle, working; inactive ones last
    (Ben 2026-11-14). Without everyone, only the waiting ones. Unsorted always lists everyone: it is where agents are
    filed from."""
    rank = {"blocked": 0, "done": 1, "idle": 2, "working": 3}
    everyone = everyone or queue == UNSORTED
    mine = [a for a in live if queue_of(state, a) == queue and (everyone or a.get("agent_status") in WAITING)]

    def order(a):
        p = position(state, queue, a)
        return (inactive(state, a), p is None, p or 0, rank.get(a.get("agent_status"), 4), a.get("machine", ""),
                a.get("workspace", ""))
    return sorted(mine, key=order)


def move(state, live, queue, agent, d, everyone=True):
    """J/K: swap the agent with its neighbour below (d=1) or above (d=-1) in the list as shown. The queue's whole
    order is written out, so agents you never placed keep the place they had. Returns the agent's new index."""
    shown = members(state, live, queue, everyone)
    i = next(n for n, a in enumerate(shown) if keys(a) == keys(agent))
    j = max(0, min(len(shown) - 1, i + d))
    full = members(state, live, queue, everyone=True)
    fi, fj = (next(n for n, a in enumerate(full) if keys(a) == keys(shown[x])) for x in (i, j))
    full[fi], full[fj] = full[fj], full[fi]
    state.setdefault("rank", {})[queue] = [keys(a)[0] for a in full]
    return j


# --- the sidebar's order (Ben 2026-11-14: "resortable by workspace order", "resortable by herdr-queues order") ----
# `s` in the popup flips every machine's workspace order between the queues' order and your own. Your own is saved
# (state["order"], per host, workspace ids) when it is left, and put back exactly; workspaces made since go last.
# herdr's agent panel (ui.agent_panel_sort = "spaces") follows the workspace order, so it moves too. workspace.move is
# a socket call the CLI lacks: run here, or over ssh on another machine (not one this one can't reach, e.g. micro).

# One request to this machine's herdr: a Unix socket, or on Windows a named pipe (herdr's socket API docs). A string,
# because CALLS runs it over ssh on other machines too.
RPC = r'''
import json, os, socket
def herdr_path():
    p = os.environ.get("HERDR_SOCKET_PATH")
    if os.name == "nt":  # a path-shaped name in the pipe namespace (herdr 0.9.5): \\.\pipe\C:\...\herdr.sock
        p = p or os.path.join(os.environ["APPDATA"], "herdr", "herdr.sock")
        return p if p.startswith("\\\\.\\pipe\\") else "\\\\.\\pipe\\" + p
    return p or os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "herdr/herdr.sock")
def rpc(method, params, rid="q", timeout=3):
    line = (json.dumps({"id": rid, "method": method, "params": params}) + "\n").encode()
    if os.name == "nt":
        with open(herdr_path(), "r+b", buffering=0) as f:
            f.write(line)
            return json.loads(f.readline())
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(herdr_path())
        s.sendall(line)
        return json.loads(s.makefile().readline())
'''
exec(RPC)
CALLS = RPC + r'''
import sys
for method, params in json.loads(sys.argv[1]):
    rpc(method, params)
'''


def call(target, reqs):
    """[method, params] socket requests on one machine's server: here (target None) or over ssh."""
    arg = json.dumps(reqs)
    cmd = [sys.executable, "-c", CALLS, arg] if not target else [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, "python3 -c " + shlex.quote(CALLS) + " " + shlex.quote(arg)]
    subprocess.run(cmd, capture_output=True, timeout=20, check=True)


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
    call(target, [["workspace.move", {"workspace_id": w, "insert_index": i}] for i, w in enumerate(order)])


# --- inactive agents (Ben 2026-11-14: "ability to mark agents inactive, which removes them from the sidebar but keeps
# them in herdr-queues") -------------------------------------------------------------------------------------------
# `i` in the popup toggles it. An inactive agent's pane carries the token queues_inactive on its own server, and every
# server gets an agent view hiding panes with that token: in a saved-machine client the selected server's view applies
# to the combined list, so a token is the one mark that can't hit another machine's pane of the same id. Views and
# tokens end with the server, so `queues.py watch` puts them back every minute (and follows a restarted agent's new
# pane). agent.list and the popup still show everything.

TOKEN = "queues_inactive"
SOURCE = "user:herdr-queues"
VIEW = {"source": SOURCE, "filter": {"op": "not", "filter": {"op": "exists", "field": {"token": TOKEN}}}}


def view(state, host):
    """The agent view for one server. With heading entries the list is one list for every machine: sorted by the
    queues_rank token (queue, your order, then status in auto), and the headings shown are the viewing server's own,
    each server keeping a set (2026-11-16 two-server sandbox: the selected server's view filters and sorts the combined
    list; a herdr agent sort interleaves machines, workspace order can't)."""
    if not entries_on(state):
        return VIEW
    ex = lambda t: {"op": "exists", "field": {"token": t}}
    return {"source": SOURCE, "filter": {"op": "all", "filters": [
        {"op": "not", "filter": ex(TOKEN)},
        {"op": "any", "filters": [{"op": "not", "filter": ex("queues_heading_host")},
                                  {"op": "eq", "field": {"token": "queues_heading_host"}, "value": host}]}]},
        "sort": [{"field": {"token": "queues_rank"}, "order": "asc"}]}


def entries_on(state):
    return state.get("sort") in ON and state.get("headings") == "queue" and bool(state.get("entries"))


def groups_of(state):
    """Every queue gets a heading, unsorted last: no one machine knows which queues have agents anywhere."""
    return list(state["queues"]) + [UNSORTED]


def rank_of(state, a=None, q=None):
    """queues_rank: a heading sorts first in its queue ("NNN-"), then agents by status (auto), your order, machine."""
    q = q if a is None else queue_of(state, a)
    gs = groups_of(state)
    qi = gs.index(q) if q in gs else len(gs)
    if a is None:
        return f"{qi:03d}-"  # a prefix of every agent's rank in the queue: sorts first
    p = position(state, q, a)
    # auto: the ones waiting on you float to the top of their queue, ahead of your J/K order (Ben 2026-11-16)
    st = NEED.get(need(a), 7) if state.get("sort") == "auto" else 0
    return f"{qi:03d}-{st}-{9999 if p is None else p + 1:04d}-{a.get('host', '')}-{a.get('workspace', '')}"


def inactive(state, agent):
    return any(k in state.get("inactive", []) for k in keys(agent))


def set_inactive(state, agent, on):
    k = set(state.get("inactive", [])) - set(keys(agent))
    state["inactive"] = sorted(k | set(keys(agent)) if on else k)


# Alerts, per agent (Ben 2026-11-14: "easy to add or remove notifications for a given agent the same as we mark them
# inactive"): `b` in the popup turns the watcher's notifications off for the selected agent, kept by its keys.

def quiet(state, agent):
    return any(k in state.get("quiet", []) for k in keys(agent))


def set_quiet(state, agent, on):
    k = set(state.get("quiet", [])) - set(keys(agent))
    state["quiet"] = sorted(k | set(keys(agent)) if on else k)


# Flags at the end of an agent's label (Ben 2026-11-19). revisit: set by hand, cleared when you next focus the agent
# ("it's meant to be a revisit flag"); pin: set by hand, stays until you clear it; lowctx: below `context N` % left.
# Every glyph is yours: `status revisit|pin|lowctx GLYPH|default`, like the marks.
FLAGS = {"revisit": "🔖", "pin": "📌", "lowctx": "🪫"}
HAND = ("revisit", "pin")  # the ones you set


def glyph(state, name):
    return ((state or {}).get("marks") or {}).get(name) or FLAGS.get(name) or MARKS.get(name, "")


def flagged(state, agent, name):
    return any(k in state.get(name, []) for k in keys(agent))


def set_flag(state, agent, name, on):
    k = set(state.get(name, [])) - set(keys(agent))
    state[name] = sorted(k | set(keys(agent)) if on else k)


def revisit(state, agent):
    return flagged(state, agent, "revisit")


def flags_of(state, a):
    """The agent's flags, in FLAGS order: [(name, glyph, words for the popup)]."""
    out = [(n, glyph(state, n), n) for n in HAND if flagged(state, a, n)]
    return out + ([("lowctx", glyph(state, "lowctx"), f"{context_left(a)}% context left")] if low_context(state, a) else [])


# Lanes (Ben 2026-11-18): a short tag of your own on one agent, to tell apart agents sharing a workspace ("urgent",
# "chores" on two mlabs-marketing sessions). Shown between the machine icon and the workspace name (Ben).
def lane(state, agent):
    return next((state["lanes"][k] for k in keys(agent) if k in state.get("lanes", {})), "")


def set_lane(state, agent, name):
    ls = {k: v for k, v in state.get("lanes", {}).items() if k not in keys(agent)}
    state["lanes"] = dict(ls, **{k: name for k in keys(agent)}) if name else ls


# Sidebar token (Ben 2026-11-15: "alert status - queue name - workspace name - machine name"). Every agent's pane
# carries its queue (queues_queue), as the queue's icon when it has one, for herdr's [ui.sidebar.agents] rows as
# "$queues_queue". herdr cuts a narrow row at its end, so a one-character icon leaves the width to the workspace name
# (Ben: "i'm good with queue emojis to save space"). `queues.py icon QUEUE EMOJI` sets one. The machine's icon is
# another plugin's job (herdr-machine-icons, "$machine_icon").
def icon(state, name):
    return state.get("icons", {}).get(name, name)


# queues_label: queue icon, machine icon, workspace name as ONE token, "🔥🐭 station-maintenance" (Ben 2026-11-15: no
# divider between the emoji and the name). herdr puts " · " between a row's tokens (none after state_icon) and has no
# setting for it, so the row is ["state_icon", "$queues_label"]. The machine icon is herdr-machine-icons' token on the
# same pane; a workspace rename shows within the watcher's minute.
# Its layout is yours: `queues.py label FORMAT` (synced to every machine like the queues), a template over the
# fields {queue} {machine} {workspace} {host} {agent} {title} AND every token on the agent's pane by name ({folder},
# {fold}, any plugin's). herdr's rows can't do this: a row is a list of tokens joined by " · ", and "$a$b" is refused.
# A field the pane doesn't have is empty; empty fields drop out with their spaces.
LABEL = "{queue}{machine} {workspace}"
PLAIN = "{workspace}"
FIELDS = ("queue", "machine", "workspace", "lane", "host", "agent", "title", "model", "gitstatus", "usage", "usage_pct",
          "flags")


def context_left(a):
    """% of context left from the agent's $usage token (used %), or None."""
    u = (a.get("tokens") or {}).get("usage")
    try:
        return 100 - int(u)
    except (TypeError, ValueError):
        return None


def low_context(state, a):
    """Ben 2026-11-19: flag an agent with less than `context N` % of its context left (his: 60)."""
    left, n = context_left(a), (state or {}).get("low_context")
    return n is not None and left is not None and left < n


class Blank(dict):
    def __missing__(self, key):
        return ""


def label(a, qi, fmt=LABEL, machine=None, lane="", flags=""):
    if flags and "{flags}" not in fmt:  # the flags show whatever your format, at its end
        fmt += " {flags}"
    if lane and "{lane}" not in fmt:  # a format of your own written before lanes still shows them
        fmt = fmt.replace("{workspace}", "{lane} {workspace}") if "{workspace}" in fmt else fmt + " {lane}"
    toks = {k: v for k, v in (a.get("tokens") or {}).items() if not k.startswith("queues_")}  # not its own
    f = Blank(toks, queue=qi or "", machine=machine or toks.get("machine_icon") or "",
              workspace=a.get("workspace") or a.get("workspace_id", ""), lane=f"[{lane}]" if lane else "",
              host=a.get("host", ""),
              agent=a.get("agent", ""), title=a.get("terminal_title_stripped", ""),
              usage_pct=f"{toks['usage']}%" if toks.get("usage") else "", flags=flags)
    try:
        out = fmt.format_map(f)
    except (ValueError, IndexError, AttributeError):  # a broken format shows the default, not nothing
        out = LABEL.format_map(f)
    return " ".join(out.split())


# --- info tokens (Ben 2026-11-15): $model, $gitstatus, $usage, each a small predictable set of values, written on the
# agent's own machine by `refresh` (herdr runs it as agents change). `queues.py badge TOKEN VALUE EMOJI` shows an emoji
# instead of a value. $usage is a bare number, so herdr's gt/lt rules can colour it; {usage_pct} in the label adds "%".
INFO = SOURCE + "-info"
MODELS = ("opus", "sonnet", "haiku", "fable")


def model_of(sid):
    """opus, sonnet, haiku, fable (or the raw id): the newest model in the Claude session's transcript."""
    import glob
    for path in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")):
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - 262144))
            found = re.findall(rb'"model":"([^"]+)"', fh.read())
        if found:
            m = found[-1].decode()
            return next((f for f in MODELS if f in m), m)
    return None


def git_of(cwd):
    """clean, dirty, ahead, behind, diverged, conflict; None outside a repository."""
    try:
        # --no-optional-locks: status otherwise takes index.lock to refresh the index, and a status killed by the timeout
        # (a loaded machine) left the lock behind, failing the agent's next commit (beast-arch, 2026-11-18, three times).
        r = subprocess.run(["git", "--no-optional-locks", "-C", cwd, "status", "--porcelain=v2", "--branch"],
                           capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired, TypeError):
        return None
    if r.returncode:
        return None
    lines = r.stdout.splitlines()
    if any(ln.startswith("u ") for ln in lines):
        return "conflict"
    ab = next((ln.split()[2:4] for ln in lines if ln.startswith("# branch.ab ")), ["+0", "-0"])
    ahead, behind = ab[0] != "+0", ab[1] != "-0"
    if ahead and behind:
        return "diverged"
    if any(not ln.startswith("#") for ln in lines):
        return "dirty"
    return "ahead" if ahead else "behind" if behind else "clean"


def usage_of(sid):
    """Context used, 0-100, from ~/.local/state/claude-ctx/<session> (remaining %, written by a Claude status line)."""
    try:
        with open(os.path.expanduser(f"~/.local/state/claude-ctx/{sid}")) as fh:
            return str(round(100 - float(fh.read().strip())))
    except (OSError, ValueError):
        return None


def info(a, state):
    sid = (a.get("agent_session") or {}).get("value")
    raw = {"model": model_of(sid) if sid else None, "gitstatus": git_of(a.get("foreground_cwd") or a.get("cwd")),
           "usage": usage_of(sid) if sid else None}
    b = state.get("badges", {})
    out = {k: (v if k == "usage" else b.get(k, {}).get(v, v)) if v else None for k, v in raw.items()}
    stopped = a.get("agent_status") in ("done", "idle")
    out["queues_asks"] = "1" if stopped and sid and asks(sid) else None
    out["queues_bg"] = "1" if stopped and a.get("agent") == "claude" and background(a["pane_id"]) else None
    out["queues_draft"] = "1" if stopped and a.get("agent") == "claude" and draft(a["pane_id"]) else None
    # idle or done: no dialog can be up (its event comes before Claude redraws, so the screen may still show it: Esc
    # in the sandbox kept the mark 11 s)
    out["queues_perm"] = "1" if a.get("agent") == "claude" and not stopped and perm_on(a["pane_id"], a.get("agent_status")) else None
    return out


def asks(sid):
    """True when the session's last reply ends on a question to you: it stopped for an answer, not with the job done."""
    import glob
    for path in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")):
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - 524288))
            lines = fh.read().splitlines()
        for ln in reversed(lines):
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("type") == "user" and not any(c.get("type") == "tool_result" for c in
                                                   (e.get("message") or {}).get("content") or [] if isinstance(c, dict)):
                return False  # you spoke after it
            if e.get("type") != "assistant":
                continue
            text = [c.get("text", "") for c in (e.get("message") or {}).get("content") or []
                    if isinstance(c, dict) and c.get("type") == "text"]
            if text:
                return asks_text(text[-1])
    return False


# A request without a "?" (confluence-consist 2026-11-16: "Reply with pass or fail for each check."): a phrase that asks
# wherever it stands, or a sentence that opens by asking you to do something.
ASK = re.compile(r"\b(?:tell me|let me know|reply with|send me|your call)\b|(?:^|[.!:]\s+|\n\s*(?:[-*]|\d+\.)?\s*)"
                 r"(?:(?:then|now|please|so),?\s+)*(?:reply|run|confirm|choose|pick|approve|paste|answer|decide|type)\b", re.I)
CODE = re.compile(r"^(?:```|    |\t)")


def asks_text(text):
    """The last paragraph of a reply asks something: a question, or a request to you. A command block at the end (an
    agent's "Run:" followed by the command) is skipped for the prose before it."""
    paras = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    while len(paras) > 1 and CODE.match(paras[-1]):
        paras.pop()
    last = paras[-1] if paras else ""
    return "?" in last or bool(ASK.search(last))


BG = re.compile(r"· \d+ (shells?|background tasks?)\b")


RULE = re.compile(r"^\s*─")


def draft(pane):
    """A prompt typed into the agent's box and not sent (Ben 2026-11-19): the text between the box's two rules, Claude
    Code's dim (SGR 2) suggestion dropped, the ❯ mark and blanks stripped."""
    try:
        ansi = herdr(None, "agent", "read", pane, "--source", "visible", "--ansi", timeout=4)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False
    lines = [re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", re.sub(r"\x1b\[2m[^\x1b\n]*", "", ln)) for ln in ansi.split("\n")]
    r = [i for i, ln in enumerate(lines) if RULE.match(ln)]
    if len(r) < 2:
        return False
    return any(ln.replace("\r", "").strip().removeprefix("❯").replace("\xa0", " ").strip() for ln in lines[r[-2] + 1:r[-1]])


# Claude Code's permission dialog ("Do you want to proceed?" over "❯ 1. Yes / 2. ... / 3. No"). herdr reads it as
# blocked, but not always (Ben 2026-11-19: "under some circumstances"): the screen is the fallback. Only the bottom of
# the screen counts, where the dialog stands in for the input box, so a quoted one in the conversation doesn't.
PERM_Q = re.compile(r"^\W*Do you want to .*\?\s*$", re.M)
PERM_YES = re.compile(r"^\W*1\. Yes\b", re.M)


def permission(pane):
    try:
        text = herdr(None, "agent", "read", pane, "--source", "visible", timeout=4)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False
    tail = "\n".join([ln for ln in text.splitlines() if ln.strip()][-14:])
    return bool(PERM_Q.search(tail) and PERM_YES.search(tail))


def background(pane):
    """Claude Code's footer counts the shells it runs in the background ("· 1 shell")."""
    try:
        return bool(BG.search(herdr(None, "agent", "read", pane, "--source", "visible", timeout=4)))
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False


ON = ("queues", "auto")  # the queue sidebar's modes (`s`): static queue order, or re-sorted every minute


SETTINGS = ("color", "colorby", "headings", "collapse", "badge", "host", "mark", "spinner", "entries", "status", "context")


def setting(state, cmd, args):
    """One settings change (the CLI and the popup's settings share it). Returns an error message or None."""
    if cmd == "color":  # color QUEUE NAME|none
        if len(args) < 2 or args[1] not in (*SIDEBAR_COLOURS, "none"):
            return f"color QUEUE {'|'.join(SIDEBAR_COLOURS)}|none"
        c = state.setdefault("colors", {})
        c[args[0]] = None if args[1] == "none" else args[1]  # None: no colour (absent: the default)
    elif cmd == "host":  # host HOST icon EMOJI|none, host HOST color NAME|none, host HOST spin N|off|default
        if len(args) < 3 or args[1] not in ("icon", "color", "spin") or (args[1] == "color" and args[2] not in (*SIDEBAR_COLOURS, "none")) \
                or (args[1] == "spin" and not (args[2] in ("off", "default") or re.fullmatch(r"\d+(\.\d+)?", args[2])
                                                and 0 < float(args[2]) <= 10)):
            return (f"host HOST icon EMOJI|none  or  host HOST color {'|'.join(SIDEBAR_COLOURS)}|none  or  "
                    "host HOST spin FRAMES-PER-SECOND|off|default")
        h = state.setdefault("hosts", {}).setdefault(args[0], {})
        h.pop(args[1], None) if args[2] in ("none", "default") else h.update({args[1]: args[2]})
    elif cmd == "heading":  # heading FORMAT|default
        fmt = " ".join(args)
        if fmt in ("", "default"):
            state.pop("heading", None)
        else:
            try:
                fmt.format_map(Blank())
            except (ValueError, IndexError, AttributeError) as e:
                return f"bad heading format ({e}); fields: {' '.join('{%s}' % k for k in HEAD_FIELDS)}"
            state["heading"] = fmt
    elif cmd == "colorby" and args[0] in ("queue", "machine"):
        state["colorby"] = args[0]
    elif cmd == "headings" and args[0] in ("queue", "machine", "off"):
        state["headings"] = None if args[0] == "off" else args[0]
    elif cmd == "collapse":
        state["collapsed"] = sorted(set(state.get("collapsed", [])) ^ {args[0]})
    elif cmd == "mark" and args[0] in ("own", "herdr"):
        state["mark"] = args[0]
    elif cmd == "status":  # status NAME GLYPH|default; status NAME color COLOUR|none; status spin "F1 F2 ..."|default
        names = (*MARKS, *FLAGS, "spin")
        if len(args) < 2 or args[0] not in names or (args[1] == "color" and (len(args) < 3 or args[2] not in
                                                                             (*SIDEBAR_COLOURS, "none"))):
            return f"status {'|'.join(names)} GLYPH|default  or  status NAME color {'|'.join(SIDEBAR_COLOURS)}|none"
        if args[1] == "color":
            state.setdefault("mark_colors", {})[args[0]] = None if args[2] == "none" else args[2]
        elif args[0] == "spin":
            state.pop("spin_frames", None) if args[1] == "default" else state.update(spin_frames=" ".join(args[1:]).split())
        else:
            m = state.setdefault("marks", {})
            m.pop(args[0], None) if args[1] == "default" else m.update({args[0]: args[1]})
    elif cmd == "context" and args and (args[0] == "off" or (args[0].isdigit() and 0 < int(args[0]) <= 100)):
        state["low_context"] = None if args[0] == "off" else int(args[0])  # flag below this % of context left
    elif cmd == "spinner" and args[0] in ("on", "off"):
        state["spinner"] = args[0] == "on"
    elif cmd == "entries" and args[0] in ("on", "off"):
        state["entries"] = args[0] == "on"
    elif cmd == "badge" and len(args) >= 2 and args[0] in ("model", "gitstatus"):
        b = state.setdefault("badges", {}).setdefault(args[0], {})
        b.update({args[1]: args[2]}) if len(args) > 2 else b.pop(args[1], None)
    else:
        return "see queues.py --help"
    return None


def file_lock(f, wait=True):
    """An exclusive lock on an open file, held until it is closed; False if wait is False and someone has it."""
    if WIN:
        import msvcrt
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if not wait:
                    return False
                time.sleep(0.1)
    import fcntl
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        return True
    except OSError:
        return False


def sock(method, params):
    """One request to this machine's herdr; the parsed response."""
    return rpc(method, params, "spin")


def spin_rate(state):
    """This machine's spinner frames a second: `host HOST spin N|off`, else 1. Each frame is a metadata update per working
    agent, which the herdr server redraws: 4/s cost ~6 % of a core on micro (i3-3227U, 2026-11-16, station-maintenance
    fleet 39) and 10-17 % on beast-arch with ~6 agents working (2026-11-19: 3-6 % with the spinner paused), so the
    default dropped from 4 to 1 (Ben 2026-11-19). 0 = off: the mark stays as it is."""
    v = host_setting(state, HOST, "spin")
    return 0.0 if v == "off" else float(v) if v else 1.0


def spin_loop():
    """Started by herdr with the server (the manifest's startup): turns the working mark of this machine's agents,
    at spin_rate frames a second, while `spinner on` and `mark own`. Only the label's mark changes; one process per
    server."""
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(os.path.join(STATE_DIR, "spin.lock"), "w")
    if not file_lock(lock, wait=False):
        sys.exit(0)
    state, seen, i, checked = {}, None, 0, time.time()
    while True:
        if time.time() - checked > 60:
            checked = time.time()
            follow_install(lock)
        try:
            m = os.stat(STATE).st_mtime
            if m != seen:
                state, seen = load(), m
        except OSError:
            pass
        rate = spin_rate(state)
        if not (state.get("spinner") and state.get("mark") == "own" and rate > 0):
            time.sleep(2)
            continue
        time.sleep(1 / rate)
        i += 1
        try:
            for a in real(sock("agent.list", {})["result"]["agents"]):
                lab = (a.get("tokens") or {}).get("queues_label") or ""
                fr = spin_frames(state)
                hit = re.compile("|".join(map(re.escape, sorted(fr, key=len, reverse=True))))
                if a.get("agent_status") == "working" and hit.search(lab):
                    new = hit.sub(fr[i % len(fr)], lab, count=1)
                    sock("pane.report_metadata", {"pane_id": a["pane_id"], "source": SOURCE, "tokens": {"queues_label": new}})
        except (OSError, ValueError, KeyError):
            time.sleep(5)  # no server (yet): try again later


# --- heading entries (experimental, Ben 2026-11-15) -------------------------------------------------------------------
# herdr gives plugins no sidebar clicks (herdrdev/herdr discussion #5806). So each queue's heading is an entry of its
# own: an empty pane in a workspace of its own, reported as an agent (HEAD_AGENT), placed just above the queue's first
# workspace. Clicking it focuses that pane; the pane.focused hook (`queues.py focused`) toggles the queue and puts focus
# back. Any focus counts: herdr reports a click and a keyboard switch the same way. Only with `s` on, headings queue,
# `entries on`; otherwise their workspaces are closed. Each machine keeps its own (its watcher and its hooks).
# herdr keeps a reported agent only if its kind is one it knows (an unknown label is dropped at the next scan of the
# pane), so headings report as "muse", an obscure kind; they are told apart by their queues_heading_for token.
HEAD_AGENT, HEAD_SOURCE = "muse", SOURCE + "-heading"


def agent_panes():
    return {a["pane_id"]: a.get("agent") for a in json.loads(herdr(None, "agent", "list"))["result"]["agents"]}


def real(agents):
    """The agents (no heading entries), each with done for an unseen finish: herdr's idle means "seen" only for claude.
    Other agents (pi on media-center, 2026-11-19) go straight from working to idle when they finish, so their idle
    reads as done until you focus them (queues_seen = the state_change_seq, set by focused())."""
    out = [a for a in agents if not (a.get("tokens") or {}).get("queues_heading_for")]
    for a in out:
        if (a.get("agent_status") == "idle" and a.get("agent") not in ("claude", "muse")
                and (a.get("tokens") or {}).get("queues_seen") != str(a.get("state_change_seq"))):
            a["agent_status"] = "done"
        # a permission dialog herdr missed (Claude's PermissionRequest hook, or perm_watch); never over herdr's idle or
        # done, which a closed dialog ("No", Esc) brings and which clears the mark at that event (info)
        if (a.get("tokens") or {}).get("queues_perm") and a.get("agent_status") not in ("idle", "done"):
            a["agent_status"] = "blocked"
    return out


HEADING = "{arrow} {icon} {name} {active}"
HEAD_FIELDS = ("arrow", "icon", "name", "active", "waiting", "hidden")


def heading_text(state, q, counts, collapsed):
    """A queue's heading from the `heading` format (Ben 2026-11-16: "headings should show the number of active agents by
    default (there should be a token template for heading contents)"). counts: {active, waiting, hidden}; active = in
    the queue and not marked inactive, waiting = of those, blocked, asking or finished unseen, hidden = of those, kept
    off the sidebar by the collapse."""
    qi = icon(state, q) if q != UNSORTED else ""
    f = Blank(arrow="▸" if collapsed else "▾", icon=qi if qi and qi != q else "", name=q,
              **{k: str((counts or {}).get(k, 0)) for k in ("active", "waiting", "hidden")})
    try:
        out = (state.get("heading") or HEADING).format_map(f)
    except (ValueError, IndexError, AttributeError):  # a broken format shows the default, not nothing
        out = HEADING.format_map(f)
    return " ".join(out.split())


def head_counts(state, agents):
    """{queue: {active, waiting, hidden}} over the agents given (heading_text says what each counts)."""
    collapsed, out = set(state.get("collapsed", [])), {}
    for a in agents:
        if inactive(state, a):
            continue
        c = out.setdefault(queue_of(state, a), {"active": 0, "waiting": 0, "hidden": 0})
        c["active"] += 1
        c["waiting"] += NEED.get(need(a), 7) <= NEED["done"]
        c["hidden"] += queue_of(state, a) in collapsed
    return out


OTHERS = os.path.join(STATE_DIR, "others.json")  # the other machines' agents, as this machine's watcher last saw them


def others():
    """The other machines' agents for this machine's headings: a heading entry belongs to one server, which sees only
    its own agents, so the watcher (which lists every machine once a minute) leaves the rest here. Nothing goes into the
    shared queues: 0.32 kept counts there, and a count saved from an older copy could undo a move made in a popup."""
    try:
        with open(OTHERS) as fh:
            d = json.load(fh)
        return [a for a in d["agents"] if a.get("host") != HOST] if time.time() - d["at"] < 180 else []
    except (OSError, ValueError, KeyError, TypeError):
        return []


def entries_sync(state, listed):
    """This machine's heading entries, one per queue: create, relabel, close. The agent view orders them (queues_rank).
    Under a lock, re-reading herdr: hooks and the watcher run it at the same time."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "entries.lock"), "w") as lk:
        file_lock(lk)
        # By token, not by agent: herdr re-detects a new pane once its shell is up and drops the reported agent, so the
        # agent is reported again whenever it is missing (else every pass made another heading: 2026-11-15 sandbox).
        heads_here = [p for p in json.loads(herdr(None, "pane", "list"))["result"]["panes"]
                      if (p.get("tokens") or {}).get("queues_heading_for")]
        agents_now = {a["pane_id"]: a for a in real(json.loads(herdr(None, "agent", "list"))["result"]["agents"])}
        for h in heads_here:  # the pane list never says which agent a pane holds
            h["agent"] = (agents_now.get(h["pane_id"]) or {}).get("agent")
            h["status"] = (agents_now.get(h["pane_id"]) or {}).get("agent_status")
        want = entries_on(state)
        have = {}  # queue -> heading agent
        for h in heads_here:
            q = (h.get("tokens") or {}).get("queues_heading_for")
            if q in have or not q or not want:
                herdr(None, "workspace", "close", h["workspace_id"])  # a duplicate, or entries are off
            else:
                have[q] = h
        if not want:
            return
        groups = groups_of(state)
        counts = head_counts(state, listed + others())
        for q in list(have):
            if q not in groups:
                herdr(None, "workspace", "close", have.pop(q)["workspace_id"])
        collapsed = set(state.get("collapsed", []))
        colour = (lambda q: marker(queue_colour(state, q))) if (state.get("colorby") or "queue") == "queue" \
            else (lambda q: "")
        for h in have.values():
            if h.get("agent") != HEAD_AGENT or h.get("status") != "unknown":  # "·": herdr's quietest state_icon
                herdr(None, "pane", "report-agent", h["pane_id"], "--source", HEAD_SOURCE, "--agent", HEAD_AGENT, "--state", "unknown")
        for q in groups:
            text = heading_text(state, q, counts.get(q), q in collapsed)
            toks = {"queues_heading_for": q, "queues_label": text + colour(q), "queues_rank": rank_of(state, q=q),
                    "queues_heading_host": HOST}
            h = have.get(q)
            if not h:
                r = json.loads(herdr(None, "workspace", "create", "--label", text, "--no-focus"))["result"]
                pane, wid = r["root_pane"]["pane_id"], r["workspace"]["workspace_id"]
                call(None, [["pane.report_metadata", {"pane_id": pane, "source": SOURCE, "tokens": toks}]])
                for _ in range(10):  # herdr drops a reported agent when it detects the new pane's shell: report after
                    time.sleep(0.5)
                    herdr(None, "pane", "report-agent", pane, "--source", HEAD_SOURCE, "--agent", HEAD_AGENT, "--state", "unknown")
                    time.sleep(1)
                    if pane in agent_panes():
                        break
                have[q] = h = {"pane_id": pane, "workspace_id": wid, "agent": HEAD_AGENT, "tokens": toks}
            old = h.get("tokens") or {}
            if any(old.get(k) != v for k, v in toks.items()):
                if old.get("queues_label") != toks["queues_label"]:
                    herdr(None, "workspace", "rename", h["workspace_id"], text)
                call(None, [["pane.report_metadata", {"pane_id": h["pane_id"], "source": SOURCE, "tokens": toks}]])


def focused():
    """pane.focused hook: a heading entry toggles its queue and hands focus back; any other pane is remembered."""
    ev = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}").get("data", {})
    pane, last = ev.get("pane_id"), os.path.join(STATE_DIR, "last_focus")
    panes = {p["pane_id"]: p for p in sock("pane.list", {})["result"]["panes"]}
    q = ((panes.get(pane) or {}).get("tokens") or {}).get("queues_heading_for")
    if not q:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(last, "w") as fh:
            fh.write(ev.get("workspace_id") or "")
        # you looked at it: a finished agent's mark dims (herdr's own seen state is per client, the server's stays done)
        a = next((a for a in sock("agent.list", {})["result"]["agents"] if a["pane_id"] == pane), None)
        if a:  # you came back to it: the revisit flag has done its job (Ben 2026-11-19); pin stays
            st = load()
            if revisit(st, a):
                set_flag(st, a, "revisit", False)
                save(st)
                refresh(fresh_info=False)
        # done, or idle already: a focus from the CLI marks herdr's seen state and fires no status event (sandbox)
        if a and a.get("agent_status") in ("done", "idle") and (a.get("tokens") or {}).get("queues_seen") != str(a.get("state_change_seq")):
            sock("pane.report_metadata", {"pane_id": pane, "source": SOURCE,
                                          "tokens": {"queues_seen": str(a.get("state_change_seq"))}})
            refresh(fresh_info=False)
        return
    state = load()
    setting(state, "collapse", [q])
    save(state)
    try:
        back = open(last).read().strip()
        if back:
            sock("workspace.focus", {"workspace_id": back})
    except (OSError, RuntimeError, ValueError):
        pass
    refresh(fresh_info=False)


def refresh(fresh_info=True):
    """This machine's tokens and heading entries. Runs coalesce: herdr fires a burst of events (each heading change is
    one), so a run that finds another going marks it to go round once more and exits."""
    os.makedirs(STATE_DIR, exist_ok=True)
    again = os.path.join(STATE_DIR, "refresh.again")
    lk = open(os.path.join(STATE_DIR, "refresh.lock"), "w")
    if not file_lock(lk, wait=False):
        open(again, "w").close()
        return
    for _ in range(3):
        try:
            os.remove(again)
        except OSError:
            pass
        refresh_once(fresh_info)
        if not os.path.exists(again):
            break


def changed(reqs, agents):
    """Only the token reports that change something: each one re-renders herdr's sidebar, and resending all of them
    made a heading click take seconds (Ben 2026-11-16: "very slow and inconsistent")."""
    now = {a["pane_id"]: a.get("tokens") or {} for a in agents}
    return [r for r in reqs if r[0] != "pane.report_metadata" or r[1]["pane_id"] not in now
            or any((now[r[1]["pane_id"]].get(k) or None) != (v or None) for k, v in r[1]["tokens"].items())]


def refresh_once(fresh_info=True):
    if os.path.exists(STATE):
        if os.environ.get("HERDR_PLUGIN_EVENT") == "pane.agent_detected":
            time.sleep(1)  # herdr-machine-icons runs on the same event: let its icon land first
        state = load()
        listed = real(json.loads(herdr(None, "agent", "list"))["result"]["agents"])
        listed = real(listed)
        if learn(state, listed):
            save(state)
        ws = json.loads(herdr(None, "workspace", "list"))["result"]["workspaces"]
        wsn = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in ws}
        ids = list(wsn)
        listed.sort(key=lambda a: ids.index(a["workspace_id"]) if a.get("workspace_id") in ids else len(ids))
        reqs, before = [], [dict(a, tokens=dict(a.get("tokens") or {})) for a in listed]
        # A status change re-reads only its own agent: reading every transcript and screen first held a red mark for
        # 30 s after Ben answered (beast-arch plugin log, 2026-11-16).
        ev = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}").get("data", {}).get("pane_id")
        only = ev if os.environ.get("HERDR_PLUGIN_EVENT") == "pane.agent_status_changed" else None
        for a in listed:
            a["host"], a["workspace"] = socket.gethostname(), wsn.get(a.get("workspace_id"), a.get("workspace_id"))
            if fresh_info and (only is None or a["pane_id"] == only):  # git and transcripts: not on a heading click
                t = info(a, state)
                reqs.append(["pane.report_metadata", {"pane_id": a["pane_id"], "source": INFO, "tokens": t}])
                a["tokens"] = {**(a.get("tokens") or {}), **t}
        call(None, changed(reqs + hiding(state, listed), before))
        entries_sync(state, listed)


def machine_icons():
    path = os.path.join(os.environ["APPDATA"] if WIN else os.path.expanduser("~/.config"),
                        "herdr", "plugins", "config", "benjmhart.machine-icons", "icons")
    try:
        return dict(ln.split()[:2] for ln in open(path, encoding="utf-8") if len(ln.split()) >= 2 and ln[0] != "#")
    except OSError:
        return {}


# --- the sidebar's look (Ben 2026-11-15) ------------------------------------------------------------------------------
# Colours come from a fixed palette, so herdr's rules never change: the label and heading carry an invisible marker,
# n word joiners (U+2060) for palette slot n, and the rules (`queues.py rows`) match them longest first. herdr can only
# colour a token by its own text. "own" status marks (instead of herdr's state_icon) let the label start with padding
# (U+2800: herdr trims spaces). herdr indents an entry's later rows by two, so a heading on its group's first agent's
# first row puts that agent two columns in, and the rest of the group pads two to match.
SIDEBAR_COLOURS = {"red": "#ff5555", "orange": "#ffb86c", "yellow": "#f1fa8c", "green": "#50fa7b", "cyan": "#8be9fd",
                   "purple": "#bd93f9", "pink": "#ff79c6", "grey": "#6272a4"}  # Dracula
WJ, PAD = "\u2060", "\u2800"
# Emoji, so they carry their own colour: a herdr rule colours a whole token, never one character of it (Ben
# 2026-11-16: status icons "brighter and colored", and a coloured spinner).
# Ben 2026-11-16: idle should not catch the eye (blank), a checked agent dims, a question differs from "job done",
# and an agent waiting on its own shell or subagent needs nothing from you. Width-1 marks are padded to two.
# Done: one check shape, green until seen (Ben: "the same as the grey checkbox but green"): no emoji is a bare green
# check, so ✔ (text, unlike ✅) turns its row green by rule, as 🔴 turns a blocked row red.
MARKS = {"blocked": "🔴", "draft": "📝", "asks": "❓", "asks_seen": "❔", "done": "✔", "done_seen": "✓", "background": "⏳",
         "idle": PAD * 2, "working": "\uee06", "unknown": "·"}
# A Nerd Font arc (Symbols Nerd Font, on every desk), turning (Ben: moons, clocks, globe all rejected). Frames are a
# list: a frame may be more than one character.
SPIN = ["\uee06", "\uee07", "\uee08", "\uee09", "\uee0a", "\uee0b"]
# Each mark may colour its row: a herdr rule colours a whole token, so a coloured spinner means a coloured row. Palette
# names (the label's invisible marker), so no config change. `queues.py status NAME color COLOUR|none`.
MARK_COLOURS = {"blocked": "red", "done": "green", "working": "yellow"}
# auto sort (Ben 2026-11-16): stuck on you first, then finished and not yet looked at, then working (its own background
# shell counts), idle last; a question or a finish you have looked at is idle.
NEED = {"blocked": 0, "draft": 1, "asks": 1, "done": 2, "working": 3, "background": 3, "asks_seen": 4, "done_seen": 4, "idle": 4}


def need(a):
    """What the agent wants from you, finer than herdr's status: a question vs. a finished job (the transcript), seen
    (you focused it since it stopped: queues_seen, or herdr's idle), waiting on its own background work (the screen)."""
    st, t = a.get("agent_status"), a.get("tokens") or {}
    if st in ("working", "blocked") or st not in ("done", "idle"):
        return st if st in ("working", "blocked") else "unknown"
    seen = st == "idle" or t.get("queues_seen") == str(a.get("state_change_seq"))
    if t.get("queues_draft"):  # you started a reply and didn't send it
        return "draft"
    if t.get("queues_asks"):
        return "asks_seen" if seen else "asks"
    if t.get("queues_bg"):
        return "background"
    return "idle" if st == "idle" else "done_seen" if seen else "done"


def marks(state):
    return {**MARKS, **(state.get("marks") or {})}


def spin_frames(state):
    return state.get("spin_frames") or SPIN


def mark_colour(state, n):
    return {**MARK_COLOURS, **(state.get("mark_colors") or {})}.get(n)


def mark_of(a, spin, state=None):
    n, state = need(a), state or {}
    m = spin_frames(state)[0] if n == "working" and spin else marks(state)[n]
    import unicodedata
    return m if sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in m) >= 2 else m + " "


# A queue with no colour set takes one by its place in the list, so a new queue matches the theme (first, the
# priority queue: red). ponytail: reordering queues reorders the defaults; set a colour to pin it.
QUEUE_DEFAULTS = ("red", "green", "cyan", "purple", "orange", "pink", "yellow")


def queue_colour(state, q):
    c, qs = state.get("colors") or {}, state.get("queues") or []
    return c[q] if q in c else QUEUE_DEFAULTS[qs.index(q) % len(QUEUE_DEFAULTS)] if q in qs else None


def marker(colour):
    return WJ * (list(SIDEBAR_COLOURS).index(colour) + 1) if colour in SIDEBAR_COLOURS else ""


def host_setting(state, host, what):
    return (state.get("hosts") or {}).get(host, {}).get(what)


def rows_toml(state):
    """herdr's [ui.sidebar.agents] for the current mark style. Static: colours are settings, not config."""
    wj = "\\u2060"  # escaped: no invisible text in config (and no backslash inside an f-string: Python < 3.12)
    slots = [f'{{ contains = "{wj * (i + 1)}", fg = "{c}" }}'
             for i, c in reversed(list(enumerate(SIDEBAR_COLOURS.values())))]
    head = '{ token = "$queues_heading", bold = true, rules = [' + ", ".join(slots) + "] }"
    if state.get("mark") == "own":
        # an unstyled token is herdr's dim comment grey: names get the bright text colour (Ben 2026-11-16)
        lab = ('{ token = "$queues_label", fg = "#f8f8f2", rules = ['
               + ", ".join(slots) + "] }")
        rows = [f"  [{head}],", f"  [{lab}],"]
    else:
        lab = '{ token = "$queues_label", fg = "#f8f8f2", rules = [' + ", ".join(slots) + "] }"
        rows = [f"  [{head}],", f'  ["state_icon", {lab}],']
    return "[ui.sidebar.agents]\nrows = [\n" + "\n".join(rows) + "\n]"


def hiding(state, agents, host=HOST):
    """One machine's requests: each agent's tokens (hidden, queue, label, headings), then the view. agents in sidebar
    order. Hidden: inactive, or in a collapsed queue and not its most urgent agent (that one carries the queue: "+N")."""
    on, own = state.get("sort") in ON, state.get("mark") == "own"
    fmt = (state.get("label") or LABEL) if on else PLAIN
    collapsed, heads = set(state.get("collapsed", [])), state.get("headings") if on else None
    entries = heads == "queue" and state.get("entries")  # the headings are entries of their own (entries_sync)
    if entries:
        heads = None
    by = state.get("colorby") or "queue"
    live = [a for a in agents if not inactive(state, a)]
    carrier, extra = {}, {}
    for q in collapsed:
        ms = [] if entries else [a for a in live if queue_of(state, a) == q]
        if ms:
            c = min(ms, key=lambda a: (NEED.get(need(a), 7), live.index(a)))
            carrier[q], extra[q] = c["pane_id"], len(ms) - 1
    hidden = {a["pane_id"] for a in live if queue_of(state, a) in carrier and carrier[queue_of(state, a)] != a["pane_id"]}
    hidden |= {a["pane_id"] for a in agents if inactive(state, a)}
    if entries:  # one list for every machine: a carrier on each would leave several under the heading
        hidden |= {a["pane_id"] for a in live if queue_of(state, a) in collapsed}
    colour = lambda a: marker(queue_colour(state, queue_of(state, a)) if by == "queue"
                              else host_setting(state, a.get("host"), "color"))
    above, last, local = {}, object(), head_counts(state, agents)
    for a in (a for a in live if a["pane_id"] not in hidden):
        q = queue_of(state, a)
        key = q if heads == "queue" else a.get("host")
        if heads and key != last:
            if heads == "queue":
                text = heading_text(state, q, dict(local.get(q) or {}, hidden=extra.get(q, 0)), q in carrier)
            else:
                mi = host_setting(state, a.get("host"), "icon") or (a.get("tokens") or {}).get("machine_icon") or ""
                text = f"{mi} {a.get('host', '')}".strip()
            # Every heading on its group's first agent's first row, flush left (Ben 2026-11-16: headings that rode on
            # the row above sat two columns in, as herdr indents an entry's later rows: "visually confusing").
            above[a["pane_id"]] = text + colour(a)
            last = key
    spin = state.get("spinner")

    def tokens(a):
        q = queue_of(state, a)
        qi = None if q == UNSORTED else icon(state, q)
        # Under a heading the heading carries the colour and names keep herdr's text colour: a whole name in red or
        # green was hard to read on Ben's translucent terminal (2026-11-16). Blocked stays red (the 🔴 rule).
        mc = mark_colour(state, need(a)) if own else None  # the mark's colour takes the row, over the queue's
        body = label(a, qi, fmt, host_setting(state, a.get("host"), "icon"), lane(state, a),
                     " ".join(g for _, g, _ in flags_of(state, a))) + (
            marker(mc) if mc else "" if heads or entries else colour(a))
        if own:
            mark = mark_of(a, spin, state)
            # herdr indents the label two when a heading is above it; the rest of the group pads to match
            body = (("" if a["pane_id"] in above else PAD * 2) if heads else PAD if entries else "") + f"{mark} {body}"
        return {TOKEN: "1" if a["pane_id"] in hidden else None, "queues_queue": qi, "queues_label": body,
                "queues_heading": above.get(a["pane_id"]), "queues_heading_after": None,  # retired 2026-11-16; cleared
                "queues_rank": rank_of(state, a) if entries else None}
    reqs = [["pane.report_metadata", {"pane_id": a["pane_id"], "source": SOURCE, "tokens": tokens(a)}] for a in agents]
    # Always set: the viewing client's server filters every machine's list, so a server with nothing of its own to
    # hide clearing it brought back other machines' hidden agents (Ben 2026-11-16, micro after a refresh).
    return reqs + [["agent.view.set", view(state, host)]]


def resort(state, agents, ms):
    """Flip the sidebar on every machine; returns a message. Mutates state (save it after)."""
    mode = state.get("sort") if state.get("sort") in ON else "workspaces"
    nxt = {"workspaces": "queues", "queues": "auto", "auto": "workspaces"}[mode]
    to_queues = nxt in ON
    saved = state.setdefault("order", {})
    done, skipped = [], []
    for label, (host, target) in ms.items():
        try:
            ids = workspace_ids(label)
            if to_queues and mode == "workspaces":
                saved[host] = ids  # your order, as it is now
            if to_queues:
                order = by_queues(state, [a for a in agents if a.get("host") == host], ids)
            else:
                order = restored(saved.get(host, ids), ids)
            if order != ids:
                apply_order(label, target, order)
            done.append(host)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
            skipped.append(host)
    state["sort"] = nxt
    what = {"queues": "queue order (static)", "auto": "queue order, waiting agents float up (every minute)",
            "workspaces": "your workspace order, plain labels"}[nxt]
    return (f"sidebar: {what} on {len(done)} machine(s)"
            + (f"; not reached: {', '.join(skipped)}" if skipped else ""))


# --- jumping: a plugin can't switch the window's machine (herdr #5904), so the window's Goto picker is driven -----
# The popup runs on the server the window shows, which may have no way to reach your display. So it appends a
# request to its own REQUESTS file, and `queues.py watch` on the machine you sit at follows that file on every
# machine (over ssh, as herdr itself reaches them) and drives the window that is showing the requesting machine.

RUN = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{UID}"


def live(*paths):
    """The first socket with a live listener: a crashed compositor leaves its socket behind, and the watcher (started
    by herdr) may lack the session's environment, so the runtime dir is searched too."""
    for s in paths:
        try:
            with socket.socket(socket.AF_UNIX) as c:
                c.connect(s)
            return s
        except (OSError, TypeError):
            pass
    return None


def newest(pattern):
    return sorted(glob.glob(os.path.join(RUN, pattern)), key=os.path.getmtime, reverse=True)


def sway_socket():
    return live(os.environ.get("SWAYSOCK"), *newest(f"sway-ipc.{UID}.*.sock"))


def hypr_sig():
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    s = live(sig and os.path.join(RUN, "hypr", sig, ".socket.sock"), *newest("hypr/*/.socket.sock"))
    return s and os.path.basename(os.path.dirname(s))


def running(name):
    try:
        return subprocess.run(["pgrep", "-x", "-u", str(UID), name], capture_output=True).returncode == 0
    except OSError:
        return False


def desktop():
    """sway, hyprland, kde, gnome (Wayland sessions) or x11. KWin and GNOME Shell only count with a Wayland socket:
    on X11 they are driven like any X11 window manager. windows: Windows itself, or WSL (its herdr window is a Windows
    Terminal window, even with WSLg's DISPLAY set)."""
    if WIN or wsl():
        return "windows"
    if sway_socket():
        return "sway"
    if hypr_sig():
        return "hyprland"
    wayland = os.environ.get("WAYLAND_DISPLAY") or any(not p.endswith(".lock") for p in glob.glob(f"{RUN}/wayland-*"))
    if wayland and running("kwin_wayland"):
        return "kde"
    if wayland and running("gnome-shell"):
        return "gnome"
    return "x11"


def tool(*cmd, env=None, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def x11(*args, prog="xdotool"):
    """xdotool, or kdotool (the same commands, for KWin on Wayland)."""
    env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY") or ":0")
    env.setdefault("KDE_SESSION_VERSION", "6")  # kdotool refuses without it ("Unsupported KDE version"; Plasma 6.8 VM)
    return tool(prog, *args, env=env)


def sway(*args):
    return tool("swaymsg", "-s", sway_socket() or "", *args)


def hyprctl(*args):
    return tool("hyprctl", *args, env=dict(os.environ, HYPRLAND_INSTANCE_SIGNATURE=hypr_sig() or ""))


def gnome(method, *args):
    """The Window Calls extension (extensions.gnome.org/extension/4724): GNOME Shell lets nothing else list or focus
    windows on Wayland."""
    out = tool("gdbus", "call", "--session", "--dest", "org.gnome.Shell", "--object-path",
               "/org/gnome/Shell/Extensions/Windows", "--method", f"org.gnome.Shell.Extensions.Windows.{method}", *args)
    m = re.match(r"\('(.*)',\)\s*$", out, re.S)  # ('<json>',)
    return m.group(1) if m else ""


def overview(close=None):
    """GNOME's Activities overview: is it open (close=None), or close it (False). Over D-Bus, never by Escape, which in
    a herdr pane would interrupt an agent. At login GNOME opens it, and keys typed then went to its search (VM, GNOME 51)."""
    args = ["gdbus", "call", "--session", "--dest", "org.gnome.Shell", "--object-path", "/org/gnome/Shell", "--method"]
    is_open = lambda: "true" in tool(*args, "org.freedesktop.DBus.Properties.Get", "org.gnome.Shell", "OverviewActive")
    if close is False and is_open():
        tool(*args, "org.freedesktop.DBus.Properties.Set", "org.gnome.Shell", "OverviewActive", "<false>")
        time.sleep(0.8)  # keys typed during the closing animation were lost (VM, GNOME 51)
    return is_open()


def wayland_env():
    env = dict(os.environ)
    if not env.get("WAYLAND_DISPLAY"):
        found = [p for p in glob.glob(f"{RUN}/wayland-*") if not p.endswith(".lock")]
        env["WAYLAND_DISPLAY"] = os.path.basename(found[0]) if found else "wayland-1"
    return env


# Windows, natively or from WSL (powershell.exe through WSL's interop): user32 lists and raises windows, SendKeys types
# (station-maintenance fleet 37, 2026-11-16). One resident PowerShell per process: starting one costs ~0.3 s (3.5 s in
# a starved VM) and a jump makes a dozen calls.
def wsl():
    try:
        with open("/proc/version") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


WINPS = r'''
Add-Type -TypeDefinition @"
using System; using System.Text; using System.Runtime.InteropServices;
public class HQ {
  public delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc f, IntPtr l);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte s, uint f, UIntPtr e);
}
"@
function Main($op, $arg) {
  if ($op -eq "list") {
    $fg = [HQ]::GetForegroundWindow(); $out = New-Object Collections.ArrayList
    [void][HQ]::EnumWindows({ param($h, $l)
      if ([HQ]::IsWindowVisible($h)) {
        $sb = New-Object Text.StringBuilder 512; [void][HQ]::GetWindowText($h, $sb, 512)
        if ($sb.Length) { [void]$out.Add(("{0}`t{1}`t{2}" -f $h, [int]($h -eq $fg), $sb)) } }
      $true }, [IntPtr]::Zero)
    $out
  } elseif ($op -eq "focus") {
    $h = [IntPtr][long]$arg
    if ([HQ]::IsIconic($h)) { [void][HQ]::ShowWindow($h, 9) }
    [HQ]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); [HQ]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)  # Alt: lifts the foreground lock
    [void][HQ]::SetForegroundWindow($h)
  } elseif ($op -eq "keys") {
    Add-Type -AssemblyName System.Windows.Forms  # one key per line, apart: herdr misses a prefix sent in one burst
    foreach ($k in $arg -split "`n") { [System.Windows.Forms.SendKeys]::SendWait($k); Start-Sleep -Milliseconds 150 }
  } elseif ($op -eq "locked") {
    if (Get-Process LogonUI -ErrorAction SilentlyContinue) { "1" } else { "0" }
  } elseif ($op -eq "notify") {
    Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing
    if ($script:n) { $script:n.Dispose() }  # the last one; this process stays, so the balloon outlives the call
    $script:n = New-Object System.Windows.Forms.NotifyIcon; $script:n.Icon = [System.Drawing.SystemIcons]::Information
    $script:n.Visible = $true; $script:n.ShowBalloonTip(10000, "Agents waiting on you", $arg, "Info")
  }
}
# One operation per stdin line, "<op><tab><base64 UTF-8 arg>"; its output, then the end marker.
[Console]::OutputEncoding = [Text.Encoding]::UTF8
while ($null -ne ($line = [Console]::In.ReadLine())) {
  $op, $b = $line -split "`t", 2
  try { $r = Main $op ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b))) | Out-String } catch { $r = "" }
  [Console]::Out.Write($r); [Console]::Out.WriteLine("--hq-end--"); [Console]::Out.Flush()
}
'''


WINPS_PROC, WINPS_LOCK = [None], threading.Lock()


def winps(op, arg=""):
    """One WINPS operation in this process's resident PowerShell (started on first use, again if it died); its output,
    "" on failure. Script and argument go base64-encoded, so no quoting reaches a shell. A call stuck 30 s (90 s with the start) kills it."""
    import base64
    with WINPS_LOCK:
        p, wait = WINPS_PROC[0], 30
        try:
            if p is None or p.poll() is not None:
                wait = 90  # starting: ~0.3 s on a desk, 30 s in a starved VM's desktop session (fleet 37)
                enc = base64.b64encode(WINPS.encode("utf-16-le")).decode()
                p = WINPS_PROC[0] = subprocess.Popen(
                    ["powershell" if WIN else "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8")
            p.stdin.write(op + "\t" + base64.b64encode(str(arg).encode()).decode() + "\n")
            p.stdin.flush()
            guard = threading.Timer(wait, p.kill)
            guard.start()
            out = []
            for ln in p.stdout:
                if ln.rstrip("\r\n") == "--hq-end--":
                    guard.cancel()
                    return "".join(out)
                out.append(ln)
            guard.cancel()
        except OSError:
            pass
        WINPS_PROC[0] = None  # died or was killed: the next call starts a fresh one (never re-sends this one's keys)
        return ""


SENDKEYS_SPECIAL = set("+^%~(){}[]")
SENDKEYS_NAMES = {"slash": "/", "Return": "{ENTER}", "ctrl": "^", "shift": "+", "alt": "%"}


def sendkeys(text=None, names=()):
    """SendKeys syntax: literal text escaped, or key names ("ctrl+b", "slash", "Return") as press() takes them."""
    if text is not None:
        return "".join("{%s}" % c if c in SENDKEYS_SPECIAL else c for c in text)
    out = ""
    for n in names:
        *mods, k = n.split("+")
        k = SENDKEYS_NAMES.get(k, k)
        out += "".join(SENDKEYS_NAMES[m] for m in mods) + ("{%s}" % k if k in SENDKEYS_SPECIAL else k)
    return out


def notify(text):
    """A desktop notification: notify-send, or a Windows balloon (natively or from WSL)."""
    try:
        if desktop() == "windows":
            winps("notify", text)
        else:
            subprocess.run(["notify-send", "-a", "herdr-queues", "Agents waiting on you (mod+shift+s)", text], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


# Key names the jump sends, per tool. wtype: the virtual-keyboard protocol (sway, Hyprland and other wlroots
# compositors); ydotool: a uinput keyboard (any desktop; KWin and GNOME refuse virtual keyboards), needs ydotoold.
YDO = {"ctrl": 29, "shift": 42, "alt": 56, "slash": 53, "Return": 28,
       **{c: n for row, start in (("qwertyuiop", 16), ("asdfghjkl", 30), ("zxcvbnm", 44)) for n, c in enumerate(row, start)}}


def press(d, *names):
    """Press keys: "ctrl+b", "shift+g", "slash", "Return"."""
    if d == "windows":
        winps("keys", "\n".join(sendkeys(names=[n]) for n in names))
        return
    if d == "x11":
        x11("key", "--clearmodifiers", *names)
        return
    for n in names:
        *mods, k = n.split("+")
        if d in ("sway", "hyprland"):
            tool("wtype", *[a for m in mods for a in ("-M", m)], "-k", k, *[a for m in mods for a in ("-m", m)],
                 env=wayland_env())
        else:
            codes = [YDO[x] for x in (*mods, k)]
            tool("ydotool", "key", *[f"{c}:1" for c in codes], *[f"{c}:0" for c in reversed(codes)])


def goto_keys():
    """The keys that open Goto in the herdr client on this desk (its own config.toml, which also serves --remote
    windows), e.g. ["ctrl+b", "a"]; None when that can't be known. herdr's default is prefix+g; prefix+a is Ben's, and
    by default it is nothing while prefix+s is settings, where blind keys moved about (headless sway, 2026-11-16)."""
    try:
        import tomllib
        cfg = os.path.join(os.environ["APPDATA"] if WIN else os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                           "herdr", "config.toml")
        try:
            with open(cfg, "rb") as f:
                k = tomllib.load(f).get("keys", {})
        except FileNotFoundError:
            k = {}
        goto = k.get("goto", "prefix+g")
        goto = goto if isinstance(goto, list) else [goto]
        g = next(g for g in goto if g)
        return [k.get("prefix", "ctrl+b"), g.removeprefix("prefix+")] if g.startswith("prefix+") else [g]
    except (ImportError, ValueError, StopIteration, AttributeError, TypeError):  # tomllib: Python 3.11+
        return None


def type_text(d, text):
    if d == "windows":
        winps("keys", sendkeys(text))
    elif d == "x11":
        x11("type", "--delay", "8", text)
    elif d in ("sway", "hyprland"):
        tool("wtype", "-d", "8", text, env=wayland_env())
    else:
        tool("ydotool", "type", "--key-delay", "8", text)


def windows(d=None):
    """[(window id, title, focused)], the focused one first."""
    d = d or desktop()
    out = []
    try:
        if d == "windows":
            for ln in winps("list").splitlines():
                h, f, title = (ln.split("\t", 2) + ["", ""])[:3]
                if h.strip().isdigit():
                    out.append((h.strip(), title.strip(), f == "1"))
        elif d == "sway":
            todo = [json.loads(sway("-t", "get_tree", "-r"))]
            while todo:
                n = todo.pop()
                kids = n.get("nodes", []) + n.get("floating_nodes", [])
                todo += kids
                if not kids and n.get("type") in ("con", "floating_con") and n.get("name"):
                    out.append((str(n["id"]), n["name"], bool(n.get("focused"))))
        elif d == "hyprland":
            out = [(c["address"], c.get("title", ""), c.get("focusHistoryID") == 0) for c in json.loads(hyprctl("-j", "clients"))]
        elif d == "gnome":
            for w in json.loads(gnome("List") or "[]"):
                title = w.get("title") or gnome("GetTitle", str(w["id"]))  # older Window Calls list no titles
                out.append((str(w["id"]), title, bool(w.get("focus"))))
        else:
            prog = "kdotool" if d == "kde" else "xdotool"
            active = x11("getactivewindow", prog=prog).strip()
            ids = dict.fromkeys(active.split() + x11("search", "--name", ": ", prog=prog).split())
            out = [(w, x11("getwindowname", w, prog=prog).strip(), w == active) for w in ids]
    except (ValueError, KeyError, TypeError):
        return []
    return sorted(out, key=lambda x: not x[2])


def focus(d, w):
    if d == "windows":
        winps("focus", w)
    elif d == "sway":
        sway(f"[con_id={w}] focus")
    elif d == "hyprland":
        # Hyprland 0.57 (Lua config) parses dispatch as Lua: the old form is an error there (Arch VM, 2026-11-16).
        if hyprctl("dispatch", "focuswindow", f"address:{w}").strip() != "ok":
            hyprctl("dispatch", f'hl.dsp.focus({{window = "address:{w}"}})')
    elif d == "gnome":
        gnome("Activate", w)
        overview(False)  # Activate leaves the Activities overview up, and keys would type into its search
    elif d == "kde":
        x11("windowactivate", w, prog="kdotool")
    else:
        x11("windowactivate", "--sync", w)
        x11("windowfocus", "--sync", w)  # input focus too: activate alone leaves it elsewhere without a WM


def herdr_windows(hosts, d=None):
    """[(window id, host it shows)] of herdr windows, the active one first. Title: "[herdr: ]<hostname>: <workspace>"."""
    out, canon = [], {h.lower(): h for h in hosts}  # Windows titles it with the upper-case computer name
    for w, title, _ in windows(d):
        h = canon.get(title.removeprefix("herdr: ").split(": ", 1)[0].lower())
        if h:
            out.append((w, h))
    return out


def window_title(w, d=None):
    return next((t for i, t, _ in windows(d) if i == w), "")


def focused_screen(machine):
    """The visible text of the focused pane on a server, None when it can't be read."""
    try:
        panes = json.loads(herdr(machine, "pane", "list"))["result"]["panes"]
        pane = next(p["pane_id"] for p in panes if p.get("focused"))
        return herdr(machine, "pane", "read", pane, "--source", "visible")
    except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError, KeyError, StopIteration):
        return None


def typed_into_pane(word, before, after):
    """Did the keys meant for Goto land in the pane (Goto didn't open)? Unreadable counts as yes: Return is withheld."""
    return before is None or after is None or after.count(word) > before.count(word)


def jump(machine, pane, ms=None, shown_by=None):
    """Switch a herdr window to the machine (label; "" = this one) and focus the pane. shown_by: prefer the window
    showing that host (where the request was made)."""
    ms = ms or machines()
    hosts = {h for h, _ in ms.values()}
    target = ms[machine][0]
    d = desktop()
    wins = herdr_windows(hosts, d)
    win = next((w for w in wins if w[1] == shown_by), None) or (wins[0] if wins else None)
    goto = goto_keys()
    if win and win[1] != target and goto:
        # prefix+a (or whatever opens Goto): Goto; / search; the machine's label lists its panes and Enter takes the first, which switches.
        # Return only once the pane the window shows is seen not to have taken the label: if Goto didn't open (another
        # prefix, keys lost), the keys went to that pane, and Return would run them in a shell or send them to an agent.
        shown = next((l for l, (h, _) in ms.items() if h == win[1]), None)
        word = machine or "local"
        focus(d, win[0])
        if d == "gnome" and overview():
            return
        before = focused_screen(shown) if shown is not None else None
        press(d, *goto, "slash")
        type_text(d, word)
        time.sleep(0.3)
        if typed_into_pane(word, before, focused_screen(shown) if shown is not None else None):
            press(d, "ctrl+u")  # clears the shell line (or Goto's search); nothing is sent
            return
        press(d, "Return")
        for _ in range(30):
            time.sleep(0.1)
            if window_title(win[0], d).removeprefix("herdr: ").lower().startswith(target.lower() + ": "):
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


CHILD = {} if WIN else {"preexec_fn": die_with_parent}  # Windows takes no preexec_fn

# `tail -n0 -F FILE` in Python, so the watcher follows this machine's requests on Windows too (no sh, no tail there).
TAIL = r'''
import os, sys, time
p = sys.argv[1]
os.makedirs(os.path.dirname(p), exist_ok=True)
open(p, "a").close()
f, buf = open(p), ""
f.seek(0, 2)
while True:
    chunk = f.read()
    if chunk:
        buf += chunk
        while "\n" in buf:
            ln, buf = buf.split("\n", 1)
            sys.stdout.write(ln + "\n")
        sys.stdout.flush()
        continue
    try:
        if os.path.getsize(p) < f.tell():  # truncated (request() empties it past 64 KiB)
            f.seek(0)
            buf = ""
    except OSError:
        pass
    time.sleep(0.2)
'''


def locked(d):
    """A lock screen up: a locker process, or GNOME's / KDE's own (no process of their own; asked over D-Bus), or
    Windows' (LogonUI running)."""
    if d == "windows":
        return winps("locked").strip() == "1"
    if any(running(p) for p in ("xsecurelock", "i3lock", "slock", "swaylock", "xlock", "hyprlock")):
        return True
    if d == "gnome":
        q = ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver.GetActive")
    elif d == "kde":
        q = ("org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver.GetActive")
    else:
        return False
    return "true" in tool("gdbus", "call", "--session", "--dest", q[0], "--object-path", q[1], "--method", q[2])


def at_desk(host):
    """Act only where you are: a watcher runs on every desktop, but only the one whose focused window is a herdr window
    showing the requesting machine, on an unlocked screen, carries out a jump (keys typed elsewhere could land in a
    lock screen's password box)."""
    d = desktop()
    if locked(d) or (d == "gnome" and overview()):
        return False
    focused = next((w for w, _, f in windows(d) if f), None)
    return any(w == focused for w, h in herdr_windows({host}, d))


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
        sh = "python3 -c " + shlex.quote(KEEP_NEWEST) + " " + shlex.quote(REMOTE_STATE)
        targets = [t for lab, (_, t) in ms.items() if t and lab != skip]
        with ThreadPoolExecutor(max(1, len(targets))) as ex:
            list(ex.map(lambda t: ssh(t, sh, input=text), targets))


def follow(label, ms):
    """One machine's request file, forever: carry out each request in the window showing that machine."""
    shown_by, target = ms[label]
    path = os.path.expanduser(REQUESTS) if not target else REMOTE_REQUESTS
    sh = f'mkdir -p "$(dirname {path})" && touch {path} && exec tail -n0 -F {path}'
    cmd = [sys.executable, "-u", "-c", TAIL, path] if not target else [
        "ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ConnectTimeout=10", target, sh]
    by_host = {h: lab for lab, (h, _) in ms.items()}
    while True:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             text=True, **CHILD)
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


RELAY_SINK = r'''
import json, os, re, sys
d = os.path.expanduser("~/.local/state/herdr/plugins/benjmhart.queues/relay")
os.makedirs(d, exist_ok=True)
for ln in sys.stdin:
    m = json.loads(ln)
    if re.fullmatch(r"[\w.-]{1,64}", m["host"]):
        p = os.path.join(d, m["host"] + ".json")
        open(p + ".tmp", "w").write(json.dumps(m["agents"]))
        os.replace(p + ".tmp", p)
'''


def relay(ms):
    """This server's agents, with what the waiting ones need, to every other machine over one ssh each, when they
    change and every 20 s. A popup there shows them only if it can't reach this machine itself (micro)."""
    # ponytail: sent to every machine, needed only by those that can't reach this one; a few KB a change.
    pipes, started, cache, last, sent = {}, {}, {}, None, 0
    while True:
        try:
            ags = real(json.loads(herdr(None, "agent", "list", timeout=4))["result"]["agents"])
            ws = json.loads(herdr(None, "workspace", "list", timeout=4))["result"]["workspaces"]
            names = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in ws}
            seen = {}
            for a in ags:
                a["workspace"] = names.get(a["workspace_id"], a["workspace_id"])
                if a.get("agent_status") in WAITING:
                    k = (a["pane_id"], a.get("state_change_seq"))
                    seen[k] = cache[k] if k in cache else " ".join(summary(screen(None, a["pane_id"])).split())
                    a["summary"] = seen[k]
            cache, body = seen, json.dumps(ags, sort_keys=True)
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            body = None
        if body and (body != last or time.time() - sent > 20):
            line = json.dumps({"host": HOST, "agents": ags}) + "\n"
            for t in (t for _, t in ms.values() if t):
                p = pipes.get(t)
                if (p is None or p.poll() is not None) and time.time() - started.get(t, 0) > 60:
                    started[t] = time.time()
                    p = pipes[t] = subprocess.Popen(
                        ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ConnectTimeout=10", t,
                         "python3 -u -c " + shlex.quote(RELAY_SINK)], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, text=True, **CHILD)
                try:
                    if p and p.poll() is None:
                        p.stdin.write(line)
                        p.stdin.flush()
                except OSError:
                    pass
            last, sent = body, time.time()
        time.sleep(3)


def perm_watch(every=2):
    """A Claude permission dialog herdr doesn't report: herdr keeps such an agent "working" and fires no status event,
    so the event-driven refresh never looks (mlabs-marketing, Ben 2026-11-20: "agents that are blocked that are marked
    as still running"). Every 2 s (15 s in 0.45: Ben 2026-11-21 "the biggest problem is the length of the delay"), the
    screen of each claude herdr calls working or that carries the mark (a screen read is ~2 ms); a change in what it
    shows re-tokens the sidebar."""
    while True:
        time.sleep(every)
        try:
            ch = perm_changes(json.loads(herdr(None, "agent", "list", timeout=4))["result"]["agents"])
            if ch:
                call(None, ch)
                refresh(fresh_info=False)
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass


# Event-driven (0.47, Ben 2026-11-21: "is there any way we can do this without polling at all"): Claude's own hooks.
# PermissionRequest fires as the dialog opens (sandbox: 0.1 s before it is drawn); PostToolUse(Failure),
# UserPromptSubmit and Stop after it is answered (0.2 s after "Yes"). "No" and Esc fire no hook, but herdr then reports
# the agent idle, and that status event re-reads the screen (info). `queues.py hooks` adds them to Claude's settings.
PERM_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "herdr-queues-perm")
PERM_FRESH = 3   # s: a hook's mark outlives a screen read made before the dialog is drawn


def perm_marker(pane):
    return os.path.join(PERM_DIR, pane.replace("/", "_"))


def perm_on(pane, status="working"):
    """A permission dialog is up: on screen, or a PermissionRequest hook in the last few seconds while herdr still
    calls the agent working (once herdr says idle after "No" or Esc, the screen decides)."""
    try:
        if status == "working" and time.time() - os.path.getmtime(perm_marker(pane)) < PERM_FRESH:
            return True
    except OSError:
        pass
    return permission(pane)


def perm_hook(state):
    """`queues.py perm-hook on|off`, run by Claude's hooks in the agent's pane: the mark at once, no screen read."""
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return
    f = perm_marker(pane)
    if state == "on":
        os.makedirs(PERM_DIR, exist_ok=True)
        open(f, "w").close()
    else:
        try:
            os.remove(f)
        except OSError:
            pass
    sock("pane.report_metadata", {"pane_id": pane, "source": INFO, "tokens": {"queues_perm": "1" if state == "on" else None}})
    refresh(fresh_info=False)


# The hook commands. "off" runs after every tool call, so it starts python only when this pane has a mark; both return
# at once (backgrounded) so Claude never waits on the sidebar.
_QPY = 'q=$(ls -dt "$HOME"/.config/herdr/plugins/github-installations/benjmhart.queues-*/*/checkout/queues.py 2>/dev/null | head -1); '
HOOKS = {"on": _QPY + '[ -n "$HERDR_PANE_ID" ] && [ -n "$q" ] && (python3 "$q" perm-hook on >/dev/null 2>&1 &); true',
         "off": '[ -e "${XDG_RUNTIME_DIR:-/tmp}/herdr-queues-perm/${HERDR_PANE_ID:-none}" ] && { ' + _QPY
                + '[ -n "$q" ] && (python3 "$q" perm-hook off >/dev/null 2>&1 &); }; true'}
HOOK_EVENTS = {"PermissionRequest": "on", "PostToolUse": "off", "PostToolUseFailure": "off", "UserPromptSubmit": "off",
               "Stop": "off"}


def install_hooks(settings, remove=False):
    """Claude settings with this plugin's hooks added (or removed), others untouched; idempotent. Claude reads hooks at
    startup: sessions running now get them when they restart."""
    hooks = settings.setdefault("hooks", {})
    mine = lambda h: "perm-hook" in (h.get("command") or "") and "queues.py" in (h.get("command") or "")
    for ev in list(hooks):
        for m in hooks[ev]:
            m["hooks"] = [h for h in m.get("hooks", []) if not mine(h)]
        hooks[ev] = [m for m in hooks[ev] if m.get("hooks")]
        if not hooks[ev]:
            del hooks[ev]
    if not remove:
        for ev, what in HOOK_EVENTS.items():
            hooks.setdefault(ev, []).append({"hooks": [{"type": "command", "command": HOOKS[what]}]})
    if not hooks:
        del settings["hooks"]
    return settings


def perm_changes(agents):
    """The token reports for claudes whose permission dialog appeared or went (only those herdr calls working, or that
    carry the mark: herdr's own blocked is red already)."""
    ch = []
    for a in agents:
        if a.get("agent") != "claude" or (a.get("agent_status") != "working" and not (a.get("tokens") or {}).get("queues_perm")):
            continue
        now = "1" if perm_on(a["pane_id"], a.get("agent_status")) else None
        if now != (a.get("tokens") or {}).get("queues_perm"):
            ch.append(["pane.report_metadata", {"pane_id": a["pane_id"], "source": INFO, "tokens": {"queues_perm": now}}])
    return ch


def on_state_change():
    """Redraw this machine's sidebar as soon as the queues change (a collapse clicked on another machine arrives here as
    a new state file); the 60 s nag pass was the only redraw before (Ben 2026-11-16: collapse "inconsistent")."""
    seen = None
    while True:
        try:
            m = os.stat(STATE).st_mtime
            if seen is not None and m != seen:
                refresh(fresh_info=False)
            seen = m
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
            pass
        time.sleep(0.3)


def watch(notify=True):
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(os.path.join(STATE_DIR, "watch.lock"), "w")
    if not file_lock(lock, wait=False):  # one watcher per user: two would type into the window together
        sys.exit("herdr-queues: a watcher is already running")
    ms = machines(learn=True)
    publish_version()
    if os.path.exists(STATE):  # this desk's queues are the master copy: every machine starts from them
        threading.Thread(target=push, args=(load(), ms, ""), daemon=True).start()
    for label in ms:
        threading.Thread(target=follow, args=(label, ms), daemon=True).start()
    threading.Thread(target=relay, args=(ms,), daemon=True).start()
    threading.Thread(target=nag, args=(ms, notify), daemon=True).start()
    threading.Thread(target=on_state_change, daemon=True).start()
    threading.Thread(target=perm_watch, daemon=True).start()
    while True:
        time.sleep(60)
        follow_install(lock)


def publish_version():
    """This machine's plugin version into the shared state: the state's meaning changes between releases (0.28's rank
    order, 0.31's folder keys), so the popup says when machines differ (micro's review, 2026-11-16)."""
    state = load()
    if (state.get("versions") or {}).get(HOST) != VERSION:
        state.setdefault("versions", {})[HOST] = VERSION
        save(state)


def newer(state, host):
    """The host runs a newer release than this copy: its labels are its own (an older watcher repainting them every
    minute dropped 0.36's lanes from beast-arch's sidebar while carbon and micro were on 0.33/0.34, 2026-11-18)."""
    v = (state.get("versions") or {}).get(host)
    try:
        return tuple(map(int, v.split("."))) > tuple(map(int, VERSION.split(".")))
    except (AttributeError, ValueError):
        return False


def skew(state, ms):
    """"" when every machine runs this version, else what differs ("carbon ?" = a release older than 0.32)."""
    vs = dict.fromkeys((h for h, _ in ms.values()), "?") | (state.get("versions") or {})
    vs[HOST] = VERSION  # this copy, whether or not its watcher has published yet
    odd = sorted(f"{h} {v}" for h, v in vs.items() if v != VERSION)
    return f"herdr-queues {VERSION} here; differs: {', '.join(odd)} (same version on every machine)" if odd else ""


def follow_install(lock):
    """A `herdr plugin install`/update leaves a running process on the old code: re-exec onto the new one (Ben
    2026-11-15). For the watcher and the spinner."""
    try:
        root = json.loads(herdr(None, "plugin", "list", "--plugin", PLUGIN, "--json"))["result"]["plugins"][0]["plugin_root"]
    except (RuntimeError, OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired):
        return
    if os.path.realpath(root) != os.path.dirname(os.path.realpath(__file__)) and os.path.exists(os.path.join(root, "queues.py")):
        lock.close()
        os.execv(sys.executable, [sys.executable, os.path.join(root, "queues.py")] + sys.argv[1:])


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


def nag(ms, notify=True):
    """Tell you, on this desk, about agents on any machine left waiting on you (station-maintenance fleet 30). With
    notify False (watch --no-notify) only the inactive marks are kept up."""
    seen, unsent = {}, {}
    while True:
        try:
            state = load()
        except RuntimeError:  # unreadable for a second: skip this pass rather than act on nothing
            time.sleep(60)
            continue
        ags, prompts, here, seen_elsewhere = [], [], None, []
        for m in ms:
            try:
                listed = real(json.loads(herdr(m, "agent", "list", timeout=4))["result"]["agents"])
                heads_here = [a for a in listed if (a.get("tokens") or {}).get("queues_heading_for")]
                listed = real(listed)
                ws = json.loads(herdr(m, "workspace", "list", timeout=4))["result"]["workspaces"]
                wsn = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in ws}
                for a in listed:
                    a["host"] = ms[m][0]
                    a["workspace"] = wsn.get(a.get("workspace_id"), a.get("workspace_id"))
                ids = list(wsn)
                listed.sort(key=lambda a: ids.index(a["workspace_id"]) if a.get("workspace_id") in ids else len(ids))
                if m == "":
                    here = listed
                else:
                    seen_elsewhere += listed
                try:
                    if state.get("sort") == "auto":  # waiting agents float up within their queue (Ben 2026-11-15)
                        hw = {a["workspace_id"] for a in heads_here}
                        rids = [w for w in ids if w not in hw]  # heading entries are placed by entries_sync
                        order = by_queues(state, listed, rids)
                        if order != rids:
                            apply_order(m, ms[m][1], order + [w for w in ids if w in hw])
                            listed.sort(key=lambda a: order.index(a["workspace_id"]) if a.get("workspace_id") in order else len(order))
                    if not newer(state, ms[m][0]):
                        call(ms[m][1], changed(hiding(state, listed, ms[m][0]), listed))
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                    pass
                for a in listed:
                    if not notify or inactive(state, a) or quiet(state, a):  # put aside on purpose: no reminders
                        continue
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
        try:  # heading entries: each machine keeps its own (no two watchers race to create them), counting every machine
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(OTHERS + ".tmp", "w") as fh:
                json.dump({"at": time.time(), "agents": seen_elsewhere + relayed(ms)}, fh)
            os.replace(OTHERS + ".tmp", OTHERS)
            if here is not None:
                entries_sync(state, here)
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError):
            pass
        now = time.time()
        due = nag_due(seen, ags, now) + unsent_due(unsent, prompts, now)
        if due:
            notify("\n".join(due))
        time.sleep(60)


# --- the list ---------------------------------------------------------------------------------------------------

HELP = ("←→/h l/tab queue · ↑↓/j k pgup pgdn g G · enter: go to agent · m: move to queue · 1-9: move to that queue · a: waiting only/all · /: search every agent\n"
        "J/K: move down/up in the queue · i: inactive (off herdr's sidebar, sinks to the bottom) · b: alerts on/off · v/V: revisit/pin · x: mark seen · t: lane (a tag of your own)\nqueues: n new · r rename · d delete · </> reorder · s: sidebar: your order / queues / queues, waiting first · c: collapse queue · ,: settings · q quit")
# The mouse (Ben 2026-11-14: "clickable buttons and scrolling of the agent list for mouse users"): each button sends its
# key. Click a tab to open it, a line to select it, the selected line again to go to it; the wheel scrolls.
BUTTONS = [("go", "enter"), ("▲", "K"), ("▼", "J"), ("move", "m"), ("waiting/all", "a"), ("inactive", "i"), ("alerts", "b"), ("revisit", "v"), ("pin", "V"), ("seen", "x"), ("sort", "s"), ("collapse", "c"), ("settings", ","), ("search", "/"),
           ("new", "n"), ("rename", "r"), ("delete", "d"), ("<", "<"), (">", ">"), ("quit", "q")]
WHEEL_UP = getattr(curses, "BUTTON4_PRESSED", 0)
WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0x200000)  # ncurses 6; older Pythons lack the name
ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}
NAMED = {curses.KEY_NPAGE: "pgdn", curses.KEY_PPAGE: "pgup", curses.KEY_HOME: "home", curses.KEY_END: "end",
         curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
         curses.KEY_ENTER: "enter", curses.KEY_BTAB: "left", curses.KEY_BACKSPACE: "backspace"} if curses else {}


LATER = "saved; the sidebar follows within a minute (the watcher on your desk applies it)"


def mark_seen(state, agents, agent, ms):
    """Mark a stopped agent seen on its own server (queues_seen = its state_change_seq), as focusing its pane does.
    Ben 2026-11-19: media-system's pi agent kept its ✔ after he opened it: its pane was already the focused one on
    media-center, so herdr fired no pane.focused and nothing else tells a plugin what a client is showing. Going to
    an agent from the popup, or x, marks it seen whatever the focus was. False when its server can't be reached."""
    if agent.get("agent_status") not in ("done", "idle"):
        return True
    seq = str(agent.get("state_change_seq"))
    req = ["pane.report_metadata", {"pane_id": agent["pane_id"], "source": SOURCE, "tokens": {"queues_seen": seq}}]
    agent.setdefault("tokens", {})["queues_seen"] = seq
    m = agent["machine"]
    try:
        if not m:
            call(None, [req])
        elif m in ms and not newer(state, ms[m][0]):
            call(ms[m][1], [req])
        else:
            return False   # relayed (micro): only its own watcher reaches its server
    except (OSError, subprocess.SubprocessError):
        return False
    return retoken(state, agents, agent, ms) if m else True


def retoken(state, agents, agent, ms):
    """The sidebar tokens of the agent's machine, now. False when only its watcher can (relayed, or unreachable)."""
    m = agent["machine"]
    if m not in ms or newer(state, ms[m][0]):  # relayed (micro): only its own watcher reaches its server
        return False
    try:
        call(ms[m][1], hiding(state, [a for a in agents if a["machine"] == m], ms[m][0]))
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def key(scr):
    """One key as a name or a character; None on timeout. Decodes arrow sequences itself as well, for a terminal
    whose terminfo does not match what it sends (a lone escape is "esc")."""
    try:
        c = scr.get_wch()
    except curses.error:
        return None
    if c == curses.KEY_MOUSE:
        try:
            _, x, y, _, b = curses.getmouse()
        except curses.error:
            return None
        return ("mouse", x, y, b)
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
        m = re.fullmatch(r"\[<(\d+);(\d+);(\d+)([Mm])", seq)  # SGR mouse, for a terminfo without kmous
        if m:
            b = int(m[1]) if m[4] == "M" else -1  # a release: nothing to do
            return ("mouse", int(m[2]) - 1, int(m[3]) - 1,
                    WHEEL_UP if b == 64 else WHEEL_DOWN if b == 65 else curses.BUTTON1_PRESSED if b == 0 else 0)
        if seq[:1] in ("[", "O"):
            return {"5~": "pgup", "6~": "pgdn", "H": "home", "F": "end", "1~": "home", "4~": "end"}.get(
                seq[1:], ARROWS.get(seq[-1:], "esc"))
        return "esc"
    return c


def prompt(scr, label, initial="", limit=30, esc=""):
    """A line typed at the bottom; Esc returns esc."""
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
            curses.curs_set(0)
            scr.timeout(250)
            return esc
        if c == "backspace":
            s = s[:-1]
        elif isinstance(c, str) and len(c) == 1 and c.isprintable():
            s += c
    curses.curs_set(0)
    scr.timeout(250)
    return s.strip()[:limit]


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
        if isinstance(c, tuple):  # a click on an option picks it; outside the box cancels; the wheel moves
            _, x, y, b = c
            if b & WHEEL_UP:
                c = "up"
            elif b & WHEEL_DOWN:
                c = "down"
            elif b & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                if not win.enclose(y, x):
                    return None
                i = y - win.getbegyx()[0] - 2
                if 0 <= i < min(len(options), bh - 4):
                    return i
                continue
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


EMOJI = ["🔥", "🏠", "💼", "⭐", "🚀", "🧪", "📚", "🎮", "💰", "🛠", "🐺", "🐭", "💎", "📺", "🏄", "🦊"]


def settings(scr, state, ms):
    """`,` or [settings] in the popup: every sidebar setting by keys or mouse (each list takes clicks and the wheel;
    emoji and colours are picked from lists, typing is optional). Returns a message once anything changed."""
    changed = False
    while True:
        hosts = sorted({h for h, _ in ms.values()})
        rows = [("queue", q) for q in state["queues"]] + [("host", h) for h in hosts] + \
               [("opt", k) for k in ("headings", "heading", "colorby", "mark", "spinner", "entries")]

        def show(r):
            k, v = r
            if k == "queue":
                qi = icon(state, v)
                return (f"queue    {qi if qi != v else '·'} {v} · {queue_colour(state, v) or 'no colour'}"
                        + (" · collapsed" if v in state.get("collapsed", []) else ""))
            if k == "host":
                sp = host_setting(state, v, "spin")
                return (f"machine  {host_setting(state, v, 'icon') or '·'} {v} · {host_setting(state, v, 'color') or 'no colour'}"
                        + f" · spinner {'still' if sp == 'off' else sp + '/s' if sp else 'default'}")
            if v == "heading":
                return f"heading text: {state.get('heading') or HEADING}"
            return f"{v}: " + {"headings": state.get("headings") or "off", "colorby": state.get("colorby") or "queue",
                               "mark": state.get("mark") or "herdr", "spinner": "on" if state.get("spinner") else "off",
                               "entries": ("on" if state.get("entries") else "off") + " (experimental: clickable headings)"}[v]
        i = choose(scr, "sidebar settings: Enter or click to change, Esc closes", [show(r) for r in rows])
        if i is None:
            return "settings saved; the sidebar follows" if changed else ""
        k, v = rows[i]
        if k == "opt" and v == "heading":
            fmt = prompt(scr, f"heading ({' '.join('{%s}' % f for f in HEAD_FIELDS)}; empty = default): ",
                         state.get("heading") or HEADING, limit=120, esc=None)
            if fmt is None:
                continue
            err = setting(state, "heading", [fmt])
            if err:
                continue
        elif k == "opt":
            opts = {"headings": ["queue", "machine", "off"], "colorby": ["queue", "machine"], "mark": ["own", "herdr"],
                    "spinner": ["on", "off"], "entries": ["on", "off"]}[v]
            j = choose(scr, v, opts)
            if j is None:
                continue
            setting(state, v, [opts[j]])
        else:
            what = choose(scr, v, ["emoji", "colour"] + (["collapse / open"] if k == "queue" else ["spinner speed"]))
            if what is None:
                continue
            if what == 2 and k == "host":
                sp = ["4", "2", "1", "off", "default"]
                j = choose(scr, f"{v}: spinner frames a second (off: still; default: 1)",
                           ["4/s", "2/s", "1/s", "off (still)", "default"])
                if j is None:
                    continue
                setting(state, "host", [v, "spin", sp[j]])
            elif what == 2:
                setting(state, "collapse", [v])
            elif what == 0:
                j = choose(scr, f"{v}: emoji", EMOJI + ["type one…", "none"])
                if j is None:
                    continue
                e = "none" if j == len(EMOJI) + 1 else prompt(scr, "emoji: ") if j == len(EMOJI) else EMOJI[j]
                if not e:
                    continue
                if k == "queue":
                    ic = state.setdefault("icons", {})
                    ic.pop(v, None) if e == "none" else ic.update({v: e})
                else:
                    setting(state, "host", [v, "icon", e])
            else:
                cs = list(SIDEBAR_COLOURS) + ["none"]
                j = choose(scr, f"{v}: colour", cs)
                if j is None:
                    continue
                setting(state, "color", [v, cs[j]]) if k == "queue" else setting(state, "host", [v, "color", cs[j]])
        save(state)
        changed = True


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
PALETTE = {"blocked": (203, 1), "done": (84, 2), "working": (228, 3), "idle": (103, 8), "machine": (141, 5),
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
    # each queue's tab: its colour on the background when current, as text otherwise
    xterm = {"red": (203, 1), "orange": (215, 3), "yellow": (228, 3), "green": (84, 2), "cyan": (117, 6),
             "purple": (141, 5), "pink": (212, 5), "grey": (61, 4)}
    for n, (name, c) in enumerate(xterm.items(), len(pairs) + 1):
        try:
            curses.init_pair(n, c[0 if big else 1], -1)
            curses.init_pair(n + len(xterm), PALETTE["tab"][0 if big else 1], c[0 if big else 1])
            C["q:" + name], C["qbg:" + name] = curses.color_pair(n), curses.color_pair(n + len(xterm))
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


def matches(state, a, query):
    """Search (/): every space-separated word of the query appears, case-blind, in the agent's machine, lane, queue,
    workspace, title or folder."""
    hay = " ".join([*(n for n in HAND if flagged(state, a, n)), a.get("machine") or "", lane(state, a) or "", queue_of(state, a), a.get("workspace") or "",
                    a.get("terminal_title_stripped") or "", a.get("cwd") or ""]).lower()
    return all(w in hay for w in query.lower().split())


def ui(scr):
    os.environ.setdefault("ESCDELAY", "25")
    curses.curs_set(0)
    colours()
    scr.timeout(250)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)  # no motion reports: clicks and the wheel only
    curses.mouseinterval(0)  # presses at once, no waiting to tell a click from a double click
    hits = []  # (y, x0, x1, what) of the last draw: ("tab", i), ("row", i), ("key", k)
    ms = machines()
    live = Live(ms)
    # Every agent in the queue, waiting ones first (Ben 2026-11-14: a queue is worked through, so all of it shows);
    # `a` narrows it to the ones waiting on you. The number on a tab counts the waiting ones.
    state, cur, sel, everyone, msg, drawn, top = load(), None, 0, True, "", None, 0
    # Search (Ben 2026-11-19: "a / search mode ... and a hotkey chord that can open a search directly, so that it
    # subsumes the other navigation apps"): None = off; a string = every agent on every machine matching it.
    direct = "--search" in sys.argv[2:]
    query = "" if direct else None
    msg = skew(state, ms)

    seen = None
    while True:
        try:  # saved elsewhere (another popup, another machine): read it, so a change here never saves an old copy
            m = os.stat(STATE).st_mtime
        except OSError:
            m = None
        if m != seen:
            state, seen, drawn = (load() if seen is not None else state), m, None
        agents, sums, version = live.snapshot()
        qs = tabs(state, agents)
        if cur is None and live.loaded:  # open on the first queue with something waiting
            cur = next((i for i, s in enumerate(qs) if members(state, agents, s)), 0)
        if cur is not None:
            cur %= len(qs)
        q = members(state, agents, qs[cur or 0], everyone)
        if query is not None:  # waiting ones first, then the rest, as the queues show them
            q = sorted((a for a in agents if matches(state, a, query)), key=lambda a: a.get("agent_status") not in WAITING)
        sel = max(0, min(sel, len(q) - 1))
        h, w = scr.getmaxyx()
        hl = len(HELP.splitlines())
        bl, bx = 1, 0  # button lines: the bar wraps on a narrow popup, so every button stays clickable
        for label, k in BUTTONS:
            if bx and bx + len(label) + 2 > w - 1:
                bl, bx = bl + 1, 0
            bx += len(label) + 3
        hl += bl - 1  # the list starts below the wrapped bar
        rows = max(1, h - hl - 3)  # tabs, the help lines, the buttons, the list, the message line
        top = min(max(top, sel - rows + 1), sel, max(0, len(q) - rows))  # keep the selection in view
        top = max(0, top)
        if drawn != (version, cur, sel, everyone, msg, len(state["queues"]), top, h, w, query):
            drawn = (version, cur, sel, everyone, msg, len(state["queues"]), top, h, w, query)
            scr.erase()
            hits, x = [], 0
            for i, s in enumerate(qs):
                n = len(members(state, agents, s))
                t = f" {s} {n} " if n else f" {s} "
                hits.append((0, x, x + len(t), ("tab", i)))
                qc = queue_colour(state, s)
                x = put_str(scr, 0, x, t, (C.get(f"qbg:{qc}", C.get("tab", curses.A_REVERSE)) if i == (cur or 0)
                                           else C.get(f"q:{qc}", 0))
                            | (curses.A_BOLD if n else 0)) + 1
            for i, ln in enumerate(HELP.splitlines()):  # (hl now counts the bar's extra lines too)
                put_str(scr, 1 + i, 0, ln, C.get("help", 0))
            x, y = 0, 1 + hl - (bl - 1)
            for label, k in BUTTONS:
                if x and x + len(label) + 2 > w - 1:
                    x, y = 0, y + 1
                hits.append((y, x, x + len(label) + 2, ("key", k)))
                x = put_str(scr, y, x, f"[{label}]", C.get("machine", 0)) + 1
            if not live.loaded:
                put_str(scr, 2 + hl, 2, f"loading {len(ms)} machines…", C.get("help", 0))
            elif not q and query is not None:
                put_str(scr, 2 + hl, 2, "no agent matches (backspace edits, esc leaves the search)")
            elif not q:
                put_str(scr, 2 + hl, 2, "no agents in this queue (m on an agent in another queue moves it here)" if everyone
                        else "nothing waiting on you here (a: show all its agents)")
            for i, a in enumerate(q[top:top + rows]):
                row, st = 2 + hl + i, a.get("agent_status")
                hits.append((row, 0, w, ("row", top + i)))
                title = a.get("terminal_title_stripped") or os.path.basename(a.get("cwd", ""))
                s = sums.get(skey(a)) if st in WAITING else ""
                if i + top == sel:  # the selected line: one solid bar
                    line = f" {ICON.get(st, '?')} {(a['machine'] + ' · ') if a['machine'] else ''}{'[' + lane(state, a) + '] ' if lane(state, a) else ''}{a['workspace']}  {title}"
                    line += "  · inactive" if inactive(state, a) else ""
                    line += "  · no alerts" if quiet(state, a) else ""
                    line += "".join(f"  · {g} {words}" for _, g, words in flags_of(state, a))
                    line += f"  — {s if s is not None else '…'}" if st in WAITING else ""
                    put_str(scr, row, 0, line.ljust(w - 1), C.get("select", curses.A_REVERSE) | curses.A_BOLD)
                    continue
                x = put_str(scr, row, 1, ICON.get(st, "?"), C.get(st, 0) | curses.A_BOLD) + 1
                if a["machine"]:
                    x = put_str(scr, row, x, a["machine"], C.get("machine", 0)) 
                    x = put_str(scr, row, x, " · ", C.get("help", 0))
                if lane(state, a):
                    x = put_str(scr, row, x, f"[{lane(state, a)}] ", C.get("machine", 0))
                x = put_str(scr, row, x, a["workspace"], C.get("workspace", 0) | curses.A_BOLD) + 2
                x = put_str(scr, row, x, title, curses.A_BOLD if st in WAITING else 0)
                if inactive(state, a):
                    x = put_str(scr, row, x, "  · inactive", C.get("help", 0))
                if quiet(state, a):
                    x = put_str(scr, row, x, "  · no alerts", C.get("help", 0))
                for _, g, words in flags_of(state, a):
                    x = put_str(scr, row, x, f"  · {g} {words}", C.get("summary", 0))
                if st in WAITING:
                    put_str(scr, row, x + 2, "— " + (s if s is not None else "…"), C.get("summary", 0))
            if len(q) > rows:  # where you are in a long list
                put_str(scr, h - 1, max(0, w - 12), f"{top + 1}-{min(len(q), top + rows)}/{len(q)}".rjust(11),
                        C.get("help", 0))
            if query is not None:  # the search line, in place of the message
                put_str(scr, h - 1, 0, f"/{query}", curses.A_BOLD)
                put_str(scr, h - 1, len(query) + 2, "↑↓ pick · enter go · esc " + ("close" if direct else "back to the queues"),
                        C.get("help", 0))
            elif msg:
                put_str(scr, h - 1, 0, msg, C.get("summary", 0))
            scr.refresh()

        c = key(scr)
        if query is not None and isinstance(c, str):  # typing goes to the search; arrows and enter work as usual
            if c == "esc":
                if direct:
                    return
                query, sel, drawn = None, 0, None
                continue
            if c == "backspace":
                query, sel = query[:-1], 0
                continue
            if c == "enter" and q:
                mark_seen(state, agents, q[sel], ms)
                msg = go(q[sel], ms)
                if not msg:
                    return
                continue
            if len(c) == 1 and c.isprintable():
                query, sel = query + c, 0
                continue
            if c not in ("up", "down", "pgup", "pgdn", "home", "end"):
                continue
        if isinstance(c, tuple):  # the mouse: turned into the key it stands for
            _, mx, my, b = c
            what = next((t for y, x0, x1, t in hits if y == my and x0 <= mx < x1), None)
            if b & WHEEL_UP:
                c = "up"
            elif b & WHEEL_DOWN:
                c = "down"
            elif not b & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED) or what is None:
                continue
            elif what[0] == "key":
                c = what[1]
            elif what[0] == "tab":
                cur, sel, c = what[1], 0, None
            elif what[1] == sel:  # the selected line again: go there
                c = "enter"
            else:
                sel, c = what[1], None
            if c is None:
                drawn = None
                continue
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
            sel += (1 if c == "pgdn" else -1) * rows
        elif c in ("home", "g"):
            sel = 0
        elif c in ("end", "G"):
            sel = len(q) - 1
        elif c == "a":
            everyone = not everyone
        elif c == "/":
            query, sel = "", 0
        elif c == ",":
            mark = state.get("mark")
            msg = settings(scr, state, ms)
            for a in {a["machine"]: a for a in agents}.values():
                retoken(state, agents, a, ms)
            if state.get("mark") != mark:
                msg = "status marks changed: herdr's rows must match (queues.py rows, into config.toml)"
            drawn = None
        elif c == "c":  # collapse / open this queue in herdr's sidebar
            name = qs[cur or 0]
            state["collapsed"] = sorted(set(state.get("collapsed", [])) ^ {name})
            save(state)
            for a in {a["machine"]: a for a in agents}.values():
                retoken(state, agents, a, ms)
            msg = f"{name}: {'collapsed' if name in state['collapsed'] else 'open'} in the sidebar"
        elif c == "i" and q:
            on = not inactive(state, q[sel])
            set_inactive(state, q[sel], on)
            save(state)
            msg = (("inactive: hidden from herdr's sidebar" if on else "active: back in herdr's sidebar")
                   if retoken(state, agents, q[sel], ms) else LATER)
        elif c in ("J", "K") and q:
            sel = move(state, agents, qs[cur], q[sel], 1 if c == "J" else -1, everyone)
            save(state)
        elif c == "t" and q:
            name = prompt(scr, "lane (empty clears): ", lane(state, q[sel]), 16, esc=None)
            if name is not None:
                set_lane(state, q[sel], " ".join(name.split()))
                save(state)
                msg = f"lane: {lane(state, q[sel]) or 'none'}" if retoken(state, agents, q[sel], ms) else LATER
        elif c in ("v", "V") and q:
            n = "revisit" if c == "v" else "pin"
            on = not flagged(state, q[sel], n)
            set_flag(state, q[sel], n, on)
            save(state)
            msg = (f"{glyph(state, n)} {n} on" if on else f"{n} cleared") if retoken(state, agents, q[sel], ms) else LATER
        elif c == "b" and q:
            on = not quiet(state, q[sel])
            set_quiet(state, q[sel], on)
            save(state)
            msg = "alerts off for this agent" if on else "alerts on for this agent"
        elif c == "s":
            msg = "sorting the sidebar…"
            put_str(scr, scr.getmaxyx()[0] - 1, 0, msg)
            scr.refresh()
            msg = resort(state, agents, ms)
            save(state)
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "refresh"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # heading entries here, at once
            for a in {a["machine"]: a for a in agents}.values():  # the labels follow at once, not in the watcher's minute
                retoken(state, agents, a, ms)
        elif c == "enter" and q:
            mark_seen(state, agents, q[sel], ms)
            msg = go(q[sel], ms)
            if not msg:
                return
        elif c == "x" and q:
            msg = "marked seen" if mark_seen(state, agents, q[sel], ms) else LATER
        elif c == "m" and q:
            here = queue_of(state, q[sel])
            opts = [s for s in state["queues"] if s != here] + ([UNSORTED] if here != UNSORTED else []) + ["new queue…"]
            i = choose(scr, "move to", opts)
            if i is not None:
                name = opts[i] if i < len(opts) - 1 else prompt(scr, "new queue: ")
                if name:
                    put(state, q[sel], name)
                    save(state)
                    msg = f"moved to {name}" if retoken(state, agents, q[sel], ms) else LATER
            drawn = None
        elif isinstance(c, str) and c in "123456789" and q and int(c) <= len(state["queues"]):
            name = state["queues"][int(c) - 1]
            put(state, q[sel], name)
            save(state)
            msg = f"moved to {name}" if retoken(state, agents, q[sel], ms) else LATER
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
        if curses is None:  # Windows: Python has no curses of its own
            input("herdr-queues: the list needs curses. On Windows: py -m pip install windows-curses\n(Enter closes) ")
            return
        curses.wrapper(ui)
    elif cmd == "jump":
        time.sleep(0.3)  # let the list close first
        jump(*sys.argv[2:4])
    elif cmd == "icon":  # queues.py icon QUEUE [EMOJI]: a queue's sidebar icon; no EMOJI clears it
        if len(sys.argv) < 3:
            sys.exit("usage: queues.py icon QUEUE [EMOJI]")
        state, name = load(), sys.argv[2]
        if len(sys.argv) > 3:
            state.setdefault("icons", {})[name] = sys.argv[3]
        else:
            state.get("icons", {}).pop(name, None)
        save(state)
        print(json.dumps(state.get("icons", {}), ensure_ascii=False))
    elif cmd == "lane":  # queues.py lane PANE NAME|none: tag an agent on this machine; PANE "here" is $HERDR_PANE_ID
        if len(sys.argv) < 4:
            sys.exit("usage: queues.py lane PANE NAME|none")
        pid = os.environ.get("HERDR_PANE_ID", "") if sys.argv[2] == "here" else sys.argv[2]
        a = next((a for a in real(json.loads(herdr(None, "agent", "list"))["result"]["agents"]) if a["pane_id"] == pid), None)
        if not a:
            sys.exit(f"herdr-queues: no agent in pane {pid!r} on this machine")
        state = load()
        set_lane(state, a, "" if sys.argv[3] == "none" else " ".join(sys.argv[3:]))
        save(state)
        refresh()
        print(lane(state, a) or "no lane")
    elif cmd in HAND:  # queues.py revisit|pin PANE on|off: flag an agent on this machine; PANE "here" = $HERDR_PANE_ID
        if len(sys.argv) < 4 or sys.argv[3] not in ("on", "off"):
            sys.exit(f"usage: queues.py {cmd} PANE on|off")
        pid = os.environ.get("HERDR_PANE_ID", "") if sys.argv[2] == "here" else sys.argv[2]
        a = next((a for a in real(json.loads(herdr(None, "agent", "list"))["result"]["agents"]) if a["pane_id"] == pid), None)
        if not a:
            sys.exit(f"herdr-queues: no agent in pane {pid!r} on this machine")
        state = load()
        set_flag(state, a, cmd, sys.argv[3] == "on")
        save(state)
        refresh()
        print(cmd if flagged(state, a, cmd) else f"no {cmd} flag")
    elif cmd == "refresh":  # herdr runs it on agent changes, renames and focus: this machine's tokens now
        refresh()
    elif cmd in SETTINGS:
        state, args = load(), sys.argv[2:]
        err = setting(state, cmd, args) if args else None
        if err:
            sys.exit(f"herdr-queues: {err}")
        if args:
            save(state)
        print(json.dumps({k: state.get(k) for k in ("icons", "colors", "hosts", "colorby", "headings", "collapsed",
                                                    "mark", "spinner", "entries", "badges")}, ensure_ascii=False))
    elif cmd == "spin":
        spin_loop()
    elif cmd == "focused":
        focused()
    elif cmd == "perm-hook":  # run by Claude's hooks (queues.py hooks)
        perm_hook(sys.argv[2] if len(sys.argv) > 2 else "on")
    elif cmd == "hooks":  # queues.py hooks [remove]: Claude's PermissionRequest etc. hooks in ~/.claude/settings.json
        path = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "settings.json")
        try:
            cur = json.load(open(path, encoding="utf-8"))
        except FileNotFoundError:
            cur = {}
        new = install_hooks(json.loads(json.dumps(cur)), remove=sys.argv[2:3] == ["remove"])
        if new != cur:
            tmp = path + ".queues-tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(new, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, path)
        print(f"{path}: {'removed' if sys.argv[2:3] == ['remove'] else 'hooks in'} ({'changed' if new != cur else 'already so'}); "
              "Claude sessions pick them up when they (re)start")
    elif cmd == "rows":  # herdr's [ui.sidebar.agents] for these colours and headings: paste into config.toml
        print(rows_toml(load()))
    elif cmd == "label":  # queues.py label [FORMAT|default]: the sidebar's $queues_label; no argument shows it
        state = load()
        if len(sys.argv) > 2:
            fmt = " ".join(sys.argv[2:])
            if fmt == "default":
                state.pop("label", None)
            else:
                try:
                    fmt.format_map(Blank())
                except (ValueError, IndexError, AttributeError) as e:
                    sys.exit(f"herdr-queues: bad label format ({e}); fields: {' '.join('{%s}' % k for k in FIELDS)}"
                             " and any pane token by name, e.g. {folder}")
                state["label"] = fmt
            save(state)
        print(state.get("label") or LABEL)
    elif cmd == "heading":  # queues.py heading [FORMAT|default]: a queue heading's text; no argument shows it
        state = load()
        if len(sys.argv) > 2:
            err = setting(state, "heading", [" ".join(sys.argv[2:])])
            if err:
                sys.exit("herdr-queues: " + err)
            save(state)
        print(state.get("heading") or HEADING)
    elif cmd == "watch":
        watch(notify="--no-notify" not in sys.argv[2:])
    elif cmd == "open":
        ep = "search" if "--search" in sys.argv[2:] else "ui"
        resp = rpc("plugin.pane.open", {"plugin_id": os.environ["HERDR_PLUGIN_ID"], "entrypoint": ep, "focus": True,
                                        "width": "98%", "height": "96%"}, "queues")
        if "error" in resp:
            raise RuntimeError(f"plugin.pane.open: {resp['error']}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "")
    except RuntimeError as e:
        sys.exit(f"herdr-queues: {e}")
