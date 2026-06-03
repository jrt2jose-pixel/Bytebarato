"""
scrapers/elektra.py — ByteBarato
Módulo especialista en la extracción de smartphones desde Elektra México.

Estrategia: JSON-LD (Schema.org ItemList)
─────────────────────────────────────────
Elektra publica su catálogo de productos en bloques estructurados
<script type="application/ld+json"> con el esquema "@type": "ItemList".
Esto es preferible al scraping de HTML crudo porque:
  - No depende de clases CSS que cambian con rediseños del frontend.
  - Los datos ya vienen tipados y limpios desde el servidor.
  - Es la misma fuente que usa Google Shopping: estable y mantenida.

Paginación con memoria:
  La función acepta `pagina_inicio` y `limite_paginas` para que el
  orquestador (main.py) recuerde qué páginas ya procesó y continue
  desde donde lo dejó en la siguiente ejecución programada.

Retorno de tupla:
  (lista_de_productos, fin_de_catalogo: bool)
  El bool le indica al orquestador si debe reiniciar el contador
  de páginas en la siguiente invocación.

Responsabilidades de este módulo:
  - HTTP + parseo HTML + extracción JSON-LD.
  - NO interactúa con la base de datos.
  - NO tiene estado global propio.

Uso directo (prueba aislada):
  python -m scrapers.elektra
"""

import json
import time
import random
import logging
from typing import Optional

import requests
from bs4 import BeautifulSoup


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

# URL base del catálogo de smartphones de Elektra MX.
# La paginación se construye añadiendo ?page=N para N > 1.
_URL_BASE    = "https://www.elektra.mx/telefonia/celulares/smartphones"
_TIMEOUT     = 20      # Segundos antes de abortar la petición HTTP
_PAUSA_MIN   = 1.5     # Espera mínima entre páginas (cortesía + anti-bloqueo)
_PAUSA_MAX   = 3.0     # Espera máxima entre páginas

log = logging.getLogger("bytebarato.scrapers.elektra")

# Headers que imitan Chrome 124 en Windows 10.
# Elektra usa Cloudflare CDN; una huella coherente reduce los bloqueos 403.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language":           "es-MX,es;q=0.9,en;q=0.8",
    "Accept-Encoding":           "gzip, deflate, br",
    "Connection":                "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Sec-Fetch-User":            "?1",
    "Cache-Control":             "max-age=0",
    "DNT":                       "1",
}


# ─────────────────────────────────────────────────────────────────────────────
# UTILIDADES INTERNAS
# ─────────────────────────────────────────────────────────────────────────────

def _construir_url(pagina: int) -> str:
    """
    Construye la URL paginada del catálogo de Elektra.

    La página 1 usa la URL base sin parámetros (URL canónica limpia).
    Las páginas siguientes añaden ?page=N para acceder a resultados
    subsecuentes del catálogo.

    Args:
        pagina: Número de página (1-based).

    Returns:
        URL completa de la página solicitada como string.
    """
    if pagina <= 1:
        return _URL_BASE
    return f"{_URL_BASE}?page={pagina}"


def _descargar_html(url: str, sesion: requests.Session) -> Optional[BeautifulSoup]:
    """
    Descarga el HTML de la URL usando la sesión de requests y lo parsea.

    Verifica que la respuesta sea sustancial (>500 bytes) para detectar
    páginas de error o retos de Cloudflare que devuelven 200 con body vacío.

    Args:
        url   : URL a descargar.
        sesion: Sesión de requests activa (mantiene cookies entre páginas).

    Returns:
        BeautifulSoup del HTML, o None si la descarga falló.
    """
    try:
        log.info("  GET %s", url)
        resp = sesion.get(url, headers=_HEADERS, timeout=_TIMEOUT)

        # 404 es señal explícita de fin de catálogo, no un error de red
        if resp.status_code == 404:
            log.info("  → HTTP 404 recibido. Fin de catálogo.")
            return None

        resp.raise_for_status()

        if len(resp.text) < 500:
            log.warning(
                "  → Respuesta corta (%d bytes). Posible bloqueo.", len(resp.text)
            )
            return None

        return BeautifulSoup(resp.text, "html.parser")

    except requests.exceptions.HTTPError as e:
        log.error("  → HTTP %s para %s", e.response.status_code, url)
    except requests.exceptions.ConnectionError:
        log.error("  → Sin conexión de red.")
    except requests.exceptions.Timeout:
        log.error("  → Timeout (%ds) para %s", _TIMEOUT, url)
    except requests.exceptions.RequestException as e:
        log.error("  → Error de red: %s", e)

    return None


