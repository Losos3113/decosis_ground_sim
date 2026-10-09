#!/usr/bin/env python3

import collections
import struct
import threading

UAS_LOCAL_SET_KEY = bytes.fromhex("060E2B34020B01010E01030101000000")

# MISB ST 0601.19 sekce 6.5 "Report-on-Change": tag, ktery v paketu chybi,
# znamena "beze zmeny" a plati dal az METADATA_REFRESH_PERIOD_S sekund STREAM
# casu (PTS), ne wall-clock casu prijmu - "Receivers treat an item as
# undefined when the item does not update within a Metadata Refresh Period."
# Zero-Length Item (ZLI, hodnota b"", viz retired ST 0601.14-33 "length of
# zero ... consumers shall interpret ... as unknown") naopak znamena
# "neznamo" OKAMZITE, bez ohledu na predchozi hodnotu.
METADATA_REFRESH_PERIOD_S = 30.0
PTS_CLOCK_HZ = 90_000
PTS_BITS = 33
PTS_MODULUS = 1 << PTS_BITS


def pts_delta(a: int, b: int) -> int:
    """a - b v 33bitovem MPEG PTS prostoru (wrap kazdych ~26.5 h dle
    ISO 13818-1/ST 1402), vraceno se znamenkem."""
    delta = (a - b) % PTS_MODULUS
    if delta > PTS_MODULUS // 2:
        delta -= PTS_MODULUS
    return delta


def extract_pes_pts(pes_packet: bytes):
    """PTS z PES hlavicky nesouci jen PTS, bez DTS - jediny tvar, jaky
    produkuje mux.pes() v transmitter.py (PTS_DTS_flags vzdy '10', viz
    hdr byte 0x80). Vraci None, pokud hlavicka chybi, je prilis kratka,
    nebo PTS neobsahuje."""
    if len(pes_packet) < 14 or pes_packet[:3] != b"\x00\x00\x01":
        return None
    if ((pes_packet[7] >> 6) & 0x3) != 0b10:      # PTS_DTS_flags == '10'
        return None
    b0, b1, b2, b3, b4 = pes_packet[9:14]
    return (((b0 >> 1) & 0x07) << 30) | (b1 << 22) | \
           (((b2 >> 1) & 0x7F) << 15) | (b3 << 7) | ((b4 >> 1) & 0x7F)


def decode_ber_length(data: bytes, position: int):
    first_byte = data[position]
    if first_byte < 0x80:
        return first_byte, position + 1
    length_byte_count = first_byte & 0x7F
    value = int.from_bytes(data[position + 1:position + 1 + length_byte_count], "big")
    return value, position + 1 + length_byte_count


def decode_ber_tag(data: bytes, position: int):
    tag = 0
    while True:
        byte = data[position]
        tag = (tag << 7) | (byte & 0x7F)
        position += 1
        if not (byte & 0x80):
            return tag, position


def compute_checksum(data: bytes) -> int:
    total = 0
    for index, byte in enumerate(data):
        total = (total + (byte << (8 * ((index + 1) % 2)))) & 0xFFFF
    return total


def decode_timestamp(raw):        return struct.unpack(">Q", raw)[0]
def decode_heading(raw):          return struct.unpack(">H", raw)[0] / 65535.0 * 360.0
def decode_pitch(raw):            return struct.unpack(">h", raw)[0] / 32767.0 * 20.0
def decode_roll(raw):             return struct.unpack(">h", raw)[0] / 32767.0 * 50.0
def decode_latitude(raw):         return struct.unpack(">i", raw)[0] / 2147483647.0 * 90.0
def decode_longitude(raw):        return struct.unpack(">i", raw)[0] / 2147483647.0 * 180.0
def decode_altitude(raw):         return struct.unpack(">H", raw)[0] / 65535.0 * 19900.0 - 900.0
def decode_field_of_view(raw):    return struct.unpack(">H", raw)[0] / 65535.0 * 180.0
def decode_relative_azimuth(raw): return struct.unpack(">I", raw)[0] / 4294967295.0 * 360.0
def decode_relative_elevation(raw): return struct.unpack(">i", raw)[0] / 2147483647.0 * 180.0
def decode_slant_range(raw):      return struct.unpack(">I", raw)[0] / 4294967295.0 * 5_000_000.0
def decode_target_width(raw):     return struct.unpack(">H", raw)[0] / 65535.0 * 10_000.0
def decode_utf8_string(raw):      return raw.decode("utf-8", errors="replace")
def decode_single_byte(raw):      return raw[0]

