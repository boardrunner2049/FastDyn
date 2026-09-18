#!/usr/bin/env python3
"""
modbus_bridge.py — Host-side Modbus RTU driver for the Nucleo-F103RB
slave example. Exercises FC 01/02/03/04/05/06/0F/10.

Usage:
    python3 modbus_bridge.py --serial /dev/ttyACM0   # real board (ST-Link VCP)
    python3 modbus_bridge.py --serial /tmp/host_modbus  # elder-mode PTY
"""
import argparse
import logging
import sys
import time

try:
    from pymodbus.client import ModbusSerialClient
except ImportError:
    sys.stderr.write(
        "pymodbus is required. Install with:\n"
        "    pip3 install --user 'pymodbus>=3.0'\n"
    )
    sys.exit(1)

import serial


def install_paced_write(pace_ms: float) -> None:
    """Patch serial.Serial.write so every TX call sends bytes one at a
    time with `pace_ms` of sleep between them. Stays well under
    FreeMODBUS RTU's t3.5 (~59 ms at 600 baud), so the chip still sees
    a single contiguous frame."""
    if pace_ms <= 0.0:
        return
    original_write = serial.Serial.write
    delay_s = pace_ms / 1000.0

    def _paced_write(self, data):
        if isinstance(data, (bytes, bytearray, memoryview)):
            buf = bytes(data)
            for i, byte in enumerate(buf):
                original_write(self, bytes([byte]))
                self.flush()
                if i + 1 < len(buf):
                    time.sleep(delay_s)
            return len(buf)
        return original_write(self, data)

    serial.Serial.write = _paced_write


SLAVE_ID = 0x0A   # matches modbus_app.c:SLAVE_ID

# Application register map — mirrors modbus_app.c.
CMD_REG       = 0
SETPOINT_REG  = 1
CMD_RESET     = 0x0001
CMD_CALIBRATE = 0x0002
CMD_DIAGNOSTIC = 0x0003


def banner(s):
    print(f"\n=== {s} ===")


def expect(label, ok, detail=""):
    tag = "OK " if ok else "FAIL"
    print(f"  [{tag}] {label}{(' — ' + detail) if detail else ''}")
    return ok


