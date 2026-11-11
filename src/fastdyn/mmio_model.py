"""Bounded Clang/LLVM analysis for automatically generated MMIO operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from typing import Any, Iterable, Optional, Sequence


class MmioModelError(RuntimeError):
    pass


Expr = tuple[Any, ...]


@dataclass(frozen=True)
class Operation:
    selector_kind: str
    selector_offset: int
    selector_width: int
    selector_value: int
    expression: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True)
class Analysis:
    read_function: str
    state_size: int
    operations: tuple[Operation, ...]
    declined_paths: int
    llvm_ir: str


@dataclass
class _Function:
    name: str
    args: list[str]
    blocks: dict[str, list[str]]


_VAR = r"%[-A-Za-z$._0-9]+"


def resolve_clang() -> str:
    requested = shutil.which("clang-18") or shutil.which("clang")
    if not requested:
        raise MmioModelError("Clang is required for --mmio-opt (tried clang-18 and clang)")
    return requested


def compile_ir(source: Path, *, cflags: Sequence[str], include_flags: Sequence[str],
               clang: Optional[str] = None) -> str:
    compiler = clang or resolve_clang()
    command = [compiler, "-O1", "-S", "-emit-llvm", "-fno-discard-value-names",
               *cflags, *include_flags, str(source.resolve()), "-o", "-"]
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode:
        raise MmioModelError("LLVM analysis compile failed:\n" + proc.stderr.strip())
    return proc.stdout


def analyze_ir(ir: str) -> Analysis:
    functions = _parse_functions(ir)
    read_name = _descriptor_callback(ir, 3)
    init_name = _descriptor_callback(ir, 2)
    if read_name not in functions:
        raise MmioModelError("fastdyn_model.read could not be resolved in LLVM IR")
    state_size = _allocation_size(functions.get(init_name))
    if state_size <= 0:
        raise MmioModelError("could not prove the size of the model state allocation")
    tbaa = _tbaa_offsets(ir)
    mutable_offsets = _mutable_state_offsets(functions, init_name, tbaa)
    paths = _execute(functions[read_name], tbaa)

    operations: list[Operation] = []
    declined = 0
    for conditions, result, effectful in paths:
        if effectful or result is None:
            declined += 1
            continue
        selector = _selector(conditions, mutable_offsets)
        postfix = _postfix(result, state_size)
        if selector is None or postfix is None or len(postfix) > 16:
            declined += 1
            continue
        operations.append(Operation(*selector, tuple(postfix)))
    operations = list(dict.fromkeys(operations))
    if not operations:
        raise MmioModelError("analysis found no pure, bounded MMIO read operations")
    return Analysis(read_name, state_size, tuple(operations), declined, ir)


def analyze_source(source: Path, *, cflags: Sequence[str], include_flags: Sequence[str],
                   clang: Optional[str] = None) -> Analysis:
    return analyze_ir(compile_ir(source, cflags=cflags, include_flags=include_flags,
                                 clang=clang))


def generated_c(analysis: Analysis) -> str:
    lines = [
        "/* Generated from host LLVM IR by fastdyn build-model --mmio-opt. */",
        "#include <fastdyn/device_model.h>", "",
        "static const FdMmioOperation fastdyn_generated_mmio_ops[] = {",
    ]
    for op in analysis.operations:
        lines.extend([
            "    {",
            f"        .selector_kind = {op.selector_kind},",
            f"        .selector_width = {op.selector_width},",
            f"        .expr_count = {len(op.expression)},",
            f"        .selector_state_offset = {op.selector_offset},",
            f"        .selector_value = {op.selector_value},",
            "        .expr = {",
        ])
        for opcode, offset, value in op.expression:
            lines.append(f"            {{ .opcode = {opcode}, .state_offset = {offset}, .value = UINT64_C({value & ((1 << 64) - 1)}) }},")
        lines.extend(["        },", "    },"])
    lines.extend([
        "};", "",
        "const FdMmioModel fastdyn_mmio_model = {",
        "    .abi = FASTDYN_MMIO_MODEL_ABI_VERSION,",
        "    .size = sizeof(FdMmioModel),",
        f"    .state_size = {analysis.state_size},",
        f"    .operation_count = {len(analysis.operations)},",
        "    .operations = fastdyn_generated_mmio_ops,", "};", "",
    ])
    return "\n".join(lines)


def _descriptor_callback(ir: str, index: int) -> str:
    match = re.search(r"@fastdyn_model\s*=.*?%struct\.FdModel\s*\{([^\n]+)\}", ir)
    if not match:
        raise MmioModelError("fastdyn_model descriptor is not a constant LLVM initializer")
    callbacks = re.findall(r"ptr\s+@([-A-Za-z$._0-9]+)|ptr\s+null", match.group(1))
    names = [next((part for part in item if part), "") if isinstance(item, tuple) else item
             for item in callbacks]
    # Callback ptrs start after abi and size; index is descriptor field index.
    callback_index = index - 2
    if callback_index >= len(names) or not names[callback_index]:
        raise MmioModelError(f"fastdyn_model callback field {index} is not set")
    return names[callback_index]


def _parse_functions(ir: str) -> dict[str, _Function]:
    lines = ir.splitlines()
    functions: dict[str, _Function] = {}
    i = 0
    while i < len(lines):
        header = re.match(r"define\b.*?@([-A-Za-z$._0-9]+)\((.*?)\).*\{", lines[i])
        if not header:
            i += 1
            continue
        name = header.group(1)
        args = re.findall(_VAR, header.group(2))
        i += 1
        body: list[str] = []
        while i < len(lines) and lines[i].strip() != "}":
            body.append(lines[i])
            i += 1
        blocks: dict[str, list[str]] = {"entry": []}
        current = "entry"
        j = 0
        while j < len(body):
            raw = body[j].strip()
            label = re.match(r"([-A-Za-z$._0-9]+):", raw)
            if label:
                current = label.group(1)
                blocks.setdefault(current, [])
                j += 1
                continue
            if raw.startswith("switch ") and "]" not in raw:
                while j + 1 < len(body):
                    j += 1
                    raw += " " + body[j].strip()
                    if "]" in body[j]:
                        break
            if raw and not raw.startswith(";"):
                blocks[current].append(raw)
            j += 1
        functions[name] = _Function(name, args, blocks)
        i += 1
    return functions


def _tbaa_offsets(ir: str) -> dict[str, int]:
    offsets: dict[str, int] = {}
    for line in ir.splitlines():
        match = re.match(r"(!\d+)\s*=\s*!\{.*i64\s+(-?\d+)\s*\}\s*$", line)
        if match:
            offsets[match.group(1)] = int(match.group(2))
    return offsets


def _allocation_size(init: Optional[_Function]) -> int:
    if init is None:
        return 0
    text = "\n".join(line for block in init.blocks.values() for line in block)
    sizes = [int(x) for x in re.findall(r"dereferenceable_or_null\((\d+)\)", text)]
    for a, b in re.findall(r"@calloc\(i64(?:\s+\w+)*\s+(\d+),\s+i64(?:\s+\w+)*\s+(\d+)\)", text):
        sizes.append(int(a) * int(b))
    for value in re.findall(r"@malloc\(i64(?:\s+\w+)*\s+(\d+)\)", text):
        sizes.append(int(value))
    return max(sizes, default=0)


def _pointer_provenance(fn: _Function) -> set[str]:
    state = {fn.args[0]} if fn.args else set()
    changed = True
    while changed:
        changed = False
        for line in (line for block in fn.blocks.values() for line in block):
            match = re.match(rf"({_VAR})\s*=\s*(?:getelementptr|bitcast)\b.*?ptr\s+({_VAR})", line)
            if match and match.group(2) in state and match.group(1) not in state:
                state.add(match.group(1)); changed = True
    return state


def _mutable_state_offsets(functions: dict[str, _Function], init_name: str,
                           tbaa: dict[str, int]) -> set[int]:
    mutable: set[int] = set()
    for name, fn in functions.items():
        if name == init_name:
            continue
        pointers = _pointer_provenance(fn)
        for line in (line for block in fn.blocks.values() for line in block):
            match = re.search(rf"\bstore\b.*?,\s*ptr\s+({_VAR}).*?!tbaa\s+(!\d+)", line)
            if match and match.group(1) in pointers and match.group(2) in tbaa:
                mutable.add(tbaa[match.group(2)])
    return mutable


def _value(token: str, env: dict[str, Expr], bits: int = 64) -> Optional[Expr]:
    vars_found = re.findall(_VAR, token)
    if vars_found:
        return env.get(vars_found[-1])
    match = re.search(r"(?:^|\s)(-?\d+|true|false)(?:\s|$)", token)
    if not match:
        return None
    raw = match.group(1)
    number = 1 if raw == "true" else 0 if raw == "false" else int(raw)
    return ("const", number & ((1 << bits) - 1), bits)


def _execute(fn: _Function, tbaa: dict[str, int]):
    if len(fn.args) < 2:
        return []
    initial: dict[str, Expr] = {fn.args[0]: ("state",), fn.args[1]: ("addr",)}
    if len(fn.args) > 2:
        initial[fn.args[2]] = ("size",)
    if len(fn.args) > 3:
        initial[fn.args[3]] = ("pc",)
    work = [("entry", "", initial, [], False, frozenset())]
    returns = []
    while work and len(returns) < 128:
        block, pred, incoming, conditions, effectful, visited = work.pop()
        if block in visited or block not in fn.blocks:
            returns.append((conditions, None, True)); continue
        env = dict(incoming)
        pointer_vars = {key for key, value in env.items() if value and value[0] in ("state", "ptr")}
        terminated = False
        for line in fn.blocks[block]:
            phi = re.match(rf"({_VAR})\s*=\s*phi\s+i\d+\s+(.+)", line)
            if phi:
                chosen = None
                for value, label in re.findall(rf"\[\s*([^,]+),\s*%([-A-Za-z$._0-9]+)\s*\]", phi.group(2)):
                    if label == pred:
                        chosen = _value(value, env); break
                env[phi.group(1)] = chosen or ("unknown",)
                continue
            gep = re.match(rf"({_VAR})\s*=\s*(?:getelementptr|bitcast)\b.*?ptr\s+({_VAR})", line)
            if gep:
                if gep.group(2) in pointer_vars:
                    pointer_vars.add(gep.group(1)); env[gep.group(1)] = ("ptr",)
                continue
            load = re.match(rf"({_VAR})\s*=\s*load\s+i(8|16|32|64),\s*ptr\s+({_VAR}).*?(?:!tbaa\s+(!\d+))?$", line)
            if load:
                tag_match = re.search(r"!tbaa\s+(!\d+)", line)
                tag = tag_match.group(1) if tag_match else ""
                if load.group(3) in pointer_vars and tag in tbaa:
                    env[load.group(1)] = ("load", tbaa[tag], int(load.group(2)))
                elif load.group(3) == fn.args[0]:
                    env[load.group(1)] = ("load", 0, int(load.group(2)))
                else:
                    env[load.group(1)] = ("unknown",)
                continue
            binary = re.match(rf"({_VAR})\s*=\s*(add|sub|and|or|xor|shl|lshr)\s+i(\d+)\s+([^,]+),\s*(.+)", line)
            if binary:
                lhs, rhs = _value(binary.group(4), env, int(binary.group(3))), _value(binary.group(5), env, int(binary.group(3)))
                env[binary.group(1)] = (binary.group(2), lhs, rhs) if lhs and rhs else ("unknown",)
                continue
            cast = re.match(rf"({_VAR})\s*=\s*(zext|trunc)\s+i\d+\s+(.+?)\s+to\s+i(\d+)", line)
            if cast:
                value = _value(cast.group(3), env, int(cast.group(4)))
                env[cast.group(1)] = (cast.group(2), value, int(cast.group(4))) if value else ("unknown",)
                continue
            cmp_ = re.match(rf"({_VAR})\s*=\s*icmp\s+(eq|ne|ult|ule|ugt|uge|slt|sle|sgt|sge)\s+i(\d+)\s+([^,]+),\s*(.+)", line)
            if cmp_:
                lhs, rhs = _value(cmp_.group(4), env, int(cmp_.group(3))), _value(cmp_.group(5), env, int(cmp_.group(3)))
                env[cmp_.group(1)] = ("cmp", cmp_.group(2), lhs, rhs)
                continue
            if re.search(r"\bcall\b", line):
                effectful = True
                assigned = re.match(rf"({_VAR})\s*=", line)
                if assigned:
                    env[assigned.group(1)] = ("unknown",)
                continue
            ret = re.match(r"ret\s+i\d+\s+(.+)", line)
            if ret:
                returns.append((conditions, _value(ret.group(1), env), effectful)); terminated = True; break
            branch = re.match(rf"br\s+i1\s+({_VAR}),\s*label\s+%([-\w.$]+),\s*label\s+%([-\w.$]+)", line)
            if branch:
                condition = env.get(branch.group(1), ("unknown",))
                seen = visited | {block}
                work.append((branch.group(3), block, env, conditions + [(condition, False)], effectful, seen))
                work.append((branch.group(2), block, env, conditions + [(condition, True)], effectful, seen))
                terminated = True; break
            jump = re.match(r"br\s+label\s+%([-\w.$]+)", line)
            if jump:
                work.append((jump.group(1), block, env, conditions, effectful, visited | {block}))
                terminated = True; break
            switch = re.match(r"switch\s+i\d+\s+([^,]+),\s*label\s+%([-\w.$]+)\s+\[(.*)\]", line)
            if switch:
                selector = _value(switch.group(1), env)
                cases = re.findall(r"i\d+\s+(-?\d+),\s*label\s+%([-\w.$]+)", switch.group(3))
                seen = visited | {block}
                negatives = [(('cmp', 'eq', selector, ('const', int(value), 64)), False) for value, _ in cases]
                work.append((switch.group(2), block, env, conditions + negatives, effectful, seen))
                for value, target in cases:
                    cond = ("cmp", "eq", selector, ("const", int(value), 64))
                    work.append((target, block, env, conditions + [(cond, True)], effectful, seen))
                terminated = True; break
        if not terminated:
            returns.append((conditions, None, True))
    return returns


def _strip_cast(expr: Optional[Expr]) -> Optional[Expr]:
    while expr and expr[0] in ("zext", "trunc"):
        expr = expr[1]
    return expr


def _selector(conditions, mutable_offsets: set[int]):
    def depends(expr, name: str) -> bool:
        if not isinstance(expr, tuple):
            return False
        return expr == (name,) or any(depends(item, name) for item in expr[1:])

    if any(depends(condition, "size") or depends(condition, "pc")
           for condition, _truth in conditions):
        return None
    # The expression format has no conditional operator. Every path predicate
    # must therefore be an address-selection predicate; accepting a branch on
    # live state here would freeze one arm of the model's behavior.
    for condition, _truth in conditions:
        normalized = _strip_cast(condition)
        if not normalized or normalized[0:2] != ("cmp", "eq") or \
           not depends(normalized, "addr"):
            return None
    for condition, truth in reversed(conditions):
        condition = _strip_cast(condition)
        if not truth or not condition or condition[0] != "cmp" or condition[1] != "eq":
            continue
        lhs, rhs = _strip_cast(condition[2]), _strip_cast(condition[3])
        if lhs and rhs and lhs[0] == "const":
            lhs, rhs = rhs, lhs
        if lhs == ("addr",) and rhs and rhs[0] == "load" and rhs[1] not in mutable_offsets:
            return ("FD_MMIO_SELECTOR_ADDRESS_EQ_STATE", rhs[1], rhs[2] // 8, 0)
        if rhs == ("addr",) and lhs and lhs[0] == "load" and lhs[1] not in mutable_offsets:
            return ("FD_MMIO_SELECTOR_ADDRESS_EQ_STATE", lhs[1], lhs[2] // 8, 0)
        if lhs and lhs[0] == "sub" and _strip_cast(lhs[1]) == ("addr",):
            base = _strip_cast(lhs[2])
            if base and base[0] == "load" and base[1] not in mutable_offsets and rhs and rhs[0] == "const":
                return ("FD_MMIO_SELECTOR_ADDRESS_REL_STATE", base[1], base[2] // 8, rhs[1])
        if lhs == ("addr",) and rhs and rhs[0] == "const":
            return ("FD_MMIO_SELECTOR_ADDRESS_EQ_CONSTANT", 0, 0, rhs[1])
    return None


def _postfix(expr: Optional[Expr], state_size: int):
    expr = _strip_cast(expr)
    if not expr:
        return None
    kind = expr[0]
    if kind == "const":
        return [("FD_MMIO_EXPR_CONST", 0, expr[1])]
    if kind == "load":
        offset, bits = expr[1], expr[2]
        if bits not in (8, 16, 32) or offset < 0 or offset + bits // 8 > state_size:
            return None
        return [(f"FD_MMIO_EXPR_LOAD_U{bits}", offset, 0)]
    opcodes = {"add": "ADD", "sub": "SUB", "and": "AND", "or": "OR",
               "xor": "XOR", "shl": "SHL", "lshr": "LSHR"}
    if kind in opcodes:
        lhs, rhs = _postfix(expr[1], state_size), _postfix(expr[2], state_size)
        if lhs is None or rhs is None:
            return None
        return lhs + rhs + [(f"FD_MMIO_EXPR_{opcodes[kind]}", 0, 0)]
    return None
