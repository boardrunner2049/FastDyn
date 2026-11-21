#!/usr/bin/env python3
"""Generate the train / heldout / final input sets of the compute fixture.

    gen_inputs.py --out DIR

Writes <out>/<split>/freertos-mps2-compute/<name>.bin plus
<out>/<split>/freertos-mps2-compute/MANIFEST.json (sha256, generator
parameters and the frame-level counters a Python model of the firmware's
receive state machine predicts: frames_ok, err_crc, err_len, err_seq,
garbage_bytes; and the frame-type mix).

File format (little-endian), loaded at 0x21000000 by the run harness:
  0  u32 magic 0x49434446 ("FDCI")     16 u32 seed
  4  u32 version 1                      20 u32 frames (generated frame count)
  8  u32 stream_len                     24 u32 crc32(stream)
  12 u32 passes                         28 u32 crc32(header[0:28])
  32 stream: frames A5 5A len type seq payload[len] crc16_ccitt_false(len..payload) (LE)
     with injected garbage, bad CRCs, oversize lengths, truncated frames,
     sequence gaps, unknown types and semantically malformed payloads.

Everything is deterministic in the seed: regenerating gives identical bytes.
"""
import argparse
import binascii
import collections
import hashlib
import json
import os
import random
import struct
import sys

MAGIC = 0x49434446
MAX_PAYLOAD = 200
T_SENSOR, T_KV_SET, T_KV_DEL, T_KV_GET, T_TEXT, T_BLOB, T_ROUTE = 0x10, 0x20, 0x21, 0x22, 0x30, 0x40, 0x50
UNKNOWN_TYPES = (0x00, 0x11, 0x60, 0x7F, 0xFF)

# Mix profiles: type weights and malformed-injection rates (per frame).
PROFILES = {
    # train: balanced, moderate errors; never emits the SHR op or upper-case hex
    "train": dict(types={T_SENSOR: 30, T_KV_SET: 12, T_KV_DEL: 5, T_KV_GET: 10, T_TEXT: 15, T_BLOB: 18, T_ROUTE: 10},
                  garbage=0.04, bad_crc=0.03, oversize=0.01, truncate=0.01, seq_gap=0.02, unknown=0.01,
                  semantic=0.04, text_err=0.10, shr=False, upper_hex=False, keyspace=96, payload=(16, 120)),
    # heldout: KV/text heavy, more malformed input, SHR + upper-case hex appear
    "heldout": dict(types={T_SENSOR: 15, T_KV_SET: 20, T_KV_DEL: 10, T_KV_GET: 18, T_TEXT: 22, T_BLOB: 8, T_ROUTE: 7},
                    garbage=0.07, bad_crc=0.05, oversize=0.02, truncate=0.02, seq_gap=0.04, unknown=0.03,
                    semantic=0.07, text_err=0.18, shr=True, upper_hex=True, keyspace=160, payload=(8, 160)),
    # final: sensor/blob heavy with long payloads, a different key space
    "final": dict(types={T_SENSOR: 35, T_KV_SET: 8, T_KV_DEL: 4, T_KV_GET: 8, T_TEXT: 12, T_BLOB: 25, T_ROUTE: 8},
                  garbage=0.05, bad_crc=0.04, oversize=0.015, truncate=0.015, seq_gap=0.03, unknown=0.02,
                  semantic=0.05, text_err=0.14, shr=True, upper_hex=True, keyspace=64, payload=(24, 200)),
}
# (split, name, seed, frames, passes)
SETS = [
    ("train", "train-1", 101, 2400, 14), ("train", "train-2", 102, 2400, 14), ("train", "train-3", 103, 2400, 14),
    ("heldout", "heldout-1", 201, 2400, 14), ("heldout", "heldout-2", 202, 2400, 14), ("heldout", "heldout-3", 203, 2400, 14),
    ("final", "final-1", 301, 2400, 14), ("final", "final-2", 302, 2400, 14), ("final", "final-3", 303, 2400, 14),
]


