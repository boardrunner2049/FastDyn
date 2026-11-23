"""Timing arms: interleaved, paired, seeded repetitions (WP13; spec §8, design §5.11, §7).

A *campaign* runs a set of arms ``reps`` times. In every repetition each
input is run once per arm; the order of the inputs and, per input, the order
of the arms are drawn from ``random.Random(seed)`` (recorded in
``campaign.json`` before the first run). Every run gets a fresh WP8 run
directory, so samples from one repetition and input form a *pair* across
arms (the report computes paired speedups from them).

Arm kinds:

* ``nocache``: the cache disabled. Its TOML is ``nocache_config`` or, when
  that is not given, the base TOML with ``[Machine].supercache`` commented
  out. Either way the normalized configuration may differ from the base only
  in ``supercache``/``supercache_flat`` (one mechanism at a time).
* ``empty``: the persistent cache enabled but empty at start (a cold run in a
  fresh run directory; nothing deployed).
* ``deploy``: a ``.sc`` or an artifact directory deployed through the WP8
  manifest oracle (``scopt deploy``) and run with the §4.8 post-run proof. The
  standard deploy arms are ``warm`` (the unoptimized warm cache, the primary
  reference), ``identity`` (identity-packaged artifact), ``layout`` (B2
  layout artifact) and per-pass ablations (``layout-no-<pass>``: an artifact
  optimized with ``--disable <pass>``; its ``parent`` is ``layout``).
  ``source = "auto"`` (warm only) makes the campaign record the warm cache
  itself: one cold run with the campaign's QEMU before timing, frozen into
  ``<out>/frozen/<arm>.sc``.

The spec's remaining primary arms (``semantic``, ``combined``) cannot be
built in B1/B2 (they need ``.sc`` v6); a campaign lists them as
``unsupported`` so that the report shows them as rows, unless an artifact is
given for them (B3/B4).

B3/B4 arms (WP26, design §10.1, §9.4): :data:`B3B4_ARMS` defines the
evaluation arms (``identity-v6``, ``b3-chain``, ``b3-static``, ``b3-spec``,
``b4``, ``semantic``, ``combined`` on top of the B2 ones) with their loader
requirement and pipeline, :data:`FIXTURE_ARMS` the per-fixture differential
arms of §9.4, :func:`b3b4_arm_specs` turns given artifacts into
:class:`ArmSpec` rows (``not_run`` for missing ones), and
:func:`ablation_arms` names the §10.1 ablations. Preflight refuses a
negative-control or test-policy artifact as an arm source
(``negative_control_arm``, ``test_only_arm``; :mod:`scopt.run.policy`) and,
when any arm deploys a ``.sc`` v6, requires the campaign's QEMU to be a
v6-capable binary (``v6_arm_needs_qemu``, ``v6_loader_required``;
:func:`scopt.validate.v6_capable_exes`): every arm of a comparison runs on
that one binary (plan "Runtime rules", R13).

Validity: a run is a timing sample only if it exited 0, did not time out,
left no ``.scm``/``.qprof`` files, printed its BENCH line(s), and -- for
``deploy`` arms -- passed the post-run proof (``PASS``; an
``INVALID_artifact_not_used`` run is never a sample). ``nocache``/``empty``
runs must end ``NOT_PROVED`` (nothing was deployed) and a ``nocache`` run must
not have a cache file.

Inputs: ``inputs`` are files written into the RAM bank ``input_bank`` after
the RAM reset of every run (WP12 fixture contract). ``input_set`` names a split
directory with a WP12 ``MANIFEST.json`` (``fixtures/inputs/<split>/<workload>``):
every listed input is used, its sha256 and size are verified, and the split
label comes from the manifest (a different ``split`` is refused as
``split_mismatch``). A ``warm = auto`` seed run uses ``seed_input`` (default:
the first input); give a train input there when timing held-out or final
inputs, so that the warm cache is not built on an input it is measured on.

QEMU: when the campaign names a QEMU binary, every run's QEMU (from the run's
log or oracle) must have that sha256, else the run is not a sample
(``qemu_mismatch``); a TOML ``qemu_path`` would otherwise silently win over
``FASTDYN_QEMU``. Without one, the distinct QEMU sha256 values seen are
recorded in ``campaign.json`` (``qemu_seen``).

Freezing: before the first run, the sha256 of every arm source, of the base
and nocache TOMLs and of every input is recorded (arm sources are compared with
``expect_sha256`` from a campaign file); after the last run all of them are
recomputed. A change is reported as ``freeze_violation`` (never silently
ignored).

Outputs (``out`` must be fresh)::

    campaign.json   plan, identities, schedule, freeze checks, host load
    runs.jsonl      one summary row per run, appended as runs complete
    ledger.jsonl    stage ``measured`` rows (one per run, one per deploy arm)
    runs/<arm>/r<rep>[-<input>]/   WP8 run directories (run.json, ...)
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import re
import time
import tomllib
import traceback
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .launcher import (FASTDYN_SRC, RunDirError, create_run_dir, launch, write_json)

__all__ = [
    "ArmSpec", "Campaign", "CampaignError", "STANDARD_ARMS", "SPEC_UNSUPPORTED_ARMS",
    "ArmDef", "B3B4_ARMS", "FIXTURE_ARMS", "b3b4_arm_specs", "ablation_arms", "sc_file_version",
    "load_campaign", "campaign_from_args", "derive_nocache_toml", "preflight", "make_schedule",
    "classify_run", "run_campaign", "source_identity", "inputs_from_manifest",
    "resolve_inputs",
]

CAMPAIGN_SCHEMA = "scopt.campaign/1"
RUNROW_SCHEMA = "scopt.runrow/1"
ARM_KINDS = ("nocache", "empty", "deploy")
STANDARD_ARMS = ("nocache", "empty", "warm", "identity", "layout")
SPLITS = ("train", "heldout", "final", "fixed")
# labels a campaign may carry: the WP12 splits, plus "unlabelled" for explicit
# input files given without a split
CAMPAIGN_SPLITS = SPLITS + ("unlabelled",)
# Spec §8 primary arms that B1/B2 cannot build (design §4.9, §9).
SPEC_UNSUPPORTED_ARMS = {
    "semantic": "capability_not_enabled: semantic cross-TB passes are B3/B4 and need .sc v6",
    "combined": "capability_not_enabled: needs the semantic arm (B3/B4, .sc v6)",
}


@dataclass(frozen=True)
class ArmDef:
    """One evaluation arm of design §10.1 / §9.4."""

    name: str
    kind: str          # nocache | empty | deploy
    loader: str        # v5 | v6 | none: the .sc version the arm deploys
    passes: tuple[str, ...] = ()  # pipeline passes (B3/B4 pass names; "b2-layout" for layout)
    content: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


_B3_SPEC = ("specialize-returns", "constant-propagate", "branch-fold", "dead-host-op")
#: design §10.1: all arms on the qint-v6 (gate) binary; baselines re-collected there
B3B4_ARMS: dict[str, ArmDef] = {a.name: a for a in (
    ArmDef("nocache", "nocache", "none", (), "cache disabled"),
    ArmDef("empty", "empty", "none", (), "persistent cache enabled, empty at start"),
    ArmDef("warm", "deploy", "v5", (), "the unoptimized warm cache (primary reference)"),
    ArmDef("identity", "deploy", "v5", ("identity",), "identity pipeline, v5 file"),
    ArmDef("identity-v6", "deploy", "v6", ("identity",),
           "identity pipeline as .sc v6 with an empty ext table (loader overhead)"),
    ArmDef("layout", "deploy", "v5", ("b2-layout",), "B2 pipeline re-run on the new binary"),
    ArmDef("b3-chain", "deploy", "v6", ("chain-returns",), "chain-returns only"),
    ArmDef("b3-static", "deploy", "v6", ("chain-static",),
           "chain-static only (beyond the spec's return scope; reported separately)"),
    ArmDef("b3-spec", "deploy", "v6", _B3_SPEC, "specialize-returns + CP + BF + DHO"),
    ArmDef("b4", "deploy", "v5", ("eliminate-state-sync",), "eliminate-state-sync only (v5)"),
    ArmDef("semantic", "deploy", "v6",
           ("chain-returns", "chain-static") + _B3_SPEC + ("eliminate-state-sync",),
           "b3-chain + b3-static + b3-spec + b4"),
    ArmDef("combined", "deploy", "v6",
           ("chain-returns", "chain-static") + _B3_SPEC + ("eliminate-state-sync", "b2-layout"),
           "semantic + layout"),
)}
#: design §9.4: arms per fixture (each in a fresh run dir, v6 post-run proof)
FIXTURE_ARMS: dict[str, ArmDef] = {a.name: a for a in (
    ArmDef("warm", "deploy", "v5", (), "v5 reference"),
    ArmDef("identity-v6", "deploy", "v6", ("identity",), "v6 header, empty ext table"),
    ArmDef("chain", "deploy", "v6", ("chain-returns", "chain-static"), "GENERAL slots"),
    ArmDef("spec", "deploy", "v6", _B3_SPEC, "SPEC slots + CP/BF/DHO"),
    ArmDef("b4", "deploy", "v5", ("eliminate-state-sync",), "B4 rewrites"),
    ArmDef("combined", "deploy", "v6",
           ("chain-returns", "chain-static") + _B3_SPEC + ("eliminate-state-sync",),
           "chain + spec + b4"),
)}
B4_CLASSES = ("D1", "D2", "D3")


def ablation_arms(table: Mapping[str, ArmDef] = B3B4_ARMS) -> list[ArmDef]:
    """§10.1 ablations: ``semantic-no-<pass>`` for every pass of ``semantic`` and
    the B4 per-class arms ``b4-<class>`` (``--param classes=<class>``)."""
    sem = table["semantic"]
    out = [ArmDef(f"semantic-no-{p}", "deploy", sem.loader,
                  tuple(x for x in sem.passes if x != p), f"semantic without {p}")
           for p in sem.passes]
    out += [ArmDef(f"b4-{c}", "deploy", "v5", ("eliminate-state-sync",),
                   f"eliminate-state-sync with classes={c}") for c in B4_CLASSES]
    return out


def b3b4_arm_specs(artifacts: Mapping[str, str], *, warm: str | None = None,
                   table: Mapping[str, ArmDef] = B3B4_ARMS, reference: str = "warm"
                   ) -> tuple[list["ArmSpec"], dict[str, str]]:
    """:class:`ArmSpec` rows for the arms of ``table`` (default §10.1) from
    ``artifacts`` (arm -> artifact dir or ``.sc``); ``warm`` defaults to
    ``auto``. Arms without an artifact become ``not_run`` (``no_artifact``).
    Ablation names (``semantic-no-<pass>``, ``b4-<class>``) are accepted in
    ``artifacts`` with ``parent`` set."""
    specs: list[ArmSpec] = []
    not_run: dict[str, str] = {}
    abl = {a.name: a for a in ablation_arms(table)} if "semantic" in table else {}
    for name, d in table.items():
        if d.kind in ("nocache", "empty"):
            specs.append(ArmSpec(name, d.kind, role=_role_for(name, d.kind, reference, None)))
            continue
        src = artifacts.get(name) or (warm or "auto" if name == "warm" else None)
        if src is None:
            not_run[name] = "no_artifact: no artifact given for this arm"
            continue
        specs.append(ArmSpec(name, "deploy", src, role=_role_for(name, "deploy", reference,
                                                                  None)))
    for name, src in artifacts.items():
        if name in table:
            continue
        if name in abl:
            parent = "semantic" if name.startswith("semantic-no-") else "b4"
            pss = name.removeprefix("semantic-no-") if parent == "semantic" else None
            specs.append(ArmSpec(name, "deploy", src, parent=parent, ablated_pass=pss,
                                 role="ablation"))
        else:
            raise CampaignError("unknown_arm", f"{name} is not an arm of the table or an "
                                "ablation")
    return specs, not_run


def sc_file_version(src: str | os.PathLike) -> int | None:
    """Header version of an arm source (``.sc`` or artifact dir); None if unreadable."""
    p = Path(src)
    sc = p / "cache.sc" if p.is_dir() else p
    try:
        with open(sc, "rb") as f:
            b = f.read(12)
        return int.from_bytes(b[8:12], "little") if len(b) == 12 else None
    except OSError:
        return None


class CampaignError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# --------------------------------------------------------------------------
# campaign description
# --------------------------------------------------------------------------

@dataclass
class ArmSpec:
    name: str
    kind: str                         # nocache | empty | deploy
    source: str | None = None         # .sc file, artifact dir, or "auto" (deploy only)
    parent: str | None = None         # ablations: the full-pipeline arm
    ablated_pass: str | None = None   # ablations: the disabled pass
    expect_sha256: str | None = None  # frozen sha256 of the .sc (hex or sha256:hex)
    role: str = "candidate"           # baseline | reference | candidate | ablation

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Campaign:
    name: str
    base_config: str
    arms: list[ArmSpec]
    reps: int = 30
    seed: int = 1
    split: str = "fixed"
    inputs: list[str] = field(default_factory=list)
    input_bank: str = "input"
    input_set: str | None = None      # WP12 split dir with MANIFEST.json (fills inputs)
    seed_input: str | None = None     # input for the warm=auto seed run (default inputs[0])
    reference: str = "warm"
    nocache_config: str | None = None
    qemu: str | None = None
    timeout_s: float = 120.0
    quiet: bool = False
    check: bool = True                # deploy --check on each deploy arm's first run
    unsupported: dict[str, str] = field(default_factory=dict)
    not_run: dict[str, str] = field(default_factory=dict)

    def arm(self, name: str) -> ArmSpec:
        for a in self.arms:
            if a.name == name:
                return a
        raise KeyError(name)

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["arms"] = [a.to_json() for a in self.arms]
        return d


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")


def _role_for(name: str, kind: str, reference: str, parent: str | None) -> str:
    if name == reference:
        return "reference"
    if kind in ("nocache", "empty"):
        return "baseline"
    if parent:
        return "ablation"
    return "candidate"


def _default_kind(name: str) -> str:
    return name if name in ("nocache", "empty") else "deploy"


def campaign_from_args(*, base_config: str, arms: Sequence[str], out_name: str,
                       artifacts: Mapping[str, str] | None = None, warm: str | None = None,
                       ablations: Mapping[str, str] | None = None, reps: int = 30,
                       seed: int = 1, split: str | None = "fixed", inputs: Sequence[str] = (),
                       input_bank: str = "input", reference: str = "warm",
                       input_set: str | None = None, seed_input: str | None = None,
                       nocache_config: str | None = None, qemu: str | None = None,
                       timeout_s: float = 120.0, quiet: bool = False,
                       check: bool = True, ablation_parent: str = "layout") -> Campaign:
    """Build a :class:`Campaign` from ``scopt run`` options.

    ``arms`` names arms (``nocache,empty,warm,identity,layout`` or any other
    name given in ``artifacts``). ``warm`` is the warm arm's source (``auto``
    when omitted). An arm whose artifact is not given becomes a ``not_run``
    row (``no_artifact``) instead of failing the campaign. ``ablations`` maps
    a pass name to an artifact built with that pass disabled; each becomes
    the arm ``<ablation_parent>-no-<pass>``.
    """
    artifacts = dict(artifacts or {})
    specs: list[ArmSpec] = []
    not_run: dict[str, str] = {}
    unsupported: dict[str, str] = {}
    for name in arms:
        if name in SPEC_UNSUPPORTED_ARMS and name not in artifacts:
            unsupported[name] = SPEC_UNSUPPORTED_ARMS[name]
            continue
        kind = _default_kind(name)
        src = None
        if kind == "deploy":
            src = warm if name == "warm" else artifacts.get(name)
            if name == "warm" and src is None:
                src = "auto"
            if src is None:
                not_run[name] = "no_artifact: no --artifact given for this arm"
                continue
        specs.append(ArmSpec(name, kind, src, role=_role_for(name, kind, reference, None)))
    for name, src in artifacts.items():
        if name not in arms:
            specs.append(ArmSpec(name, "deploy", src,
                                 role=_role_for(name, "deploy", reference, None)))
    for pss, src in (ablations or {}).items():
        name = f"{ablation_parent}-no-{pss}"
        specs.append(ArmSpec(name, "deploy", src, parent=ablation_parent, ablated_pass=pss,
                             role="ablation"))
    for name, why in SPEC_UNSUPPORTED_ARMS.items():
        if name not in artifacts:
            unsupported.setdefault(name, why)
    return Campaign(name=out_name, base_config=base_config, arms=specs, reps=reps, seed=seed,
                    split=split, inputs=list(inputs), input_bank=input_bank,
                    input_set=input_set, seed_input=seed_input, reference=reference, nocache_config=nocache_config, qemu=qemu,
                    timeout_s=timeout_s, quiet=quiet, check=check, unsupported=unsupported,
                    not_run=not_run)


def load_campaign(path: str | os.PathLike, **overrides: Any) -> Campaign:
    """Read a campaign TOML (relative paths resolve against the file's directory)::

        [campaign]
        name = "b2-freertos-final"
        base_config = "configs/compute-sc.toml"
        nocache_config = "configs/compute-nocache.toml"   # optional
        qemu = "/data/.../wt/qemu-int/build/qemu-system-arm"  # optional
        reps = 30
        seed = 7
        split = "final"
        inputs = ["inputs/final/x/final-1.bin", ...]       # optional
        input_bank = "input"
        input_set = "fixtures/inputs/final/freertos-mps2-compute"  # optional (MANIFEST.json)
        seed_input = "fixtures/inputs/train/freertos-mps2-compute/train-1.bin"  # optional
        reference = "warm"
        timeout_s = 300

        [[arm]]
        name = "warm"
        kind = "deploy"            # nocache | empty | deploy (default from the name)
        source = "store/snapshots/S/cache.sc"
        sha256 = "…"                # frozen; the campaign refuses a mismatch

        [[arm]]
        name = "layout-no-cluster-hot"
        source = "store/artifacts/…"
        parent = "layout"
        ablated_pass = "cluster-hot"

        [unsupported]
        semantic = "capability_not_enabled"
    """
    path = Path(path).resolve()
    doc = tomllib.loads(path.read_text())
    c = doc.get("campaign", {})
    base = path.parent

    def res(p: str | None) -> str | None:
        if p is None or p == "auto":
            return p
        q = Path(p)
        return str(q if q.is_absolute() else (base / q).resolve())

    reference = c.get("reference", "warm")
    arms = []
    for a in doc.get("arm", []):
        name = a["name"]
        kind = a.get("kind", _default_kind(name))
        arms.append(ArmSpec(name, kind, res(a.get("source")), a.get("parent"),
                            a.get("ablated_pass"), a.get("sha256"),
                            a.get("role") or _role_for(name, kind, reference, a.get("parent"))))
    unsupported = dict(SPEC_UNSUPPORTED_ARMS)
    unsupported.update(doc.get("unsupported", {}))
    for a in arms:
        unsupported.pop(a.name, None)
    camp = Campaign(
        name=c.get("name", path.stem), base_config=res(c["base_config"]), arms=arms,
        reps=int(c.get("reps", 30)), seed=int(c.get("seed", 1)), split=c.get("split"),
        inputs=[res(i) for i in c.get("inputs", [])], input_bank=c.get("input_bank", "input"),
        input_set=res(c.get("input_set")), seed_input=res(c.get("seed_input")),
        reference=reference, nocache_config=res(c.get("nocache_config")),
        qemu=res(c.get("qemu")), timeout_s=float(c.get("timeout_s", 120.0)),
        quiet=bool(c.get("quiet", False)), check=bool(c.get("check", True)),
        unsupported=unsupported, not_run=dict(doc.get("not_run", {})))
    for k, v in overrides.items():
        if v is not None:
            setattr(camp, k, v)
    return camp


# --------------------------------------------------------------------------
# configuration checks
# --------------------------------------------------------------------------

_SC_LINE = re.compile(r"^(\s*)(supercache)(\s*=)")


def derive_nocache_toml(text: str) -> str:
    """``text`` with the ``[Machine].supercache`` line commented out (nothing else)."""
    out = []
    table = None
    for line in text.splitlines(keepends=True):
        s = line.strip()
        m = re.match(r"^\[\[?\s*([^\]]+?)\s*\]\]?", s)
        if m and not s.startswith("#"):
            table = m.group(1).strip()
        if table == "Machine" and _SC_LINE.match(line):
            line = _SC_LINE.sub(r"\1# scopt-nocache: \2\3", line, count=1)
        out.append(line)
    new = "".join(out)
    if (tomllib.loads(new).get("Machine") or {}).get("supercache") not in (None, ""):
        raise CampaignError("nocache_derive_failed", "could not remove [Machine].supercache")
    return new


def _flatten(v: Any, path: tuple = ()) -> dict[tuple, Any]:
    if isinstance(v, dict):
        out: dict[tuple, Any] = {}
        for k, x in v.items():
            out.update(_flatten(x, path + (k,)))
        return out
    if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
        out = {}
        for i, x in enumerate(v):
            out.update(_flatten(x, path + (i,)))
        return out
    return {path: v}


def config_diff(a_text: str, b_text: str) -> list[str]:
    """Dotted keys whose values differ between two TOML texts."""
    a = _flatten(tomllib.loads(a_text))
    b = _flatten(tomllib.loads(b_text))
    return sorted(".".join(map(str, k)) for k in set(a) | set(b) if a.get(k) != b.get(k))


_NOCACHE_ALLOWED = {"Machine.supercache", "Machine.supercache_flat"}
_DEBUG_FLAGS = ("enable_gdb", "stop_on_start", "launch_gdb")


def production_problems(cfg: Mapping[str, Any]) -> list[str]:
    """Settings that change TB keys or timing and are refused in production arms (§9)."""
    out = []
    for path, v in _flatten(dict(cfg)).items():
        key = path[-1] if path else ""
        if key in _DEBUG_FLAGS and v is True:
            out.append(f"debug_option:{'.'.join(map(str, path))}")
        if key == "log_options" and isinstance(v, str):
            items = {x.strip().lower() for x in v.split(",")}
            if "nochain" in items:
                out.append(f"nochain:{'.'.join(map(str, path))}")
        if key == "supercache_flat" and v is True:
            out.append("unsupported_flat")
    return out


def _hex(s: str | None) -> str | None:
    if s is None:
        return None
    return s.split(":", 1)[1] if s.startswith("sha256:") else s


def source_identity(src: str) -> dict[str, Any]:
    """What a deploy arm runs: the ``.sc`` path, its sha256 and its artifact id."""
    from ..formats.sc_v5 import sha256_sparse

    p = Path(src)
    info: dict[str, Any] = {"source": str(p)}
    if p.is_dir():
        sc = p / "cache.sc"
        info["kind"] = "artifact_dir"
        man = p / "MANIFEST.sha256"
        if man.is_file():
            from ..ids import artifact_id
            info["artifact_id"] = artifact_id(man.read_bytes())
        else:
            info["artifact_id"] = p.name
        side = p / "cache.sc.scopt.json"
        if side.is_file():
            try:
                sd = json.loads(side.read_text())
                for k in ("input_snapshot_id", "output_snapshot_id", "loader_requirement"):
                    if k in sd:
                        info[k] = sd[k]
                info["pipeline"] = [{"pass": e.get("pass"), "version": e.get("version"),
                                     "outcome": e.get("outcome")}
                                    for e in sd.get("pipeline", []) if isinstance(e, dict)]
            except (ValueError, OSError) as e:
                info["sidecar_error"] = str(e)
    else:
        sc = p
        info["kind"] = "sc_file"
    info["sc"] = str(sc)
    if not sc.is_file():
        raise CampaignError("arm_source_missing", f"{sc} does not exist")
    info["sha256"] = sha256_sparse(sc)
    if info["kind"] == "sc_file":
        info["artifact_id"] = "sc:sha256:" + info["sha256"]
    st = os.stat(sc)
    info["size"] = st.st_size
    info["allocated_bytes"] = st.st_blocks * 512
    return info


def _file_sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def inputs_from_manifest(d: str | os.PathLike) -> tuple[list[str], str | None, dict[str, Any]]:
    """Inputs of a WP12 split directory: ``(paths, split, manifest identity)``.

    Every input listed in ``MANIFEST.json`` must exist with the recorded size
    and sha256 (else ``input_manifest_mismatch``). The split is the inputs'
    common ``profile`` when it is a split name, else the directory's parent name
    (``inputs/<split>/<workload>``) when that is one, else ``None``.
    """
    d = Path(d).resolve()
    man = d / "MANIFEST.json"
    try:
        doc = json.loads(man.read_text())
        entries = doc["inputs"]
        if not isinstance(entries, dict) or not entries:
            raise ValueError("no inputs")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise CampaignError("input_manifest_unreadable", f"{man}: {e}") from e
    paths: list[str] = []
    profiles = set()
    for name in sorted(entries):
        e = entries[name]
        p = d / e.get("file", f"{name}.bin")
        if not p.is_file():
            raise CampaignError("input_manifest_mismatch", f"{p} is missing")
        sha = _file_sha(str(p))
        if sha != _hex(e.get("sha256")) or (e.get("size") is not None
                                            and os.path.getsize(p) != e["size"]):
            raise CampaignError("input_manifest_mismatch",
                                f"{p}: sha256 {sha} size {os.path.getsize(p)} != manifest")
        paths.append(str(p))
        profiles.add(e.get("profile"))
    split = None
    if len(profiles) == 1 and next(iter(profiles)) in SPLITS:
        split = next(iter(profiles))
    elif d.parent.name in SPLITS:
        split = d.parent.name
    return paths, split, {"path": str(d), "manifest": str(man),
                          "manifest_sha256": _file_sha(str(man)), "inputs": sorted(entries),
                          "split": split}


def resolve_inputs(c: Campaign) -> dict[str, Any] | None:
    """Fill ``c.inputs``/``c.split`` from ``c.input_set``; returns the manifest identity."""
    info = None
    if c.input_set:
        if c.inputs:
            raise CampaignError("inputs_conflict", "give either input_set or inputs, not both")
        paths, split, info = inputs_from_manifest(c.input_set)
        if c.split not in (None, "fixed") and split is not None and c.split != split:
            raise CampaignError("split_mismatch",
                                f"split {c.split} but {c.input_set} holds {split} inputs")
        c.inputs = paths
        if split is not None:
            c.split = split
    if c.split is None:
        c.split = "fixed"
    if c.inputs and c.split == "fixed":
        c.split = "unlabelled"
    # A split label without inputs (fixed-input workload) is kept; the report
    # labels such a held-out/final evaluation as a re-run of the same input.
    return info


def _check_input_bank(c: Campaign, cfg: Mapping[str, Any], where: str) -> None:
    """When inputs (or a seed input) are requested, ``cfg`` must have a file-backed
    ``[Memory]`` bank with id ``c.input_bank`` large enough for every input
    (``input_bank_missing`` / ``input_too_large``, refused before any run)."""
    from .toml_rewrite import memory_banks

    files = list(c.inputs) + ([c.seed_input] if c.seed_input else [])
    if not files:
        return
    try:
        banks = {b.id: b for b in memory_banks(cfg)}
    except (ValueError, TypeError) as e:
        raise CampaignError("input_bank_missing", f"{where}: unreadable [Memory]: {e}") from e
    b = banks.get(c.input_bank)
    if b is None or not b.is_file or not b.memory_file:
        raise CampaignError("input_bank_missing",
                            f"inputs requested but {where} has no file-backed [Memory] bank "
                            f"with id {c.input_bank!r} (banks: {sorted(banks)})")
    for f in files:
        try:
            n = os.path.getsize(f)
        except OSError as e:
            raise CampaignError("input_missing", f"{f}: {e}") from e
        if b.size is not None and n > b.size:
            raise CampaignError("input_too_large",
                                f"{f}: {n} bytes > bank {c.input_bank!r} size {b.size} "
                                f"({where})")


def _check_qemu_path(c: Campaign, cfg: Mapping[str, Any], where: str) -> None:
    """A TOML ``[Machine].qemu_path`` wins over ``FASTDYN_QEMU`` inside FastDyn, so a
    campaign QEMU that differs from it would make every run ``qemu_mismatch``
    (and a warm=auto seed cache would be recorded with the wrong binary):
    refused up front (``qemu_path_conflict``)."""
    if not c.qemu:
        return
    qp = (cfg.get("Machine") or {}).get("qemu_path")
    if not qp:
        return
    q = Path(qp)
    if not q.is_absolute():
        q = FASTDYN_SRC / q  # FastDyn runs with cwd=src/fastdyn
    if not q.is_file() or _file_sha(str(q)) != _file_sha(c.qemu):
        raise CampaignError("qemu_path_conflict",
                            f"{where} pins [Machine].qemu_path={qp}, which is not the campaign "
                            f"QEMU {c.qemu} (sha256 differs or the file is missing)")


def preflight(c: Campaign, out: Path) -> dict[str, Any]:
    """Refuse a campaign that cannot give valid samples; returns the identities."""
    input_set = resolve_inputs(c)
    if not _NAME_RE.match(c.name):
        raise CampaignError("bad_campaign_name", c.name)
    if c.reps < 1:
        raise CampaignError("bad_reps", str(c.reps))
    if c.split not in CAMPAIGN_SPLITS:
        raise CampaignError("bad_split", f"{c.split} not in {CAMPAIGN_SPLITS}")
    if c.seed_input and not Path(c.seed_input).is_file():
        raise CampaignError("seed_input_missing", c.seed_input)
    names = [a.name for a in c.arms]
    if len(set(names)) != len(names):
        raise CampaignError("duplicate_arm", ", ".join(names))
    for a in c.arms:
        if not _NAME_RE.match(a.name):
            raise CampaignError("bad_arm_name", a.name)
        if a.kind not in ARM_KINDS:
            raise CampaignError("bad_arm_kind", f"{a.name}: {a.kind}")
        if a.kind == "deploy" and not a.source:
            raise CampaignError("arm_source_missing", a.name)
        if a.source == "auto" and a.kind != "deploy":
            raise CampaignError("bad_arm_source", f"{a.name}: auto is only for deploy arms")
        if a.parent and a.parent not in names:
            raise CampaignError("ablation_parent_missing", f"{a.name}: parent {a.parent}")
    if c.reference not in names:
        raise CampaignError("reference_arm_missing", c.reference)
    try:
        base_text = Path(c.base_config).read_text()
        base_cfg = tomllib.loads(base_text)
    except (OSError, ValueError) as e:
        raise CampaignError("base_config_unreadable", f"{c.base_config}: {e}") from e
    probs = production_problems(base_cfg)
    if probs:
        raise CampaignError("refused_production_config", ", ".join(probs))
    if not (base_cfg.get("Machine") or {}).get("supercache"):
        if any(a.kind != "nocache" for a in c.arms):
            raise CampaignError("base_has_no_supercache", c.base_config)
    _check_input_bank(c, base_cfg, c.base_config)
    _check_qemu_path(c, base_cfg, c.base_config)
    ids: dict[str, Any] = {
        "base_config": {"path": c.base_config, "sha256": _file_sha(c.base_config)},
        "inputs": [{"path": i, "sha256": _file_sha(i), "size": os.path.getsize(i)}
                   for i in c.inputs],
        "input_set": input_set,
        "arms": {},
    }
    if c.seed_input:
        ids["seed_input"] = {"path": c.seed_input, "sha256": _file_sha(c.seed_input),
                             "also_timed": c.seed_input in c.inputs}
    if any(a.kind == "nocache" for a in c.arms):
        if c.nocache_config:
            try:
                noc_text = Path(c.nocache_config).read_text()
                tomllib.loads(noc_text)
            except (OSError, ValueError) as e:
                raise CampaignError("nocache_config_unreadable",
                                    f"{c.nocache_config}: {e}") from e
            noc_path = c.nocache_config
        else:
            noc_text = derive_nocache_toml(base_text)
            (out / "configs").mkdir(parents=True, exist_ok=True)
            noc_path = str(out / "configs" / "nocache.toml")
            Path(noc_path).write_text(noc_text)
        extra = set(config_diff(base_text, noc_text)) - _NOCACHE_ALLOWED
        if extra:
            raise CampaignError("nocache_config_differs",
                                "nocache TOML differs from the base beyond supercache: "
                                + ", ".join(sorted(extra)))
        noc_cfg = tomllib.loads(noc_text)
        if (noc_cfg.get("Machine") or {}).get("supercache"):
            raise CampaignError("nocache_config_has_supercache", noc_path)
        _check_input_bank(c, noc_cfg, noc_path)
        _check_qemu_path(c, noc_cfg, noc_path)
        ids["nocache_config"] = {"path": noc_path, "sha256": _file_sha(noc_path),
                                 "derived": not c.nocache_config,
                                 "diff_vs_base": config_diff(base_text, noc_text)}
    if c.qemu:
        ids["qemu"] = {"path": str(Path(c.qemu).resolve()),
                       "sha256": "sha256:" + _file_sha(c.qemu)}
    from .policy import artifact_tags

    v6_arms = []
    for a in c.arms:
        if a.kind != "deploy" or a.source == "auto":
            continue
        tags = artifact_tags(a.source)
        if tags["negative_control"]:
            raise CampaignError("negative_control_arm",
                                f"{a.name}: {a.source} is a negative control (never timed)")
        if tags["test_policies"]:
            raise CampaignError("test_only_arm", f"{a.name}: {a.source} was built under test "
                                f"policies {tags['test_policies']} (never timed)")
        info = source_identity(a.source)
        if a.expect_sha256 and _hex(a.expect_sha256) != info["sha256"]:
            raise CampaignError("freeze_violation",
                                f"{a.name}: sha256 {info['sha256']} != frozen {a.expect_sha256}")
        info["file_version"] = sc_file_version(a.source)
        if info["file_version"] == 6:
            v6_arms.append(a.name)
        ids["arms"][a.name] = info
    if v6_arms:
        from ..validate import v6_capable_exes

        if not c.qemu:
            raise CampaignError("v6_arm_needs_qemu", "arms " + ", ".join(v6_arms) + " deploy "
                                ".sc v6: name the v6-capable QEMU (every arm runs on it)")
        if _file_sha(c.qemu) not in v6_capable_exes():
            raise CampaignError("v6_loader_required", f"{c.qemu} is not a v6-capable binary "
                                "(runs/wp22/gate-binary.json or $SCOPT_V6_EXES); arms "
                                + ", ".join(v6_arms))
        ids["v6_arms"] = v6_arms
    return ids


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------

def make_schedule(arms: Sequence[str], reps: int, n_inputs: int, seed: int
                  ) -> list[dict[str, Any]]:
    """Interleaved, seeded order: ``[{rep, input, arm, pos}]`` in execution order.

    Per repetition the input order is shuffled; per (repetition, input) the
    arm order is shuffled. ``n_inputs == 0`` means one fixed input (index None).
    """
    rng = random.Random(seed)
    out = []
    pos = 0
    for rep in range(reps):
        ins: list[int | None] = list(range(n_inputs)) if n_inputs else [None]
        rng.shuffle(ins)
        for i in ins:
            order = list(arms)
            rng.shuffle(order)
            for a in order:
                out.append({"rep": rep, "input": i, "arm": a, "pos": pos})
                pos += 1
    return out


# --------------------------------------------------------------------------
# one run
# --------------------------------------------------------------------------

def classify_run(kind: str, doc: Mapping[str, Any], *, cache_present: bool | None = None,
                 expect_qemu_sha256: str | None = None) -> tuple[bool, str]:
    """``(valid, reason)`` for a WP8 ``run.json`` of an arm of ``kind``."""
    if expect_qemu_sha256 and doc.get("qemu_sha256") and \
            _hex(doc["qemu_sha256"]) != _hex(expect_qemu_sha256):
        return False, f"qemu_mismatch:{doc['qemu_sha256']}"
    if doc.get("timed_out"):
        return False, "timeout"
    if doc.get("exit_code") != 0:
        return False, f"exit_code:{doc.get('exit_code')}"
    if doc.get("stray_capture_files"):
        return False, "stray_capture_files"
    v = doc.get("verdict")
    if kind == "deploy":
        if v != "PASS":
            return False, str(v)
    else:
        if v != "NOT_PROVED":
            return False, f"unexpected_verdict:{v}"
        if kind == "nocache" and cache_present:
            return False, "nocache_arm_has_cache"
    if not doc.get("bench"):
        return False, "no_bench"
    return True, "ok"


def _bench_ns(doc: Mapping[str, Any]) -> int | None:
    b = doc.get("bench") or []
    if not b:
        return None
    return sum(int(x["elapsed_ns"]) for x in b)


def _sha_lines(lines: Iterable[str] | None) -> str | None:
    if lines is None:
        return None
    return "sha256:" + hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


def _cache_after(rd_root: Path) -> dict[str, Any] | None:
    """Record count and sizes of the run directory's .sc after the run (empty arm)."""
    from ..formats.sc_v5 import HEADER_SIZE, ScHeader

    cache = rd_root / "cache"
    scs = sorted(cache.glob("*.sc")) if cache.is_dir() else []
    if not scs:
        return None
    p = scs[0]
    try:
        with open(p, "rb") as f:
            h = ScHeader.from_bytes(f.read(HEADER_SIZE))
        st = os.stat(p)
        return {"path": str(p), "record_count": h.record_count, "size": st.st_size,
                "allocated_bytes": st.st_blocks * 512, "code_size": h.code_size}
    except Exception as e:  # noqa: BLE001 - a truncated header is data, not a crash
        return {"path": str(p), "error": f"{type(e).__name__}: {e}"}


def _tag(step: Mapping[str, Any]) -> str:
    inp = step["input"]
    return f"r{step['rep']:03d}" + ("" if inp is None else f"-i{inp}")


def _run_one(c: Campaign, arm: ArmSpec, src: str | None, step: Mapping[str, Any], out: Path,
             first_of_arm: bool, nocache_config: str | None,
             expect_qemu_sha256: str | None = None) -> dict[str, Any]:
    from .deploy import DeployError, deploy_new

    rep, inp = step["rep"], step["input"]
    tag = _tag(step)
    root = out / "runs" / arm.name / tag
    label = f"{c.name}/{arm.name}/{tag}"
    inputs = {c.input_bank: c.inputs[inp]} if inp is not None else None
    row: dict[str, Any] = {
        "schema": RUNROW_SCHEMA, "campaign": c.name, "arm": arm.name, "kind": arm.kind,
        "rep": rep, "input": inp, "input_name": None if inp is None else Path(c.inputs[inp]).stem,
        "split": c.split, "pos": step["pos"], "run_dir": str(root), "started": time.time(),
    }
    try:
        if arm.kind == "deploy":
            try:
                rd, dep = deploy_new(src, c.base_config, root, check=c.check and first_of_arm,
                                     quiet=c.quiet, qemu=c.qemu, timeout_s=c.timeout_s,
                                     label=label)
                row["deploy_seconds"] = dep.get("deploy_seconds")
                row["deployed_records"] = dep["state"]["n"]
            except DeployError as e:
                row.update(valid=False, verdict=f"INVALID_deploy_refused:{e.code}",
                           reason=f"deploy_refused:{e.code}", detail=e.detail[:400])
                return row
            doc = launch(rd, timeout_s=c.timeout_s, inputs=inputs)
        else:
            cfg = c.base_config if arm.kind == "empty" else nocache_config
            rd = create_run_dir(cfg, root, quiet=c.quiet, qemu=c.qemu, label=label)
            doc = launch(rd, timeout_s=c.timeout_s, prove=False, inputs=inputs)
    except (RunDirError, OSError) as e:
        row.update(valid=False, verdict=f"ERROR:{getattr(e, 'code', type(e).__name__)}",
                   reason="harness_error", detail=str(e)[:400])
        return row
    after = _cache_after(rd.root)
    valid, reason = classify_run(arm.kind, doc, cache_present=after is not None,
                                 expect_qemu_sha256=expect_qemu_sha256)
    t = doc.get("timings") or {}
    cpu = None
    if t.get("children_user_s") is not None and t.get("children_sys_s") is not None:
        cpu = t["children_user_s"] + t["children_sys_s"]
    row.update({
        "valid": valid, "reason": reason, "verdict": doc.get("verdict"),
        "exit_code": doc.get("exit_code"), "timed_out": doc.get("timed_out"),
        "bench": doc.get("bench"), "bench_ns": _bench_ns(doc),
        "wall_s": t.get("wall_s"), "cpu_s": cpu, "maxrss_kb": t.get("children_maxrss_kb"),
        "appended": doc.get("appended"), "qemu": doc.get("qemu"),
        "qemu_sha256": doc.get("qemu_sha256"),
        "guest_norm_sha256": _sha_lines(doc.get("guest_norm")),
        "guest_norm": doc.get("guest_norm"),
        "output_norm_sha256": doc.get("output_norm_sha256"),
        "cache_after": after, "inputs": doc.get("inputs"),
        "run_json": str(rd.root / "run.json"),
    })
    return row


# --------------------------------------------------------------------------
# warm auto
# --------------------------------------------------------------------------

def _record_warm(c: Campaign, arm: ArmSpec, out: Path) -> dict[str, Any]:
    """``source = "auto"``: one cold run with the campaign QEMU, frozen as the warm cache."""
    from ..formats.sc_v5 import read_sc, sparse_copy

    root = out / "seed" / f"{arm.name}-cold"
    rd = create_run_dir(c.base_config, root, quiet=c.quiet, qemu=c.qemu,
                        label=f"{c.name}/seed/{arm.name}")
    seed_input = c.seed_input or (c.inputs[0] if c.inputs else None)
    inputs = {c.input_bank: seed_input} if seed_input else None
    doc = launch(rd, timeout_s=c.timeout_s, prove=False, inputs=inputs)
    want = _hex(_file_sha(c.qemu)) if c.qemu else None
    got = _hex(doc.get("qemu_sha256"))
    if want and got and got != want:
        # the cache would carry another binary's runtime_signature: every warm run
        # would rebuild it. Refuse before any timing run.
        raise CampaignError("warm_seed_qemu_mismatch",
                            f"seed run used QEMU {doc.get('qemu')} (sha256 {got}), campaign "
                            f"QEMU {c.qemu} has sha256 {want}; see {root}")
    if doc.get("exit_code") != 0 or rd.cache is None or not rd.cache.is_file():
        raise CampaignError("warm_seed_failed",
                            f"cold seed run exit {doc.get('exit_code')}; see {root}")
    if doc.get("stray_capture_files"):
        raise CampaignError("warm_seed_failed", f"stray capture files in {root}")
    sc = read_sc(rd.cache, versions=(7,))
    if not sc.loadable or not sc.valid_slots:
        raise CampaignError("warm_seed_failed", f"{rd.cache}: no valid records")
    frozen = out / "frozen"
    frozen.mkdir(parents=True, exist_ok=True)
    dst = frozen / f"{arm.name}.sc"
    sparse_copy(rd.cache, dst)
    os.chmod(dst, 0o444)
    return {"seed_run_dir": str(root), "seed_input": seed_input,
            "seed_input_timed": seed_input in c.inputs if seed_input else None,
            "seed_qemu_sha256": doc.get("qemu_sha256"),
            "seed_qemu_check": ("match" if want and got else
                                "unknown_seed_qemu" if want else "no_campaign_qemu"),
            "frozen": str(dst), "records": len(sc.valid_slots), "seed_exit": doc["exit_code"]}


# --------------------------------------------------------------------------
# campaign
# --------------------------------------------------------------------------

def _host() -> dict[str, Any]:
    try:
        la = list(os.getloadavg())
    except OSError:  # pragma: no cover
        la = None
    return {"loadavg": la, "nproc": os.cpu_count(), "uname": list(platform.uname()),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}


def _ledger_rows(c: Campaign, rows: Sequence[Mapping[str, Any]], ids: Mapping[str, Any],
                 campaign_json: Path) -> Any:
    from ..ledger import Ledger

    led = Ledger()
    for r in rows:
        tag = Path(r["run_dir"]).name
        led.emit("run", f"{c.name}/{r['arm']}/{tag}", "measured",
                 "measured" if r.get("valid") else str(r.get("verdict") or "invalid"),
                 str(r.get("reason") or ""), r.get("run_json") or r["run_dir"])
    for a in c.arms:
        if a.kind != "deploy":
            continue
        info = ids["arms"].get(a.name) or {}
        aid = info.get("artifact_id") or f"arm:{a.name}"
        if info.get("kind") == "sc_file":
            # a bare .sc is not a published artifact; keep arms with equal bytes apart
            aid = f"sc:{a.name}:sha256:{info['sha256']}"
        mine = [r for r in rows if r["arm"] == a.name]
        nv = sum(1 for r in mine if r.get("valid"))
        verdict = "measured" if nv else "not_measured"
        led.emit("artifact", aid, "measured", verdict,
                 f"arm={a.name} campaign={c.name} split={c.split} valid={nv}/{len(mine)}",
                 str(campaign_json))
    return led


def run_campaign(c: Campaign, out: str | os.PathLike, *, log=print,
                 dry_run: bool = False) -> dict[str, Any]:
    """Run campaign ``c`` into the fresh directory ``out``; returns campaign.json's content."""
    if log is None:
        def log(_msg: str) -> None:
            return None
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise CampaignError("out_not_fresh", f"{out} exists and is not empty")
    out.mkdir(parents=True, exist_ok=True)
    ids = preflight(c, out)
    nocache_path = (ids.get("nocache_config") or {}).get("path")
    doc: dict[str, Any] = {
        "schema": CAMPAIGN_SCHEMA, "campaign": c.to_json(), "identities": ids,
        "fastdyn_src": str(FASTDYN_SRC), "host_start": _host(), "status": "running",
        "warm_auto": {},
    }
    cj = out / "campaign.json"
    write_json(cj, doc)
    try:
        return _run_campaign_body(c, out, doc, cj, ids, nocache_path, log=log,
                                  dry_run=dry_run)
    except BaseException as e:
        # campaign.json is never left "running": a refusal after preflight (e.g.
        # the warm seed run) or a crash is recorded with its code
        doc["status"] = "refused" if isinstance(e, CampaignError) else "error"
        doc["error"] = {"code": getattr(e, "code", type(e).__name__),
                        "detail": getattr(e, "detail", str(e))[:2000]}
        doc["host_end"] = _host()
        try:
            write_json(cj, doc)
        except OSError:  # pragma: no cover - nothing more we can do
            pass
        raise


def _run_campaign_body(c: Campaign, out: Path, doc: dict[str, Any], cj: Path,
                       ids: dict[str, Any], nocache_path: str | None, *, log,
                       dry_run: bool) -> dict[str, Any]:
    sources: dict[str, str | None] = {}
    for a in c.arms:
        if a.kind != "deploy":
            sources[a.name] = None
        elif a.source == "auto":
            if dry_run:
                sources[a.name] = "auto"
                continue
            log(f"[{c.name}] recording the warm cache for arm {a.name} (one cold run)")
            w = _record_warm(c, a, out)
            doc["warm_auto"][a.name] = w
            info = source_identity(w["frozen"])
            ids["arms"][a.name] = info
            sources[a.name] = w["frozen"]
        else:
            sources[a.name] = a.source
    sched = make_schedule([a.name for a in c.arms], c.reps, len(c.inputs), c.seed)
    doc["schedule"] = sched
    write_json(cj, doc)
    if dry_run:
        doc["status"] = "dry_run"
        write_json(cj, doc)
        return doc
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    expect_qemu = (ids.get("qemu") or {}).get("sha256")
    t0 = time.monotonic()
    with open(out / "runs.jsonl", "a", encoding="utf-8") as rf:
        for k, step in enumerate(sched):
            arm = c.arm(step["arm"])
            try:
                row = _run_one(c, arm, sources[arm.name], step, out, arm.name not in seen,
                               nocache_path, expect_qemu)
            except Exception as e:  # never abort a campaign on one run
                row = {"schema": RUNROW_SCHEMA, "campaign": c.name, "arm": arm.name,
                       "kind": arm.kind, "rep": step["rep"], "input": step["input"],
                       "pos": step["pos"], "split": c.split,
                       "run_dir": str(out / "runs" / arm.name / _tag(step)),
                       "valid": False, "verdict": f"ERROR:{type(e).__name__}",
                       "reason": "harness_exception", "detail": traceback.format_exc()[-800:]}
            seen.add(arm.name)
            row["finished"] = time.time()
            rows.append(row)
            rf.write(json.dumps(row, sort_keys=True) + "\n")
            rf.flush()
            if log:
                b = row.get("bench_ns")
                log(f"[{c.name}] {k + 1}/{len(sched)} rep={step['rep']} input={step['input']} "
                    f"arm={arm.name}: {row.get('verdict')} "
                    f"bench={'-' if b is None else f'{b / 1e3:.1f}us'} "
                    f"{'' if row.get('valid') else '(not a sample: ' + str(row.get('reason')) + ')'}")
    # freeze check after timing: arm sources, TOMLs, inputs and the QEMU binary
    after = {}
    for name, info in ids["arms"].items():
        try:
            now = source_identity(info["source"])["sha256"]
        except CampaignError as e:
            now = f"missing:{e.detail}"
        after[name] = {"before": info["sha256"], "after": now, "ok": now == info["sha256"]}
    files = [("config:base", ids["base_config"])]
    if ids.get("nocache_config"):
        files.append(("config:nocache", ids["nocache_config"]))
    files += [(f"input:{Path(i['path']).name}", i) for i in ids["inputs"]]
    if ids.get("seed_input"):
        files.append(("seed_input", ids["seed_input"]))
    if ids.get("qemu"):
        files.append(("qemu", {"path": ids["qemu"]["path"], "sha256": _hex(ids["qemu"]["sha256"])}))
    for name, info in files:
        try:
            now = _file_sha(info["path"])
        except OSError as e:
            now = f"missing:{e}"
        after[name] = {"before": info["sha256"], "after": now, "ok": now == info["sha256"]}
    doc["freeze_check"] = after
    doc["qemu_seen"] = sorted({str(r.get("qemu_sha256")) for r in rows
                               if r.get("exit_code") is not None})
    doc["freeze_ok"] = all(v["ok"] for v in after.values())
    doc["host_end"] = _host()
    doc["duration_s"] = time.monotonic() - t0
    doc["n_runs"] = len(rows)
    doc["n_valid"] = sum(1 for r in rows if r.get("valid"))
    doc["status"] = "done" if doc["freeze_ok"] else "freeze_violation"
    write_json(cj, doc)
    led = _ledger_rows(c, rows, ids, cj)
    led.write(out / "ledger.jsonl")
    return doc
