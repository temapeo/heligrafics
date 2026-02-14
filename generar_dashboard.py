#!/usr/bin/env python3
"""
TeMapeo — Generador de Dashboard de Avance Heligrafics
=====================================================

Este script procesa los archivos KML (polígonos) y MRK (vuelos)
y genera un HTML autocontenido con todos los datos embebidos,
listo para subir a GitHub Pages.

USO:
    python3 generar_dashboard.py

ESTRUCTURA DE CARPETAS ESPERADA:
    proyecto_heligrafics/
    ├── generar_dashboard.py          ← este script
    ├── datos/
    │   ├── kml/                      ← archivos KML de solicitudes de vuelo
    │   │   ├── 20260209_ChillanDron.kml
    │   │   └── 20260126_FASA_ValdiviaDron.kml
    │   └── mrk/                      ← archivos MRK de vuelos ejecutados
    │       ├── 20260215_vuelo01.mrk
    │       ├── 20260216_vuelo02.mrk
    │       └── ...
    ├── template/
    │   └── dashboard_template.html   ← template del dashboard
    ├── assets/
    │   └── horizontal.png            ← logo TeMapeo
    └── docs/                         ← output para GitHub Pages
        └── index.html                ← dashboard generado (se sube a GitHub)

FLUJO DIARIO:
    1. Copias los MRK del día a datos/mrk/
    2. Ejecutas: python3 generar_dashboard.py
    3. Haces git add . && git commit -m "Avance dia X" && git push
    4. El cliente recarga la página y ve el avance actualizado
"""

import json
import os
import re
import sys
import math
import base64
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

# ============================================================
# CONFIGURACIÓN
# ============================================================
PROYECTO_DIR = Path(__file__).parent
DATOS_DIR = PROYECTO_DIR / "datos"
KML_DIR = DATOS_DIR / "kml"
MRK_DIR = DATOS_DIR / "mrk"
TEMPLATE_DIR = PROYECTO_DIR / "template"
ASSETS_DIR = PROYECTO_DIR / "assets"
OUTPUT_DIR = PROYECTO_DIR / "docs"  # GitHub Pages sirve desde /docs

# Parámetros del proyecto
RENDIMIENTO_HA_DIA = 100
EQUIPOS = 2
FECHA_INICIO = "2026-02-15"
FOTOS_POR_HA = 55
UMBRAL_VOLADO = 0.65  # 65% de fotos esperadas = VOLADO (~36 fotos/ha mínimo)


