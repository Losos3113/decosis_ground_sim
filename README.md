# DECOSIS simulátor

Přehrává **MPEG-TS video se STANAG 4609 / MISB ST 0601 metadaty**, rozkládá
ho na snímky a KLV, slučuje je s PX4 telemetrií pozemní stanice a výsledek
posílá na API zákazníka.

```
./video/*.ts ──► replay ──► multicast ──► converter ──┬─► POST na API zákazníka
                                              ▲       ├─► POST na naše API
                                              │       └─► .jpg + .json na disk
                            ground-station-sim ┘
                            (PX4 telemetrie z CSV přes REST)
```

Ke každému snímku odejde **jedna multipart zpráva**: část `metadata` (JSON
schématu `DECOSIS-SNAP-2`) a část `image` (JPEG). Když zrovna není co
poslat, odejde místo toho heartbeat — příjemce tak podle ticha pozná výpadek.

## Spuštění

```bash
cp .env.example .env     # a upravit, hlavně TARGET_API_URL
docker compose up -d
```

Video, které se má přehrávat, patří do `./video/` jako jediný `.ts`.
Výměna videa je tedy: zastavit, nahradit soubor, spustit.

## Konfigurace

Všechno je v `.env`, v `docker-compose.yml` není pro běžný provoz co měnit.
Nejdůležitější:

| Proměnná | K čemu |
|---|---|
| `TARGET_API_URL` | kam se posílá zákazníkovi; `{uav}` v cestě se nahradí ID letounu, takže `…/path/to/{uav}/` → `…/path/to/uav1/` |
| `SNAPSHOT_API_URL` | naše vlastní API, nezávislé na zákaznickém |
| `SNAPSHOT_SAVE_DIR_HOST` / `_KEEP` | kam na hostu ukládat snímky a kolik posledních držet (~290 MB/hod) |
| `SEND_INTERVAL_S` | takt: jedna zpráva za tolik sekund na každý cíl |
| `GROUND_SIM_UAV` | které UAV z CSV se bere jako zdroj telemetrie |
| `SIMULATOR_PORT` | port REST API pozemní stanice |

Změna samotného `.env` stačí restartovat (`docker compose up -d`). Zásah do
`entrypoint.sh` nebo `Dockerfile` vyžaduje `--build`, protože se kopírují do
obrazu.

## Části

| Složka | Co dělá |
|---|---|
| `converter/` | parser TS + KLV, slučování metadat, odesílání; vlastní dokumentace v `converter/DOCUMENTATION.md` |
| `server.py`, `sim_data/` | pozemní stanice: PX4 telemetrie z CSV přes REST (`/api/<uav>/state`) |
| `testing/` | pomocné nástroje — `replay.py` (přehrávač), `analyze_ts.py` (rozbor TS), `network_preflight.py` (kontrola multicastu) |
| `video/`, `snapshots/`, `received/` | vstupní video a vygenerovaná data — v gitu ignorované |

## Co odpadlo

Dřív simulátor publikoval **MP4 jako ROS2 snímky** (`video_streamer.py`,
`sim_video/`, klient `ros2_client`) a telemetrii řešil zvlášť, protože
nebyla k dispozici žádná TS videa. S parserem TS je tahle větev zbytečná a
byla odstraněna — spolu s ní zmizel z obrazu pozemní stanice celý ROS2,
OpenCV a cv_bridge, takže build spadl z ~2,5 GB na ~400 MB.

Historie je v gitu, kdyby se k tomu někdy bylo potřeba vrátit.
