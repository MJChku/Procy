"""SQLite trace store for procy — records prompts, responses, actions, diffs."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path


class TraceStore:
    def __init__(self, db_path: str = "procy_traces.db"):
        self.db_path = db_path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    status TEXT NOT NULL DEFAULT 'running',
                    goal TEXT,
                    agent_session_id TEXT
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_num INTEGER NOT NULL,
                    role TEXT NOT NULL,          -- 'human', 'procy', 'agent'
                    content TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    metadata TEXT,               -- JSON: cost, model, etc.
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_num INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    tool_input TEXT,             -- JSON
                    tool_result TEXT,
                    timestamp REAL NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS diffs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_num INTEGER NOT NULL,
                    diff_text TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_num INTEGER NOT NULL,
                    original_prompt TEXT NOT NULL,
                    corrected_prompt TEXT NOT NULL,
                    note TEXT,
                    timestamp REAL NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS evolve_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    iteration INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    response_summary TEXT,
                    eval_result TEXT,            -- JSON
                    score REAL,
                    source TEXT NOT NULL,        -- 'human' or 'procy'
                    timestamp REAL NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
            """)

    # ── Session lifecycle ──

    def new_session(self, goal: str | None = None) -> str:
        sid = str(uuid.uuid4())
        with self._conn() as c:
            c.execute(
                "INSERT INTO sessions (id, started_at, goal) VALUES (?, ?, ?)",
                (sid, time.time(), goal),
            )
        return sid

    def end_session(self, session_id: str, status: str = "done") -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE sessions SET ended_at=?, status=? WHERE id=?",
                (time.time(), status, session_id),
            )

    def set_agent_session(self, session_id: str, agent_session_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE sessions SET agent_session_id=? WHERE id=?",
                (agent_session_id, session_id),
            )

    # ── Turn logging ──

    def log_turn(
        self, session_id: str, turn_num: int, role: str, content: str,
        metadata: dict | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO turns (session_id, turn_num, role, content, timestamp, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, turn_num, role, content, time.time(),
                 json.dumps(metadata) if metadata else None),
            )
            return cur.lastrowid

    def log_action(
        self, session_id: str, turn_num: int,
        tool_name: str, tool_input: str, tool_result: str,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO actions (session_id, turn_num, tool_name, tool_input, tool_result, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, turn_num, tool_name, tool_input, tool_result, time.time()),
            )
            return cur.lastrowid

    def log_diff(self, session_id: str, turn_num: int, diff_text: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO diffs (session_id, turn_num, diff_text, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (session_id, turn_num, diff_text, time.time()),
            )
            return cur.lastrowid

    def log_correction(
        self, session_id: str, turn_num: int,
        original: str, corrected: str, note: str | None = None,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO corrections (session_id, turn_num, original_prompt, corrected_prompt, note, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, turn_num, original, corrected, note, time.time()),
            )
            return cur.lastrowid

    def log_evolve(
        self, session_id: str, iteration: int, prompt: str,
        response_summary: str | None, eval_result: dict | None,
        score: float | None, source: str,
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO evolve_runs (session_id, iteration, prompt, response_summary, eval_result, score, source, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, iteration, prompt, response_summary,
                 json.dumps(eval_result) if eval_result else None,
                 score, source, time.time()),
            )
            return cur.lastrowid

    # ── Queries ──

    def get_session(self, session_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def get_turns(self, session_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM turns WHERE session_id=? ORDER BY turn_num, timestamp",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_actions(self, session_id: str, turn_num: int | None = None) -> list[dict]:
        with self._conn() as c:
            if turn_num is not None:
                rows = c.execute(
                    "SELECT * FROM actions WHERE session_id=? AND turn_num=? ORDER BY id",
                    (session_id, turn_num),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM actions WHERE session_id=? ORDER BY id",
                    (session_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_corrections(self, session_id: str | None = None) -> list[dict]:
        with self._conn() as c:
            if session_id:
                rows = c.execute(
                    "SELECT * FROM corrections WHERE session_id=? ORDER BY id",
                    (session_id,),
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM corrections ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def get_evolve_runs(self, session_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM evolve_runs WHERE session_id=? ORDER BY iteration",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_sessions(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_training_pairs(self) -> list[dict]:
        """Get all correction pairs for SFT training."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT c.*, t.content as agent_response FROM corrections c "
                "LEFT JOIN turns t ON c.session_id = t.session_id "
                "AND c.turn_num = t.turn_num AND t.role = 'agent' "
                "ORDER BY c.id"
            ).fetchall()
            return [dict(r) for r in rows]
