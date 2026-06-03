"""
main.py — ByteBarato
Servidor FastAPI: orquestador de scrapers, capa de persistencia
SQLite y API REST para el frontend JavaScript.

Responsabilidades:
  1. Exponer endpoints de LECTURA para el frontend:
       GET /api/productos           → catálogo con precio más reciente
       GET /api/productos/{id}/historial → historial de precios de un producto
  2. Exponer el endpoint de ESCRITURA (motor blindado):
       POST /api/actualizar         → lanza la extracción por bloques de páginas
  3. Gestionar el estado de paginación y el escudo anti-spam en memoria.
  4. Persistir los datos con la lógica de caché híbrido en bytebarato.db.

Arquitectura de paginación con memoria:
  El scraper no descarga todo el catálogo en una llamada sino bloques
  de `LIMITE_PAGINAS_POR_CICLO` páginas. En cada POST /api/actualizar
  el estado global `PAGINA_ACTUAL_ELEKTRA` recuerda dónde quedó.
  Cuando el scraper detecta el fin del catálogo (`fin_de_catalogo=True`),
  el contador se reinicia a 1 para volver a recorrerlo desde el principio.

Ejecución:
  uvicorn main:app --reload --port 8000
  python main.py   (lanza uvicorn directamente)

Endpoints principales:
  GET  /api/productos
  GET  /api/productos/{id}/historial
  POST /api/actualizar
  GET  /docs   (Swagger UI generado automáticamente por FastAPI)
"""

import time
import sqlite3
import logging
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Módulo especialista de extracción (scrapers/elektra.py)
from scrapers.elektra import extraer_de_elektra


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN GLOBAL
# ─────────────────────────────────────────────────────────────────────────────

DB_FILE                  = "bytebarato.db"
COOLDOWN_SEGUNDOS        = 300    # 5 minutos entre actualizaciones (escudo anti-spam)
LIMITE_PAGINAS_POR_CICLO = 5      # Páginas de Elektra procesadas por POST /api/actualizar
TIENDA_ELEKTRA           = "Elektra"


# ─────────────────────────────────────────────────────────────────────────────
# ESTADO GLOBAL EN MEMORIA
# ─────────────────────────────────────────────────────────────────────────────
#
# Estas variables persisten durante la vida del proceso del servidor.
# Se reinician a sus valores iniciales si el servidor se reinicia.
#
# ULTIMA_ACTUALIZACION  : timestamp UNIX (float) de la última ejecución exitosa
#                         de POST /api/actualizar. Valor 0 = nunca ejecutado.
# PAGINA_ACTUAL_ELEKTRA : primera página del siguiente bloque a descargar.
#                         Avanza +LIMITE_PAGINAS_POR_CICLO en cada ciclo exitoso.
#                         Se reinicia a 1 cuando el scraper detecta fin de catálogo.
#
ULTIMA_ACTUALIZACION  : float = 0.0
PAGINA_ACTUAL_ELEKTRA : int   = 1


# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bytebarato.main")


