#!/usr/bin/env python3
"""Nahradni pozemni simulator (projekt decosis_ground_sim) pro lokalni test
bez skutecneho PX4/simulatoru.

Vystavuje presne to, co `ground_link.py` cte:

    GET /api/<uav_id>/state  ->  {"by_topic": {<topic>: {"payload": {...}}}}

Hodnoty se pri kazdem requestu malinko hnou (heading, baterie), aby bylo v
`unified.py` videt, ze telemetrie skutecne prichazi a stari (`age_s`,
`age_ref=fetch_wallclock`) ma smysl.

    python3 testing/mock_ground_sim.py --port 8002 --uav uav1
"""

import argparse
import json
import math
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START = time.monotonic()


def _sample(uav_id: str) -> dict:
    t = time.monotonic() - START
    heading_rad = (t * 0.2) % (2 * math.pi)
    return {
        "vehicle_global_position": {
            "payload": {
                "lat": 49.2 + 0.0005 * math.sin(t * 0.1),
                "lon": 16.6 + 0.0005 * math.cos(t * 0.1),
                "altitude": 380.0 + 5.0 * math.sin(t * 0.05),
            }
        },
        "vehicle_odometry": {
            "payload": {
                "position": [0.0, 0.0, -380.0],
                "velocity": [12.0, 0.0, 0.0],
                # [w, x, y, z], jen otoceni kolem Z (yaw) - viz
                # unified.quaternion_to_euler_deg
                "q": [math.cos(heading_rad / 2), 0.0, 0.0, math.sin(heading_rad / 2)],
            }
        },
        "vehicle_status": {
            "payload": {
                "armed": True,
                "arming_state": "ARMED",
                "nav_state": "AUTO_MISSION",
                "failsafe": False,
                "pre_flight_checks_pass": True,
                "rc_signal_lost": False,
                "data_link_lost": False,
                "vehicle_type": "FIXED_WING",
            }
        },
        "battery_status": {
            "payload": {
                "voltage_v": 22.2 - 0.01 * (t % 300),
                "current_a": 8.5,
                "remaining": max(0.1, 1.0 - (t % 300) / 300.0),
            }
        },
    }


class StateHandler(BaseHTTPRequestHandler):

    expected_uav = None
    served = 0

    def do_GET(self):
        prefix, _, uav_id = self.path.rpartition("/api/")
        uav_id, _, suffix = uav_id.partition("/state")
        if not uav_id or suffix not in ("", "/") or "/api/" not in self.path:
            self.send_error(404, "cekam /api/<uav>/state")
            return
        if self.expected_uav and uav_id != self.expected_uav:
            self.send_error(404, f"neznamy uav '{uav_id}'")
            return

        StateHandler.served += 1
        body = json.dumps({"by_topic": _sample(uav_id)}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # vlastni souhrn staci, viz main()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--uav", default=None,
                        help="pokud zadano, odpovida jen na tohle UAV ID "
                             "(bez nej na libovolne)")
    args = parser.parse_args()

    StateHandler.expected_uav = args.uav
    server = ThreadingHTTPServer((args.host, args.port), StateHandler)
    print(f"[MOCK GROUND SIM] poslouchám na http://{args.host}:{args.port}/api/"
         f"{args.uav or '<uav>'}/state")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[MOCK GROUND SIM] konec | obslouzeno {StateHandler.served} dotazu")


if __name__ == "__main__":
    sys.exit(main())
