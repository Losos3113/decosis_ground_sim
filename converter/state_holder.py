#!/usr/bin/env python3

import argparse
import collections
import ipaddress
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from klv_core import KLVStateStore, extract_pes_pts, UAS_LOCAL_SET_KEY, pts_delta, PTS_CLOCK_HZ
from ground_link import GroundSimLink
from snapshot_sender import Destination, SnapshotSender
from snapshot_store import SnapshotStore
from unified import SCHEMA_VERSION, build_unified_metadata

# v3 (viz DOCUMENTATION.md "Wire format history"): SNAPSHOT kanal uz neni
# UDP datagram s binarni hlavickou, ale HTTP POST (multipart) na REST API -
# viz NAVRH_rest_output.md a snapshot_sender.py. Zaroven uz metadata nejsou
# jen KLV: slucuji se s PX4 telemetrii z pozemniho simulatoru do jednoho
# tvaru (unified.py, schema DECOSIS-SNAP-1).
#
# Z v2 zustava beze zmeny to podstatne: snimek a KLV metadata jsou parovane
# pres PTS (stejny PTS jako ve ST 1402 video/KLV PES), ne pres tiker na
# hodinach pozemni stanice - u KLV poli proto `age_s` znamena "o kolik je
# hodnota starsi nez SNIMEK". U poli ze simulatoru ma `age_s` jiny vyznam
# (wall-clock od stazeni), proto si kazde pole nese `age_ref`.

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47


class TsPacketResync:
    """Rozdeli proud bajtu na platne 188B TS pakety, s odolnosti proti
    ztrate zarovnani.

    Puvodni kod (`for offset in range(0, len(data) - 187, 188)`) slepe
    predpokladal, ze vstup je OD PRVNIHO BAJTU dokonale zarovnany na 188 B
    navzdy - kdyz se to jednou posune (chybejici/navic bajt v zachycenem
    souboru, poskozeny zaznam, cokoliv), `TransportStreamDemuxer.feed()`
    sice spravne odmita jednotlive nezarovnane "pakety" (`ts_packet[0] !=
    0x47`), ale NIC v cestovce samotne se znovu nezarovna - zbytek vstupu
    od tohoto bodu je tise necitelny, bez jedine chybove hlasky. Overeno na
    realne nahravce (viz NALEZY_2026 ... "vsb.ts"): zarovnani se po pevnych
    188B krocich rozpadlo uz po ~14 paketech.

    Tahle trida dela presne to, co realne TS demuxery (vcetne ffmpeg):
    synchronizaci POTVRZUJE - 0x47 nejen na jedne pozici, ale i o 188 B a
    376 B dal (nahodny bajt 0x47 v obsahu PAKETU by jinak mohl zpusobit
    falesne pozitivni zarovnani) - a kdyz se behem provozu ztrati (dalsi
    "paket" nezacina na 0x47), posune se o jeden bajt a hleda znovu, misto
    aby zbytek vstupu jen tise zahazovala.

    Pouziti (lze volat feed() opakovane - soubor po castech, UDP
    datagramy...; nezpracovany zbytek se drzi mezi volanimi):

        resync = TsPacketResync(log=print)
        for packet in resync.feed(chunk):
            demuxer.feed(packet)
    """

    def __init__(self, log=print, summary_interval_s: float = 10.0):
        self.log = log
        self._buffer = bytearray()
        self._synced = False
        self.resync_count = 0
        # Prvni ztrata synchronizace se hlasi hned, dalsi jen souhrnne
        # nejvys 1x za summary_interval_s - u souboru s pravidelnou vsuvkou
        # (vsb.ts z VLC: 233 B kazdych par paketu) by jinak resync zaplnil
        # log stovkami radku. Celkovy pocet je v resync_count a v
        # zaverecnem souhrnu state_holder.py.
        self._summary_interval_s = summary_interval_s
        self._last_report_at = None
        self._unreported_count = 0
        self._unreported_bytes = 0

    def _report_resync(self, skipped: int) -> None:
        now = time.monotonic()
        if self._last_report_at is None:
            self.log(f"[DEMUX] WARNING: ztracena synchronizace TS paketu - "
                     f"{skipped} B preskoceno pred novym zarovnanim (dalsi se "
                     f"hlasi souhrnne, nejvys 1x za {self._summary_interval_s:g} s)")
            self._last_report_at = now
            return
        self._unreported_count += 1
        self._unreported_bytes += skipped
        elapsed = now - self._last_report_at
        if elapsed >= self._summary_interval_s:
            self.log(f"[DEMUX] WARNING: ztracena synchronizace TS paketu "
                     f"{self._unreported_count}x za poslednich {elapsed:.0f} s "
                     f"({self._unreported_bytes} B preskoceno, celkem "
                     f"{self.resync_count}x)")
            self._last_report_at = now
            self._unreported_count = 0
            self._unreported_bytes = 0

    def feed(self, chunk: bytes):
        self._buffer += chunk
        packets = []
        while True:
            if not self._synced:
                confirm_span = 2 * TS_PACKET_SIZE
                found = None
                search_limit = len(self._buffer) - confirm_span
                for i in range(max(0, search_limit)):
                    if (self._buffer[i] == TS_SYNC_BYTE
                            and self._buffer[i + TS_PACKET_SIZE] == TS_SYNC_BYTE
                            and self._buffer[i + confirm_span] == TS_SYNC_BYTE):
                        found = i
                        break
                if found is None:
                    # Jeste neni dost dat na potvrzeni (potrebujeme 3 pakety
                    # dopredu) - pockej na dalsi feed(), jen omez neomezeny
                    # rust bufferu, kdyby vstup byl dlouho necitelny
                    if len(self._buffer) > 8 * TS_PACKET_SIZE:
                        del self._buffer[:-confirm_span]
                    break
                if found > 0:
                    self.resync_count += 1
                    self._report_resync(found)
                del self._buffer[:found]
                self._synced = True
            if len(self._buffer) < TS_PACKET_SIZE:
                break
            if self._buffer[0] != TS_SYNC_BYTE:
                self._synced = False
                continue
            packets.append(bytes(self._buffer[:TS_PACKET_SIZE]))
            del self._buffer[:TS_PACKET_SIZE]
        return packets