def _localizar_itemlist(soup: BeautifulSoup) -> Optional[dict]:
    """
    Busca entre todos los bloques <script type="application/ld+json">
    el que contiene "@type": "ItemList".

    Elektra incluye múltiples bloques JSON-LD por página (WebSite,
    BreadcrumbList, ItemList, SearchAction, etc.). Se itera sobre todos
    sin asumir índice fijo para mayor robustez ante cambios de orden.

    Nota: "@type" puede ser un string o una lista de strings según la
    especificación JSON-LD, por lo que se normaliza a lista antes de comparar.

    Args:
        soup: HTML parseado de la página del catálogo.

    Returns:
        El dict del bloque ItemList, o None si no se encontró ninguno.
    """
    scripts = soup.find_all("script", {"type": "application/ld+json"})

    for i, script in enumerate(scripts):
        contenido = script.string
        if not contenido or not contenido.strip():
            continue

        try:
            data = json.loads(contenido)

            tipo_raw = data.get("@type", "")
            tipos    = [tipo_raw] if isinstance(tipo_raw, str) else (tipo_raw or [])

            if "ItemList" in tipos:
                n = len(data.get("itemListElement", []))
                log.info("  → ItemList hallado en bloque #%d (%d ítems).", i, n)
                return data

        except json.JSONDecodeError:
            log.debug("  → Bloque #%d no es JSON válido.", i)
            continue

    return None


def _extraer_precio(item: dict) -> Optional[float]:
    """
    Extrae el precio numérico del nodo 'offers' de un elemento Schema.org.

    Prioridad de campos:
        AggregateOffer.lowPrice → Offer.price → *.highPrice

    El valor puede llegar como int, float o str; se convierte
    explícitamente a float en todos los casos.

    Args:
        item: Dict del nodo 'item' dentro de un ListItem del ItemList.

    Returns:
        Precio como float > 0, o None si el nodo no es parseable.
    """
    offers = item.get("offers")
    if not offers or not isinstance(offers, dict):
        return None

    valor_raw = (
        offers.get("lowPrice") or
        offers.get("price")    or
        offers.get("highPrice")
    )
    if valor_raw is None:
        return None

    try:
        precio = float(valor_raw)
        return precio if precio > 0 else None
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# FUNCIÓN PÚBLICA — INTERFAZ CON EL ORQUESTADOR
# ─────────────────────────────────────────────────────────────────────────────

