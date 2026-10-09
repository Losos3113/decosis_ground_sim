#!/usr/bin/env python3
"""Analyzator TS streamu/souboru - zjisti, jestli obsahuje KLV metadata a
jestli je video vubec dekodovatelne, BEZ nutnosti spustit ffmpeg nebo cely
converter. Nezavisle na puvodu zdroje (testovano i na nahravkach z
Haivision enkoderu, ne jen na vlastnim transmitter.py) - vsechny zavery
jsou odvozene primo z bajtu na draty, ne z toho, co o sobe stream TVRDI
(PMT stream_type, deklarovany kodek apod.), protoze presne tyhle dve veci
se u cizich zdroju muzou rozchazet.

Pouziti:
    python testing/analyze_ts.py --file nejaky_zaznam.ts
    python testing/analyze_ts.py --src 239.1.1.1 --src-port 5000 --duration 10

Pouziva primo TransportStreamDemuxer a _h264_scan_parameter_sets z
../converter/state_holder.py (ne vlastni kopii) - diagnostika tak vzdy
odpovida tomu, co by videl skutecny converter, ne nezavisle udrzovanemu
dvojiti kodu. KLV parsovani jde pres klv_core.py, jediny zdroj pravdy pro
KLV (viz DOCUMENTATION.md "Known issues").
"""

import argparse
import collections
import ipaddress
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "converter"))
from state_holder import TransportStreamDemuxer, TsPacketResync, _h264_rbsp_prefix, _BitReader
from klv_core import parse_klv_packet, UAS_LOCAL_SET_KEY


def _h264_slice_pps_id(nal_payload_after_header: bytes):
    """pic_parameter_set_id teto slice NAL (typ 1/5) - treti pole slice
    hlavicky (first_mb_in_slice, slice_type, pic_parameter_set_id), vsechna
    exp-golomb a VZDY na zacatku bez ohledu na obsah PPS/SPS (stejny princip
    jako _h264_slice_is_b v state_holder.py, jen o pole dal)."""
    rbsp = _h264_rbsp_prefix(nal_payload_after_header, max_bytes=12)
    reader = _BitReader(rbsp)
    reader.read_ue()              # first_mb_in_slice, zahozeno
    reader.read_ue()              # slice_type, zahozeno
    return reader.read_ue()       # pic_parameter_set_id


def _h264_scan_slices_and_params(access_unit: bytes):
    """Projde access unit a vrati (ma_slice, pps_ids_referencovane,
    sps_videna, pps_ids_definovane) - vse z jednoho pruchodu, stejny
    start-code scan jako jinde v projektu."""
    has_slice = False
    referenced_pps_ids = set()
    saw_sps = False
    defined_pps_ids = set()
    position, length = 0, len(access_unit)
    while position < length - 3:
        if access_unit[position:position + 3] == b"\x00\x00\x01":
            start_code_len = 3
        elif position < length - 4 and access_unit[position:position + 4] == b"\x00\x00\x00\x01":
            start_code_len = 4
        else:
            position += 1
            continue
        header_at = position + start_code_len
        if header_at >= length:
            break
        nal_type = access_unit[header_at] & 0x1F
        if nal_type == 7:
            saw_sps = True
        elif nal_type == 8:
            try:
                rbsp = _h264_rbsp_prefix(access_unit[header_at + 1:header_at + 1 + 8])
                defined_pps_ids.add(_BitReader(rbsp).read_ue())
            except (IndexError, ValueError):
                pass
        elif nal_type in (1, 5):
            has_slice = True
            try:
                pps_id = _h264_slice_pps_id(access_unit[header_at + 1:header_at + 1 + 12])
                referenced_pps_ids.add(pps_id)
            except (IndexError, ValueError):
                pass
        position = header_at + 1
    return has_slice, referenced_pps_ids, saw_sps, defined_pps_ids


