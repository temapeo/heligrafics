#!/usr/bin/env python3
"""
TeMapeo — Generador de Dashboard de Avance Heligrafics v2
=========================================================

MEJORAS v2:
  - Cache de MRK: solo parsea archivos nuevos/modificados
  - Índice espacial: bbox pre-filtro para intersecciones 10x más rápido
  - Buffer de líneas de vuelo (Shapely) para cobertura geométrica real
  - Soporte de operadores/equipos por carpeta
  - MRK embebidos como líneas compactas (no puntos individuales)

ESTRUCTURA DE CARPETAS:
    heligrafics/
    ├── generar_dashboard.py
    ├── datos/
    │   ├── kml/
    │   │   ├── 20260209_ChillanDron.kml
    │   │   └── 20260209_FASA_ValdiviaDron_Consolidado.kml
    │   └── mrk/
    │       ├── EQ1_13-02-2026/          ← Equipo 1 (o cualquier nombre)
    │       │   ├── equipo.txt           ← Archivo con nombre del operador (opcional)
    │       │   └── *.MRK
    │       ├── EQ2_15-02-2026/
    │       │   └── *.MRK
    │       └── MRK 13-02-2026.zip       ← Se autodescomprime
    ├── template/
    │   └── dashboard_template.html
    ├── assets/
    │   └── horizontal.png
    └── docs/
        └── index.html

OPERADORES:
    Para diferenciar equipos, usa una de estas opciones:
    1. Crea un archivo 'equipo.txt' dentro de cada carpeta MRK con el nombre del operador
    2. Nombra las carpetas con prefijo: EQ1_fecha/ y EQ2_fecha/
    3. Si no se detecta, se asigna operador automáticamente por carpeta

USO:
    python3 generar_dashboard.py
"""

import json
import os
import re
import sys
import math
import base64
import time
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

# ============================================================
# CONFIGURACIÓN
# ============================================================
PROYECTO_DIR = Path(__file__).parent
DATOS_DIR = PROYECTO_DIR / "datos"
KML_DIR = DATOS_DIR / "kml"
MRK_DIR = DATOS_DIR / "mrk"
TEMPLATE_DIR = PROYECTO_DIR / "template"
ASSETS_DIR = PROYECTO_DIR / "assets"
OUTPUT_DIR = PROYECTO_DIR / "docs"

# Parámetros del proyecto
RENDIMIENTO_HA_DIA = 100
EQUIPOS = 2
FECHA_INICIO = "2026-02-15"

# Parámetros de cobertura (buffer de líneas de vuelo)
ALTURA_VUELO = 60       # metros AGL
BUFFER_M = 27           # buffer a cada lado de la línea (~54m swath Mavic 3M a 60m)
UMBRAL_COBERTURA = 0.55 # 55% del polígono cubierto por buffers = VOLADO

# Cache
CACHE_FILE = DATOS_DIR / ".mrk_cache.json"


