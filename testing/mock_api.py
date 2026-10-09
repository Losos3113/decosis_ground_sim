#!/usr/bin/env python3
"""Nahradni REST prijemce snimku pro lokalni test bez skutecneho API.

Nahrazuje `reference_client.py`, ktery poslouchal na UDP a pro HTTP kanal
uz je k nicemu. Prijima presne to, co posila `snapshot_sender.py`:
multipart/form-data s casti "metadata" (JSON) a "image" (JPEG).

    python3 testing/mock_api.py --port 9000 --save-dir /tmp/snapshots

Pak se Converter pusti s --api-url http://127.0.0.1:9000/snapshot
"""

import argparse
import collections
import json
import threading
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


VIEWER_PAGE = """<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Converter - zivy nahled</title><style>
*{box-sizing:border-box}
body{margin:0;background:#0d1216;color:#e2e9ec;
     font:14px/1.5 ui-monospace,"SF Mono",Menlo,Consolas,monospace}
.wrap{max-width:1100px;margin:0 auto;padding:1.5rem 1rem 3rem;
      display:flex;flex-direction:column;gap:1rem}
h1{font-size:1rem;margin:0;letter-spacing:.14em;text-transform:uppercase;color:#76868e}
.bar{display:flex;gap:.6rem;flex-wrap:wrap}
.chip{background:#151d22;border:1px solid #28343a;border-radius:3px;
      padding:.35rem .7rem;font-size:.82rem}
.chip b{color:#5dc3c6;font-weight:600}
.chip.warn b{color:#e58b7a}
.main{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:1rem}
@media(max-width:800px){.main{grid-template-columns:1fr}}
img{width:100%;display:block;border:1px solid #28343a;border-radius:3px;background:#000}
.panel{background:#151d22;border:1px solid #28343a;border-radius:3px;
       overflow:auto;max-height:70vh}
table{width:100%;border-collapse:collapse;font-size:.78rem}
td{padding:.3rem .55rem;border-bottom:1px solid #1f2a30;vertical-align:top}
tr:last-child td{border-bottom:0}
.sec td{background:#1c262c;color:#76868e;font-size:.7rem;letter-spacing:.1em;
        text-transform:uppercase}
.f{color:#a2b1b9;white-space:nowrap}
.v{color:#e2e9ec;font-variant-numeric:tabular-nums}
.s{font-size:.68rem;padding:.05rem .3rem;border-radius:2px;white-space:nowrap}
.s-klv{background:#33260f;color:#e0a44c}
.s-gs{background:#0d2f31;color:#5dc3c6}
.idle{opacity:.45}
</style></head><body><div class="wrap">
<h1>Converter &mdash; zivy nahled (port __PORT__)</h1>
<div class="bar">
  <span class="chip">snimku: <b id="n">0</b></span>
  <span class="chip">heartbeatu: <b id="hb">0</b></span>
  <span class="chip">frame: <b id="fn">&ndash;</b></span>
  <span class="chip" id="agewrap">posledni zprava pred: <b id="age">&ndash;</b></span>
</div>
<div class="main">
  <div><img id="img" alt="posledni snimek"></div>
  <div class="panel"><table id="meta"></table></div>
</div>
</div><script>
const E=i=>document.getElementById(i);
let shown=null;
function row(f,v,src){
  const cls=src&&src.startsWith("klv")?"s-klv":"s-gs";
  const tag=src?`<span class="s ${cls}">${src}</span>`:"";
  return `<tr><td class="f">${f}</td><td class="v">${v}</td><td>${tag}</td></tr>`;
}
function fmt(v){
  if(typeof v==="number")return Number.isInteger(v)?v:v.toFixed(5);
  if(Array.isArray(v))return "["+v.map(fmt).join(", ")+"]";
  if(v&&typeof v==="object")return JSON.stringify(v).slice(0,60);
  return String(v);
}
async function tick(){
  try{
    const r=await fetch("latest.json",{cache:"no-store"});
    const d=await r.json();
    E("n").textContent=d.received; E("hb").textContent=d.heartbeats;
    E("age").textContent=d.last_message_age_s==null?"-":d.last_message_age_s.toFixed(1)+" s";
    E("agewrap").className="chip"+(d.last_message_age_s>3?" warn":"");
    if(d.frame_number==null){document.body.classList.add("idle");return;}
    document.body.classList.remove("idle");
    E("fn").textContent="#"+d.frame_number;
    if(d.frame_number!==shown){
      shown=d.frame_number;
      E("img").src="latest.jpg?f="+d.frame_number;
      let h="";
      for(const [branch,fields] of Object.entries(d.branches||{})){
        h+=`<tr class="sec"><td colspan="3">${branch}</td></tr>`;
        for(const [k,o] of Object.entries(fields))h+=row(k,fmt(o.value),o.source);
      }
      E("meta").innerHTML=h||'<tr><td class="f">(zadna metadata)</td></tr>';
    }
  }catch(e){}
}
tick(); setInterval(tick,500);
</script></body></html>"""