SECURITY_CLASSIFICATION_NAMES = {
    0x01: "UNCLASSIFIED", 0x02: "RESTRICTED", 0x03: "CONFIDENTIAL",
    0x04: "SECRET", 0x05: "TOP SECRET",
}


def decode_security_local_set(raw: bytes) -> dict:
    result, position = {}, 0
    while position < len(raw):
        tag, position = decode_ber_tag(raw, position)
        length, position = decode_ber_length(raw, position)
        value = raw[position:position + length]
        position += length
        if tag == 1 and value:
            result["classification"] = SECURITY_CLASSIFICATION_NAMES.get(value[0], f"0x{value[0]:02X}")
        elif tag == 3:
            result["classifying_country"] = value.decode("ascii", errors="replace")
        elif tag == 4:
            result["sci"] = value.decode("ascii", errors="replace")
        elif tag == 5:
            result["caveats"] = value.decode("ascii", errors="replace")
        elif tag == 6:
            result["releasing_instructions"] = value.decode("ascii", errors="replace")
        elif tag == 13:
            result["object_country"] = value.decode("utf-16-be", errors="replace")
        elif tag == 22 and len(value) == 2:
            result["version"] = struct.unpack(">H", value)[0]
    return result


def decode_miis_identifier(raw: bytes) -> dict:
    if len(raw) < 34:
        return {"raw": raw.hex()}
    usage_byte = raw[1]
    sensor_type = (usage_byte >> 5) & 0x03
    platform_type = (usage_byte >> 3) & 0x03
    return {
        "sensor_uuid": raw[2:18].hex(),
        "platform_uuid": raw[18:34].hex(),
        "sensor_id_type": "PHYSICAL" if sensor_type == 0b11 else "VIRTUAL",
        "platform_id_type": "PHYSICAL" if platform_type == 0b11 else "VIRTUAL",
    }


TAG_DEFINITIONS = {
    2:  ("Precision Time Stamp", decode_timestamp),
    3:  ("Mission ID", decode_utf8_string),
    5:  ("Platform Heading", decode_heading),
    6:  ("Platform Pitch", decode_pitch),
    7:  ("Platform Roll", decode_roll),
    10: ("Platform Designation", decode_utf8_string),
    11: ("Image Source Sensor", decode_utf8_string),
    12: ("Image Coordinate System", decode_utf8_string),
    13: ("Sensor Latitude", decode_latitude),
    14: ("Sensor Longitude", decode_longitude),
    16: ("Sensor HFOV", decode_field_of_view),
    17: ("Sensor VFOV", decode_field_of_view),
    18: ("Sensor Relative Azimuth", decode_relative_azimuth),
    19: ("Sensor Relative Elevation", decode_relative_elevation),
    20: ("Sensor Relative Roll", decode_relative_azimuth),
    21: ("Slant Range", decode_slant_range),
    22: ("Target Width", decode_target_width),
    23: ("Frame Center Latitude", decode_latitude),
    24: ("Frame Center Longitude", decode_longitude),
    25: ("Frame Center Elevation (MSL)", decode_altitude),
    48: ("Security Local Set", decode_security_local_set),
    65: ("UAS LS Version", decode_single_byte),
    75: ("Sensor Ellipsoid Height (HAE)", decode_altitude),
    94: ("MIIS Core Identifier", decode_miis_identifier),
}

STATIC_TAG_NUMBERS = {3, 10, 11, 12, 48, 94}


