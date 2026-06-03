/**
 * app.js — ByteBarato
 * Lógica completa del frontend: fetch a la API, renderizado de tarjetas,
 * búsqueda en tiempo real, botón Actualizar con escudo anti-spam y
 * gráfica de historial de precios con Chart.js.
 *
 * Arquitectura:
 *   - Sin frameworks: Vanilla JS puro (ES2022).
 *   - Módulo IIFE para encapsular el estado global y evitar
 *     contaminación del scope global.
 *   - Funciones pequeñas y de responsabilidad única.
 *
 * Endpoints consumidos (FastAPI en localhost:8000):
 *   GET  /api/productos
 *   GET  /api/productos/{id}/historial
 *   POST /api/actualizar
 */

(() => {
  'use strict';

  /* ────────────────────────────────────────────────────────────
     CONFIGURACIÓN
  ──────────────────────────────────────────────────────────── */

  const API_BASE = 'http://localhost:8000/api';

  /* ────────────────────────────────────────────────────────────
     REFERENCIAS AL DOM
  ──────────────────────────────────────────────────────────── */

  const $grid          = document.getElementById('grid-productos');
  const $inputBusqueda = document.getElementById('input-busqueda');
  const $btnActualizar = document.getElementById('btn-actualizar');
  const $btnTexto      = document.getElementById('btn-actualizar-texto');
  const $btnIcono      = document.getElementById('btn-actualizar-icono');
  const $contador      = document.getElementById('contador-productos');
  const $toasts        = document.getElementById('toast-contenedor');

  // Modal
  const $modalOverlay  = document.getElementById('modal-overlay');
  const $modalTitulo   = document.getElementById('modal-titulo');
  const $modalTienda   = document.getElementById('modal-tienda');
  const $btnCerrar     = document.getElementById('btn-cerrar-modal');
  const $statActual    = document.getElementById('stat-actual');
  const $statMin       = document.getElementById('stat-min');
  const $statMax       = document.getElementById('stat-max');
  const $statTotal     = document.getElementById('stat-total');
  const $canvas        = document.getElementById('historialChart');

  /* ────────────────────────────────────────────────────────────
     ESTADO LOCAL
  ──────────────────────────────────────────────────────────── */

  /** Todos los productos cargados desde la API */
  let productosCache = [];

  /** Instancia activa de Chart.js (se destruye antes de crear una nueva) */
  let chartInstancia = null;

  /* ────────────────────────────────────────────────────────────
     UTILIDADES DE FORMATO
  ──────────────────────────────────────────────────────────── */

  /**
   * Formatea un número como precio en MXN.
   * @param {number} monto
   * @returns {string} ej. "$7,999.00"
   */
  const formatearPrecio = (monto) =>
    new Intl.NumberFormat('es-MX', {
      style:                 'currency',
      currency:              'MXN',
      minimumFractionDigits: 2,
    }).format(monto);

  /**
   * Formatea un string de fecha ISO o de SQLite a formato legible.
   * @param {string} fechaStr  ej. "2026-05-20 14:30:00"
   * @returns {string}         ej. "20 may 2026"
   */
  const formatearFecha = (fechaStr) => {
    if (!fechaStr) return '—';
    // SQLite devuelve "YYYY-MM-DD HH:MM:SS" — reemplazar espacio por T
    const fecha = new Date(fechaStr.replace(' ', 'T'));
    if (isNaN(fecha)) return fechaStr;
    return fecha.toLocaleDateString('es-MX', {
      day:   'numeric',
      month: 'short',
      year:  'numeric',
    });
  };

  /* ────────────────────────────────────────────────────────────
     SISTEMA DE TOAST (notificaciones flotantes)
  ──────────────────────────────────────────────────────────── */

  /**
   * Muestra una notificación flotante que desaparece automáticamente.
   * @param {string} mensaje      Texto del toast.
   * @param {'exito'|'error'|'aviso'|'info'} tipo
   * @param {number} duracion     Milisegundos antes de cerrar (default 4000).
   */
  function mostrarToast(mensaje, tipo = 'info', duracion = 4000) {
    const iconos = { exito: '✅', error: '❌', aviso: '⚠️', info: 'ℹ️' };

    const $toast = document.createElement('div');
    $toast.className = `toast toast--${tipo}`;
    $toast.setAttribute('role', 'alert');
    $toast.innerHTML = `
      <span class="toast__icono" aria-hidden="true">${iconos[tipo] ?? 'ℹ️'}</span>
      <span class="toast__texto">${mensaje}</span>
    `;

    $toasts.appendChild($toast);

    // Animación de salida → eliminar del DOM
    const cerrar = () => {
      $toast.classList.add('toast--salir');
      $toast.addEventListener('animationend', () => $toast.remove(), { once: true });
    };

    const timer = setTimeout(cerrar, duracion);
    // Clic en el toast lo cierra inmediatamente
    $toast.addEventListener('click', () => { clearTimeout(timer); cerrar(); });
  }

  /* ────────────────────────────────────────────────────────────
     ESTADOS DE PANTALLA (carga / vacío / error)
  ──────────────────────────────────────────────────────────── */

  /** Muestra el spinner de carga dentro del grid. */
  function mostrarCargando() {
    $grid.innerHTML = `
      <div class="estado-pantalla">
        <div class="spinner" aria-label="Cargando..."></div>
        <p class="estado-pantalla__titulo">Cargando catálogo…</p>
        <p class="estado-pantalla__desc">
          Consultando la base de datos de ByteBarato.
        </p>
      </div>
    `;
    $contador.textContent = '';
  }

  /** Muestra un mensaje de catálogo vacío. */
  function mostrarVacio() {
    $grid.innerHTML = `
      <div class="estado-pantalla">
        <span class="estado-pantalla__icono">📦</span>
        <p class="estado-pantalla__titulo">Sin productos todavía</p>
        <p class="estado-pantalla__desc">
          Haz clic en <strong>Actualizar Catálogo</strong> para iniciar
          la extracción de precios.
        </p>
      </div>
    `;
    $contador.textContent = '';
  }

  /**
   * Muestra un mensaje de error dentro del grid.
   * @param {string} detalle  Descripción técnica del error.
   */
  function mostrarError(detalle) {
    $grid.innerHTML = `
      <div class="estado-pantalla">
        <span class="estado-pantalla__icono">⚠️</span>
        <p class="estado-pantalla__titulo">No se pudo cargar el catálogo</p>
        <p class="estado-pantalla__desc">${detalle}</p>
      </div>
    `;
    $contador.textContent = '';
  }

  /* ────────────────────────────────────────────────────────────
     RENDERIZADO DE TARJETAS
  ──────────────────────────────────────────────────────────── */

  /**
   * Construye el HTML de una tarjeta de producto.
   * @param {Object} p  Producto de la API: {id, nombre, tienda, url_origen, precio, capturado_en}
   * @returns {string}  HTML de la tarjeta lista para insertar en el DOM.
   */
  function crearTarjetaHTML(p) {
    const precio      = formatearPrecio(p.precio);
    const fecha       = formatearFecha(p.capturado_en);
    const nombreSafe  = p.nombre
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');

    return `
      <article class="tarjeta" role="listitem">
        <div class="tarjeta__meta">
          <span class="tarjeta__tienda">${p.tienda}</span>
          <span class="tarjeta__fecha">${fecha}</span>
        </div>

        <p class="tarjeta__nombre" title="${nombreSafe}">${nombreSafe}</p>

        <div>
          <p class="tarjeta__precio">${precio}</p>
          <p class="tarjeta__precio-label">Último precio registrado</p>
        </div>

        <div class="tarjeta__footer">
          <button
            class="btn btn--secundario tarjeta__btn-historial"
            data-id="${p.id}"
            data-nombre="${nombreSafe}"
            data-tienda="${p.tienda}"
            data-precio="${p.precio}"
            aria-label="Ver historial de precios de ${nombreSafe}"
          >
            📈 Ver Historial
          </button>

          <a
            href="${p.url_origen}"
            target="_blank"
            rel="noopener noreferrer"
            class="tarjeta__link"
            aria-label="Ver en tienda: ${nombreSafe}"
            title="Ver en tienda"
          >↗</a>
        </div>
      </article>
    `;
  }

  /**
   * Renderiza una lista de productos en el grid.
   * @param {Array} lista  Array de objetos producto.
   */
  function renderizarProductos(lista) {
    if (!lista || lista.length === 0) {
      mostrarVacio();
      return;
    }

    $grid.innerHTML = lista.map(crearTarjetaHTML).join('');
    $contador.textContent = `${lista.length} producto${lista.length !== 1 ? 's' : ''}`;

    // Delegar el evento click de los botones "Ver Historial"
    $grid.querySelectorAll('[data-id]').forEach(($btn) => {
      $btn.addEventListener('click', () => {
        abrirHistorial(
          Number($btn.dataset.id),
          $btn.dataset.nombre,
          $btn.dataset.tienda,
          Number($btn.dataset.precio),
        );
      });
    });
  }

  /* ────────────────────────────────────────────────────────────
     CARGAR PRODUCTOS (GET /api/productos)
  ──────────────────────────────────────────────────────────── */

  /**
   * Obtiene el catálogo completo desde la API y lo renderiza.
   * Actualiza `productosCache` para que el buscador pueda filtrar sin red.
   */
  async function cargarProductos() {
    mostrarCargando();

    try {
      const resp = await fetch(`${API_BASE}/productos`);

      if (resp.status === 404) {
        // La API devuelve 404 cuando la BD está vacía (aún no se ha extraído nada)
        productosCache = [];
        mostrarVacio();
        return;
      }

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail ?? `HTTP ${resp.status}`);
      }

      const productos = await resp.json();
      productosCache = productos;
      renderizarProductos(productos);

    } catch (err) {
      console.error('[ByteBarato] cargarProductos:', err);
      mostrarError(
        err.message.includes('Failed to fetch')
          ? 'No se pudo conectar con el servidor. ¿Está corriendo <code>python main.py</code>?'
          : err.message,
      );
    }
  }

  /* ────────────────────────────────────────────────────────────
     BUSCADOR EN TIEMPO REAL
  ──────────────────────────────────────────────────────────── */

  /**
   * Filtra `productosCache` localmente según el texto del buscador.
   * No realiza peticiones adicionales a la API.
   */
  function filtrarProductos() {
    const termino = $inputBusqueda.value.trim().toLowerCase();

    if (!termino) {
      renderizarProductos(productosCache);
      return;
    }

    const filtrados = productosCache.filter((p) =>
      p.nombre.toLowerCase().includes(termino) ||
      p.tienda.toLowerCase().includes(termino),
    );

    renderizarProductos(filtrados);

    if (filtrados.length === 0) {
      $grid.innerHTML = `
        <div class="estado-pantalla">
          <span class="estado-pantalla__icono">🔍</span>
          <p class="estado-pantalla__titulo">Sin resultados</p>
          <p class="estado-pantalla__desc">
            No se encontraron productos para "<strong>${termino}</strong>".
            Intenta con otro término.
          </p>
        </div>
      `;
      $contador.textContent = '0 productos';
    }
  }

  /* ────────────────────────────────────────────────────────────
     BOTÓN ACTUALIZAR (POST /api/actualizar)
  ──────────────────────────────────────────────────────────── */

  /** Activa el estado visual de "Cargando" en el botón. */
  function setBtnCargando(activo) {
    $btnActualizar.disabled = activo;
    $btnTexto.textContent   = activo ? 'Actualizando…' : 'Actualizar Catálogo';
    $btnIcono.style.animation = activo
      ? 'girar 0.75s linear infinite'
      : 'none';
    $btnIcono.textContent   = '↻';
  }

  /** Manejador del botón Actualizar Catálogo. */
  async function manejarActualizar() {
    setBtnCargando(true);

    try {
      const resp = await fetch(`${API_BASE}/actualizar`, { method: 'POST' });
      const data = await resp.json().catch(() => ({}));

      if (resp.status === 429) {
        // Escudo anti-spam activo
        const msg = data.detail ?? '';
        // Intentar extraer los segundos del mensaje de la API
        const match = msg.match(/(\d+)\s*segundo/i);
        const seg   = match ? Number(match[1]) : 300;
        const min   = Math.ceil(seg / 60);

        mostrarToast(
          `🛡️ Escudo anti-spam activo. Espera ${min} minuto${min !== 1 ? 's' : ''} antes de volver a actualizar.`,
          'aviso',
          6000,
        );
        return;
      }

      if (!resp.ok) {
        throw new Error(data.detail ?? `HTTP ${resp.status}`);
      }

      // Éxito
      const { nuevos = 0, actualizados = 0, paginas_procesadas = '' } = data;
      mostrarToast(
        `✅ Catálogo actualizado — ${nuevos} nuevos, ${actualizados} actualizados` +
        (paginas_procesadas ? ` (páginas ${paginas_procesadas})` : '') + '.',
        'exito',
        5000,
      );

      // Recargar el grid con los datos más recientes
      await cargarProductos();

    } catch (err) {
      console.error('[ByteBarato] manejarActualizar:', err);
      mostrarToast(
        err.message.includes('Failed to fetch')
          ? 'No se pudo conectar con el servidor.'
          : `Error al actualizar: ${err.message}`,
        'error',
        6000,
      );
    } finally {
      setBtnCargando(false);
    }
  }

  /* ────────────────────────────────────────────────────────────
     MODAL DE GRÁFICA (GET /api/productos/{id}/historial)
  ──────────────────────────────────────────────────────────── */

  /** Abre el modal y lo marca como accesible. */
  function abrirModal() {
    $modalOverlay.classList.add('abierto');
    $modalOverlay.setAttribute('aria-hidden', 'false');
    $btnCerrar.focus();
    document.body.style.overflow = 'hidden'; // bloquear scroll del fondo
  }

  /** Cierra el modal y restaura el scroll. */
  function cerrarModal() {
    $modalOverlay.classList.remove('abierto');
    $modalOverlay.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
  }

  /**
   * Destruye la instancia previa de Chart.js si existe.
   * Necesario para evitar el error "Canvas already in use".
   */
  function destruirChart() {
    if (chartInstancia) {
      chartInstancia.destroy();
      chartInstancia = null;
    }
  }

  /**
   * Instancia la gráfica de líneas con los datos del historial.
   * @param {Array<{precio: number, capturado_en: string}>} historial
   */
  function crearGrafica(historial) {
    destruirChart();

    const etiquetas = historial.map((h) => formatearFecha(h.capturado_en));
    const precios   = historial.map((h) => h.precio);

    // Detectar tendencia para colorear la línea
    const esBaja    = precios.length > 1 && precios.at(-1) < precios[0];
    const colorLinea = esBaja ? '#10B981' : '#2563EB';

    chartInstancia = new Chart($canvas, {
      type: 'line',
      data: {
        labels:   etiquetas,
        datasets: [{
          label:                'Precio (MXN)',
          data:                 precios,
          borderColor:          colorLinea,
          backgroundColor:      esBaja
                                  ? 'rgba(16, 185, 129, 0.08)'
                                  : 'rgba(37, 99, 235, 0.08)',
          borderWidth:          2.5,
          pointBackgroundColor: colorLinea,
          pointRadius:          historial.length <= 20 ? 5 : 3,
          pointHoverRadius:     7,
          tension:              0.35,       // curva suave
          fill:                 true,
        }],
      },
      options: {
        responsive:         true,
        maintainAspectRatio:false,
        interaction: {
          mode:      'index',
          intersect: false,
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => ` ${formatearPrecio(ctx.parsed.y)}`,
            },
            backgroundColor: '#0F172A',
            titleColor:      '#94A3B8',
            bodyColor:       '#F1F5F9',
            padding:         12,
            cornerRadius:    8,
          },
        },
        scales: {
          x: {
            grid:  { display: false },
            ticks: {
              color:     '#475569',
              font:      { size: 11 },
              maxRotation: 45,
              // Limitar etiquetas si hay muchos puntos
              maxTicksLimit: 10,
            },
          },
          y: {
            grid:  { color: '#F1F5F9' },
            ticks: {
              color:    '#475569',
              font:     { size: 11 },
              callback: (v) => formatearPrecio(v),
            },
          },
        },
      },
    });
  }

  /**
   * Carga el historial de un producto, rellena el modal y lo abre.
   * @param {number} id        ID del producto.
   * @param {string} nombre    Nombre del producto (para el encabezado).
   * @param {string} tienda    Tienda de origen.
   * @param {number} precioActual Precio más reciente (para las stats).
   */
  async function abrirHistorial(id, nombre, tienda, precioActual) {
    // Rellenar encabezado del modal con los datos ya disponibles
    $modalTitulo.textContent = nombre;
    $modalTienda.textContent = tienda;
    $statActual.textContent  = formatearPrecio(precioActual);
    $statMin.textContent     = '—';
    $statMax.textContent     = '—';
    $statTotal.textContent   = '—';

    // Limpiar gráfica anterior y mostrar el modal con spinner
    destruirChart();
    $canvas.style.display = 'none';
    $canvas.insertAdjacentHTML('beforebegin', '<div id="modal-spinner" class="estado-pantalla"><div class="spinner"></div></div>');
    abrirModal();

    try {
      const resp = await fetch(`${API_BASE}/productos/${id}/historial`);

      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.detail ?? `HTTP ${resp.status}`);
      }

      const { historial } = await resp.json();

      // Quitar spinner
      document.getElementById('modal-spinner')?.remove();
      $canvas.style.display = '';

      if (!historial || historial.length === 0) {
        $canvas.insertAdjacentHTML('beforebegin',
          '<div class="estado-pantalla"><p class="estado-pantalla__desc">Sin registros de precio todavía.</p></div>',
        );
        return;
      }

      // Calcular estadísticas rápidas
      const precios = historial.map((h) => h.precio);
      const pMin    = Math.min(...precios);
      const pMax    = Math.max(...precios);

      $statMin.textContent   = formatearPrecio(pMin);
      $statMax.textContent   = formatearPrecio(pMax);
      $statTotal.textContent = `${historial.length} registro${historial.length !== 1 ? 's' : ''}`;

      crearGrafica(historial);

    } catch (err) {
      console.error('[ByteBarato] abrirHistorial:', err);
      document.getElementById('modal-spinner')?.remove();
      $canvas.style.display = 'none';
      $canvas.insertAdjacentHTML('beforebegin', `
        <div class="estado-pantalla">
          <span class="estado-pantalla__icono">⚠️</span>
          <p class="estado-pantalla__desc">
            No se pudo cargar el historial: ${err.message}
          </p>
        </div>
      `);
    }
  }

  /* ────────────────────────────────────────────────────────────
     EVENTOS GLOBALES
  ──────────────────────────────────────────────────────────── */

  // Botón Actualizar
  $btnActualizar.addEventListener('click', manejarActualizar);

  // Búsqueda en tiempo real — debounce de 300 ms para no filtrar en cada tecla
  let timerBusqueda = null;
  $inputBusqueda.addEventListener('input', () => {
    clearTimeout(timerBusqueda);
    timerBusqueda = setTimeout(filtrarProductos, 300);
  });

  // Cerrar modal con el botón ✕
  $btnCerrar.addEventListener('click', cerrarModal);

  // Cerrar modal haciendo clic fuera del contenido (en el overlay oscuro)
  $modalOverlay.addEventListener('click', (e) => {
    if (e.target === $modalOverlay) cerrarModal();
  });

  // Cerrar modal con la tecla Escape
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $modalOverlay.classList.contains('abierto')) {
      cerrarModal();
    }
  });

  /* ────────────────────────────────────────────────────────────
     INICIALIZACIÓN
  ──────────────────────────────────────────────────────────── */

  // Cargar el catálogo al inicio
  cargarProductos();

})();