class TransportStreamDemuxer:

    def __init__(self, on_video_data, on_klv_data, log=print):
        self.on_video_data = on_video_data
        self.on_klv_data = on_klv_data
        self.log = log
        self.program_map_pid = None
        self.klv_stream_pid = None
        self.video_stream_pid = None
        self.video_codec = None

        self._klv_buffer = None
        self._klv_expected_length = None
        self._klv_continuity_counter = None
        self._warned_unknown_klv_wrapper = False

        self._video_buffer = None
        self._video_pes_started = False
        self._video_continuity_counter = None

        # PTS posledniho ZACATEHO video PES. Slouzi jako nahradni cas pro KLV
        # pakety, ktere vlastni PTS nenesou - viz _finish_klv_pes_packet.
        self.last_video_pts = None
        self._warned_derived_klv_pts = False

    def _parse_program_association_table(self, payload):
        pointer_field = payload[0]
        section = payload[1 + pointer_field:]
        if len(section) < 8 or section[0] != 0x00:
            return
        section_length = ((section[1] & 0x0F) << 8) | section[2]
        program_entries = section[8:3 + section_length - 4]
        # Prvni PMT ze skutecneho programu. Zaznam s program_number 0 neni
        # program, ale odkaz na NIT (ISO 13818-1 2.4.4.3) - spousta
        # profesionalnich enkoderu ho dava v PAT na prvni misto. Puvodne
        # se bral bez kontroly prvni zaznam, takze u takoveho zdroje by se
        # jako PMT cetla NIT, PMT by se nikdy nenasla a converter by tise
        # nedelal nic.
        for offset in range(0, len(program_entries) - 3, 4):
            program_number = (program_entries[offset] << 8) | program_entries[offset + 1]
            if program_number == 0:
                continue
            pmt_pid = ((program_entries[offset + 2] & 0x1F) << 8) | program_entries[offset + 3]
            if self.program_map_pid != pmt_pid:
                self.program_map_pid = pmt_pid
                self.log(f"[DEMUX] PMT PID 0x{pmt_pid:04X} (program {program_number})")
            return

    @staticmethod
    def _registration_identifier(descriptors: bytes):
        """format_identifier z registration descriptoru (tag 0x05, ISO 13818-1).
        U KLV nesenem jako soukroma data je to jedine, co ten stream odlisi
        od jakychkoli jinych soukromych dat na stejnem stream_type."""
        position = 0
        while position + 2 <= len(descriptors):
            descriptor_tag = descriptors[position]
            descriptor_length = descriptors[position + 1]
            if descriptor_tag == 0x05 and descriptor_length >= 4:
                return descriptors[position + 2:position + 6]
            position += 2 + descriptor_length
        return None

    def _parse_program_map_table(self, payload):
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
            descriptors = section[offset + 5:offset + 5 + es_info_length]

            # KLV se v STANAG 4609 streamech vyskytuje ve DVOU podobach a obe
            # jsou podle MISB ST 1402 legalni:
            #   0x15 = "metadata carried in PES packets" (SMPTE 336M),
            #   0x06 = "PES se soukromymi daty" + registration descriptor
            #          s format_identifier "KLVA".
            # Puvodne se hledalo jen 0x15, coz je tvar, ktery produkuje nas
            # vlastni vysilac a testovaci sample.ts. VSECHNY skutecne nahravky
            # v "DECOSIS VIDEA/VIDEA_TS" ale pouzivaji 0x06+KLVA, takze se u
            # nich KLV stream vubec nenasel a snimky odchazely s nula tagy -
            # bez jedine chybove hlasky, protoze "zadne KLV" je legitimni stav.
            # Obe podoby se navic lisi i vnitrnim obalem, viz _klv_from_pes_payload.
            is_klv = stream_type == 0x15 or (
                stream_type == 0x06 and self._registration_identifier(descriptors) == b"KLVA")

            if is_klv and self.klv_stream_pid != elementary_stream_pid:
                self.klv_stream_pid = elementary_stream_pid
                carriage = ("stream_type 0x15, metadata in PES" if stream_type == 0x15
                            else "stream_type 0x06, private PES, registration KLVA")
                self.log(f"[DEMUX] KLV PID 0x{elementary_stream_pid:04X} ({carriage})")
            elif stream_type in (0x1B, 0x24) and self.video_stream_pid != elementary_stream_pid:
                self.video_stream_pid = elementary_stream_pid
                self.video_codec = "h264" if stream_type == 0x1B else "hevc"
                self.log(f"[DEMUX] video PID 0x{elementary_stream_pid:04X} "
                         f"({self.video_codec}, stream_type 0x{stream_type:02X})")
            offset += 5 + es_info_length

    # Kolik bajtu pred klicem je jeste ochotny prohledat - staci na znamé
    # varianty (0x15 s 5B AU cell hlavickou, 0x06 bez ni) a s rezervou i na
    # nezname vendor-specific obaly, ale neprohledava cely paket naslepo.
    _KLV_KEY_SEARCH_WINDOW = 32

    def _klv_from_pes_payload(self, payload: bytes):
        """Vyrizne KLV sadu z tela PES paketu.

        Puvodne se zkousely dva PEVNE offsety podle stream_type z PMT (0 pro
        0x06, 5 pro 0x15 - viz _parse_program_map_table) - v praxi se ale
        ukazalo, ze pocet hlavickovych bajtu pred klicem (AU cell hlavicka,
        ruzne vendor-specific obaly...) se stream_type sam o sobe neurcuje
        spolehlive; realne nahravky to kombinuji jinak, nez PMT tvrdi.

        Misto hadani konkretniho obalu se tedy hleda primo univerzalni
        16bajtovy klic UAS Local Set kdekoli v malem okne na zacatku PES
        tela - funguje pro libovolny pocet hlavickovych bajtu pred nim, ne
        jen pro dve zname varianty. Delku sady uz dopocita BER parser v
        parse_klv_packet(), takze ocasek za koncem sady (padding, zbytek
        PES tela) nevadi - vraci se vse od klice dal, ne presne vyrizly
        usek."""
        index = payload.find(UAS_LOCAL_SET_KEY, 0,
                              self._KLV_KEY_SEARCH_WINDOW + len(UAS_LOCAL_SET_KEY))
        if index < 0:
            return None
        return payload[index:]

    def _finish_klv_pes_packet(self):
        packet = bytes(self._klv_buffer)
        self._klv_buffer = self._klv_expected_length = None
        if len(packet) < 9 or packet[:3] != b"\x00\x00\x01":
            return
        payload = packet[9 + packet[8]:]
        if len(payload) < 21:
            return
        klv_bytes = self._klv_from_pes_payload(payload)
        if klv_bytes is None:
            # Bez tohohle by neznamy obal znamenal tiche "zadne metadata" -
            # presne ta chyba, kvuli ktere se 0x06+KLVA dlouho nevedelo.
            if not self._warned_unknown_klv_wrapper:
                self._warned_unknown_klv_wrapper = True
                self.log(f"[DEMUX] WARNING: v PES na KLV PID nenalezen klic UAS "
                         f"Local Set (prvni bajty: {payload[:8].hex()}) - metadata "
                         f"se z tohohle streamu nedaji cist (hlasi se jednou)")
            return
        pts = extract_pes_pts(packet)
        pts_is_derived = False
        if pts is None:
            # Nektere skutecne STANAG 4609 nahravky nesou KLV PES BEZ PTS
            # (PTS_DTS_flags == 00), i kdyz kazdy video PES ho ma - overeno na
            # nahravkach v "DECOSIS VIDEA/VIDEA_TS". KLVStateStore takovy paket
            # zahodi, protoze ho nema kam umistit na casovou osu, a snimky pak
            # odchazeji bez metadat. Nahradou je cas naposledy zacateho video
            # PES: v STANAG 4609 je KLV multiplexovane tesne u snimku, ktery
            # popisuje, takze chyba je rádove snimek, ne sekundy.
            #
            # NENI to tiche: priznak jde az do metadat (sources.klv.timing a
            # age_ref u kazdeho KLV pole), aby prijemce poznal odvozeny cas od
            # casu, ktery stream skutecne nesl.
            pts = self.last_video_pts
            pts_is_derived = True
            if not self._warned_derived_klv_pts:
                self._warned_derived_klv_pts = True
                self.log("[DEMUX] KLV PES nenese vlastni PTS - prirazuje se cas "
                         "posledniho video PES (v metadatech oznaceno jako "
                         "odvozene; hlasi se jednou)")
        if pts is None:
            return        # jeste nedorazil zadny video PES, nemame co priradit
        self.on_klv_data(klv_bytes, pts, pts_is_derived)

    def _finish_video_pes_packet(self):
        if self._video_buffer is None:
            return
        packet = bytes(self._video_buffer)
        self._video_buffer = None
        if len(packet) < 9 or packet[:3] != b"\x00\x00\x01":
            return
        elementary_stream_bytes = packet[9 + packet[8]:]
        if elementary_stream_bytes:
            self.on_video_data(elementary_stream_bytes, extract_pes_pts(packet))

    def feed(self, ts_packet: bytes) -> None:
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
            payload_offset = 5 + ts_packet[4]
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

        if self.klv_stream_pid is not None and packet_id == self.klv_stream_pid:
            if payload_unit_start:
                if self._klv_buffer is not None:
                    self.log("[DEMUX] WARNING: incomplete KLV PES dropped (lost TS packet?)")
                self._klv_buffer = bytearray(payload)
                self._klv_continuity_counter = continuity_counter
                if len(self._klv_buffer) >= 6:
                    pes_length = struct.unpack(">H", self._klv_buffer[4:6])[0]
                    self._klv_expected_length = 6 + pes_length if pes_length else None
            elif self._klv_buffer is not None:
                expected_counter = (self._klv_continuity_counter + 1) & 0xF
                if continuity_counter != expected_counter:
                    self.log(f"[DEMUX] WARNING: continuity counter gap on KLV PID - "
                             f"PES in progress dropped")
                    self._klv_buffer = self._klv_expected_length = None
                else:
                    self._klv_continuity_counter = continuity_counter
                    self._klv_buffer += payload
            if self._klv_expected_length is not None and self._klv_buffer is not None \
                    and len(self._klv_buffer) >= self._klv_expected_length:
                self._finish_klv_pes_packet()
            return

        if self.video_stream_pid is not None and packet_id == self.video_stream_pid:
            if payload_unit_start:
                self._finish_video_pes_packet()
                self._video_buffer = bytearray(payload)
                self._video_pes_started = True
                self._video_continuity_counter = continuity_counter
                # PTS je v PES hlavicce, ktera cela dorazila uz v tomhle prvnim
                # TS paketu - zapsat ho hned, ne az se PES uzavre (to se deje
                # az prichodem DALSIHO snimku), aby KLV paket bez vlastniho PTS
                # zdedil cas snimku, ktery mu predchazi nejblize
                video_pts = extract_pes_pts(payload)
                if video_pts is not None:
                    self.last_video_pts = video_pts
            elif self._video_pes_started and self._video_buffer is not None:
                # Stejny princip jako u KLV CC kontroly vyse (byl jen tam,
                # video vubec nekontrolovano - viz diskuze o continuity
                # counteru): ztraceny TS paket uprostred video PES by jinak
                # tise pokracoval ve skladani access unity s dirou v datech
                # a poslal by ji takovou do ffmpeg, misto aby se cela
                # zahodila s viditelnym varovanim. U zdraveho streamu (CC
                # nikdy neskoci) se tahle vetev nikdy neaktivuje - stejne
                # bezrizikove jako TsPacketResync.
                expected_counter = (self._video_continuity_counter + 1) & 0xF
                if continuity_counter != expected_counter:
                    self.log("[DEMUX] WARNING: continuity counter gap on video PID - "
                             "access unit in progress dropped")
                    self._video_buffer = None
                    self._video_pes_started = False
                else:
                    self._video_continuity_counter = continuity_counter
                    self._video_buffer += payload
            return


