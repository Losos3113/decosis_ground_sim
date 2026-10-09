#!/usr/bin/env python3
"""Odber PX4 telemetrie z pozemniho simulatoru (projekt decosis_ground_sim).

KLV v TS streamu nese geometrii senzoru, ale uz ne stav letounu (baterie,
arming, rychlost v NED). Ta je jen v simulatoru, ktery ji vystavuje pres
REST (`GET /api/<uav>/state`). Tenhle modul je jedina cesta, kterou se ta
data do Converteru dostanou.

Proc vlakno na pozadi a ne dotaz primo ve chvili, kdy je snimek hotovy:
`on_frame_ready()` bezi na ctecim vlakne dekoderu (viz state_holder.py
`VideoFrameDecoder._read_loop`). Synchronni HTTP dotaz primo v nem by pri
pomalem/nedostupnem simulatoru zablokoval cteni z ffmpeg stdout na dobu
timeoutu - a tim zadrhl cele dekodovani, ne jen obohaceni metadat. Proto
se telemetrie stahuje nezavisle a `latest()` jen cte posledni hotovy
vzorek z pameti; nikdy neceka na sit.

Stejna filozofie jako u KLV `METADATA_REFRESH_PERIOD_S`: vzorek starsi nez
`max_age_s` se nevraci vubec. Lepe zadna telemetrie nez tise pripojena
hodnota z doby, kdy simulator jeste bezel.
"""

import json
import threading
import time
import urllib.error
import urllib.request


class GroundSimLink:

    def __init__(self, base_url: str, uav_id: str, poll_interval_s: float = 1.0,
                 timeout_s: float = 1.0, max_age_s: float = 10.0, log=print):
        self.base_url = base_url.rstrip("/")
        self.uav_id = uav_id
        self.url = f"{self.base_url}/api/{uav_id}/state"
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s
        self.max_age_s = max_age_s
        self.log = log

        self._lock = threading.Lock()
        self._by_topic = None
        self._fetched_at = None
        self._stop_event = threading.Event()
        self._thread = None

        # Nedostupny simulator by jinak vypsal radek do logu kazdou sekundu
        # donekonecna. Hlasi se prvni selhani a pak uz jen zmena stavu.
        self._consecutive_failures = 0
        self._failure_reported = False
        self._total_polls = 0
        self._total_failures = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        self.log(f"[GROUND] polling {self.url} every {self.poll_interval_s:.1f} s "
                 f"(timeout {self.timeout_s*1000:.0f} ms, max age {self.max_age_s:.0f} s)")

    def stop(self) -> None:
        self._stop_event.set()

    def latest(self):
        """(by_topic, age_s) posledniho uspesneho vzorku, nebo (None, None),
        pokud jeste zadny nedorazil nebo je starsi nez max_age_s. age_s je
        wall-clock stari od stazeni - NE stream cas jako u KLV; simulator a
        TS stream nemaji spolecne hodiny, parovat je pres PTS nelze."""
        with self._lock:
            by_topic, fetched_at = self._by_topic, self._fetched_at
        if by_topic is None:
            return None, None
        age_s = time.monotonic() - fetched_at
        if age_s > self.max_age_s:
            return None, None
        return by_topic, age_s

    def stats(self) -> dict:
        return {"polls": self._total_polls, "failures": self._total_failures}

    def _fetch_once(self) -> dict:
        request = urllib.request.Request(self.url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            body = response.read()
        document = json.loads(body.decode("utf-8"))
        by_topic = document.get("by_topic")
        if not isinstance(by_topic, dict):
            raise ValueError(f"odpoved nema ocekavany klic 'by_topic' (dostal jsem {list(document)})")
        return by_topic

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            started_at = time.monotonic()
            self._total_polls += 1
            try:
                by_topic = self._fetch_once()
            except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as error:
                self._total_failures += 1
                self._consecutive_failures += 1
                if not self._failure_reported:
                    self._failure_reported = True
                    self.log(f"[GROUND] WARNING: {self.url} nedostupny ({error}) - "
                             f"snimky pojedou bez telemetrie ze simulatoru "
                             f"(dalsi selhani uz se nehlasi, az zmena stavu)")
            else:
                if self._failure_reported:
                    self.log(f"[GROUND] simulator opet odpovida "
                             f"(po {self._consecutive_failures} neuspesnych pokusech)")
                self._consecutive_failures = 0
                self._failure_reported = False
                with self._lock:
                    self._by_topic = by_topic
                    self._fetched_at = time.monotonic()
            # perioda se pocita od ZACATKU dotazu, aby pomala odpoved
            # nepostrkovala takt dal a dal (jinak by se pri 900ms odpovedi
            # a 1s periode vzorkovalo realne po ~1.9 s)
            remaining = self.poll_interval_s - (time.monotonic() - started_at)
            if remaining > 0:
                self._stop_event.wait(remaining)
