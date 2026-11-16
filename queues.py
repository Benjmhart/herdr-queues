#!/usr/bin/env python3
"""herdr-queues: your own queues (personal, work, ...) of the agents waiting on you, on every machine herdr knows,
each with one line of what it needs. Enter jumps to the agent, switching the herdr window to its machine.

  queues.py ui           the popup (the plugin's pane)
  queues.py open         opens the popup (the plugin's action)
  queues.py standalone   the same list outside herdr (a desktop key); jumps directly
  queues.py watch        run on the machine you sit at: carries out jumps asked for by a popup on any machine
  queues.py jump M P     switch the herdr window to machine M and focus pane P
  queues.py icon Q [E]   icon E for queue Q in herdr's sidebar ($queues_queue); no E clears it
  queues.py refresh      this machine's sidebar tokens now (herdr runs it on rename and new agents)
  queues.py color Q C    queue Q's colour (red orange yellow green cyan purple pink grey, none); colorby queue|machine
  queues.py host H icon E | host H color C   a machine's emoji (overrides herdr-machine-icons) and colour
  queues.py headings queue|machine|off;  collapse Q;  mark own|herdr;  spinner on|off;  badge model|gitstatus V [E]
  queues.py rows         herdr's [ui.sidebar.agents] for the mark style (colours are settings: it never changes)
  queues.py spin         the spinner (herdr starts it with the server)
  queues.py entries on|off   experimental: each queue heading an entry of its own; clicking it collapses the queue
  queues.py label [F]    the sidebar's $queues_label template, e.g. "{queue}{machine} {workspace} {folder}" (any pane
                         token by name); "default" resets

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
        try:
            with open(path) as f:
                s = json.load(f)
            if "streams" in s:  # herdr-streams' file
                s = {"queues": s["streams"], "members": s["members"]}
            return s
        except (FileNotFoundError, ValueError):
            continue
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
    open(p + ".tmp", "w").write(new)
    os.replace(p + ".tmp", p)
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

CALLS = r'''
import json, os, socket, sys
P = os.environ.get("HERDR_SOCKET_PATH") or os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "herdr/herdr.sock")
for method, params in json.loads(sys.argv[1]):
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(P)
        s.sendall((json.dumps({"id": "q", "method": method, "params": params}) + "\n").encode())
        s.makefile().readline()
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
FIELDS = ("queue", "machine", "workspace", "host", "agent", "title", "model", "gitstatus", "usage", "usage_pct")


class Blank(dict):
    def __missing__(self, key):
        return ""


def label(a, qi, fmt=LABEL, machine=None):
    toks = {k: v for k, v in (a.get("tokens") or {}).items() if not k.startswith("queues_")}  # not its own
    f = Blank(toks, queue=qi or "", machine=machine or toks.get("machine_icon") or "",
              workspace=a.get("workspace") or a.get("workspace_id", ""), host=a.get("host", ""),
              agent=a.get("agent", ""), title=a.get("terminal_title_stripped", ""),
              usage_pct=f"{toks['usage']}%" if toks.get("usage") else "")
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
        r = subprocess.run(["git", "-C", cwd, "status", "--porcelain=v2", "--branch"], capture_output=True, text=True,
                           timeout=3)
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


def asks_text(text):
    """The last paragraph of a reply asks something."""
    last = [p for p in text.strip().split("\n\n") if p.strip()][-1:] or [""]
    return "?" in last[0]


BG = re.compile(r"· \d+ (shells?|background tasks?)\b")


def background(pane):
    """Claude Code's footer counts the shells it runs in the background ("· 1 shell")."""
    try:
        return bool(BG.search(herdr(None, "agent", "read", pane, "--source", "visible", timeout=4)))
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False


RANK = {"blocked": 0, "done": 1, "idle": 2, "working": 3}
ON = ("queues", "auto")  # the queue sidebar's modes (`s`): static queue order, or re-sorted every minute


SETTINGS = ("color", "colorby", "headings", "collapse", "badge", "host", "mark", "spinner", "entries")