def exercise(client, profile="full"):
    """Run one pass over every register-callback path FreeMODBUS exposes,
    covering FC 01/02/03/04/05/06/0F/10."""
    failures = 0

    banner("Read input registers (FC 04) — sanity / liveness")
    rr = client.read_input_registers(address=0, count=5, device_id=SLAVE_ID)
    if rr.isError():
        failures += not expect("FC 04 read input[0..4]", False, str(rr))
    else:
        expect("FC 04 read input[0..4]", True, f"values={rr.registers}")

    banner("Read holding registers (FC 03) — single + multi")
    rr = client.read_holding_registers(address=0, count=1, device_id=SLAVE_ID)
    failures += not expect("FC 03 read holding[0]   (cmd reg)",
                           not rr.isError(),
                           "" if rr.isError() else f"value={rr.registers}")
    rr = client.read_holding_registers(address=0, count=8, device_id=SLAVE_ID)
    failures += not expect("FC 03 read holding[0..7]",
                           not rr.isError(),
                           "" if rr.isError() else f"values={rr.registers}")

    banner("Write single holding register (FC 06)")
    rr = client.write_register(address=SETPOINT_REG, value=750, device_id=SLAVE_ID)
    failures += not expect("FC 06 setpoint=750", not rr.isError())
    rr = client.read_holding_registers(address=SETPOINT_REG, count=1, device_id=SLAVE_ID)
    if not rr.isError():
        failures += not expect("FC 03 setpoint readback", rr.registers[0] == 750,
                               f"got {rr.registers[0]}")

    if profile == "full":
        banner("Write multiple holding registers (FC 16) — range path")
        rr = client.write_registers(address=10,
                                    values=[0x1111, 0x2222, 0x3333, 0x4444],
                                    device_id=SLAVE_ID)
        failures += not expect("FC 16 write holding[10..13]", not rr.isError())
        rr = client.read_holding_registers(address=10, count=4, device_id=SLAVE_ID)
        if not rr.isError():
            failures += not expect("FC 03 holding[10..13] readback",
                                   rr.registers == [0x1111, 0x2222, 0x3333, 0x4444],
                                   f"got {rr.registers}")

    banner("Range validation — setpoint clamp")
    rr = client.write_register(address=SETPOINT_REG, value=5000, device_id=SLAVE_ID)
    failures += not expect("FC 06 setpoint=5000 (clamps to 1000)", not rr.isError())
    rr = client.read_holding_registers(address=SETPOINT_REG, count=1, device_id=SLAVE_ID)
    if not rr.isError():
        failures += not expect("FC 03 setpoint readback (1000)",
                               rr.registers[0] == 1000, f"got {rr.registers[0]}")

    banner("Coils (FC 01 / 05 / 0F)")
    rr = client.read_coils(address=0, count=8, device_id=SLAVE_ID)
    failures += not expect("FC 01 read coils[0..7]", not rr.isError(),
                           "" if rr.isError() else f"bits={rr.bits[:8]}")
    rr = client.write_coil(address=0, value=True, device_id=SLAVE_ID)
    failures += not expect("FC 05 write coil[0]=1", not rr.isError())
    rr = client.write_coils(address=8, values=[True, False, True, True, False],
                            device_id=SLAVE_ID)
    failures += not expect("FC 0F write coils[8..12]", not rr.isError())

    banner("Discrete inputs (FC 02) — coil cross-link")
    rr = client.read_discrete_inputs(address=0, count=4, device_id=SLAVE_ID)
    if rr.isError():
        failures += not expect("FC 02 read discrete[0..3]", False, str(rr))
    else:
        failures += not expect("FC 02 read discrete[0..3]", True,
                               f"bits={rr.bits[:4]}")
        failures += not expect("discrete[0] mirrors coil[0]=1", rr.bits[0] is True,
                               f"got {rr.bits[0]}")

    if profile == "full":
        banner("State machine — CALIBRATE then RESET")
        rr = client.write_register(address=CMD_REG, value=CMD_CALIBRATE, device_id=SLAVE_ID)
        failures += not expect("FC 06 cmd=CALIBRATE", not rr.isError())
        time.sleep(0.05)
        rr = client.read_input_registers(address=3, count=1, device_id=SLAVE_ID)
        if not rr.isError():
            failures += not expect("input[3] == CALIBRATION (1)", rr.registers[0] == 1,
                                   f"got {rr.registers[0]}")
        rr = client.write_register(address=CMD_REG, value=CMD_RESET, device_id=SLAVE_ID)
        failures += not expect("FC 06 cmd=RESET", not rr.isError())
        time.sleep(0.05)
        rr = client.read_input_registers(address=3, count=1, device_id=SLAVE_ID)
        if not rr.isError():
            failures += not expect("input[3] == NORMAL (0)", rr.registers[0] == 0,
                                   f"got {rr.registers[0]}")

        banner("Out-of-range address — exception path")
        rr = client.read_holding_registers(address=999, count=1, device_id=SLAVE_ID)
        failures += not expect("FC 03 holding[999] returns exception", rr.isError(),
                               "no exception" if not rr.isError() else "got expected exception")

    return failures


def main():
    p = argparse.ArgumentParser(description="Modbus RTU slave host driver")
    p.add_argument("--serial", default="/dev/ttyACM0",
                   help="Serial port (real ST-Link VCP or socat'd PTY)")
    p.add_argument("--baud", type=int, default=600)
    p.add_argument("--timeout", type=float, default=5.0,
                   help="pymodbus per-frame timeout in seconds")
    p.add_argument("--repeat", type=int, default=1,
                   help="Run the exercise N times back-to-back to grow the trace")
    p.add_argument("--profile", choices=("full", "core"), default="full",
                   help="full runs every documented path; core omits FC 16, "
                        "the application-state sequence, and the deliberate "
                        "out-of-range exception request")
    p.add_argument("--pace-ms", type=float, default=0.0,
                   help="Inter-byte gap on TX in ms (0 = back-to-back). "
                        "Use 0 for the virtual-time elder model; paced "
                        "delivery is only needed when explicitly testing "
                        "physical wire timing.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)

    install_paced_write(args.pace_ms)

    client = ModbusSerialClient(port=args.serial, baudrate=args.baud,
                                bytesize=8, parity="N", stopbits=1,
                                timeout=args.timeout)
    if not client.connect():
        print(f"failed to open {args.serial}", file=sys.stderr)
        return 2

    total_fail = 0
    try:
        for i in range(args.repeat):
            if args.repeat > 1:
                print(f"\n########## Iteration {i + 1}/{args.repeat} ##########")
            total_fail += exercise(client, args.profile)
    finally:
        client.close()

    print(f"\nTotal failures: {total_fail}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
