"""Station Search's engine: one full-text index over coding agents' conversations, plus live terminals.

    python search.py index                    update the index (only changed files)
    python search.py query <text>             update, then print matches as JSON
    python search.py context <row> <text>     a match with the messages around it, as JSON

Reads Claude Code (~/.claude/projects), Codex ($CODEX_HOME or ~/.codex) and Hermes Agent
($HERMES_HOME or ~/.hermes). The index lives in ~/.cache/station-search/index.db
($STATION_SEARCH_DB overrides it). Words are ANDed, prefix-matched and accent-blind.
Filters: /claude /codex /hermes (agents), /open (only what's running now), #word (folder).
"""
import json, os, re, shutil, sqlite3, subprocess, sys, time, unicodedata
from pathlib import Path

HOME = Path.home()
DB = Path(os.environ.get("STATION_SEARCH_DB") or HOME / ".cache" / "station-search" / "index.db")
CLAUDE = HOME / ".claude"
CODEX = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
HERMES = Path(os.environ.get("HERMES_HOME") or HOME / ".hermes")
AGENTS = ("claude", "codex", "hermes")


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
    return db


def texts(content):
    if isinstance(content, str):
        return [content]
    return [c["text"] for c in content or []
            if isinstance(c, dict) and c.get("type") in ("text", "input_text", "output_text") and c.get("text")]


def human(t):
    t = t.strip()
    return t if t and not t.startswith("<") else ""


def parse_claude(path):
    sid, cwd, rows = path.stem, None, []
    for line in open(path, encoding="utf-8", errors="replace"):
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
    for line in open(path, encoding="utf-8", errors="replace"):
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


def store(db, agent, path, sid, cwd, rows, title=None):
    db.execute("delete from docs where path = ?", (str(path),))
    db.executemany("insert into docs (text, agent, session, role, ts, path) values (?, ?, ?, ?, ?, ?)",
                   [(t, agent, sid, role, ts, str(path)) for t, role, ts in rows])
    if rows:
        first = next((t for t, role, _ in rows if role == "user"), rows[0][0])
        db.execute("insert or replace into sessions values (?, ?, ?, ?, ?)",
                   (agent, sid, cwd, title or first.splitlines()[0][:120], max(ts for _, _, ts in rows)))


def index(db):
    seen = set()
    jobs = [("claude", p, parse_claude) for p in (CLAUDE / "projects").glob("*/*.jsonl")]
    jobs += [("codex", p, parse_codex) for p in (CODEX / "sessions").rglob("rollout-*.jsonl")]
    for agent, path, parse in jobs:
        st = path.stat()
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
        seen.add(str(path))
        if db.execute("select stamp from files where path = ?", (str(path),)).fetchone() == (stamp,):
            continue
        sid, cwd, rows = parse(path)
        store(db, agent, path, sid, cwd, rows)
        db.execute("insert or replace into files values (?, ?)", (str(path), stamp))
        db.commit()
    for (gone,) in db.execute("select path from files").fetchall():
        if gone not in seen and not gone.startswith("hermes:"):
            db.execute("delete from docs where path = ?", (gone,))
            db.execute("delete from files where path = ?", (gone,))
    state = HERMES / "state.db"
    if state.exists():
        wal = HERMES / "state.db-wal"
        stamp = f"{state.stat().st_mtime_ns}:{wal.stat().st_mtime_ns if wal.exists() else ''}"
        if db.execute("select stamp from files where path = 'hermes:'").fetchone() != (stamp,):
            index_hermes(db, state)
            db.execute("insert or replace into files values ('hermes:', ?)", (stamp,))
    db.commit()


def index_hermes(db, state):
    src = sqlite3.connect(f"file:{state}?mode=ro", uri=True)
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
    return subprocess.run([HC, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout


def terminals():
    ts = json.loads(hc("ls", "--json") or "[]")
    ts = ts if isinstance(ts, list) else ts.get("terminals", [])
    out = []
    for t in ts:
        if t.get("state") == "live":
            try:
                out.append({**t, **json.loads(hc("get", t["address"], "--json"))})
            except ValueError:
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
        if t.get("agent") == "codex" and t.get("title"):
            sid = names.get(t["title"].split(" | ")[0].strip())
            if sid:
                found[("codex", sid)] = t["address"]
    # Optional: a Hermes hook that records <session>.terminal files (see RECIPE.md).
    live = {t["address"] for t in terms}
    for f in (HERMES / "agent-hooks" / "state").glob("*.terminal"):
        addr = f.read_text().strip()
        if addr in live:
            found[("hermes", f.stem)] = addr
    return found


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(c))


def live(terms, words, folder, skip):
    words = [fold(w) for w in words]
    out = []
    for t in terms:
        if t["address"] in skip or (folder and folder not in (t.get("cwd") or "").lower()):
            continue
        try:
            screen = hc("read", t["address"])
        except Exception:
            continue
        hits = [l.strip() for l in screen.splitlines() if all(w in fold(l) for w in words)]
        if hits:
            agent = {"claude-code": "claude"}.get(t.get("agent"), t.get("agent")) if t.get("status") else "terminal"
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
        if low.startswith("/") and low[1:] in AGENTS + ("open",):
            agents.append(low[1:])
        elif low.startswith("#") and len(low) > 1:
            folder = low[1:]
        else:
            words.append(low)
    try:
        terms = terminals()
        opened = open_sessions(terms)
    except Exception:
        terms, opened = [], {}
    names = thread_names()
    want = [a for a in agents if a in AGENTS] or list(AGENTS)
    results = []
    if words:
        rows = db.execute(f"""
            select agent, session, ts, snippet(docs, 0, '[', ']', '…', 14), rowid from docs
            where docs match ? and agent in ({",".join("?" * len(want))})
            order by ts desc limit 3000""", [fts(words), *want]).fetchall()
        groups = {}
        for agent, sid, when, snip, rowid in rows:
            g = groups.setdefault((agent, sid), {"when": when, "snippet": " ".join(snip.split()), "hits": 0, "row": rowid})
            g["hits"] += 1
        for (agent, sid), g in list(groups.items())[:60]:
            cwd, title = db.execute("select cwd, title from sessions where agent = ? and id = ?", (agent, sid)).fetchone() or (None, None)
            if folder and folder not in (cwd or "").lower():
                continue
            if agent == "codex" and names.get(sid):
                title = names[sid]
            results.append({"kind": "session", "agent": agent, "session": sid, "cwd": cwd, "title": title or sid,
                            "terminal": opened.get((agent, sid)), **g})
    if "open" in agents:
        results = [r for r in results if r.get("terminal")]
    if words and (not agents or "open" in agents):
        shown = {r["terminal"] for r in results if r.get("terminal")}
        results = live(terms, words, folder, shown) + results
    results.sort(key=lambda r: not r.get("terminal"))
    print(json.dumps(results, ensure_ascii=False))


def context(row, text):
    """The matched message (marks: \\x01 … \\x02) with two messages before and after it."""
    db = connect()
    hit = db.execute("select agent, session from docs where rowid = ?", (row,)).fetchone()
    if not hit:
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
    elif cmd == ["index"]:
        t = time.time()
        index(connect())
        print(f"indexed in {time.time() - t:.1f}s")
    elif cmd == ["query"]:
        query(" ".join(sys.argv[2:]))
    elif cmd == ["context"]:
        context(int(sys.argv[2]), " ".join(sys.argv[3:]))
