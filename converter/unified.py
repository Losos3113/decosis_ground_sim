#!/usr/bin/env python3
"""Sestaveni metadat, ktera odchazi na API (schema DECOSIS-SNAP-2).

Dokument ma ctyri vetve nejvyssi urovne, delene podle TOHO, ODKUD DATA
PRISLA:

    schema        popis zpravy - verze, typ, poradi snimku, UAV, zdroje
    vehicle_data  vse z pozemniho simulatoru (PX4 telemetrie pres REST)
    camera_data   vse z KLV ve video streamu (MISB ST 0601)
    mission_data  udaje o misi

Syrova data obou zdroju se uz neposilaji (do v2 byla ve vetvi `raw`) -
zprava je o ~40 % mensi a neprichazi se temer o nic: kazdy KLV tag, ktery
KLVStateStore vubec vyda, ma nize sve pole. Jedina vyjimka je topic
`timesync_status` ze simulatoru, ktery mapovani nema a tim padem odpada.

Pozor na jednu vec, ktera z toho deleni plyne a neni na prvni pohled videt:
KLV nese i polohu a naklon LETOUNU (tagy 5, 6, 7, 13, 14), ne jen geometrii
senzoru. Protoze se deli podle zdroje, zustavaji tyhle hodnoty v
`camera_data` - a tatáž velicina (poloha letounu) je tim padem ve zprave
DVAKRAT: jednou ze simulatoru ve `vehicle_data`, jednou z KLV v
`camera_data`. Obe jsou spravne, kazda z jineho zdroje; kterou pouzit je
rozhodnuti prijemce. Schema je takhle zamerne (rozhodnuto pri navrhu v2),
drive se slucovalo do jedne hodnoty s prednosti KLV.

Kazde pole je objekt, nikdy hola hodnota:

    {"value": ..., "source": ..., "age_s": ..., "age_ref": ...}

`age_ref` rika, VUCI CEMU se stari meri, protoze oba zdroje nemaji spolecne
hodiny a to cislo by jinak nebylo porovnatelne:

    "frame_pts"          KLV: cas streamu mezi snimkem a KLV paketem
    "frame_pts_derived"  totez, ale KLV nenesl vlastni PTS a cas mu priradil
                         demuxer podle posledniho video PES (presnost ~snimek)
    "fetch_wallclock"    simulator: hodiny stroje od stazeni z REST API
"""

import datetime
import math

SCHEMA_VERSION = "DECOSIS-SNAP-2"

# --- KLV (MISB ST 0601) -> camera_data / mission_data ---------------------
# tag -> (vetev, pole). Tagy, ktere tu nejsou, skonci jen v raw.klv.
KLV_FIELD_MAP = {
    2:  ("camera_data",  "timestamp_utc"),
    # poloha a naklon LETOUNU, prestoze dorazi videem - viz poznamka nahore
    5:  ("camera_data",  "platform_heading_deg"),
    6:  ("camera_data",  "platform_pitch_deg"),
    7:  ("camera_data",  "platform_roll_deg"),
    13: ("camera_data",  "platform_latitude"),
    14: ("camera_data",  "platform_longitude"),
    75: ("camera_data",  "platform_altitude_m"),
    # vlastni geometrie senzoru
    16: ("camera_data",  "hfov_deg"),
    17: ("camera_data",  "vfov_deg"),
    18: ("camera_data",  "relative_azimuth_deg"),
    19: ("camera_data",  "relative_elevation_deg"),
    20: ("camera_data",  "relative_roll_deg"),
    21: ("camera_data",  "slant_range_m"),
    22: ("camera_data",  "target_width_m"),
    23: ("camera_data",  "frame_center_latitude"),
    24: ("camera_data",  "frame_center_longitude"),
    25: ("camera_data",  "frame_center_elevation_m"),
    11: ("camera_data",  "image_source_sensor"),
    12: ("camera_data",  "coordinate_system"),
    65: ("camera_data",  "uas_ls_version"),
    # udaje o misi - jedina vyjimka z deleni podle zdroje: prestoze dorazi
    # v KLV, patri vecne do mission_data, jinak by ta vetev zustala navzdy
    # prazdna a mission_id by lezelo v camera_data, kam nazvem nepatri
    3:  ("mission_data", "mission_id"),
    48: ("mission_data", "security"),
    94: ("mission_data", "miis_core_identifier"),
}

