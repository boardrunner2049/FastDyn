"""Per-run TOML: line-level rewrite of the path keys, verified with ``tomllib`` (§4.8).

FastDyn hashes the *whole* TOML text into the supercache fingerprint (G13), so
a per-run TOML must be a byte-for-byte copy of the base TOML except for the
lines that hold the run-directory paths. This module rewrites exactly these
keys and nothing else:

* ``[Machine].supercache`` (only when the base TOML has one; a no-cache arm
  stays without a cache);
* ``[Machine].qmp_socket`` (inserted right after the ``[Machine]`` header when
  the base TOML has none, because FastDyn's default ``/tmp/qmp.sock`` would be
  shared between runs);
* ``memory_file`` of every ``[Memory.<bank>]`` / ``[[Memory.<bank>]]`` table with
  the ``file`` backend (inserted after the table header when missing).

After the substitution both texts are parsed with ``tomllib`` and every leaf is
compared: any difference outside the keys above raises
:class:`TomlRewriteError`. ``supercache_flat = true`` is refused
(``unsupported_flat``): B1/B2 only support the region-tree lookup.

:func:`normalized_config` / :func:`normalized_config_sha256` give the
path-free view of a configuration (§4.2): ``supercache``, ``qmp_socket``,
``log_file`` and every ``memory_file`` removed, then canonical JSON. Every
per-run TOML of one base TOML has the base's normalized hash.
"""

from __future__ import annotations

import datetime as _dt
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..canon import canonical_json, sha256_id

__all__ = [
    "TomlRewriteError", "RewriteResult", "rewrite_toml", "verify_rewrite",
    "memory_banks", "MemoryBank", "normalized_config", "normalized_config_sha256",
    "toml_basic_string", "parse_memory_size", "PATH_KEYS",
]

# Keys whose values are run-directory paths (normalized away for identities).
PATH_KEYS = ("supercache", "memory_file", "qmp_socket", "log_file")


class TomlRewriteError(ValueError):
    """The base TOML cannot be rewritten safely; ``code`` is a stable reason."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def toml_basic_string(s: str) -> str:
    """``s`` as a TOML basic string (double quotes, escapes as needed)."""
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


_SIZE_RE = re.compile(r"^(0x[0-9a-fA-F]+|\d+)\s*([kmgtpeKMGTPE]?)(?:i?[bB])?$")
_SIZE_MUL = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40, "P": 1 << 50,
             "E": 1 << 60}


def parse_memory_size(v: Any) -> int:
    """FastDyn's ``memory_size`` syntax (``qemu_target._parse_memory_size``)."""
    if isinstance(v, bool):
        raise ValueError(f"bad memory size {v!r}")
    if isinstance(v, int):
        return v
    m = _SIZE_RE.match(str(v).strip())
    if not m:
        raise ValueError(f"bad memory size {v!r}")
    n = int(m.group(1), 0)
    return n * _SIZE_MUL[m.group(2).upper()]


def _parse_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    try:
        return int(str(v).strip(), 0)
    except ValueError:
        return None


@dataclass(frozen=True)
class MemoryBank:
    """One ``[Memory.<name>]`` (or one element of ``[[Memory.<name>]]``)."""

    name: str
    index: int | None  # position in an array of tables, else None
    id: str
    base: int | None
    size: int | None
    type: str
    backend: str
    share: bool
    memory_file: str | None

    @property
    def is_file(self) -> bool:
        return self.backend.lower() == "file"

    def canonical(self) -> dict[str, Any]:
        """Path-free view for ``memory_map_hash`` (no host pointers, no paths)."""
        return {
            "id": self.id,
            "base": None if self.base is None else f"0x{self.base:x}",
            "size": self.size,
            "type": self.type,
            "backend": self.backend.lower(),
            "share": bool(self.share),
        }


def memory_banks(cfg: Mapping[str, Any]) -> list[MemoryBank]:
    """Memory banks of a parsed TOML, in file order."""
    out = []
    mem = cfg.get("Memory") or {}
    for name, val in mem.items():
        tables = [(None, val)] if isinstance(val, Mapping) else \
            [(i, t) for i, t in enumerate(val)] if isinstance(val, list) else []
        for idx, t in tables:
            if not isinstance(t, Mapping):
                continue
            size = t.get("memory_size")
            out.append(MemoryBank(
                name=name, index=idx, id=str(t.get("id", name)),
                base=_parse_int(t.get("base_address")),
                size=parse_memory_size(size) if size is not None else None,
                type=str(t.get("memory_type", "SRAM")),
                backend=str(t.get("backend", "file")),
                share=bool(t.get("share", True)),
                memory_file=t.get("memory_file"),
            ))
    return out


