"""Conservative host-side candidate memory-access analysis.

The analysis deliberately starts from the correctness baseline: every ARM
Thumb instruction that Capstone exposes with a memory operand is a candidate.
Consumers can later add proof-based pruning, but must never drop an access
merely because its possible pointer flow is unknown.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastdyn.host import AnalysisSession


@dataclass(frozen=True)
class CandidateAccess:
    pc: int


class ObjectAccessAnalysis:
    """Shared conservative access-plan producer for object-aware plugins."""

    def __init__(self, binary: Path | AnalysisSession, architecture: str | None = None):
        self.session = (binary if isinstance(binary, AnalysisSession)
                        else AnalysisSession.open(
                            binary, architecture=architecture or "",
                            instruction_mode=("thumb-mclass"
                                              if (architecture or "").lower() == "arm"
                                              else "default"),
                        ))

    def candidate_accesses(self) -> tuple[CandidateAccess, ...]:
        candidates: set[int] = set()
        for instruction in self.session.decoder.instructions():
            if instruction.memory_transfer:
                candidates.add(instruction.address)
        return tuple(CandidateAccess(pc) for pc in sorted(candidates))