# Tag 10 (Platform Designation, napr. "ScanEagle") nejde do zadne vetve -
# je to popis letounu, ne merena velicina, takze patri do hlavicky
# `schema.uav` vedle id ze simulatoru. Tam se uvadi holou hodnotou, protoze
# blok `schema` popisuje zpravu, nenese data.
KLV_TAG_PLATFORM_DESIGNATION = 10

# --- simulator (PX4) -> vehicle_data --------------------------------------
# topic -> [(klic v payloadu, pole ve vehicle_data)]
# vehicle_odometry.q se resi zvlast (kvaternion -> tri uhly), viz nize.
GROUND_FIELD_MAP = {
    "vehicle_global_position": [
        ("lat",      "latitude"),
        ("lon",      "longitude"),
        ("altitude", "altitude_m"),
    ],
    "vehicle_odometry": [
        ("position", "position_ned_m"),
        ("velocity", "velocity_ned_ms"),
    ],
    "vehicle_status": [
        ("armed",                  "armed"),
        ("arming_state",           "arming_state"),
        ("nav_state",              "nav_state"),
        ("failsafe",               "failsafe"),
        ("pre_flight_checks_pass", "pre_flight_checks_pass"),
        ("rc_signal_lost",         "rc_signal_lost"),
        ("data_link_lost",         "data_link_lost"),
        ("vehicle_type",           "vehicle_type"),
    ],
    "battery_status": [
        ("voltage_v",  "battery_voltage_v"),
        ("current_a",  "battery_current_a"),
        ("remaining",  "battery_remaining"),
    ],
}

BRANCHES = ["vehicle_data", "camera_data", "mission_data"]


def _micros_to_iso(microseconds):
    """ST 0601 tag 2 je mikrosekundy od epochy UTC. Na vystupu ISO 8601,
    aby prijemce nemusel hadat jednotku."""
    try:
        moment = datetime.datetime.fromtimestamp(microseconds / 1_000_000,
                                                 tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError, TypeError):
        return None
    return moment.isoformat().replace("+00:00", "Z")


def quaternion_to_euler_deg(q):
    """PX4 `vehicle_odometry.q` je [w, x, y, z] (FRD vuci NED). Prevod na
    (heading, pitch, roll) ve stupnich, aby sel postavit vedle KLV tagu
    5/6/7, ktere uz uhly jsou. heading je 0-360, jak ho ma ST 0601."""
    if not isinstance(q, (list, tuple)) or len(q) != 4:
        return None
    try:
        w, x, y, z = (float(component) for component in q)
    except (TypeError, ValueError):
        return None

    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    # asin mimo [-1, 1] by u lehce nenormalizovaneho kvaternionu spadlo na
    # ValueError misto toho, aby vratilo +-90 stupnu
    sin_pitch = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
    pitch = math.asin(sin_pitch)
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + z * z))

    return {
        "heading_deg": math.degrees(yaw) % 360.0,
        "pitch_deg": math.degrees(pitch),
        "roll_deg": math.degrees(roll),
    }


def _uav_identity(ground_uav_id, klv_snapshot: dict) -> dict:
    """Kdo letel: id ze simulatoru a typ letounu z KLV. Klice s neznamou
    hodnotou se vynechavaji, aby prijemce nemusel rozlisovat chybejici od
    null."""
    identity = {}
    if ground_uav_id:
        identity["id"] = ground_uav_id
    designation = klv_snapshot.get(KLV_TAG_PLATFORM_DESIGNATION)
    if designation and designation.get("value"):
        identity["designation"] = designation["value"]
    return identity