# --------------------------------------------------------------------------
# normalized config
# --------------------------------------------------------------------------

def _jsonable(v: Any) -> Any:
    if isinstance(v, Mapping):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return {"x_float": repr(v)}
        return v
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return {"x_datetime": v.isoformat()}
    return v


def normalized_config(text_or_cfg: str | Mapping[str, Any]) -> dict[str, Any]:
    """The parsed configuration with every run-directory path key removed."""
    cfg = tomllib.loads(text_or_cfg) if isinstance(text_or_cfg, str) else text_or_cfg
    cfg = _jsonable(cfg)
    m = cfg.get("Machine")
    if isinstance(m, dict):
        for k in ("supercache", "qmp_socket", "log_file"):
            m.pop(k, None)
    mem = cfg.get("Memory")
    if isinstance(mem, dict):
        for val in mem.values():
            for t in ([val] if isinstance(val, dict) else val if isinstance(val, list) else []):
                if isinstance(t, dict):
                    t.pop("memory_file", None)
    return cfg


def normalized_config_sha256(text_or_cfg: str | Mapping[str, Any]) -> str:
    """``sha256:`` of the canonical JSON of :func:`normalized_config`."""
    return sha256_id(canonical_json(normalized_config(text_or_cfg)))


# --------------------------------------------------------------------------
# line-level rewrite
# --------------------------------------------------------------------------

_HEADER_RE = re.compile(r"^\s*(\[\[?)\s*([^\]]+?)\s*(\]\]?)\s*(#.*)?$")
_KEY_RE = re.compile(
    r"""^(?P<indent>\s*)(?P<key>[A-Za-z0-9_-]+)(?P<eq>\s*=\s*)"""
    r"""(?P<val>"(?:[^"\\]|\\.)*"|'[^']*')(?P<rest>\s*(?:\#.*)?)$""")
_ANYKEY_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def _split_dotted(name: str) -> tuple[str, ...]:
    parts = []
    for p in re.findall(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'|[^.]+', name):
        p = p.strip()
        if p[:1] in "\"'":
            p = p[1:-1]
        parts.append(p)
    return tuple(parts)


@dataclass
class _Table:
    path: tuple[str, ...]
    array: bool
    index: int  # occurrence index for arrays of tables
    header_line: int
    keys: dict[str, int] = field(default_factory=dict)  # key -> line index


def _scan(lines: list[str]) -> list[_Table]:
    """Tables with the line index of each top-level key (multi-line strings skipped)."""
    tables = [_Table((), False, 0, -1)]
    counts: dict[tuple[str, ...], int] = {}
    in_ml: str | None = None
    for i, line in enumerate(lines):
        if in_ml is not None:
            if line.count(in_ml) % 2 == 1:
                in_ml = None
            continue
        m = _HEADER_RE.match(line)
        if m and m.group(1) == m.group(3)[::-1].replace("]", "["):
            path = _split_dotted(m.group(2))
            array = m.group(1) == "[["
            idx = counts.get(path, 0)
            counts[path] = idx + 1
            tables.append(_Table(path, array, idx if array else 0, i))
            continue
        km = _ANYKEY_RE.match(line)
        if km:
            tables[-1].keys.setdefault(km.group(1), i)
        for q in ('"""', "'''"):
            if line.count(q) % 2 == 1:
                in_ml = q
                break
    return tables


@dataclass(frozen=True)
class RewriteResult:
    text: str
    changed: tuple[str, ...]  # dotted key paths that were substituted or inserted
    memory_files: Mapping[str, str]  # bank id -> path
    supercache: str | None
    qmp_socket: str


def _replace_value(line: str, value: str) -> str:
    m = _KEY_RE.match(line.rstrip("\n"))
    if not m:
        raise TomlRewriteError("unsupported_value", f"cannot rewrite line {line.rstrip()!r}: "
                               "only single-line string values are supported")
    nl = "\n" if line.endswith("\n") else ""
    return f"{m.group('indent')}{m.group('key')}{m.group('eq')}{toml_basic_string(value)}" \
           f"{m.group('rest')}{nl}"


