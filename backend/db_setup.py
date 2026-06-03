import sqlite3
import os
import sys

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────

DB_FILE = "bytebarato.db"

# ─────────────────────────────────────────────
# DDL — DEFINICIÓN DE TABLAS
# ─────────────────────────────────────────────

SQL_CREAR_TABLA_PRODUCTOS = """
CREATE TABLE IF NOT EXISTS productos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre          TEXT    NOT NULL,
    url_origen      TEXT    NOT NULL UNIQUE,
    tienda          TEXT    NOT NULL,
    fecha_registro  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""
# • id            → clave primaria autoincremental.
# • nombre        → nombre completo del smartphone tal como aparece en la tienda.
# • url_origen    → URL canónica del producto; UNIQUE evita duplicados en la tabla.
# • tienda        → nombre de la plataforma de e-commerce (ej. "Mercado Libre").
# • fecha_registro→ timestamp de la primera vez que el bot rastreó este producto.

SQL_CREAR_TABLA_HISTORIAL = """
CREATE TABLE IF NOT EXISTS historial_precios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    producto_id INTEGER NOT NULL,
    precio      REAL    NOT NULL CHECK (precio > 0),
    capturado_en TEXT   NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (producto_id)
        REFERENCES productos (id)
        ON DELETE CASCADE
);
"""
# • id            → clave primaria autoincremental.
# • producto_id   → llave foránea que vincula el precio a su producto.
#                   ON DELETE CASCADE elimina el historial si se borra el producto.
# • precio        → REAL (punto flotante de 64 bits) para valores monetarios en MXN.
#                   CHECK garantiza que ningún precio cero o negativo sea insertado.
# • capturado_en  → timestamp exacto de la captura; columna crítica para el
#                   modelo de caché híbrido (evalúa antigüedad del dato).

# ─────────────────────────────────────────────
# DDL — ÍNDICES DE BÚSQUEDA
# ─────────────────────────────────────────────

SQL_INDICES = [
    # Acelera las JOINs y filtros por producto en historial_precios.
    # Es la consulta más frecuente del sistema (recuperar historial de un producto).
    """
    CREATE INDEX IF NOT EXISTS idx_historial_producto_id
    ON historial_precios (producto_id);
    """,

    # Acelera las consultas de caché: "¿cuándo fue la última captura de este producto?"
    # El sistema evalúa este timestamp para decidir si activa el bot en caliente.
    """
    CREATE INDEX IF NOT EXISTS idx_historial_capturado_en
    ON historial_precios (capturado_en DESC);
    """,

    # Acelera búsquedas de productos por tienda (ej. filtrar solo Mercado Libre).
    """
    CREATE INDEX IF NOT EXISTS idx_productos_tienda
    ON productos (tienda);
    """,
]

# ─────────────────────────────────────────────
# FUNCIÓN PRINCIPAL
# ─────────────────────────────────────────────

def inicializar_base_de_datos(ruta: str = DB_FILE) -> None:
    """
    Crea (o valida) el archivo de base de datos SQLite con el esquema
    completo de ByteBarato: tablas, restricciones e índices.

    Args:
        ruta: Ruta del archivo .db a crear o reutilizar.
    """
    es_nueva = not os.path.exists(ruta)

    try:
        # Abre (o crea) el archivo de base de datos.
        conexion = sqlite3.connect(ruta)
        cursor = conexion.cursor()

        print(f"  Base de datos: '{ruta}'")
        print(f"  Estado       : {'creada por primera vez' if es_nueva else 'ya existente, validando esquema'}\n")

        # Habilita el soporte de claves foráneas (desactivado por defecto en SQLite).
        cursor.execute("PRAGMA foreign_keys = ON;")

        # ── Crear tablas ──────────────────────────────
        print("  [1/3] Creando tablas...")
        cursor.execute(SQL_CREAR_TABLA_PRODUCTOS)
        print("        ✓ productos")
        cursor.execute(SQL_CREAR_TABLA_HISTORIAL)
        print("        ✓ historial_precios")

        # ── Crear índices ─────────────────────────────
        print("\n  [2/3] Creando índices de búsqueda...")
        nombres_indices = [
            "idx_historial_producto_id",
            "idx_historial_capturado_en",
            "idx_productos_tienda",
        ]
        for sql, nombre in zip(SQL_INDICES, nombres_indices):
            cursor.execute(sql)
            print(f"        ✓ {nombre}")

        # ── Confirmar transacción ─────────────────────
        conexion.commit()
        print("\n  [3/3] Transacción confirmada (COMMIT).")

    except sqlite3.OperationalError as e:
        print(f"\n  [ERROR] Error operacional de SQLite: {e}", file=sys.stderr)
        sys.exit(1)

    except sqlite3.DatabaseError as e:
        print(f"\n  [ERROR] Error de base de datos: {e}", file=sys.stderr)
        sys.exit(1)

    except OSError as e:
        print(f"\n  [ERROR] No se pudo acceder al sistema de archivos: {e}", file=sys.stderr)
        sys.exit(1)

    finally:
        # Cierra la conexión siempre, haya error o no.
        if "conexion" in locals():
            conexion.close()

# ─────────────────────────────────────────────
# VERIFICACIÓN POST-CREACIÓN
# ─────────────────────────────────────────────

def verificar_esquema(ruta: str = DB_FILE) -> None:
    """
    Consulta el catálogo interno de SQLite para confirmar que las tablas
    e índices fueron creados correctamente y los imprime en consola.

    Args:
        ruta: Ruta del archivo .db a verificar.
    """
    try:
        conexion = sqlite3.connect(ruta)
        cursor = conexion.cursor()

        # sqlite_master es el catálogo interno de objetos de la base de datos.
        cursor.execute("""
            SELECT type, name
            FROM sqlite_master
            WHERE type IN ('table', 'index')
              AND name NOT LIKE 'sqlite_%'
            ORDER BY type DESC, name ASC;
        """)
        objetos = cursor.fetchall()

        print("\n  Objetos registrados en el esquema:")
        for tipo, nombre in objetos:
            icono = "📋" if tipo == "table" else "🔍"
            print(f"        {icono} [{tipo.upper()}] {nombre}")

    except sqlite3.DatabaseError as e:
        print(f"\n  [ERROR] No se pudo verificar el esquema: {e}", file=sys.stderr)

    finally:
        if "conexion" in locals():
            conexion.close()

# ─────────────────────────────────────────────
# PUNTO DE ENTRADA
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 52)
    print("  ByteBarato — Inicialización de Base de Datos")
    print("=" * 52 + "\n")

    inicializar_base_de_datos(DB_FILE)
    verificar_esquema(DB_FILE)

    print("\n" + "=" * 52)
    print("  Inicialización completada exitosamente.")
    print("=" * 52)