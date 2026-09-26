import json
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum

from .models import Network


class Status(str, Enum):
    PENDING = "pending"        # прошёл security, ждёт ручного решения
    APPROVED = "approved"      # одобрен, ждёт сбора данных
    REJECTED = "rejected"
    COLLECTED = "collected"    # X/Fomo/LP собраны, ждёт LLM-сводки
    RESEARCHED = "researched"  # отчёт готов


# Разрешённые переходы: защищает от двойного нажатия кнопки и гонок между процессами
TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.PENDING: frozenset({Status.APPROVED, Status.REJECTED}),
    Status.APPROVED: frozenset({Status.COLLECTED}),
    Status.COLLECTED: frozenset({Status.RESEARCHED}),
    Status.REJECTED: frozenset(),
    Status.RESEARCHED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Candidate:
    network: Network
    pool: str
    token: str
    verdict: str
    card: str
    status: Status
    report: dict | None
    updated: float


class CandidateStore:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS candidates (
        network TEXT NOT NULL,
        pool    TEXT NOT NULL,
        token   TEXT NOT NULL,
        verdict TEXT NOT NULL,
        card    TEXT NOT NULL,
        status  TEXT NOT NULL,
        report  TEXT,
        updated REAL NOT NULL,
        PRIMARY KEY (network, pool)
    );
    CREATE INDEX IF NOT EXISTS idx_candidates_status ON candidates(status);
    """

    def __init__(self, path: str, cooldown_hours: float = 24.0):
        self.cooldown = cooldown_hours * 3600
        self.db = sqlite3.connect(path, timeout=10)
        # WAL: cron-скрининг и Telegram-бот пишут в базу из разных процессов
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(self._SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add_pending(self, network: Network, pool: str, token: str, verdict: str, card: str,
                    now: float | None = None) -> bool:
        """False, если пул уже рассматривался в пределах cooldown."""
        now = time.time() if now is None else now
        pool = pool.lower()
        with self.db:
            row = self.db.execute(
                "SELECT updated FROM candidates WHERE network=? AND pool=?",
                (network.value, pool),
            ).fetchone()
            if row and now - row[0] < self.cooldown:
                return False
            self.db.execute(
                "REPLACE INTO candidates VALUES (?,?,?,?,?,?,NULL,?)",
                (network.value, pool, token.lower(), verdict, card, Status.PENDING.value, now),
            )
        return True

    def transition(self, network: Network, pool: str, to: Status,
                   report: dict | None = None) -> bool:
        """Атомарный переход статуса. False, если текущий статус его не допускает."""
        allowed_from = [s.value for s, targets in TRANSITIONS.items() if to in targets]
        if not allowed_from:
            return False
        placeholders = ",".join("?" * len(allowed_from))
        report_json = json.dumps(report) if report is not None else None
        with self.db:
            cur = self.db.execute(
                f"UPDATE candidates SET status=?, updated=?, report=COALESCE(?, report) "
                f"WHERE network=? AND pool=? AND status IN ({placeholders})",
                (to.value, time.time(), report_json, network.value, pool.lower(), *allowed_from),
            )
        return cur.rowcount == 1

    def get(self, network: Network, pool: str) -> Candidate | None:
        row = self.db.execute(
            "SELECT * FROM candidates WHERE network=? AND pool=?", (network.value, pool.lower())
        ).fetchone()
        return self._row(row) if row else None

    def by_status(self, status: Status) -> list[Candidate]:
        rows = self.db.execute(
            "SELECT * FROM candidates WHERE status=? ORDER BY updated", (status.value,)
        ).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(r: tuple) -> Candidate:
        network, pool, token, verdict, card, status, report, updated = r
        return Candidate(
            Network(network), pool, token, verdict, card, Status(status),
            json.loads(report) if report else None, updated,
        )
