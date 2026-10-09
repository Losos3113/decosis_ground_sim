#!/usr/bin/env python3
"""Odeslani snimku (JPEG + metadata) na REST API jako jeden multipart POST.

Nahrazuje UDP kanal DSNAP002. Zadani viz NAVRH_rest_output.md; tohle je
jeho implementace. Ctyri veci, ktere z nej primo plynou:

1. multipart/form-data, ne JSON s base64 obrazkem. base64 by payload
   zvetsil o ~33 % a znovu zavedl presne ten tlak na kompresi kvuli
   velikosti, kvuli kteremu se od UDP odchazi. Multipart nese JPEG jako
   syrove bajty.
2. Jeden request = obrazek i metadata pohromade. Tim je zachovany
   invariant z UDP verze: co dorazi spolu, patri k sobe (stejne PTS,
   stejna pristupova jednotka). Dva samostatne requesty by se mohly
   rozjet nebo prolozit.
3. Zahodit, nikdy neopakovat. Jeden neuspesny POST = ten snimek je ztraceny,
   nic vic - stejna filozofie jako `except OSError` u puvodniho
   `sock.sendto()`. Zadna fronta, zadny retry, zadne bufferovani na disk.
4. POST bezi na pozadi a nikdy neblokuje dekoder.

Pevny takt: kazdych `interval_s` (realny cas, vychozi 1 s) odejde PRESNE
jedna zprava. Dekoder snimky jen odklada (`submit()`), a kdyz jich mezi
dvema tiky prijde vic, nechava se jen ten nejnovejsi (prorezani, ne
ztrata). V tiku se pro nej sestavi metadata (callback `build_snapshot`) a
odesle se; kdyz zadny novy snimek neni, nebo se poslat nema (bez KLV),
odejde misto nej "prazdna obalka" - multipart jen s casti metadata
`{"schema": {"name": ..., "kind": "heartbeat", "sent_at": ...}}` a bez
casti image. Tvar sedi na snapshot (DECOSIS-SNAP-2): typ zpravy je u obou
na `schema.kind`, takze prijemce vetvi na jednom miste a nemusi hadat
podle toho, jestli dorazil obrazek.
Prijemce tak dostava jednu zpravu za sekundu bez ohledu na to, jak rychle
chodi video (rychly replay, kamera na jine fps, vypadek videu).

Nejde o navrat k v1 tikeru (DSNAP001): ten posilal posledni snimek znovu
s metadaty platnymi v case tiku. Tady kazdy snimek nese metadata ke svemu
vlastnimu PTS a zadny snimek neodejde dvakrat - kdyz neni novy, jde
heartbeat.
"""

import datetime
import json
import secrets
import threading
import time
import traceback
import urllib.error
import urllib.request


def encode_multipart(metadata: dict, jpeg_bytes: bytes = None, frame_number: int = None):
    """(telo, content_type). Cast "metadata" je JSON, cast "image" syrove
    JPEG - poradi zachovano, aby prijemce mohl zacit parsovat metadata
    drive, nez docte obrazek. Bez jpeg_bytes (heartbeat) se cast "image"
    vynecha uplne."""
    boundary = secrets.token_hex(16)
    metadata_bytes = json.dumps(metadata, default=str, ensure_ascii=False).encode("utf-8")
    delimiter = f"--{boundary}\r\n".encode("ascii")

    body = bytearray()
    body += delimiter
    body += b'Content-Disposition: form-data; name="metadata"\r\n'
    body += b"Content-Type: application/json; charset=utf-8\r\n\r\n"
    body += metadata_bytes
    body += b"\r\n"
    if jpeg_bytes is not None:
        body += delimiter
        body += (f'Content-Disposition: form-data; name="image"; '
                 f'filename="frame_{frame_number}.jpg"\r\n').encode("ascii")
        body += b"Content-Type: image/jpeg\r\n\r\n"
        body += jpeg_bytes
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode("ascii")

    return bytes(body), f"multipart/form-data; boundary={boundary}"


