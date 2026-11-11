"""Lazy immutable firmware analysis session."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from elftools.elf.elffile import ELFFile

from .constants import ConstantPropagation
from .decode import Decoder
from .dwarf import DwarfQueries
from .image import ImageAccess
from .symbols import SymbolIndex


class AnalysisSession:
    """Run-shareable access to immutable host analysis of one firmware image."""

    def __init__(self, firmware: str | Path, *, architecture: str,
                 instruction_mode: str | None = None):
        self.path = Path(firmware).expanduser().resolve()
        self.architecture = architecture.lower()
        self.instruction_mode = instruction_mode or self._default_mode(self.architecture)
        self._stream = None
        self._elf = None
        self._image = None
        self._symbols = None
        self._dwarf = None
        self._decoder = None
        self._constant_propagation = None

    @staticmethod
    def _default_mode(architecture: str) -> str:
        # An architecture family alone does not select an instruction mode.
        return "default"

    @classmethod
    def open(cls, firmware: str | Path, *, architecture: str,
             instruction_mode: str | None = None) -> "AnalysisSession":
        return cls(firmware, architecture=architecture, instruction_mode=instruction_mode)

    @property
    def elf(self):
        if self._elf is None:
            self._stream = BytesIO(self.path.read_bytes())
            self._elf = ELFFile(self._stream)
        return self._elf

    @property
    def image(self) -> ImageAccess:
        if self._image is None:
            self._image = ImageAccess(self.elf)
        return self._image

    @property
    def symbols(self) -> SymbolIndex:
        if self._symbols is None:
            self._symbols = SymbolIndex(
                self.elf,
                normalize_thumb_functions=(self.architecture == "arm"
                                           and self.instruction_mode == "thumb-mclass"),
            )
        return self._symbols

    @property
    def dwarf(self) -> DwarfQueries:
        if self._dwarf is None:
            self._dwarf = DwarfQueries(self.elf)
        return self._dwarf

    @property
    def decoder(self) -> Decoder:
        if self._decoder is None:
            self._decoder = Decoder(self.elf, self.architecture, self.instruction_mode)
        return self._decoder

    @property
    def constant_propagation(self) -> ConstantPropagation:
        """Reusable, cached known-register/effective-address analysis."""
        if self._constant_propagation is None:
            entry = int(self.elf.header["e_entry"]) & ~1
            self._constant_propagation = ConstantPropagation(
                self.decoder.executable_regions(),
                architecture=self.architecture,
                instruction_mode=self.instruction_mode,
                entry_points=(entry,),
            )
        return self._constant_propagation

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
        self._stream = self._elf = self._image = self._symbols = None
        self._dwarf = self._decoder = self._constant_propagation = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class AnalysisSessionPool:
    """Preparation-run scoped reuse keyed by immutable image and target facts."""

    def __init__(self):
        self._sessions: dict[tuple[Path, str, str], AnalysisSession] = {}

    def get(self, firmware: str | Path, *, architecture: str,
            instruction_mode: str | None = None) -> AnalysisSession:
        path = Path(firmware).expanduser().resolve()
        architecture = architecture.lower()
        mode = instruction_mode or AnalysisSession._default_mode(architecture)
        key = (path, architecture, mode)
        if key not in self._sessions:
            self._sessions[key] = AnalysisSession(path, architecture=architecture,
                                                  instruction_mode=mode)
        return self._sessions[key]

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()
