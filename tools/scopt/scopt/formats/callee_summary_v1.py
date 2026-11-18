"""CalleeSummary v1: ``summaries/callee_summaries.jsonl`` (B3/B4 design §3.3; WP20).

One canonical-JSON row per **return unit** (an R unit whose exit instruction
is a return), exported whether or not any edge is specialized. Schema
``fastdyn.callee_summary`` version 1 (``schemas/callee_summary.v1.json``).

Identities:

* ``row_hash`` = ``sha256:`` of the canonical JSON of the row without
  ``row_hash`` (:func:`row_hash`). A FactRef carries it as
  ``summary_row_hash``; a FactRef whose row is not in the current summary
  set is stale (:func:`check_factref` raises
  :class:`~scopt.passes.base.StaleFact`, reason ``stale_fact``).
* ``fact_id`` = ``sha256:`` of the canonical JSON of
  ``{return_unit, content_hash, exit_id, field, value, defs}``
  (:func:`fact_id`): the same fact on the same unit bytes has the same id.

:func:`factref` builds the FactRef of §3.7/§3.9:
``{fact_id, field, value, def_insn, source, cert_hash, summary_row_hash}``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from ..canon import canonical_json, canonical_jsonl, sha256_id

__all__ = ["SCHEMA", "VERSION", "row_hash", "fact_id", "factref", "validate_row", "seal_row",
           "write_callee_summaries", "read_callee_summaries", "check_factref",
           "CalleeSummaryError", "DEFAULT_RELPATH"]

SCHEMA = "fastdyn.callee_summary"
VERSION = 1
DEFAULT_RELPATH = "summaries/callee_summaries.jsonl"


class CalleeSummaryError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator

    text = resources.files("scopt.formats").joinpath("schemas", "callee_summary.v1.json") \
        .read_text("utf-8")
    schema = json.loads(text)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_row(row: Mapping[str, Any]) -> None:
    errs = sorted(f"{'/'.join(str(p) for p in e.absolute_path) or '$'}: {e.message}"
                  for e in _validator().iter_errors(row))
    if errs:
        raise CalleeSummaryError("callee_summary_schema", "; ".join(errs[:5]))
    if row_hash(row) != row.get("row_hash"):
        raise CalleeSummaryError("callee_summary_row_hash",
                                 f"{row.get('return_unit')}: stored {row.get('row_hash')}")


def row_hash(row: Mapping[str, Any]) -> str:
    return sha256_id(canonical_json({k: v for k, v in row.items() if k != "row_hash"}))


def fact_id(return_unit: str, content_hash: str, exit_id: str, field: str, value: int,
            defs: Sequence[tuple[int, int | None]]) -> str:
    return sha256_id(canonical_json({
        "return_unit": return_unit, "content_hash": content_hash, "exit_id": exit_id,
        "field": field, "value": f"0x{value:x}",
        "defs": [[o, i] for o, i in defs]}))


def seal_row(row: dict[str, Any]) -> dict[str, Any]:
    """Set ``row_hash`` and validate."""
    row = dict(row)
    row.pop("row_hash", None)
    row["row_hash"] = row_hash(row)
    validate_row(row)
    return row


def factref(row: Mapping[str, Any], fact: Mapping[str, Any]) -> dict[str, Any]:
    """The FactRef (§3.7 ``ATTACH_FACT`` payload, §3.9 item schema) of one fact row."""
    return {"fact_id": fact["fact_id"], "field": fact["field"], "value": fact["value"],
            "def_insn": fact["def_insn"], "source": fact["source"],
            "cert_hash": fact["cert_hash"], "summary_row_hash": row["row_hash"]}


def write_callee_summaries(path: str | Path, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Write the rows (sorted by return unit, exit) as canonical JSONL; returns a summary."""
    rs = sorted(rows, key=lambda r: (r["return_unit"], r["exit_id"]))
    for r in rs:
        validate_row(r)
    data = canonical_jsonl(rs)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)
    return {"path": str(p), "rows": len(rs), "sha256": sha256_id(data),
            "facts": sum(len(r["facts"]) for r in rs)}


def read_callee_summaries(path: str | Path) -> list[dict[str, Any]]:
    rows = []
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        r = json.loads(line)
        try:
            validate_row(r)
        except CalleeSummaryError as e:
            raise CalleeSummaryError(e.code, f"line {i + 1}: {e}") from None
        rows.append(r)
    return rows


def check_factref(ref: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The fact row ``ref`` cites, or :class:`StaleFact` (``stale_fact``) when its
    summary row is gone (unit bytes, plan, layout or certification changed)."""
    from ..passes.base import StaleFact

    for r in rows:
        if r.get("row_hash") != ref.get("summary_row_hash"):
            continue
        for f in r["facts"]:
            if f["fact_id"] == ref.get("fact_id") and f["cert_hash"] == ref.get("cert_hash"):
                return f
    raise StaleFact(f"stale_fact: {ref.get('fact_id')} (row {ref.get('summary_row_hash')})")