class Destination:
    """Jeden cil odesilani - vlastni URL, timeout, token, zamek i statistiky.

    Cile jsou zamerne UPLNE oddelene. Jeden z nich pise partner, ne my:
    muze byt pomaly, viset az do timeoutu nebo vracet nesmysly, a nic z toho
    nesmi zpomalit nas vlastni kanal ani dekoder. Proto ma kazdy cil svuj
    in-flight zamek (pomaly cil zahazuje snimky jen sobe), vlastni pocitadla
    a vlastni hlaseni vypadku - v logu je pak hned videt, ci strana je
    nedostupna.
    """

    def __init__(self, name: str, url: str, timeout_s: float = 2.0,
                 auth_token: str = None, log=print):
        self.name = name
        self.url = url
        self.timeout_s = timeout_s
        self.auth_token = auth_token
        self.log = log

        self._in_flight = threading.Lock()
        self._heartbeat_in_flight = threading.Lock()
        self.sent = 0
        self.failed = 0
        self.dropped_busy = 0
        self.heartbeats_sent = 0
        self.heartbeats_failed = 0
        # nedostupny cil by jinak vypsal radek do logu ke kazde zprave;
        # hlasi se prvni selhani a pak uz jen zmena stavu (spolecne pro
        # snimky i heartbeat - jde o dostupnost cile, ne o typ zpravy)
        self._failure_reported = False

    def try_send_snapshot(self, frame_number: int, body: bytes, content_type: str) -> bool:
        """False = cil je zaneprazdneny, snimek pro NEJ zahozen. Ostatni cile
        to neovlivni - kazdy se rozhoduje sam za sebe."""
        if not self._in_flight.acquire(blocking=False):
            self.dropped_busy += 1
            return False
        threading.Thread(target=self._post_snapshot,
                         args=(frame_number, body, content_type), daemon=True).start()
        return True

    def send_heartbeat(self, body: bytes, content_type: str) -> None:
        if not self._heartbeat_in_flight.acquire(blocking=False):
            return   # predchozi heartbeat jeste leti
        threading.Thread(target=self._post_heartbeat, args=(body, content_type),
                         daemon=True).start()

    def _post_snapshot(self, frame_number: int, body: bytes, content_type: str) -> None:
        try:
            if self._post(body, content_type, f"snimek {frame_number}"):
                self.sent += 1
            else:
                self.failed += 1
        finally:
            self._in_flight.release()

    def _post_heartbeat(self, body: bytes, content_type: str) -> None:
        try:
            if self._post(body, content_type, "heartbeat"):
                self.heartbeats_sent += 1
            else:
                self.heartbeats_failed += 1
        finally:
            self._heartbeat_in_flight.release()

    def _post(self, body: bytes, content_type: str, label: str) -> bool:
        headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                response.read()
        except (urllib.error.URLError, OSError) as error:
            # sem spadne i HTTPError (non-2xx) - podtrida URLError. Zalogovat
            # a zahodit, zadny retry.
            if not self._failure_reported:
                self._failure_reported = True
                self.log(f"[SEND:{self.name}] WARNING: POST na {self.url} selhal "
                         f"({error}) - {label} zahozen, bez opakovani "
                         f"(dalsi selhani uz se nehlasi, az zmena stavu)")
            return False
        if self._failure_reported:
            self._failure_reported = False
            self.log(f"[SEND:{self.name}] cil opet prijima ({label})")
        return True

    def stats(self) -> dict:
        return {"sent": self.sent, "failed": self.failed,
                "dropped_busy": self.dropped_busy,
                "heartbeats_sent": self.heartbeats_sent,
                "heartbeats_failed": self.heartbeats_failed}