def setting(state, cmd, args):
    """One settings change (the CLI and the popup's settings share it). Returns an error message or None."""
    if cmd == "color":  # color QUEUE NAME|none
        if len(args) < 2 or args[1] not in (*SIDEBAR_COLOURS, "none"):
            return f"color QUEUE {'|'.join(SIDEBAR_COLOURS)}|none"
        c = state.setdefault("colors", {})
        c.pop(args[0], None) if args[1] == "none" else c.update({args[0]: args[1]})
    elif cmd == "host":  # host HOST icon EMOJI|none, host HOST color NAME|none
        if len(args) < 3 or args[1] not in ("icon", "color") or (args[1] == "color" and args[2] not in (*SIDEBAR_COLOURS, "none")):
            return f"host HOST icon EMOJI|none  or  host HOST color {'|'.join(SIDEBAR_COLOURS)}|none"
        h = state.setdefault("hosts", {}).setdefault(args[0], {})
        h.pop(args[1], None) if args[2] == "none" else h.update({args[1]: args[2]})
    elif cmd == "colorby" and args[0] in ("queue", "machine"):
        state["colorby"] = args[0]
    elif cmd == "headings" and args[0] in ("queue", "machine", "off"):
        state["headings"] = None if args[0] == "off" else args[0]
    elif cmd == "collapse":
        state["collapsed"] = sorted(set(state.get("collapsed", [])) ^ {args[0]})
    elif cmd == "mark" and args[0] in ("own", "herdr"):
        state["mark"] = args[0]
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


def sock(method, params):
    """One request to this machine's herdr; the parsed response."""
    path = os.environ.get("HERDR_SOCKET_PATH") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "herdr/herdr.sock")
    with socket.socket(socket.AF_UNIX) as sk:
        sk.settimeout(3)
        sk.connect(path)
        sk.sendall((json.dumps({"id": "spin", "method": method, "params": params}) + "\n").encode())
        return json.loads(sk.makefile().readline())