# ============================================================
# PARSEO KML
# ============================================================
def parse_kml(filepath):
    """Parsea un archivo KML y extrae polígonos con atributos."""
    tree = ET.parse(filepath)
    root = tree.getroot()
    
    # Manejar namespaces de KML
    ns = ''
    if root.tag.startswith('{'):
        ns = root.tag.split('}')[0] + '}'
    
    filename = os.path.basename(filepath)
    fn_lower = filename.lower()
    
    # Detectar zona por nombre de archivo
    default_zone = 'Desconocida'
    if 'chillan' in fn_lower or 'chillán' in fn_lower or 'norte' in fn_lower:
        default_zone = 'Chillán'
    elif 'valdivia' in fn_lower or 'sur' in fn_lower:
        default_zone = 'Valdivia'
    
    polygons = []
    
    for idx, pm in enumerate(root.iter(f'{ns}Placemark')):
        poly = {
            'id': f'{filename}-{idx}',
            '_file': filename
        }
        
        # Extraer SimpleData
        for sd in pm.iter(f'{ns}SimpleData'):
            name = sd.get('name')
            if name and sd.text:
                poly[name.upper()] = sd.text.strip()
        
        # Extraer Data/value
        for d in pm.iter(f'{ns}Data'):
            name = d.get('name')
            val = d.find(f'{ns}value')
            if name and val is not None and val.text:
                poly[name.upper()] = val.text.strip()
        
        # Nombre del placemark
        name_el = pm.find(f'{ns}name')
        if name_el is not None and name_el.text and 'NOM_PREDIO' not in poly:
            poly['NOM_PREDIO'] = name_el.text.strip()
        
        # Defaults
        if 'ZONA' not in poly:
            poly['ZONA'] = default_zone
        poly['ESTADO'] = 'PENDIENTE'
        
        # Parsear SUP_HA (manejar coma decimal)
        sup_ha = None
        for field in ['SUP_HA', 'SUPERFICIE', 'AREA_HA']:
            if field in poly:
                try:
                    sup_ha = float(poly[field].replace(',', '.'))
                    poly['_supSource'] = 'kml'
                    break
                except (ValueError, AttributeError):
                    pass
        
        # Extraer coordenadas
        coords = []
        for coord_el in pm.iter(f'{ns}coordinates'):
            if coord_el.text:
                pts = coord_el.text.strip().split()
                parsed = []
                for pt in pts:
                    parts = pt.split(',')
                    if len(parts) >= 2:
                        try:
                            lng, lat = float(parts[0]), float(parts[1])
                            if not math.isnan(lat) and not math.isnan(lng):
                                parsed.append([lat, lng])
                        except ValueError:
                            pass
                if len(parsed) >= 3:
                    coords = parsed
                    break
        
        if coords and len(coords) >= 3:
            # Centroid
            poly['centroid'] = [
                sum(c[0] for c in coords) / len(coords),
                sum(c[1] for c in coords) / len(coords)
            ]
            poly['coords'] = coords
            
            # Calcular área si no viene del KML
            if sup_ha is None or math.isnan(sup_ha):
                sup_ha = calc_area_ha(coords)
                poly['_supSource'] = 'calc'
            
            poly['SUP_HA'] = round(sup_ha, 4)
            
            # Limpiar campos que no necesitamos en el JSON
            clean = {}
            for k, v in poly.items():
                if k == 'coords':
                    # Reducir precisión de coordenadas para ahorrar espacio
                    clean['coords'] = [[round(c[0], 6), round(c[1], 6)] for c in v]
                elif k == 'centroid':
                    clean['centroid'] = [round(v[0], 6), round(v[1], 6)]
                else:
                    clean[k] = v
            
            polygons.append(clean)
    
    return polygons


def calc_area_ha(coords):
    """Calcula área en hectáreas usando fórmula esférica."""
    area = 0
    n = len(coords)
    for i in range(n):
        j = (i + 1) % n
        lat1 = math.radians(coords[i][0])
        lat2 = math.radians(coords[j][0])
        dlng = math.radians(coords[j][1] - coords[i][1])
        area += dlng * (2 + math.sin(lat1) + math.sin(lat2))
    return abs(area * 6378137 * 6378137 / 2) / 10000


# ============================================================
# PARSEO MRK
# ============================================================
def parse_mrk(filepath):
    """Parsea un archivo MRK/CSV y extrae foto-centros.
    
    Soporta múltiples formatos:
    - DJI Timestamp MRK: "1  479823.980  [2405]  ... -36.67002938,Lat  -71.69181229,Lon  733.930,Ellh ..."
    - CSV genérico: lat,lng o lng,lat
    """
    filename = os.path.basename(filepath)
    points = []
    
    with open(filepath, 'r', errors='ignore') as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            # Saltar headers
            if i == 0 and ('lat' in line.lower() and 'lon' in line.lower()):
                continue
            
            lat, lng = None, None
            
            # Formato DJI: buscar patrones "valor,Lat" y "valor,Lon"
            lat_match = re.search(r'([-\d.]+),Lat', line)
            lon_match = re.search(r'([-\d.]+),Lon', line)
            
            if lat_match and lon_match:
                try:
                    lat = float(lat_match.group(1))
                    lng = float(lon_match.group(1))
                except ValueError:
                    pass
            
            # Fallback: buscar dos números consecutivos que parezcan coordenadas
            if lat is None or lng is None:
                parts = re.split(r'[,\t\s]+', line)
                for j in range(len(parts) - 1):
                    try:
                        a, b = float(parts[j]), float(parts[j + 1])
                        if (abs(a) > 10 and abs(a) <= 90 and 
                            abs(b) > 10 and abs(b) <= 180):
                            lat, lng = a, b
                            break
                    except ValueError:
                        pass
            
            if lat is not None and lng is not None:
                if abs(lat) <= 90 and abs(lng) <= 180:
                    points.append({
                        'lat': round(lat, 7),
                        'lng': round(lng, 7),
                        'file': filename,
                        'index': i
                    })
    
    return points