def crc16(data, crc=0xFFFF):
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def text_payload(rng, prof, maxlen):
    ops = ["SET", "ADD", "MUL", "XOR", "PUT", "GET", "CRC"] + (["SHR"] if prof["shr"] else [])

    def num():
        v = rng.randrange(0, 1 << rng.choice((4, 8, 16, 24)))
        if rng.random() < 0.3:
            s = f"0x{v:x}"
            return s.upper().replace("0X", "0x") if prof["upper_hex"] and rng.random() < 0.5 else s
        return ("-" if rng.random() < 0.1 else "") + str(v)

    def reg():
        return f"R{rng.randrange(8)}"

    stmts = []
    while True:
        op = rng.choice(ops)
        if op in ("SET",):
            s = f"SET {reg()} {num()}"
        elif op in ("ADD", "MUL", "XOR"):
            s = f"{op} {reg()} {reg() if rng.random() < 0.4 else num()}"
        elif op == "SHR":
            s = f"SHR {reg()} {rng.randrange(32)}"
        elif op == "PUT":
            s = f"PUT {rng.randrange(prof['keyspace'])} {reg()}"
        elif op == "GET":
            s = f"GET {reg()} {rng.randrange(prof['keyspace'])}"
        else:
            s = f"CRC {reg()}"
        if rng.random() < prof["text_err"]:            # malformed statement
            kind = rng.randrange(6)
            if kind == 0:
                s = "NOP " + s[4:]                     # unknown op
            elif kind == 1:
                s = s.split(" ")[0]                    # missing operands
            elif kind == 2:
                s = s + " 1"                           # extra operand
            elif kind == 3:
                s = f"SET {reg()} 99999999999"         # overflow
            elif kind == 4:
                s = f"ADD R9 {num()}"                  # bad register
            else:
                s = f"SET {reg()} 0xZZ"                # bad digit
        if len(";".join(stmts + [s])) > maxlen:
            break
        stmts.append(s)
    return (";".join(stmts)).encode()