def _contains_vcl_nal(access_unit: bytes, codec: str) -> bool:
    """True, pokud access unit obsahuje aspon jeden VCL (slice) NAL - tedy
    neco, co ffmpeg skutecne vydekoduje jako novy snimek. Bez tohohle by
    access unit slozena jen z SEI/AUD/SPS/PPS (zadna slice) tise posunula
    nase pocitadlo snimku, aniz by u ffmpeg vznikl odpovidajici vystup -
    viz komentar u volani v feed()."""
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
        if codec == "h264":
            if header_at >= length:
                break
            nal_type = access_unit[header_at] & 0x1F
            if 1 <= nal_type <= 5:    # slice types incl. partitions A/B/C (2-4)
                return True
        else:   # hevc: 2bajtova NAL hlavicka, typ v bitech 1-6 prvniho bajtu
            if header_at + 1 >= length:
                break
            nal_type = (access_unit[header_at] >> 1) & 0x3F
            if nal_type <= 31:         # VCL NAL types 0-31 (ITU-T H.265 7.4.2)
                return True
        position = header_at + 1
    return False


def _contains_sps(access_unit: bytes, codec: str) -> bool:
    """True, pokud access unit obsahuje SPS NAL (H.264 typ 7, HEVC typ 33) -
    tedy bod, od ktereho ma smysl zacit dekodovat. Viz VideoFrameDecoder.feed()
    a _seen_sps."""
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
        if codec == "h264":
            if access_unit[header_at] & 0x1F == 7:
                return True
        elif (access_unit[header_at] >> 1) & 0x3F == 33:
            return True
        position = header_at + 1
    return False


def _h264_rbsp_prefix(nal_payload: bytes, max_bytes: int = 8) -> bytes:
    """Prvnich `max_bytes` bajtu RBSP (po odstraneni emulation-prevention
    00 00 03 -> 00 00) z NAL payloadu za hlavickovym bajtem. Staci na prvni
    dve exp-golomb pole slice hlavicky (first_mb_in_slice, slice_type) -
    nema smysl de-emulovat cely NAL jen kvuli dvema cislum."""
    out = bytearray()
    zero_run = 0
    for byte in nal_payload:
        if zero_run >= 2 and byte == 0x03:
            zero_run = 0
            continue
        out.append(byte)
        zero_run = zero_run + 1 if byte == 0 else 0
        if len(out) >= max_bytes:
            break
    return bytes(out)


class _BitReader:
    """Minimalni MSB-first bitovy ctecka pro exp-golomb pole H.264 slice
    hlavicky - nic vic (zadne SPS/PPS kontexty, zadne CABAC)."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read_bit(self) -> int:
        byte_index = self.pos // 8
        if byte_index >= len(self.data):
            raise IndexError("prectena vsechna data, bit chybi")
        bit = (self.data[byte_index] >> (7 - self.pos % 8)) & 1
        self.pos += 1
        return bit

    def read_ue(self) -> int:
        """Exp-Golomb ue(v), ITU-T H.264 9.1."""
        leading_zeros = 0
        while self.read_bit() == 0:
            leading_zeros += 1
            if leading_zeros > 32:
                raise ValueError("ue(v) delsi nez 32 bitu - poskozena data?")
        value = 0
        for _ in range(leading_zeros):
            value = (value << 1) | self.read_bit()
        return (1 << leading_zeros) - 1 + value


def _h264_slice_is_b(nal_payload_after_header: bytes) -> bool:
    """True, pokud slice_type teto slice NAL (typ 1/5) znamena B-snimek.
    Cte jen prvni dve pole slice hlavicky (first_mb_in_slice, slice_type) -
    ta jsou VZDY na zacatku bez ohledu na obsah PPS/SPS, takze je nepotrebujeme
    znat. slice_type 0..4 = P,B,I,SP,SI; 5..9 totez + priznak "vsechny slice
    v obrazku stejneho typu" (ITU-T H.264 7.4.3 Table 7-6) - odtud `% 5`."""
    rbsp = _h264_rbsp_prefix(nal_payload_after_header)
    reader = _BitReader(rbsp)
    reader.read_ue()              # first_mb_in_slice, zahozeno
    slice_type = reader.read_ue()
    return slice_type % 5 == 1