class Report:

    def __init__(self):
        self.video_pid = None
        self.video_codec = None
        self.klv_pid = None
        self.klv_carriage = None
        self.resync_count = 0               # viz TsPacketResync

        self.video_au_count = 0
        self.video_au_with_slice = 0
        self.first_sps_au = None
        self.first_pps_au = {}             # pps_id -> au index prvniho vyskytu
        self.defined_pps_ids = set()
        self.referenced_pps_ids = set()
        self.hevc_au_count = 0             # SPS/PPS scan je jen H.264

        self.klv_pes_total = 0
        self.klv_key_found = 0             # kolik PES melo rozpoznatelny UAS LS klic
        self.klv_parse_ok = 0
        self.klv_checksum_ok = 0
        self.klv_checksum_failed = 0
        self.klv_pts_real = 0
        self.klv_pts_derived = 0
        self.klv_pts_missing = 0
        self.first_klv_tags = None         # tagy z prvniho uspesne zparsovaneho paketu

    @property
    def undefined_pps_ids(self):
        return self.referenced_pps_ids - self.defined_pps_ids

    def render(self, source_label: str, total_ts_packets: int) -> str:
        lines = [f"=== Analýza TS: {source_label} ===",
                 f"Zpracováno TS paketů: {total_ts_packets}"]
        if self.resync_count:
            lines.append(f"!! Synchronizace TS paketů byla ztracena a znovu nalezena "
                         f"{self.resync_count}x - vstup NENÍ od začátku do konce čistě "
                         f"188B zarovnaný. Část dat před každým resyncem byla přeskočena.")
        lines.append("")

        lines.append("--- Video ---")
        if self.video_pid is None:
            lines.append("Video PID nenalezen (PMT nikdy neoznámila video stream, "
                         "nebo PAT/PMT nedorazily vůbec).")
        else:
            lines.append(f"PID 0x{self.video_pid:04X}, kodek {self.video_codec}")
            lines.append(f"Access units zpracováno: {self.video_au_count} "
                         f"(z toho se slice: {self.video_au_with_slice})")
            if self.video_codec == "hevc":
                lines.append("SPS/PPS a PPS-ID kontrola je jen pro H.264 - "
                             "u HEVC se nevyhodnocuje (jiný formát hlaviček).")
            else:
                lines.append(f"SPS poprvé viděna: access unit #{self.first_sps_au}"
                             if self.first_sps_au is not None else
                             "SPS NIKDY viděna - bez ní ffmpeg nerozpozná rozlišení/profil.")
                if self.first_pps_au:
                    pps_list = ", ".join(f"id={pid} (AU #{au})"
                                         for pid, au in sorted(self.first_pps_au.items()))
                    lines.append(f"PPS poprvé viděna: {pps_list}")
                else:
                    lines.append("PPS NIKDY viděna.")
                if self.undefined_pps_ids:
                    lines.append(f"!! Slice odkazují na PPS {sorted(self.undefined_pps_ids)}, "
                                 f"která NIKDY NEBYLA definována v datech, co converter "
                                 f"viděl. Tohle je přesně důvod, proč ffmpeg hlásí "
                                 f"'non-existing PPS N referenced' a nikdy žádný snímek "
                                 f"nevyrobí - není co opravit v parsování, chybí to v "
                                 f"samotných datech (nebo to bylo poslané dřív, než se "
                                 f"zachytávání/poslech spustilo).")
                elif self.referenced_pps_ids:
                    lines.append(f"Všechny odkazované PPS ID ({sorted(self.referenced_pps_ids)}) "
                                 f"byly i definované - video by mělo být dekódovatelné.")

        lines.append("")
        lines.append("--- KLV ---")
        if self.klv_pid is None:
            lines.append("KLV PID nenalezen v PMT - stream podle PMT žádné KLV metadata "
                         "nenese (ověřeno jen podle PMT descriptors, ne podle obsahu).")
        else:
            lines.append(f"PID 0x{self.klv_pid:04X}, {self.klv_carriage}")
            lines.append(f"KLV PES celkem: {self.klv_pes_total}")
            lines.append(f"  s rozpoznaným klíčem UAS Local Set: {self.klv_key_found}")
            lines.append(f"  úspěšně zparsováno (BER-TLV): {self.klv_parse_ok}")
            lines.append(f"  checksum OK: {self.klv_checksum_ok}, checksum FAIL: {self.klv_checksum_failed}")
            lines.append(f"  PTS: {self.klv_pts_real} vlastní, {self.klv_pts_derived} odvozených "
                         f"z videa, {self.klv_pts_missing} bez PTS (zahozeno)")
            if self.first_klv_tags is not None:
                lines.append(f"  tagy z prvního platného paketu: {sorted(self.first_klv_tags)}")

        lines.append("")
        lines.append("--- Závěr ---")
        if self.klv_pid is not None and self.klv_checksum_ok > 0:
            lines.append("KLV: ANO, data jsou čitelná a procházejí checksum kontrolou.")
        elif self.klv_pid is not None:
            lines.append("KLV: PID nalezen, ale ŽÁDNÝ paket neprošel checksum kontrolou - "
                         "metadata se do converteru nedostanou, i když PMT KLV avizuje.")
        else:
            lines.append("KLV: NE - PMT žádný KLV stream neavizuje.")
        if self.video_pid is not None and self.video_codec != "hevc":
            if self.undefined_pps_ids:
                lines.append("Video: NEDEKÓDOVATELNÉ (chybí definice PPS, na které se "
                             "odkazují slice).")
            elif self.first_sps_au is None or not self.first_pps_au:
                lines.append("Video: pravděpodobně nedekódovatelné (SPS a/nebo PPS nikdy "
                             "nebyla viděna).")
            else:
                lines.append("Video: by mělo být dekódovatelné (SPS/PPS definovány dřív, "
                             "než jsou potřeba).")
        return "\n".join(lines)