def payload_for(rng, prof, t, semantic_bad):
    lo, hi = prof["payload"]
    n = rng.randrange(lo, hi + 1)
    ks = prof["keyspace"]
    if t == T_SENSOR:
        n &= ~1
        if semantic_bad:
            n |= 1                                     # odd length
        base = rng.randrange(-3000, 3000)
        out = bytearray()
        for i in range(n // 2):
            v = base + int(800 * ((i % 16) - 8) / 8) + rng.randrange(-200, 200)
            out += struct.pack("<h", max(-32768, min(32767, v)))
        if n & 1:
            out.append(rng.randrange(256))
        return bytes(out)
    if t == T_KV_SET:
        k = max(1, n // 6)
        out = b"".join(struct.pack("<HI", rng.randrange(ks), rng.randrange(1 << 32)) for _ in range(min(k, 33)))
        return out + (b"\x01" if semantic_bad else b"")
    if t in (T_KV_DEL, T_KV_GET):
        k = max(1, n // 2)
        out = b"".join(struct.pack("<H", rng.randrange(ks)) for _ in range(min(k, 100)))
        return out + (b"\x02" if semantic_bad else b"")
    if t == T_TEXT:
        return text_payload(rng, prof, min(n + 40, MAX_PAYLOAD)) if not semantic_bad else b"?? ;;; NOP"
    if t == T_BLOB:
        if semantic_bad:
            return b""
        out = bytearray()
        while len(out) < n:                            # runs + noise, like sensor dumps
            if rng.random() < 0.5:
                out += bytes([rng.randrange(256)]) * rng.randrange(2, 12)
            else:
                out += bytes(rng.randrange(256) for _ in range(rng.randrange(1, 8)))
        return bytes(out[:n])
    if t == T_ROUTE:
        if semantic_bad:
            return bytes(rng.randrange(256) for _ in range(2))
        return bytes(rng.randrange(0, 64) for _ in range(min(n, 64)))
    return bytes(rng.randrange(256) for _ in range(min(n, 32)))


def frame(t, seq, payload):
    body = bytes([len(payload), t, seq & 0xFF]) + payload
    return b"\xA5\x5A" + body + struct.pack("<H", crc16(body))


def generate(prof, seed, frames):
    rng = random.Random(seed)
    types, weights = zip(*sorted(prof["types"].items()))
    out = bytearray()
    seq = 0
    mix = collections.Counter()
    for _ in range(frames):
        if rng.random() < prof["garbage"]:
            g = bytes(rng.choice((rng.randrange(256), 0xA5, 0x5A)) for _ in range(rng.randrange(1, 9)))
            out += g
        if rng.random() < prof["oversize"]:
            out += bytes([0xA5, 0x5A, rng.randrange(MAX_PAYLOAD + 1, 256)]) + bytes(rng.randrange(256) for _ in range(rng.randrange(0, 6)))
        if rng.random() < prof["seq_gap"]:
            seq += rng.randrange(2, 5)
        t = rng.choices(types, weights)[0] if rng.random() >= prof["unknown"] else rng.choice(UNKNOWN_TYPES)
        semantic_bad = rng.random() < prof["semantic"]
        p = payload_for(rng, prof, t, semantic_bad)
        if len(p) > MAX_PAYLOAD:
            p = p[:MAX_PAYLOAD]
        f = bytearray(frame(t, seq, p))
        seq += 1
        mix[f"0x{t:02x}"] += 1
        if rng.random() < prof["bad_crc"] and len(f) > 7:
            f[5 + rng.randrange(len(f) - 7)] ^= 1 << rng.randrange(8)
        if rng.random() < prof["truncate"] and len(f) > 8:
            f = f[: rng.randrange(5, len(f) - 2)]
        out += f
    return bytes(out), mix


def model_rx(stream, passes):
    """Python model of prvRxTask's state machine (main_compute.c)."""
    c = collections.Counter()
    types = collections.Counter()
    for _ in range(passes):
        state, have_seq, last_seq = "SOF0", False, 0
        ln = typ = sq = got = 0
        crc = 0xFFFF
        rx = 0
        for b in stream:
            if state == "SOF0":
                if b == 0xA5:
                    state = "SOF1"
                else:
                    c["garbage_bytes"] += 1
            elif state == "SOF1":
                if b == 0x5A:
                    state, crc = "LEN", 0xFFFF
                elif b != 0xA5:
                    state = "SOF0"
                    c["garbage_bytes"] += 2
                else:
                    c["garbage_bytes"] += 1
            elif state == "LEN":
                if b > MAX_PAYLOAD:
                    c["err_len"] += 1
                    state = "SOF0"
                else:
                    ln, crc, state = b, crc16([b], crc), "TYPE"
            elif state == "TYPE":
                typ, crc, state = b, crc16([b], crc), "SEQ"
            elif state == "SEQ":
                sq, crc, got = b, crc16([b], crc), 0
                state = "PAYLOAD" if ln else "CRC0"
            elif state == "PAYLOAD":
                crc = crc16([b], crc)
                got += 1
                if got == ln:
                    state = "CRC0"
            elif state == "CRC0":
                rx, state = b, "CRC1"
            else:
                rx |= b << 8
                state = "SOF0"
                if rx != crc:
                    c["err_crc"] += 1
                    continue
                if have_seq and sq != ((last_seq + 1) & 0xFF):
                    c["err_seq"] += 1
                have_seq, last_seq = True, sq
                c["frames_ok"] += 1
                types[f"0x{typ:02x}"] += 1
        if state != "SOF0":
            c["err_len"] += 1
    return {k: c[k] for k in ("frames_ok", "err_crc", "err_len", "err_seq", "garbage_bytes")}, dict(sorted(types.items()))


def build(seed, frames, passes, prof):
    stream, mix = generate(prof, seed, frames)
    hdr = struct.pack("<7I", MAGIC, 1, len(stream), passes, seed, frames, binascii.crc32(stream))
    hdr += struct.pack("<I", binascii.crc32(hdr))
    return hdr + stream, stream, mix


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    manifests = collections.defaultdict(dict)
    for split, name, seed, frames, passes in SETS:
        prof = PROFILES[split]
        blob, stream, mix = build(seed, frames, passes, prof)
        assert len(blob) <= 1 << 20, (name, len(blob))
        d = os.path.join(args.out, split, "freertos-mps2-compute")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{name}.bin")
        with open(path, "wb") as f:
            f.write(blob)
        counters, ok_types = model_rx(stream, passes)
        manifests[split][name] = {
            "file": f"{name}.bin", "sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob),
            "seed": seed, "frames_generated": frames, "passes": passes, "stream_len": len(stream),
            "profile": split, "generated_type_mix": dict(sorted(mix.items())),
            "model_rx": counters, "model_ok_frame_types": ok_types,
        }
        print(f"{path}: {len(blob)} B, passes={passes}, model={counters}")
    for split, m in manifests.items():
        p = os.path.join(args.out, split, "freertos-mps2-compute", "MANIFEST.json")
        with open(p, "w") as f:
            json.dump({"generator": os.path.abspath(__file__), "profile": PROFILES[split] | {"types": {f"0x{k:02x}": v for k, v in PROFILES[split]["types"].items()}},
                       "inputs": m}, f, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