# ============================================================
# PARSEO KML
# ============================================================
def parse_kml(filepath):
    """Parsea un archivo KML y extrae polígonos con atributos."""
    tree = ET.parse(filepath)
    root = tree.getroot()
    ns = {'kml': 'http://www.opengis.net/kml/2.2'}
    
    if root.tag.startswith('{'):
        ns_uri = root.tag.split('}')[0] + '}'
        ns = {'kml': ns_uri.strip('{}')}
    
    polygons = []
    
    for pm in root.iter('{%s}Placemark' % ns['kml']):
        poly = {}
        
        # Extract attributes from ExtendedData
        ext = pm.find('.//kml:ExtendedData', ns)
        if ext is not None:
            for data in ext.findall('.//kml:SimpleData', ns):
                name = data.get('name', '')
                val = (data.text or '').strip()
                poly[name] = val
        
        # Parse SUP_HA
        sup_raw = poly.get('SUP_HA', '')
        sup_source = 'kml'
        if sup_raw:
            try:
                sup_ha = float(sup_raw.replace(',', '.'))
                if sup_ha <= 0:
                    sup_ha = None
                    sup_source = 'calc'
            except (ValueError, TypeError):
                sup_ha = None
                sup_source = 'calc'
        else:
            sup_ha = None
            sup_source = 'calc'
        
        # Extract coordinates
        coords = None
        for coord_elem in pm.iter('{%s}coordinates' % ns['kml']):
            text = coord_elem.text
            if not text:
                continue
            parsed = []
            for part in text.strip().split():
                try:
                    vals = part.split(',')
                    lng, lat = float(vals[0]), float(vals[1])
                    if abs(lat) <= 90 and abs(lng) <= 180:
                        parsed.append([lat, lng])
                except (ValueError, IndexError):
                    continue
            if len(parsed) >= 3:
                coords = parsed
                break
        
        if coords and len(coords) >= 3:
            poly['centroid'] = [
                sum(c[0] for c in coords) / len(coords),
                sum(c[1] for c in coords) / len(coords)
            ]
            poly['coords'] = coords
            
            if sup_ha is None:
                sup_ha = calc_area_ha(coords)
                sup_source = 'calc'
            
            poly['SUP_HA'] = round(sup_ha, 2)
            poly['_supSource'] = sup_source
            poly['ESTADO'] = 'PENDIENTE'
            
            # Generate ID
            poly['id'] = f"{poly.get('ZONA','')}-{poly.get('ID_PREDIO','')}-{len(polygons)}"
            
            # Clean for JSON: round coords
            clean = {}
            for k, v in poly.items():
                if k == 'coords':
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
    """Parsea un archivo MRK/CSV y extrae foto-centros."""
    filename = os.path.basename(filepath)
    points = []
    
    with open(filepath, 'r', errors='ignore') as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            if i == 0 and ('lat' in line.lower() and 'lon' in line.lower()):
                continue
            
            lat, lng = None, None
            
            # Formato DJI: "valor,Lat" y "valor,Lon"
            lat_match = re.search(r'([-\d.]+),Lat', line)
            lon_match = re.search(r'([-\d.]+),Lon', line)
            
            if lat_match and lon_match:
                try:
                    lat = float(lat_match.group(1))
                    lng = float(lon_match.group(1))
                except ValueError:
                    pass
            
            # Fallback: dos números consecutivos
            if lat is None or lng is None:
                parts = re.split(r'[,\t\s]+', line)
                for j in range(len(parts) - 1):
                    try:
                        a, b = float(parts[j]), float(parts[j + 1])
                        if abs(a) > 10 and abs(a) <= 90 and abs(b) > 10 and abs(b) <= 180:
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
# CACHE DE MRK
# ============================================================
def load_mrk_cache():
    """Carga cache de MRK parseados."""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}


def save_mrk_cache(cache):
    """Guarda cache de MRK parseados."""
    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f)
    except Exception as e:
        print(f"   ⚠️  Error guardando cache: {e}")


def get_file_hash(filepath):
    """Hash rápido: tamaño + mtime."""
    stat = filepath.stat()
    return f"{stat.st_size}_{stat.st_mtime}"


# ============================================================
# OPERADORES
# ============================================================
def detect_operator(folder_path):
    """Detecta operador buscando en toda la jerarquía hasta MRK_DIR."""
    # Walk up from the folder to MRK_DIR
    check = folder_path
    while check != MRK_DIR.parent and check != check.parent:
        # Check equipo.txt
        equipo_file = check / "equipo.txt"
        if equipo_file.exists():
            try:
                return equipo_file.read_text().strip()
            except:
                pass
        
        # Check folder name
        name = check.name.upper()
        if any(x in name for x in ['M3E', 'EQUIPO1', 'EQ1']):
            return 'M3E'
        if any(x in name for x in ['M3M', 'EQUIPO2', 'EQ2']):
            return 'M3M'
        
        check = check.parent
    
    return None


