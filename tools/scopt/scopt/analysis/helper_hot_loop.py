"""Training-replay call counts and cyclic regions of the observed TB graph."""

import json
from collections import defaultdict
from pathlib import Path

from ..canon import sha256_id
from ..formats.enums import SiteKind
from ..formats.profile_v1 import METHOD


def exact_loop_profile(view, snapshot_id, phase):
    if view is None:
        return False
    data = view.profile
    phase_id = data.phase_id(phase)
    exact_source = data.snapshot_id == snapshot_id
    target = getattr(view, "target", None)
    if target is not None and target.kind == "helper_splice":
        exact_source = (target.snapshot_id == snapshot_id
                        and target.input_snapshot_ids == (data.snapshot_id,))
    return (exact_source and data.complete and data.method == METHOD
            and not data.collection["overflow"] and data.collection["chain_disabled"]
            and phase_id is not None and data.phase_reached(phase_id))


def load_site_counts(path, snapshot, expected_digest):
    raw = Path(path).read_bytes()
    if sha256_id(raw) != expected_digest:
        raise ValueError("helper profile differs from the selected training run")
    doc = json.loads(raw)
    if (doc.get("schema") != "scopt.helper-call-profile/1"
            or doc.get("complete") is not True or doc.get("snapshot_id") != snapshot.snapshot_id):
        raise ValueError("a complete helper profile of this snapshot is required")
    counts = {}
    for row in doc["counts"]:
        key = (row["unit"], row["call_off"])
        if (type(key[1]) is not int or type(row["count"]) is not int
                or row["count"] < 0 or key in counts):
            raise ValueError("invalid or duplicate helper call count")
        counts[key] = row["count"]
    expected = {(u.uid, s.insn_off) for u in snapshot.units() for s in u.sites
                if s.kind in (SiteKind.HELPER_CALL, SiteKind.LDST_HELPER_CALL)}
    if counts.keys() != expected:
        raise ValueError("helper profile site set differs from snapshot")
    return counts


#: Edge kinds that are ordinary control transfers out of a TB body.
LOOP_TRANSFER_KINDS = frozenset({"chain0", "chain1", "lookup_hit", "lookup_miss"})
#: Firmware hot-loop also follows ``exit_null``: FreeRTOS critical sections
#: (``MSR BASEPRI``) and handler-mode returns end their TBs with
#: ``exit_tb(0)``, which otherwise hides loops through them.  The caller must
#: drop the ``exit_null`` edges that are not control flow (see
#: :func:`real_exit_null`).  ``exit_requested`` is never a transfer: the
#: source TB was aborted at its poll before its body ran, and the re-dispatch
#: A -> A would create a false self-loop.
FIRMWARE_LOOP_KINDS = LOOP_TRANSFER_KINDS | {"exit_null"}


def real_exit_null(unit_of):
    """Predicate over profile edges: whether an unflagged ``exit_null`` edge is
    control flow inside one execution mode.

    ``unit_of(uid)`` returns the snapshot unit (or None).  A handler-mode
    source (``cs_base`` HANDLER) ends a ``BX``/``POP {pc}`` with ``exit_tb(0)``
    because the target may be an ``EXC_RETURN`` value; the edge's destination
    is then whatever code the exception return resumes, not a successor, and
    following it can join ISR code and the interrupted task into a phantom
    loop.  An edge that changes mode without an exception or IRQ flag is not
    a transfer either.  Exception entries and exits themselves carry the
    ``irq``/``exception``/``longjmp`` flags and are excluded before this.
    """
    from .return_edges import HANDLER_BIT

    modes: dict[str, bool | None] = {}

    def handler(uid):
        if uid not in modes:
            unit = unit_of(uid)
            modes[uid] = None if unit is None else bool(unit.key.cs_base & HANDLER_BIT)
        return modes[uid]

    def accept(edge):
        if edge["kind"] != "exit_null":
            return True
        src, dst = handler(edge["src"]), handler(edge["dst"])
        return src is False and dst is False
    return accept


def observed_loop_units(edges, kinds=LOOP_TRANSFER_KINDS, accept=None):
    """Find SCCs of observed control transfers without using the Python call stack.

    Only unflagged exact edges of ``kinds`` count; ``accept(edge)`` can veto
    more (firmware hot-loop passes :func:`real_exit_null`).
    """
    forward, reverse = defaultdict(set), defaultdict(set)
    loops = set()
    for e in edges:
        src, dst = e["src"], e["dst"]
        if (not src or not dst or e["count"] <= 0 or e.get("flags")
                or e.get("status") != "exact"
                or e["kind"] not in kinds or (accept is not None and not accept(e))):
            continue
        if src == dst:
            loops.add(src)
        forward[src].add(dst)
        reverse[dst].add(src)
    visited, finished = set(), []
    for root in tuple(forward):
        if root in visited:
            continue
        stack = [(root, False)]
        while stack:
            node, returning = stack.pop()
            if returning:
                finished.append(node)
            elif node not in visited:
                visited.add(node)
                stack.append((node, True))
                stack.extend((child, False) for child in forward.get(node, ()))
    for root in reversed(finished):
        if root not in visited:
            continue
        component, stack = set(), [root]
        while stack:
            node = stack.pop()
            if node in visited:
                visited.remove(node)
                component.add(node)
                stack.extend(reverse.get(node, ()))
        if len(component) > 1:
            loops.update(component)
    return loops