class SnapshotHandler(BaseHTTPRequestHandler):

    save_dir = None
    keep = None
    received = 0
    # posledni prijaty snimek pro zivy nahled v prohlizeci (GET /) - drzi se
    # v pameti, takze nahled funguje i bez --save-dir
    latest_lock = threading.Lock()
    latest = None          # (frame_number, jpeg_bytes, metadata, cas prijmu)
    last_message_at = None
    heartbeats = 0
    # jmena uz zapsanych dvojic, aby sly nejstarsi mazat; drzi se jen to, co
    # zapsal TENHLE beh - nikdy se nemaze nic, co uz ve slozce bylo
    _written = collections.deque()
    _write_lock = threading.Lock()

    def do_GET(self):
        """Zivy nahled v prohlizeci: / je stranka, latest.jpg a latest.json
        jsou to, co si sama dotahuje. Data se berou z pameti, takze nahled
        funguje i kdyz se neuklada na disk."""
        path = self.path.split("?", 1)[0]
        with SnapshotHandler.latest_lock:
            latest = SnapshotHandler.latest
            last_at = SnapshotHandler.last_message_at

        if path in ("/", "/index.html"):
            body = VIEWER_PAGE.replace("__PORT__", str(self.server.server_address[1]))
            self._reply(200, "text/html; charset=utf-8", body.encode("utf-8"))
            return

        if path == "/latest.jpg":
            if latest is None:
                self.send_error(404, "zatim nedorazil zadny snimek")
                return
            self._reply(200, "image/jpeg", latest[1])
            return

        if path == "/latest.json":
            document = {
                "received": SnapshotHandler.received,
                "heartbeats": SnapshotHandler.heartbeats,
                "last_message_age_s": (time.time() - last_at) if last_at else None,
                "frame_number": latest[0] if latest else None,
                # jen datove vetve - hlavicka schema ma vlastni radky nahore
                "branches": {k: v for k, v in (latest[2] if latest else {}).items()
                             if k != "schema" and isinstance(v, dict)},
            }
            self._reply(200, "application/json; charset=utf-8",
                        json.dumps(document, ensure_ascii=False, default=str).encode("utf-8"))
            return

        self.send_error(404, "nic tu neni; zkus /")

    def _reply(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            self.send_error(415, "ocekavam multipart/form-data")
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        metadata_bytes, image_bytes = self._split_multipart(body, content_type)
        if metadata_bytes is None:
            self.send_error(400, "chybi cast 'metadata'")
            return

        try:
            metadata = json.loads(metadata_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self.send_error(400, f"cast 'metadata' neni platny JSON: {error}")
            return

        # Typ zpravy je u snapshotu i heartbeatu na jednom miste -
        # schema.kind (DECOSIS-SNAP-2) - prave proto, aby se nemuselo
        # vetvit podle toho, jestli dorazil obrazek.
        schema = metadata.get("schema") or {}
        kind = schema.get("kind", "snapshot")

        # heartbeat = prazdna obalka (viz snapshot_sender.py): jen metadata,
        # bez obrazku - neuklada se, jen se vypise
        if kind == "heartbeat":
            SnapshotHandler.heartbeats += 1
            SnapshotHandler.last_message_at = time.time()
            print(f"[{time.strftime('%H:%M:%S')}] {self.path} heartbeat "
                  f"({schema.get('sent_at')})")
            self.send_response(204)
            self.end_headers()
            return
        if image_bytes is None:
            self.send_error(400, "chybi cast 'image'")
            return

        SnapshotHandler.received += 1
        with SnapshotHandler.latest_lock:
            SnapshotHandler.latest = (schema.get("frame_number"), image_bytes,
                                      metadata, time.time())
            SnapshotHandler.last_message_at = time.time()
        frame_number = schema.get("frame_number", "?")
        sources = schema.get("sources") or {}
        camera = sources.get("camera") or {}
        vehicle = sources.get("vehicle") or {}
        vehicle_note = (f"vehicle {len(vehicle.get('topics') or [])} topicu "
                       f"({vehicle.get('age_s')} s)" if vehicle.get("present")
                       else "bez vehicle_data")
        counts = " + ".join(f"{len(metadata[b])} {b.split('_')[0]}"
                            for b in ("vehicle_data", "camera_data", "mission_data")
                            if metadata.get(b))

        # cesta se vypisuje schvalne: zakaznicke API ma UAV v ceste
        # (TARGET_API_URL=.../{uav}/), takze az se bude overovat, jestli to
        # chodi tam, kam ma, je to videt rovnou tady
        print(f"[{time.strftime('%H:%M:%S')}] {self.path} #{frame_number}: "
              f"JPEG {len(image_bytes)} B, metadata {len(metadata_bytes)} B, "
              f"{camera.get('tag_count', 0)} KLV tagu, "
              f"{vehicle_note} | poli: {counts or 'zadna'}")

        if self.save_dir:
            self._save(frame_number, image_bytes, metadata)

        self.send_response(204)
        self.end_headers()

    def _save(self, frame_number, image_bytes: bytes, metadata: dict) -> None:
        base = os.path.join(self.save_dir, f"frame_{frame_number}")
        with open(f"{base}.jpg", "wb") as f:
            f.write(image_bytes)
        with open(f"{base}.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        if not self.keep:
            return
        # Pri beznem behu pribyvaji dve dvojice za sekundu, takze slozka po
        # chvili obsahuje tisice souboru a nejde se v ni vyznat. --keep drzi
        # jen posledni N snimku. Zamek je potreba, protoze ThreadingHTTPServer
        # obsluhuje requesty soubezne a deque by se jinak rozjela.
        with SnapshotHandler._write_lock:
            SnapshotHandler._written.append(base)
            while len(SnapshotHandler._written) > self.keep:
                stale = SnapshotHandler._written.popleft()
                for suffix in (".jpg", ".json"):
                    try:
                        os.remove(stale + suffix)
                    except OSError:
                        pass          # uz smazano rucne - nic to nemeni

    @staticmethod
    def _split_multipart(body: bytes, content_type: str):
        """Rucni rozdeleni multipart tela na casti podle jmena.

        Zamerne bez `cgi.FieldStorage`: modul `cgi` je od Pythonu 3.13
        odstraneny ze standardni knihovny, a tenhle mock ma jit spustit i
        za par let bez instalace cehokoli. Format je tu navic uzky - dve
        casti, obe produkuje `snapshot_sender.encode_multipart` - takze
        plny parser podle RFC 2046 neni potreba.
        """
        marker = "boundary="
        if marker not in content_type:
            return None, None
        boundary = content_type.split(marker, 1)[1].strip().strip('"')
        separator = f"--{boundary}".encode("ascii")

        parts = {}
        for chunk in body.split(separator):
            if b"\r\n\r\n" not in chunk:
                continue           # preambule a zaviraci "--" ocasek
            raw_headers, _, content = chunk.partition(b"\r\n\r\n")
            name = None
            for line in raw_headers.decode("utf-8", "replace").split("\r\n"):
                if line.lower().startswith("content-disposition:") and 'name="' in line:
                    name = line.split('name="', 1)[1].split('"', 1)[0]
            if name:
                # kazda cast konci CRLF pred dalsim oddelovacem - ten do
                # obsahu nepatri (u JPEG by rozbil koncovou znacku EOI)
                parts[name] = content[:-2] if content.endswith(b"\r\n") else content

        if "metadata" not in parts:
            return None, None
        return parts["metadata"], parts.get("image")   # image chybi u heartbeatu

    def log_message(self, *args):
        pass          # vlastni vypis vyse staci, prednastaveny access log jen sumi


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--save-dir", default=None,
                        help="kam ukladat prichozi .jpg/.json (bez nej se jen vypisuji)")
    parser.add_argument("--keep", type=int, default=50, metavar="N",
                        help="drzet jen poslednich N snimku, starsi mazat "
                             "(0 = neomezene; vychozi 50)")
    args = parser.parse_args()

    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)
    SnapshotHandler.save_dir = args.save_dir
    SnapshotHandler.keep = args.keep or None

    server = ThreadingHTTPServer((args.host, args.port), SnapshotHandler)
    print(f"[MOCK API] poslouchám na http://{args.host}:{args.port}/ (libovolná cesta)"
          f"{f', ukládám do {args.save_dir}' if args.save_dir else ''}"
          f"{f' (jen posledních {args.keep} snímků)' if args.save_dir and args.keep else ''}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[MOCK API] konec | přijato {SnapshotHandler.received} snímků, "
              f"{SnapshotHandler.heartbeats} heartbeatů")


if __name__ == "__main__":
    sys.exit(main())