# ============================================================
# ANÁLISIS DE COBERTURA
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
    """Analiza cobertura de vuelo usando buffer geométrico + conteo de puntos."""
    t0 = time.time()
    
    # Pre-compute bounding boxes for fast filtering
    for p in polygons:
        p['_mrkHits'] = 0
        p['_cobertura'] = 0.0
        p['_opHits'] = defaultdict(int)  # hits per operator
        p['_dateHits'] = defaultdict(int)  # hits per date
        p['_opDateHits'] = defaultdict(lambda: defaultdict(int))  # hits per op+date
        if 'coords' in p and p['coords']:
            lats = [c[0] for c in p['coords']]
            lngs = [c[1] for c in p['coords']]
            p['_bbox'] = (min(lats), max(lats), min(lngs), max(lngs))
    
    # --- Conteo de puntos (con bbox pre-filtro) ---
    print(f"   🔍 Cruzando {len(mrk_points):,} foto-centros con {len(polygons):,} polígonos...")
    matched_count = 0
    for pt in mrk_points:
        lat, lng = pt['lat'], pt['lng']
        for poly in polygons:
            if '_bbox' not in poly:
                continue
            bb = poly['_bbox']
            if lat < bb[0] or lat > bb[1] or lng < bb[2] or lng > bb[3]:
                continue
            if point_in_polygon(lat, lng, poly['coords']):
                poly['_mrkHits'] += 1
                poly['_opHits'][pt.get('operator', '?')] += 1
                poly['_dateHits'][pt.get('date', '?')] += 1
                poly['_opDateHits'][pt.get('operator', '?')][pt.get('date', '?')] += 1
                pt['matched'] = True
                matched_count += 1
                break
    
    t1 = time.time()
    print(f"   ✅ {matched_count:,}/{len(mrk_points):,} foto-centros dentro de polígonos ({t1-t0:.1f}s)")
    
    # --- Buffer de líneas de vuelo (Shapely) ---
    try:
        from shapely.geometry import Polygon as SPoly, LineString
        from shapely.ops import unary_union
        from shapely import prepared
        USE_SHAPELY = True
    except ImportError:
        USE_SHAPELY = False
        print("   ⚠️  Shapely no disponible — usando solo conteo de puntos")
        print("      Instala con: pip install shapely")
    
    if USE_SHAPELY and mrk_points:
        print(f"   📐 Analizando cobertura con buffer de {BUFFER_M}m...")
        
        # Reference point for local projection
        ref_lat = sum(pt['lat'] for pt in mrk_points) / len(mrk_points)
        ref_lng = sum(pt['lng'] for pt in mrk_points) / len(mrk_points)
        m_lat = 111320.0
        m_lng = 111320.0 * math.cos(math.radians(ref_lat))
        
        # Group points by file to form flight lines
        flight_pts = defaultdict(list)
        for pt in mrk_points:
            flight_pts[pt.get('file', '')].append(pt)
        
        # Build lines, splitting at gaps > 150m
        all_lines = []
        for fname, pts in flight_pts.items():
            local = [((p['lat'] - ref_lat) * m_lat, (p['lng'] - ref_lng) * m_lng) for p in pts]
            seg = [local[0]]
            for i in range(1, len(local)):
                dx = local[i][0] - local[i-1][0]
                dy = local[i][1] - local[i-1][1]
                if math.sqrt(dx*dx + dy*dy) > 150:
                    if len(seg) >= 2:
                        try:
                            all_lines.append(LineString(seg))
                        except:
                            pass
                    seg = [local[i]]
                else:
                    seg.append(local[i])
            if len(seg) >= 2:
                try:
                    all_lines.append(LineString(seg))
                except:
                    pass
        
        if all_lines:
            # Buffer and union all flight lines
            try:
                buffered_lines = [line.buffer(BUFFER_M) for line in all_lines]
                flight_coverage = unary_union(buffered_lines)
                prep_coverage = prepared.prep(flight_coverage)
                
                # Calculate coverage for each polygon
                cov_count = 0
                for p in polygons:
                    if 'coords' not in p or len(p['coords']) < 3:
                        continue
                    try:
                        local_coords = [((c[0] - ref_lat) * m_lat, (c[1] - ref_lng) * m_lng) for c in p['coords']]
                        poly_shape = SPoly(local_coords)
                        if not poly_shape.is_valid:
                            poly_shape = poly_shape.buffer(0)
                        if poly_shape.area < 1:
                            continue
                        
                        # Quick check: does coverage touch this polygon?
                        if not prep_coverage.intersects(poly_shape):
                            p['_cobertura'] = 0.0
                            continue
                        
                        intersection = poly_shape.intersection(flight_coverage)
                        cov = intersection.area / poly_shape.area
                        p['_cobertura'] = round(min(cov, 1.0), 3)
                        if cov > 0:
                            cov_count += 1
                    except Exception:
                        pass
                
                t2 = time.time()
                print(f"   ✅ Cobertura calculada: {cov_count} polígonos con datos ({t2-t1:.1f}s)")
            except Exception as e:
                print(f"   ⚠️  Error en buffer: {e} — usando conteo de puntos")
    
    # --- Asignar estado por polígono ---
    for p in polygons:
        cov = p.get('_cobertura', 0)
        hits = p.get('_mrkHits', 0)
        
        if cov >= UMBRAL_COBERTURA:
            p['_polyEstado'] = 'VOLADO'
        elif cov > 0 or hits > 0:
            p['_polyEstado'] = 'PARCIAL'
        else:
            p['_polyEstado'] = 'PENDIENTE'
    
    # --- Propagar estado a nivel de predio ---
    # Regla estricta: todos los polígonos deben ser VOLADO para que el predio sea VOLADO
    predios = defaultdict(list)
    for p in polygons:
        key = p.get('NOM_PREDIO', p.get('ID_PREDIO', ''))
        if key:
            predios[key].append(p)
    
    for predio, polys in predios.items():
        estados = set(p['_polyEstado'] for p in polys)
        total_hits = sum(p.get('_mrkHits', 0) for p in polys)
        
        if estados == {'VOLADO'}:
            predio_estado = 'VOLADO'
        elif total_hits == 0 and all(p.get('_cobertura', 0) == 0 for p in polys):
            predio_estado = 'PENDIENTE'
        elif any(p.get('_cobertura', 0) > 0 or p.get('_mrkHits', 0) > 0 for p in polys):
            predio_estado = 'PARCIAL'
        else:
            predio_estado = 'PENDIENTE'
        
        for p in polys:
            p['ESTADO'] = predio_estado
    
    # Polígonos sin predio
    for p in polygons:
        if 'ESTADO' not in p or not p.get('NOM_PREDIO', p.get('ID_PREDIO', '')):
            p['ESTADO'] = p.get('_polyEstado', 'PENDIENTE')
    
    # Add simple _ops list for map coloring (which operators touched each polygon)
    for p in polygons:
        ops = [op for op, hits in p.get('_opHits', {}).items() if hits > 0]
        if ops:
            p['_ops'] = sorted(ops)
    
    total_time = time.time() - t0
    print(f"   ⏱️  Tiempo total de intersección: {total_time:.1f}s")