def _h264_contains_b_slice(access_unit: bytes) -> bool:
    """Projde access unit a vrati True, pokud nektera slice NAL (typ 1 ci 5)
    je B-snimek. Pouzito jen pro jednorazove varovani (viz VideoFrameDecoder)
    - neni soucasti "obsahuje vubec obraz" rozhodnuti v _contains_vcl_nal."""
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
        if nal_type in (1, 5):
            try:
                if _h264_slice_is_b(access_unit[header_at + 1:header_at + 1 + 8]):
                    return True
            except (IndexError, ValueError):
                pass   # poskozeny/useknuty NAL - jen pro diagnostiku, nehazet dal
        position = header_at + 1
    return False


def _h264_scan_parameter_sets(access_unit: bytes):
    """Vrati [('SPS', None), ('PPS', pps_id), ...] pro kazdou SPS/PPS NAL
    jednotku v access unit.

    Diagnostika pro pripad, kdy ffmpeg hlasi "non-existing PPS N referenced"
    a nikdy se nevzpamatuje (na rozdil od prehravek z transmitter.py, ktery
    nastavuje enkoderu repeat-headers=1, takze SPS/PPS prijdou znovu pred
    KAZDYM klicovym snimkem - viz DOCUMENTATION.md). U ciziho zdroje, ktery
    SPS/PPS neopakuje, je potreba vedet, jestli converteru vubec DORAZILY
    (a s jakym PPS ID), nebo jestli slice odkazuji na PPS, ktere sem nikdy
    neprisla - to se bez tohoto skenu nedalo rozlisit. Jen H.264 (stejne
    jako _h264_contains_b_slice - HEVC PPS parsovani potrebuje jiny format
    a neni tu implementovane)."""
    found = []
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
            found.append(("SPS", None))
        elif nal_type == 8:
            try:
                rbsp = _h264_rbsp_prefix(access_unit[header_at + 1:header_at + 1 + 8])
                pps_id = _BitReader(rbsp).read_ue()
            except (IndexError, ValueError):
                pps_id = None   # poskozeny/useknuty NAL - jen diagnostika
            found.append(("PPS", pps_id))
        position = header_at + 1
    return found


