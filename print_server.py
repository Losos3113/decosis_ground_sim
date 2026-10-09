#!/usr/bin/env python3
"""Docasny HTTP server pro ladeni - prijme POST (multipart/form-data i
obycejny JSON/raw telo) a VYPISE CELOU strukturu na konzoli, ne jen
jednoradkovy souhrn jako testing/mock_api.py.

Pouziti:
    python print_server.py --port 9000

Converter pak miri na:
    --api-url http://127.0.0.1:9000/snapshot
"""

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def split_multipart(body: bytes, content_type: str):
    """Rozdeli multipart/form-data telo na {jmeno_casti: (headers, obsah)}.
    Stejny pristup jako testing/mock_api.py - rucne, bez modulu `cgi`
    (ten je od Pythonu 3.13 pryc ze standardni knihovny)."""
    marker = "boundary="
    if marker not in content_type:
        return None
    boundary = content_type.split(marker, 1)[1].strip().strip('"')
    separator = f"--{boundary}".encode("ascii")

    parts = {}
    for chunk in body.split(separator):
        if b"\r\n\r\n" not in chunk:
            continue
        raw_headers, _, content = chunk.partition(b"\r\n\r\n")
        headers = raw_headers.decode("utf-8", "replace").strip()
        name = None
        for line in headers.split("\r\n"):
            if line.lower().startswith("content-disposition:") and 'name="' in line:
                name = line.split('name="', 1)[1].split('"', 1)[0]
        if name:
            content = content[:-2] if content.endswith(b"\r\n") else content
            parts[name] = (headers, content)
    return parts or None


class PrintHandler(BaseHTTPRequestHandler):

    received = 0
    # posledni prijaty snimek pro webovy nahled (/) - chranene zamkem,
    # protoze ThreadingHTTPServer obsluhuje POST a GET soubezne na ruznych
    # vlaknech
    _lock = threading.Lock()
    _latest_image = None          # bytes
    _latest_metadata_json = None  # uz zformatovany text (bytes), pro znovuuziti
    _latest_frame_number = None

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._serve_viewer()
        elif self.path.startswith("/latest.jpg"):
            self._serve_latest_image()
        elif self.path.startswith("/latest.json"):
            self._serve_latest_metadata()
        else:
            # health-check na cokoli jineho, aby i prohlizec/`curl` bez -X
            # dal rozumnou odpoved
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                f"print_server bezi, prijato {PrintHandler.received} POST pozadavku. "
                f"Nahled: http://{self.headers.get('Host', 'localhost')}/\n"
                .encode("utf-8"))

    def _serve_viewer(self):
        html = """<!doctype html>
<html lang="cs"><head><meta charset="utf-8">
<title>print_server - nahled</title>
<style>
  body { font-family: monospace; background: #1e1e1e; color: #ddd; margin: 1.5rem; }
  h1 { font-size: 1rem; color: #9cdcfe; }
  #frameInfo { color: #6a9955; margin-bottom: .5rem; }
  .wrap { display: flex; gap: 1.5rem; align-items: flex-start; flex-wrap: wrap; }
  img { max-width: 640px; border: 1px solid #444; background: #000; }
  pre { background: #252526; padding: 1rem; max-height: 80vh; overflow: auto;
        max-width: 50vw; white-space: pre-wrap; word-break: break-word; }
  .empty { color: #888; }
</style></head>
<body>
  <h1>print_server &mdash; posledni prijaty snimek (auto-refresh 1 s)</h1>
  <div id="frameInfo">cekam na prvni POST...</div>
  <div class="wrap">
    <img id="img" alt="(zatim zadny snimek)">
    <pre id="meta" class="empty">(zatim zadna metadata)</pre>
  </div>
<script>
async function tick() {
  try {
    const res = await fetch('/latest.json?t=' + Date.now());
    if (res.status === 200) {
      const data = await res.json();
      document.getElementById('meta').textContent = JSON.stringify(data, null, 2);
      document.getElementById('img').src = '/latest.jpg?t=' + Date.now();
      document.getElementById('frameInfo').textContent =
        'frame_number ' + data.frame_number + ' | schema ' + data.schema +
        ' | ' + new Date().toLocaleTimeString();
    }
  } catch (e) { /* server zrovna neni dostupny - zkusit znovu pristi tik */ }
}
tick();
setInterval(tick, 1000);
</script>
</body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_latest_image(self):
        with PrintHandler._lock:
            image = PrintHandler._latest_image
        if image is None:
            self.send_error(404, "jeste nedorazil zadny snimek")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(image)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(image)

    def _serve_latest_metadata(self):
        with PrintHandler._lock:
            meta = PrintHandler._latest_metadata_json
        if meta is None:
            self.send_error(404, "jeste nedorazila zadna metadata")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(meta)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(meta)

    def do_POST(self):
        PrintHandler.received += 1
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")

        banner = f" POZADAVEK #{PrintHandler.received} [{time.strftime('%H:%M:%S')}] "
        print(f"\n{banner:=^78}")
        print(f"cesta:         {self.path}")
        print(f"Content-Type:  {content_type}")
        print(f"Content-Length:{length} B")

        if content_type.startswith("multipart/form-data"):
            parts = split_multipart(body, content_type)
            if parts is None:
                print("!! multipart boundary se nepodarilo rozpoznat")
            for name, (headers, content) in (parts or {}).items():
                print(f"\n--- cast '{name}' ({len(content)} B) ---")
                print(headers)
                if name == "metadata" or b"application/json" in headers.lower().encode():
                    try:
                        parsed = json.loads(content.decode("utf-8"))
                        print(json.dumps(parsed, indent=2, ensure_ascii=False))
                        # pro webovy nahled (/) - ulozit jak je, uz validni JSON;
                        # heartbeat (prazdna obalka bez obrazku) nahled
                        # neprepisuje, jinak by u stareho JPEGu visela jeho metadata
                        if parsed.get("kind") != "heartbeat":
                            with PrintHandler._lock:
                                PrintHandler._latest_metadata_json = content
                                PrintHandler._latest_frame_number = parsed.get("frame_number")
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        print(f"(neplatny JSON: {error})")
                elif name == "image" or b"image/" in headers.lower().encode():
                    print(f"(binarni obsah, prvnich 16 B: {content[:16].hex()})")
                    with PrintHandler._lock:
                        PrintHandler._latest_image = content
                else:
                    print(f"(binarni obsah, prvnich 16 B: {content[:16].hex()})")
        elif "json" in content_type:
            try:
                parsed = json.loads(body.decode("utf-8"))
                print(json.dumps(parsed, indent=2, ensure_ascii=False))
                with PrintHandler._lock:
                    PrintHandler._latest_metadata_json = body
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                print(f"(neplatny JSON: {error})")
        else:
            print(f"(syrove telo, prvnich 200 B)\n{body[:200]!r}")

        print("=" * 78)

        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass  # vlastni vypis vyse staci


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), PrintHandler)
    print(f"[PRINT SERVER] poslouchám na http://{args.host}:{args.port}/ "
          f"- vypisuje celou strukturu kazdeho POST pozadavku")
    print(f"[PRINT SERVER] webovy nahled (posledni JPEG + metadata): "
          f"http://127.0.0.1:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print(f"\n[PRINT SERVER] konec | přijato {PrintHandler.received} požadavků")


if __name__ == "__main__":
    sys.exit(main())