def rewrite_toml(text: str, *, supercache: str | None,
                 memory_file: Callable[[MemoryBank], str] | Mapping[str, str],
                 qmp_socket: str) -> RewriteResult:
    """Rewrite the path keys of ``text`` (see the module docstring).

    ``supercache`` is the new cache path; it is ignored (and must be None) for
    a base TOML without ``[Machine].supercache``. ``memory_file`` maps a bank
    (or its id) to its new RAM file. Raises :class:`TomlRewriteError`.
    """
    try:
        old = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise TomlRewriteError("toml_parse", str(e)) from e
    machine = old.get("Machine")
    if not isinstance(machine, Mapping):
        raise TomlRewriteError("no_machine_table", "[Machine] is missing")
    if machine.get("supercache_flat", False) is not False:
        raise TomlRewriteError("unsupported_flat", "supercache_flat must be false")
    has_cache = machine.get("supercache") not in (None, "", False)
    if has_cache and supercache is None:
        raise TomlRewriteError("missing_cache_path", "base TOML has a supercache; give its path")

    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    tables = _scan(lines)
    edits: dict[int, str] = {}
    inserts: dict[int, list[str]] = {}
    changed: list[str] = []

    def table(path: tuple[str, ...], index: int = 0) -> _Table | None:
        for t in tables:
            if t.path == path and t.index == index:
                return t
        return None

    mt = table(("Machine",))
    if mt is None:
        raise TomlRewriteError("no_machine_table",
                               "[Machine] must be a table header line (dotted keys unsupported)")
    if has_cache:
        if "supercache" not in mt.keys:
            raise TomlRewriteError("unsupported_layout", "[Machine].supercache not found as a line")
        edits[mt.keys["supercache"]] = _replace_value(lines[mt.keys["supercache"]], supercache)
        changed.append("Machine.supercache")
    if "qmp_socket" in mt.keys:
        edits[mt.keys["qmp_socket"]] = _replace_value(lines[mt.keys["qmp_socket"]], qmp_socket)
    else:
        inserts.setdefault(mt.header_line, []).append(f"qmp_socket = {toml_basic_string(qmp_socket)}\n")
    changed.append("Machine.qmp_socket")

    mem_files: dict[str, str] = {}
    for bank in memory_banks(old):
        if not bank.is_file:
            continue
        new = memory_file[bank.id] if isinstance(memory_file, Mapping) else memory_file(bank)
        mem_files[bank.id] = new
        t = table(("Memory", bank.name), bank.index or 0)
        if t is None:
            raise TomlRewriteError("unsupported_layout",
                                   f"Memory.{bank.name} is not written as a table header")
        if "memory_file" in t.keys:
            edits[t.keys["memory_file"]] = _replace_value(lines[t.keys["memory_file"]], new)
        else:
            inserts.setdefault(t.header_line, []).append(f"memory_file = {toml_basic_string(new)}\n")
        changed.append(f"Memory.{bank.name}" + ("" if bank.index is None else f"[{bank.index}]")
                       + ".memory_file")

    out = []
    for i, line in enumerate(lines):
        out.append(edits.get(i, line))
        out.extend(inserts.get(i, ()))
    new_text = "".join(out)
    verify_rewrite(text, new_text)
    return RewriteResult(new_text, tuple(changed), mem_files,
                         supercache if has_cache else None, qmp_socket)


def _flatten(v: Any, path: tuple = ()) -> dict[tuple, Any]:
    out: dict[tuple, Any] = {}
    if isinstance(v, Mapping):
        if not v:
            out[path] = {}
        for k, x in v.items():
            out.update(_flatten(x, path + (k,)))
    elif isinstance(v, list) and v and all(isinstance(x, Mapping) for x in v):
        for i, x in enumerate(v):
            out.update(_flatten(x, path + (i,)))
    else:
        out[path] = v
    return out


def _allowed(path: tuple) -> bool:
    if path in (("Machine", "supercache"), ("Machine", "qmp_socket")):
        return True
    if len(path) >= 3 and path[0] == "Memory" and path[-1] == "memory_file":
        return len(path) == 3 or (len(path) == 4 and isinstance(path[2], int))
    return False


def verify_rewrite(old_text: str, new_text: str) -> list[tuple]:
    """Parse both texts; raise unless they differ only at the allowed path keys.

    Returns the differing key paths.
    """
    try:
        a = _flatten(tomllib.loads(old_text))
        b = _flatten(tomllib.loads(new_text))
    except tomllib.TOMLDecodeError as e:
        raise TomlRewriteError("rewrite_broke_toml", str(e)) from e
    diff = sorted((p for p in set(a) | set(b) if a.get(p, _MISSING) != b.get(p, _MISSING)),
                  key=repr)
    bad = [p for p in diff if not _allowed(p)]
    if bad:
        raise TomlRewriteError("rewrite_changed_other_keys",
                               ", ".join(".".join(map(str, p)) for p in bad))
    return diff


_MISSING = object()
