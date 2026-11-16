"""Station Search's engine: one full-text index over coding agents' conversations, plus live terminals.

    python search.py index                    update the index (only changed files)
    python search.py query <text>             update, then print matches as JSON
    python search.py context <row> <text>     a match with the messages around it, as JSON

Reads Claude Code, Codex, Hermes, Antigravity CLI, Pi and configured normalized exports.
Live agents are discovered from Herdr without an allowlist. See docs/agents.md for source paths.
The index lives in ~/.cache/station-search/index.db
($STATION_SEARCH_DB overrides it). Words are ANDed, prefix-matched and accent-blind.
Filters: /agent-id (any agent), /open (only what's running now), #word (folder).
"""
import glob, json, os, re, shutil, sqlite3, subprocess, sys, time, unicodedata
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlparse

HOME = Path.home()
DB = Path(os.environ.get("STATION_SEARCH_DB") or HOME / ".cache" / "station-search" / "index.db")
CLAUDE = HOME / ".claude"
CODEX = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
HERMES = Path(os.environ.get("HERMES_HOME") or HOME / ".hermes")
ANTIGRAVITY = Path(os.environ.get("ANTIGRAVITY_HOME") or HOME / ".gemini/antigravity-cli")
PI = Path(os.environ.get("PI_CODING_AGENT_DIR") or HOME / ".pi/agent")
SOURCES = Path(os.environ.get("STATION_SEARCH_SOURCES") or HOME / ".config/station-search/sources.json")
AGENTS = ("claude", "codex", "hermes", "antigravity", "pi")
ALIASES = {"claude-code": "claude", "agy": "antigravity", "gemini-cli": "gemini", "pi-coding-agent": "pi"}
NAMES = {"claude": "Claude", "codex": "Codex", "hermes": "Hermes", "antigravity": "Antigravity", "pi": "Pi"}


def agent_id(value):
    value = str(value or "terminal").lower()
    return ALIASES.get(value, value)


def terminal_agent(t):
    status = t.get("status")
    reported = status.get("app") if isinstance(status, dict) else None
    value = reported or t.get("agent")
    if not status and (not value or re.search(r"[\\/]|\.(exe|cmd)$", value, re.I)):
        return "terminal"
    return agent_id(value)


