"""Opt-in private executor: durable request identity, never a second write attempt."""
from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import stat
from time import time
from typing import Any, TYPE_CHECKING
from uuid import UUID, uuid4

from filelock import FileLock, Timeout

from lightnow_proxy.config import ProxyConfig, expand_env
from lightnow_proxy.runtime_secrets import RuntimeSecretResolver
from lightnow_proxy.secret_operations import OAuthOperation, OAuthOperationExecutor, SecretOperation, SecretOperationExecutor

if TYPE_CHECKING:
    from lightnow_proxy.registry import RegistryApiClient

logger = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "canceled", "expired"}


class SecretWorkerError(Exception):
    def __init__(self):
        super().__init__("SECRET_WORKER_STATE_UNAVAILABLE")


class SecretOperationJournal:
    """Only nonsecret IDs/phase/expiry; FULL commit precedes each external side effect."""
    MAX_ROWS = 1024
    RETENTION_SECONDS = 600

    def __init__(self, path: Path):
        self.path = path
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.parent.is_symlink() or path.is_symlink():
                raise SecretWorkerError()
            parent = path.parent.stat(follow_symlinks=False)
            if (not hasattr(os, "getuid") or parent.st_uid != os.getuid()
                    or not stat.S_ISDIR(parent.st_mode) or stat.S_IMODE(parent.st_mode) & 0o077
                    or stat.S_IMODE(parent.st_mode) & 0o700 != 0o700):
                raise ValueError("Secret worker journal requires an owned private directory with mode 0700")
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
            with closing(self.connect()) as db, db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS requests (
                        namespace TEXT NOT NULL, slot TEXT NOT NULL, request_id TEXT NOT NULL,
                        operation_id TEXT, claim_id TEXT, claim_revision INTEGER,
                        claim_expires_at REAL, expires_at REAL NOT NULL, created_at REAL NOT NULL,
                        dispatched INTEGER NOT NULL DEFAULT 0, terminal_at REAL,
                        PRIMARY KEY(namespace, request_id)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS active_slot ON requests(namespace,slot)
                        WHERE terminal_at IS NULL;
                """)
        except (OSError, sqlite3.Error) as exc:
            raise SecretWorkerError() from exc

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=1)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA secure_delete=ON")
        return db

    def lock(self, namespace: str) -> FileLock:
        return FileLock(self.path.parent / f"{namespace}.lock", timeout=0, thread_local=False, mode=0o600)

    def ensure(self, namespace: str, slot: str, expiry: float) -> dict[str, Any] | None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM requests WHERE terminal_at IS NOT NULL AND terminal_at<=?", (time()-600,))
            row = db.execute("SELECT * FROM requests WHERE namespace=? AND slot=? AND terminal_at IS NULL", (namespace, slot)).fetchone()
            if row is not None:
                return dict(row)
            if slot != "pool" and db.execute("SELECT 1 FROM requests WHERE namespace=? AND slot=?", (namespace, slot)).fetchone():
                return None
            if db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= self.MAX_ROWS:
                raise SecretWorkerError()
            request = {"namespace": namespace, "slot": slot, "request_id": str(uuid4()),
                       "operation_id": None if slot == "pool" else slot, "expires_at": expiry, "created_at": time(),
                       "dispatched": 0}
            db.execute("INSERT INTO requests(namespace,slot,request_id,operation_id,expires_at,created_at) VALUES(?,?,?,?,?,?)",
                       tuple(request[key] for key in ("namespace", "slot", "request_id", "operation_id", "expires_at", "created_at")))
            return request

    def pending(self, namespace: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("SELECT * FROM requests WHERE namespace=? AND terminal_at IS NULL ORDER BY created_at,request_id", (namespace,))]

    def stage(self, row: dict[str, Any], operation: SecretOperation | OAuthOperation) -> None:
        if row["operation_id"] is not None and row["operation_id"] != str(operation.operation_id):
            raise SecretWorkerError()
        if operation.claim.action in ("kv2.write", "oauth.write", "oauth.refresh") and row["dispatched"]:
            raise SecretWorkerError()
        with closing(self.connect()) as db, db:
            changed = db.execute("UPDATE requests SET operation_id=?,claim_id=?,claim_revision=?,claim_expires_at=?,dispatched=1 WHERE namespace=? AND request_id=? AND terminal_at IS NULL",
                       (str(operation.operation_id), str(operation.claim.id), operation.claim.revision,
                        operation.claim.expires_at.timestamp(), row["namespace"], row["request_id"]))
            if changed.rowcount != 1:
                raise SecretWorkerError()

    def finish(self, row: dict[str, Any]) -> None:
        with closing(self.connect()) as db, db:
            db.execute("UPDATE requests SET terminal_at=? WHERE namespace=? AND request_id=?", (time(), row["namespace"], row["request_id"]))


class SecretStoreWorker:
    def __init__(self, config: ProxyConfig, registry: RegistryApiClient):
        self.config, self.registry = config, registry
        settings = config.runtime_secrets
        self.connector_id = str(settings.connector_id) if settings.connector_id else None
        self.journal = SecretOperationJournal(Path(expand_env(settings.journal_path)).expanduser())
        self.executor = SecretOperationExecutor(RuntimeSecretResolver(settings))
        self.oauth_executor = OAuthOperationExecutor(self.executor.resolver)
        self.active = False
        self.last_error: str | None = None
        self.registry.secret_worker = self

    async def prepare(self) -> bool:
        try:
            await self.registry.secret_worker_authorization(self.connector_id, self.registry.runtime_client_id)
        except Exception:
            self.active = False
            self.last_error = "SECRET_WORKER_IDENTITY_UNAVAILABLE"
            return False
        self.active = True
        return True

    async def _step(self, identity: tuple[str, ...], row: dict[str, Any]) -> None:
        runtime_id = None if self.connector_id else self.registry.runtime_client_id
        common = {"connector_id": self.connector_id, "runtime_client_id": runtime_id}
        if self.connector_id:
            root = f"/integrations/secret-store-connectors/{self.connector_id}/operations"
            claim_path = root + "/claim"
            body = {"request_id": row["request_id"], **({"operation_id": row["operation_id"]} if row["operation_id"] else {})}
        else:
            root = f"/integrations/secret-operations/{row['operation_id']}"
            claim_path = root + "/claim"
            body = {"request_id": row["request_id"], "runtime_client_id": runtime_id}
        response = await self.registry.secret_operation_request(claim_path, body, identity, **common)
        if response["data"] is None:
            self.journal.finish(row)
            return
        data = response["data"]
        operation = (OAuthOperation.model_validate(data) if data.get("schema") == "secret-store.oauth.v1"
                     else SecretOperation.model_validate(data))
        self.journal.stage(row, operation)
        # A context switch after claim cannot dispatch material under a new session.
        current, _ = await self.registry.secret_worker_authorization(self.connector_id, runtime_id)
        if current != identity:
            raise SecretWorkerError()
        result = await (self.oauth_executor if isinstance(operation, OAuthOperation) else self.executor).execute(operation)
        result_path = (root + f"/{operation.operation_id}/result") if self.connector_id else root + "/result"
        if not self.connector_id:
            result["runtime_client_id"] = runtime_id
        receipt = await self.registry.secret_operation_request(result_path, result, identity, **common)
        data = receipt.get("data")
        if isinstance(data, dict) and data.get("id") == str(operation.operation_id) and data.get("status") in TERMINAL:
            self.journal.finish(row)
        # Unknown IO or an unconfirmed/malformed receipt retains the same nonce.

    async def personal_pending(self, metadata: Any) -> None:
        if self.connector_id or not self.registry.runtime_client_id:
            return
        await self._run(metadata)

    async def _run(self, metadata: Any = None) -> None:
        if not await self.prepare():
            return
        try:
            identity, _ = await self.registry.secret_worker_authorization(self.connector_id, self.registry.runtime_client_id)
            namespace = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            with self.journal.lock(namespace):
                if self.connector_id:
                    self.journal.ensure(namespace, "pool", time()+600)
                elif isinstance(metadata, list):
                    for pending in metadata[:20]:
                        operation_id = str(UUID(pending["operation_id"]))
                        expiry = datetime.fromisoformat(pending["expires_at"].replace("Z", "+00:00")).timestamp()
                        if expiry > time():
                            self.journal.ensure(namespace, operation_id, expiry)
                for row in self.journal.pending(namespace):
                    try:
                        await self._step(identity, row)
                    except Exception:
                        self.last_error = "SECRET_WORKER_STEP_UNCONFIRMED"
                        logger.warning("Secret Store worker step unconfirmed; request identity retained")
                        continue
                if not self.journal.pending(namespace):
                    self.last_error = None
        except Timeout:
            return  # Another actual worker holds this namespace through IO/result.
        except Exception:
            self.last_error = "SECRET_WORKER_STATE_UNAVAILABLE"
            logger.warning("Secret Store worker unavailable; no new write attempted")

    async def run_connector(self) -> None:
        if not self.connector_id:
            raise SecretWorkerError()
        while True:
            await self._run()
            await asyncio.sleep(self.config.runtime_secrets.poll_interval_seconds)
