#!/usr/bin/env python3
"""Send a local INAV simulated receiver signal; Ctrl-C stops the signal."""
import argparse
import socket
import struct
import time


def pulse(value):
    value = int(value)
    if not 900 <= value <= 2100:
        raise argparse.ArgumentTypeError('PWM must be between 900 and 2100 microseconds')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--roll', type=pulse, default=1500)
    parser.add_argument('--pitch', type=pulse, default=1500)
    parser.add_argument('--throttle', type=pulse, default=1000)
    parser.add_argument('--yaw', type=pulse, default=1500)
    parser.add_argument('--arm', action='store_true', help='hold AUX1 high (arm switch)')
    parser.add_argument('--duration', type=float, default=10, help='seconds to transmit')
    parser.add_argument('--arm-after', type=float, default=2, help='seconds of disarmed signal before arming')
    parser.add_argument('--throttle-after', type=float, default=4, help='seconds to hold low throttle')
    args = parser.parse_args()
    if args.duration <= 0 or args.arm_after < 0 or args.throttle_after < args.arm_after:
        parser.error('duration must be positive and throttle-after must be at least arm-after')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        start = time.monotonic()
        while time.monotonic() - start < args.duration:
            elapsed = time.monotonic() - start
            armed = args.arm and elapsed >= args.arm_after
            throttle = args.throttle if elapsed >= args.throttle_after else 1000
            channels = (args.roll, args.pitch, throttle, args.yaw,
                        1900 if armed else 1000, 1000, 1000, 1000)
            payload = b'FDRC' + struct.pack('<8H', *channels)
            sock.sendto(payload, ('127.0.0.1', 14590))
            time.sleep(.02)


if __name__ == '__main__':
    main()
