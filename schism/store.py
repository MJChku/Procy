"""SQLite catalog store for Schism — tools, sessions, feedback, factory runs."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHISM_HOME = Path.home() / ".schism"


class SchismStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(SCHISM_HOME / "catalog.db")
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            # Migration: factory_runs.progress_id
            cols = {row[1] for row in c.execute("PRAGMA table_info(factory_runs)")}
            if "progress_id" not in cols and cols:
                c.execute("ALTER TABLE factory_runs ADD COLUMN progress_id INTEGER")

            # Migration: tools.session_id + drop old UNIQUE(name) → UNIQUE(name, session_id)
            tool_cols = {row[1] for row in c.execute("PRAGMA table_info(tools)")}
            if "session_id" not in tool_cols and tool_cols:
                c.executescript("""
                    CREATE TABLE tools_migrated (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        session_id TEXT,
                        capability TEXT NOT NULL,
                        use_cases TEXT NOT NULL DEFAULT '[]',
                        patterns TEXT NOT NULL DEFAULT '[]',
                        requirements TEXT NOT NULL DEFAULT '[]',
                        code TEXT NOT NULL DEFAULT '',
                        tool_type TEXT NOT NULL DEFAULT 'mcp',
                        install_path TEXT,
                        is_installed BOOLEAN DEFAULT 0,
                        generation INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(name, session_id)
                    );
                    INSERT INTO tools_migrated
                        SELECT id, name, NULL, capability, use_cases, patterns,
                               requirements, code, tool_type, install_path,
                               is_installed, generation, created_at, updated_at
                        FROM tools;
                    DROP TABLE tools;
                    ALTER TABLE tools_migrated RENAME TO tools;
                """)

            c.executescript("""
                CREATE TABLE IF NOT EXISTS tools (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    session_id TEXT,
                    capability TEXT NOT NULL,
                    use_cases TEXT NOT NULL DEFAULT '[]',
                    patterns TEXT NOT NULL DEFAULT '[]',
                    requirements TEXT NOT NULL DEFAULT '[]',
                    code TEXT NOT NULL DEFAULT '',
                    tool_type TEXT NOT NULL DEFAULT 'mcp',
                    install_path TEXT,
                    is_installed BOOLEAN DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(name, session_id)
                );

                CREATE TABLE IF NOT EXISTS tool_generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_id INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    capability TEXT NOT NULL,
                    use_cases TEXT NOT NULL,
                    patterns TEXT NOT NULL,
                    requirements TEXT NOT NULL,
                    code TEXT NOT NULL,
                    evolution_note TEXT,
                    factory_run_id INTEGER,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(tool_id) REFERENCES tools(id)
                );
                CREATE INDEX IF NOT EXISTS idx_gen_tool ON tool_generations(tool_id, generation DESC);

                CREATE VIRTUAL TABLE IF NOT EXISTS tools_fts USING fts5(
                    name, capability, use_cases
                );

                CREATE TABLE IF NOT EXISTS executive_sessions (
                    id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    task_summary TEXT
                );

                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    executive_session_id TEXT NOT NULL,
                    task TEXT NOT NULL,
                    tools_used TEXT NOT NULL DEFAULT '{}',
                    tools_unhelpful TEXT NOT NULL DEFAULT '{}',
                    challenges TEXT NOT NULL DEFAULT '',
                    processed BOOLEAN DEFAULT 0,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(executive_session_id) REFERENCES executive_sessions(id)
                );

                -- Mid-task progress events: fired whenever Claude solves a sub-problem
                CREATE TABLE IF NOT EXISTS progress_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    executive_session_id TEXT NOT NULL,
                    problem TEXT NOT NULL,        -- what challenge was encountered
                    solution TEXT NOT NULL,       -- how it was solved
                    commands_used TEXT NOT NULL DEFAULT '[]', -- JSON: actual commands/snippets
                    tool_name TEXT NOT NULL DEFAULT '',  -- Schism tool involved (if any)
                    processed BOOLEAN DEFAULT 0,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(executive_session_id) REFERENCES executive_sessions(id)
                );

                CREATE TABLE IF NOT EXISTS factory_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feedback_id INTEGER,
                    progress_id INTEGER,
                    mode TEXT NOT NULL DEFAULT 'cli',
                    model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
                    actions TEXT,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT,
                    duration_s REAL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(feedback_id) REFERENCES feedback(id)
                );

                CREATE TABLE IF NOT EXISTS installations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_id INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    install_path TEXT NOT NULL,
                    settings_backup_path TEXT,
                    installed_at REAL NOT NULL,
                    uninstalled_at REAL,
                    status TEXT NOT NULL DEFAULT 'active',
                    FOREIGN KEY(tool_id) REFERENCES tools(id)
                );
            """)

    # ── Tools ────────────────────────────────────────────────────────────────

    def create_tool(
        self,
        name: str,
        capability: str,
        use_cases: list[str],
        patterns: list[str],
        requirements: list[str],
        code: str,
        tool_type: str = "mcp",
        evolution_note: str = "",
        factory_run_id: int | None = None,
        session_id: str | None = None,
    ) -> int:
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO tools
                   (name, session_id, capability, use_cases, patterns, requirements, code,
                    tool_type, generation, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
                (
                    name, session_id, capability,
                    json.dumps(use_cases), json.dumps(patterns),
                    json.dumps(requirements), code, tool_type, now, now,
                ),
            )
            tool_id = cur.lastrowid
            # Store generation 1
            c.execute(
                """INSERT INTO tool_generations
                   (tool_id, generation, capability, use_cases, patterns,
                    requirements, code, evolution_note, factory_run_id, created_at)
                   VALUES (?,1,?,?,?,?,?,?,?,?)""",
                (
                    tool_id, capability,
                    json.dumps(use_cases), json.dumps(patterns),
                    json.dumps(requirements), code,
                    evolution_note, factory_run_id, now,
                ),
            )
            # Update FTS index
            c.execute(
                "INSERT INTO tools_fts(rowid, name, capability, use_cases) VALUES (?,?,?,?)",
                (tool_id, name, capability, json.dumps(use_cases)),
            )
        return tool_id

    def update_tool_generation(
        self,
        tool_id: int,
        capability: str,
        use_cases: list[str],
        patterns: list[str],
        requirements: list[str],
        code: str,
        evolution_note: str = "",
        factory_run_id: int | None = None,
    ) -> int:
        """Add a new generation. Returns new generation number."""
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT generation FROM tools WHERE id=?", (tool_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"Tool {tool_id} not found")
            new_gen = row["generation"] + 1

            c.execute(
                """INSERT INTO tool_generations
                   (tool_id, generation, capability, use_cases, patterns,
                    requirements, code, evolution_note, factory_run_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    tool_id, new_gen, capability,
                    json.dumps(use_cases), json.dumps(patterns),
                    json.dumps(requirements), code,
                    evolution_note, factory_run_id, now,
                ),
            )
            c.execute(
                """UPDATE tools SET generation=?, capability=?, use_cases=?,
                   patterns=?, requirements=?, code=?, updated_at=?
                   WHERE id=?""",
                (
                    new_gen, capability,
                    json.dumps(use_cases), json.dumps(patterns),
                    json.dumps(requirements), code, now, tool_id,
                ),
            )
            # Rebuild FTS for this tool
            c.execute("DELETE FROM tools_fts WHERE rowid=?", (tool_id,))
            c.execute(
                "INSERT INTO tools_fts(rowid, name, capability, use_cases) VALUES (?,?,?,?)",
                (tool_id, self.get_tool_by_id(tool_id)["name"],
                 capability, json.dumps(use_cases)),
            )
        return new_gen

    def rollback_tool(self, tool_id: int, generation: int) -> None:
        """Restore a tool to a specific generation's content."""
        with self._conn() as c:
            row = c.execute(
                """SELECT capability, use_cases, patterns, requirements, code
                   FROM tool_generations WHERE tool_id=? AND generation=?""",
                (tool_id, generation),
            ).fetchone()
            if not row:
                raise ValueError(f"Generation {generation} not found for tool {tool_id}")
            now = time.time()
            c.execute(
                """UPDATE tools SET generation=?, capability=?, use_cases=?,
                   patterns=?, requirements=?, code=?, updated_at=?
                   WHERE id=?""",
                (
                    generation, row["capability"],
                    row["use_cases"], row["patterns"],
                    row["requirements"], row["code"],
                    now, tool_id,
                ),
            )
            # Rebuild FTS
            name = c.execute(
                "SELECT name FROM tools WHERE id=?", (tool_id,)
            ).fetchone()["name"]
            c.execute("DELETE FROM tools_fts WHERE rowid=?", (tool_id,))
            c.execute(
                "INSERT INTO tools_fts(rowid, name, capability, use_cases) VALUES (?,?,?,?)",
                (tool_id, name, row["capability"], row["use_cases"]),
            )

    def get_tool(self, name: str, session_id: str | None = None) -> dict | None:
        with self._conn() as c:
            if session_id is not None:
                row = c.execute(
                    "SELECT * FROM tools WHERE name=? AND session_id=?", (name, session_id)
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM tools WHERE name=?", (name,)
                ).fetchone()
        return _row_to_dict(row) if row else None

    def get_tool_by_id(self, tool_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM tools WHERE id=?", (tool_id,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list_tools(
        self,
        tool_type: str | None = None,
        session_id: str | None = _UNSET := object(),
    ) -> list[dict]:
        """If session_id is provided (including None), filter by it.
        If omitted entirely, return all tools."""
        with self._conn() as c:
            conditions = []
            params: list = []
            if tool_type:
                conditions.append("tool_type=?")
                params.append(tool_type)
            if session_id is not _UNSET:
                if session_id is None:
                    conditions.append("session_id IS NULL")
                else:
                    conditions.append("session_id=?")
                    params.append(session_id)
            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            rows = c.execute(
                f"SELECT * FROM tools {where} ORDER BY name", params
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get_tool_generations(self, tool_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM tool_generations WHERE tool_id=?
                   ORDER BY generation ASC""",
                (tool_id,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def set_tool_installed(self, tool_id: int, install_path: str | None, installed: bool) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE tools SET install_path=?, is_installed=? WHERE id=?",
                (install_path, 1 if installed else 0, tool_id),
            )

    # ── Search ────────────────────────────────────────────────────────────────

    def search_tools(self, query: str, session_id: str | None = None) -> list[dict]:
        """FTS5 ranked search over name, capability, use_cases, filtered by session."""
        with self._conn() as c:
            if session_id is not None:
                rows = c.execute(
                    """SELECT t.* FROM tools t
                       JOIN tools_fts f ON t.id = f.rowid
                       WHERE tools_fts MATCH ? AND t.session_id=?
                       ORDER BY rank""",
                    (query, session_id),
                ).fetchall()
            else:
                rows = c.execute(
                    """SELECT t.* FROM tools t
                       JOIN tools_fts f ON t.id = f.rowid
                       WHERE tools_fts MATCH ?
                       ORDER BY rank""",
                    (query,),
                ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── Sessions & Feedback ──────────────────────────────────────────────────

    def record_session(self, session_id: str) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT OR IGNORE INTO executive_sessions(id, started_at)
                   VALUES (?,?)""",
                (session_id, now),
            )

    def end_session(self, session_id: str, task_summary: str = "") -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                """UPDATE executive_sessions SET ended_at=?, task_summary=?
                   WHERE id=?""",
                (now, task_summary, session_id),
            )

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM executive_sessions
                   ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def submit_feedback(
        self,
        executive_session_id: str,
        task: str,
        tools_used: dict,
        tools_unhelpful: dict,
        challenges: str,
    ) -> int:
        self.record_session(executive_session_id)
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO feedback
                   (executive_session_id, task, tools_used, tools_unhelpful,
                    challenges, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    executive_session_id, task,
                    json.dumps(tools_used), json.dumps(tools_unhelpful),
                    challenges, now,
                ),
            )
            # Update task_summary on the session
            c.execute(
                """UPDATE executive_sessions SET task_summary=?
                   WHERE id=? AND (task_summary IS NULL OR task_summary='')""",
                (task[:200], executive_session_id),
            )
        return cur.lastrowid

    def get_unprocessed_feedback(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM feedback WHERE processed=0 ORDER BY created_at"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def mark_feedback_processed(self, feedback_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE feedback SET processed=1 WHERE id=?", (feedback_id,)
            )

    # ── Progress Events ───────────────────────────────────────────────────────

    def record_progress(
        self,
        executive_session_id: str,
        problem: str,
        solution: str,
        commands_used: list[str],
        tool_name: str = "",
    ) -> int:
        """Record a mid-task progress event. Triggers factory in background."""
        self.record_session(executive_session_id)
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO progress_events
                   (executive_session_id, problem, solution, commands_used,
                    tool_name, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    executive_session_id, problem, solution,
                    json.dumps(commands_used), tool_name, now,
                ),
            )
        return cur.lastrowid

    def get_unprocessed_progress(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM progress_events WHERE processed=0 ORDER BY created_at"
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def mark_progress_processed(self, progress_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE progress_events SET processed=1 WHERE id=?", (progress_id,)
            )

    def list_progress(self, session_id: str | None = None, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            if session_id:
                rows = c.execute(
                    """SELECT * FROM progress_events WHERE executive_session_id=?
                       ORDER BY created_at DESC LIMIT ?""",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM progress_events ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_feedback(self, session_id: str | None = None, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            if session_id:
                rows = c.execute(
                    """SELECT * FROM feedback WHERE executive_session_id=?
                       ORDER BY created_at DESC LIMIT ?""",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM feedback ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── Factory Runs ──────────────────────────────────────────────────────────

    def create_factory_run(
        self,
        feedback_id: int | None = None,
        progress_id: int | None = None,
        mode: str = "cli",
        model: str = "claude-sonnet-4-6",
    ) -> int:
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO factory_runs(feedback_id, progress_id, mode, model, created_at)
                   VALUES (?,?,?,?,?)""",
                (feedback_id, progress_id, mode, model, now),
            )
        return cur.lastrowid

    def update_factory_run(
        self,
        run_id: int,
        status: str,
        actions: list | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        error: str | None = None,
        duration_s: float | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """UPDATE factory_runs SET status=?, actions=?, prompt_tokens=?,
                   completion_tokens=?, error=?, duration_s=? WHERE id=?""",
                (
                    status,
                    json.dumps(actions) if actions is not None else None,
                    prompt_tokens, completion_tokens,
                    error, duration_s, run_id,
                ),
            )

    def get_factory_run(self, run_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM factory_runs WHERE id=?", (run_id,)
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list_factory_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM factory_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ── Installations ─────────────────────────────────────────────────────────

    def record_install(
        self,
        tool_id: int,
        generation: int,
        install_path: str,
        settings_backup_path: str | None = None,
    ) -> int:
        now = time.time()
        # Mark any existing active installs as superseded
        with self._conn() as c:
            c.execute(
                """UPDATE installations SET status='superseded'
                   WHERE tool_id=? AND status='active'""",
                (tool_id,),
            )
            cur = c.execute(
                """INSERT INTO installations
                   (tool_id, generation, install_path, settings_backup_path, installed_at)
                   VALUES (?,?,?,?,?)""",
                (tool_id, generation, install_path, settings_backup_path, now),
            )
        self.set_tool_installed(tool_id, install_path, True)
        return cur.lastrowid

    def record_uninstall(self, installation_id: int) -> None:
        now = time.time()
        with self._conn() as c:
            row = c.execute(
                "SELECT tool_id FROM installations WHERE id=?", (installation_id,)
            ).fetchone()
            c.execute(
                "UPDATE installations SET status='uninstalled', uninstalled_at=? WHERE id=?",
                (now, installation_id),
            )
            if row:
                c.execute(
                    "UPDATE tools SET is_installed=0, install_path=NULL WHERE id=?",
                    (row["tool_id"],),
                )

    def get_active_install(self, tool_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                """SELECT * FROM installations WHERE tool_id=? AND status='active'
                   ORDER BY installed_at DESC LIMIT 1""",
                (tool_id,),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list_installations(self, active_only: bool = True) -> list[dict]:
        with self._conn() as c:
            if active_only:
                rows = c.execute(
                    "SELECT * FROM installations WHERE status='active'"
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM installations ORDER BY installed_at DESC"
                ).fetchall()
        return [_row_to_dict(r) for r in rows]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    # Auto-parse JSON columns
    for key in ("use_cases", "patterns", "requirements", "tools_used",
                "tools_unhelpful", "actions", "commands_used"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except (json.JSONDecodeError, TypeError):
                pass
    return d
