"""Search integration tests with synthetic histories only; never read a user's agent data."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("station_search", Path(__file__).parents[1] / "mods/station-search/search.py")
search = importlib.util.module_from_spec(spec)
spec.loader.exec_module(search)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name in ("HOME", "CLAUDE", "CODEX", "HERMES", "ANTIGRAVITY", "PI"):
            setattr(search, name, root / name.lower())
        search.DB = root / "index.db"
        search.SOURCES = root / "sources.json"
        search.terminals = lambda: []
        search.parents = lambda: {}
        search.thread_names = lambda: {}
        search.hc = lambda *args: ""

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def query(self, text):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            search.query(text)
        return json.loads(output.getvalue())

    def antigravity(self):
        path = search.ANTIGRAVITY / "conversation_summaries.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
                create table conversation_summaries (conversation_id text, title text, workspace_uris text);
                create virtual table conversations_fts using fts5(conversation_id unindexed,
                    chunk_index unindexed, last_step_created_at unindexed, user_body, agent_body);
            """)
            for sid, folder in (("agy-one", "atlas"), ("agy-two", "api")):
                db.execute("insert into conversation_summaries values (?, ?, ?)",
                           (sid, "Fix the login", json.dumps(["file:///projects/" + folder])))
                db.execute("insert into conversations_fts values (?, ?, ?, ?, ?)",
                           (sid, 0, "2026-11-16T10:00:00Z", "Cómo reparar login", "Login repaired"))
        return path

    def test_antigravity_multiple_sessions_context_and_resume(self):
        self.antigravity()
        results = self.query("/antigravity como")
        self.assertEqual({r["session"] for r in results}, {"agy-one", "agy-two"})
        self.assertEqual(results[0]["resume"][:2], ["agy", "--conversation"])
        self.assertEqual({r["cwd"] for r in results}, {"/projects/atlas", "/projects/api"})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            search.context(results[0]["row"], "/antigravity como")
        messages = json.loads(output.getvalue())
        self.assertTrue(any(m["hit"] and "\x01" in m["text"] for m in messages))
        self.assertEqual({m["role"] for m in messages}, {"user", "assistant"})
        self.assertEqual(len(self.query("/agy #api login")), 1)

    def test_open_antigravity_uses_foreground_conversation_flag(self):
        self.antigravity()
        search.terminals = lambda: [{"address": "t8", "agent": "antigravity", "status": {"app": "antigravity"},
            "foreground": {"argv": ["agy", "--conversation=agy-one"]}}]
        results = self.query("/antigravity /open login")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["terminal"], "t8")

    def test_any_live_agent_and_combined_filters(self):
        search.terminals = lambda: [
            {"address": "t9", "agent": "future-agent", "status": {"app": "future-agent"}, "cwd": "/projects/api"},
            {"address": "t10", "agent": "antigravity", "status": "idle", "cwd": "/projects/api"}]
        search.hc = lambda *args: "The login is fixed" if args[0] == "read" else ""
        results = self.query("/future-agent /open #api login")
        self.assertEqual([(r["agent"], r["terminal"]) for r in results], [("future-agent", "t9")])
        self.assertEqual(len(self.query("login")), 2)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            search.catalog()
        self.assertIn("future-agent", {a["id"] for a in json.loads(output.getvalue())})

    def test_pi_history_and_resume_file(self):
        path = search.PI / "sessions/demo/session.jsonl"
        entries = [{"type": "session", "id": "pi-one", "cwd": "/projects/demo"},
                   {"type": "message", "timestamp": "2026-11-16T10:00:00Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "Review the login"}]}},
                   {"type": "message", "message": {"role": "toolResult", "content": "login secret tool output"}}]
        self.write(path, "\n".join(json.dumps(e) for e in entries))
        result, = self.query("/pi login")
        self.assertEqual(result["session"], "pi-one")
        self.assertEqual(result["resume"], ["pi", "--session", str(path)])
        self.assertEqual(self.query("/pi secret"), [])

    def test_custom_history_and_no_implicit_resume(self):
        path = search.HOME / "exports/session.json"
        self.write(path, json.dumps({"id": "future-one", "cwd": "/projects/demo", "title": "Custom login",
            "messages": [{"role": "user", "content": "Cómo hacer login", "timestamp": "2026-11-16T10:00:00Z"}]}))
        source = {"agent": "future-agent", "paths": [str(path)]}
        self.write(search.SOURCES, json.dumps([source]))
        result, = self.query("/future-agent como")
        self.assertIsNone(result["resume"])
        source["resume"] = ["future-agent", "resume", "{id}"]
        self.write(search.SOURCES, json.dumps([source]))
        result, = self.query("/future-agent login")
        self.assertEqual(result["resume"], ["future-agent", "resume", "future-one"])

    def test_legacy_index_migration_and_claude_codex(self):
        with contextlib.closing(sqlite3.connect(search.DB)) as db, db:
            db.execute("create table sessions(agent text, id text, cwd text, title text, last text, primary key(agent,id))")
        self.write(search.CLAUDE / "projects/demo/one.jsonl", json.dumps({"type": "user", "sessionId": "claude-one",
            "cwd": "/projects/demo", "message": {"content": "Cómo hacer login"}, "timestamp": "2026-11-16T10:00:00Z"}))
        self.write(search.CODEX / "sessions/rollout-one.jsonl", "\n".join(map(json.dumps, [
            {"type": "session_meta", "payload": {"id": "codex-one", "cwd": "/projects/demo"}},
            {"type": "response_item", "timestamp": "2026-11-16T10:00:00Z", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Fix login"}]}}])))
        self.assertEqual({r["agent"] for r in self.query("login")}, {"claude", "codex"})
        self.assertEqual(len(self.query("/claude-code como")), 1)

    def test_bad_source_keeps_last_good_results_and_other_agents(self):
        path = self.antigravity()
        self.assertEqual(len(self.query("/antigravity login")), 2)
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("drop table conversations_fts")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(len(self.query("/antigravity login")), 2)

    def test_broken_history_mapping_does_not_hide_live_agents(self):
        search.terminals = lambda: [{"address": "t9", "agent": "future-agent", "status": "idle"}]
        search.hc = lambda *args: "login repaired"
        old_parents = search.parents
        def broken():
            raise OSError("process metadata unavailable")
        search.parents = broken
        try:
            result, = self.query("/future-agent login")
            self.assertEqual(result["terminal"], "t9")
        finally:
            search.parents = old_parents

    def test_hermes_history_survives_schema_migration(self):
        path = search.HERMES / "state.db"
        path.parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.executescript("create table sessions(id text,cwd text,title text);"
                             "create table messages(session_id text,role text,content text,timestamp real);")
            db.execute("insert into sessions values ('hermes-one','/projects/demo','Login repair')")
            db.execute("insert into messages values ('hermes-one','user','Repair login',1794823200)")
        result, = self.query("/hermes login")
        self.assertEqual(result["session"], "hermes-one")

    def test_wal_changes_and_deleted_source(self):
        path = self.antigravity()
        self.assertEqual(len(self.query("/antigravity login")), 2)
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("pragma journal_mode=wal")
            db.execute("insert into conversations_fts values ('agy-one',1,'2026-11-16T11:00:00Z','new uniquephrase','answer')")
            db.commit()
            self.assertEqual(len(self.query("uniquephrase")), 1)
        path.unlink()
        self.assertEqual(self.query("/antigravity login"), [])


if __name__ == "__main__":
    unittest.main()
