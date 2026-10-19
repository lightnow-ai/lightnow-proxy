"""Private, bounded durable facts. Never stores business payloads or retries calls."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
from time import monotonic, time
from typing import Any
from uuid import uuid4

from filelock import FileLock, Timeout

from lightnow_proxy.registry import ManagedRuntime

MAX_PENDING_CALLS = 2000
MAX_REPORT_RECEIPTS = 2000


class EvidenceStoreError(Exception):
    def __init__(self):
        super().__init__("EVIDENCE_STORE_UNAVAILABLE")


@dataclass(repr=False)
class ManagedCall:
    id: str
    lock: FileLock
    started: float = field(default_factory=monotonic)
    observation_id: str | None = None
    original_tool_name: str | None = None
    handoff: bool = False

    def release(self) -> None:
        self.lock.release()


class ManagedEventOutbox:
    def __init__(self, path: Path):
        self.path = path
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.parent.is_symlink() or path.is_symlink():
                raise EvidenceStoreError()
            path.parent.chmod(0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
            with closing(self._connect()) as db, db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS calls (
                        id TEXT PRIMARY KEY, identity TEXT NOT NULL, base TEXT NOT NULL,
                        phase TEXT NOT NULL, terminal INTEGER NOT NULL DEFAULT 0,
                        report_until INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS report_receipts (
                        identity TEXT NOT NULL, profile TEXT NOT NULL, assignment_id TEXT NOT NULL,
                        proof TEXT NOT NULL, report_until INTEGER NOT NULL,
                        PRIMARY KEY(identity,profile,assignment_id)
                    );
                    CREATE TABLE IF NOT EXISTS evidence_gaps (
                        id INTEGER PRIMARY KEY CHECK(id=1), unattributed_attempts INTEGER NOT NULL
                    );
                    INSERT OR IGNORE INTO evidence_gaps VALUES(1,0);
                    CREATE TABLE IF NOT EXISTS events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                        call_id TEXT NOT NULL REFERENCES calls(id), payload TEXT NOT NULL,
                        rejection TEXT
                    );
                """)
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceStoreError() from exc

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=1, isolation_level="IMMEDIATE")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous = FULL")
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA secure_delete = ON")
        return db

    def _lock(self, call_id: str) -> FileLock:
        try:
            lock = FileLock(self.path.parent / f"{call_id}.lock", timeout=0, thread_local=False, mode=0o600)
            lock.acquire()
            return lock
        except Timeout:
            raise
        except OSError as exc:
            raise EvidenceStoreError() from exc

    def remember(self, managed: ManagedRuntime, identity: tuple[str, str, str]) -> None:
        if not managed.reporting_proof:
            raise EvidenceStoreError()
        try:
            with closing(self._connect()) as db, db:
                db.execute("DELETE FROM report_receipts WHERE report_until<=?", (int(time()),))
                db.execute("INSERT INTO report_receipts VALUES(?,?,?,?,?) ON CONFLICT(identity,profile,assignment_id) "
                           "DO UPDATE SET proof=excluded.proof,report_until=excluded.report_until",
                           (json.dumps(identity), managed.profile, str(managed.assignment_id), managed.reporting_proof, managed.issued_at + 7 * 86400))
                db.execute("DELETE FROM report_receipts WHERE rowid IN "
                           "(SELECT rowid FROM report_receipts ORDER BY report_until DESC,rowid DESC LIMIT -1 OFFSET ?)", (MAX_REPORT_RECEIPTS,))
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceStoreError() from exc

    def begin_cached(self, identity: tuple[str, str, str], profile: str, assignment_id: str, *, reserved: bool = False) -> ManagedCall | None:
        try:
            with closing(self._connect()) as db:
                row = db.execute("SELECT proof,report_until FROM report_receipts WHERE identity=? AND profile=? AND assignment_id=? AND report_until>?",
                                 (json.dumps(identity), profile, assignment_id, int(time()))).fetchone()
            return self._begin(row["proof"], row["report_until"], identity, reserved=reserved) if row is not None else None
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceStoreError() from exc

    def unattributed(self) -> None:
        try:
            with closing(self._connect()) as db, db:
                db.execute("UPDATE evidence_gaps SET unattributed_attempts=unattributed_attempts+1 WHERE id=1")
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceStoreError() from exc

    def begin(self, managed: ManagedRuntime, identity: tuple[str, str, str], *, reserved: bool = False) -> ManagedCall:
        if not managed.reporting_proof:
            raise EvidenceStoreError()
        return self._begin(managed.reporting_proof, managed.issued_at + 7 * 86400, identity, reserved=reserved)

    def _begin(self, proof: str, report_until: int, identity: tuple[str, str, str], *, reserved: bool) -> ManagedCall:
        call_id = str(uuid4())
        lock: FileLock | None = None
        try:
            with closing(self._connect()) as db, db:
                # Take the native DB writer lock before reserving bounded capacity.
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] >= MAX_PENDING_CALLS:
                    raise EvidenceStoreError()
                lock = self._lock(call_id)
                base = {"schema_version": 2, "call_id": call_id, "reporting_proof": proof}
                db.execute("INSERT INTO calls(id,identity,base,phase,report_until) VALUES(?,?,?,?,?)",
                           (call_id, json.dumps(identity), json.dumps(base), "attempted", report_until))
                self._append(db, call_id, base, "attempted")
                if reserved and db.execute("UPDATE evidence_gaps SET unattributed_attempts=unattributed_attempts-1 "
                                           "WHERE id=1 AND unattributed_attempts>0").rowcount != 1:
                    raise EvidenceStoreError()
            return ManagedCall(call_id, lock)
        except (OSError, sqlite3.Error) as exc:
            if lock is not None:
                lock.release()
            raise EvidenceStoreError() from exc
        except BaseException:
            if lock is not None:
                lock.release()
            raise

    @staticmethod
    def _append(db: sqlite3.Connection, call_id: str, base: dict[str, Any], phase: str,
                reason: str | None = None, duration: int | None = None) -> None:
        event = {**base, "id": str(uuid4()), "phase": phase, "occurred_at": datetime.now(UTC).isoformat()}
        if reason is not None:
            event["reason_code"] = reason
        if duration is not None:
            event["duration_ms"] = min(86400000, max(0, duration))
        db.execute("INSERT INTO events(id,call_id,payload) VALUES(?,?,?)", (event["id"], call_id, json.dumps(event)))
        db.execute("UPDATE calls SET phase=? WHERE id=?", (phase, call_id))

    def record(self, call: ManagedCall, phase: str, reason: str | None = None) -> None:
        terminal = phase in {"denied", "runtime_failed", "succeeded", "upstream_failed", "outcome_unknown"}
        try:
            with closing(self._connect()) as db, db:
                row = db.execute("SELECT base,terminal FROM calls WHERE id=?", (call.id,)).fetchone()
                if row is None or row["terminal"]:
                    raise EvidenceStoreError()
                base = json.loads(row["base"])
                if call.observation_id and call.original_tool_name:
                    base.update(observation_id=call.observation_id, original_tool_name=call.original_tool_name)
                db.execute("UPDATE calls SET base=? WHERE id=?", (json.dumps(base), call.id))
                if phase in {"succeeded", "upstream_failed"}:
                    self._append(db, call.id, base, "dispatched")
                self._append(db, call.id, base, phase, reason, int((monotonic() - call.started) * 1000) if terminal else None)
                if terminal:
                    db.execute("UPDATE calls SET terminal=1 WHERE id=?", (call.id,))
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceStoreError() from exc
        if phase == "dispatch_started":
            call.handoff = True

    def recover(self) -> None:
        """Only recover calls whose native OS lock has been released by its owner."""
        try:
            with closing(self._connect()) as db:
                ids = [row[0] for row in db.execute("SELECT id FROM calls WHERE terminal=0")]
            for call_id in ids:
                try:
                    lock = self._lock(call_id)
                except Timeout:
                    continue
                try:
                    with closing(self._connect()) as db, db:
                        row = db.execute("SELECT * FROM calls WHERE id=? AND terminal=0", (call_id,)).fetchone()
                        if row is not None:
                            handed_off = row["phase"] == "dispatch_started"
                            self._append(db, call_id, json.loads(row["base"]),
                                         "outcome_unknown" if handed_off else "runtime_failed",
                                         "UPSTREAM_OUTCOME_UNKNOWN" if handed_off else "RUNTIME_START_FAILED")
                            db.execute("UPDATE calls SET terminal=1 WHERE id=?", (call_id,))
                finally:
                    lock.release()
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceStoreError() from exc

    def pending(self, identity: tuple[str, str, str]) -> list[dict[str, Any]]:
        self.recover()
        try:
            with closing(self._connect()) as db, db:
                db.execute("UPDATE events SET rejection='REPORT_EXPIRED' WHERE rejection IS NULL AND call_id IN "
                           "(SELECT id FROM calls WHERE report_until<=?)", (int(time()),))
                rows = db.execute("SELECT events.id,payload FROM events JOIN calls ON calls.id=events.call_id "
                                  "WHERE identity=? AND rejection IS NULL ORDER BY sequence LIMIT 64", (json.dumps(identity),)).fetchall()
                return [json.loads(row["payload"]) for row in rows]
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceStoreError() from exc

    def acknowledge(self, event_id: str) -> None:
        try:
            with closing(self._connect()) as db, db:
                row = db.execute("SELECT call_id FROM events WHERE id=?", (event_id,)).fetchone()
                if row is None:
                    return
                call_id = row[0]
                db.execute("DELETE FROM events WHERE id=?", (event_id,))
                db.execute("DELETE FROM calls WHERE id=? AND terminal=1 AND NOT EXISTS "
                           "(SELECT 1 FROM events WHERE call_id=?)", (call_id, call_id))
                removed = db.execute("SELECT 1 FROM calls WHERE id=?", (call_id,)).fetchone() is None
            if removed:
                (self.path.parent / f"{call_id}.lock").unlink(missing_ok=True)
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceStoreError() from exc

    def reject(self, event_id: str, code: str) -> None:
        try:
            with closing(self._connect()) as db, db:
                db.execute("UPDATE events SET rejection=? WHERE id=?", (code, event_id))
        except (OSError, sqlite3.Error) as exc:
            raise EvidenceStoreError() from exc

    def status(self) -> dict[str, Any]:
        try:
            with closing(self._connect()) as db:
                return {"unattributed_attempts": db.execute("SELECT unattributed_attempts FROM evidence_gaps WHERE id=1").fetchone()[0],
                        "pending": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                        "unresolved": db.execute("SELECT COUNT(*) FROM events WHERE rejection IS NOT NULL").fetchone()[0],
                        "rejections": {row[0]: row[1] for row in db.execute("SELECT rejection,COUNT(*) FROM events WHERE rejection IS NOT NULL GROUP BY rejection")}}
        except (OSError, sqlite3.Error):
            return {"last_error": "EVIDENCE_STORE_UNAVAILABLE"}
