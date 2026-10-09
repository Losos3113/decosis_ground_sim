#!/usr/bin/env python3

import argparse
import functools
import os
import socket
import struct
import sys
import time

# klv_core.py zije v ../converter/ (jediny zdroj pravdy, viz Known issues v
# DOCUMENTATION.md - nic sem neduplikujeme, aby se to casem nerozjelo jako
# driv 4609/ vs Covnertor/).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "converter"))
from klv_core import KLVStateStore, TAG_DEFINITIONS, STATIC_TAG_NUMBERS, extract_pes_pts

print = functools.partial(print, flush=True)


class KlvStreamDemuxer:

    def __init__(self, on_klv_data, log=print):
        self.on_klv_data = on_klv_data
        self.log = log
        self.program_map_pid = None
        self.klv_stream_pid = None
        self._pes_buffer = None
        self._pes_expected_length = None
        self._last_continuity_counter = None

    def _parse_program_association_table(self, payload: bytes):
        pointer_field = payload[0]
        section = payload[1 + pointer_field:]
        if len(section) < 8 or section[0] != 0x00:
            return
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        program_entries = section[8:3 + section_length - 4]
        if len(program_entries) >= 4:
            self.program_map_pid = ((program_entries[2] & 0x1F) << 8) | program_entries[3]

    def _parse_program_map_table(self, payload: bytes):
        pointer_field = payload[0]
        section = payload[1 + pointer_field:]
        if len(section) < 12 or section[0] != 0x02:
            return
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        program_info_length = ((section[10] & 0x0F) << 8) | section[11]
        offset = 12 + program_info_length
        section_end = 3 + section_length - 4
        while offset + 5 <= section_end:
            stream_type = section[offset]
            elementary_stream_pid = ((section[offset + 1] & 0x1F) << 8) | section[offset + 2]
            es_info_length = ((section[offset + 3] & 0x0F) << 8) | section[offset + 4]
            if stream_type == 0x15:
                if self.klv_stream_pid != elementary_stream_pid:
                    self.klv_stream_pid = elementary_stream_pid
                    self.log(f"[KLV] found KLV PID 0x{elementary_stream_pid:04X} "
                             f"in PMT (stream_type 0x15)")
            offset += 5 + es_info_length

    def _finish_pes_packet(self):
        packet = bytes(self._pes_buffer)
        self._pes_buffer = None
        self._pes_expected_length = None
        if len(packet) < 9 or packet[:3] != b"\x00\x00\x01":
            return
        header_data_length = packet[8]
        payload_start = 9 + header_data_length
        payload = packet[payload_start:]
        if len(payload) < 5:
            return
        cell_length = struct.unpack(">H", payload[3:5])[0]
        klv_bytes = payload[5:5 + cell_length]
        if len(klv_bytes) != cell_length:
            self.log(f"[KLV] WARNING: AU cell length {cell_length} B does not match "
                     f"received data ({len(klv_bytes)} B) - packet dropped")
            return
        self.on_klv_data(klv_bytes, extract_pes_pts(packet))

    def feed(self, ts_packet: bytes):
        if len(ts_packet) != 188 or ts_packet[0] != 0x47:
            return
        packet_id = ((ts_packet[1] & 0x1F) << 8) | ts_packet[2]
        if packet_id == 0x1FFF:
            return
        payload_unit_start = bool(ts_packet[1] & 0x40)
        adaptation_field_control = (ts_packet[3] >> 4) & 0x3
        continuity_counter = ts_packet[3] & 0xF
        payload_offset = 4
        if adaptation_field_control in (2, 3):
            field_length = ts_packet[4]
            payload_offset = 5 + field_length
        if adaptation_field_control == 2 or payload_offset >= len(ts_packet):
            return
        payload = ts_packet[payload_offset:188]

        if packet_id == 0x0000:
            if payload_unit_start:
                self._parse_program_association_table(payload)
            return
        if self.program_map_pid is not None and packet_id == self.program_map_pid:
            if payload_unit_start:
                self._parse_program_map_table(payload)
            return
        if self.klv_stream_pid is None or packet_id != self.klv_stream_pid:
            return

        if payload_unit_start:
            if self._pes_buffer is not None:
                self.log("[KLV] WARNING: new PES started before previous one finished - "
                         "incomplete packet dropped (lost TS packet?)")
            self._pes_buffer = bytearray(payload)
            self._last_continuity_counter = continuity_counter
            if len(self._pes_buffer) >= 6:
                pes_length = struct.unpack(">H", self._pes_buffer[4:6])[0]
                self._pes_expected_length = 6 + pes_length if pes_length else None
        else:
            if self._pes_buffer is None:
                return
            expected_counter = (self._last_continuity_counter + 1) & 0xF
            if continuity_counter != expected_counter:
                self.log(f"[KLV] WARNING: continuity counter gap on PID "
                         f"0x{packet_id:04X} ({self._last_continuity_counter}->"
                         f"{continuity_counter}) - PES in progress dropped")
                self._pes_buffer = None
                self._pes_expected_length = None
                return
            self._last_continuity_counter = continuity_counter
            self._pes_buffer += payload

        if self._pes_expected_length is not None and self._pes_buffer is not None \
                and len(self._pes_buffer) >= self._pes_expected_length:
            self._finish_pes_packet()


