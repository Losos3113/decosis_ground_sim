# UAV Ground Station Simulator

Tento projekt slouží jako kontejnerizovaný simulátor pro pozemní stanici (Ground Station). Simuluje provoz dvou bezpilotních letounů (**uav1** a **uav2**) a poskytuje jak telemetrická data přes FastAPI (REST API + WebSockets), tak video stream v nekonečné smyčce přes ROS2.

## Architektura
Celý simulátor běží v **jednom Docker kontejneru** na síťovém režimu `host`, což zajišťuje minimální režii a okamžitou viditelnost ROS2 topiců v lokální síti.
- **FastAPI Server:** Vyčítá telemetrii ze složky `sim_data/*.csv` a posílá ji dál. Respektuje konfiguraci portu ze souboru `.env`.
- **ROS2 Video Streamer:** Bere videa ze složky `sim_video/*.mp4` a publikuje je jako komprimované snímky frekvencí 1 FPS (vhodné pro simulaci průzkumných dronů).

---

## 1. Spuštění simulátoru (na serveru / hostiteli)

### Prerekvizity
Ujisti se, že máš v kořenovém adresáři vytvořenou strukturu pro data:
```text
ground_station_sim/
├── sim_data/
│   ├── uav1.csv
│   └── uav2.csv
└── sim_video/
    ├── uav1.mp4
    └── uav2.mp4

```

### Konfigurace

V kořenové složce vytvoř soubor `.env` pro dynamické nastavení portu FastAPI serveru:

```env
SIMULATOR_PORT=8002

```

### Spuštění kontejneru

Sestavení a spuštění simulátoru provedete příkazem:

```bash
docker compose down
docker compose build --no-cache
docker compose up

```

Po spuštění je API dokumentace dostupná na adrese `http://<IP_SERVERU>:<PORT>/docs`.

---

## 2. Zachytávání a zobrazení videa (na klientském PC)

Pro testování a zobrazení streamu na vašem počítači využijte oficiální ROS2 desktopový kontejner. Jelikož se spouští grafické rozhraní (`rqt_image_view`), je nutné povolit přístup k X-serveru.

### Krok 1: Povolení grafiky na hostitelském PC

Před spuštěním kontejneru povolte v terminálu svého počítače lokální připojení k vašemu monitoru:

```bash
xhost +local:docker

```

### Krok 2: Spuštění klientského kontejneru

Spusťte kontejner s namontovaným grafickým socketem a sdílenou hostitelskou sítí:

```bash
docker run -it \
  --name ros2_client \
  --network host \
  --env="DISPLAY" \
  --volume="/tmp/.X11-unix:/tmp/.X11-unix:rw" \
  osrf/ros:humble-desktop

```

### Krok 3: Instalace pluginu a spuštění rqt (uvnitř kontejneru)

Jakmile jste uvnitř spuštěného kontejneru, doinstalujte balíček pro dekompresi obrazu, načtěte ROS prostředí a ověřte přítomnost streamu:

```bash
# 1. Aktualizace a instalace pluginu pro komprimované obrázky
apt-get update && apt-get install -y ros-humble-image-transport-plugins

# 2. Načtení ROS2 prostředí
source /opt/ros/humble/setup.bash

# 3. Kontrola, zda vidíte topicy ze simulátoru
ros2 topic list
# Ve výpisu musíte vidět:
# /uav1/camera/image_raw/compressed
# /uav2/camera/image_raw/compressed

# 4. Spuštění grafického prohlížeče snímků
ros2 run rqt_image_view rqt_image_view

```

**V okně `rqt_image_view`:**
V rozevíracím seznamu vlevo nahoře vyberte požadovaný topic (např. `/uav1/camera/image_raw/compressed`). Průzkumný stream běží v plynulých vteřinových intervalech (1 FPS).

```