def _field(value, source: str, age_s, age_ref: str):
    return {
        "value": value,
        "source": source,
        "age_s": round(age_s, 4) if isinstance(age_s, (int, float)) else age_s,
        "age_ref": age_ref,
    }


def build_unified_metadata(frame_number: int, frame_pts, klv_snapshot: dict,
                           ground_by_topic, ground_age_s, ground_uav_id=None,
                           klv_timing_is_derived: bool = False) -> dict:
    """Telo `metadata` partu jednoho snimku.

    klv_snapshot    vystup KLVStateStore.get_snapshot(frame_pts):
                    {tag -> {name, value, age_s, static}}
    ground_by_topic vystup GroundSimLink.latest()[0], nebo None, kdyz
                    simulator neni k dispozici (vehicle_data pak chybi)
    klv_timing_is_derived
                    True, kdyz KLV pakety nenesly vlastni PTS - promita se do
                    age_ref i do schema.sources.camera.timing
    """
    branches = {name: {} for name in BRANCHES}
    klv_age_ref = "frame_pts_derived" if klv_timing_is_derived else "frame_pts"

    # --- KLV -> camera_data / mission_data ---
    for tag, info in sorted(klv_snapshot.items()):
        mapping = KLV_FIELD_MAP.get(tag)
        if mapping is None:
            continue
        branch, field = mapping
        value = _micros_to_iso(info["value"]) if tag == 2 else info["value"]
        if value is None:
            continue
        branches[branch][field] = _field(value, f"klv:{tag}", info["age_s"], klv_age_ref)

    # --- simulator -> vehicle_data ---
    ground_topics = []
    if ground_by_topic:
        for topic_name, record in sorted(ground_by_topic.items()):
            if not isinstance(record, dict):
                continue
            ground_topics.append(topic_name)
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            source = f"ground_sim:{topic_name}"
            for payload_key, field in GROUND_FIELD_MAP.get(topic_name, []):
                value = payload.get(payload_key)
                if value is None:
                    continue
                branches["vehicle_data"][field] = _field(value, source, ground_age_s,
                                                         "fetch_wallclock")
            if topic_name == "vehicle_odometry":
                angles = quaternion_to_euler_deg(payload.get("q"))
                if angles:
                    for field, value in angles.items():
                        branches["vehicle_data"][field] = _field(
                            value, f"{source}.q", ground_age_s, "fetch_wallclock")

    document = {
        "schema": {
            "name": SCHEMA_VERSION,
            # na stejny endpoint chodi i heartbeat (kind=heartbeat, bez
            # obrazku - viz snapshot_sender.py); prijemce vetvi podle tohohle
            "kind": "snapshot",
            "frame_number": frame_number,
            "pts": frame_pts,
            "uav": _uav_identity(ground_uav_id, klv_snapshot),
            "sources": {
                "camera": {
                    "present": bool(klv_snapshot),
                    "tag_count": len(klv_snapshot),
                    # "pes_pts" = cas nesl primo KLV PES (presne sparovani)
                    # "derived_from_video_pts" = odvozeny, viz docstring
                    "timing": "derived_from_video_pts" if klv_timing_is_derived else "pes_pts",
                },
                "vehicle": {
                    "present": bool(ground_by_topic),
                    "uav_id": ground_uav_id,
                    "age_s": round(ground_age_s, 4) if isinstance(ground_age_s, (int, float)) else None,
                    "topics": ground_topics,
                },
            },
        },
    }
    # vetev, kterou nebylo cim naplnit, se do zpravy nedava vubec - prazdny
    # objekt by prijemce nutil rozlisovat "nic nedoslo" od "doslo prazdne"
    for name in BRANCHES:
        if branches[name]:
            document[name] = branches[name]
    return document
