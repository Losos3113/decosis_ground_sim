#!/usr/bin/env python3

import argparse
import glob
import os
import socket
import sys
import time

PACKETS_PER_BURST = 7
TS_PACKET_SIZE = 188
BURST_SIZE = PACKETS_PER_BURST * TS_PACKET_SIZE


def replay_once(data: bytes, sock: socket.socket, address: tuple, rate_bps: int, log=print) -> int:
    if len(data) % TS_PACKET_SIZE != 0:
        log(f"[REPLAY] WARNING: file size ({len(data)} B) is not a multiple of "
           f"{TS_PACKET_SIZE} B (TS packet) - trailing partial packet discarded")
    usable_length = (len(data) // TS_PACKET_SIZE) * TS_PACKET_SIZE
    data = data[:usable_length]

    burst_duration = BURST_SIZE * 8 / rate_bps
    start_time = time.monotonic()
    bytes_sent = 0
    burst_count = 0
    for offset in range(0, len(data), BURST_SIZE):
        chunk = data[offset:offset + BURST_SIZE]
        sock.sendto(chunk, address)
        bytes_sent += len(chunk)
        burst_count += 1
        due_time = start_time + burst_count * burst_duration
        remaining = due_time - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
    return bytes_sent


def main():
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="konkretni .ts soubor")
    source.add_argument("--dir", metavar="DIR",
                        help="prehraj jediny .ts soubor v teto slozce - vymena "
                             "videa je pak jen nahrazeni souboru, bez zasahu do "
                             "konfigurace (pouziva to sluzba replay v compose)")
    parser.add_argument("--dst", default="239.1.1.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--iface", default=None, metavar="IP")
    parser.add_argument("--ttl", type=int, default=8)
    parser.add_argument("--rate", default="5M", metavar="RATE")
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()

    rate_text = args.rate.strip().lower()
    multiplier = 1_000_000 if rate_text.endswith("m") else (1_000 if rate_text.endswith("k") else 1)
    rate_bps = int(float(rate_text.rstrip("mk")) * multiplier)

    path = args.file
    if args.dir:
        candidates = sorted(glob.glob(os.path.join(args.dir, "*.ts")))
        if not candidates:
            sys.exit(f"ERROR: ve {args.dir} neni zadny .ts soubor")
        path = candidates[0]
        if len(candidates) > 1:
            # radeji hlasit, nez tise vybrat - jinak clovek vymeni video,
            # zapomene smazat stare a divi se, ze se prehrava porad to same
            print(f"[REPLAY] WARNING: ve {args.dir} je {len(candidates)} .ts "
                  f"souboru, prehravam prvni podle abecedy: "
                  f"{os.path.basename(path)}")

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as error:
        sys.exit(f"ERROR: cannot read {path}: {error}")

    print(f"[REPLAY] {path} ({len(data)} B) -> {args.dst}:{args.port}, "
         f"rate {rate_bps/1e6:.2f} Mb/s, TTL {args.ttl}"
         f"{' (looping)' if args.loop else ''}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, args.ttl)
    if args.iface:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                        socket.inet_aton(args.iface))
    address = (args.dst, args.port)

    run_count = 0
    try:
        while True:
            bytes_sent = replay_once(data, sock, address, rate_bps)
            run_count += 1
            print(f"[REPLAY] run {run_count}: sent {bytes_sent} B "
                 f"({bytes_sent/1e6:.1f} MB, ~{bytes_sent*8/rate_bps:.1f} s at this rate)")
            if not args.loop:
                break
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        print(f"\n[REPLAY] done | {run_count} complete runs")


if __name__ == "__main__":
    main()
