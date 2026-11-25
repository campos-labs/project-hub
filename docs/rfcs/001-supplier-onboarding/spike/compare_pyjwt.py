#!/usr/bin/env python3
"""PyJWT 2.10.1 standalone invitation spike runner."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

ROOT = Path(__file__).resolve().parent
RESEARCH_DATA_DIR = ROOT.parent / "research" / "data"
DATABASE_PATH = RESEARCH_DATA_DIR / "invitations.db"
TTL_SECONDS = 72 * 60 * 60


class InviteClaims(BaseModel):
    """The same minimal, non-personal invitation payload for both signers."""

    model_config = ConfigDict(extra="forbid", strict=True)

    jti: str
    request_id: str
    purpose: Literal["supplier_invitation"]
    iat: int
    exp: int

    @model_validator(mode="after")
    def validate_lifetime(self) -> "InviteClaims":
        if self.exp != self.iat + TTL_SECONDS:
            raise ValueError("exp must be exactly 72 hours after iat")
        return self


class Clock:
    def __init__(self, timestamp: int):
        self._timestamp = timestamp
        self._lock = threading.Lock()

    def now(self) -> int:
        with self._lock:
            return self._timestamp

    def set(self, timestamp: int) -> None:
        with self._lock:
            self._timestamp = timestamp


class RejectedToken(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(database_path: Path = DATABASE_PATH) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS invitations (
            strategy TEXT NOT NULL,
            jti TEXT NOT NULL,
            request_id TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            issued_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            consumed_at INTEGER,
            revoked_at INTEGER,
            PRIMARY KEY (strategy, jti),
            CHECK (consumed_at IS NULL OR revoked_at IS NULL)
        );
        CREATE TABLE IF NOT EXISTS invitation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL,
            jti TEXT NOT NULL,
            event TEXT NOT NULL CHECK (event IN ('issued', 'consumed', 'revoked')),
            happened_at INTEGER NOT NULL,
            FOREIGN KEY (strategy, jti)
                REFERENCES invitations(strategy, jti)
        );
        """
    )
    return connection


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class InvitationStore:
    def __init__(self, strategy: str, signer, clock: Clock):
        self.strategy = strategy
        self.signer = signer
        self.clock = clock
        self.connection = connect()

    def close(self) -> None:
        self.connection.close()

    def reset_strategy(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "DELETE FROM invitation_events WHERE strategy = ?", (self.strategy,)
            )
            self.connection.execute(
                "DELETE FROM invitations WHERE strategy = ?", (self.strategy,)
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def issue(self, request_id: str | None = None) -> str:
        now = self.clock.now()
        claims = InviteClaims(
            jti=secrets.token_urlsafe(18),
            request_id=request_id or str(uuid.uuid4()),
            purpose="supplier_invitation",
            iat=now,
            exp=now + TTL_SECONDS,
        )
        token = self.signer.issue(claims)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """INSERT INTO invitations
                   (strategy, jti, request_id, token_hash, issued_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    self.strategy,
                    claims.jti,
                    claims.request_id,
                    token_digest(token),
                    claims.iat,
                    claims.exp,
                ),
            )
            self.connection.execute(
                """INSERT INTO invitation_events
                   (strategy, jti, event, happened_at) VALUES (?, ?, 'issued', ?)""",
                (self.strategy, claims.jti, now),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return token

    def verify_token(self, token: str) -> InviteClaims:
        try:
            claims = self.signer.verify(token)
        except RejectedToken:
            raise
        except ValidationError as error:
            raise RejectedToken("invalid_contract") from error

        if claims.iat > self.clock.now():
            raise RejectedToken("not_yet_valid")
        if claims.exp <= self.clock.now():
            raise RejectedToken("expired")
        return claims

    def validate(self, token: str) -> tuple[bool, str, str | None]:
        try:
            claims = self.verify_token(token)
        except RejectedToken as error:
            return False, error.reason, None

        row = self.connection.execute(
            """SELECT request_id, token_hash, expires_at, consumed_at, revoked_at
               FROM invitations WHERE strategy = ? AND jti = ?""",
            (self.strategy, claims.jti),
        ).fetchone()
        if row is None:
            return False, "unknown_invitation", claims.jti
        if row["request_id"] != claims.request_id or row["expires_at"] != claims.exp:
            return False, "record_mismatch", claims.jti
        if not hmac.compare_digest(row["token_hash"], token_digest(token)):
            return False, "token_hash_mismatch", claims.jti
        if row["revoked_at"] is not None:
            return False, "revoked", claims.jti
        if row["consumed_at"] is not None:
            return False, "consumed", claims.jti
        if row["expires_at"] <= self.clock.now():
            return False, "expired", claims.jti
        return True, "active", claims.jti

    def consume(
        self,
        token: str,
        before_lock: Callable[[], None] | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> tuple[bool, str, str | None]:
        try:
            claims = self.signer.verify(token)
        except RejectedToken as error:
            return False, error.reason, None
        except ValidationError:
            return False, "invalid_contract", None

        if before_lock is not None:
            before_lock()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            now = self.clock.now()
            if claims.iat > now:
                self.connection.rollback()
                return False, "not_yet_valid", claims.jti
            if claims.exp <= now:
                self.connection.rollback()
                return False, "expired", claims.jti

            updated = self.connection.execute(
                """UPDATE invitations SET consumed_at = ?
                   WHERE strategy = ? AND jti = ? AND request_id = ?
                     AND expires_at > ? AND consumed_at IS NULL
                     AND revoked_at IS NULL AND token_hash = ?""",
                (
                    now,
                    self.strategy,
                    claims.jti,
                    claims.request_id,
                    now,
                    token_digest(token),
                ),
            ).rowcount
            if updated != 1:
                reason = self._terminal_reason(claims.jti)
                self.connection.rollback()
                return False, reason, claims.jti

            self.connection.execute(
                """INSERT INTO invitation_events
                   (strategy, jti, event, happened_at)
                   VALUES (?, ?, 'consumed', ?)""",
                (self.strategy, claims.jti, now),
            )
            if before_commit is not None:
                before_commit()
            self.connection.commit()
            return True, "consumed", claims.jti
        except Exception:
            self.connection.rollback()
            raise

    def revoke(self, token: str) -> tuple[bool, str, str | None]:
        try:
            claims = self.signer.verify(token)
        except RejectedToken as error:
            return False, error.reason, None
        except ValidationError:
            return False, "invalid_contract", None

        self.connection.execute("BEGIN IMMEDIATE")
        try:
            now = self.clock.now()
            if claims.iat > now:
                self.connection.rollback()
                return False, "not_yet_valid", claims.jti
            if claims.exp <= now:
                self.connection.rollback()
                return False, "expired", claims.jti
            updated = self.connection.execute(
                """UPDATE invitations SET revoked_at = ?
                   WHERE strategy = ? AND jti = ? AND request_id = ?
                     AND expires_at > ? AND consumed_at IS NULL
                     AND revoked_at IS NULL AND token_hash = ?""",
                (
                    now,
                    self.strategy,
                    claims.jti,
                    claims.request_id,
                    now,
                    token_digest(token),
                ),
            ).rowcount
            if updated != 1:
                reason = self._terminal_reason(claims.jti)
                self.connection.rollback()
                return False, reason, claims.jti
            self.connection.execute(
                """INSERT INTO invitation_events
                   (strategy, jti, event, happened_at)
                   VALUES (?, ?, 'revoked', ?)""",
                (self.strategy, claims.jti, now),
            )
            self.connection.commit()
            return True, "revoked", claims.jti
        except Exception:
            self.connection.rollback()
            raise

    def _terminal_reason(self, jti: str) -> str:
        row = self.connection.execute(
            """SELECT expires_at, consumed_at, revoked_at FROM invitations
               WHERE strategy = ? AND jti = ?""",
            (self.strategy, jti),
        ).fetchone()
        if row is None:
            return "unknown_invitation"
        if row["revoked_at"] is not None:
            return "revoked"
        if row["consumed_at"] is not None:
            return "consumed"
        if row["expires_at"] <= self.clock.now():
            return "expired"
        return "record_mismatch"

    def row_for(self, jti: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT * FROM invitations WHERE strategy = ? AND jti = ?""",
            (self.strategy, jti),
        ).fetchone()

    def events_for(self, jti: str, event: str | None = None) -> list[sqlite3.Row]:
        if event is None:
            return list(
                self.connection.execute(
                    """SELECT * FROM invitation_events
                       WHERE strategy = ? AND jti = ? ORDER BY id""",
                    (self.strategy, jti),
                )
            )
        return list(
            self.connection.execute(
                """SELECT * FROM invitation_events
                   WHERE strategy = ? AND jti = ? AND event = ? ORDER BY id""",
                (self.strategy, jti, event),
            )
        )

    def other_strategy_fingerprint(self) -> str:
        invitations = [
            tuple(row)
            for row in self.connection.execute(
                """SELECT strategy, jti, request_id, token_hash, issued_at,
                          expires_at, consumed_at, revoked_at
                   FROM invitations WHERE strategy <> ? ORDER BY strategy, jti""",
                (self.strategy,),
            )
        ]
        events = [
            tuple(row)
            for row in self.connection.execute(
                """SELECT strategy, jti, event, happened_at
                   FROM invitation_events WHERE strategy <> ?
                   ORDER BY strategy, jti, event, happened_at, id""",
                (self.strategy,),
            )
        ]
        serialized = json.dumps([invitations, events], separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def states_for_ids(self, jtis: dict[str, str], now: int) -> dict[str, str | None]:
        states: dict[str, str | None] = {}
        for label, jti in jtis.items():
            row = self.row_for(jti)
            if row is None:
                states[label] = None
            elif row["revoked_at"] is not None:
                states[label] = "revoked"
            elif row["consumed_at"] is not None:
                states[label] = "consumed"
            elif row["expires_at"] <= now:
                states[label] = "expired"
            else:
                states[label] = "active"
        return states


def record(results: list[dict], scenario: str, expected: str, observed: str,
           evidence: dict, passed: bool) -> None:
    results.append(
        {
            "scenario": scenario,
            "expected": expected,
            "observed": observed,
            "passed": passed,
            "evidence": evidence,
        }
    )


def new_store(strategy: str, signer, clock: Clock) -> InvitationStore:
    return InvitationStore(strategy, signer, clock)


def make_invitation(store: InvitationStore) -> tuple[str, str]:
    token = store.issue()
    valid, _, jti = store.validate(token)
    if not valid or jti is None:
        raise RuntimeError("freshly issued invitation did not validate")
    return token, jti


def tamper_token(token: str) -> str:
    parts = token.split(".")
    last = parts[-1]
    replacement = "A" if last[0] != "A" else "B"
    parts[-1] = replacement + last[1:]
    return ".".join(parts)


@contextmanager
def reopened_connection(store: InvitationStore):
    store.connection.close()
    store.connection = connect()
    yield


def child_check(strategy: str, signer, payload: dict) -> int:
    now = int(payload["now"])
    clock = Clock(now)
    store = new_store(strategy, signer, clock)
    try:
        states = store.states_for_ids(payload["jtis"], now)
        valid, reason, _ = store.validate(payload["active_token"])
        print(json.dumps({"states": states, "active_validation": valid, "reason": reason}))
        return 0
    finally:
        store.close()


def process_restart_check(script: Path, payload: dict) -> dict:
    process_payload = {
        "now": payload["now"],
        "active_token": payload["active_token"],
        "jtis": payload["jtis"],
    }
    completed = subprocess.run(
        [sys.executable, str(script), "--child-check"],
        input=json.dumps(process_payload),
        text=True,
        capture_output=True,
        check=False,
        env=os.environ.copy(),
        timeout=20,
    )
    if completed.returncode != 0:
        return {"returncode": completed.returncode, "stderr": completed.stderr[-1000:]}
    return json.loads(completed.stdout)


def run_experiment(strategy: str, signer, script: Path, environment: dict) -> dict:
    secret = os.environ.get("SPIKE_INVITATION_SECRET")
    if not secret:
        raise SystemExit("Set SPIKE_INVITATION_SECRET to a local test secret before running.")

    base = 1_900_000_000
    expiry = base + TTL_SECONDS
    clock = Clock(base)
    store = new_store(strategy, signer(secret, clock), clock)
    store.reset_strategy()
    other_strategy_fingerprint = store.other_strategy_fingerprint()
    results: list[dict] = []
    token_sizes: list[int] = []

    try:
        token, jti = make_invitation(store)
        token_sizes.append(len(token.encode("utf-8")))
        validations = [store.validate(token), store.validate(token)]
        row = store.row_for(jti)
        record(
            results, "issue_and_validate_twice",
            "validar duas vezes, válido por 72h, sem consumo",
            f"validations={[x[1] for x in validations]}, consumed_at={row['consumed_at']}",
            {"jti": jti, "expires_at": row["expires_at"], "token_hash_persisted": bool(row["token_hash"])},
            all(x[0] for x in validations) and row["expires_at"] - row["issued_at"] == TTL_SECONDS and row["consumed_at"] is None,
        )

        altered = tamper_token(token)
        altered_result = store.validate(altered)
        wrong_signer = signer(secret + "wrong-key", clock)
        try:
            wrong_signer.verify(token)
            wrong_key_result = (True, "accepted", jti)
        except RejectedToken as error:
            wrong_key_result = (False, error.reason, jti)

        malformed_claims = {"jti": str(uuid.uuid4()), "request_id": "fake", "purpose": "supplier_invitation", "iat": base, "exp": base + TTL_SECONDS, "unexpected": "field"}
        malformed = signer(secret, clock).issue_raw(malformed_claims)
        malformed_result = store.validate(malformed)
        unknown_claims = InviteClaims(jti=str(uuid.uuid4()), request_id="unregistered", purpose="supplier_invitation", iat=base, exp=expiry)
        unknown = signer(secret, clock).issue(unknown_claims)
        unknown_result = store.validate(unknown)
        unchanged = store.row_for(jti)
        original_unchanged = unchanged["consumed_at"] is None and unchanged["revoked_at"] is None
        record(
            results, "tampered_token",
            "token adulterado recusado sem alterar o convite legítimo",
            altered_result[1],
            {"original_jti": jti, "original_state_unchanged": original_unchanged},
            not altered_result[0] and original_unchanged,
        )
        record(
            results, "wrong_signing_key",
            "assinatura feita com chave incorreta recusada",
            wrong_key_result[1],
            {"original_jti": jti, "original_state_unchanged": original_unchanged},
            not wrong_key_result[0] and original_unchanged,
        )
        record(
            results, "invalid_claim_contract",
            "token assinado com campo incompatível com o contrato recusado",
            malformed_result[1],
            {"original_jti": jti, "original_state_unchanged": original_unchanged},
            not malformed_result[0] and original_unchanged,
        )
        record(
            results, "token_without_persisted_record",
            "token assinado sem registro correspondente recusado",
            unknown_result[1],
            {"unregistered_jti": unknown_claims.jti, "original_state_unchanged": original_unchanged},
            not unknown_result[0] and unknown_result[1] == "unknown_invitation" and original_unchanged,
        )

        boundary_outcomes = []
        for label, moment in (("one_second_before", expiry - 1), ("exact_expiry", expiry), ("one_second_after", expiry + 1)):
            clock.set(base)
            boundary_token, boundary_jti = make_invitation(store)
            clock.set(moment)
            try:
                store.signer.verify(boundary_token)
                native_verification = "accepted"
            except RejectedToken as error:
                native_verification = error.reason
            validation = store.validate(boundary_token)
            consumption = store.consume(boundary_token)
            boundary_row = store.row_for(boundary_jti)
            expected_ok = label == "one_second_before"
            ok = validation[0] == expected_ok and consumption[0] == expected_ok
            if not expected_ok:
                ok = ok and boundary_row["consumed_at"] is None
            boundary_outcomes.append({"case": label, "native_verification": native_verification, "validation": validation[1], "consume": consumption[1], "passed": ok})
        record(
            results, "expiration_boundary",
            "ativo em exp-1; recusado em exp e exp+1, sem consumo nos vencidos",
            str(boundary_outcomes), {"expiry": expiry, "ttl_seconds": TTL_SECONDS},
            all(case["passed"] for case in boundary_outcomes),
        )

        clock.set(base)
        revoked_token, revoked_jti = make_invitation(store)
        revoke_result = store.revoke(revoked_token)
        validate_revoked = store.validate(revoked_token)
        consume_revoked = store.consume(revoked_token)
        revoke_events = store.events_for(revoked_jti, "revoked")
        record(
            results, "revocation",
            "revogação única; validar e resgatar recusados",
            f"revoke={revoke_result[1]}, validate={validate_revoked[1]}, consume={consume_revoked[1]}",
            {"jti": revoked_jti, "revocation_events": len(revoke_events)},
            revoke_result[0] and validate_revoked[1] == "revoked" and not consume_revoked[0] and len(revoke_events) == 1,
        )

        clock.set(base)
        one_time_token, one_time_jti = make_invitation(store)
        first_consume = store.consume(one_time_token)
        second_consume = store.consume(one_time_token)
        consume_events = store.events_for(one_time_jti, "consumed")
        record(
            results, "single_use_sequential",
            "primeiro resgate aceito, segundo recusado, uma transição persistida",
            f"first=({first_consume[0]}, {first_consume[1]}), second=({second_consume[0]}, {second_consume[1]})",
            {"jti": one_time_jti, "consume_events": len(consume_events)},
            first_consume[0] and not second_consume[0] and len(consume_events) == 1,
        )

        concurrent_outcomes = []
        for attempt in range(20):
            clock.set(base)
            concurrent_token, concurrent_jti = make_invitation(store)
            barrier = threading.Barrier(2)
            outcome_lock = threading.Lock()
            outcomes: list[tuple[bool, str, str | None]] = []

            def concurrent_consume() -> None:
                local_store = new_store(strategy, signer(secret, clock), clock)
                try:
                    outcome = local_store.consume(concurrent_token, before_lock=barrier.wait)
                    with outcome_lock:
                        outcomes.append(outcome)
                finally:
                    local_store.close()

            threads = [threading.Thread(target=concurrent_consume) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            completed_threads = all(not thread.is_alive() for thread in threads)
            event_count = len(store.events_for(concurrent_jti, "consumed"))
            concurrent_outcomes.append(
                completed_threads and len(outcomes) == 2
                and sum(1 for item in outcomes if item[0]) == 1
                and event_count == 1
            )
        record(
            results, "single_use_concurrent_20_pairs",
            "em cada par simultâneo, exatamente um sucesso e um evento de consumo",
            f"passed_pairs={sum(concurrent_outcomes)}/20",
            {"pairs": 20, "successes_per_pair": 1, "events_per_invitation": 1},
            all(concurrent_outcomes),
        )

        clock.set(base)
        delayed_token, delayed_jti = make_invitation(store)
        clock.set(expiry - 1)
        blocker = connect()
        blocker.execute("BEGIN IMMEDIATE")
        before_begin = threading.Event()
        delayed_outcome: list[tuple[bool, str, str | None]] = []
        delayed_error: list[str] = []

        def delayed_consume() -> None:
            local_store = new_store(strategy, signer(secret, clock), clock)
            try:
                delayed_outcome.append(
                    local_store.consume(delayed_token, before_lock=before_begin.set)
                )
            except Exception as error:
                delayed_error.append(type(error).__name__)
            finally:
                local_store.close()

        delayed_thread = threading.Thread(target=delayed_consume)
        delayed_thread.start()
        started_before_expiry = before_begin.wait(timeout=5)
        clock.set(expiry)
        blocker.commit()
        blocker.close()
        delayed_thread.join(timeout=10)
        delayed_row = store.row_for(delayed_jti)
        record(
            results, "lock_acquired_after_expiry",
            "iniciou antes do prazo, adquiriu bloqueio após expirar, recusado sem consumed_at",
            f"outcome={delayed_outcome[0][1] if delayed_outcome else delayed_error}",
            {"started_before_expiry": started_before_expiry, "expires_at": delayed_row["expires_at"], "consumed_at": delayed_row["consumed_at"]},
            started_before_expiry and bool(delayed_outcome) and delayed_outcome[0][1] == "expired" and delayed_row["consumed_at"] is None,
        )

        clock.set(base)
        rollback_token, rollback_jti = make_invitation(store)
        injected_failure = "commit_interrupted"

        def interrupt_before_commit() -> None:
            raise RuntimeError(injected_failure)

        try:
            store.consume(rollback_token, before_commit=interrupt_before_commit)
            rollback_returned_success = True
        except RuntimeError as error:
            rollback_returned_success = error.args != (injected_failure,)
        with reopened_connection(store):
            rollback_row = store.row_for(rollback_jti)
            rollback_events = store.events_for(rollback_jti, "consumed")
        record(
            results, "rollback_before_commit",
            "rollback preserva convite ativo; nenhum sucesso nem evento de consumo",
            f"consumed_at={rollback_row['consumed_at']}, consumed_events={len(rollback_events)}",
            {"jti": rollback_jti, "injected_failure": injected_failure},
            not rollback_returned_success and rollback_row["consumed_at"] is None and len(rollback_events) == 0,
        )

        clock.set(base)
        expired_token, expired_jti = make_invitation(store)
        clock.set(expiry + 1)
        persisted_active, active_jti = make_invitation(store)
        revoked_old_token, revoked_old_jti = make_invitation(store)
        revoked_state = store.revoke(revoked_old_token)
        consumed_token, consumed_jti = make_invitation(store)
        consumed_state = store.consume(consumed_token)
        expected_states = {"active": "active", "expired": "expired", "revoked": "revoked", "consumed": "consumed"}
        state_ids = {"active": active_jti, "expired": expired_jti, "revoked": revoked_old_jti, "consumed": consumed_jti}
        reopen_snapshot = store.states_for_ids(state_ids, clock.now())
        with reopened_connection(store):
            reopened_snapshot = store.states_for_ids(state_ids, clock.now())
        child_payload = {"now": clock.now(), "active_token": persisted_active, "jtis": state_ids}
        process_result = process_restart_check(script, child_payload)
        states_ok = (
            reopen_snapshot == expected_states
            and reopened_snapshot == expected_states
            and process_result.get("states") == expected_states
            and process_result.get("active_validation") is True
        )
        record(
            results, "connection_and_process_restart",
            "active, expired, revoked, consumed persistem após reabrir e reiniciar processo",
            f"reopened={reopened_snapshot}, child={process_result}",
            {"jti_ids": state_ids, "same_database": str(DATABASE_PATH), "same_secret_from_environment": True},
            states_ok and revoked_state[0] and consumed_state[0],
        )

        token_sizes.extend(
            len(token.encode("utf-8")) for token in (persisted_active, expired_token, revoked_old_token, consumed_token)
        )
        strategy_rows = store.connection.execute(
            "SELECT COUNT(*) FROM invitations WHERE strategy = ?", (strategy,)
        ).fetchone()[0]
        other_rows = store.connection.execute(
            "SELECT COUNT(*) FROM invitations WHERE strategy <> ?", (strategy,)
        ).fetchone()[0]
        other_strategy_unchanged = store.other_strategy_fingerprint() == other_strategy_fingerprint
        record(
            results, "strategy_isolation",
            "operações não alteram registros da outra estratégia",
            f"own_rows={strategy_rows}, other_strategy_rows={other_rows}, other_unchanged={other_strategy_unchanged}",
            {"strategy": strategy, "records_from_other_strategy": other_rows},
            strategy_rows > 0 and other_strategy_unchanged,
        )

        passed = sum(item["passed"] for item in results)
        run = {
            "run_at_utc": utc_now(),
            "strategy": strategy,
            "environment": environment,
            "ttl_seconds": TTL_SECONDS,
            "database": "research/data/invitations.db",
            "secret_source": "SPIKE_INVITATION_SECRET environment variable",
            "token_sizes_bytes": {
                "observations": len(token_sizes),
                "mean": round(sum(token_sizes) / len(token_sizes), 2),
                "median": sorted(token_sizes)[len(token_sizes) // 2],
                "minimum": min(token_sizes),
                "maximum": max(token_sizes),
            },
            "summary": {"passed": passed, "total": len(results), "all_passed": passed == len(results)},
            "scenarios": results,
        }
        print(json.dumps(run, indent=2, sort_keys=True))
        return run
    finally:
        store.close()



import jwt
from importlib.metadata import version

STRATEGY = "pyjwt"
ALGORITHM = "HS256"
_PYJWT_CLOCK_LOCK = threading.RLock()


class PyJWTSigner:
    def __init__(self, secret: str, clock: Clock):
        self.secret = secret
        self.clock = clock

    def issue(self, claims: InviteClaims) -> str:
        return jwt.encode(claims.model_dump(), self.secret, algorithm=ALGORITHM)

    def issue_raw(self, payload: dict) -> str:
        return jwt.encode(payload, self.secret, algorithm=ALGORITHM)

    def verify(self, token: str) -> InviteClaims:
        original_datetime = jwt.api_jwt.datetime
        clock = self.clock

        class ControlledDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                current = datetime.fromtimestamp(clock.now(), timezone.utc)
                return current if tz is None else current.astimezone(tz)

        try:
            with _PYJWT_CLOCK_LOCK:
                jwt.api_jwt.datetime = ControlledDateTime
                payload = jwt.decode(
                    token,
                    self.secret,
                    algorithms=[ALGORITHM],
                    options={
                        "require": ["jti", "request_id", "purpose", "iat", "exp"],
                    },
                )
        except jwt.ExpiredSignatureError as error:
            raise RejectedToken("expired") from error
        except jwt.InvalidTokenError as error:
            raise RejectedToken("invalid_signature_or_claim") from error
        finally:
            with _PYJWT_CLOCK_LOCK:
                jwt.api_jwt.datetime = original_datetime

        try:
            return InviteClaims.model_validate(payload)
        except ValidationError:
            raise


def main() -> int:
    secret = os.environ.get("SPIKE_INVITATION_SECRET")
    if not secret:
        raise SystemExit("Set SPIKE_INVITATION_SECRET to a local test secret before running.")
    clock = Clock(0)
    signer = PyJWTSigner(secret, clock)
    if len(sys.argv) == 2 and sys.argv[1] == "--child-check":
        payload = json.load(sys.stdin)
        clock.set(int(payload["now"]))
        return child_check(STRATEGY, signer, payload)
    environment = {
        "python": sys.version.split()[0],
        "pydantic": version("pydantic"),
        "pyjwt": version("pyjwt"),
        "sqlite": __import__("sqlite3").sqlite_version,
        "platform": sys.platform,
    }
    run = run_experiment(STRATEGY, lambda key, active_clock: PyJWTSigner(key, active_clock), Path(__file__).resolve(), environment)
    return 0 if run["summary"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