class FrameRateDetector:
    """Odvodi skutecnou snimkovou frekvenci vstupniho enkoderu z rozestupu
    PTS mezi po sobe jdoucimi OBRAZKOVYMI access units (ne ze jmenovite
    zadaneho --input-fps).

    Duvod: cely mechanismus parovani PTS<->JPEG v VideoFrameDecoder zavisi
    na tom, ze `select=not(mod(n,K))` ve ffmpeg vybira presne K-ty
    DEKODOVANY snimek - a K musi odpovidat REALNE frekvenci zdroje, jinak
    (viz DOCUMENTATION.md "Snapshot cadence") vychazi jiny pocet snimku za
    sekundu, nez se ceka (overeno v praxi: cizi stream na jine fps nez
    predpokladanych 30 produkoval snimky ~2x casteji). U vlastniho
    transmitter.py se --fps zna predem, u cizich zdroju (realna kamera,
    Haivision...) se jen hada - a hadani je presne to, cemu se chceme
    vyhnout.

    Mereni: nasbira PTS prvnich `sample_target` obrazkovych access units,
    spocita rozestupy (`pts_delta`, odolne proti 33bit wrapu) a vezme
    MEDIAN - ne prumer, aby ojedinely vypadek/duplicitni PTS nezkresli
    odhad. fps = PTS_CLOCK_HZ / median_rozestup, zaokrouhleno.

    Behem mereni se VSECHNY prichozi access units (i ty bez obrazku -
    SPS/PPS/SEI) musi bufferovat a po zmereni poslat dekoderu ve spravnem
    poradi - jinak by SPS/PPS prisle PRED prvnim snimkem nikdy nedorazily
    do ffmpeg (presne ten problem, co resil resync a PPS diagnostika jinde
    v tomhle souboru)."""

    def __init__(self, sample_target: int = 10, max_buffered: int = 500, log=print):
        self.sample_target = sample_target
        self.max_buffered = max_buffered
        self.log = log
        self._buffered = []          # [(elementary_stream_bytes, pts), ...] VSE, v poradi
        self._picture_pts = []       # jen PTS obrazkovych AU, pro mereni

    def feed(self, elementary_stream_bytes: bytes, pts, codec: str):
        """Prida access unit do bufferu. Vraci zmerenou fps (int), jakmile
        je dost vzorku NEBO byl prekrocen max_buffered (pak padne na default
        30 s varovanim) - jinak None (jeste se ceka)."""
        self._buffered.append((elementary_stream_bytes, pts))
        if pts is not None and _contains_vcl_nal(elementary_stream_bytes, codec):
            self._picture_pts.append(pts)
        if len(self._picture_pts) >= self.sample_target:
            return self._measure()
        if len(self._buffered) >= self.max_buffered:
            self.log(f"[VIDEO] WARNING: auto-detekce fps se za {self.max_buffered} "
                     f"access units nedobrala {self.sample_target} snimku s PTS "
                     f"- padam na vychozich 30 fps (zadej --input-fps rucne, "
                     f"pokud to nesedi)")
            return 30
        return None

    def _measure(self) -> int:
        deltas = sorted(d for d in (pts_delta(b, a) for a, b in
                                    zip(self._picture_pts, self._picture_pts[1:]))
                        if d > 0)
        if not deltas:
            self.log("[VIDEO] WARNING: auto-detekce fps nenasla zadny kladny "
                     "rozestup PTS mezi snimky - padam na vychozich 30 fps")
            return 30
        median_delta = deltas[len(deltas) // 2]
        fps = max(1, round(PTS_CLOCK_HZ / median_delta))
        self.log(f"[VIDEO] fps auto-detekovana ze streamu: {fps} "
                 f"(medianovy rozestup PTS {median_delta} z {len(deltas)} vzorku; "
                 f"prepsat lze pomoci --input-fps)")
        return fps

    def drain(self):
        """Vrati vsechny bufferovane access units v poradi, jak prisly -
        volat JEDNOU po tom, co feed() vrati zmerenou fps, pred prepnutim
        na primé krmeni."""
        buffered, self._buffered = self._buffered, []
        return buffered


class VideoFrameDecoder:
    """Dekoduje H.264/HEVC access unity do JPEG, priblizne 1 vystupni snimek
    na kazdych `input_fps` vstupnich, a k danemu vystupu vraci PTS presne
    te access unity, ze ktere vznikl.

    METODA PAROVANI: zamerne NEpouziva ffmpegovo vlastni casove razitkovani
    (filtr `fps=1`) - to vychazi z PTS, ktere si ffmpeg u syroveho
    elementarniho streamu (bez vlastnich casovych znacek) sam DOMYSLI z
    --input-fps, a chovani teto heuristiky se muze mezi verzemi ffmpegu
    lisit. Misto toho `select=not(mod(n\\,input_fps))` vybira vyhradne podle
    POCTU vstupnich snimku (0, input_fps, 2*input_fps, ...) - jednoduche,
    zdokumentovane, na verzi nezavisle chovani ffmpegu - a tato trida si
    sama pamatuje PTS kazde access unity, kterou do ffmpegu poslala, podle
    stejneho poradoveho cisla. N-ty vystupni JPEG tak podle definice
    pochazi z access unity cislo N * input_fps, jejiz skutecny PTS (z PES
    hlavicky prijate ze site, ne domysleny ffmpegem) uz mame k dispozici.

    `-vsync 0` (a.k.a. -fps_mode passthrough) je nutny spolu se `select`,
    jinak by ffmpeg sam snimky duplikoval/zahazoval, aby dosahl nejakeho
    "rozumneho" vystupniho tempa - a tim by tiše rozbil parovani podle poctu
    vyse.

    OVERIT NA REALNEM FFMPEGU (tady zatim nebylo k dispozici): ze
    `select=not(mod(n\\,FPS))` + `-vsync 0` opravdu vydá presne kazdy
    FPS-ty vstupni snimek, v poradi, bez duplicitnich vystupu - je to
    bezny a zdokumentovany idiom, ale empiricky neoverovano v tomto repu."""

    def __init__(self, codec: str, input_fps: int, jpeg_quality: int = 4,
                 on_frame_ready=None, log=print, pts_horizon_s: float = 30.0,
                 frames_per_s: float = 2.0):
        self.codec = codec
        input_format = "h264" if codec == "h264" else "hevc"
        # Kolikaty dekodovany snimek se ma vybrat. Dekoduje se zamerne
        # ~2x rychleji, nez se odesila (odesilani je pevny 1 s takt v
        # SnapshotSender): kdyby dekoder vyrabel presne 1 snimek/s, staci
        # maly fazovy posun proti taktu a v nektere sekunde by cerstvy
        # snimek chybel - misto nej by odesel heartbeat a v dalsi sekunde
        # by se jeden snimek zahodil jako prebytecny.
        self.select_every = max(1, round(input_fps / frames_per_s))
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                  "-probesize", "32", "-analyzeduration", "0",
                  "-f", input_format, "-r", str(input_fps), "-i", "pipe:0",
                  "-vf", f"select=not(mod(n\\,{self.select_every}))",
                  "-vsync", "0",
                  "-f", "image2pipe", "-vcodec", "mjpeg",
                  "-q:v", str(jpeg_quality), "pipe:1"]
        self.input_fps = input_fps
        self.on_frame_ready = on_frame_ready
        self.log = log
        # Puvodne pevnych "4 * input_fps" (4 s) - spolehalo na to, ze
        # dekoderova latence je nanejvys par snimku. U hodne sumaveho
        # vstupu (hodne ztrat TS synchronizace - viz TsPacketResync) to
        # neplati: kazda tise chybne rozpoznana "obrazkova" access unit
        # (nahodne bajty po resyncu, co vypadaji jako slice NAL) posune
        # _fed_au_count rychleji, nez reálně pribyvaji snimky, a okno se
        # zavre driv, nez ffmpeg stihne odpovedet - PTS zmizi, snimek se
        # zahodi (viz "[VIDEO] WARNING: no PTS for output frame"). Parametr
        # je proto konfigurovatelny, s mnohem stedrejsim vychozim oknem;
        # pamet navic stoji levne (slovnik par tisic intu).
        self._pts_horizon_au = max(1, int(pts_horizon_s * input_fps))
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._lock = threading.Lock()
        self._latest_jpeg_bytes = None
        self._frame_count = 0          # vystupnich snimku dosud vyrobenych
        self._fed_au_count = 0         # access unit poslanych do ffmpeg stdin
        self._pts_by_au_index = {}     # au index -> PTS (nebo None)
        # dekodovaci chyby pri ztrate paketu by jinak zaplavily log; drzi se
        # jen posledni chvost a vypisuje az pri padu (viz exit_report)
        self._stderr_tail = collections.deque(maxlen=20)
        self._reader_failed = False
        self._warned_b_frame = False   # viz _h264_contains_b_slice nize
        self._seen_param_sets = set()  # {('SPS', None), ('PPS', id), ...} - viz _h264_scan_parameter_sets
        self._seen_sps = False         # viz zacatek feed()
        self.dropped_before_sps = 0
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._stderr_thread.start()

    def has_exited(self) -> bool:
        return self.process.poll() is not None

    def reader_alive(self) -> bool:
        return self._reader_thread.is_alive() and not self._reader_failed

    def exit_report(self) -> str:
        tail = "\n".join(self._stderr_tail) or "(no stderr output)"
        return f"ffmpeg exited with code {self.process.returncode}, last stderr lines:\n{tail}"

    def feed(self, elementary_stream_bytes: bytes, pts) -> None:
        # Pripojeni k zivemu streamu (nebo restart converteru) temer vzdy
        # padne doprostred GOP. Vsechno pred prvnim SPS/PPS se nedá
        # dekodovat ("non-existing PPS N referenced") a kdyz toho je moc,
        # ffmpeg skonci uplne ("Decode error rate 1 exceeds maximum
        # 0.666667") - overeno restartem state_holder.py behem replaye
        # vsb.ts: jednou SPS prislo na AU #5 a slo to, jindy na AU #53 a
        # ffmpeg umrel s 0 snimky. Navic kazda takova nedekodovatelna
        # obrazkova AU posunula _fed_au_count, ale ne ffmpegove `n` v
        # select filtru - tichy posun parovani PTS<->JPEG (u sample.ts,
        # ktery taky zacina uprostred GOP, o desitky snimku). Proto se do
        # ffmpeg neposila NIC, dokud neprijde prvni access unit se SPS -
        # tak to dela kazdy dekoder ziveho vysilani. Stream, ktery SPS
        # nese hned v prvni access unit, se tim nijak nemeni.
        if not self._seen_sps:
            if not _contains_sps(elementary_stream_bytes, self.codec):
                self.dropped_before_sps += 1
                if self.dropped_before_sps == 1:
                    self.log("[VIDEO] cekam na prvni SPS (pripojeni doprostred "
                             "streamu) - access units do te doby se zahazuji")
                return
            self._seen_sps = True
            if self.dropped_before_sps:
                self.log(f"[VIDEO] SPS nalezeno - dekodovani zacina, predtim "
                         f"zahozeno {self.dropped_before_sps} access units")
        # Pocitat SMI jen access unity se skutecnym obrazovym obsahem (VCL/
        # slice NAL) - overeno na sample.ts, ze ne kazda "access unit", jak
        # ji slozi nas PES demuxer, obsahuje snimek: nektere pripady (napr.
        # SEI poslany jako samostatny PES pred vlastnim snimkem) maji jen
        # SEI/AUD/SPS/PPS a zadnou slice. Takovou jednotku ffmpeg nedekoduje
        # jako novy snimek, takze by NEMELA posunout nase "kolikaty snimek
        # jsme nakrmili" cislo - jinak by parovani PTS<->JPEG bylo mimo uz
        # od druheho snimku dal, ne jen ojedinele.
        is_picture = _contains_vcl_nal(elementary_stream_bytes, self.codec)
        # B-snimky by tise rozbily parovani PTS<->JPEG vyse (dekoder je
        # preradi z decode do display poradi drive, nez se dostanou k
        # select filtru - overeno testem, viz DOCUMENTATION.md "Unstated
        # assumption: no B-frames"). Zatim zadny znamy zdroj v tomhle
        # projektu B-snimky nepouziva, ale kdyby nekdy ano, chceme hlasite
        # varovani misto ticheho spatneho parovani. Jen H.264 - detekce
        # B-slice u HEVC potrebuje znat PPS a neni implementovana.
        if self.codec == "h264" and is_picture and not self._warned_b_frame:
            try:
                if _h264_contains_b_slice(elementary_stream_bytes):
                    self._warned_b_frame = True
                    self.log("[VIDEO] WARNING: B-frame detected in the incoming "
                             "stream - PTS<->JPEG pairing assumes decode order == "
                             "display order (no B-frames); with B-frames present, "
                             "frames may be paired with the WRONG metadata. "
                             "See DOCUMENTATION.md 'Unstated assumption: no "
                             "B-frames'. (this warning prints once)")
            except Exception:
                pass   # jen diagnostika - nikdy nesmi shodit skutecne krmeni ffmpeg
        # Diagnostika pro "ffmpeg hlasi non-existing PPS N referenced a nikdy
        # se nevzpamatuje" (viz _h264_scan_parameter_sets): zaloguje, KDY a
        # JAKE SPS/PPS converter na draty videl, aby se dalo rozlisit "zdroj
        # je neposlal vubec" od "poslal, ale neco je cestou poskodilo/
        # prehledlo". Kazda kombinace (typ, id) se hlasi jen jednou.
        if self.codec == "h264":
            try:
                for kind, pps_id in _h264_scan_parameter_sets(elementary_stream_bytes):
                    key = (kind, pps_id)
                    if key not in self._seen_param_sets:
                        self._seen_param_sets.add(key)
                        label = kind if pps_id is None else f"{kind} id={pps_id}"
                        self.log(f"[VIDEO] {label} poprve na draty - access unit "
                                 f"#{self._fed_au_count}")
            except Exception:
                pass   # jen diagnostika - nikdy nesmi shodit skutecne krmeni ffmpeg
        # PTS se MUSI zapsat pred odeslanim bytu do ffmpeg, ne az po -
        # jinak (overeno testem s rychlym fake ffmpeg) muze čtecí vlakno
        # vyzvednout hotovy JPEG a hledat PTS drive, nez se sem vubec
        # zapise, protoze mezi zapisem do roury a zapisem do slovniku
        # neni zadna synchronizace s tim, jak rychle podprocess odpovi.
        if is_picture:
            with self._lock:
                au_index = self._fed_au_count
                self._fed_au_count += 1
                self._pts_by_au_index[au_index] = pts
                # viz komentar u self._pts_horizon_au v __init__
                horizon = au_index - self._pts_horizon_au
                if horizon > 0:
                    for old_index in [k for k in self._pts_by_au_index if k < horizon]:
                        del self._pts_by_au_index[old_index]
        try:
            self.process.stdin.write(elementary_stream_bytes)
            self.process.stdin.flush()   # bez tohohle by AU mohla sedet v
                                          # Pythonove bufferu misto v ffmpeg,
                                          # a zbytecne prodlouzit latenci
        except (BrokenPipeError, OSError) as error:
            self.log(f"[VIDEO] WARNING: write to ffmpeg failed ({error}) - "
                     f"decoder has likely crashed")

    def get_latest_frame(self):
        with self._lock:
            return self._latest_jpeg_bytes

    def _read_loop(self) -> None:
        try:
            buffer = bytearray()
            while True:
                # read1 misto read: nevyckava na plnych 4096/65536 B, takze
                # se dokoncena JPEG nezdrzi v OS bufferu az do pristiho
                # snimku (puvodni `read()` tenhle posun o cely takt zpusoboval)
                chunk = self.process.stdout.read1(65536)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    start_marker = buffer.find(b"\xFF\xD8")
                    if start_marker < 0:
                        break
                    end_marker = buffer.find(b"\xFF\xD9", start_marker + 2)
                    if end_marker < 0:
                        break
                    jpeg_bytes = bytes(buffer[start_marker:end_marker + 2])
                    del buffer[:end_marker + 2]
                    with self._lock:
                        output_index = self._frame_count
                        self._frame_count += 1
                        expected_au_index = output_index * self.select_every
                        pts = self._pts_by_au_index.pop(expected_au_index, None)
                        self._latest_jpeg_bytes = jpeg_bytes
                    if pts is None:
                        self.log(f"[VIDEO] WARNING: no PTS for output frame {output_index} "
                                 f"(expected access unit #{expected_au_index}) - "
                                 f"frame dropped instead of sent unpaired")
                        continue
                    if self.on_frame_ready is not None:
                        self.on_frame_ready(jpeg_bytes, pts)
        except Exception:
            # bez tohohle by vyjimka (napr. z on_frame_ready/get_snapshot)
            # tiše ukoncila jen tohle vlakno - presne ten puvodni "ticker
            # umrel a nikdo si nevsiml" bug, jen v jinem vlakne
            self.log("[VIDEO] FATAL: frame reader thread crashed:")
            traceback.print_exc()
            self._reader_failed = True

    def _stderr_loop(self) -> None:
        # Puvodne se radky jen sbiraly do _stderr_tail a vypsaly az pri
        # padu (exit_report) - pokud ffmpeg na nejakem vstupu jen tiše
        # nic nevyprodukuje, ale sam neskonci (napr. nezvladne dekodovat
        # obsah, ale pipe zustane otevrena), operator nevidi VUBEC nic,
        # ani kdyz ffmpeg sam hlasi chyby na stderr (-loglevel error, takze
        # tam jsou jen opravdu chybove radky, ne sum). Proto se tu kazdy
        # radek navic rovnou vypisuje, ne jen ulozi pro pozdejsi pad.
        for line in self.process.stderr:
            text = line.decode("utf-8", errors="replace").rstrip()
            self._stderr_tail.append(text)
            if text:
                self.log(f"[FFMPEG] {text}")

    def close(self) -> None:
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            self.process.terminate()


