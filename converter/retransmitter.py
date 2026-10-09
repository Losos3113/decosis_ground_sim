#!/usr/bin/env python3

import argparse
import ipaddress
import socket
import struct
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--src", default="239.1.1.1")
parser.add_argument("--src-port", type=int, default=5000)
parser.add_argument("--iface", default=None, metavar="IP")
parser.add_argument("--dst", required=True)
parser.add_argument("--dst-port", type=int, required=True)
args = parser.parse_args()

receive_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
receive_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
receive_socket.bind(("0.0.0.0", args.src_port))

if ipaddress.ip_address(args.src).is_multicast:
    multicast_request = struct.pack("4s4s", socket.inet_aton(args.src),
                                    socket.inet_aton(args.iface or "0.0.0.0"))
    receive_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, multicast_request)
    print(f"[RELAY] multicast mode - joining group {args.src}")
else:
    print(f"[RELAY] unicast mode - {args.src} is not a multicast address, "
          f"listening on port {args.src_port}")

send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
destination_address = (args.dst, args.dst_port)

print(f"[RELAY] {args.src}:{args.src_port} -> {args.dst}:{args.dst_port}")

try:
    while True:
        try:
            data, _ = receive_socket.recvfrom(2048)
        except OSError as error:
            print(f"[RELAY] WARNING: recvfrom failed ({error})", file=sys.stderr)
            continue
        try:
            send_socket.sendto(data, destination_address)
        except OSError as error:
            print(f"[RELAY] WARNING: sendto failed ({error}), datagram dropped", file=sys.stderr)
except KeyboardInterrupt:
    pass
finally:
    receive_socket.close()
    send_socket.close()
