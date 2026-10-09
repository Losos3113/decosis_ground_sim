from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
import pandas as pd
import json
import asyncio
import os
import glob
import re
import time
from typing import Dict, Any

app = FastAPI(
    title="DECOSIS - PX4 Multi-UAV Ground Station Simulator",
    description="Simulátor pro více dronů. Data se cyklují nezávisle z CSV souborů ve složce sim_data.",
    version="2.1.0"
)

# Složka, kde se nachází CSV soubory
DATA_DIR = "sim_data"

# Kam converter ukládá snímky - <SNAPSHOT_DIR>/<uav_id>/frame_<n>.jpg + .json.
# Čte se odtud jen pro /video; mezi kontejnery tím nevzniká žádná síťová
# vazba navíc, sdílí se prostě ta složka.
SNAPSHOT_DIR = os.getenv("SNAPSHOT_DIR", "/data/snapshots")

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



VIDEO_PAGE = """<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DECOSIS - __UAV__</title><style>
*{box-sizing:border-box}
body{margin:0;background:#0d1216;color:#e2e9ec;
     font:14px/1.5 ui-monospace,"SF Mono",Menlo,Consolas,monospace}
.wrap{max-width:1100px;margin:0 auto;padding:1.5rem 1rem 3rem;
      display:flex;flex-direction:column;gap:1rem}
h1{font-size:1rem;margin:0;letter-spacing:.14em;text-transform:uppercase;color:#76868e}
h1 b{color:#e2e9ec}
.bar{display:flex;gap:.6rem;flex-wrap:wrap}
.chip{background:#151d22;border:1px solid #28343a;border-radius:3px;
      padding:.35rem .7rem;font-size:.82rem}
.chip b{color:#5dc3c6;font-weight:600}
.chip.warn b{color:#e58b7a}
.main{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:1rem}
@media(max-width:800px){.main{grid-template-columns:1fr}}
img{width:100%;display:block;border:1px solid #28343a;border-radius:3px;background:#000}
.panel{background:#151d22;border:1px solid #28343a;border-radius:3px;
       overflow:auto;max-height:72vh}
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
.msg{color:#76868e;padding:1rem}
</style></head><body><div class="wrap">
<h1>DECOSIS &mdash; <b>__UAV__</b></h1>
<div class="bar">
  <span class="chip">snimek: <b id="fn">&ndash;</b></span>
  <span class="chip" id="agewrap">stari: <b id="age">&ndash;</b></span>
  <span class="chip">zdroj: <b id="src">&ndash;</b></span>
</div>
<div class="main">
  <div><img id="img" alt="posledni snimek"></div>
  <div class="panel"><table id="meta"></table><div class="msg" id="msg">nacitam...</div></div>
</div>
</div><script>
const UAV="__UAV__", E=i=>document.getElementById(i);
let shown=null;
function fmt(v){
  if(typeof v==="number")return Number.isInteger(v)?v:v.toFixed(5);
  if(Array.isArray(v))return "["+v.map(fmt).join(", ")+"]";
  if(v&&typeof v==="object")return JSON.stringify(v).slice(0,60);
  return String(v);
}
async function tick(){
  try{
    const r=await fetch(`/api/${UAV}/frame.json`,{cache:"no-store"});
    if(!r.ok){E("msg").textContent="zatim nedorazil zadny snimek - bezi converter?";
              E("msg").style.display="";return;}
    const d=await r.json();
    E("msg").style.display="none";
    E("fn").textContent="#"+d.frame_number;
    E("age").textContent=d.age_s==null?"-":d.age_s.toFixed(1)+" s";
    E("agewrap").className="chip"+(d.age_s>3?" warn":"");
    const sc=(d.metadata.schema&&d.metadata.schema.sources)||{};
    E("src").textContent=`${(sc.camera||{}).tag_count||0} KLV / ${((sc.vehicle||{}).topics||[]).length} topicu`;
    if(d.frame_number!==shown){
      shown=d.frame_number;
      E("img").src=`/api/${UAV}/frame.jpg?f=${d.frame_number}`;
      let h="";
      for(const [branch,fields] of Object.entries(d.metadata)){
        if(branch==="schema"||typeof fields!=="object")continue;
        h+=`<tr class="sec"><td colspan="3">${branch}</td></tr>`;
        for(const [k,o] of Object.entries(fields)){
          const cls=(o.source||"").startsWith("klv")?"s-klv":"s-gs";
          h+=`<tr><td class="f">${k}</td><td class="v">${fmt(o.value)}</td>`
            +`<td><span class="s ${cls}">${o.source||""}</span></td></tr>`;
        }
      }
      E("meta").innerHTML=h;
    }
  }catch(e){}
}
tick(); setInterval(tick,500);
</script></body></html>"""


# --- Snímky z converteru -----------------------------------------------------

def latest_frame(uav_id: str):
    """(číslo snímku, cesta k .jpg, cesta k .json, stáří v s) nejnovějšího
    snímku daného UAV, nebo None. Bere se nejvyšší číslo, ne nejnovější
    mtime - converter zapisuje .json před .jpg, takže podle času by šlo
    trefit dvojici rozepsanou v půli."""
    folder = os.path.join(SNAPSHOT_DIR, uav_id)
    best = None
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for name in names:
        match = re.fullmatch(r"frame_(\d+)\.jpg", name)
        if not match:
            continue
        number = int(match.group(1))
        if best is None or number > best:
            best = number
    if best is None:
        return None
    jpg = os.path.join(folder, f"frame_{best}.jpg")
    meta = os.path.join(folder, f"frame_{best}.json")
    if not os.path.exists(meta):
        return None          # .jpg bez metadat = dvojice rozepsaná v půli
    try:
        age = time.time() - os.path.getmtime(jpg)
    except OSError:
        age = None
    return best, jpg, meta, age


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


# --- Živý obraz z converteru -------------------------------------------------

@app.get("/api/{uav_id}/frame.json", summary="Metadata posledního snímku")
async def get_frame_metadata(uav_id: str):
    """Metadata posledního snímku, který converter vyrobil pro tohle UAV -
    schéma DECOSIS-SNAP-2, tedy přesně to, co odešlo partnerovi."""
    get_uav_state_or_404(uav_id)
    frame = latest_frame(uav_id)
    if frame is None:
        raise HTTPException(status_code=404,
                            detail=f"Pro UAV '{uav_id}' zatím nedorazil žádný snímek. "
                                   f"Běží converter a přehrává se video?")
    number, _, meta_path, age = frame
    with open(meta_path, encoding="utf-8") as f:
        metadata = json.load(f)
    return {"frame_number": number, "age_s": round(age, 3) if age else None,
            "metadata": metadata}


@app.get("/api/{uav_id}/frame.jpg", summary="Poslední snímek jako JPEG")
async def get_frame_image(uav_id: str):
    get_uav_state_or_404(uav_id)
    frame = latest_frame(uav_id)
    if frame is None:
        raise HTTPException(status_code=404, detail=f"Pro UAV '{uav_id}' zatím není snímek.")
    return FileResponse(frame[1], media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


@app.get("/video/{uav_id}", response_class=HTMLResponse,
         summary="Živý pohled na UAV v prohlížeči")
async def video_page(uav_id: str):
    """Stránka s posledním snímkem a jeho metadaty, obnovuje se sama.
    Spojuje obě půlky dohromady: obraz z converteru a telemetrii odsud."""
    get_uav_state_or_404(uav_id)
    return VIDEO_PAGE.replace("__UAV__", uav_id)


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
