import pandas as pd
import json
import sys

# 1. Načteme původní data
df = pd.read_csv(sys.argv[1])

def modify_payload(row):
    try:
        # Rozbalíme JSON řetězec z payloadu
        data = json.loads(row['payload'])
        
        # Upravíme pozici pro globální GPS (posuneme UAV2 cca o 100 metrů vedle a o 10 metrů výš)
        if row['topic'] == 'vehicle_global_position':
            if 'lat' in data: data['lat'] += 0.001       # Posun na sever
            if 'lon' in data: data['lon'] += 0.001       # Posun na východ
            if 'altitude' in data: data['altitude'] += 10.0 # Poletí o 10 metrů výše
            
        # Upravíme pozici pro lokální odometrii (v PX4 je osa Z orientovaná dolů, takže -10 znamená nahoru)
        elif row['topic'] == 'vehicle_odometry':
            if 'position' in data:
                data['position'][0] += 5.0   # Posun v ose X
                data['position'][1] += 5.0   # Posun v ose Y
                data['position'][2] -= 10.0  # Vyšší výška (NED souřadnice)
                
        # Změníme stav baterie, aby bylo vidět, že jde o jiné letadlo
        elif row['topic'] == 'battery_status':
            if 'remaining' in data:
                data['remaining'] = max(0.0, data['remaining'] - 0.05) # UAV2 má o 5 % méně baterky
                
        # Zabalíme zpět do stringu pro CSV
        return json.dumps(data)
    except Exception:
        # Pokud by se parsování nepovedlo, necháme původní string
        return row['payload']

# 2. Aplikujeme změny na sloupec payload
df['payload'] = df.apply(modify_payload, axis=1)

# 3. Uložíme jako uav2.csv a původní soubor si přejmenuj na uav1.csv
out_path= sys.argv[2] if len(sys.argv) == 3 else "uav2.csv"
df.to_csv(out_path, index=False)
print(f"Soubor {out_path} byl úspěšně vygenerován s upravenými daty!")