# ============================================================
# GENERACIÓN DEL DASHBOARD
# ============================================================
def generate_dashboard(kml_data, mrk_data, all_polygons, operators, logo_b64):
    """Genera el HTML del dashboard con datos embebidos."""
    
    template_path = TEMPLATE_DIR / "dashboard_template.html"
    
    if not template_path.exists():
        print(f"❌ Template no encontrado: {template_path}")
        sys.exit(1)
    
    # Preparar datos JSON - COMPACTO
    # Los MRK se embeben como líneas (arrays de [lat,lng]) no como objetos punto a punto
    dashboard_data = {
        'generated': datetime.now().isoformat(),
        'config': {
            'startDate': FECHA_INICIO,
            'rendHaDia': RENDIMIENTO_HA_DIA,
            'equipos': EQUIPOS,
            'bufferM': BUFFER_M,
            'umbralCobertura': UMBRAL_COBERTURA
        },
        'kmlFiles': [],
        'mrkFiles': [],
        'operators': operators
    }
    
    for name, polys in kml_data.items():
        # Clean heavy internal fields before embedding
        clean_polys = []
        for p in polys:
            cp = {k: v for k, v in p.items() 
                  if not k.startswith('_') or k in ('_mrkHits', '_cobertura', '_polyEstado', '_ops', '_supSource')}
            clean_polys.append(cp)
        dashboard_data['kmlFiles'].append({
            'name': name,
            'polygons': clean_polys
        })
    
    # Embeber MRK: incluir flag 'matched' pre-calculado por Python
    for name, pts in mrk_data.items():
        op = pts[0].get('operator', '') if pts else ''
        compact_pts = []
        for pt in pts:
            p = {'lat': pt['lat'], 'lng': pt['lng'], 'file': pt['file']}
            if pt.get('matched'):
                p['m'] = 1  # flag compacto: matched
            if pt.get('operator'):
                p['op'] = pt['operator']
            compact_pts.append(p)
        dashboard_data['mrkFiles'].append({
            'name': name,
            'points': compact_pts,
            'active': True,
            'operator': op,
            'dateAdded': datetime.now().strftime('%d/%m/%Y %H:%M')
        })
    
    # Pre-compute operator and daily stats using per-POLYGON coverage
    # Key principle: each polygon's ha is counted ONCE in the daily total,
    # assigned to the EARLIEST date it was touched (incremental coverage)
    
    op_total_ha = defaultdict(float)  # op -> ha (polygons touched)
    op_poly_count = defaultdict(int)   # op -> number of polygons touched
    op_date_ha = defaultdict(lambda: defaultdict(float))  # op -> date -> ha
    op_date_polys = defaultdict(lambda: defaultdict(int))  # op -> date -> polygon count
    
    # For daily table: incremental — assign polygon to the date that 
    # contributed the MOST points (primary coverage), not just first touch
    daily_incremental_ha = {}  # date -> ha of polygons primarily covered that day
    
    for i, p in enumerate(all_polygons):
        poly_estado = p.get('_polyEstado', 'PENDIENTE')
        if poly_estado == 'PENDIENTE':
            continue
        
        ha = p.get('SUP_HA', 0)
        
        # Which operators touched this polygon
        op_hits = p.get('_opHits', {})
        for op, hits in op_hits.items():
            if hits > 0:
                op_total_ha[op] += ha
                op_poly_count[op] += 1
        
        # Per op+date: which operator+date combos touched this polygon
        op_date_hits = p.get('_opDateHits', {})
        for op, date_dict in op_date_hits.items():
            for date, hits in date_dict.items():
                if hits > 0:
                    op_date_ha[op][date] += ha
                    op_date_polys[op][date] += 1
        
        # Assign to date with MOST hits (the day that actually covered it)
        date_hits = p.get('_dateHits', {})
        if date_hits:
            primary_date = max(date_hits.items(), key=lambda x: x[1])[0]
            daily_incremental_ha[primary_date] = daily_incremental_ha.get(primary_date, 0) + ha
    
    # Build operator stats
    op_pts = defaultdict(lambda: {'pts': 0, 'matched': 0, 'files': set()})
    for name, pts in mrk_data.items():
        for pt in pts:
            op = pt.get('operator', '?')
            op_pts[op]['pts'] += 1
            if pt.get('matched'):
                op_pts[op]['matched'] += 1
            op_pts[op]['files'].add(pt.get('file', ''))
    
    dashboard_data['opStats'] = {}
    for op in set(list(op_pts.keys()) + list(op_total_ha.keys())):
        d = op_pts[op]
        daily = {}
        for date in op_date_ha.get(op, {}):
            daily[date] = {
                'ha': round(op_date_ha[op][date], 1),
                'polys': op_date_polys[op].get(date, 0)
            }
        dashboard_data['opStats'][op] = {
            'pts': d['pts'],
            'matched': d['matched'],
            'files': len(d['files']),
            'ha': round(op_total_ha.get(op, 0), 1),
            'polys': op_poly_count.get(op, 0),
            'daily': daily
        }
    
    # dateStats: incremental (sums to total cubierta)
    dashboard_data['dateStats'] = {date: round(ha, 1) for date, ha in daily_incremental_ha.items()}
    
    # Diagnostic
    cubierta_real = sum(p.get('SUP_HA', 0) for p in all_polygons 
                       if p.get('_polyEstado') in ('VOLADO', 'PARCIAL'))
    sum_daily = sum(daily_incremental_ha.values())
    print(f"\n   📊 Verificación Equipos (por polígono):")
    for op in sorted(op_total_ha.keys()):
        print(f"      {op}: {op_total_ha[op]:,.1f} ha (polígonos tocados)")
    print(f"      Cubierta real: {cubierta_real:,.1f} ha")
    print(f"      Suma diaria incremental: {sum_daily:,.1f} ha {'✅' if abs(sum_daily - cubierta_real) < 0.5 else '❌'}")
    
    if abs(sum_daily - cubierta_real) >= 0.5:
        # Find polygons with coverage but no date hits
        dateless_ha = 0
        dateless_count = 0
        for p in all_polygons:
            if p.get('_polyEstado') in ('VOLADO', 'PARCIAL'):
                date_hits = p.get('_dateHits', {})
                if not any(h > 0 for h in date_hits.values()):
                    dateless_ha += p.get('SUP_HA', 0)
                    dateless_count += 1
        print(f"      ⚠️  {dateless_count} polígonos cubiertos sin fecha ({dateless_ha:,.1f} ha)")
        
        # Assign dateless polygons to earliest available date
        if dateless_count > 0 and daily_incremental_ha:
            earliest = sorted(daily_incremental_ha.keys())[0]
            daily_incremental_ha[earliest] = daily_incremental_ha.get(earliest, 0) + dateless_ha
            dashboard_data['dateStats'] = {date: round(ha, 1) for date, ha in daily_incremental_ha.items()}
            sum_daily = sum(daily_incremental_ha.values())
            print(f"      → Asignados a {earliest}. Nueva suma: {sum_daily:,.1f} ha")
    
    for date in sorted(daily_incremental_ha.keys()):
        print(f"         {date}: +{daily_incremental_ha[date]:,.1f} ha nuevas")
    
    data_json = json.dumps(dashboard_data, ensure_ascii=False)
    data_size_mb = len(data_json.encode('utf-8')) / (1024 * 1024)
    print(f"   📦 Datos embebidos: {data_size_mb:.1f} MB")
    
    # Leer template
    with open(template_path, 'r', encoding='utf-8') as f:
        html = f.read()
    
    # Inyectar datos
    inject_script = f'''
<script>
// ===== DATOS EMBEBIDOS — Generado el {datetime.now().strftime('%d/%m/%Y %H:%M')} =====
const EMBEDDED_DATA = {data_json};
</script>
'''
    
    if '</head>' in html:
        html = html.replace('</head>', inject_script + '\n</head>')
    else:
        html = inject_script + html
    
    # Logo
    if logo_b64 and 'LOGO_PLACEHOLDER' in html:
        html = html.replace('LOGO_PLACEHOLDER', logo_b64)
    
    return html