def spin_loop():
    """Started by herdr with the server (the manifest's startup): turns the working mark of this machine's agents,
    4 frames a second, while `spinner on` and `mark own`. Only the label's mark changes; one process per server."""
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(os.path.join(STATE_DIR, "spin.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
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
        if not (state.get("spinner") and state.get("mark") == "own"):
            time.sleep(2)
            continue
        time.sleep(0.25)
        i += 1
        try:
            for a in sock("agent.list", {})["result"]["agents"]:
                lab = (a.get("tokens") or {}).get("queues_label") or ""
                if a.get("agent_status") == "working" and any(ch in lab for ch in SPIN):
                    new = re.sub(f"[{SPIN}]", SPIN[i % len(SPIN)], lab, count=1)
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
    return [a for a in agents if not (a.get("tokens") or {}).get("queues_heading_for")]


def heading_text(state, q, n_hidden, collapsed):
    qi = icon(state, q) if q != UNSORTED else ""
    name = f"{qi} {q}" if qi and qi != q else q
    return (f"▸ {name} +{n_hidden}" if collapsed and n_hidden else f"▸ {name}" if collapsed else f"▾ {name}")


def entries_sync(state, listed):
    """This machine's heading entries, one per queue: create, relabel, close. The agent view orders them (queues_rank).
    Under a lock, re-reading herdr: hooks and the watcher run it at the same time."""
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "entries.lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        # By token, not by agent: herdr re-detects a new pane once its shell is up and drops the reported agent, so the
        # agent is reported again whenever it is missing (else every pass made another heading: 2026-11-15 sandbox).
        heads_here = [p for p in json.loads(herdr(None, "pane", "list"))["result"]["panes"]
                      if (p.get("tokens") or {}).get("queues_heading_for")]
        agents_now = {a["pane_id"]: a for a in json.loads(herdr(None, "agent", "list"))["result"]["agents"]}
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
        for q in list(have):
            if q not in groups:
                herdr(None, "workspace", "close", have.pop(q)["workspace_id"])
        collapsed = set(state.get("collapsed", []))
        colour = (lambda q: marker((state.get("colors") or {}).get(q))) if (state.get("colorby") or "queue") == "queue" \
            else (lambda q: "")
        for h in have.values():
            if h.get("agent") != HEAD_AGENT or h.get("status") != "unknown":  # "·": herdr's quietest state_icon
                herdr(None, "pane", "report-agent", h["pane_id"], "--source", HEAD_SOURCE, "--agent", HEAD_AGENT, "--state", "unknown")
        for q in groups:
            text = heading_text(state, q, 0, q in collapsed)  # ponytail: no "+N": no one machine sees every agent
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
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    again = os.path.join(STATE_DIR, "refresh.again")
    lk = open(os.path.join(STATE_DIR, "refresh.lock"), "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
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
        listed = json.loads(herdr(None, "agent", "list"))["result"]["agents"]
        listed = real(listed)
        ws = json.loads(herdr(None, "workspace", "list"))["result"]["workspaces"]
        wsn = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in ws}
        ids = list(wsn)
        listed.sort(key=lambda a: ids.index(a["workspace_id"]) if a.get("workspace_id") in ids else len(ids))
        reqs, before = [], [dict(a, tokens=dict(a.get("tokens") or {})) for a in listed]
        for a in listed:
            a["host"], a["workspace"] = socket.gethostname(), wsn.get(a.get("workspace_id"), a.get("workspace_id"))
            if fresh_info:  # git and transcripts: not on a heading click, the tokens already there do
                t = info(a, state)
                reqs.append(["pane.report_metadata", {"pane_id": a["pane_id"], "source": INFO, "tokens": t}])
                a["tokens"] = {**(a.get("tokens") or {}), **t}
        call(None, changed(reqs + hiding(state, listed), before))
        entries_sync(state, listed)


def machine_icons():
    path = os.path.expanduser("~/.config/herdr/plugins/config/benjmhart.machine-icons/icons")
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
MARKS = {"blocked": "🔴", "asks": "❓", "asks_seen": "❔", "done": "✅", "done_seen": "✓", "background": "⏳",
         "idle": PAD * 2, "working": "🕐", "unknown": "·"}
SPIN = "🕐🕑🕒🕓🕔🕕🕖🕗🕘🕙🕚🕛"  # clock faces: light on a dark background (the new moon was invisible)
NEED = {"blocked": 0, "asks": 1, "done": 2, "asks_seen": 3, "background": 4, "done_seen": 5, "idle": 5, "working": 6}


def need(a):
    """What the agent wants from you, finer than herdr's status: a question vs. a finished job (the transcript), seen
    (you focused it since it stopped: queues_seen, or herdr's idle), waiting on its own background work (the screen)."""
    st, t = a.get("agent_status"), a.get("tokens") or {}
    if st in ("working", "blocked") or st not in ("done", "idle"):
        return st if st in ("working", "blocked") else "unknown"
    seen = st == "idle" or t.get("queues_seen") == str(a.get("state_change_seq"))
    if t.get("queues_asks"):
        return "asks_seen" if seen else "asks"
    if t.get("queues_bg"):
        return "background"
    return "idle" if st == "idle" else "done_seen" if seen else "done"


def mark_of(a, spin):
    n = need(a)
    m = SPIN[0] if n == "working" and spin else MARKS[n]
    import unicodedata
    return m if sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in m) >= 2 else m + " "


def marker(colour):
    return WJ * (list(SIDEBAR_COLOURS).index(colour) + 1) if colour in SIDEBAR_COLOURS else ""


def host_setting(state, host, what):
    return (state.get("hosts") or {}).get(host, {}).get(what)


def rows_toml(state):
    """herdr's [ui.sidebar.agents] for the current mark style. Static: colours are settings, not config."""
    slots = [f'{{ contains = "{"\\u2060" * (i + 1)}", fg = "{c}" }}'  # escaped: no invisible text in config
             for i, c in reversed(list(enumerate(SIDEBAR_COLOURS.values())))]
    head = '{ token = "$queues_heading", bold = true, rules = [' + ", ".join(slots) + "] }"
    if state.get("mark") == "own":
        # an unstyled token is herdr's dim comment grey: names get the bright text colour (Ben 2026-11-16)
        lab = ('{ token = "$queues_label", fg = "#f8f8f2", rules = [{ contains = "' + MARKS["blocked"] + '", fg = "#ff5555", bold = true }, '
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
    colour = lambda a: marker((state.get("colors") or {}).get(queue_of(state, a)) if by == "queue"
                              else host_setting(state, a.get("host"), "color"))
    above, last = {}, object()
    for a in (a for a in live if a["pane_id"] not in hidden):
        q = queue_of(state, a)
        key = q if heads == "queue" else a.get("host")
        if heads and key != last:
            if heads == "queue":
                qi = icon(state, q) if q != UNSORTED else ""
                name = f"{qi} {q}" if qi and qi != q else q
                text = (f"▸ {name} +{extra[q]}" if q in carrier and extra[q] else
                        f"▸ {name}" if q in carrier else f"▾ {name}")
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
        body = label(a, qi, fmt, host_setting(state, a.get("host"), "icon")) + ("" if heads or entries else colour(a))
        if own:
            st = a.get("agent_status")
            mark = mark_of(a, spin)
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
        sh = "python3 -c " + shlex.quote(KEEP_NEWEST) + " " + shlex.quote(REMOTE_STATE)
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
                        stderr=subprocess.DEVNULL, text=True, preexec_fn=die_with_parent)
                try:
                    if p and p.poll() is None:
                        p.stdin.write(line)
                        p.stdin.flush()
                except OSError:
                    pass
            last, sent = body, time.time()
        time.sleep(3)


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
    threading.Thread(target=relay, args=(ms,), daemon=True).start()
    threading.Thread(target=nag, args=(ms, notify), daemon=True).start()
    threading.Thread(target=on_state_change, daemon=True).start()
    while True:
        time.sleep(60)
        follow_install(lock)


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
        ags, prompts, state = [], [], load()
        for m in ms:
            try:
                listed = json.loads(herdr(m, "agent", "list", timeout=4))["result"]["agents"]
                heads_here = [a for a in listed if (a.get("tokens") or {}).get("queues_heading_for")]
                listed = real(listed)
                ws = json.loads(herdr(m, "workspace", "list", timeout=4))["result"]["workspaces"]
                wsn = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in ws}
                for a in listed:
                    a["host"] = ms[m][0]
                    a["workspace"] = wsn.get(a.get("workspace_id"), a.get("workspace_id"))
                ids = list(wsn)
                listed.sort(key=lambda a: ids.index(a["workspace_id"]) if a.get("workspace_id") in ids else len(ids))
                try:
                    if state.get("sort") == "auto":  # waiting agents float up within their queue (Ben 2026-11-15)
                        hw = {a["workspace_id"] for a in heads_here}
                        rids = [w for w in ids if w not in hw]  # heading entries are placed by entries_sync
                        order = by_queues(state, listed, rids)
                        if order != rids:
                            apply_order(m, ms[m][1], order + [w for w in ids if w in hw])
                            listed.sort(key=lambda a: order.index(a["workspace_id"]) if a.get("workspace_id") in order else len(order))
                    call(ms[m][1], changed(hiding(state, listed, ms[m][0]), listed))
                    if m == "":  # heading entries: each machine keeps its own (no two watchers race to create them)
                        entries_sync(state, listed)
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
        "J/K: move down/up in the queue · i: inactive (off herdr's sidebar, sinks to the bottom) · b: alerts on/off\nqueues: n new · r rename · d delete · </> reorder · s: sidebar: your order / queues / queues, waiting first · c: collapse queue · ,: settings · q quit")