def extraer_de_elektra(
    pagina_inicio: int = 1,
    limite_paginas: int = 5,
) -> tuple[list[dict], bool]:
    """
    Extrae TODOS los smartphones disponibles en el rango de páginas
    especificado del catálogo de Elektra México.

    Itera desde `pagina_inicio` hasta `pagina_inicio + limite_paginas - 1`.
    Si en alguna iteración el bloque JSON-LD está ausente, vacío o devuelve
    HTTP 404, activa el freno de emergencia: rompe el ciclo y marca
    `fin_de_catalogo = True` para que el orquestador reinicie el contador.

    No aplica límite al número de productos por página: extrae todos los
    que el JSON-LD exponga en cada iteración.

    Args:
        pagina_inicio  : Primera página a descargar (default: 1).
        limite_paginas : Cantidad máxima de páginas a procesar (default: 5).

    Returns:
        Una tupla (productos, fin_de_catalogo):
            - productos       : list[dict] con claves 'nombre', 'precio', 'url'.
            - fin_de_catalogo : True si se detectó el final del catálogo
                                (ItemList vacío, ausente o HTTP 404).
                                False si el ciclo completó sin problemas.

    Ejemplo de uso en el orquestador (main.py):
        productos, fin = extraer_de_elektra(pagina_inicio=6, limite_paginas=5)
        if fin:
            PAGINA_ACTUAL_ELEKTRA = 1   # reiniciar al principio
        else:
            PAGINA_ACTUAL_ELEKTRA += 5  # avanzar al siguiente bloque
    """
    log.info("━" * 54)
    log.info(
        "Elektra — páginas %d a %d (bloque de %d)",
        pagina_inicio,
        pagina_inicio + limite_paginas - 1,
        limite_paginas,
    )

    sesion          = requests.Session()
    productos       : list[dict] = []
    fin_de_catalogo : bool       = False

    for pagina in range(pagina_inicio, pagina_inicio + limite_paginas):

        # Pausa cortés entre páginas para no saturar el servidor
        if pagina > pagina_inicio:
            pausa = random.uniform(_PAUSA_MIN, _PAUSA_MAX)
            log.info("  Pausa %.1f s antes de página %d...", pausa, pagina)
            time.sleep(pausa)

        url  = _construir_url(pagina)
        soup = _descargar_html(url, sesion)

        # ── Freno de emergencia: error de descarga ─────────────────────────
        if soup is None:
            log.warning(
                "  Página %d: descarga fallida o HTTP 404. "
                "Fin de catálogo declarado. BREAK.",
                pagina,
            )
            fin_de_catalogo = True
            break

        # ── Freno de emergencia: ItemList ausente ──────────────────────────
        item_list = _localizar_itemlist(soup)
        if item_list is None:
            log.warning(
                "  Página %d: sin bloque ItemList. "
                "Fin de catálogo declarado. BREAK.",
                pagina,
            )
            fin_de_catalogo = True
            break

        # ── Freno de emergencia: ItemList vacío ────────────────────────────
        elementos = item_list.get("itemListElement", [])
        if not elementos:
            log.warning(
                "  Página %d: ItemList presente pero vacío. "
                "Fin de catálogo declarado. BREAK.",
                pagina,
            )
            fin_de_catalogo = True
            break

        # ── Extracción de ítems ────────────────────────────────────────────
        validos_esta_pagina = 0

        for elemento in elementos:
            try:
                item = elemento.get("item", {})
                if not item:
                    continue

                # Nombre
                nombre = str(item.get("name", "")).strip()
                if not nombre:
                    continue

                # URL canónica (@id en Schema.org identifica al producto)
                url_producto = str(item.get("@id", "")).strip()
                if not url_producto or not url_producto.startswith("http"):
                    continue

                # Precio
                precio = _extraer_precio(item)
                if precio is None:
                    log.debug(
                        "    Ítem omitido: precio no disponible "
                        "(nombre='%s').", nombre[:40]
                    )
                    continue

                productos.append({
                    "nombre": nombre,
                    "precio": precio,
                    "url":    url_producto,
                })
                validos_esta_pagina += 1

            except Exception as e:
                # Tolerancia a fallos: un ítem malformado no detiene la extracción
                log.warning("    Ítem omitido por error inesperado: %s", e)
                continue

        log.info(
            "  Página %d: %d ítems válidos extraídos. "
            "Acumulado total: %d.",
            pagina, validos_esta_pagina, len(productos),
        )

    # ── Resumen del bloque ─────────────────────────────────────────────────
    log.info(
        "Elektra — extracción completada. "
        "Total: %d productos | fin_de_catalogo: %s",
        len(productos), fin_de_catalogo,
    )

    return (productos, fin_de_catalogo)


# ─────────────────────────────────────────────────────────────────────────────
# PUNTO DE ENTRADA (prueba directa del módulo)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json as _json
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=" * 60)
    print("  scrapers/elektra.py — Prueba directa")
    print("=" * 60 + "\n")

    productos, fin = extraer_de_elektra(pagina_inicio=1, limite_paginas=3)

    if productos:
        print(f"\nResultados ({len(productos)} productos | fin_de_catalogo={fin}):\n")
        for i, p in enumerate(productos, 1):
            print(f"  {i:3}. [MXN ${p['precio']:>10,.2f}] {p['nombre'][:52]}")
    else:
        print(f"\nSin productos. fin_de_catalogo={fin}")