def configured_sources():
    if not SOURCES.exists():
        return []
    try:
        data = json.loads(SOURCES.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("expected an array")
        out = []
        for source in data:
            if not isinstance(source, dict) or not re.fullmatch(r"[a-z][a-z0-9_-]*", source.get("agent", "")):
                raise ValueError("each source needs an agent id")
            if source["agent"] in AGENTS:
                raise ValueError("custom sources must use a distinct agent id")
            if not isinstance(source.get("paths"), list) or not all(isinstance(p, str) for p in source["paths"]):
                raise ValueError("paths must be an array of glob strings")
            resume = source.get("resume", [])
            if not isinstance(resume, list) or not all(isinstance(a, str) for a in resume):
                raise ValueError("resume must be an argv array")
            out.append(source)
        return out
    except (OSError, ValueError, TypeError) as error:
        print(f"Station Search: custom sources unavailable: {error}", file=sys.stderr)
        return []


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def file_stamp(path):
    stat = path.stat()
    wal = Path(str(path) + "-wal")
    base = f"{stat.st_mtime_ns}:{stat.st_size}"
    return base + (f":{wal.stat().st_mtime_ns}:{wal.stat().st_size}" if wal.exists() else "")


def hc_path():
    found = shutil.which("hc")
    if found:
        return found
    for p in (HOME / "AppData/Local/Programs/herdr-core/hc.exe", HOME / ".local/bin/hc"):
        if p.exists():
            return str(p)
    return "hc"


HC = hc_path()


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript("""
        create table if not exists files (path text primary key, stamp text);
        create table if not exists sessions (agent text, id text, cwd text, title text, last text,
            primary key (agent, id));
        create virtual table if not exists docs using fts5(text, agent unindexed, session unindexed,
            role unindexed, ts unindexed, path unindexed, tokenize = 'unicode61 remove_diacritics 2');
    """)
    if "resume" not in {r[1] for r in db.execute("pragma table_info(sessions)")}:
        db.execute("alter table sessions add column resume text")
    return db


def texts(content):
    if isinstance(content, str):
        return [content]
    return [c["text"] for c in content or []
            if isinstance(c, dict) and c.get("type") in ("text", "input_text", "output_text") and c.get("text")]


def human(t):
    t = t.strip()
    return t if t and not t.startswith("<") else ""


def file_lines(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        yield from handle


def parse_claude(path):
    sid, cwd, rows = path.stem, None, []
    for line in file_lines(path):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
            continue
        cwd = cwd or d.get("cwd")
        sid = d.get("sessionId") or sid
        for t in texts((d.get("message") or {}).get("content")):
            t = human(t)
            if t:
                rows.append((t, d["type"], d.get("timestamp", "")))
    return sid, cwd, rows


def parse_codex(path):
    sid, cwd, rows = None, None, []
    for line in file_lines(path):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        p = d.get("payload") or {}
        if d.get("type") == "session_meta":
            sid, cwd = p.get("id"), p.get("cwd")
        elif d.get("type") == "response_item" and p.get("type") == "message" and p.get("role") in ("user", "assistant"):
            for t in texts(p.get("content")):
                t = human(t)
                if t:
                    rows.append((t, p["role"], d.get("timestamp", "")))
    return sid or path.stem, cwd, rows


def store(db, agent, path, sid, cwd, rows, title=None, resume=None):
    db.execute("delete from docs where path = ?", (str(path),))
    db.executemany("insert into docs (text, agent, session, role, ts, path) values (?, ?, ?, ?, ?, ?)",
                   [(t, agent, sid, role, ts, str(path)) for t, role, ts in rows])
    if rows:
        first = next((t for t, role, _ in rows if role == "user"), rows[0][0])
        db.execute("insert or replace into sessions (agent, id, cwd, title, last, resume) values (?, ?, ?, ?, ?, ?)",
                   (agent, sid, cwd, title or first.splitlines()[0][:120], max(ts for _, _, ts in rows), json.dumps(resume) if resume else None))


def parse_pi(path):
    sid, cwd, rows = path.stem, None, []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "session":
            sid, cwd = entry.get("id") or sid, entry.get("cwd")
        message = entry.get("message") or {}
        if entry.get("type") == "message" and message.get("role") in ("user", "assistant"):
            for body in texts(message.get("content")):
                if human(body):
                    rows.append((human(body), message["role"], entry.get("timestamp") or ""))
    return sid, cwd, rows


def parse_custom(path):
    """Normalized JSON sessions or JSONL session/message records; no agent-specific guessing."""
    if path.suffix == ".jsonl":
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        head = next((e for e in entries if e.get("type") == "session"), {})
        messages = [e.get("message", e) for e in entries if e.get("type") != "session"]
    else:
        head = json.loads(path.read_text(encoding="utf-8"))
        messages = head.get("messages", [])
    rows = []
    for message in messages:
        if message.get("role") not in ("user", "assistant"):
            continue
        for body in texts(message.get("content", message.get("text", ""))):
            if human(body):
                rows.append((human(body), message["role"], str(message.get("timestamp") or "")))
    return str(head.get("id") or path.stem), head.get("cwd"), rows, head.get("title")


def index_antigravity(db, path):
    """Use Antigravity CLI's own readable full-text chunks, not its private protobuf payloads."""
    db.execute("delete from docs where agent = 'antigravity'")
    db.execute("delete from sessions where agent = 'antigravity'")
    with closing(readonly(path)) as src:
        rows = src.execute("""select f.conversation_id, f.chunk_index, f.last_step_created_at,
            f.user_body, f.agent_body, s.title, s.workspace_uris
            from conversations_fts f join conversation_summaries s
            on s.conversation_id = f.conversation_id order by f.conversation_id, f.chunk_index""").fetchall()
    grouped = {}
    for sid, chunk, at, user, assistant, title, uris in rows:
        if sid not in grouped:
            workspace = (json.loads(uris or "[]") or [""])[0]
            uri = urlparse(workspace)
            cwd = unquote(uri.path) if uri.scheme == "file" else None
            if cwd and re.match(r"^/[A-Za-z]:/", cwd):
                cwd = cwd[1:]
            if cwd and uri.netloc:
                cwd = "//" + uri.netloc + cwd
            grouped[sid] = {"cwd": cwd, "title": title, "rows": []}
        for role, body in (("user", user), ("assistant", assistant)):
            if body and human(body):
                grouped[sid]["rows"].append((human(body), role, str(at or "")))
    for sid, item in grouped.items():
        store(db, "antigravity", f"antigravity:{sid}", sid, item["cwd"], item["rows"], item["title"], ["agy", "--conversation", sid])


def index(db):
    seen = set()
    jobs = [("claude", p, parse_claude) for p in (CLAUDE / "projects").glob("*/*.jsonl")]
    jobs += [("codex", p, parse_codex) for p in (CODEX / "sessions").rglob("rollout-*.jsonl")]
    jobs += [("pi", p, parse_pi) for p in (PI / "sessions").glob("*/*.jsonl")]
    for agent, path, parse in jobs:
        seen.add(str(path))
        def load(agent=agent, path=path, parse=parse):
            sid, cwd, rows = parse(path)
            resume = ["pi", "--session", str(path)] if agent == "pi" else None
            store(db, agent, path, sid, cwd, rows, resume=resume)
        update_source(db, str(path), path, load)
    for source in configured_sources():
        for pattern in source["paths"]:
            for name in glob.iglob(os.path.expandvars(os.path.expanduser(pattern)), recursive=True):
                path = Path(name)
                if not path.is_file():
                    continue
                key = f"custom:{source['agent']}:{path}"
                seen.add(key)
                def load(source=source, path=path, key=key):
                    sid, cwd, rows, title = parse_custom(path)
                    resume = [arg.replace("{id}", sid).replace("{path}", str(path)) for arg in source.get("resume", [])]
                    store(db, source["agent"], key, sid, cwd, rows, title, resume)
                update_source(db, key, path, load, json.dumps(source, sort_keys=True))
    for agent, path, loader in (("hermes", HERMES / "state.db", index_hermes),
                                ("antigravity", ANTIGRAVITY / "conversation_summaries.db", index_antigravity)):
        if path.exists():
            seen.add(agent + ":")
            update_source(db, agent + ":", path, lambda loader=loader, path=path: loader(db, path))
    for (gone,) in db.execute("select path from files").fetchall():
        if gone not in seen:
            if gone in ("hermes:", "antigravity:"):
                db.execute("delete from docs where agent = ?", (gone[:-1],))
                db.execute("delete from sessions where agent = ?", (gone[:-1],))
            else:
                db.execute("delete from docs where path = ?", (gone,))
            db.execute("delete from files where path = ?", (gone,))
    db.commit()


def update_source(db, key, path, load, extra=""):
    """A locked/corrupt source must not hide results from the other agents or destroy its last good index."""
    try:
        stamp = file_stamp(path) + extra
        previous = db.execute("select stamp from files where path = ?", (key,)).fetchone()
        if previous == (stamp,) or (path.suffix not in (".db", ".sqlite") and previous == (stamp + ":",)):
            return
        db.execute("savepoint source")
        try:
            load()
            db.execute("insert or replace into files values (?, ?)", (key, stamp))
        except Exception:
            db.execute("rollback to source")
            raise
        finally:
            db.execute("release source")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, sqlite3.Error) as error:
        print(f"Station Search: skipped source {key}: {error}", file=sys.stderr)


def index_hermes(db, state):
    src = readonly(state)
    db.execute("delete from docs where agent = 'hermes'")
    db.execute("delete from sessions where agent = 'hermes'")
    meta = {sid: (cwd, title) for sid, cwd, title in src.execute("select id, cwd, title from sessions")}
    by = {}
    for sid, role, content, ts in src.execute(
            "select session_id, role, content, timestamp from messages where role in ('user', 'assistant') and content != '' order by timestamp"):
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts))) if ts else ""
        t = human(str(content))
        if t:
            by.setdefault(sid, []).append((t, role, when))
    for sid, rows in by.items():
        cwd, title = meta.get(sid, (None, None))
        store(db, "hermes", f"hermes:{sid}", sid, cwd, rows, title)
    src.close()


