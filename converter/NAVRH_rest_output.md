# Návrh: SNAPSHOT výstup jako HTTP POST (nahrazuje UDP DSNAP002)

Stav: **implementováno.** Tenhle soubor zůstává jako zadání a zápis
rozhodnutí; závaznou dokumentací je od implementace sekce "Protocol:
SNAPSHOT channel" v `DOCUMENTATION.md` / `../readme.md`.

Kde to skončilo v kódu:

| Bod návrhu | Soubor |
|---|---|
| multipart POST, fire-and-forget, odesílání na pozadí | `snapshot_sender.py` |
| konfigurovatelná URL místo pevné IP:port | `SNAPSHOT_API_URL` (+ `_TIMEOUT_MS`, `_TOKEN`) v `entrypoint.sh` |
| mock REST server pro lokální test | `../testing/mock_api.py` |
| JPEG kvalita odvázaná od velikosti paketu | `JPEG_QUALITY` default `4` -> `2` |

Nad rámec původního návrhu přibylo obohacení metadat o PX4 telemetrii z
pozemního simulátoru a sjednocení obou zdrojů do jednoho schématu
(`ground_link.py`, `unified.py`, schéma `DECOSIS-SNAP-1`) - to v téhle
poradě ještě na stole nebylo.

Mění se jen "poslední míle" v
`state_holder.py` (jak se hotový `(jpeg_bytes, metadata, pts)` pošle ven) -
demux, dekódování, PTS párování a `KLVStateStore` se nemění vůbec.

## 1. Rozhodnutí z porady

SNAPSHOT kanál (dnes UDP `DSNAP002` na `SNAPSHOT_DST`/`SNAPSHOT_DST_PORT`)
se nahrazuje HTTP POST na REST API. Tvar requestu si určujeme sami (není to
cizí vynucený kontrakt). Požadavky:

1. Odstranit limit velikosti UDP datagramu (dnes tvrdý strop ~65507 B,
   `send_snapshot_message()` už od 60 000 B varuje - viz `state_holder.py`
   řádek ~471). HTTP/TCP tenhle strop nemá.
2. Odstranit zbytečnou kompresi JPEG, která byla volená kvůli UDP limitu,
   ne kvůli skutečné potřebě kvality obrazu.
3. Zachovat invariant: JPEG a metadata v JEDNÉ zprávě si vždy odpovídají
   (stejné PTS, stejná atomická jednotka) - nerozpadnout do dvou
   samostatných requestů, které by se mohly rozjet/proložit.
4. Chybová strategie: **zahodit, nikdy retry.** Jeden neúspěšný POST = ten
   snímek je ztracený, nic víc - stejná filozofie jako dnešní
   `send_snapshot_message()` u UDP (`except OSError: log a zahodit`),
   jen jiný transport.
5. Cíl (URL) se odpojuje od dosavadní vazby na kolegův docker/síť -
   konfigurovatelná URL, ne pevná IP:port předpokládající společnou síť
   s `network_mode: host` jako dnes.

## 2. Tvar HTTP požadavku

**Doporučení: `multipart/form-data`, ne JSON + base64 JPEG.**

Base64 by zvětšil payload o ~33 % a znovu by zavedl přesně ten typ
"zbytečné komprese kvůli velikosti", který se má odstranit (buď větší
přenos, nebo tlak snížit JPEG kvalitu, aby se base64 verze vešla do nějakého
rozumného limitu). Multipart nese JPEG jako syrové binární bajty bez obalu.

```
POST <SNAPSHOT_API_URL>
Content-Type: multipart/form-data; boundary=...

  part "metadata"  Content-Type: application/json
    {"frame_number": 123, "tags": {"5": {...}, "3": {...}}}
    (stejný JSON tvar jako dnes uvnitř DSNAP002, bez binární hlavičky)

  part "image"      Content-Type: image/jpeg, filename="frame_123.jpg"
    <syrové JPEG bajty, SOI...EOI>
```

Jedno HTTP tělo = jeden request = oboje pohromadě naráz -> stejná atomicita
jako dnes jeden UDP datagram (bod 3 výše je tím automaticky splněný, žádná
zvláštní synchronizace navíc).

## 3. JPEG kvalita

Dnešní `JPEG_QUALITY` / `--jpeg-quality` (ffmpeg `-q:v`, default `4`) je v
`DOCUMENTATION.md` zdokumentovaný výslovně jako volba "aby se vlezlo pod
UDP limit". Tohle omezení u HTTP mizí. Hodnotu lze/má se nastavit podle
skutečné potřeby (co má být na snímku vidět), ne podle velikosti paketu -
zrušit tu větu v dokumentaci a nechat kvalitu zvolit podle obrazového
požadavku, ne podle transportu.

## 4. Chybová strategie - fire-and-forget, bez retry

- POST se zkusí jednou, s rozumně krátkým timeoutem (řádově stovky ms,
  přesná hodnota je na kolegovi/měření).
- Neúspěch (timeout, spojení odmítnuto, non-2xx) -> zalogovat a zahodit,
  stejně jako dnešní `except OSError` u `sock.sendto()`. Žádná fronta,
  žádné opakování, žádné bufferování na disk.
- **Jedno rozšíření stejné filozofie, které stojí za zvážení:** i bez
  retry logiky by synchronní POST přímo v `on_frame_ready()` (dnes voláno
  rovnou z dekodérova `_read_loop` vlákna) při pomalém/nedostupném API
  zablokoval čtení z ffmpeg stdout na dobu timeoutu - a tím zpětně
  zadrhl celé dekódování, ne jen odeslání. Doporučuju POST odpalovat na
  krátkodobém pozadí (jedno vlákno na request, nebo malý threadpool) a
  pokud už něco běží/čeká, nový snímek rovnou zahodit místo frontování -
  to je jen rozšíření "zahazování, ne retry" i na přetížení, ne nová
  myšlenka navíc.

## 5. Co se nemění

- `retransmitter.py` (raw TS passthrough) - netýká se ho to vůbec.
- `TransportStreamDemuxer`, `VideoFrameDecoder`, PTS párování,
  `KLVStateStore`/`get_snapshot()` - beze změny, viz úvod.
- ~~Formát JSON uvnitř `metadata` partu - stejný jako dnešní obsah
  `DSNAP002` JSON těla (tag -> `{name, value, age_s, static}`).~~
  **Tohle se nakonec změnilo** - viz poznámka o sjednoceném schématu
  nahoře. Původní tvar se ale neztratil: je v `metadata` celý pod klíčem
  `raw.klv`, takže příjemce zvyklý na starý tvar v něm pořád najde
  všechno, co míval.

## 6. Otevřené pro implementaci (kolega)

Hotovo: body 2, 4 a 5 (viz tabulka nahoře). Zbývá:

1. **Přesná URL a autentizace REST API** - pořád neurčeno. Implementace
   počítá s volitelným `Authorization: Bearer <SNAPSHOT_API_TOKEN>`; když
   je proměnná prázdná, hlavička se neposílá vůbec. mTLS by znamenalo
   sáhnout do `snapshot_sender.py` (vlastní `ssl.SSLContext` do
   `urlopen`), zatím tam není.
2. **Timeout** - default je `SNAPSHOT_API_TIMEOUT_MS=2000`, což je odhad,
   ne měření. Změřit na reálném nasazení, jak bylo dohodnuto. Vodítko:
   log na konci běhu vypisuje, kolik snímků spadlo na `dropped (API busy)`
   - když to číslo roste, timeout je delší než odstup snímků.
