from fastapi import FastAPI, HTTPException
import pandas as pd
import json
import asyncio
import os
import glob
from typing import Dict, Any

app = FastAPI(
    title="ROS2 / PX4 Multi-UAV Ground Station Simulator",
    description="Simulátor pro více dronů. Data se cyklují nezávisle z CSV souborů ve složce sim_data.",
    version="2.1.0"
)

# Složka, kde se nachází CSV soubory
DATA_DIR = "sim_data"

# --- Globální stav pro všechna UAV ---
# Struktura: { "uav1": { "current_row": ..., "by_topic": ... }, "uav2": { ... } }
UAV_SIMULATIONS: Dict[str, Dict[str, Any]] = {}

async def uav_simulation_loop(uav_id: str, df: pd.DataFrame):
    """
    Nezávislá smyčka pro konkrétní UAV. Každou sekundu posune stav daného dronu.
    """
    global UAV_SIMULATIONS
    total_rows = len(df)
    current_index = 0
    
    print(f"-> [{uav_id}] Smyčka na pozadí spuštěna (celkem {total_rows} zpráv).")
    
    while True:
        row = df.iloc[current_index]
        
        try:
            payload_dict = json.loads(row['payload'])
        except Exception:
            payload_dict = {"error": "Invalid JSON in CSV payload"}

        row_data = {
            "sequence": int(row['sequence']),
            "timestamp_us": int(row['timestamp_us']),
            "topic": str(row['topic']),
            "payload": payload_dict
        }
        
        # Zápis do globálního stavu pro toto konkrétní UAV
        UAV_SIMULATIONS[uav_id]["current_row"] = row_data
        UAV_SIMULATIONS[uav_id]["by_topic"][row_data["topic"]] = row_data
        
        # Posun indexu dokola
        current_index = (current_index + 1) % total_rows
        
        # Každé UAV tiká nezávisle každou sekundu
        await asyncio.sleep(1.0)


@app.on_event("startup")
async def startup_event():
    """Při startu automaticky proskenuje složku sim_data a načte všechna CSV."""
    global UAV_SIMULATIONS
    
    # Najde všechny .csv soubory ve složce sim_data
    csv_pattern = os.path.join(DATA_DIR, "*.csv")
    csv_files = glob.glob(csv_pattern)
    
    if not csv_files:
        print(f"Chyba: Ve složce '{DATA_DIR}' nebyly nalezeny žádné .csv soubory!")
        return

    print(f"Spouštím detekci simulací ve složce '{DATA_DIR}':")
    
    for file_path in csv_files:
        # Získá název souboru bez přípony (např. 'uav1.csv' -> 'uav1')
        file_name = os.path.basename(file_path)
        uav_id = os.path.splitext(file_name)[0]
        
        print(f" - Načítám data pro '{uav_id}' ze souboru: {file_path}")
        try:
            df = pd.read_csv(file_path)
            df = df.sort_values(by="timestamp_us").reset_index(drop=True)
            
            # Inicializace stavu
            UAV_SIMULATIONS[uav_id] = {
                "current_row": {},
                "by_topic": {}
            }
            
            # Spuštění úlohy na pozadí pro toto konkrétní UAV
            asyncio.create_task(uav_simulation_loop(uav_id, df))
            
        except Exception as e:
            print(f" X Nepodařilo se načíst soubor {file_path}: {e}")
            
    print(f"Inicializace hotova. Aktivní simulace: {list(UAV_SIMULATIONS.keys())}\n")


# --- Pomocná funkce pro validaci UAV v URL ---
def get_uav_state_or_404(uav_id: str) -> Dict[str, Any]:
    if uav_id not in UAV_SIMULATIONS:
        raise HTTPException(
            status_code=404, 
            detail=f"UAV s ID '{uav_id}' neexistuje. Dostupná UAV na tomto serveru: {list(UAV_SIMULATIONS.keys())}"
        )
    return UAV_SIMULATIONS[uav_id]

# --- API Endpointy ---
@app.get("/api/uavs", summary="Seznam všech aktuálně simulovaných UAV")
async def list_simulated_uavs():
    """
    Vrátí jednoduchý seznam (pole) s ID všech dronů,
    které server úspěšně načetl ze složky sim_data a momentálně je simuluje.
    """
    return {
        "active_uavs": list(UAV_SIMULATIONS.keys()),
        "total_count": len(UAV_SIMULATIONS)
    }


@app.get("/api/{uav_id}/topics", summary="Seznam všech dostupných topiců pro konkrétní UAV")
async def list_topics(uav_id: str):
    """
    Vrátí jednoduché pole s názvy všech topiců, které toto UAV
    obsahuje v CSV souboru (např. ['vehicle_status', 'battery_status']).
    """
    uav_data = get_uav_state_or_404(uav_id)

    # Vezmeme klíče ze slovníku by_topic, což jsou přesně názvy jednotlivých topiců
    topic_list = list(uav_data["by_topic"].keys())

    return {
        "uav_id": uav_id,
        "topics": topic_list,
        "total_topics": len(topic_list)
    }


@app.get("/api/{uav_id}/state", summary="Poslední známý stav všech topiců pro konkrétní UAV")
async def get_full_state(uav_id: str):
    """Vrátí poslední známou hodnotu KAŽDÉHO topicu najednou - jeden request
    místo jednoho dotazu na topic. Tohle je endpoint, který odebírá Converter:
    telemetrie se v něm sbírá k jednomu snímku, takže ji potřebuje
    konzistentně a bez N samostatných requestů."""
    uav_data = get_uav_state_or_404(uav_id)
    return {
        "uav_id": uav_id,
        "by_topic": uav_data["by_topic"],
    }


@app.get("/api/{uav_id}/topic/{topic_name}", summary="Stav konkrétního topicu pro konkrétní UAV")
async def get_topic_state(uav_id: str, topic_name: str):
    """Vrátí poslední stav jednoho konkrétního topicu pro vybrané UAV."""
    uav_data = get_uav_state_or_404(uav_id)
    topic_data = uav_data["by_topic"].get(topic_name)
    if not topic_data:
        raise HTTPException(status_code=404, detail=f"Topic '{topic_name}' pro UAV '{uav_id}' zatím neproběhl.")
    return topic_data


@app.get("/api/{uav_id}/current", summary="Poslední vygenerovaná zpráva pro konkrétní UAV")
async def get_current_state(uav_id: str):
    """Vrátí aktuální zprávu, která zrovna v tuto sekundu pro dané UAV prošla."""
    uav_data = get_uav_state_or_404(uav_id)
    return uav_data["current_row"]


if __name__ == "__main__":
    import uvicorn
    import os
    
    # Načtení z prostředí
    port_env = os.getenv("SIMULATOR_PORT")
    
    # Ošetření prázdné proměnné nebo neviditelných znaků
    if port_env:
        port_env = port_env.strip()
    else:
        port_env = "8001"
        
    try:
        port = int(port_env)
    except ValueError:
        print(f"Varování: Neplatný port v konfiguraci '{port_env}', padám na default 8001")
        port = 8001
        
    print(f"=== Spouštím Uvicorn na portu {port} ===")
    uvicorn.run(app, host="0.0.0.0", port=port)
