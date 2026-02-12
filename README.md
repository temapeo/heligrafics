# 🛩️ TeMapeo — Control de Avance Heligrafics

Dashboard de seguimiento de vuelos fotogramétricos para el proyecto Heligrafics FASA.

**URL del dashboard:** `https://temapeo.github.io/heligrafics/` *(actualizar con tu URL real)*

---

## 📁 Estructura del proyecto

```
heligrafics/
├── README.md                          ← este archivo
├── generar_dashboard.py               ← script que genera el dashboard
├── datos/
│   ├── kml/                           ← KML de solicitudes de vuelo
│   │   ├── 20260209_ChillanDron.kml
│   │   └── 20260126_FASA_ValdiviaDron.kml
│   └── mrk/                           ← MRK de vuelos ejecutados (se van sumando)
│       ├── 20260215_vuelo01.mrk
│       ├── 20260216_vuelo02.mrk
│       └── ...
├── template/
│   └── dashboard_template.html        ← template del dashboard
├── assets/
│   └── horizontal.png                 ← logo TeMapeo
└── docs/
    └── index.html                     ← dashboard generado (GitHub Pages)
```

---

## 🚀 Configuración inicial (una sola vez)

### 1. Crear repositorio en GitHub

```bash
# Crear la carpeta del proyecto
mkdir heligrafics
cd heligrafics
git init

# Copiar los archivos del proyecto
# - generar_dashboard.py
# - template/dashboard_template.html (el HTML del dashboard)
# - assets/horizontal.png (logo TeMapeo)
# - datos/kml/ (tus archivos KML)

# Primer commit
git add -A
git commit -m "Inicio proyecto Heligrafics"

# Crear repo en GitHub (desde github.com o con gh cli)
gh repo create temapeo/heligrafics --public
git remote add origin https://github.com/temapeo/heligrafics.git
git push -u origin main
```

### 2. Activar GitHub Pages

1. Ve a **github.com/temapeo/heligrafics** → **Settings** → **Pages**
2. En **Source** selecciona: **Deploy from a branch**
3. En **Branch** selecciona: **main** → carpeta **/docs**
4. Click **Save**
5. En 1-2 minutos tu dashboard estará en: `https://temapeo.github.io/heligrafics/`

### 3. Generar el primer dashboard

```bash
# Asegúrate de tener los KML en datos/kml/
python3 generar_dashboard.py

# Subir
git add -A
git commit -m "Dashboard inicial"
git push
```

---

## 📅 Flujo diario

Cada día después de volar:

```bash
# 1. Copiar los MRK del día a la carpeta
cp /ruta/a/los/mrk/*.mrk datos/mrk/

# 2. Regenerar el dashboard
python3 generar_dashboard.py

# 3. Verificar localmente (opcional)
open docs/index.html   # macOS
# o xdg-open docs/index.html en Linux

# 4. Subir a GitHub
git add -A
git commit -m "Avance día $(date +%d-%m-%Y)"
git push
```

**¡Eso es todo!** El cliente recarga la página y ve el avance actualizado.

---

## 🔄 Si cambian los KML

Si Heligrafics envía polígonos actualizados:

1. Reemplaza el archivo en `datos/kml/` (mismo nombre = lo reemplaza)
2. Ejecuta `python3 generar_dashboard.py`
3. `git add -A && git commit -m "KML actualizado" && git push`

---

## ⚙️ Configuración del script

En `generar_dashboard.py` puedes ajustar:

```python
RENDIMIENTO_HA_DIA = 100     # hectáreas/día por equipo
EQUIPOS = 2                   # cantidad de equipos
FECHA_INICIO = "2026-02-15"   # fecha de inicio del proyecto
FOTOS_POR_HA = 80             # foto-centros esperados por hectárea
UMBRAL_VOLADO = 0.7           # 70% cobertura = polígono completado
```

---

## 📱 Acceso del cliente

Comparte esta URL con el cliente:
```
https://temapeo.github.io/heligrafics/
```

El cliente:
- ✅ Ve el avance actualizado cada vez que recargas
- ✅ Puede navegar por el mapa, filtrar por zona, ver cronograma
- ✅ Puede exportar reporte CSV y MRK como KMZ
- ❌ No puede modificar datos (solo tú subes MRK)

---

## 🛡️ Privacidad

- El repositorio puede ser **privado** en GitHub (necesitas GitHub Pro o usar una organización)
- Con repo privado, GitHub Pages sigue funcionando pero solo para colaboradores
- Alternativa: usar **Cloudflare Pages** o **Netlify** (gratis, soportan repos privados)

---

*TeMapeo.com — No vendemos mapas, entregamos decisiones*
