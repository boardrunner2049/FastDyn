"""Test-only artifact tags and test policies (B3/B4 design §9.5 F-B3-15/17, §9.4 O4; WP26).

Two kinds of artifact must never reach a store's ``artifacts/`` directory, a
timing arm or a production deploy:

* **negative controls** (:mod:`scopt.run.negative`): mutants packaged with
  V12-V17 bypassed, used only as dynamic negative controls (O4). Sidecar key
  ``x_negative_control`` (an object) and the marker file
  ``NEGATIVE_CONTROL.json`` in the artifact directory.
* **test-policy artifacts**: built under a test policy that waives a
  production admission rule. The only policy is ``allow_ram_resident``
  (F-B3-15 mps2 alias, F-B3-17 SMC in RAM): RAM-resident code is excluded in
  production (``ram_resident``, design §5.6 "SMC while a member runs", §14),
  and these two fixtures exist to exercise exactly that code under the v6
  loader. Sidecar key ``x_test_only = {"policies": [...], "waived": [...]}``.

Who checks what:

* :func:`scopt.formats.artifact.publish` refuses both (``negative_control`` /
  ``test_only_artifact``) through :func:`publish_refusal`, so ``scopt
  publish`` and every packager that publishes through it refuse them;
* :func:`scopt.run.deploy.deploy` refuses both unless the caller passes
  ``allow_negative_control=True`` / ``test_policies={...}`` naming every policy
  the artifact was built under (``negative_control_artifact``,
  ``test_only_artifact:<policy>``, ``unknown_test_policy:<policy>``);
* campaign preflight (:mod:`scopt.run.arms`) refuses both as arm sources.

The admission side of ``allow_ram_resident`` (WP21's manager) uses
:func:`waive` on the §3.1 reason lists and records what it waived with
:func:`tag_sidecar`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = ["NEGATIVE_TAG", "NEGATIVE_MARKER", "TEST_ONLY_TAG", "TEST_POLICIES",
           "ALLOW_RAM_RESIDENT", "artifact_tags", "publish_refusal", "deploy_refusals",
           "waive", "tag_sidecar"]

NEGATIVE_TAG = "x_negative_control"
NEGATIVE_MARKER = "NEGATIVE_CONTROL.json"
TEST_ONLY_TAG = "x_test_only"
ALLOW_RAM_RESIDENT = "allow_ram_resident"
#: test policy -> admission reason codes it waives
TEST_POLICIES: dict[str, tuple[str, ...]] = {ALLOW_RAM_RESIDENT: ("ram_resident",)}
SIDECAR = "cache.sc.scopt.json"


def _sidecar(path: Path, name: str = SIDECAR) -> tuple[dict[str, Any] | None, str | None]:
    p = path / name
    if not p.is_file():
        return None, None
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        return None, f"sidecar unreadable: {e}"
    return (doc if isinstance(doc, dict) else None), (None if isinstance(doc, dict)
                                                      else "sidecar is not an object")


def artifact_tags(path: str | os.PathLike) -> dict[str, Any]:
    """The test-only tags of an artifact directory or of a bare ``.sc`` file.

    ``{"negative_control": bool, "test_policies": [...], "sidecar_error": str|None}``.
    A directory with the negative-control marker file counts as a negative
    control even if its sidecar lost the tag (fail closed). A bare file
    ``X`` (WP26 review: ``deploy(<neg_dir>/cache.sc)`` must not bypass the
    tags) is judged by its directory's marker file and its own sidecar
    ``X.scopt.json`` (``cache.sc.scopt.json`` for an artifact's ``cache.sc``).

    Known limit (WP26 review 2, minor 6): the tags live next to the file
    (marker file, sidecars), not in the ``.sc`` bytes. A negative control's
    ``cache.sc`` copied to another directory is an untagged bare ``.sc``:
    it deploys like any bare file (``identity.checked = false`` in
    ``deploy.json``). The tags guard against mistakes inside the artifact
    store and the run tree; they are not a boundary against copying. Gate
    reports must name artifacts by directory (sidecar + manifest), never by
    a bare ``.sc``."""
    p = Path(path)
    out: dict[str, Any] = {"negative_control": False, "test_policies": [],
                           "sidecar_error": None}
    if p.is_dir():
        side, err = _sidecar(p)
    elif p.is_file():
        side, err = _sidecar(p.parent, p.name + ".scopt.json")
        p = p.parent
    else:
        return out
    out["sidecar_error"] = err
    if (p / NEGATIVE_MARKER).exists() or (side is not None and side.get(NEGATIVE_TAG)):
        out["negative_control"] = True
    t = (side or {}).get(TEST_ONLY_TAG)
    if t:
        pols = t.get("policies") if isinstance(t, Mapping) else None
        # a malformed tag is still a test-only artifact: name it so nothing accepts it
        out["test_policies"] = sorted(str(x) for x in pols) if isinstance(pols, list) and pols \
            else ["<malformed>"]
    return out


def publish_refusal(path: str | os.PathLike) -> tuple[str, str] | None:
    """``(code, detail)`` when the directory must not be published, else None."""
    t = artifact_tags(path)
    if t["negative_control"]:
        return ("negative_control", "negative-control artifacts (V12-V17 bypassed, "
                "scopt.run.negative) are never publishable")
    if t["test_policies"]:
        return ("test_only_artifact", "built under test policies "
                + ", ".join(t["test_policies"]) + "; never publishable")
    return None


def deploy_refusals(path: str | os.PathLike, *, allow_negative_control: bool = False,
                    test_policies: Iterable[str] = ()) -> list[str]:
    """Deploy reason codes for the artifact's test-only tags (empty: no objection)."""
    t = artifact_tags(path)
    allowed = set(test_policies)
    out = []
    if t["negative_control"] and not allow_negative_control:
        out.append("negative_control_artifact")
    for p in t["test_policies"]:
        if p not in TEST_POLICIES:
            out.append(f"unknown_test_policy:{p}")
        elif p not in allowed:
            out.append(f"test_only_artifact:{p}")
    return out


def waive(reasons: Iterable[Any], policies: Iterable[str]) -> tuple[list[Any], list[str]]:
    """Drop the admission reasons the test ``policies`` waive.

    ``reasons`` are codes or ``(code, detail)`` pairs (both shapes of
    :mod:`scopt.analysis.return_edges`). Returns ``(kept, waived_codes)``.
    An unknown policy raises ``ValueError`` (fail closed)."""
    codes: set[str] = set()
    for p in policies:
        if p not in TEST_POLICIES:
            raise ValueError(f"unknown test policy {p!r}")
        codes.update(TEST_POLICIES[p])
    kept, waived = [], []
    for r in reasons:
        c = r[0] if isinstance(r, tuple) else r
        if c in codes:
            waived.append(c)
        else:
            kept.append(r)
    return kept, sorted(set(waived))


def tag_sidecar(sidecar: dict[str, Any], policies: Iterable[str],
                waived: Iterable[str] = ()) -> dict[str, Any]:
    """Add ``x_test_only`` to a sidecar (in place; returns it)."""
    pols = sorted(set(policies))
    for p in pols:
        if p not in TEST_POLICIES:
            raise ValueError(f"unknown test policy {p!r}")
    if pols:
        sidecar[TEST_ONLY_TAG] = {"policies": pols, "waived": sorted(set(waived))}
    return sidecar