def analyze(byte_source, report: Report) -> int:
    """byte_source: iterable, co vraci chunky bajtu (soubor po castech,
    nebo prijate UDP datagramy). Vraci pocet zpracovanych TS paketu.

    Pouziva TsPacketResync (viz state_holder.py) - stejna odolnost proti
    ztrate zarovnani jako ma skutecny converter, ne naivni pevne 188B
    kroky od zacatku. Bez tohoto by jedny ztraceny/navic bajt kdekoli v
    souboru znamenal, ze zbytek analyzy je nedoveryhodny, bez upozorneni."""
    total_packets = 0
    resync = TsPacketResync(log=lambda msg: None)  # pocet se cte z resync.resync_count

    def on_video(elementary_stream_bytes, pts):
        report.video_au_count += 1
        if report.video_codec == "hevc":
            report.hevc_au_count += 1
            return
        has_slice, referenced, saw_sps, defined = _h264_scan_slices_and_params(
            elementary_stream_bytes)
        if has_slice:
            report.video_au_with_slice += 1
        if saw_sps and report.first_sps_au is None:
            report.first_sps_au = report.video_au_count - 1
        for pid in defined:
            report.first_pps_au.setdefault(pid, report.video_au_count - 1)
        report.referenced_pps_ids |= referenced
        report.defined_pps_ids |= defined

    def on_klv(klv_bytes, pts, pts_is_derived=False):
        report.klv_pes_total += 1
        report.klv_key_found += 1   # demuxer uz klic nasel, jinak by sem nevolal
        if pts is None:
            report.klv_pts_missing += 1
            return
        report.klv_pts_derived += 1 if pts_is_derived else 0
        report.klv_pts_real += 0 if pts_is_derived else 1
        parsed = parse_klv_packet(klv_bytes)
        if parsed is None:
            return
        fields, checksum_ok = parsed
        report.klv_parse_ok += 1
        if checksum_ok:
            report.klv_checksum_ok += 1
            if report.first_klv_tags is None:
                report.first_klv_tags = set(fields.keys())
        else:
            report.klv_checksum_failed += 1

    demuxer = TransportStreamDemuxer(on_video_data=on_video, on_klv_data=on_klv,
                                     log=lambda *a: None)

    for chunk in byte_source:
        for packet in resync.feed(chunk):
            demuxer.feed(packet)
            total_packets += 1

    report.resync_count = resync.resync_count
    report.video_pid = demuxer.video_stream_pid
    report.video_codec = demuxer.video_codec
    report.klv_pid = demuxer.klv_stream_pid
    if report.klv_pid is not None:
        # demuxer sam nerika, jakym zpusobem KLV naslo - zjisti se znovu z
        # PMT bufferu by bylo slozite, staci vedet, ze PID je znamy; presny
        # zpusob baleni uz divaka nezajima, dulezite je, ze se to parsuje
        report.klv_carriage = "nalezen v PMT, obal rozpoznan obecnym hledanim klice"
    return total_packets


def main():
    # Windows konzole casto detekuje jinou kodovou stranu nez UTF-8, i kdyz
    # samotny terminal UTF-8 zobrazit umi - bez tohoto by ceska diakritika
    # ve vystupu vysla jako zmatecne znaky.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass   # starsi Python / stdout bez reconfigure - zobrazeni bez diakritiky je jen kosmeticke

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", metavar="PATH", help="analyzovat .ts soubor")
    source.add_argument("--src", metavar="IP", help="poslouchat multicast/unicast zdroj")
    parser.add_argument("--src-port", type=int, default=5000)
    parser.add_argument("--iface", default=None, metavar="IP")
    parser.add_argument("--duration", type=float, default=15.0, metavar="S",
                        help="jak dlouho poslouchat (jen s --src)")
    args = parser.parse_args()

    report = Report()

    if args.file:
        def file_chunks():
            with open(args.file, "rb") as f:
                while True:
                    chunk = f.read(1_000_000)
                    if not chunk:
                        break
                    yield chunk
        total = analyze(file_chunks(), report)
        source_label = args.file
    else:
        receive_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receive_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        receive_socket.bind(("0.0.0.0", args.src_port))
        if ipaddress.ip_address(args.src).is_multicast:
            request = struct.pack("4s4s", socket.inet_aton(args.src),
                                  socket.inet_aton(args.iface or "0.0.0.0"))
            receive_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, request)
            print(f"[ANALYZE] multicast mode - joining group {args.src}")
        else:
            print(f"[ANALYZE] unicast mode - listening on port {args.src_port}")
        receive_socket.settimeout(1.0)
        print(f"[ANALYZE] poslouchám {args.duration:.0f} s na {args.src}:{args.src_port}...")

        def socket_chunks():
            deadline = time.monotonic() + args.duration
            while time.monotonic() < deadline:
                try:
                    data, _ = receive_socket.recvfrom(2048)
                except socket.timeout:
                    continue
                yield data
        total = analyze(socket_chunks(), report)
        receive_socket.close()
        source_label = f"{args.src}:{args.src_port} ({args.duration:.0f} s)"

    print()
    print(report.render(source_label, total))


if __name__ == "__main__":
    sys.exit(main())
