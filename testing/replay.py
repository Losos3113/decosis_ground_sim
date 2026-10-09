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
                        help="prehraj .ts soubor z teto slozky - vymena videa je "
                             "pak jen nahrazeni souboru, bez zasahu do konfigurace")
    parser.add_argument("--uav", metavar="ID",
                        help="s --dir: prehraj <dir>/<ID>.ts, tedy soubor "
                             "pojmenovany podle letounu (uav1.ts, uav2.ts). Bez "
                             "nej se vezme jediny .ts ve slozce.")
    parser.add_argument("--dst", default="239.1.1.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--iface", default=None, metavar="IP")
    parser.add_argument("--ttl", type=int, default=8)
    parser.add_argument("--rate", default="5M", metavar="RATE")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--wait-s", type=float, default=5.0, metavar="S",
                        help="jak casto kontrolovat, jestli uz soubor existuje "
                             "(jen s --uav, kdyz video jeste neni ve slozce)")
    args = parser.parse_args()

    rate_text = args.rate.strip().lower()
    multiplier = 1_000_000 if rate_text.endswith("m") else (1_000 if rate_text.endswith("k") else 1)
    rate_bps = int(float(rate_text.rstrip("mk")) * multiplier)

    path = args.file
    if args.dir and args.uav:
        # prirazeni podle jmena, jako to driv delal video_streamer.py s MP4:
        # uav1.ts patri letounu uav1
        path = os.path.join(args.dir, f"{args.uav}.ts")
        if not os.path.exists(path):
            # CEKAT, ne spadnout. Kdyz je ve slozce jen jedno video, sluzba
            # toho druheho letounu by jinak skoncila chybou, restart policy
            # by ji nastartovala znovu a vznikl by nekonecny restart loop,
            # ktery zahlti vypis. Takhle jen tise ceka - a kdyz soubor
            # pozdeji pribude, rozjede se sama, bez restartu kontejneru.
            available = sorted(os.path.basename(p) for p in
                               glob.glob(os.path.join(args.dir, "*.ts")))
            print(f"[REPLAY] {path} zatim neexistuje "
                  f"(ve slozce je: {', '.join(available) or 'nic'}) - cekam, "
                  f"az se objevi; kontroluji kazdych {args.wait_s:g} s",
                  flush=True)
            while not os.path.exists(path):
                time.sleep(args.wait_s)
            print(f"[REPLAY] {os.path.basename(path)} se objevil, spoustim", flush=True)
    elif args.dir:
        candidates = sorted(glob.glob(os.path.join(args.dir, "*.ts")))
        if not candidates:
            sys.exit(f"ERROR: ve {args.dir} neni zadny .ts soubor")
        path = candidates[0]
        if len(candidates) > 1:
            # radeji hlasit, nez tise vybrat - jinak clovek vymeni video,
            # zapomene smazat stare a divi se, ze se prehrava porad to same
            print(f"[REPLAY] WARNING: ve {args.dir} je {len(candidates)} .ts "
                  f"souboru a neni zadane --uav; prehravam prvni podle "
                  f"abecedy: {os.path.basename(path)}")

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
