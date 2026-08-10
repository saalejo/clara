"""Seguimiento del log del agente desde el panel.

El panel corre en otro contenedor, así que no puede leer el journal ni usar
`podman logs` —Quadlet añade `--rm` y el contenedor se destruye al parar, justo
cuando querrías saber por qué se cayó—. Por eso el agente escribe además a
`<DATA_DIR>/logs/agente.log`, en el volumen que ambos montan, y aquí se sigue
ese fichero.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

from django.conf import settings

#: Cuántas líneas se enseñan al abrir la página.
LINEAS_INICIALES = 200

#: Cada cuánto se mira si hay líneas nuevas. Medio segundo es imperceptible
#: leyendo y no calienta la CPU de la placa.
INTERVALO = 0.5

#: Tope de vida de una conexión. Sin esto, una pestaña olvidada mantendría el
#: hilo vivo indefinidamente; el navegador reconecta solo con SSE.
SEGUNDOS_MAXIMOS = 900


def ultimas_lineas(ruta: Path | None = None, cuantas: int = LINEAS_INICIALES) -> list[str]:
    """Devuelve el final del log, sin cargar el fichero entero en memoria."""
    ruta = ruta or settings.RUTA_LOG_AGENTE
    if not ruta.is_file():
        return []
    with ruta.open("r", encoding="utf-8", errors="replace") as fichero:
        # Los ficheros rotan a los 5 MB, así que leerlo entero y quedarse con la
        # cola es aceptable y mucho más simple que ir hacia atrás por bloques.
        return fichero.read().splitlines()[-cuantas:]


def eventos(ruta: Path | None = None) -> Iterator[str]:
    """Genera eventos SSE con las líneas que van apareciendo.

    Detecta la rotación del fichero: si el inodo cambia o encoge, vuelve a
    abrirlo desde el principio. Sin eso, el seguimiento se queda mudo para
    siempre en cuanto loguru rota los 5 MB.
    """
    ruta = ruta or settings.RUTA_LOG_AGENTE
    limite = time.monotonic() + SEGUNDOS_MAXIMOS
    fichero = None
    inodo = None

    try:
        while time.monotonic() < limite:
            if fichero is None:
                if not ruta.is_file():
                    yield ": esperando al fichero de log\n\n"
                    time.sleep(INTERVALO * 4)
                    continue
                fichero = ruta.open("r", encoding="utf-8", errors="replace")
                fichero.seek(0, 2)  # al final: solo interesa lo nuevo
                inodo = ruta.stat().st_ino

            linea = fichero.readline()
            if linea:
                yield f"data: {linea.rstrip()}\n\n"
                continue

            # Sin líneas nuevas: comprobar si el fichero ha rotado.
            try:
                actual = ruta.stat()
            except OSError:
                actual = None
            if actual is None or actual.st_ino != inodo or actual.st_size < fichero.tell():
                fichero.close()
                fichero = None
                continue

            # Un comentario SSE de vez en cuando mantiene viva la conexión.
            yield ": latido\n\n"
            time.sleep(INTERVALO)
    finally:
        if fichero is not None:
            fichero.close()
