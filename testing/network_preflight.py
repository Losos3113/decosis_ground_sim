#!/usr/bin/env python3

import argparse
import socket
import struct
import sys
import time
import uuid


def print_local_interfaces() -> None:
    print("Local interfaces (name -> IP):")
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            print(f"  {info[4][0]}")
    except Exception as error:
        print(f"  (failed to determine: {error})")
    print()


def run_listen(args) -> None:
    listen_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen_socket.bind(("0.0.0.0", args.port))
    multicast_request = struct.pack("4s4s", socket.inet_aton(args.group),
                                    socket.inet_aton(args.iface or "0.0.0.0"))
    listen_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, multicast_request)
    listen_socket.settimeout(1.0)

    print_local_interfaces()
    print(f"[LISTEN] joined group {args.group}:{args.port}"
         f"{f' on interface {args.iface}' if args.iface else ''}")
    print(f"[LISTEN] waiting for test packets (Ctrl+C to stop)...\n")

    packet_count = 0
    seen_senders = set()
    try:
        while True:
            try:
                data, address = listen_socket.recvfrom(2048)
            except socket.timeout:
                continue
            packet_count += 1
            is_probe_packet = data[:6] == b"PREFLT"
            label = data[6:].decode("utf-8", "replace") if is_probe_packet else "(unrecognized format)"
            is_new_sender = address[0] not in seen_senders
            seen_senders.add(address[0])
            marker = " <- NEW SENDER" if is_new_sender else ""
            print(f"[LISTEN] #{packet_count} from {address[0]}:{address[1]} | {label}{marker}")
    except KeyboardInterrupt:
        pass
    finally:
        listen_socket.close()
        print(f"\n[LISTEN] done | {packet_count} packets from {len(seen_senders)} "
             f"distinct senders ({sorted(seen_senders)})")
        if packet_count == 0:
            print("[LISTEN] NOTHING ARRIVED - check firewall, IGMP snooping/querier, "
                 "wrong group/port, sender TTL, wrong interface on multi-NIC hosts.")


def run_send(args) -> None:
    send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    send_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, args.ttl)
    if args.iface:
        send_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                               socket.inet_aton(args.iface))
    sender_id = uuid.uuid4().hex[:8]

    print_local_interfaces()
    print(f"[SEND] sending to {args.group}:{args.port}, TTL={args.ttl}, "
         f"sender id={sender_id}")

    packet_count = 0
    try:
        while True:
            packet_count += 1
            payload = b"PREFLT" + f"packet {packet_count} from {sender_id}".encode("utf-8")
            send_socket.sendto(payload, (args.group, args.port))
            print(f"[SEND] #{packet_count} sent ({len(payload)} B)")
            if not args.loop:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        send_socket.close()
        print(f"\n[SEND] done | {packet_count} packets sent")


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    listen_parser = subparsers.add_parser("listen")
    listen_parser.add_argument("--group", default="239.1.1.1")
    listen_parser.add_argument("--port", type=int, default=5000)
    listen_parser.add_argument("--iface", default=None)

    send_parser = subparsers.add_parser("send")
    send_parser.add_argument("--group", default="239.1.1.1")
    send_parser.add_argument("--port", type=int, default=5000)
    send_parser.add_argument("--ttl", type=int, default=8)
    send_parser.add_argument("--iface", default=None)
    send_parser.add_argument("--loop", action="store_true")
    send_parser.add_argument("--interval", type=float, default=1.0)

    args = parser.parse_args()
    if args.command == "listen":
        run_listen(args)
    else:
        run_send(args)


if __name__ == "__main__":
    main()