def parse_klv_packet(data: bytes):
    if len(data) < 16 + 1 + 2 or data[:16] != UAS_LOCAL_SET_KEY:
        return None
    total_length, position = decode_ber_length(data, 16)
    body_end = position + total_length
    if body_end > len(data):
        return None
    fields = {}
    cursor = position
    try:
        # Truncated/malformed tag-length-value data (a cut-off UDP datagram,
        # a corrupted length field) can walk `cursor` past the end of `data`
        # before this loop's own bound check catches it - decode_ber_tag/
        # decode_ber_length then index past the buffer and raise IndexError.
        # One bad packet used to crash the whole process for this reason;
        # parse_klv_packet's contract is "parse it or say None", so it
        # absorbs that here instead of leaving every caller to remember to.
        while cursor < body_end - 2:
            tag, cursor = decode_ber_tag(data, cursor)
            length, cursor = decode_ber_length(data, cursor)
            fields[tag] = data[cursor:cursor + length]
            cursor += length
    except IndexError:
        return None
    checksum_ok = False
    if body_end >= 2:
        expected_checksum = struct.unpack(">H", data[body_end - 2:body_end])[0]
        checksum_ok = compute_checksum(data[:body_end - 2]) == expected_checksum
    return fields, checksum_ok


class KLVStateStore:
    """Report-on-Change store dle ST 0601.19 6.5 (viz komentar nahore).

    Drzi PER-TAG HISTORII (ne jen "aktualni" hodnotu), protoze snapshot pro
    snimek s danym PTS se casto sklada AZ PO dekoderove latenci - v tu chvili
    uz store muze mit novejsi udaje z pozdejsich paketu, ktere se snimku
    predchazejicimu v case netykaji. get_snapshot(frame_pts) proto rekonstruuje
    stav TAK, JAK VYPADAL V CASE frame_pts, ne aktualni stav.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # tag -> deque[(pts, value_or_None)], rostouci PTS; None = ZLI ("unknown")
        self._history = {}
        self.valid_packet_count = 0
        self.checksum_failure_count = 0

    def update(self, klv_bytes: bytes, pts) -> None:
        if pts is None:
            return  # paket bez PTS nejde umistit na casovou osu streamu
        parsed = parse_klv_packet(klv_bytes)
        if parsed is None:
            return
        fields, checksum_ok = parsed
        if not checksum_ok:
            with self._lock:
                self.checksum_failure_count += 1
            return
        decoded_values = {}
        for tag, raw_value in fields.items():
            if tag not in TAG_DEFINITIONS:
                continue
            if raw_value == b"":
                decoded_values[tag] = None   # ZLI -> "unknown" (ST 0601.14-33)
                continue
            _, decode_function = TAG_DEFINITIONS[tag]
            try:
                decoded_values[tag] = decode_function(raw_value)
            except (struct.error, IndexError, UnicodeDecodeError):
                continue
        refresh_window_pts = int(METADATA_REFRESH_PERIOD_S * PTS_CLOCK_HZ)
        with self._lock:
            self.valid_packet_count += 1
            for tag, value in decoded_values.items():
                history = self._history.setdefault(tag, collections.deque())
                history.append((pts, value))
                # bound memory: nic starsiho nez refresh-period uz zadny
                # budouci snapshot nepotrebuje
                while len(history) > 1 and pts_delta(pts, history[0][0]) > refresh_window_pts:
                    history.popleft()

    def get_snapshot(self, frame_pts: int) -> dict:
        """Metadata tak, jak vypadala v case frame_pts (stream cas) - ne
        aktualni stav. Tag chybi ve vystupu, pokud jeste nedorazil, pokud
        byl naposledy nahlasen jako ZLI ("unknown"), nebo pokud od jeho
        posledni aktualizace k frame_pts uplynulo vic nez
        METADATA_REFRESH_PERIOD_S (Report-on-Change refresh period)."""
        with self._lock:
            history_snapshot = {tag: list(history) for tag, history in self._history.items()}
        snapshot = {}
        for tag, entries in history_snapshot.items():
            match = None
            for pts, value in reversed(entries):
                if pts_delta(frame_pts, pts) >= 0:   # pts je v case frame_pts jiz znamy
                    match = (pts, value)
                    break
            if match is None:
                continue
            pts, value = match
            if value is None:
                continue   # ZLI: v tomto okamziku explicitne "unknown"
            age_s = pts_delta(frame_pts, pts) / PTS_CLOCK_HZ
            if age_s > METADATA_REFRESH_PERIOD_S:
                continue   # refresh period uplynul -> "undefined"
            snapshot[tag] = {
                "name": TAG_DEFINITIONS[tag][0],
                "value": value,
                "age_s": age_s,
                "static": tag in STATIC_TAG_NUMBERS,
            }
        return snapshot