# The mouse (Ben 2026-11-14: "clickable buttons and scrolling of the agent list for mouse users"): each button sends its
# key. Click a tab to open it, a line to select it, the selected line again to go to it; the wheel scrolls.
BUTTONS = [("go", "enter"), ("▲", "K"), ("▼", "J"), ("move", "m"), ("waiting/all", "a"), ("inactive", "i"), ("alerts", "b"), ("sort", "s"), ("collapse", "c"), ("settings", ","),
           ("new", "n"), ("rename", "r"), ("delete", "d"), ("<", "<"), (">", ">"), ("quit", "q")]
WHEEL_UP = getattr(curses, "BUTTON4_PRESSED", 0)
WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0x200000)  # ncurses 6; older Pythons lack the name
ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}
NAMED = {curses.KEY_NPAGE: "pgdn", curses.KEY_PPAGE: "pgup", curses.KEY_HOME: "home", curses.KEY_END: "end",
         curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
         curses.KEY_ENTER: "enter", curses.KEY_BTAB: "left", curses.KEY_BACKSPACE: "backspace"}


LATER = "saved; the sidebar follows within a minute (the watcher on your desk applies it)"


def retoken(state, agents, agent, ms):
    """The sidebar tokens of the agent's machine, now. False when only its watcher can (relayed, or unreachable)."""
    m = agent["machine"]
    if m not in ms:  # relayed (micro): only its own watcher reaches its server
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
               [("opt", k) for k in ("headings", "colorby", "mark", "spinner", "entries")]

        def show(r):
            k, v = r
            if k == "queue":
                qi = icon(state, v)
                return (f"queue    {qi if qi != v else '·'} {v} · {(state.get('colors') or {}).get(v, 'no colour')}"
                        + (" · collapsed" if v in state.get("collapsed", []) else ""))
            if k == "host":
                return f"machine  {host_setting(state, v, 'icon') or '·'} {v} · {host_setting(state, v, 'color') or 'no colour'}"
            return f"{v}: " + {"headings": state.get("headings") or "off", "colorby": state.get("colorby") or "queue",
                               "mark": state.get("mark") or "herdr", "spinner": "on" if state.get("spinner") else "off",
                               "entries": ("on" if state.get("entries") else "off") + " (experimental: clickable headings)"}[v]
        i = choose(scr, "sidebar settings: Enter or click to change, Esc closes", [show(r) for r in rows])
        if i is None:
            return "settings saved; the sidebar follows" if changed else ""
        k, v = rows[i]
        if k == "opt":
            opts = {"headings": ["queue", "machine", "off"], "colorby": ["queue", "machine"], "mark": ["own", "herdr"],
                    "spinner": ["on", "off"], "entries": ["on", "off"]}[v]
            j = choose(scr, v, opts)
            if j is None:
                continue
            setting(state, v, [opts[j]])
        else:
            what = choose(scr, v, ["emoji", "colour"] + (["collapse / open"] if k == "queue" else []))
            if what is None:
                continue
            if what == 2:
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
    curses.mousemask(curses.ALL_MOUSE_EVENTS)  # no motion reports: clicks and the wheel only
    curses.mouseinterval(0)  # presses at once, no waiting to tell a click from a double click
    hits = []  # (y, x0, x1, what) of the last draw: ("tab", i), ("row", i), ("key", k)
    ms = machines()
    live = Live(ms)
    # Every agent in the queue, waiting ones first (Ben 2026-11-14: a queue is worked through, so all of it shows);
    # `a` narrows it to the ones waiting on you. The number on a tab counts the waiting ones.
    state, cur, sel, everyone, msg, drawn, top = load(), None, 0, True, "", None, 0

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
        if drawn != (version, cur, sel, everyone, msg, len(state["queues"]), top, h, w):
            drawn = (version, cur, sel, everyone, msg, len(state["queues"]), top, h, w)
            scr.erase()
            hits, x = [], 0
            for i, s in enumerate(qs):
                n = len(members(state, agents, s))
                t = f" {s} {n} " if n else f" {s} "
                hits.append((0, x, x + len(t), ("tab", i)))
                x = put_str(scr, 0, x, t, (C.get("tab", curses.A_REVERSE) if i == (cur or 0) else 0)
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
            elif not q:
                put_str(scr, 2 + hl, 2, "no agents in this queue (m on an agent in another queue moves it here)" if everyone
                        else "nothing waiting on you here (a: show all its agents)")
            for i, a in enumerate(q[top:top + rows]):
                row, st = 2 + hl + i, a.get("agent_status")
                hits.append((row, 0, w, ("row", top + i)))
                title = a.get("terminal_title_stripped") or os.path.basename(a.get("cwd", ""))
                s = sums.get(skey(a)) if st in WAITING else ""
                if i + top == sel:  # the selected line: one solid bar
                    line = f" {ICON.get(st, '?')} {(a['machine'] + ' · ') if a['machine'] else ''}{a['workspace']}  {title}"
                    line += "  · inactive" if inactive(state, a) else ""
                    line += "  · no alerts" if quiet(state, a) else ""
                    line += f"  — {s if s is not None else '…'}" if st in WAITING else ""
                    put_str(scr, row, 0, line.ljust(w - 1), C.get("select", curses.A_REVERSE) | curses.A_BOLD)
                    continue
                x = put_str(scr, row, 1, ICON.get(st, "?"), C.get(st, 0) | curses.A_BOLD) + 1
                if a["machine"]:
                    x = put_str(scr, row, x, a["machine"], C.get("machine", 0)) 
                    x = put_str(scr, row, x, " · ", C.get("help", 0))
                x = put_str(scr, row, x, a["workspace"], C.get("workspace", 0) | curses.A_BOLD) + 2
                x = put_str(scr, row, x, title, curses.A_BOLD if st in WAITING else 0)
                if inactive(state, a):
                    x = put_str(scr, row, x, "  · inactive", C.get("help", 0))
                if quiet(state, a):
                    x = put_str(scr, row, x, "  · no alerts", C.get("help", 0))
                if st in WAITING:
                    put_str(scr, row, x + 2, "— " + (s if s is not None else "…"), C.get("summary", 0))
            if len(q) > rows:  # where you are in a long list
                put_str(scr, h - 1, max(0, w - 12), f"{top + 1}-{min(len(q), top + rows)}/{len(q)}".rjust(11),
                        C.get("help", 0))
            if msg:
                put_str(scr, h - 1, 0, msg, C.get("summary", 0))
            scr.refresh()

        c = key(scr)
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
    elif cmd == "watch":
        watch(notify="--no-notify" not in sys.argv[2:])
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
