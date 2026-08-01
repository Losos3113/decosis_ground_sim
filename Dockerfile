FROM ros:humble-ros-base

# 1. Instalace systémových závislostí pro OpenCV, Python a nástroje pro entrypoint
RUN apt-get update && apt-get install -y \
    python3-pip \
    python3-opencv \
    ros-humble-cv-bridge \
    dos2unix \
    && rm -rf /var/lib/apt/lists/*

# 2. Instalace balíčků s explicitním uzamčením NumPy na verzi 1.x
RUN pip3 install --no-cache-dir fastapi uvicorn pandas "numpy<2.0.0" --force-reinstall

WORKDIR /app

# 3. Zkopírování zdrojových kódů a skriptů
COPY server.py .
COPY video_streamer.py .
COPY entrypoint.sh .

# Sychr pro případ, že by skript měl Windows konce řádků (\r\n) a nastavení spustitelnosti
RUN dos2unix entrypoint.sh && chmod +x entrypoint.sh

# Otevření portu pro FastAPI
EXPOSE 8001

# Spuštění přes náš startovací skript
CMD ["./entrypoint.sh"]
