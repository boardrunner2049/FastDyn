"""ELF image access with explicit file-backed and zero-fill semantics."""
from __future__ import annotations

from dataclasses import dataclass

from .errors import ImageAddressError


@dataclass(frozen=True)
class ImageChunk:
    address: int
    data: bytes
    source: str  # file | zero-fill


@dataclass(frozen=True)
class ImageRead:
    address: int
    size: int
    data: bytes
    chunks: tuple[ImageChunk, ...]

    @property
    def source(self) -> str:
        sources = {chunk.source for chunk in self.chunks}
        return next(iter(sources)) if len(sources) == 1 else "mixed"


class ImageAccess:
    """Address-based reads from the immutable load image of one ELF."""

    def __init__(self, elf):
        self._elf = elf

    def read(self, address: int, size: int) -> ImageRead:
        if isinstance(address, bool) or isinstance(size, bool) or address < 0 or size < 0:
            raise ImageAddressError("image address and size must be non-negative integers")
        if size == 0:
            return ImageRead(address, 0, b"", ())
        cursor, end = address, address + size
        chunks: list[ImageChunk] = []
        segments = sorted(
            (segment for segment in self._elf.iter_segments()
             if segment["p_type"] == "PT_LOAD" and int(segment["p_memsz"])),
            key=lambda segment: int(segment["p_vaddr"]),
        )
        while cursor < end:
            segment = next((candidate for candidate in segments
                            if int(candidate["p_vaddr"]) <= cursor
                            < int(candidate["p_vaddr"]) + int(candidate["p_memsz"])), None)
            if segment is None:
                raise ImageAddressError(
                    f"unmapped ELF image read at {cursor:#x} (request {address:#x}+{size})"
                )
            base = int(segment["p_vaddr"])
            file_end = base + int(segment["p_filesz"])
            memory_end = base + int(segment["p_memsz"])
            if cursor < file_end:
                chunk_end = min(end, file_end)
                offset = cursor - base
                data = bytes(segment.data()[offset:offset + chunk_end - cursor])
                if len(data) != chunk_end - cursor:
                    raise ImageAddressError(f"truncated file-backed ELF data at {cursor:#x}")
                source = "file"
            else:
                chunk_end = min(end, memory_end)
                data = bytes(chunk_end - cursor)
                source = "zero-fill"
            chunks.append(ImageChunk(cursor, data, source))
            cursor = chunk_end
        return ImageRead(address, size, b"".join(chunk.data for chunk in chunks), tuple(chunks))

    def read_bytes(self, address: int, size: int) -> bytes:
        return self.read(address, size).data