# ============================================================
# INTERSECCIÓN PUNTO-POLÍGONO
# ============================================================
def point_in_polygon(lat, lng, coords):
    """Ray casting algorithm."""
    inside = False
    x, y = lat, lng
    n = len(coords)
    j = n - 1
    for i in range(n):
        xi, yi = coords[i]
        xj, yj = coords[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def process_intersections(polygons, mrk_points):
    """Cruza foto-centros con polígonos y asigna estados."""
    for p in polygons:
        p['_mrkHits'] = 0
    
    for pt in mrk_points:
        for poly in polygons:
            if 'coords' not in poly:
                continue
            if point_in_polygon(pt['lat'], pt['lng'], poly['coords']):
                poly['_mrkHits'] = poly.get('_mrkHits', 0) + 1
                pt['matched'] = True
                break
    
    # Paso 1: Evaluar cada polígono individualmente
    for p in polygons:
        hits = p.get('_mrkHits', 0)
        expected = p.get('SUP_HA', 0) * FOTOS_POR_HA
        if hits == 0:
            p['_polyEstado'] = 'PENDIENTE'
        elif expected > 0 and hits >= expected * UMBRAL_VOLADO:
            p['_polyEstado'] = 'VOLADO'
        else:
            p['_polyEstado'] = 'PARCIAL'
    
    # Paso 2: Propagar estado a nivel de predio (NOM_PREDIO)
    # Si un predio tiene al menos un polígono PENDIENTE/PARCIAL,
    # todo el predio baja a PARCIAL (si tiene algún hit) o PENDIENTE (si no tiene ninguno)
    from collections import defaultdict
    predios = defaultdict(list)
    for p in polygons:
        key = p.get('NOM_PREDIO', p.get('ID_PREDIO', ''))
        if key:
            predios[key].append(p)
    
    for predio, polys in predios.items():
        estados = set(p['_polyEstado'] for p in polys)
        total_hits = sum(p.get('_mrkHits', 0) for p in polys)
        
        if estados == {'VOLADO'}:
            # Todos volados → predio VOLADO
            predio_estado = 'VOLADO'
        elif total_hits == 0:
            # Ningún hit → predio PENDIENTE
            predio_estado = 'PENDIENTE'
        else:
            # Mezcla de estados → predio PARCIAL
            predio_estado = 'PARCIAL'
        
        for p in polys:
            p['ESTADO'] = predio_estado
    
    # Polígonos sin predio: usar estado individual
    for p in polygons:
        if 'ESTADO' not in p or not p.get('NOM_PREDIO', p.get('ID_PREDIO', '')):
            p['ESTADO'] = p.get('_polyEstado', 'PENDIENTE')


# ============================================================
# GENERACIÓN DEL DASHBOARD
# ============================================================
def generate_dashboard(kml_data, mrk_data, logo_b64):
    """Genera el HTML del dashboard con datos embebidos."""
    
    template_path = TEMPLATE_DIR / "dashboard_template.html"
    
    if not template_path.exists():
        print(f"⚠️  Template no encontrado en {template_path}")
        print("   Usando el dashboard base...")
        # Leer el dashboard base si existe
        base_path = OUTPUT_DIR / "index.html"
        if not base_path.exists():
            print("❌ No se encontró ningún template. Copia dashboard_template.html a template/")
            sys.exit(1)
    
    # Preparar datos JSON
    dashboard_data = {
        'generated': datetime.now().isoformat(),
        'config': {
            'startDate': FECHA_INICIO,
            'rendHaDia': RENDIMIENTO_HA_DIA,
            'equipos': EQUIPOS,
            'fotosPerHa': FOTOS_POR_HA,
            'umbralVolado': UMBRAL_VOLADO
        },
        'kmlFiles': [],
        'mrkFiles': []
    }
    
    for name, polys in kml_data.items():
        # No incluir coords completas para ahorrar espacio — 
        # solo incluirlas en la versión con mapa
        dashboard_data['kmlFiles'].append({
            'name': name,
            'polygons': polys
        })
    
    for name, pts in mrk_data.items():
        dashboard_data['mrkFiles'].append({
            'name': name,
            'points': pts,
            'active': True,
            'dateAdded': datetime.now().strftime('%d/%m/%Y %H:%M')
        })
    
    data_json = json.dumps(dashboard_data, ensure_ascii=False)
    
    # Leer template
    with open(template_path, 'r', encoding='utf-8') as f:
        html = f.read()
    
    # Inyectar datos embebidos
    inject_script = f'''
<script>
// ===== DATOS EMBEBIDOS — Generado el {datetime.now().strftime('%d/%m/%Y %H:%M')} =====
const EMBEDDED_DATA = {data_json};
</script>
'''
    
    # Insertar antes del cierre </head> o antes del primer <script>
    if '</head>' in html:
        html = html.replace('</head>', inject_script + '\n</head>')
    else:
        html = inject_script + html
    
    # Reemplazar logo placeholder si existe
    if logo_b64 and 'LOGO_PLACEHOLDER' in html:
        html = html.replace('LOGO_PLACEHOLDER', logo_b64)
    
    return html


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 60)
    print("  TeMapeo — Generador de Dashboard Heligrafics")
    print("=" * 60)
    print()
    
    # Crear directorios si no existen
    for d in [KML_DIR, MRK_DIR, TEMPLATE_DIR, ASSETS_DIR, OUTPUT_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    
    # 1. Procesar KMLs
    print("📍 Procesando archivos KML...")
    kml_files = sorted(KML_DIR.glob('*.kml'))
    if not kml_files:
        print(f"   ⚠️  No se encontraron KML en {KML_DIR}")
        print(f"   Copia tus archivos KML a esa carpeta.")
        sys.exit(1)
    
    all_kml_data = {}
    all_polygons = []
    for kf in kml_files:
        polys = parse_kml(kf)
        all_kml_data[kf.name] = polys
        all_polygons.extend(polys)
        
        total_ha = sum(p.get('SUP_HA', 0) for p in polys)
        from_kml = sum(1 for p in polys if p.get('_supSource') == 'kml')
        from_calc = sum(1 for p in polys if p.get('_supSource') == 'calc')
        
        print(f"   ✅ {kf.name}: {len(polys)} polígonos, {total_ha:,.1f} ha")
        print(f"      SUP_HA del KML: {from_kml} | Calculadas: {from_calc}")
    
    total_ha = sum(p.get('SUP_HA', 0) for p in all_polygons)
    print(f"\n   📊 Total: {len(all_polygons)} polígonos, {total_ha:,.1f} ha")
    
    # 2. Procesar MRKs
    print("\n✈️  Procesando archivos MRK...")
    
    # First, extract any ZIP files found in MRK_DIR
    zip_files = sorted(MRK_DIR.glob('*.zip')) + sorted(MRK_DIR.glob('*.ZIP'))
    for zf in zip_files:
        import zipfile
        try:
            # Extract to a subfolder with same name as zip (without extension)
            extract_dir = MRK_DIR / zf.stem
            if not extract_dir.exists():
                print(f"   📦 Descomprimiendo {zf.name}...")
                with zipfile.ZipFile(zf, 'r') as z:
                    z.extractall(extract_dir)
                print(f"      → {sum(1 for _ in extract_dir.rglob('*.MRK')) + sum(1 for _ in extract_dir.rglob('*.mrk'))} archivos MRK extraídos")
            else:
                print(f"   📦 {zf.name} ya descomprimido → {extract_dir.name}/")
        except Exception as e:
            print(f"   ⚠️  Error descomprimiendo {zf.name}: {e}")
    
    # Now search recursively for MRK files in all subdirectories
    mrk_extensions = ['*.mrk', '*.MRK']
    mrk_files = []
    for ext in mrk_extensions:
        mrk_files.extend(MRK_DIR.rglob(ext))
    mrk_files = sorted(set(mrk_files))
    
    # Group by parent folder for display
    mrk_by_folder = {}
    for mf in mrk_files:
        folder = mf.parent.name if mf.parent != MRK_DIR else '(raíz)'
        if folder not in mrk_by_folder:
            mrk_by_folder[folder] = []
        mrk_by_folder[folder].append(mf)
    
    all_mrk_data = {}
    all_mrk_points = []
    if mrk_files:
        for folder, files in sorted(mrk_by_folder.items()):
            folder_pts = 0
            for mf in files:
                pts = parse_mrk(mf)
                if pts:
                    # Use folder/filename as key to avoid name collisions
                    rel_name = f"{mf.parent.name}/{mf.name}" if mf.parent != MRK_DIR else mf.name
                    all_mrk_data[rel_name] = pts
                    all_mrk_points.extend(pts)
                    folder_pts += len(pts)
            print(f"   ✅ {folder}: {len(files)} archivos, {folder_pts:,} foto-centros")
        
        if all_mrk_points:
            print(f"\n   📊 Total: {len(all_mrk_points)} foto-centros")
            
            # 3. Cruzar MRK con polígonos
            print("\n🔄 Procesando intersecciones...")
            process_intersections(all_polygons, all_mrk_points)
            
            matched = sum(1 for p in all_mrk_points if p.get('matched'))
            volados = sum(1 for p in all_polygons if p.get('ESTADO') == 'VOLADO')
            parciales = sum(1 for p in all_polygons if p.get('ESTADO') == 'PARCIAL')
            volado_ha = sum(p.get('SUP_HA', 0) for p in all_polygons if p.get('ESTADO') == 'VOLADO')
            parcial_ha = sum(p.get('SUP_HA', 0) for p in all_polygons if p.get('ESTADO') == 'PARCIAL')
            cubierta_ha = volado_ha + parcial_ha
            
            # Contar predios
            from collections import Counter
            pred_estados = {}
            for p in all_polygons:
                key = p.get('NOM_PREDIO', p.get('ID_PREDIO', ''))
                if key:
                    pred_estados[key] = p.get('ESTADO', 'PENDIENTE')
            n_pred_volado = sum(1 for e in pred_estados.values() if e == 'VOLADO')
            n_pred_parcial = sum(1 for e in pred_estados.values() if e == 'PARCIAL')
            
            print(f"   ✅ {matched}/{len(all_mrk_points)} foto-centros dentro de polígonos")
            print(f"   ✅ {n_pred_volado} predios volados ({volado_ha:,.1f} ha)")
            print(f"   🔶 {n_pred_parcial} predios parciales ({parcial_ha:,.1f} ha)")
            print(f"   📊 Superficie cubierta: {cubierta_ha:,.1f} ha ({cubierta_ha/total_ha*100:.1f}%)")
    else:
        print("   ℹ️  Sin archivos MRK (avance al 0%)")
    
    # 4. Logo
    logo_b64 = ''
    logo_path = ASSETS_DIR / "horizontal.png"
    if logo_path.exists():
        with open(logo_path, 'rb') as f:
            logo_b64 = base64.b64encode(f.read()).decode('utf-8')
        print(f"\n🎨 Logo cargado: {logo_path.name}")
    
    # 5. Generar dashboard
    print("\n📄 Generando dashboard...")
    
    template_path = TEMPLATE_DIR / "dashboard_template.html"
    if not template_path.exists():
        print(f"   ⚠️  No se encontró template en {template_path}")
        print(f"   Copia 'control_avance_heligrafics.html' como 'template/dashboard_template.html'")
        sys.exit(1)
    
    html = generate_dashboard(all_kml_data, all_mrk_data, logo_b64)
    
    # Guardar
    output_path = OUTPUT_DIR / "index.html"
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    
    size_kb = os.path.getsize(output_path) / 1024
    print(f"   ✅ Dashboard generado: {output_path}")
    print(f"   📦 Tamaño: {size_kb:,.0f} KB")
    
    # 6. Resumen
    print()
    print("=" * 60)
    print("  ✅ LISTO")
    print("=" * 60)
    print()
    print("  Próximos pasos:")
    print("  1. cd docs/")
    print("  2. Abre index.html en el navegador para verificar")
    print("  3. git add -A && git commit -m \"Avance\" && git push")
    print("  4. El cliente ve la actualización en la URL de GitHub Pages")
    print()


if __name__ == '__main__':
    main()