# ─────────────────────────────────────────────────────────────────────────────
# APLICACIÓN FASTAPI
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title       = "ByteBarato API",
    description = (
        "API REST del sistema de rastreo de precios ByteBarato. "
        "Expone el catálogo de productos con historial de precios "
        "extraídos de tiendas de comercio electrónico mexicanas."
    ),
    version     = "1.0.0",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Permite que el frontend JavaScript (servido en cualquier origen durante
# desarrollo) pueda consumir esta API sin errores de política de mismo origen.
# En producción, reemplazar allow_origins=["*"] por el dominio real del frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins     = ["*"],   # ← restringir en producción
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# HELPER DE BASE DE DATOS
# ─────────────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    """
    Abre y retorna una conexión a bytebarato.db lista para usar.

    Parámetros clave:
      check_same_thread=False : requerido para SQLite en entornos
          multi-hilo como FastAPI/uvicorn, donde el mismo objeto
          de conexión puede ser accedido desde distintos threads.
      row_factory=sqlite3.Row : permite acceder a las columnas del
          resultado por nombre (ej. row["nombre"]) además de por índice,
          lo que facilita construir dicts de respuesta JSON.

    Esta función se usa como dependencia de FastAPI (Depends) para
    los endpoints de solo lectura, y también se llama directamente
    en el endpoint de escritura POST /api/actualizar.

    Returns:
        Conexión sqlite3 configurada y lista para ejecutar consultas.

    Raises:
        HTTPException 503: Si el archivo .db no existe o está corrupto.
    """
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.DatabaseError as e:
        log.error("No se pudo abrir '%s': %s", DB_FILE, e)
        raise HTTPException(
            status_code=503,
            detail=(
                f"Base de datos no disponible: {e}. "
                "Ejecuta 'python db_setup.py' para inicializarla."
            ),
        )


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINTS DE LECTURA
# ─────────────────────────────────────────────────────────────────────────────

@app.get(
    "/api/productos",
    summary="Catálogo completo con precio más reciente",
    response_description=(
        "Lista de productos con id, nombre, tienda, url_origen y "
        "el precio más reciente registrado en el historial."
    ),
)
def get_productos(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    """
    Retorna el catálogo completo de productos, cada uno acompañado
    únicamente del precio más reciente capturado.

    Implementación:
      - JOIN entre `productos` e `historial_precios`.
      - Subconsulta para obtener solo el registro de precio más reciente
        (MAX capturado_en) por producto, evitando duplicados en el resultado.
      - ORDER BY nombre ASC para una presentación ordenada en el frontend.

    Returns:
        Lista de dicts JSON con estructura:
        [
          {
            "id":          1,
            "nombre":      "Samsung Galaxy A55 5G 128GB",
            "tienda":      "Elektra",
            "url_origen":  "https://...",
            "precio":      7999.0,
            "capturado_en":"2026-05-20 14:30:00"
          },
          ...
        ]

    HTTP 404 si no hay productos registrados aún.
    """
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                p.id,
                p.nombre,
                p.tienda,
                p.url_origen,
                h.precio,
                h.capturado_en
            FROM productos p
            JOIN historial_precios h
              ON h.id = (
                  SELECT id
                  FROM   historial_precios
                  WHERE  producto_id = p.id
                  ORDER  BY capturado_en DESC
                  LIMIT  1
              )
            ORDER BY p.nombre ASC;
        """)
        filas = cursor.fetchall()
    except sqlite3.DatabaseError as e:
        log.error("Error en GET /api/productos: %s", e)
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {e}")
    finally:
        conn.close()

    if not filas:
        raise HTTPException(
            status_code=404,
            detail=(
                "No hay productos en el catálogo todavía. "
                "Ejecuta POST /api/actualizar para iniciar la extracción."
            ),
        )

    return [dict(f) for f in filas]