class SnapshotSender:

    def __init__(self, destinations, schema: str = None, store=None, log=print):
        """destinations  seznam Destination - kazdy dostane v kazdem tiku
                         prave jednu zpravu, nezavisle na ostatnich
        store            volitelny SnapshotStore (ukladani na disk); dostava
                         snimek bez ohledu na to, jak dopadly POSTy"""
        self.destinations = list(destinations)
        self.schema = schema
        self.store = store
        self.log = log

        self._pending_lock = threading.Lock()
        self._pending = None            # (jpeg_bytes, pts) nejnovejsiho neodeslaneho snimku
        self._ticker = None
        self._ticker_failed = False
        self._superseded = 0

    def submit(self, jpeg_bytes: bytes, pts) -> None:
        """Volano z cteciho vlakna dekoderu - nikdy neblokuje. Odlozi snimek
        k odeslani v pristim tiku; starsi neodeslany snimek se tim nahradi."""
        with self._pending_lock:
            if self._pending is not None:
                self._superseded += 1
            self._pending = (jpeg_bytes, pts)

    def start(self, interval_s: float, build_snapshot) -> None:
        """build_snapshot(jpeg_bytes, pts) -> (frame_number, metadata) nebo
        None (snimek neposilat, v tomhle tiku odejde heartbeat)."""
        self._ticker = threading.Thread(target=self._tick_loop,
                                        args=(interval_s, build_snapshot), daemon=True)
        self._ticker.start()

    def ticker_alive(self) -> bool:
        return self._ticker is not None and self._ticker.is_alive() and not self._ticker_failed

    def _tick_loop(self, interval_s: float, build_snapshot) -> None:
        # Tiky na pevne mrizce (start + k*interval), ne "sleep(interval)" po
        # kazdem kole - jinak by se doba zpracovani tiku pricitala a takt
        # by pomalu ujizdel. Kdyz se tik nestihne (system zatizeny), mrizka
        # se posune, misto aby se zmeskane tiky dohanely naraz.
        next_tick = time.monotonic() + interval_s
        try:
            while True:
                delay = next_tick - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                next_tick += interval_s
                if next_tick < time.monotonic():
                    next_tick = time.monotonic() + interval_s
                self._tick(build_snapshot)
        except Exception:
            self.log("[SEND] FATAL: vlakno odesilaciho taktu spadlo:")
            traceback.print_exc()
            self._ticker_failed = True

    def _tick(self, build_snapshot) -> None:
        with self._pending_lock:
            pending, self._pending = self._pending, None
        if pending is None:
            self._broadcast_heartbeat()
            return
        result = build_snapshot(*pending)
        if result is None:
            # snimek se posilat nema (napr. zadna KLV metadata)
            self._broadcast_heartbeat()
            return
        frame_number, metadata = result
        jpeg_bytes, _ = pending

        # Disk je na odesilani nezavisly: ulozi se i snimek, ktery se na
        # zadny cil nedostane.
        if self.store is not None:
            self.store.submit(frame_number, jpeg_bytes, metadata)

        # Telo se slozi JEDNOU a posle na vsechny cile - zabalovat multipart
        # zvlast pro kazdy by byla jen prace navic, obsah je stejny.
        body, content_type = encode_multipart(metadata, jpeg_bytes, frame_number)
        heartbeat_body = heartbeat_content_type = None
        for destination in self.destinations:
            if destination.try_send_snapshot(frame_number, body, content_type):
                continue
            # Tenhle cil je zaneprazdneny, ostatni snimek dostanou. Aby pro
            # nej porad platilo "jedna zprava za interval", posle se mu
            # aspon heartbeat.
            self.log(f"[{time.strftime('%H:%M:%S')}] [SNAPSHOT] {destination.name}: "
                     f"snimek {frame_number} zahozen - predchozi POST jeste "
                     f"nedobehl (pomaly cil)")
            if heartbeat_body is None:
                heartbeat_body, heartbeat_content_type = self._heartbeat_message()
            destination.send_heartbeat(heartbeat_body, heartbeat_content_type)

    def _heartbeat_message(self):
        """Telo heartbeatu. Tvar sedi na snapshot (DECOSIS-SNAP-2): typ zpravy
        je u obou na schema.kind, takze prijemce vetvi na jednom miste."""
        metadata = {
            "schema": {
                "name": self.schema,
                "kind": "heartbeat",
                "sent_at": datetime.datetime.now(datetime.timezone.utc)
                           .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            },
        }
        return encode_multipart(metadata)

    def _broadcast_heartbeat(self) -> None:
        body, content_type = self._heartbeat_message()
        for destination in self.destinations:
            destination.send_heartbeat(body, content_type)

    def stats(self) -> dict:
        """Souhrn za odesilatele; pocitadla jednotlivych cilu jsou na nich."""
        return {"superseded": self._superseded,
                "destinations": {d.name: d.stats() for d in self.destinations}}