def parents():
    """pid -> parent pid, for every process."""
    if sys.platform == "win32":
        import ctypes, ctypes.wintypes as wt

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("pid", wt.DWORD), ("heap", ctypes.c_size_t),
                        ("mod", wt.DWORD), ("threads", wt.DWORD), ("ppid", wt.DWORD), ("pri", ctypes.c_long),
                        ("flags", wt.DWORD), ("exe", ctypes.c_char * 260)]
        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
        snap = k32.CreateToolhelp32Snapshot(2, 0)
        e, out = PE(dwSize=ctypes.sizeof(PE)), {}
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            out[e.pid] = e.ppid
            ok = k32.Process32Next(snap, ctypes.byref(e))
        k32.CloseHandle(snap)
        return out
    if Path("/proc").is_dir():
        out = {}
        for p in Path("/proc").iterdir():
            if p.name.isdigit():
                try:
                    out[int(p.name)] = int((p / "stat").read_text().rsplit(")", 1)[1].split()[1])
                except (OSError, IndexError, ValueError):
                    pass
        return out
    ps = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True).stdout
    return {int(a): int(b) for a, b in (l.split() for l in ps.splitlines() if l.strip())}


def hc(*args):
    return subprocess.run([HC, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                          **({"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {})).stdout


def terminals():
    ts = json.loads(hc("ls", "--json") or "[]")
    ts = ts if isinstance(ts, list) else ts.get("terminals", [])
    out = []
    for t in ts:
        if t.get("state") == "live":
            try:
                detail = {**t, **json.loads(hc("get", t["address"], "--json"))}
                detail["cwd"] = (detail.get("foreground") or {}).get("cwd") or detail.get("cwd")
                out.append(detail)
            except (ValueError, OSError, subprocess.SubprocessError):
                out.append(t)
    return out


def thread_names():
    names, index = {}, CODEX / "session_index.jsonl"
    if index.exists():
        for line in open(index, encoding="utf-8", errors="replace"):
            try:
                d = json.loads(line)
                names[d["id"]] = d.get("thread_name") or names.get(d["id"])
            except (ValueError, KeyError):
                pass
    return names


def open_sessions(terms):
    """(agent, session id) -> the live terminal it runs in."""
    found = {}
    by_pid = {t.get("pid"): t["address"] for t in terms if t.get("pid")}
    tree = parents()
    for f in (CLAUDE / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid, seen = d.get("pid"), set()
        while pid and pid not in by_pid and pid not in seen:
            seen.add(pid)
            pid = tree.get(pid)
        if pid in by_pid:
            found[("claude", d.get("sessionId"))] = by_pid[pid]
    names = {}
    for sid, name in thread_names().items():
        names.setdefault(name, sid)
    for t in terms:
        if terminal_agent(t) == "codex" and t.get("title"):
            sid = names.get(t["title"].split(" | ")[0].strip())
            if sid:
                found[("codex", sid)] = t["address"]
        # CLI flags are stronger evidence than a title and also work for Antigravity/Pi.
        argv = (t.get("foreground") or {}).get("argv") or []
        for i, arg in enumerate(argv):
            for flag, agent in (("--conversation", "antigravity"), ("--session", "pi")):
                value = arg.split("=", 1)[1] if arg.startswith(flag + "=") else argv[i + 1] if arg == flag and i + 1 < len(argv) else None
                if value and terminal_agent(t) == agent:
                    if agent == "pi" and value.endswith(".jsonl"):
                        # Pi accepts a session file as well as a session id.
                        try:
                            value = parse_pi(Path(value))[0]
                        except OSError:
                            continue
                    found[(agent, value)] = t["address"]
    # Optional: a Hermes hook that records <session>.terminal files (see RECIPE.md).
    live = {t["address"] for t in terms}
    for f in (HERMES / "agent-hooks" / "state").glob("*.terminal"):
        try:
            addr = f.read_text().strip()
        except OSError:
            continue
        if addr in live:
            found[("hermes", f.stem)] = addr
    return found


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(c))


def live(terms, words, folder, skip, selected=()):
    words = [fold(w) for w in words]
    out = []
    for t in terms:
        agent = terminal_agent(t)
        if selected and agent not in selected:
            continue
        if t["address"] in skip or (folder and folder not in (t.get("cwd") or "").lower()):
            continue
        try:
            screen = hc("read", t["address"])
        except Exception:
            continue
        hits = [l.strip() for l in screen.splitlines() if all(w in fold(l) for w in words)]
        if hits:
            title = t.get("title") or ""
            if not title or re.match(r"^([A-Za-z]:[\\/]|/)", title):
                title = (agent or "terminal").capitalize() if agent and agent != "terminal" else t["address"]
            out.append({"kind": "live", "agent": agent or "terminal", "terminal": t["address"],
                        "cwd": t.get("cwd"), "title": title, "when": "",
                        "snippet": hits[-1][:200], "hits": len(hits)})
    return out


def fts(words):
    return " ".join('"' + w.replace('"', '""') + '"*' for w in words)


def query(text):
    db = connect()
    index(db)
    agents, folder, words = [], None, []
    for tok in text.split():
        low = tok.lower()
        if re.fullmatch(r"/[a-z][a-z0-9_-]*", low):
            agents.append(agent_id(low[1:]))
        elif low.startswith("#") and len(low) > 1:
            folder = low[1:]
        else:
            words.append(low)
    try:
        terms = terminals()
    except (OSError, ValueError, subprocess.SubprocessError):
        terms = []
    try:
        opened = open_sessions(terms)
    except Exception:
        opened = {}
    names = thread_names()
    want = [a for a in agents if a != "open"]
    results = []
    if words:
        restrict = f"and agent in ({','.join('?' * len(want))})" if want else ""
        rows = db.execute(f"""
            select agent, session, ts, snippet(docs, 0, '[', ']', '…', 14), rowid from docs
            where docs match ? {restrict}
            order by ts desc limit 3000""", [fts(words), *want]).fetchall()
        groups = {}
        for agent, sid, when, snip, rowid in rows:
            g = groups.setdefault((agent, sid), {"when": when, "snippet": " ".join(snip.split()), "hits": 0, "row": rowid})
            g["hits"] += 1
        for (agent, sid), g in groups.items():
            cwd, title, resume = db.execute("select cwd, title, resume from sessions where agent = ? and id = ?", (agent, sid)).fetchone() or (None, None, None)
            if folder and folder not in (cwd or "").lower():
                continue
            if agent == "codex" and names.get(sid):
                title = names[sid]
            terminal = opened.get((agent, sid))
            if "open" in agents and not terminal:
                continue
            results.append({"kind": "session", "agent": agent, "session": sid, "cwd": cwd, "title": title or sid,
                            "terminal": terminal, "resume": json.loads(resume) if resume else None, **g})
            if len(results) >= 60:
                break
    if "open" in agents:
        results = [r for r in results if r.get("terminal")]
    if words:
        shown = {r["terminal"] for r in results if r.get("terminal")}
        results = live(terms, words, folder, shown, want) + results
    results.sort(key=lambda r: not r.get("terminal"))
    db.close()
    print(json.dumps(results, ensure_ascii=False))


def catalog():
    ids = set(AGENTS) | {s["agent"] for s in configured_sources()}
    try:
        ids.update(terminal_agent(t) for t in terminals())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    print(json.dumps([{"id": key, "name": NAMES.get(key, key.replace("-", " ").title())}
                      for key in sorted(ids) if key != "terminal"], ensure_ascii=False))


def context(row, text):
    """The matched message (marks: \\x01 … \\x02) with two messages before and after it."""
    db = connect()
    hit = db.execute("select agent, session from docs where rowid = ?", (row,)).fetchone()
    if not hit:
        db.close()
        print("[]")
        return
    words = [w for w in text.split() if not w.startswith(("/", "#"))]
    marked = db.execute("select highlight(docs, 0, char(1), char(2)) from docs where rowid = ? and docs match ?",
                        (row, fts(words))).fetchone() if words else None
    near = db.execute("""select rowid, role, ts, text from docs where agent = ? and session = ?
                         and rowid between ? and ? order by rowid""", (*hit, row - 40, row + 40)).fetchall()
    at = next(i for i, r in enumerate(near) if r[0] == row)
    out = []
    for rowid, role, ts, body in near[max(0, at - 2):at + 3]:
        if rowid == row:
            body = marked[0] if marked else body
            first = body.find("\x01")
            if len(body) > 2400 and first > 1200:
                body = "…" + body[first - 1000:]
            body = body[:2400] + ("…" if len(body) > 2400 else "")
        else:
            body = body[:700] + ("…" if len(body) > 700 else "")
        out.append({"role": role, "when": ts, "text": body, "hit": rowid == row})
    db.close()
    print(json.dumps(out, ensure_ascii=False))


def lang():
    """The system's UI language, two letters."""
    code = ""
    if sys.platform == "win32":
        import ctypes, locale
        code = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), "")
    if not code:
        code = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    if not code and sys.platform == "darwin":
        code = subprocess.run(["defaults", "read", "-g", "AppleLocale"], capture_output=True, text=True).stdout
    print(code.strip()[:2].lower() or "en")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1:2]
    if cmd == ["lang"]:
        lang()
    elif cmd == ["agents"]:
        catalog()
    elif cmd == ["index"]:
        t = time.time()
        with closing(connect()) as db:
            index(db)
        print(f"indexed in {time.time() - t:.1f}s")
    elif cmd == ["query"]:
        query(" ".join(sys.argv[2:]))
    elif cmd == ["context"]:
        context(int(sys.argv[2]), " ".join(sys.argv[3:]))
