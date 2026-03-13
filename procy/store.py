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
        conn.execute("PRAGMA foreign_keys=ON")
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
                CREATE TABLE IF NOT EXISTS terminal_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_num INTEGER NOT NULL,
                    stream TEXT NOT NULL,        -- 'stdout'|'stdin'|'meta'
                    payload BLOB NOT NULL,
                    timestamp REAL NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_turns_session_turn
                    ON turns(session_id, turn_num, timestamp);
                CREATE INDEX IF NOT EXISTS idx_actions_session_turn
                    ON actions(session_id, turn_num, timestamp);
                CREATE INDEX IF NOT EXISTS idx_corrections_session_turn
                    ON corrections(session_id, turn_num, timestamp);
                CREATE INDEX IF NOT EXISTS idx_evolve_session_iter
                    ON evolve_runs(session_id, iteration);
                CREATE INDEX IF NOT EXISTS idx_terminal_events_session_id
                    ON terminal_events(session_id, id);
            """)

    def _session_exists(self, c: sqlite3.Connection, session_id: str) -> bool:
        row = c.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
        return row is not None

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

    def mark_session_running(self, session_id: str) -> None:
        with self._conn() as c:
            if not self._session_exists(c, session_id):
                raise ValueError(f"unknown session_id: {session_id}")
            c.execute(
                "UPDATE sessions SET ended_at=NULL, status='running' WHERE id=?",
                (session_id,),
            )

    def set_agent_session(self, session_id: str, agent_session_id: str) -> None:
        with self._conn() as c:
            if not self._session_exists(c, session_id):
                raise ValueError(f"unknown session_id: {session_id}")
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
            if not self._session_exists(c, session_id):
                raise ValueError(f"unknown session_id: {session_id}")
            cur = c.execute(
                "INSERT INTO turns (session_id, turn_num, role, content, timestamp, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, turn_num, role, content, time.time(),
                 json.dumps(metadata) if metadata else None),
            )
            return cur.lastrowid

    def append_turn_content(
        self,
        session_id: str,
        turn_num: int,
        role: str,
        content: str,
    ) -> int:
        """Append text to a single turn row (creates it if missing)."""
        with self._conn() as c:
            if not self._session_exists(c, session_id):
                raise ValueError(f"unknown session_id: {session_id}")
            row = c.execute(
                "SELECT id FROM turns WHERE session_id=? AND turn_num=? AND role=? ORDER BY id LIMIT 1",
                (session_id, turn_num, role),
            ).fetchone()
            if row:
                tid = int(row["id"])
                c.execute(
                    "UPDATE turns SET content=content || ?, timestamp=? WHERE id=?",
                    (content, time.time(), tid),
                )
                return tid
            cur = c.execute(
                "INSERT INTO turns (session_id, turn_num, role, content, timestamp, metadata) "
                "VALUES (?, ?, ?, ?, ?, NULL)",
                (session_id, turn_num, role, content, time.time()),
            )
            return cur.lastrowid

    def log_action(
        self, session_id: str, turn_num: int,
        tool_name: str, tool_input: str, tool_result: str,
    ) -> int:
        with self._conn() as c:
            if not self._session_exists(c, session_id):
                raise ValueError(f"unknown session_id: {session_id}")
            cur = c.execute(
                "INSERT INTO actions (session_id, turn_num, tool_name, tool_input, tool_result, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, turn_num, tool_name, tool_input, tool_result, time.time()),
            )
            return cur.lastrowid

    def log_diff(self, session_id: str, turn_num: int, diff_text: str) -> int:
        with self._conn() as c:
            if not self._session_exists(c, session_id):
                raise ValueError(f"unknown session_id: {session_id}")
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
            if not self._session_exists(c, session_id):
                raise ValueError(f"unknown session_id: {session_id}")
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
            if not self._session_exists(c, session_id):
                raise ValueError(f"unknown session_id: {session_id}")
            cur = c.execute(
                "INSERT INTO evolve_runs (session_id, iteration, prompt, response_summary, eval_result, score, source, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, iteration, prompt, response_summary,
                 json.dumps(eval_result) if eval_result else None,
                 score, source, time.time()),
            )
            return cur.lastrowid

    def log_terminal_event(
        self,
        session_id: str,
        turn_num: int,
        stream: str,
        payload: bytes,
    ) -> int:
        with self._conn() as c:
            if not self._session_exists(c, session_id):
                raise ValueError(f"unknown session_id: {session_id}")
            cur = c.execute(
                "INSERT INTO terminal_events (session_id, turn_num, stream, payload, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, turn_num, stream, sqlite3.Binary(payload), time.time()),
            )
            return cur.lastrowid

    # ── Queries ──

    def get_session(self, session_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def resolve_session_id(self, value: str) -> str | None:
        """Resolve an exact id or unique prefix to a full session id."""
        v = (value or "").strip()
        if not v:
            return None
        with self._conn() as c:
            row = c.execute("SELECT id FROM sessions WHERE id=?", (v,)).fetchone()
            if row:
                return str(row["id"])
            rows = c.execute(
                "SELECT id FROM sessions WHERE id LIKE ? ORDER BY started_at DESC LIMIT 5",
                (f"{v}%",),
            ).fetchall()
            if len(rows) == 1:
                return str(rows[0]["id"])
            return None

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

    def update_evolve_response(self, evolve_id: int, response_summary: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE evolve_runs SET response_summary=? WHERE id=?",
                (response_summary, evolve_id),
            )

    def update_evolve_score(self, evolve_id: int, eval_result: dict | None, score: float | None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE evolve_runs SET eval_result=?, score=? WHERE id=?",
                (json.dumps(eval_result) if eval_result else None, score, evolve_id),
            )

    def get_evolve_runs(self, session_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM evolve_runs WHERE session_id=? ORDER BY iteration",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_terminal_events(
        self,
        session_id: str,
        after_id: int = 0,
        limit: int = 5000,
        turn_num: int | None = None,
    ) -> list[dict]:
        with self._conn() as c:
            if turn_num is None:
                rows = c.execute(
                    "SELECT * FROM terminal_events WHERE session_id=? AND id>? ORDER BY id LIMIT ?",
                    (session_id, after_id, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM terminal_events WHERE session_id=? AND turn_num=? AND id>? ORDER BY id LIMIT ?",
                    (session_id, turn_num, after_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def list_sessions(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_sessions_summary(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT
                    s.*,
                    COALESCE(tc.human_turns, 0) AS turn_count,
                    COALESCE(cc.correction_count, 0) AS correction_count,
                    COALESCE(ec.evolve_count, 0) AS evolve_count
                FROM sessions s
                LEFT JOIN (
                    SELECT session_id, COUNT(*) AS human_turns
                    FROM turns
                    WHERE role = 'human'
                    GROUP BY session_id
                ) tc ON tc.session_id = s.id
                LEFT JOIN (
                    SELECT session_id, COUNT(*) AS correction_count
                    FROM corrections
                    GROUP BY session_id
                ) cc ON cc.session_id = s.id
                LEFT JOIN (
                    SELECT session_id, COUNT(*) AS evolve_count
                    FROM evolve_runs
                    GROUP BY session_id
                ) ec ON ec.session_id = s.id
                ORDER BY s.started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_training_pairs(self) -> list[dict]:
        """Get all correction pairs for SFT training."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT
                    c.*,
                    (
                        SELECT GROUP_CONCAT(x.content, '')
                        FROM (
                            SELECT t.content
                            FROM turns t
                            WHERE t.session_id = c.session_id
                              AND t.turn_num = c.turn_num
                              AND t.role IN ('agent', 'agent_chunk')
                            ORDER BY t.id ASC
                        ) x
                    ) AS agent_response
                FROM corrections c
                ORDER BY c.id
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def get_training_data_all(self) -> list[dict]:
        """Get all three categories of training data.

        Returns list of dicts with keys:
            category: 'human' | 'corrected' | 'proxy'
            prompt: the prompt text (corrected version for 'corrected')
            original_prompt: original proxy prompt (only for 'corrected')
            context: task + history context (for evolve-derived data)
            agent_response: the agent's response to this prompt
            session_id, turn_num, timestamp, score
        """
        results = []
        with self._conn() as c:
            # Category 1: Pure human prompts (no correction exists for this turn)
            human_rows = c.execute(
                """
                SELECT t.session_id, t.turn_num, t.content AS prompt, t.timestamp,
                    (
                        SELECT GROUP_CONCAT(x.content, '')
                        FROM (
                            SELECT t2.content FROM turns t2
                            WHERE t2.session_id = t.session_id
                              AND t2.turn_num = t.turn_num
                              AND t2.role IN ('agent', 'agent_chunk')
                            ORDER BY t2.id ASC
                        ) x
                    ) AS agent_response
                FROM turns t
                WHERE t.role = 'human'
                  AND NOT EXISTS (
                      SELECT 1 FROM corrections c
                      WHERE c.session_id = t.session_id AND c.turn_num = t.turn_num
                  )
                ORDER BY t.timestamp
                """
            ).fetchall()
            for r in human_rows:
                d = dict(r)
                d["category"] = "human"
                d["original_prompt"] = None
                d["context"] = None
                d["score"] = None
                results.append(d)

            # Category 2: Human-corrected prompts (correction exists)
            corr_rows = c.execute(
                """
                SELECT c.session_id, c.turn_num, c.corrected_prompt AS prompt,
                    c.original_prompt, c.timestamp,
                    (
                        SELECT GROUP_CONCAT(x.content, '')
                        FROM (
                            SELECT t.content FROM turns t
                            WHERE t.session_id = c.session_id
                              AND t.turn_num = c.turn_num
                              AND t.role IN ('agent', 'agent_chunk')
                            ORDER BY t.id ASC
                        ) x
                    ) AS agent_response
                FROM corrections c
                ORDER BY c.timestamp
                """
            ).fetchall()
            for r in corr_rows:
                d = dict(r)
                d["category"] = "corrected"
                d["context"] = None
                d["score"] = None
                results.append(d)

            # Category 3: Pure proxy prompts (evolve-generated, not corrected)
            proxy_rows = c.execute(
                """
                SELECT e.session_id, e.iteration AS turn_num,
                    e.prompt, e.response_summary AS agent_response,
                    e.score, e.timestamp
                FROM evolve_runs e
                WHERE e.source = 'procy'
                  AND NOT EXISTS (
                      SELECT 1 FROM corrections c
                      WHERE c.session_id = e.session_id
                        AND c.original_prompt = e.prompt
                  )
                ORDER BY e.timestamp
                """
            ).fetchall()
            for r in proxy_rows:
                d = dict(r)
                d["category"] = "proxy"
                d["original_prompt"] = None
                d["context"] = None
                results.append(d)

        results.sort(key=lambda x: x.get("timestamp", 0))
        return results
