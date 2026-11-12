"""Bounded Clang/LLVM analysis for automatically generated MMIO operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Optional, Sequence

from .host.llvm_layout import LlvmLayoutError, gep_offsets


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
    predicates: tuple[tuple[str, int, int, int, bool], ...] = ()


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
    command = [compiler, "-fPIC", "-O1", "-S", "-emit-llvm", "-fno-discard-value-names",
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
    if state_size <= 0 or state_size > 0xFFFFFFFF:
        raise MmioModelError("could not prove the size of the model state allocation")
    try:
        offsets = gep_offsets(ir)
    except LlvmLayoutError as exc:
        raise MmioModelError(str(exc)) from exc
    mutable_offsets = _mutable_state_offsets(functions, init_name, offsets, state_size)
    lifetime_safe = _state_lifetime_safe(functions, init_name, offsets)
    if not lifetime_safe:
        mutable_offsets = {-1}  # unknown writes, without materializing every byte
    paths = _execute(functions[read_name], offsets, state_size)

    operations: list[Operation] = []
    declined = 0
    for conditions, result, effectful in paths:
        if effectful or result is None:
            declined += 1
            continue
        selectors = _selectors(conditions, mutable_offsets, state_size)
        postfix = _postfix(result, state_size)
        if not lifetime_safe and postfix and any("LOAD" in op[0] for op in postfix):
            postfix = None
        if selectors is None or postfix is None or len(postfix) > 16:
            declined += 1
            continue
        selector, predicates = selectors
        operations.append(Operation(*selector, tuple(postfix), tuple(predicates)))
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
        lines.extend(["        },", f"        .predicate_count = {len(op.predicates)},",
                      "        .predicates = {"])
        for kind, offset, width, value, truth in op.predicates:
            lines.append(f"            {{ .selector_kind = {kind}, .state_offset = {offset}, "
                         f".width = {width}, .value = {value}, .equal = {int(truth)} }},")
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
        args = [re.findall(_VAR, item)[-1] for item in header.group(2).split(",")
                if re.findall(_VAR, item)]
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


def _allocation_size(init: Optional[_Function]) -> int:
    if init is None:
        return 0
    text = "\n".join(line for block in init.blocks.values() for line in block)
    allocations = {}
    for line in text.splitlines():
        assigned = re.match(rf"({_VAR})\s*=.*?@(?:calloc|malloc)\(", line)
        if not assigned:
            continue
        calloc = re.search(r"@calloc\(i64(?:\s+\w+)*\s+(\d+),\s+i64(?:\s+\w+)*\s+(\d+)\)", line)
        malloc = re.search(r"@malloc\(i64(?:\s+\w+)*\s+(\d+)\)", line)
        if calloc:
            allocations[assigned[1]] = int(calloc[1]) * int(calloc[2])
        elif malloc:
            allocations[assigned[1]] = int(malloc[1])
    if len(allocations) != 1:
        return 0
    # Prove every non-null return is the allocation base, including phi merges.
    safe = set(allocations) | {"null"}
    changed = True
    while changed:
        changed = False
        for dst, choices in re.findall(rf"({_VAR})\s*=\s*phi\s+ptr\s+([^\n]+)", text):
            values = re.findall(r"\[\s*([^,]+),", choices)
            if values and all(value.strip() in safe for value in values) and dst not in safe:
                safe.add(dst)
                changed = True
    returns = re.findall(r"ret ptr\s+([^\s,]+)", text)
    return next(iter(allocations.values())) if returns and all(v in safe for v in returns) else 0


def _pointer_provenance(fn: _Function, offsets) -> dict[str, int | None]:
    state = {fn.args[0]: 0} if fn.args else {}
    changed = True
    while changed:
        changed = False
        for line in (line for block in fn.blocks.values() for line in block):
            match = re.match(rf"({_VAR})\s*=\s*getelementptr\b", line)
            if match and match.group(1) not in state:
                base, delta = offsets.get((fn.name, match.group(1)), ("", None))
                if base in state:
                    state[match.group(1)] = (state[base] + delta
                                            if state[base] is not None and delta is not None
                                            else None)
                    changed = True
    return state


def _mutable_state_offsets(functions: dict[str, _Function], init_name: str,
                           offsets, state_size: int) -> set[int]:
    mutable: set[int] = set()
    for name, fn in functions.items():
        if name == init_name:
            # Publishing a state pointer during initialization defeats the
            # closed-world immutability/lifetime proof.
            if any(re.search(r"\bstore\s+(?:(?:volatile|atomic)\s+)?ptr\b", line)
                   for block in fn.blocks.values() for line in block):
                return {-1}
            continue
        pointers = _pointer_provenance(fn, offsets)
        for line in (line for block in fn.blocks.values() for line in block):
            # A helper/indirect call or escaped pointer can modify any byte.
            # Interprocedural alias proof is deliberately outside this subset.
            if re.search(r"\b(call|invoke|atomicrmw|cmpxchg)\b", line):
                return {-1}
            if line.startswith("store "):
                match = re.match(rf"store\s+i(\d+)\s+[^,]+,\s*ptr\s+({_VAR})", line)
                if not match or pointers.get(match.group(2)) is None:
                    return {-1}
                offset = pointers[match.group(2)]
                end = offset + (int(match.group(1)) + 7) // 8
                if not 0 <= offset <= end <= state_size:
                    return {-1}
                mutable.update(range(offset, end))
    return mutable


def _state_lifetime_safe(functions, init_name, offsets):
    """Closed model: calls to defined helpers are allowed only for lifetime.

    They still invalidate selector immutability. Unknown calls, pointer stores,
    and pointer returns can publish/free state and forbid live state loads.
    """
    for name, fn in functions.items():
        allocation_pointers = set()
        if name == init_name:
            for line in (line for block in fn.blocks.values() for line in block):
                match = re.match(rf"({_VAR})\s*=.*?@(?:calloc|malloc)\(", line)
                if match:
                    allocation_pointers.add(match[1])
            changed = True
            while changed:
                changed = False
                for line in (line for block in fn.blocks.values() for line in block):
                    assigned = re.match(rf"({_VAR})\s*=\s*(.*)", line)
                    if assigned and assigned[1] not in allocation_pointers and \
                            allocation_pointers & set(re.findall(_VAR, assigned[2])):
                        # Also cover phi/select/bitcast/ptrtoint aliases. This
                        # intentionally over-taints scalar values read from
                        # state rather than losing a potential escape.
                        allocation_pointers.add(assigned[1])
                        changed = True
        for line in (line for block in fn.blocks.values() for line in block):
            if re.search(r"\bstore\s+(?:(?:volatile|atomic)\s+)?ptr\b", line):
                return False
            if name == init_name:
                if line.startswith("store "):
                    value, _, destination = line.partition(", ptr")
                    if allocation_pointers & set(re.findall(_VAR, value)) and \
                            not allocation_pointers & set(re.findall(_VAR, destination)):
                        return False
                if re.search(r"\b(call|invoke)\b", line):
                    arguments = line[line.find("("):]
                    if allocation_pointers & set(re.findall(_VAR, arguments)):
                        return False
                continue
            if line.startswith("ret ptr"):
                return False
            if re.search(r"\b(call|invoke)\b", line):
                target = re.search(r"@([-A-Za-z$._0-9]+)\(", line)
                if not target or target[1] not in functions:
                    return False
    return True


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


def _execute(fn: _Function, offsets, state_size: int):
    if len(fn.args) < 2:
        return []
    initial: dict[str, Expr] = {fn.args[0]: ("ptr", 0), fn.args[1]: ("addr",)}
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
        terminated = False
        for line in fn.blocks[block]:
            phi = re.match(rf"({_VAR})\s*=\s*phi\s+i(\d+)\s+(.+)", line)
            if phi:
                chosen = None
                for value, label in re.findall(rf"\[\s*([^,]+),\s*%([-A-Za-z$._0-9]+)\s*\]", phi.group(3)):
                    if label == pred:
                        chosen = _value(value, env, int(phi[2])); break
                env[phi.group(1)] = chosen or ("unknown",)
                continue
            gep = re.match(rf"({_VAR})\s*=\s*getelementptr\b", line)
            if gep:
                base, delta = offsets.get((fn.name, gep.group(1)), ("", None))
                pointer = env.get(base)
                env[gep.group(1)] = (("ptr", pointer[1] + delta)
                                    if pointer and pointer[0] == "ptr" and delta is not None
                                    else ("unknown",))
                continue
            load = re.match(rf"({_VAR})\s*=\s*load\s+i(8|16|32|64),\s*ptr\s+({_VAR}).*?(?:!tbaa\s+(!\d+))?$", line)
            if load:
                pointer = env.get(load.group(3))
                bits = int(load.group(2))
                if pointer and pointer[0] == "ptr" and 0 <= pointer[1] <= state_size - bits // 8:
                    env[load.group(1)] = ("load", pointer[1], bits)
                else:
                    env[load.group(1)] = ("unknown",)
                    effectful = True  # even an unused invalid/unknown load is unsafe
                continue
            binary = re.match(rf"({_VAR})\s*=\s*(add|sub|and|or|xor|shl|lshr)\s+i(\d+)\s+([^,]+),\s*(.+)", line)
            if binary:
                lhs, rhs = _value(binary.group(4), env, int(binary.group(3))), _value(binary.group(5), env, int(binary.group(3)))
                env[binary.group(1)] = (binary.group(2), lhs, rhs, int(binary.group(3))) if lhs and rhs else ("unknown",)
                continue
            cast = re.match(rf"({_VAR})\s*=\s*(zext|trunc)\s+i(\d+)\s+(.+?)\s+to\s+i(\d+)", line)
            if cast:
                value = _value(cast[4], env, int(cast[3]))
                width = int(cast[3] if cast[2] == "zext" else cast[5])
                env[cast[1]] = (cast[2], value, width) if value else ("unknown",)
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
            # Closed admission list: stores, volatile/atomic loads, fences,
            # inline asm, and every unsupported instruction poison this path.
            effectful = True
        if not terminated:
            returns.append((conditions, None, True))
    return returns


def _selectors(conditions, mutable_offsets: set[int], state_size: int):
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
        normalized = condition
        if not normalized or normalized[0:2] != ("cmp", "eq") or \
           not depends(normalized, "addr"):
            return None
    predicates = []
    primary = None
    for condition, truth in conditions:
        selector = None
        lhs, rhs = condition[2], condition[3]
        if lhs and rhs and lhs[0] == "const":
            lhs, rhs = rhs, lhs
        if rhs == ("addr",):
            lhs, rhs = rhs, lhs
        def immutable(load):
            return (load and load[0] == "load" and load[2] == 64
                    and -1 not in mutable_offsets
                    and 0 <= load[1] <= state_size - 8
                    and not (set(range(load[1], load[1] + 8)) & mutable_offsets))
        if lhs == ("addr",) and immutable(rhs):
            selector = ("FD_MMIO_SELECTOR_ADDRESS_EQ_STATE", rhs[1], 8, 0)
        if lhs and lhs[0] == "sub" and lhs[1] == ("addr",):
            base = lhs[2]
            if immutable(base) and rhs and rhs[0] == "const" and lhs[3] == 64:
                selector = ("FD_MMIO_SELECTOR_ADDRESS_REL_STATE", base[1], 8, rhs[1])
        if lhs == ("addr",) and rhs and rhs[0] == "const":
            selector = ("FD_MMIO_SELECTOR_ADDRESS_EQ_CONSTANT", 0, 0, rhs[1])
        if selector is None:
            return None
        predicates.append((*selector, truth))
        if truth:
            primary = selector
    return (primary, predicates) if primary and len(predicates) <= 8 else None


def _postfix(expr: Optional[Expr], state_size: int):
    if not expr:
        return None
    kind = expr[0]
    def mask(code, bits):
        if code is None or bits not in (8, 16, 32, 64):
            return None
        return code + [("FD_MMIO_EXPR_CONST", 0, (1 << bits) - 1),
                       ("FD_MMIO_EXPR_AND", 0, 0)] if bits < 32 else code
    if kind in ("zext", "trunc"):
        return mask(_postfix(expr[1], state_size), expr[2])
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
        # The runtime evaluates at 32 bits. Wider computations require a wider
        # evaluator and are conservatively declined.
        if expr[3] > 32 or (kind in ("shl", "lshr") and
                           (expr[2][0] != "const" or expr[2][1] >= expr[3])):
            return None
        lhs, rhs = _postfix(expr[1], state_size), _postfix(expr[2], state_size)
        if lhs is None or rhs is None:
            return None
        return mask(lhs + rhs + [(f"FD_MMIO_EXPR_{opcodes[kind]}", 0, 0)], expr[3])
    return None
