#!/usr/bin/env python3
"""Ukladani snimku na disk - ke kazdemu snimku dvojice .jpg + .json.

Disk je treti odberatel vedle obou API a je na nich nezavisly: kdyz POST
selze, snimek se stejne ulozi, a naopak. Zapis se nedela v odesilacim
taktu, ale na kratkodobem vlakne - pomaly nebo plny disk by jinak zdrzel
odeslani, a to je presne ta vazba, kterou tenhle projekt jinde rozplita.

Kdyz uz jeden zapis probiha, dalsi snimek se zahodi misto frontovani -
stejna filozofie jako u POSTu ("zahazuj, neopakuj"), jen aplikovana na
disk. Ulozit kazdy druhy snimek je lepsi nez nechat narust frontu, kterou
disk uz nikdy nedozene.

KAPACITA: pri jedne zprave za sekundu a ~75 kB na JPEG narusta slozka o
zhruba 290 MB za hodinu, tj. ~7 GB za den. `keep` je proto zapnuty
vychozi, ne volitelny luxus; keep=0 ho vypne, ale pak uklid lezi na
obsluze.
"""

import collections
import json
import os
import threading


class SnapshotStore:

    def __init__(self, directory: str, keep: int = 500, log=print):
        self.directory = directory
        self.keep = keep or 0
        self.log = log

        self._write_in_flight = threading.Lock()
        self._written_lock = threading.Lock()
        # zaklady jmen, ktere zapsal TENHLE beh - maze se jen z nich, nikdy
        # se neprochazi cizi obsah slozky
        self._written = collections.deque()

        self._saved = 0
        self._failed = 0
        self._dropped_busy = 0
        self._failure_reported = False

        os.makedirs(self.directory, exist_ok=True)
        self.log(f"[STORE] ukladam do {self.directory}"
                 + (f" (jen poslednich {self.keep} snimku)" if self.keep
                    else " (bez omezeni poctu - uklid je na obsluze)"))

    def submit(self, frame_number: int, jpeg_bytes: bytes, metadata: dict) -> None:
        """Nikdy neblokuje. Kdyz uz jeden zapis bezi, snimek se zahodi."""
        if not self._write_in_flight.acquire(blocking=False):
            self._dropped_busy += 1
            return
        threading.Thread(target=self._write_and_release,
                         args=(frame_number, jpeg_bytes, metadata), daemon=True).start()

    def _write_and_release(self, frame_number: int, jpeg_bytes: bytes, metadata: dict) -> None:
        try:
            self._write(frame_number, jpeg_bytes, metadata)
        finally:
            self._write_in_flight.release()

    def _write(self, frame_number: int, jpeg_bytes: bytes, metadata: dict) -> None:
        base = os.path.join(self.directory, f"frame_{frame_number}")
        try:
            # .jpg az po .json: kdyby beh skoncil mezi zapisy, zustane
            # metadata bez obrazku (a to je poznat) misto obrazku, ktery se
            # tvari kompletni, ale nikdo nevi, co je na nem
            with open(f"{base}.json", "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False, default=str)
            with open(f"{base}.jpg", "wb") as f:
                f.write(jpeg_bytes)
        except OSError as error:
            self._failed += 1
            if not self._failure_reported:
                self._failure_reported = True
                self.log(f"[STORE] WARNING: zapis snimku {frame_number} do "
                         f"{self.directory} selhal ({error}) - snimek se neuklada, "
                         f"odesilani to neovlivni (hlasi se jednou)")
            return
        if self._failure_reported:
            self._failure_reported = False
            self.log(f"[STORE] zapis opet funguje (snimek {frame_number})")
        self._saved += 1
        self._rotate(base)

    def _rotate(self, base: str) -> None:
        if not self.keep:
            return
        with self._written_lock:
            self._written.append(base)
            while len(self._written) > self.keep:
                stale = self._written.popleft()
                for suffix in (".jpg", ".json"):
                    try:
                        os.remove(stale + suffix)
                    except OSError:
                        pass          # uz smazano rucne - na nicem to nemeni

    def stats(self) -> dict:
        return {"saved": self._saved, "failed": self._failed,
                "dropped_busy": self._dropped_busy}
