"""Outcome ledger (design §5.9).

``ledger.jsonl`` holds one canonical JSON row per line::

    {"subject": "unit|pass|artifact|run", "id": …, "stage": …, "verdict": …,
     "reason": …, "evidence_ref": … | null, "ts": "2026-11-16T12:00:00.000Z"}

A row may also carry ``"counts": {name: int}`` (structured tallies, e.g. the
artifact ``inspected`` row's unit statuses). The key is written only when
present, so rows without it are byte-identical to the §5.9 schema.

Stages (each is its own report column; one stage is never inferred from
another, spec §6):

* ``inspected`` — imported and classified (WP7);
* ``optimized`` — a pass ran (or was skipped) in a pipeline (WP9; see below);
* ``compiled`` — candidate bytes emitted (WP10 linker);
* ``accepted`` — V1-V9 passed (WP10);
* ``validated_dynamic`` — V10 passed (WP10/WP8);
* ``measured`` — paired timing collected (WP13).

``optimized`` is an addition to the §5.9 list: pass rows need a stage, and
none of the five listed stages describes "the pass ran"; using ``compiled``
would claim bytes were emitted. For pass rows the verdict is the pass
``Outcome`` value (``no_change``, ``candidate``, ``skipped_unknown``, …).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .canon import canonical_json

__all__ = ["LedgerRow", "Ledger", "SUBJECTS", "STAGES", "LedgerError", "utc_now"]

SUBJECTS = ("unit", "pass", "artifact", "run")
STAGES = ("inspected", "optimized", "compiled", "accepted", "validated_dynamic", "measured")


class LedgerError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True)
class LedgerRow:
    subject: str
    id: str
    stage: str
    verdict: str
    reason: str
    evidence_ref: str | None
    ts: str
    counts: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.subject not in SUBJECTS:
            raise LedgerError(f"unknown ledger subject {self.subject!r}")
        if self.stage not in STAGES:
            raise LedgerError(f"unknown ledger stage {self.stage!r}")
        for name in ("id", "verdict", "ts"):
            v = getattr(self, name)
            if not isinstance(v, str) or not v:
                raise LedgerError(f"ledger {name} must be a non-empty string")
        if not isinstance(self.reason, str):
            raise LedgerError("ledger reason must be a string")
        if self.evidence_ref is not None and not isinstance(self.evidence_ref, str):
            raise LedgerError("ledger evidence_ref must be a string or null")
        if self.counts is not None:
            if not isinstance(self.counts, dict) or not all(
                    isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
                    for k, v in self.counts.items()):
                raise LedgerError("ledger counts must map strings to integers")

    def to_json(self) -> dict[str, Any]:
        d = {"subject": self.subject, "id": self.id, "stage": self.stage,
             "verdict": self.verdict, "reason": self.reason,
             "evidence_ref": self.evidence_ref, "ts": self.ts}
        if self.counts is not None:
            d["counts"] = dict(self.counts)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "LedgerRow":
        extra = set(d) - {"subject", "id", "stage", "verdict", "reason", "evidence_ref", "ts",
                          "counts"}
        if extra:
            raise LedgerError(f"unknown ledger keys {sorted(extra)}")
        return cls(d["subject"], d["id"], d["stage"], d["verdict"], d.get("reason", ""),
                   d.get("evidence_ref"), d["ts"], d.get("counts"))


class Ledger:
    """An append-only list of rows, written as canonical JSONL."""

    def __init__(self, rows: Iterable[LedgerRow] = (), *, clock: Callable[[], str] = utc_now):
        self._rows: list[LedgerRow] = list(rows)
        self._clock = clock

    def emit(self, subject: str, id: str, stage: str, verdict: str, reason: str = "",
             evidence_ref: str | None = None, ts: str | None = None, *,
             counts: dict[str, int] | None = None) -> LedgerRow:
        row = LedgerRow(subject, id, stage, verdict, reason, evidence_ref,
                        ts if ts is not None else self._clock(), counts)
        self._rows.append(row)
        return row

    def extend(self, rows: Iterable[LedgerRow]) -> None:
        for r in rows:
            if not isinstance(r, LedgerRow):
                raise LedgerError(f"not a LedgerRow: {r!r}")
            self._rows.append(r)

    @property
    def rows(self) -> tuple[LedgerRow, ...]:
        return tuple(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def select(self, *, subject: str | None = None, stage: str | None = None,
               id: str | None = None) -> tuple[LedgerRow, ...]:
        return tuple(r for r in self._rows
                     if (subject is None or r.subject == subject)
                     and (stage is None or r.stage == stage) and (id is None or r.id == id))

    def to_jsonl(self) -> bytes:
        return b"".join(canonical_json(r.to_json()) + b"\n" for r in self._rows)

    def write(self, path: str | os.PathLike, *, append: bool = False) -> None:
        """Write (or append) the rows; a fresh file is written atomically."""
        data = self.to_jsonl()
        path = os.fspath(path)
        if append:
            with open(path, "ab") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            return
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    @classmethod
    def read(cls, path: str | os.PathLike) -> "Ledger":
        rows = []
        with open(path, "rb") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    rows.append(LedgerRow.from_json(json.loads(line)))
                except (ValueError, KeyError) as exc:
                    raise LedgerError(f"{path}:{n}: {exc}") from None
        return cls(rows)