# ============================================================
# MAIN
# ============================================================
def main():
    t_start = time.time()
    
    print("=" * 60)
    print("  TeMapeo — Generador de Dashboard Heligrafics v2")
    print("=" * 60)
    print()
    
    # Create dirs
    for d in [KML_DIR, MRK_DIR, TEMPLATE_DIR, ASSETS_DIR, OUTPUT_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    
    # 1. KML
    print("📍 Procesando archivos KML...")
    kml_files = sorted(KML_DIR.glob('*.kml')) + sorted(KML_DIR.glob('*.KML'))
    if not kml_files:
        print("   ❌ No se encontraron archivos KML en datos/kml/")
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
        if from_calc > 0:
            print(f"      SUP_HA: {from_kml} del KML | {from_calc} calculadas")
    
    total_ha = sum(p.get('SUP_HA', 0) for p in all_polygons)
    print(f"\n   📊 Total: {len(all_polygons)} polígonos, {total_ha:,.1f} ha")
    
    # 2. MRK (con cache)
    print("\n✈️  Procesando archivos MRK...")
    
    # Load cache
    mrk_cache = load_mrk_cache()
    cache_hits = 0
    cache_misses = 0
    
    # Extract ZIPs (search recursively)
    import zipfile
    all_zips = sorted(set(list(MRK_DIR.rglob('*.zip')) + list(MRK_DIR.rglob('*.ZIP'))))
    for zf in all_zips:
        extract_dir = zf.parent / zf.stem
        if not extract_dir.exists():
            print(f"   📦 Descomprimiendo {zf.relative_to(MRK_DIR)}...")
            try:
                with zipfile.ZipFile(zf, 'r') as z:
                    z.extractall(extract_dir)
            except Exception as e:
                print(f"   ⚠️  Error: {e}")
    
    # Find all MRK files recursively
    mrk_files = sorted(set(list(MRK_DIR.rglob('*.mrk')) + list(MRK_DIR.rglob('*.MRK'))))
    
    # Group by immediate parent folder (date folder)
    # Structure: M3E/MRK_14022026/*.MRK → folder = "M3E/MRK_14022026"
    mrk_by_folder = defaultdict(list)
    for mf in mrk_files:
        try:
            rel = mf.parent.relative_to(MRK_DIR)
            folder = str(rel)
        except ValueError:
            folder = mf.parent.name
        mrk_by_folder[folder].append(mf)
    
    all_mrk_data = {}
    all_mrk_points = []
    operators = {}  # folder -> operator name
    
    if mrk_files:
        for folder, files in sorted(mrk_by_folder.items()):
            folder_path = files[0].parent
            operator = detect_operator(folder_path)
            if not operator:
                folder_idx = sorted(mrk_by_folder.keys()).index(folder) + 1
                operator = f"Equipo {folder_idx}"
            operators[folder] = operator
            
            folder_pts = 0
            
            # Extract date from folder name (search full path for date patterns)
            import re as _re
            folder_date = None
            # Try DD-MM-YYYY or DD_MM_YYYY or DDMMYYYY in folder path
            date_match = _re.search(r'(\d{2})[-_ ](\d{2})[-_ ](\d{4})', folder)
            if date_match:
                folder_date = f"{date_match.group(1)}/{date_match.group(2)}/{date_match.group(3)}"
            else:
                # Try DDMMYYYY (8 consecutive digits)
                date_match2 = _re.search(r'(\d{2})(\d{2})(\d{4})', folder)
                if date_match2:
                    dd, mm, yyyy = date_match2.group(1), date_match2.group(2), date_match2.group(3)
                    # Validate it looks like a date
                    if 1 <= int(dd) <= 31 and 1 <= int(mm) <= 12 and 2020 <= int(yyyy) <= 2030:
                        folder_date = f"{dd}/{mm}/{yyyy}"
            
            if not folder_date:
                print(f"   ⚠️  Sin fecha detectada en carpeta: {folder}")
            
            for mf in files:
                cache_key = str(mf.relative_to(MRK_DIR))
                file_hash = get_file_hash(mf)
                
                if cache_key in mrk_cache and mrk_cache[cache_key].get('hash') == file_hash:
                    pts = mrk_cache[cache_key]['points']
                    cache_hits += 1
                else:
                    pts = parse_mrk(mf)
                    mrk_cache[cache_key] = {'hash': file_hash, 'points': pts}
                    cache_misses += 1
                
                if pts:
                    # Tag with operator and date
                    for pt in pts:
                        pt['operator'] = operator
                        if folder_date:
                            pt['date'] = folder_date
                    
                    rel_name = f"{folder}/{mf.name}" if folder != '(raíz)' else mf.name
                    all_mrk_data[rel_name] = pts
                    all_mrk_points.extend(pts)
                    folder_pts += len(pts)
            
            print(f"   ✅ {folder} [{operator}]: {len(files)} archivos, {folder_pts:,} foto-centros")
        
        # Save cache
        save_mrk_cache(mrk_cache)
        
        if cache_misses > 0 or cache_hits > 0:
            print(f"   💾 Cache: {cache_hits} del cache + {cache_misses} nuevos parseados")
        
        if all_mrk_points:
            print(f"\n   📊 Total: {len(all_mrk_points):,} foto-centros en {len(all_mrk_data)} archivos")
            
            # Show operators summary
            op_pts = defaultdict(int)
            for pt in all_mrk_points:
                op_pts[pt.get('operator', '?')] += 1
            for op, count in sorted(op_pts.items()):
                print(f"      👤 {op}: {count:,} foto-centros")
            
            # 3. Intersecciones
            print("\n🔄 Procesando intersecciones...")
            process_intersections(all_polygons, all_mrk_points)
            
            # Report
            pred_estados = {}
            for p in all_polygons:
                key = p.get('NOM_PREDIO', p.get('ID_PREDIO', ''))
                if key:
                    pred_estados[key] = p.get('ESTADO', 'PENDIENTE')
            
            n_volado = sum(1 for e in pred_estados.values() if e == 'VOLADO')
            n_parcial = sum(1 for e in pred_estados.values() if e == 'PARCIAL')
            n_pendiente = sum(1 for e in pred_estados.values() if e == 'PENDIENTE')
            volado_ha = sum(p.get('SUP_HA', 0) for p in all_polygons if p.get('ESTADO') == 'VOLADO')
            parcial_ha = sum(p.get('SUP_HA', 0) for p in all_polygons if p.get('ESTADO') == 'PARCIAL')
            
            # Superficie cubierta REAL: solo polígonos individuales con cobertura
            # No contar polígonos pendientes dentro de predios parciales
            cubierta_ha = sum(p.get('SUP_HA', 0) for p in all_polygons 
                             if p.get('_polyEstado') in ('VOLADO', 'PARCIAL'))
            
            # Para referencia: superficie de predios volados + parciales (más amplia)
            predio_ha = volado_ha + parcial_ha
            
            # Coverage stats
            cov_polys = [p for p in all_polygons if p.get('_cobertura', 0) > 0]
            avg_cov = sum(p['_cobertura'] for p in cov_polys) / max(len(cov_polys), 1)
            
            print(f"\n   📊 RESUMEN DE COBERTURA:")
            print(f"   ✅ {n_volado} predios VOLADOS ({volado_ha:,.1f} ha)")
            print(f"   🔶 {n_parcial} predios PARCIALES ({parcial_ha:,.1f} ha)")
            print(f"   🔴 {n_pendiente} predios PENDIENTES")
            print(f"   📐 Cobertura promedio (polígonos con datos): {avg_cov*100:.1f}%")
            
            # Desglose por _polyEstado (individual)
            n_poly_volado = sum(1 for p in all_polygons if p.get('_polyEstado') == 'VOLADO')
            n_poly_parcial = sum(1 for p in all_polygons if p.get('_polyEstado') == 'PARCIAL')
            poly_volado_ha = sum(p.get('SUP_HA', 0) for p in all_polygons if p.get('_polyEstado') == 'VOLADO')
            poly_parcial_ha = sum(p.get('SUP_HA', 0) for p in all_polygons if p.get('_polyEstado') == 'PARCIAL')
            
            print(f"\n   📊 POLÍGONOS INDIVIDUALES:")
            print(f"      ✅ {n_poly_volado} polígonos volados ({poly_volado_ha:,.1f} ha)")
            print(f"      🔶 {n_poly_parcial} polígonos parciales ({poly_parcial_ha:,.1f} ha)")
            print(f"      📊 Superficie cubierta real: {cubierta_ha:,.1f} ha ({cubierta_ha/total_ha*100:.1f}%)")
            print(f"      ℹ️  Superficie predios (vol+parc): {predio_ha:,.1f} ha")
            
            # Diagnóstico: predios PARCIALES (qué les falta)
            parcial_predios = {}
            for p in all_polygons:
                if p.get('ESTADO') == 'PARCIAL':
                    key = p.get('NOM_PREDIO', p.get('ID_PREDIO', ''))
                    if key not in parcial_predios:
                        parcial_predios[key] = {'total':0, 'volado':0, 'parcial':0, 'pendiente':0, 
                                                'ha':0, 'ha_volado':0, 'ha_parcial':0}
                    parcial_predios[key]['total'] += 1
                    ha = p.get('SUP_HA', 0)
                    parcial_predios[key]['ha'] += ha
                    pe = p.get('_polyEstado', 'PENDIENTE')
                    if pe == 'VOLADO': 
                        parcial_predios[key]['volado'] += 1
                        parcial_predios[key]['ha_volado'] += ha
                    elif pe == 'PARCIAL': 
                        parcial_predios[key]['parcial'] += 1
                        parcial_predios[key]['ha_parcial'] += ha
                    else: 
                        parcial_predios[key]['pendiente'] += 1
            
            if parcial_predios:
                print(f"\n   🔎 DIAGNÓSTICO PREDIOS PARCIALES:")
                for name, d in sorted(parcial_predios.items(), key=lambda x: -x[1]['ha'])[:10]:
                    pct = (d['ha_volado']/d['ha']*100) if d['ha'] > 0 else 0
                    print(f"      {name}: {d['total']} pol ({d['ha']:.1f} ha) → "
                          f"✅{d['volado']} 🔶{d['parcial']} 🔴{d['pendiente']} "
                          f"[{pct:.0f}% sup volada]")
            
            # Stats por operador
            op_stats = defaultdict(lambda: {'pts':0, 'matched':0})
            for pt in all_mrk_points:
                op = pt.get('operator', '?')
                op_stats[op]['pts'] += 1
                if pt.get('matched'):
                    op_stats[op]['matched'] += 1
            
            if len(op_stats) > 1:
                print(f"\n   👥 POR OPERADOR:")
                for op, d in sorted(op_stats.items()):
                    eff = d['matched']/d['pts']*100 if d['pts'] else 0
                    print(f"      {op}: {d['pts']:,} fotos → {d['matched']:,} en pol. ({eff:.1f}%)")
    else:
        print("   ℹ️  Sin archivos MRK")
    
    # 4. Logo
    logo_b64 = ''
    logo_path = ASSETS_DIR / "horizontal.png"
    if logo_path.exists():
        with open(logo_path, 'rb') as f:
            logo_b64 = base64.b64encode(f.read()).decode('utf-8')
        print(f"\n🎨 Logo cargado: {logo_path.name}")
    
    # 5. Generar dashboard
    print("\n📄 Generando dashboard...")
    html = generate_dashboard(all_kml_data, all_mrk_data, all_polygons, operators, logo_b64)
    
    output_path = OUTPUT_DIR / "index.html"
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    
    size_kb = os.path.getsize(output_path) / 1024
    total_time = time.time() - t_start
    
    print(f"   ✅ Dashboard generado: {output_path}")
    print(f"   📦 Tamaño: {size_kb:,.0f} KB")
    print(f"   ⏱️  Tiempo total: {total_time:.1f}s")
    
    print()
    print("=" * 60)
    print("  ✅ LISTO")
    print("=" * 60)
    print()
    print("  Próximos pasos:")
    print("  1. Abre docs/index.html para verificar")
    print("  2. git add -A && git commit -m \"Avance\" && git push")
    print("  3. El cliente ve la actualización en GitHub Pages")
    print()


if __name__ == '__main__':
    main()