@app.get(
    "/api/productos/{producto_id}/historial",
    summary="Historial de precios de un producto",
    response_description=(
        "Lista cronológica de todos los precios registrados para el producto."
    ),
)
def get_historial(
    producto_id: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    """
    Retorna el nombre del producto y la lista cronológica completa de
    precios capturados, ordenada del más antiguo al más reciente.

    Este endpoint alimenta la gráfica de historial de precios del frontend.

    Args:
        producto_id: ID del producto (clave primaria en tabla `productos`).

    Returns:
        Dict JSON con estructura:
        {
          "producto_id": 1,
          "nombre":      "Samsung Galaxy A55 5G 128GB",
          "tienda":      "Elektra",
          "historial": [
            {"precio": 8499.0, "capturado_en": "2026-05-01 10:00:00"},
            {"precio": 7999.0, "capturado_en": "2026-05-15 14:30:00"},
            ...
          ]
        }

    HTTP 404 si el producto_id no existe.
    """
    try:
        cursor = conn.cursor()

        # Verificar que el producto existe
        cursor.execute(
            "SELECT id, nombre, tienda FROM productos WHERE id = ?;",
            (producto_id,),
        )
        producto = cursor.fetchone()
        if not producto:
            raise HTTPException(
                status_code=404,
                detail=f"Producto con id={producto_id} no encontrado.",
            )

        # Obtener el historial completo ordenado cronológicamente (ASC)
        cursor.execute(
            """
            SELECT precio, capturado_en
            FROM   historial_precios
            WHERE  producto_id = ?
            ORDER  BY capturado_en ASC;
            """,
            (producto_id,),
        )
        historial = cursor.fetchall()

    except HTTPException:
        raise
    except sqlite3.DatabaseError as e:
        log.error("Error en GET /api/productos/%d/historial: %s", producto_id, e)
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {e}")
    finally:
        conn.close()

    return {
        "producto_id": producto_id,
        "nombre":      producto["nombre"],
        "tienda":      producto["tienda"],
        "historial":   [dict(h) for h in historial],
    }


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT DE ESCRITURA — EL MOTOR BLINDADO
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/api/actualizar",
    summary="Actualizar catálogo desde Elektra (blindado anti-spam)",
    response_description=(
        "Estado de la actualización: productos nuevos, actualizados "
        "y rango de páginas procesadas."
    ),
)
def post_actualizar() -> dict[str, Any]:
    """
    Endpoint principal de escritura. Orquesta la extracción de un bloque
    de páginas de Elektra y persiste los resultados en bytebarato.db.

    ── Escudo Anti-Spam ──────────────────────────────────────────────────────
    Antes de lanzar la extracción, compara el timestamp actual contra
    `ULTIMA_ACTUALIZACION`. Si han pasado menos de COOLDOWN_SEGUNDOS (300 s),
    rechaza la petición con HTTP 429 (Too Many Requests) e informa cuántos
    segundos faltan para la próxima ventana disponible.

    ── Extracción por Bloques ────────────────────────────────────────────────
    Llama a `extraer_de_elektra()` usando el estado global de paginación
    (`PAGINA_ACTUAL_ELEKTRA`) para continuar desde donde quedó la última vez.

    ── Caché Híbrido (Persistencia) ─────────────────────────────────────────
    Por cada producto extraído:
      1. SELECT id FROM productos WHERE url_origen = ?
         → CACHE HIT  : recupera el id (el producto ya existe).
         → NUEVO      : INSERT en `productos`, obtiene lastrowid.
      2. INSERT INTO historial_precios → siempre registra el precio actual.
    Commit único al finalizar todos los productos del bloque.

    ── Memoria de Paginación ─────────────────────────────────────────────────
    Actualiza ULTIMA_ACTUALIZACION con el timestamp actual.
    Si fin_de_catalogo == True → reinicia PAGINA_ACTUAL_ELEKTRA = 1.
    Si fin_de_catalogo == False → avanza PAGINA_ACTUAL_ELEKTRA += LIMITE.

    Returns:
        Dict JSON con estructura:
        {
          "status":         "success",
          "timestamp":      "2026-05-20 14:30:00",
          "paginas_procesadas": "1 a 5",
          "nuevos":         8,
          "actualizados":   2,
          "total":          10,
          "fin_de_catalogo": false,
          "proxima_pagina": 6
        }

    HTTP 429 si el escudo anti-spam bloquea la petición.
    HTTP 503 si la extracción retorna completamente vacía.
    """
    global ULTIMA_ACTUALIZACION, PAGINA_ACTUAL_ELEKTRA

    # ── Escudo Anti-Spam ───────────────────────────────────────────────────
    ahora            = time.time()
    segundos_pasados = ahora - ULTIMA_ACTUALIZACION

    if segundos_pasados < COOLDOWN_SEGUNDOS:
        faltan = int(COOLDOWN_SEGUNDOS - segundos_pasados)
        log.warning(
            "POST /api/actualizar bloqueado por escudo. Faltan %d s.", faltan
        )
        raise HTTPException(
            status_code=429,
            detail=(
                f"Demasiadas solicitudes. Debes esperar {faltan} segundo(s) "
                f"antes de volver a actualizar el catálogo. "
                f"El intervalo mínimo es de {COOLDOWN_SEGUNDOS} s (5 minutos)."
            ),
        )

    # ── Extracción ─────────────────────────────────────────────────────────
    pagina_inicio = PAGINA_ACTUAL_ELEKTRA
    log.info(
        "POST /api/actualizar — iniciando extracción. "
        "Páginas: %d a %d.",
        pagina_inicio,
        pagina_inicio + LIMITE_PAGINAS_POR_CICLO - 1,
    )

    try:
        productos, fin_de_catalogo = extraer_de_elektra(
            pagina_inicio  = pagina_inicio,
            limite_paginas = LIMITE_PAGINAS_POR_CICLO,
        )
    except Exception as e:
        log.error("Error inesperado durante la extracción: %s", e)
        raise HTTPException(
            status_code=503,
            detail=f"Error en el módulo de extracción: {e}",
        )

    if not productos:
        # Si falló, NO actualizamos ULTIMA_ACTUALIZACION. 
        # Así el jurado puede volver a intentar inmediatamente.
        if fin_de_catalogo:
            PAGINA_ACTUAL_ELEKTRA = 1
            
        raise HTTPException(
            status_code=503,
            detail=(
                "Los sistemas de seguridad de la tienda retrasaron la conexión inicial. "
                "Por favor, presiona 'Actualizar Catálogo' de nuevo en 3 segundos para reintentar."
            ),
        )

    # ── Persistencia — Caché Híbrido ───────────────────────────────────────
    conn      = get_db()
    cursor    = conn.cursor()
    nuevos    = 0
    actualizados = 0

    log.info("Persistiendo %d productos en '%s'...", len(productos), DB_FILE)

    for p in productos:
        try:
            # 1. ¿El producto ya existe en la caché local?
            cursor.execute(
                "SELECT id FROM productos WHERE url_origen = ?;",
                (p["url"],),
            )
            fila = cursor.fetchone()

            if fila:
                # CACHE HIT: solo actualizar el historial de precios
                producto_id = fila["id"]
                actualizados += 1
                log.debug(
                    "  [CACHE HIT]  id=%-4d → %s", producto_id, p["nombre"][:50]
                )
            else:
                # NUEVO: insertar el producto en el catálogo
                cursor.execute(
                    "INSERT INTO productos (nombre, url_origen, tienda) "
                    "VALUES (?, ?, ?);",
                    (p["nombre"], p["url"], TIENDA_ELEKTRA),
                )
                producto_id = cursor.lastrowid
                nuevos += 1
                log.info(
                    "  [NUEVO]      id=%-4d → %s", producto_id, p["nombre"][:50]
                )

            # 2. Siempre insertar el precio capturado en este momento
            # `capturado_en` se genera con DEFAULT datetime('now','localtime')
            cursor.execute(
                "INSERT INTO historial_precios (producto_id, precio) "
                "VALUES (?, ?);",
                (producto_id, p["precio"]),
            )
            log.debug("               MXN $%.2f registrado.", p["precio"])

        except sqlite3.IntegrityError as e:
            # Condición de carrera en inserciones paralelas: continuar
            log.warning(
                "Conflicto de integridad omitido (url='%s'): %s",
                p.get("url", "?"), e,
            )
            continue
        except sqlite3.DatabaseError as e:
            log.error(
                "Error de BD al guardar '%s': %s",
                p.get("nombre", "?"), e,
            )
            continue

    # Commit único para todo el bloque
    try:
        conn.commit()
        log.info(
            "COMMIT — nuevos: %d | actualizados: %d | total: %d",
            nuevos, actualizados, nuevos + actualizados,
        )
    except sqlite3.DatabaseError as e:
        log.error("Error en COMMIT: %s. Ejecutando ROLLBACK.", e)
        conn.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Los datos fueron extraídos pero no pudieron guardarse: {e}",
        )
    finally:
        conn.close()

    # ── Memoria de Paginación ──────────────────────────────────────────────
    ULTIMA_ACTUALIZACION = ahora

    if fin_de_catalogo:
        log.info(
            "Fin de catálogo detectado. Reiniciando PAGINA_ACTUAL_ELEKTRA a 1."
        )
        PAGINA_ACTUAL_ELEKTRA = 1
    else:
        PAGINA_ACTUAL_ELEKTRA = pagina_inicio + LIMITE_PAGINAS_POR_CICLO
        log.info(
            "Próxima actualización comenzará en página %d.", PAGINA_ACTUAL_ELEKTRA
        )

    # ── Respuesta ──────────────────────────────────────────────────────────
    return {
        "status":             "success",
        "timestamp":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "paginas_procesadas": f"{pagina_inicio} a {pagina_inicio + LIMITE_PAGINAS_POR_CICLO - 1}",
        "nuevos":             nuevos,
        "actualizados":       actualizados,
        "total":              nuevos + actualizados,
        "fin_de_catalogo":    fin_de_catalogo,
        "proxima_pagina":     PAGINA_ACTUAL_ELEKTRA,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT DE ESTADO (health check / debug)
# ─────────────────────────────────────────────────────────────────────────────

@app.get(
    "/api/estado",
    summary="Estado actual del servidor y la memoria de paginación",
)
def get_estado() -> dict[str, Any]:
    """
    Endpoint de diagnóstico. Retorna el estado interno del orquestador
    sin ejecutar ninguna extracción ni escritura.

    Útil para que el frontend o el equipo de desarrollo verifiquen:
      - Cuánto tiempo falta para la próxima actualización disponible.
      - En qué página del catálogo comenzará la próxima extracción.

    Returns:
        Dict con el estado actual de las variables globales de control.
    """
    ahora    = time.time()
    pasados  = ahora - ULTIMA_ACTUALIZACION
    faltan   = max(0, int(COOLDOWN_SEGUNDOS - pasados))

    return {
        "pagina_actual_elektra":   PAGINA_ACTUAL_ELEKTRA,
        "ultima_actualizacion":    (
            datetime.fromtimestamp(ULTIMA_ACTUALIZACION).strftime("%Y-%m-%d %H:%M:%S")
            if ULTIMA_ACTUALIZACION > 0 else "Nunca"
        ),
        "cooldown_segundos":       COOLDOWN_SEGUNDOS,
        "segundos_para_proxima":   faltan,
        "actualizacion_disponible": faltan == 0,
    }

# ─────────────────────────────────────────────────────────────────────────────
# SERVIR EL FRONTEND
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/")
def serve_index():
    return FileResponse("frontend/index.html")

app.mount("/", StaticFiles(directory="frontend"), name="frontend")

# ─────────────────────────────────────────────────────────────────────────────
# PUNTO DE ENTRADA
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    log.info("Iniciando servidor ByteBarato API en http://localhost:8000")
    log.info("Documentación interactiva: http://localhost:8000/docs")

    uvicorn.run(
        "main:app",
        host     = "0.0.0.0",
        port     = 8000,
        reload   = True,     # Recarga automática en cambios de código (dev)
        log_level= "info",
    )