def print_klv_snapshot(klv_state_store: KLVStateStore, latest_pts) -> None:
    # latest_pts = PTS of the most recently received KLV packet: this tool
    # watches the stream live for a human operator rather than pairing with
    # a specific video frame, so "now" is defined as the newest KLV info we
    # have, same idea as state_holder.py's per-frame snapshot but anchored
    # to the last packet instead of a decoded frame's PTS.
    if latest_pts is None:
        print("[STATE] no valid KLV data yet")
        return
    snapshot = klv_state_store.get_snapshot(latest_pts)
    if not snapshot:
        print("[STATE] no valid KLV data yet")
        return
    print(f"[STATE] --- {len(snapshot)} tags, {klv_state_store.valid_packet_count} "
         f"packets ok, {klv_state_store.checksum_failure_count} checksum failures ---")
    for tag in sorted(snapshot):
        info = snapshot[tag]
        kind = "static " if info["static"] else "dynamic"
        value = info["value"]
        if isinstance(value, float):
            value_text = f"{value:.4f}"
        elif isinstance(value, dict):
            value_text = ", ".join(f"{k}={v}" for k, v in value.items())
        else:
            value_text = str(value)
        print(f"  [{kind}] Tag {tag:2d} {info['name']:<28} = {value_text}  "
             f"(age {info['age_s']:5.1f} s)")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="239.1.1.1", metavar="IP")
    parser.add_argument("--src-port", type=int, default=5000)
    parser.add_argument("--iface", default=None, metavar="IP")
    parser.add_argument("--print-interval", type=float, default=2.0, metavar="S")
    return parser.parse_args()


def main():
    args = parse_args()

    receive_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receive_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    receive_socket.bind(("0.0.0.0", args.src_port))
    multicast_request = struct.pack("4s4s", socket.inet_aton(args.src),
                                    socket.inet_aton(args.iface or "0.0.0.0"))
    receive_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, multicast_request)

    klv_state_store = KLVStateStore()
    latest_pts = [None]

    def on_klv_data(klv_bytes, pts):
        klv_state_store.update(klv_bytes, pts)
        if pts is not None:
            latest_pts[0] = pts

    demuxer = KlvStreamDemuxer(on_klv_data=on_klv_data)

    print(f"[KLV] receiving {args.src}:{args.src_port}, looking for PAT/PMT -> KLV PID...")
    receive_socket.settimeout(max(0.5, args.print_interval) if args.print_interval > 0 else None)
    last_print_time = time.monotonic()
    try:
        while True:
            try:
                data, _ = receive_socket.recvfrom(2048)
            except socket.timeout:
                data = b""
            for offset in range(0, len(data) - 187, 188):
                demuxer.feed(data[offset:offset + 188])
            if args.print_interval > 0 and time.monotonic() - last_print_time >= args.print_interval:
                print_klv_snapshot(klv_state_store, latest_pts[0])
                last_print_time = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        receive_socket.close()
        print_klv_snapshot(klv_state_store, latest_pts[0])


if __name__ == "__main__":
    main()
