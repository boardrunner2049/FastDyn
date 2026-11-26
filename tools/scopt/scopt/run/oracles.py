"""Dynamic oracles O1-O6 of the B3/B4 differential tests (design §9.4; WP26).

Every oracle compares an *artifact* run (a v5 or v6 artifact, a negative
control) with the *reference* run of the same input on the same binary
(``warm``, the reference arm of §10.1) and returns an :class:`OracleResult`
(``verdict`` one of ``pass``, ``fail``, ``vacuous``, ``not_run``,
``reported``). The comparators are pure functions over recorded files, so
they are unit-tested on recorded logs; :func:`dynamic_oracles` drives the
runs (deploy, launch, diag re-runs) and is what ``scopt validate --dynamic
--oracles ...`` calls.

* **O1 outputs** (:func:`o1_outputs`): both runs proved (``PASS``), equal exit
  code, equal normalized output (``output_norm_sha256``, ``norm_b0``) and equal
  guest output (``guest_norm``, BENCH times masked). With a fixture oracle
  (:func:`load_fixture_expected`, ``fixtures/b3/expected.py``) the artifact
  run must also satisfy the fixture's per-mode oracle (exact ``.norm`` in
  icount, named invariants in noicount); in noicount that oracle replaces the
  byte comparison (timer phases print host-time-dependent records, R14).
* **O2a exception sequence** (:func:`parse_int_log`, :func:`o2a_int`): the
  ``-d int`` lines of the two runs (diag re-runs, :func:`scopt.run.diag.diag_exec`
  with ``exec_trace=False``) are identical. They carry the exception number,
  vector PC, EXC_RETURN magic and fault addresses, not icount or the
  interrupted PC (G31): O2a shows the order of exception kinds only.
* **O2b handler records** (:func:`parse_handler_records`, :func:`o2b_records`):
  the fixture's guest log (``Reg 0:<tag>`` / ``Reg 1:<value>`` pairs, tag
  layout of ``fixtures/b3/README.md``): SysTick records ``0x53xx`` (CVR,
  stacked PC, xPSR, r0-r3, iteration), fault records ``0x54xx`` and
  spare-IRQ records ``0x55xx``, plus the end counters ``0x5fxx`` and handler
  counters ``0x56xx``. **icount**: every record field equal (CVR is a
  function of the executed instruction count under shift 0, so equal records
  mean the same PC and the same instruction count). **noicount**: invariants:
  record counts consistent with the end counters, every record complete,
  fault records equal in their position fields (PC, CFSR, IPSR, BFAR, MMFAR,
  iteration), spare-IRQ records equal in (PC, iteration) (they are raised by a
  count of TB executions, not by time); SysTick
  positions are not compared (timer phases depend on host time), handler
  counters ``0x56xx`` are not compared either (they count SysTick handler
  entries, which depend on host time too), and the fixture's named invariants
  apply when given. In both modes the end counters (``0x5f02``-``0x5f04``)
  must be present in both logs: a log cut short before them fails.
* **O3 non-vacuity** (:func:`o3_nonvacuity`): requirements over the
  ``QEMU_SC_V6_STATS`` counters (``links_armed > 0`` ...) and gdb hit counts
  (``C'`` entry > 0, ``F`` > 0 ...). Unmet: ``vacuous``; stats or hits that
  were not collected, or hit counts from a gdb run whose QEMU did not exit 0:
  ``not_run`` (unknown is never zero).
* **O4 negative controls** (:func:`o4_detected`): a mutant is detected when
  O1, O2a or O2b of the control against the reference record a concrete
  difference, and only for a control run whose post-run proof is ``PASS``
  (the mutant ran; a QEMU abort with a ``PASS`` proof counts). A control
  that was not proved to run, or whose reference was not proved, is
  ``not_run``, never detected.
* **O5 fallback** (:func:`o5_fallback`): reported counts: appended records
  (post-run proof), ``disarm_*``, refusals by reason, variant exits/restores;
  ``fail`` only when the proof failed or the loader's invariant checks report
  a violation.
* **O6 attribution** (:func:`o6_attribution`): the coverage observable equals
  the reference's (FastDyn coverage files when the build writes them; in this
  build none exist, WP16 note 4 / WP24 finding 3, so FastDyn's LOG_REG ring at
  ``benchmark_end`` from the diag twin) and the specialized code ran
  (``C'`` hits > 0).

:func:`oracle_rows` flattens results into report rows for the gate reports
(WP28/WP29).

:func:`dynamic_oracles` refuses a ``.sc`` v6 artifact on a QEMU binary that
cannot load v6 (``OracleRefused("v6_loader_required")``). With a negative
control it runs in O4 mode (its verdict is O4's; see its docstring).
Otherwise its verdicts: ``fail`` when an oracle fails or is
``vacuous`` (or a deploy is refused); ``incomplete`` when a requested oracle
was not run in some case (``not_run``: e.g. no stats file from a runtime without semantic counters, a run that was not proved); ``pass`` only
when every requested oracle ran in every case and none failed. Only ``pass``
exits 0 from ``scopt validate --dynamic --oracles`` (unknown is never
acceptable).
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "OracleResult", "parse_handler_records", "o2b_records", "o1_outputs", "parse_int_log",
    "o2a_int", "o3_nonvacuity", "o4_detected", "o5_fallback", "o6_attribution",
    "oracle_rows", "fixture_norm", "load_fixture_expected", "icount_mode", "dynamic_oracles",
    "ORACLES", "SREC", "FREC", "IREC", "OracleRefused", "O4_EXECUTED_VERDICTS",
]

ORACLES = ("O1", "O2a", "O2b", "O3", "O4", "O5", "O6")
VERDICTS = ("pass", "fail", "vacuous", "not_run", "reported")

# record layouts (fixtures/b3/README.md "Phase selection and the guest log")
SREC = ("cvr", "pc", "xpsr", "r0", "r1", "r2", "r3", "iter")
FREC = ("pc", "xpsr", "cfsr", "bfar", "mmfar", "ipsr", "iter", "r0", "r1", "r2", "r3",
        "r12", "lr")
IREC = ("pc", "xpsr", "iter", "r0")
_KINDS = {0x53: ("systick", SREC), 0x54: ("fault", FREC), 0x55: ("irq", IREC)}
#: fault fields that fix the fault position (compared in noicount too)
FAULT_POSITION = ("pc", "cfsr", "ipsr", "bfar", "mmfar", "iter")
IRQ_POSITION = ("pc", "iter")


@dataclass
class OracleResult:
    oracle: str
    verdict: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.oracle not in ORACLES:
            raise ValueError(f"unknown oracle {self.oracle!r}")
        if self.verdict not in VERDICTS:
            raise ValueError(f"unknown verdict {self.verdict!r}")

    @property
    def ok(self) -> bool:
        return self.verdict in ("pass", "reported")

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# O2b: handler records
# ---------------------------------------------------------------------------

def _lines(text_or_lines: str | Sequence[str]) -> list[str]:
    return text_or_lines.splitlines() if isinstance(text_or_lines, str) else list(text_or_lines)


def parse_handler_records(text_or_lines: str | Sequence[str]) -> dict[str, Any]:
    """Parse a fixture guest log into its records.

    Returns ``{"pairs", "header", "results", "hk", "end", "systick", "fault",
    "irq", "errors"}``; each record is a dict in its field order. The log starts
    at the phase header pair (tag ``0x00500000``); ``Reg`` lines before it
    (callback output) are not records."""
    lines = [x for x in _lines(text_or_lines) if x.startswith("Reg ")]
    out: dict[str, Any] = {"pairs": [], "header": {}, "results": {}, "hk": {}, "end": {},
                           "systick": [], "fault": [], "irq": [], "errors": []}
    i = 0
    while i < len(lines) and lines[i] != "Reg 0:0x00500000":
        i += 1
    while i < len(lines):
        a = lines[i]
        b = lines[i + 1] if i + 1 < len(lines) else ""
        if a.startswith("Reg 0:") and b.startswith("Reg 1:"):
            try:
                out["pairs"].append((int(a[6:], 16), int(b[6:], 16)))
            except ValueError:
                out["errors"].append(f"bad pair {a!r} {b!r}")
            i += 2
            continue
        out["errors"].append(f"unpaired line {a!r}")
        i += 1
    cur = None
    for tag, val in out["pairs"]:
        kind, idx = tag >> 16, tag & 0xFFFF
        if kind == 0x50:
            out["header"][idx] = val
        elif kind == 0x52:
            out["results"][idx] = val
        elif kind == 0x56:
            out["hk"][idx] = val
        elif kind == 0x5F:
            out["end"][idx] = val
        elif kind in _KINDS:
            name, fields = _KINDS[kind]
            if idx == 0:
                cur = (name, {})
                out[name].append(cur[1])
            if cur is None or cur[0] != name or idx >= len(fields):
                out["errors"].append(f"bad record tag {tag:#x}")
                continue
            if fields[idx] in cur[1]:
                # a record whose index-0 pair is missing: its fields would
                # overwrite the previous record of that kind (WP26 review 2)
                out["errors"].append(f"repeated field {name}.{fields[idx]} (tag {tag:#x}) "
                                     f"in {name}[{len(out[name]) - 1}]: a record without "
                                     "its index-0 pair")
                continue
            cur[1][fields[idx]] = val
        else:
            out["errors"].append(f"unknown tag {tag:#x}")
    return out


def _rec_diff(kind: str, ref: list[dict], art: list[dict], fields: Iterable[str]) -> list[str]:
    out = []
    if len(ref) != len(art):
        out.append(f"{kind} records: {len(art)} != reference {len(ref)}")
    for k, (r, a) in enumerate(zip(ref, art)):
        for f in fields:
            if r.get(f) != a.get(f):
                rv, av = r.get(f), a.get(f)
                out.append(f"{kind}[{k}].{f}: {_hx(av)} != reference {_hx(rv)}")
    return out


def _hx(v: Any) -> str:
    return f"{v:#x}" if isinstance(v, int) else repr(v)


def o2b_records(ref: str | Sequence[str], art: str | Sequence[str], mode: str, *,
                invariants: Callable[[], list[str]] | None = None,
                max_diffs: int = 20) -> OracleResult:
    """Compare the handler records of ``art`` with ``ref`` (guest logs).

    ``mode``: ``icount`` (exact) or ``noicount`` (invariants, see the module
    docstring). ``invariants`` (optional) returns the fixture's named-invariant
    failures for the artifact run; they count in both modes."""
    if mode not in ("icount", "noicount"):
        raise ValueError(f"mode must be icount or noicount, not {mode!r}")
    pr, pa = parse_handler_records(ref), parse_handler_records(art)
    diffs: list[str] = []
    ref_problems: list[str] = []
    for side, p in (("reference", pr), ("artifact", pa)):
        n_before = len(diffs)
        if p["errors"]:
            diffs.append(f"{side} log: {p['errors'][:3]}")
        if not p["pairs"]:
            diffs.append(f"{side} log has no guest record pairs")
        for name, fields in (("systick", SREC), ("fault", FREC), ("irq", IREC)):
            inc = [k for k, r in enumerate(p[name]) if len(r) != len(fields)]
            if inc:
                diffs.append(f"{side} {name} records incomplete: {inc[:5]}")
        e = p["end"]
        missing = [f"0x5f{idx:02x}" for idx in (2, 3, 4) if e.get(idx) is None]
        if missing:
            diffs.append(f"{side} log lacks end counters {missing} (cut short?)")
        for idx, name in ((2, "fault"), (3, "systick"), (4, "irq")):
            if e.get(idx) is not None and e[idx] != len(p[name]):
                diffs.append(f"{side} end[{idx}] = {e[idx]} but {len(p[name])} {name} records")
        if side == "reference":
            ref_problems = diffs[n_before:]
    counts = {k: (len(pr[k]), len(pa[k])) for k in ("systick", "fault", "irq")}
    if mode == "icount":
        diffs += _rec_diff("systick", pr["systick"], pa["systick"], SREC)
        diffs += _rec_diff("fault", pr["fault"], pa["fault"], FREC)
        diffs += _rec_diff("irq", pr["irq"], pa["irq"], IREC)
        for name in ("hk", "end"):
            if pr[name] != pa[name]:
                diffs.append(f"{name} counters {pa[name]} != reference {pr[name]}")
    else:
        diffs += _rec_diff("fault", pr["fault"], pa["fault"], FAULT_POSITION)
        diffs += _rec_diff("irq", pr["irq"], pa["irq"], IRQ_POSITION)
    if invariants is not None:
        diffs += [f"fixture invariant: {x}" for x in invariants()]
    data = {"mode": mode, "records": {k: {"reference": a, "artifact": b}
                                      for k, (a, b) in counts.items()},
            "differences": len(diffs), "reference_problems": ref_problems[:5]}
    if diffs:
        return OracleResult("O2b", "fail", "; ".join(diffs[:max_diffs]), data)
    n = sum(b for _, b in counts.values())
    return OracleResult("O2b", "pass", f"{mode}: {n} handler records "
                        + ("equal" if mode == "icount" else "consistent"), data)


# ---------------------------------------------------------------------------
# O1: outputs
# ---------------------------------------------------------------------------

_FIXTURE_GUEST = re.compile(r"^(Current CPU type|\[[0-9]+\]\[[0-9.]+\] : |Reg [0-9]+:|\[BENCH\]|"
                            r"qemu-system-arm:)")


def fixture_norm(guest_norm: Sequence[str], exit_code: int | None) -> str:
    """A scopt run's ``guest_norm`` in the fixture's ``guest.norm`` form
    (``fixtures/lib/fdrun.py``: guest lines, BENCH masked, ``exit_code=<rc>``)."""
    keep = [x for x in guest_norm if _FIXTURE_GUEST.match(x)]
    return "\n".join(keep + [f"exit_code={exit_code}"]) + "\n"


def load_fixture_expected(path: str | os.PathLike) -> Callable[[str, str, str], list[str]]:
    """Load a fixture's ``expected.py`` (``compare(text, input, mode) -> [failures]``)
    from its path (the fixture lives outside the package; its directory is put
    on ``sys.path`` for its own imports)."""
    p = Path(path).resolve()
    d = str(p.parent)
    if d not in sys.path:
        sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(f"scopt_fixture_expected_{abs(hash(d))}", p)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {p}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.compare


def o1_outputs(ref: Mapping[str, Any], art: Mapping[str, Any], *,
               fixture: Callable[[], list[str]] | None = None,
               exact: bool = True) -> OracleResult:
    """O1 over two launch documents (``run.json``).

    ``exact=False`` (noicount with a fixture oracle, R14): the outputs of timer
    phases depend on host time, so only the exit codes are compared and the
    fixture's per-mode oracle decides the rest (it still requires the exact
    ``.norm`` for phases without a timer). ``exact=False`` without
    ``fixture`` is refused (nothing would be compared)."""
    if not exact and fixture is None:
        raise ValueError("o1_outputs(exact=False) needs a fixture oracle")
    fails = []
    unproved = []
    for side, d in (("reference", ref), ("artifact", art)):
        if d.get("verdict") != "PASS":
            fails.append(f"{side} run verdict {d.get('verdict')}")
            unproved.append(side)
    differences = []
    for k in (("exit_code", "output_norm_sha256", "guest_norm") if exact else ("exit_code",)):
        if ref.get(k) != art.get(k):
            fails.append(f"{k} differs")
            differences.append(k)
    if fixture is not None:
        fx = fixture()
        fails += [f"fixture oracle: {x}" for x in fx]
        if fx:
            differences.append("fixture")
    # ``differences``: what the outputs differ in (O4 counts only these);
    # ``unproved``: runs whose verdict is not PASS (an O1 failure for that
    # reason alone says nothing about the outputs)
    data = {"exit_code": art.get("exit_code"), "differences": differences,
            "unproved": unproved,
            "guest_norm_sha256": "sha256:" + hashlib.sha256(
                "\n".join(art.get("guest_norm") or []).encode()).hexdigest()}
    if fails:
        return OracleResult("O1", "fail", "; ".join(fails), data)
    return OracleResult("O1", "pass", "outputs equal to the reference", data)


# ---------------------------------------------------------------------------
# O2a: -d int exception sequence
# ---------------------------------------------------------------------------

_INT_LINE = re.compile(r"^(Taking exception|\.\.\.|Exception return|Loaded reset SP|"
                       r"Unexpected|Lazy|Stack|v7m|M profile)")


def parse_int_log(text: str) -> list[str]:
    """The exception lines of a ``-d int`` log (other lines, e.g. loader
    warnings, are dropped)."""
    return [x for x in text.splitlines() if _INT_LINE.match(x)]


def o2a_int(ref_text: str | None, art_text: str | None) -> OracleResult:
    """O2a: identical ``-d int`` exception sequences."""
    if ref_text is None or art_text is None:
        return OracleResult("O2a", "not_run", "no -d int log for "
                            + ("the reference" if ref_text is None else "the artifact"))
    a, b = parse_int_log(ref_text), parse_int_log(art_text)
    data = {"lines": {"reference": len(a), "artifact": len(b)},
            "sha256": {"reference": _sha_lines(a), "artifact": _sha_lines(b)}}
    if a == b:
        return OracleResult("O2a", "pass", f"{len(a)} exception lines identical", data)
    k = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    data["first_difference"] = {"line": k, "reference": a[k] if k < len(a) else None,
                                "artifact": b[k] if k < len(b) else None}
    return OracleResult("O2a", "fail", f"exception sequence differs at line {k}", data)


def _sha_lines(xs: Sequence[str]) -> str:
    return "sha256:" + hashlib.sha256(("\n".join(xs) + "\n").encode()).hexdigest()


# ---------------------------------------------------------------------------
# O3 / O5: statistics and hit counts
# ---------------------------------------------------------------------------

_REQ = re.compile(r"^(>=|>|==|<=|<)\s*(\d+)$")


def _req_ok(v: int, req: str) -> bool:
    m = _REQ.match(req.strip())
    if not m:
        raise ValueError(f"bad requirement {req!r} (use '>0', '>=1', '==0' ...)")
    op, n = m.group(1), int(m.group(2))
    return {">": v > n, ">=": v >= n, "==": v == n, "<=": v <= n, "<": v < n}[op]


def o3_nonvacuity(stats: Mapping[str, Any] | None, hits: Mapping[str, Any] | None,
                  requirements: Mapping[str, Any]) -> OracleResult:
    """O3 against ``requirements``: ``{"stats": {"links_armed": ">0", ...},
    "hits": {"<target label>": ">0", ...}}``. A stats counter may name a
    ``refused`` reason as ``refused.<reason>``."""
    unmet, unknown, seen = [], [], {}
    for k, req in (requirements.get("stats") or {}).items():
        if not stats or not stats.get("present", True) or stats.get("problems"):
            unknown.append(f"stats.{k}")
            continue
        v = stats.get("refused", {}).get(k.split(".", 1)[1]) if k.startswith("refused.") \
            else stats.get(k)
        if not isinstance(v, int):
            unknown.append(f"stats.{k}")
            continue
        seen[f"stats.{k}"] = v
        if not _req_ok(v, req):
            unmet.append(f"stats.{k} = {v}, need {req}")
    # hit counts from a gdb run whose QEMU did not exit 0 (crashed, killed,
    # timed out) are unknown, not counts (WP26 review)
    hruns = (hits or {}).get("runs")
    bad_runs: set[Any] = set()
    if isinstance(hruns, list):
        bad_runs = {i for i, r in enumerate(hruns) if r.get("qemu_exit") != 0}
    elif hits is not None and hits.get("ok") is False:
        bad_runs = {"*"}
    rows = {r["what"]: (None if ("*" in bad_runs or r.get("run") in bad_runs)
                        else r.get("hits"))
            for r in ((hits or {}).get("targets") or [])}
    for k, req in (requirements.get("hits") or {}).items():
        v = rows.get(k)
        if v is None:
            unknown.append(f"hits[{k}]")
            continue
        seen[f"hits[{k}]"] = v
        if not _req_ok(v, req):
            unmet.append(f"hits[{k}] = {v}, need {req}")
    data = {"observed": seen, "unknown": unknown, "unmet": unmet,
            "requirements": dict(requirements)}
    if unmet:
        return OracleResult("O3", "vacuous", "; ".join(unmet), data)
    if unknown:
        return OracleResult("O3", "not_run", "not collected: " + ", ".join(unknown), data)
    if not seen:
        return OracleResult("O3", "not_run", "no requirement given", data)
    return OracleResult("O3", "pass", ", ".join(f"{k}={v}" for k, v in seen.items()), data)


def o5_fallback(run: Mapping[str, Any], stats: Mapping[str, Any] | None) -> OracleResult:
    """O5: fallback counts (reported)."""
    pr = run.get("proof") or {}
    data: dict[str, Any] = {"proof": pr.get("verdict"), "appended": pr.get("appended"),
                            "file_version": pr.get("file_version")}
    fails = []
    if pr.get("verdict") != "PASS":
        fails.append(f"post-run proof {pr.get('verdict')} ({', '.join(pr.get('failed') or [])})")
    if stats and stats.get("present"):
        for k in ("links_armed", "disarm_tlb_events", "disarm_tlb_links", "disarm_flush_links",
                  "disarm_forbid_links", "variant_exits", "variant_entry_exits",
                  "variant_restores", "invariant_checks", "invariant_violations", "forbidden",
                  "forbid_reason", "loaded"):
            data[k] = stats.get(k)
        data["refused"] = {k: v for k, v in (stats.get("refused") or {}).items() if v}
        if stats.get("invariant_violations"):
            fails.append(f"loader invariant violations {stats['invariant_violations']}")
        if stats.get("problems"):
            fails.append(f"stats problems {stats['problems'][:2]}")
    else:
        data["stats"] = "not collected"
    if fails:
        return OracleResult("O5", "fail", "; ".join(fails), data)
    return OracleResult("O5", "reported", f"appended {data['appended']}", data)


# ---------------------------------------------------------------------------
# O4 / O6
# ---------------------------------------------------------------------------

#: launch verdicts of a control run that executed the deployed mutant: ``PASS``,
#: or ``INVALID_exit_code`` with a ``PASS`` proof (QEMU stopped with a non-zero
#: status while running the proved artifact, e.g. F-B4-9's deleting mutant aborts)
O4_EXECUTED_VERDICTS = ("PASS", "INVALID_exit_code")


def _o4_evidence(r: OracleResult) -> bool:
    """A ``fail`` of O1/O2a/O2b that records a concrete difference of the
    outputs, the exception sequence or the handler records (not a failure
    for an unproved run or a broken reference log)."""
    if r.oracle == "O1":
        return bool(r.data.get("differences"))
    if r.oracle == "O2a":
        return bool(r.data.get("first_difference"))
    if r.oracle == "O2b":
        return ("reference_problems" in r.data and not r.data["reference_problems"]
                and bool(r.data.get("differences")))
    return False


def o4_detected(results: Iterable[OracleResult], *, artifact_run: Mapping[str, Any] | None,
                reference_run: Mapping[str, Any] | None) -> OracleResult:
    """O4 for a negative control (design §9.3/§9.4): the mutant is detected
    iff O1, O2a or O2b of a run that **executed the mutant** differs from the
    reference.

    ``artifact_run``/``reference_run`` are the launch documents (``run.json``)
    of the control run and of the reference run. Fail closed, unknown is
    never a result (WP26 review 2, blocking):

    * the control run's post-run proof is not ``PASS``, or its launch verdict
      is not ``PASS``/``INVALID_exit_code`` (e.g. ``INVALID_artifact_not_used``:
      the loader refused or rebuilt the file, or the runtime lacked the control’s required features; stray capture files): ``not_run``, the mutant never ran;
    * the reference run is not ``PASS``: ``not_run``;
    * a ``fail`` counts as detection only when it records a concrete
      difference (O1 ``data["differences"]``: exit code, outputs, fixture
      oracle; O2a ``first_difference``; O2b record differences with a
      structurally sound reference log). A QEMU abort with a ``PASS`` proof
      counts (its exit code differs from the reference's 0). Other ``fail`` results
      are listed in ``data["ignored"]``;
    * no O1/O2a/O2b result that ran: ``not_run``; ran and none detected:
      ``fail`` (mutant not detected)."""
    rs = [r for r in results if r.oracle in ("O1", "O2a", "O2b")]
    art, ref = artifact_run or {}, reference_run or {}
    a_verdict, r_verdict = art.get("verdict"), ref.get("verdict")
    a_proof = (art.get("proof") or {}).get("verdict")
    data: dict[str, Any] = {"artifact_run": {"verdict": a_verdict, "proof": a_proof},
                            "reference_run": {"verdict": r_verdict},
                            "ran": [], "detected_by": [], "ignored": []}
    if artifact_run is None or reference_run is None:
        return OracleResult("O4", "not_run", "no launch document for the "
                            + ("control" if artifact_run is None else "reference") + " run",
                            data)
    if a_proof != "PASS" or a_verdict not in O4_EXECUTED_VERDICTS:
        return OracleResult("O4", "not_run", f"the control did not provably run the mutant "
                            f"(launch verdict {a_verdict}, proof {a_proof}); unknown is never "
                            "detected", data)
    if r_verdict != "PASS":
        return OracleResult("O4", "not_run", f"reference run verdict {r_verdict}", data)
    for r in rs:
        if r.verdict == "pass":
            data["ran"].append(r.oracle)
        elif r.verdict == "fail":
            if _o4_evidence(r):
                data["ran"].append(r.oracle)
                data["detected_by"].append(r.oracle)
            else:
                data["ignored"].append(f"{r.oracle}: {r.detail[:160]}")
    det = data["detected_by"]
    if det:
        return OracleResult("O4", "pass", "mutant detected by " + ",".join(det), data)
    if not data["ran"]:
        return OracleResult("O4", "not_run", "no O1/O2a/O2b result that ran"
                            + (f" (ignored: {data['ignored'][:2]})" if data["ignored"] else ""),
                            data)
    return OracleResult("O4", "fail", "mutant not detected: O1/O2a/O2b equal to the reference",
                        data)


def o6_attribution(ref_cov: Mapping[str, Any] | None, art_cov: Mapping[str, Any] | None,
                   hits: Mapping[str, Any] | None = None,
                   hit_target: str | None = None) -> OracleResult:
    """O6: coverage observable equal (``{"files": {name: sha}}`` and/or
    ``{"ring": {"index", "buffer_sha256"}}`` per side) and, when
    ``hit_target`` is named, its hit count > 0 (``C'`` ran)."""
    if not ref_cov or not art_cov:
        return OracleResult("O6", "not_run", "coverage observable not collected")
    fails, data = [], {"reference": dict(ref_cov), "artifact": dict(art_cov)}
    compared = []
    rf, af = ref_cov.get("files") or {}, art_cov.get("files") or {}
    if rf or af:
        compared.append("files")
        if rf != af:
            fails.append("coverage files differ: " + ", ".join(
                sorted(k for k in set(rf) | set(af) if rf.get(k) != af.get(k))))
    rr, ar = ref_cov.get("ring") or {}, art_cov.get("ring") or {}
    got = [bool(x.get("buffer_sha256")) for x in (rr, ar)]
    if all(got):
        compared.append("ring")
        if (rr.get("index"), rr.get("buffer_sha256")) != (ar.get("index"),
                                                          ar.get("buffer_sha256")):
            fails.append("LOG_REG coverage ring differs at benchmark_end")
    elif any(got):
        # one side has a ring and the other none: unknown, never equal
        return OracleResult("O6", "not_run", "coverage ring collected on one side only", data)
    if not compared:
        return OracleResult("O6", "not_run", "neither coverage files nor the ring "
                            "(no coverage instrumentation in this configuration?)", data)
    if hit_target is not None:
        rows = {r["what"]: r.get("hits") for r in ((hits or {}).get("targets") or [])}
        v = rows.get(hit_target)
        data["hits"] = {hit_target: v}
        if v is None:
            return OracleResult("O6", "not_run", f"no hit count for {hit_target}", data)
        if v <= 0:
            fails.append(f"{hit_target} hits {v}: the specialized code did not run")
    data["compared"] = compared
    if fails:
        return OracleResult("O6", "fail", "; ".join(fails), data)
    return OracleResult("O6", "pass", "coverage equal (" + "+".join(compared) + ")", data)


def coverage_files(work: str | os.PathLike) -> dict[str, str]:
    """sha256 of FastDyn's coverage outputs in a run's work directory (``bbl.txt``,
    ``edges.txt``, ``*cov*``); empty when the build writes none."""
    w = Path(work)
    out = {}
    if w.is_dir():
        for p in sorted(w.rglob("*")):
            if p.is_file() and (p.name in ("bbl.txt", "edges.txt") or "cov" in p.name.lower()):
                out[str(p.relative_to(w))] = "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# report rows
# ---------------------------------------------------------------------------

def oracle_rows(results: Iterable[OracleResult], **ctx: Any) -> list[dict[str, Any]]:
    """Flat report rows ``{**ctx, oracle, verdict, detail}`` (ctx: fixture, input,
    arm, mode, gate binary ...), one per result, in oracle order."""
    order = {o: i for i, o in enumerate(ORACLES)}
    return [{**ctx, "oracle": r.oracle, "verdict": r.verdict, "detail": r.detail}
            for r in sorted(results, key=lambda r: order[r.oracle])]


# ---------------------------------------------------------------------------
# the dynamic driver (scopt validate --dynamic --oracles ...)
# ---------------------------------------------------------------------------

_ICOUNT_RE = re.compile(r"^\s*icount\s*=\s*\S", re.M)


def _icount_on(v: Any) -> bool:
    if isinstance(v, Mapping):
        return v.get("enabled", True) is not False
    if isinstance(v, str):
        return v.strip().lower() not in ("", "off", "false", "no", "none", "0")
    return bool(v) and v is not None


def _find_icount(doc: Mapping[str, Any]) -> Any:
    if "icount" in doc:
        return doc["icount"]
    for v in doc.values():
        if isinstance(v, Mapping):
            r = _find_icount(v)
            if r is not None:
                return r
    return None


def icount_mode(config_text: str) -> str:
    """``icount`` when the FastDyn TOML enables icount (an ``icount`` key, at the
    top or in a table, whose value is a table without ``enabled = false``, a
    true/non-zero value or a non-empty string other than ``off``/``false``),
    else ``noicount``. ``icount = false`` is noicount. A TOML that does not
    parse falls back to the text form of the same rule."""
    import tomllib

    try:
        v = _find_icount(tomllib.loads(config_text))
    except tomllib.TOMLDecodeError:
        m = _ICOUNT_RE.search(config_text)
        if not m:
            return "noicount"
        rest = config_text[m.end() - 1:].split("\n", 1)[0].split("#", 1)[0].strip().lower()
        v = rest not in ("false", "0", '""', "''", '"off"', "'off'")
    return "icount" if v is not None and _icount_on(v) else "noicount"


def _read(p: Path | None) -> str | None:
    return p.read_text(errors="replace") if p is not None and p.is_file() else None


class OracleRefused(RuntimeError):
    """:func:`dynamic_oracles` refuses to run (fail closed before any launch)."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _semantic_runtime_refusal(art: Path, qemu: str | os.PathLike | None) -> tuple[str, str] | None:
    """``(code, detail)`` when ``art`` is a semantic-v7 artifact and the QEMU the
    runs would use (``qemu``, or the integrated runtime for None) is not a
    v6-capable binary (:func:`scopt.validate.semantic_capable_exes`): run there,
    every v6 run is ``INVALID_artifact_not_used``."""
    from .arms import sc_file_features, sc_file_version
    from .launcher import RunDirError, resolve_qemu

    if sc_file_version(art) != 7:
        return ("legacy_cache_version", f"{art} is not canonical top-level v7")
    from ..formats.sc_semantic import SC7_SEMANTIC_MARKER
    if not (sc_file_features(art) or 0) & SC7_SEMANTIC_MARKER:
        return None
    from ..validate import semantic_capable_exes

    try:
        q = resolve_qemu(qemu)
    except RunDirError as e:
        return ("semantic_loader_required", f"cannot resolve the QEMU binary: {e}")
    sha = q.get("qemu_sha256")
    hexsha = None if sha is None else str(sha).removeprefix("sha256:")
    if hexsha is None or hexsha not in semantic_capable_exes():
        return ("semantic_loader_required",
                f"{art} is semantic v7 but QEMU {q.get('qemu')} (sha256 {hexsha}, "
                f"{q.get('qemu_selection')}) is not semantic-v7-capable")
    return None


def dynamic_oracles(art_dir: str | os.PathLike, configs: Sequence[str | os.PathLike],
                    out_dir: str | os.PathLike, *, oracles: Iterable[str] = ("O1", "O2b", "O5"),
                    baseline: str | os.PathLike | None = None, reps: int = 1,
                    qemu: str | os.PathLike | None = None, timeout_s: float = 300.0,
                    inputs: Sequence[str | os.PathLike] = (), input_bank: str = "input",
                    mode: str | None = None, fixture_expected: str | os.PathLike | None = None,
                    fixture_inputs: Sequence[str] | None = None,
                    o3_requirements: Mapping[str, Any] | None = None,
                    hit_targets: Sequence[str] | None = None, v6_stats: bool = True,
                    allow_negative_control: bool = False, test_policies: Iterable[str] = (),
                    quiet: bool = False) -> dict[str, Any]:
    """Run the artifact and the reference (``baseline``: default the input
    snapshot's store directory, i.e. ``warm``) once per config x input x rep in
    fresh run directories and evaluate ``oracles`` (see the module docstring).

    ``O2a`` and ``O6`` and the hit counts of ``O3`` need diag re-runs of both
    runs (``-d int``; the twin with the coverage ring; gdb hit counters on the
    artifact's ``C'``/``F`` addresses, :func:`scopt.run.diag.artifact_probe_addrs`,
    restricted to ``hit_targets`` labels when given, at most 4 per gdb run).
    ``fixture_expected`` (``fixtures/b3/expected.py``) with ``fixture_inputs``
    (one fixture input name per entry of ``inputs``) adds the fixture's
    per-mode oracle to O1 and its named invariants to O2b. Writes
    ``out_dir/dynamic_oracles.json`` and a ``validated_dynamic`` ledger row;
    ``verdict``: ``fail`` when an oracle ``fail``\\s or is ``vacuous`` (or a
    deploy is refused), else ``incomplete`` when a requested oracle is
    ``not_run`` (or missing) in some case, else ``pass``.

    ``allow_negative_control=True`` (``--negative-control``) is the **O4
    mode**: the artifact is a negative control, each case adds
    :func:`o4_detected` over its O1/O2a/O2b results and the launch
    documents, and the verdict follows O4 alone: ``pass`` = the mutant was
    detected in every case, ``fail`` = not detected in some case (or a
    deploy was refused), ``incomplete`` = O4 ``not_run`` (the control was
    not proved to run the mutant). The control's O1/O2a/O2b failures are
    its detections, not failures.

    Refuses (:class:`OracleRefused` ``v6_loader_required``, before any run) a
    ``.sc`` v6 artifact when the QEMU the runs would use is not a v6-capable
    binary (:func:`scopt.validate.semantic_capable_exes`).

    ``quiet=True`` runs FastDyn with ``-q``, which changes its ``log_level`` and so the
    bundle's ``contract_id``: an artifact whose bundle was computed for a non-quiet base is
    refused ``bundle_mismatch`` at deploy (fail closed; the refusal detail says so)."""
    from ..formats import artifact as A
    from ..ids import sha256_id
    from ..ledger import Ledger
    from .deploy import DeployError, deploy_new
    from .diag import (DiagError, artifact_probe_addrs, diag_exec, diag_twin, gdb_hits,
                       read_v6_stats, stats_env)
    from .launcher import launch

    want = [o.strip() for o in oracles if o.strip()]
    negative = bool(allow_negative_control)
    if negative:
        want = [o for o in want if o != "O4"]
        if not any(o in want for o in ("O1", "O2a", "O2b")):
            raise ValueError("a negative control needs O1, O2a or O2b (O4 is computed "
                             "from them)")
    bad = [o for o in want if o not in ORACLES or o == "O4"]
    if bad:
        raise ValueError(f"unknown or non-runnable oracles {bad} (O4 is evaluated only for "
                         "a negative control: allow_negative_control / --negative-control)")
    art = Path(art_dir).resolve()
    info = A.read_artifact(art) if (art / A.SIDECAR).is_file() else None
    if baseline is None:
        if info is None:
            raise ValueError("no baseline given and the artifact has no sidecar")
        baseline = art.parent.parent / "snapshots" / A.id_hex(info.input_snapshot_id)
    baseline = Path(baseline).resolve()
    if qemu is None and info is not None and info.runtime_identity:
        qemu = (info.runtime_identity.get("qemu_exe_stat") or {}).get("path")
    why = _semantic_runtime_refusal(art, qemu)
    if why is not None:
        raise OracleRefused(*why)
    fx = load_fixture_expected(fixture_expected) if fixture_expected else None
    ins = [str(Path(x).resolve()) for x in inputs]
    if fixture_inputs is not None and len(fixture_inputs) != max(1, len(ins)):
        raise ValueError("fixture_inputs needs one name per input")
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    fails: list[str] = []
    incomplete: list[str] = []
    probes = artifact_probe_addrs(art / "cache.sc" if art.is_dir() else art)
    if hit_targets is not None:
        probes = [p for p in probes if p["what"] in set(hit_targets)]
    for ci, cfg in enumerate(configs):
        cfg_text = Path(cfg).read_text()
        md = mode or icount_mode(cfg_text)
        for ii, inp in enumerate(ins or [None]):
            fin = None if fixture_inputs is None else fixture_inputs[ii]
            tag = f"c{ci}" if inp is None else f"c{ci}-i{ii}"
            for rep in range(reps):
                case: dict[str, Any] = {"config": str(cfg), "input": inp, "rep": rep, "mode": md,
                                        "fixture_input": fin}
                runs: dict[str, Any] = {}
                for arm, src in (("artifact", art), ("reference", baseline)):
                    rd_path = out / f"{tag}-r{rep}-{arm}"
                    try:
                        kw = {}
                        if arm == "artifact":
                            kw = {"allow_negative_control": allow_negative_control,
                                  "test_policies": tuple(test_policies)}
                        rd, _dep = deploy_new(src, cfg, rd_path, qemu=qemu, quiet=quiet,
                                              timeout_s=min(timeout_s, 300.0), **kw)
                    except DeployError as e:
                        detail = e.detail
                        if quiet and e.code == "bundle_mismatch":
                            # WP28 finding: -q selects FastDyn's log_level, which is part of
                            # the bundle's contract_id; an artifact built from a non-quiet
                            # base is therefore always refused under quiet=True.
                            detail = (f"{detail} (quiet=True: -q changes log_level, which is "
                                      "part of contract_id; run without quiet for artifacts "
                                      "built from a non-quiet base)")
                        runs[arm] = {"run_dir": str(rd_path), "deploy_refused": e.code,
                                     "detail": detail}
                        fails.append(f"{tag} r{rep} {arm}: deploy refused {e.code}"
                                     + (" (quiet=True changes contract_id)"
                                        if quiet and e.code == "bundle_mismatch" else ""))
                        continue
                    sp = rd.root / "v6-stats.json"
                    doc = launch(rd, timeout_s=timeout_s,
                                 inputs={input_bank: inp} if inp else None,
                                 extra_env=stats_env(sp) if v6_stats else None)
                    doc["v6_stats"] = read_v6_stats(sp) if v6_stats else None
                    runs[arm] = doc
                case["runs"] = {k: {x: v.get(x) for x in ("run_dir", "verdict", "exit_code",
                                                            "appended", "deploy_refused",
                                                            "detail")}
                                for k, v in runs.items()}
                a, r = runs.get("artifact", {}), runs.get("reference", {})
                res: list[OracleResult] = []
                both = "verdict" in a and "verdict" in r
                if not both:
                    res = [OracleResult(o, "not_run", "deploy refused") for o in want]
                else:
                    gn = a.get("guest_norm") or []
                    if "O1" in want:
                        fxo = (lambda: fx(fixture_norm(gn, a.get("exit_code")), fin, md)) \
                            if fx and fin else None
                        res.append(o1_outputs(r, a, fixture=fxo,
                                              exact=not (fxo is not None and md == "noicount")))
                    if "O2b" in want:
                        inv = None
                        if fx and fin and md == "noicount":
                            inv = lambda: fx(fixture_norm(gn, a.get("exit_code")), fin, md)  # noqa: E731
                        res.append(o2b_records(r.get("guest_norm") or [], gn, md,
                                               invariants=inv))
                    if "O5" in want:
                        res.append(o5_fallback(a, a.get("v6_stats")))
                    diag_ok = a.get("verdict") == "PASS" and r.get("verdict") == "PASS"
                    if "O2a" in want:
                        if not diag_ok:
                            res.append(OracleResult("O2a", "not_run", "a run was not proved"))
                        else:
                            try:
                                ta = diag_exec(a["run_dir"], out / f"{tag}-r{rep}-artifact-int",
                                               exec_trace=False, interrupts=True,
                                               timeout_s=timeout_s)
                                tr = diag_exec(r["run_dir"], out / f"{tag}-r{rep}-reference-int",
                                               exec_trace=False, interrupts=True,
                                               timeout_s=timeout_s)
                                res.append(o2a_int(_read(Path(tr["diag_dir"]) / "qemu.log"),
                                                   _read(Path(ta["diag_dir"]) / "qemu.log")))
                            except DiagError as e:
                                res.append(OracleResult("O2a", "not_run", f"diag: {e}"))
                    hits = None
                    if ("O3" in want or "O6" in want) and probes and diag_ok:
                        try:
                            hits = gdb_hits(a["run_dir"], out / f"{tag}-r{rep}-hits",
                                            [(p["what"], p["addr"]) for p in probes],
                                            timeout_s=timeout_s)
                        except DiagError as e:
                            hits = {"error": str(e), "targets": []}
                    if "O3" in want:
                        req = dict(o3_requirements or {})
                        res.append(o3_nonvacuity(a.get("v6_stats"), hits, req))
                    if "O6" in want:
                        if not diag_ok:
                            res.append(OracleResult("O6", "not_run", "a run was not proved"))
                        else:
                            try:
                                tw = {}
                                for side, d in (("artifact", a), ("reference", r)):
                                    t = diag_twin(d["run_dir"], out / f"{tag}-r{rep}-{side}-twin",
                                                  cov=True, timeout_s=timeout_s)
                                    tw[side] = {"ring": t.get("coverage_ring"),
                                                "files": coverage_files(Path(d["run_dir"]) /
                                                                        "work")}
                                tgt = next((p["what"] for p in probes if p["kind"] == "variant"),
                                           None)
                                res.append(o6_attribution(tw["reference"], tw["artifact"], hits,
                                                          tgt))
                            except DiagError as e:
                                res.append(OracleResult("O6", "not_run", f"diag: {e}"))
                    if hits is not None:
                        case["hits"] = hits
                if negative:
                    o4 = o4_detected(res, artifact_run=a if "verdict" in a else None,
                                     reference_run=r if "verdict" in r else None)
                    res.append(o4)
                    # the control's O1/O2a/O2b failures are its detections:
                    # only O4 decides (fail: mutant not detected)
                    case["oracles"] = [x.to_json() for x in res]
                    case["rows"] = oracle_rows(res, config=str(cfg), input=inp, rep=rep,
                                               mode=md)
                    if o4.verdict == "fail":
                        fails.append(f"{tag} r{rep} O4: fail: {o4.detail[:200]}")
                    elif o4.verdict != "pass":
                        incomplete.append(f"{tag} r{rep} O4: {o4.verdict}: {o4.detail[:200]}")
                    cases.append(case)
                    continue
                case["oracles"] = [x.to_json() for x in res]
                case["rows"] = oracle_rows(res, config=str(cfg), input=inp, rep=rep, mode=md)
                for x in res:
                    if x.verdict in ("fail", "vacuous"):
                        fails.append(f"{tag} r{rep} {x.oracle}: {x.verdict}: {x.detail[:200]}")
                    elif x.verdict == "not_run":
                        incomplete.append(f"{tag} r{rep} {x.oracle}: not_run: "
                                          f"{x.detail[:200]}")
                got = {x.oracle for x in res}
                incomplete += [f"{tag} r{rep} {o}: missing" for o in want if o not in got]
                cases.append(case)
    verdict = "fail" if fails else ("incomplete" if incomplete or not cases else "pass")
    doc = {"schema": "scopt.dynamic_oracles/1", "artifact_dir": str(art),
           "negative_control": negative,
           "artifact_id": None if info is None else info.artifact_id,
           "baseline": str(baseline), "qemu": None if qemu is None else str(qemu),
           "oracles": want, "configs": [str(c) for c in configs], "inputs": ins,
           "input_bank": input_bank, "reps": reps, "verdict": verdict, "failures": fails,
           "incomplete": incomplete,
           "probes": [{**p, "addr": f"0x{p['addr']:x}"} for p in probes], "cases": cases}
    data = A.write_json_file(out / "dynamic_oracles.json", doc)
    led = Ledger()
    led.emit("artifact", (info.artifact_id if info is not None and info.artifact_id else str(art)),
             "validated_dynamic", verdict,
             ("negative control O4 (pass = mutant detected): " if negative else "")
             + (fails[0][:200] if fails else (incomplete[0][:200] if incomplete
                                              else ",".join(want))),
             sha256_id(data))
    led.write(out / "ledger.jsonl")
    return doc