def resolve_uav_in_url(url: str, uav_id: str, label: str) -> str:
    """Dosadi ID letounu do URL misto zastupneho `{uav}`.

    Zakaznik chce mit UAV v ceste, napr.

        TARGET_API_URL=http://10.0.0.5/path/to/{uav}/   ->  /path/to/uav1/

    Zastupny znak, a ne pripojovani na konec: ID pak muze byt kdekoli v ceste
    (treba /uav1/snapshots), je z konfigurace na prvni pohled videt, ze se
    tam neco dosazuje, a URL bez nej zustane presne takova, jak je zapsana.
    """
    if "{uav}" not in url:
        return url
    if not uav_id:
        sys.exit(f"ERROR: {label} obsahuje {{uav}}, ale neni znamo ID letounu "
                 f"(--ground-sim-uav je prazdne)")
    return url.replace("{uav}", uav_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="239.1.1.1")
    parser.add_argument("--src-port", type=int, default=5000)
    parser.add_argument("--iface", default=None, metavar="IP")
    parser.add_argument("--api-url", required=True, metavar="URL",
                        help="REST endpoint, kam se POSTuji snimky (multipart)")
    parser.add_argument("--api-timeout-ms", type=int, default=2000)
    parser.add_argument("--api-token", default=None,
                        help="volitelny Bearer token do hlavicky Authorization")
    # Druhy cil. Pise ho ZAKAZNIK, ne my - proto vlastni jmeno v konfiguraci
    # i v logu: az bude neco padat, musi byt na prvni pohled videt, ci strana
    # je nedostupna. Prazdna URL = cil se nepouzije.
    parser.add_argument("--target-api-url", default=None, metavar="URL",
                        help="druhy REST endpoint (zakaznicky); bez nej se posila jen na --api-url")
    parser.add_argument("--target-api-timeout-ms", type=int, default=2000)
    parser.add_argument("--target-api-token", default=None)
    parser.add_argument("--save-dir", default=None, metavar="DIR",
                        help="kam ukladat .jpg + .json ke kazdemu snimku; bez nej se neuklada")
    parser.add_argument("--save-keep", type=int, default=500, metavar="N",
                        help="drzet jen poslednich N snimku (0 = bez omezeni, uklid na obsluze)")
    parser.add_argument("--ground-sim-url", default=None, metavar="URL",
                        help="zaklad REST API pozemniho simulatoru, napr. "
                             "http://127.0.0.1:8002; bez nej jedou snimky jen s KLV")
    parser.add_argument("--ground-sim-uav", default="uav1")
    parser.add_argument("--ground-sim-poll-s", type=float, default=1.0)
    parser.add_argument("--ground-sim-timeout-ms", type=int, default=1000)
    parser.add_argument("--ground-sim-max-age-s", type=float, default=10.0)
    # Puvodni default 4 byl volby kvuli tomu, aby se zprava vesla pod UDP
    # limit ~65507 B. HTTP tenhle strop nema (NAVRH_rest_output.md sekce 3),
    # takze kvalita se ted volí podle toho, co ma byt na snimku videt.
    parser.add_argument("--input-fps", type=int, default=None, metavar="N",
                        help="snimkova frekvence vstupniho enkoderu, pro "
                             "select=not(mod(n,N)) a vypocet PTS-lookup okna. "
                             "Bez tohoto se ZMERI automaticky z PTS rozestupu "
                             "prvnich snimku streamu (viz FrameRateDetector) - "
                             "nepredpoklada se konkretni hodnota. Zadej jen "
                             "pokud automaticke mereni z nejakeho duvodu "
                             "nesedi (napr. hodne promenlivy framerate).")
    parser.add_argument("--jpeg-quality", type=int, default=2, metavar="1-31")
    parser.add_argument("--pts-horizon-s", type=float, default=30.0, metavar="S",
                        help="jak dlouho (ve stream case) drzet PTS cekajici na "
                             "vystup z ffmpeg, nez se zahodi jako nedohledatelny "
                             "(vychozi 30 s - vetsi rezerva pro sumave vstupy, "
                             "viz VideoFrameDecoder._pts_horizon_au)")
    parser.add_argument("--interval-s", type=float, default=1.0, metavar="S",
                        help="pevny takt odesilani: kazdych S sekund (realny cas) "
                             "odejde presne jedna zprava - nejnovejsi hotovy snimek, "
                             "a kdyz zadny neni, prazdna obalka kind=heartbeat")
    parser.add_argument("--recv-buffer-mb", type=float, default=8.0, metavar="MB",
                        help="o jak velky prijmovy buffer socketu pozadat "
                             "(vychozi 8 MB); viz komentar u setsockopt nize")
    args = parser.parse_args()

    receive_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receive_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Systemovy default prijmoveho bufferu byva desitky az stovky kB - u
    # kamery s vyssim bitratem staci kratke zdrzeni v Pythonu (zapis do
    # ffmpeg, GC...) a jadro zacne UDP datagramy zahazovat. Navenek to
    # vypada jako ztraty TS synchronizace a mezery v continuity counteru.
    # Linux pozadavek tise orizne na net.core.rmem_max, proto se skutecna
    # hodnota cte zpatky a hlasi.
    requested_rcvbuf = int(args.recv_buffer_mb * 1024 * 1024)
    try:
        receive_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, requested_rcvbuf)
    except OSError as error:
        print(f"[MAIN] WARNING: SO_RCVBUF {requested_rcvbuf} B se nepodarilo nastavit ({error})")
    actual_rcvbuf = receive_socket.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    print(f"[MAIN] prijmovy buffer socketu: {actual_rcvbuf // 1024} kB "
          f"(pozadovano {requested_rcvbuf // 1024} kB)")
    if actual_rcvbuf < requested_rcvbuf // 2:
        print(f"[MAIN] WARNING: system buffer orizl - na Linuxu zvysit limit, "
              f"napr. `sysctl -w net.core.rmem_max={requested_rcvbuf}`")
    receive_socket.bind(("0.0.0.0", args.src_port))
    if ipaddress.ip_address(args.src).is_multicast:
        multicast_request = struct.pack("4s4s", socket.inet_aton(args.src),
                                        socket.inet_aton(args.iface or "0.0.0.0"))
        receive_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, multicast_request)
        print(f"[MAIN] multicast mode - joining group {args.src}")
    else:
        print(f"[MAIN] unicast mode - {args.src} is not a multicast address, "
             f"listening on port {args.src_port}")
    receive_socket.settimeout(1.0)

    uav_id = args.ground_sim_uav
    destinations = [Destination("primary",
                                resolve_uav_in_url(args.api_url, uav_id, "--api-url"),
                                timeout_s=args.api_timeout_ms / 1000.0,
                                auth_token=args.api_token)]
    if args.target_api_url:
        destinations.append(Destination(
            "target",
            resolve_uav_in_url(args.target_api_url, uav_id, "--target-api-url"),
            timeout_s=args.target_api_timeout_ms / 1000.0,
            auth_token=args.target_api_token))
    snapshot_store = None
    if args.save_dir:
        try:
            snapshot_store = SnapshotStore(args.save_dir, keep=args.save_keep)
        except OSError as error:
            # Nepristupna slozka (typicky bind mount vyrobeny Dockerem jako
            # root) nesmi shodit prevod - disk je doplnek k odesilani, ne
            # podminka. Hlasi se hlasite a jede se dal bez ukladani.
            print(f"[MAIN] WARNING: do {args.save_dir} nelze zapisovat ({error}) - "
                 f"bezi se BEZ ukladani na disk, odesilani to neovlivni",
                 file=sys.stderr)

    sender = SnapshotSender(destinations, schema=SCHEMA_VERSION, store=snapshot_store)
    for destination in destinations:
        print(f"[MAIN] snapshots -> POST {destination.name}: {destination.url} "
             f"(timeout {destination.timeout_s * 1000:.0f} ms)")
    print(f"[MAIN] schema {SCHEMA_VERSION}, pevne 1 zprava za {args.interval_s:g} s "
         f"na KAZDY cil - snimek, jinak heartbeat"
         + (f"; na disk {args.save_dir}" if args.save_dir else "; bez ukladani na disk"))

    ground_link = None
    if args.ground_sim_url:
        ground_link = GroundSimLink(args.ground_sim_url, args.ground_sim_uav,
                                    poll_interval_s=args.ground_sim_poll_s,
                                    timeout_s=args.ground_sim_timeout_ms / 1000.0,
                                    max_age_s=args.ground_sim_max_age_s)
        ground_link.start()
    else:
        print("[MAIN] --ground-sim-url nezadana - metadata pojedou jen z KLV, "
             "bez PX4 telemetrie")

    klv_state_store = KLVStateStore()
    frame_decoder = None
    # Nepouziva se, pokud je --input-fps zadana rucne - viz handle_video_data
    fps_detector = None if args.input_fps is not None else FrameRateDetector()
    frame_number = 0
    dropped_without_klv = 0
    warned_without_klv = False
    klv_timing_is_derived = False

    def on_frame_ready(jpeg_bytes: bytes, pts: int) -> None:
        # bezi na _read_loop vlakne dekoderu - jen odlozi snimek, odesila se
        # az v pevnem taktu (SnapshotSender, build_snapshot nize). Nic tady
        # nesmi cekat na sit, jinak by se to propsalo do cteni z ffmpeg.
        sender.submit(jpeg_bytes, pts)

    def build_snapshot(jpeg_bytes: bytes, pts: int):
        # Vola SnapshotSender v taktu odesilani pro nejnovejsi odlozeny
        # snimek. Vraci (frame_number, metadata), nebo None, kdyz se snimek
        # poslat nema (pak v teto sekunde odejde heartbeat). Metadata se
        # skladaji az tady, ne pri dekodovani: KLV se hleda podle PTS
        # snimku (KLVStateStore drzi historii), takze vysledek je stejny, a
        # frame_number se prideli jen snimkum, ktere opravdu odchazeji -
        # snimky nahrazene novejsim v ramci jedne sekundy v cislovani
        # mezeru nedelaji (nejsou ztrata, jen zamerne prorezani).
        nonlocal frame_number, dropped_without_klv, warned_without_klv
        klv_snapshot = klv_state_store.get_snapshot(pts)
        ground_by_topic, ground_age_s = (ground_link.latest() if ground_link else (None, None))

        # Snimek bez jedineho KLV tagu se zahazuje - odchazeji jen snimky, ke
        # kterym jsou udaje ze senzoru. Telemetrie ze simulatoru tenhle test
        # NEnahrazuje: popisuje letoun, ne zaber kamery, takze snimek bez KLV
        # by sel ven bez geometrie senzoru, a to je k nicemu.
        #
        # SEM PATRI BUDOUCI ODBOCKA pro snimky bez KLV: az se bude resit, co s
        # nimi (jina fronta, jiny endpoint, jine schema), vetvi se to tady -
        # `frame_number` se zvysuje i u zahozenych, takze mezera v cislovani
        # prijemci rovnou rekne, ze se neco zahodilo.
        if not klv_snapshot:
            dropped_without_klv += 1
            if not warned_without_klv:
                warned_without_klv = True
                print(f"[SNAPSHOT] WARNING: snimek {frame_number} nema zadna KLV "
                     f"metadata - zahazuje se (hlasi se jednou, celkovy pocet je "
                     f"v zaverecnem souhrnu)")
            frame_number += 1
            return None
        metadata = build_unified_metadata(frame_number, pts, klv_snapshot,
                                          ground_by_topic, ground_age_s,
                                          args.ground_sim_uav if ground_link else None,
                                          klv_timing_is_derived)
        ground_note = (f", ground_sim {len(ground_by_topic)} topics "
                      f"({ground_age_s:.1f} s old)" if ground_by_topic else
                      (", no ground_sim data" if ground_link else ""))
        print(f"[{time.strftime('%H:%M:%S')}] [SNAPSHOT] #{frame_number}: JPEG "
             f"{len(jpeg_bytes)} B, {len(klv_snapshot)} KLV tags{ground_note} "
             f"-> {args.api_url}")
        sent_number = frame_number
        frame_number += 1
        return sent_number, metadata

    def handle_klv_packet(klv_bytes: bytes, pts, pts_is_derived: bool = False) -> None:
        # Puvodne "if pts_is_derived: klv_timing_is_derived = True" - jednou
        # nastaveny priznak uz se nikdy nevratil zpet na False, takze jeden
        # KLV paket bez vlastni PTS kdykoli behem behu oznacil VSECHNY dalsi
        # snapshoty az do konce procesu jako "derived_from_video_pts", i kdyz
        # KLV uz zase nesla vlastni PTS. Bezpodminecne prirazeni odrazi stav
        # POSLEDNIHO zpracovaneho KLV paketu, ne historii celeho behu.
        nonlocal klv_timing_is_derived
        klv_timing_is_derived = pts_is_derived
        klv_state_store.update(klv_bytes, pts)

    def handle_video_data(elementary_stream_bytes: bytes, pts) -> None:
        nonlocal frame_decoder
        if frame_decoder is None:
            codec = demuxer.video_codec or "h264"
            if fps_detector is not None:
                # --input-fps nezadana rucne: cekej, dokud FrameRateDetector
                # nezmeri skutecnou fps ze stream u samotneho (nebo nespadne
                # na vychozich 30 po max_buffered access units) - viz
                # FrameRateDetector docstring, proc se VSECHNY access units
                # musi bufferovat, ne jen obrazkove.
                fps = fps_detector.feed(elementary_stream_bytes, pts, codec)
                if fps is None:
                    return   # jeste se meri, decoder se nevytvari
                buffered = fps_detector.drain()
            else:
                fps = args.input_fps
                buffered = None
            frame_decoder = VideoFrameDecoder(codec, fps, args.jpeg_quality,
                                              on_frame_ready=on_frame_ready,
                                              pts_horizon_s=args.pts_horizon_s,
                                              frames_per_s=2.0 / args.interval_s)
            print(f"[VIDEO] starting decoder ({codec}, --input-fps {fps}, "
                  f"dekoduje se kazdy {frame_decoder.select_every}. snimek)")
            if buffered is not None:
                for buffered_bytes, buffered_pts in buffered:
                    frame_decoder.feed(buffered_bytes, buffered_pts)
                return   # tahle access unit uz je mezi bufferovanymi (feed() ji pridal)
        frame_decoder.feed(elementary_stream_bytes, pts)

    demuxer = TransportStreamDemuxer(on_video_data=handle_video_data, on_klv_data=handle_klv_packet)
    resync = TsPacketResync()
    sender.start(args.interval_s, build_snapshot)

    print(f"[MAIN] receiving {args.src}:{args.src_port}, looking for PAT/PMT...")
    exit_code = 0
    try:
        while True:
            # kontrolovano na kazdem datagramu a nejmene 1x/s (recv timeout);
            # bez tohohle by po padu ffmpegu/dekoderoveho vlakna kontejner
            # bezel dal a neposilal nic, aniz by si toho kdokoli vsiml
            if not sender.ticker_alive():
                exit_code = 1   # vyjimka v build_snapshot - vypsana ze SnapshotSender
                break
            if frame_decoder is not None:
                if frame_decoder.has_exited():
                    print(f"[VIDEO] FATAL: {frame_decoder.exit_report()}", file=sys.stderr)
                    exit_code = 1
                    break
                if not frame_decoder.reader_alive():
                    exit_code = 1
                    break
            try:
                data, _ = receive_socket.recvfrom(2048)
            except socket.timeout:
                continue
            for packet in resync.feed(data):
                demuxer.feed(packet)
    except KeyboardInterrupt:
        pass
    finally:
        if frame_decoder:
            frame_decoder.close()
        if ground_link:
            ground_link.stop()
        receive_socket.close()
        send_stats = sender.stats()
        print(f"\n[MAIN] done | {frame_number} frames produced, "
             f"{send_stats['superseded']} prorezano (novejsi snimek v teze sekunde), "
             f"{dropped_without_klv} dropped (no KLV), "
             f"{resync.resync_count}x ztracena TS synchronizace")
        for name, stats in send_stats["destinations"].items():
            print(f"[MAIN] cil {name} | {stats['sent']} posted, {stats['failed']} failed, "
                 f"{stats['dropped_busy']} dropped (cil zaneprazdnen), "
                 f"{stats['heartbeats_sent']} heartbeats ({stats['heartbeats_failed']} failed)")
        if snapshot_store:
            store_stats = snapshot_store.stats()
            print(f"[MAIN] disk | {store_stats['saved']} ulozeno, "
                 f"{store_stats['failed']} selhalo, "
                 f"{store_stats['dropped_busy']} zahozeno (zapis nestihal)")
        if ground_link:
            ground_stats = ground_link.stats()
            print(f"[MAIN] ground sim | {ground_stats['polls']} polls, "
                 f"{ground_stats['failures']} failed")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
