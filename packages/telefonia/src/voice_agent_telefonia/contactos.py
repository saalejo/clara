"""La agenda: búsqueda por nombre, resolución por número y caché en disco.

Aquí **no hay D-Bus ni Bluetooth**. Descargar la agenda del móvil es trabajo de
`pbap.py`; esto es lo que se hace con ella una vez descargada. La separación es
lo que permite probar toda la lógica de búsqueda —que es la parte que puede
fallar de formas sutiles— sin un móvil delante.

## La regla del ganador claro

Buscar devuelve una lista puntuada, pero decidir si eso es "una respuesta" o
"una pregunta" no es cosa del que busca. La regla vive en `hay_ganador_claro` y
es deliberadamente estricta:

> el primero puntúa al menos 80 **y** le saca 15 puntos al segundo.

Es estricta porque el precio de los dos errores no es el mismo. Preguntar "¿Ana
Pérez o Ana Gómez?" cuando estaba claro cuesta una frase. Llamar a la Ana
equivocada cuesta una llamada a alguien que no esperaba tu voz.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from voice_agent_core.telefonia import Coincidencia, Contacto
from voice_agent_telefonia.normaliza import mismo_numero, puntuar

#: Puntuación mínima para considerar que un contacto se parece a lo buscado.
#: Por debajo, la lista se llenaría de gente que no tiene nada que ver y el
#: modelo acabaría preguntando "¿te refieres a...?" con cuatro desconocidos.
UMBRAL_PARECIDO = 45

#: Puntuación mínima del primero para poder actuar sin preguntar.
UMBRAL_GANADOR = 80

#: Ventaja mínima del primero sobre el segundo para que no haya duda.
VENTAJA_GANADOR = 15


class Agenda:
    """Los contactos del móvil, en memoria, con su copia en disco.

    La caché en disco no es una optimización: es lo que hace que el agente sepa
    quién llama en los primeros segundos tras un reinicio, antes de que dé
    tiempo a hablar con el móvil. Descargar la agenda por PBAP tarda entre unos
    segundos y varios minutos según lo grande que sea.
    """

    def __init__(self, ruta_cache: Path) -> None:
        """Prepara una agenda vacía respaldada por el fichero indicado.

        Args:
            ruta_cache: JSON donde se guarda la copia. No hace falta que exista.
        """
        self.ruta_cache = ruta_cache
        self.contactos: list[Contacto] = []
        self.actualizada: datetime | None = None

    # --- Caché ---------------------------------------------------------------

    def cargar_cache(self) -> bool:
        """Lee la copia de disco, si la hay.

        Un fichero corrupto no es un error fatal: se registra y se sigue con la
        agenda vacía, que se rellenará en la siguiente sincronización. Perder la
        caché solo cuesta que el agente no sepa nombres durante un rato.

        Returns:
            `True` si se cargó algo.
        """
        if not self.ruta_cache.exists():
            return False
        try:
            datos = json.loads(self.ruta_cache.read_text(encoding="utf-8"))
            self.contactos = [Contacto.model_validate(c) for c in datos["contactos"]]
            marca = datos.get("actualizada")
            self.actualizada = datetime.fromisoformat(marca) if marca else None
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.warning(f"La caché de la agenda ({self.ruta_cache}) no se pudo leer: {e}")
            self.contactos = []
            self.actualizada = None
            return False
        logger.info(f"Agenda cargada de la caché: {len(self.contactos)} contactos")
        return True

    def guardar_cache(self) -> None:
        """Escribe la copia en disco, de forma atómica."""
        self.ruta_cache.parent.mkdir(parents=True, exist_ok=True)
        datos = {
            "actualizada": (self.actualizada or datetime.now(UTC)).isoformat(),
            "contactos": [c.model_dump() for c in self.contactos],
        }
        # Escritura atómica: si el proceso muere a mitad, la caché anterior
        # sigue entera en vez de quedar un JSON truncado que ya no se puede leer.
        temporal = self.ruta_cache.with_suffix(".tmp")
        temporal.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        temporal.replace(self.ruta_cache)

    def reemplazar(self, contactos: list[Contacto]) -> None:
        """Sustituye la agenda entera y la guarda."""
        self.contactos = contactos
        self.actualizada = datetime.now(UTC)
        self.guardar_cache()

    def caducada(self, horas: int) -> bool:
        """Indica si toca volver a descargar la agenda del móvil."""
        if self.actualizada is None:
            return True
        transcurrido = datetime.now(UTC) - self.actualizada
        return transcurrido.total_seconds() >= horas * 3600

    # --- Consultas -----------------------------------------------------------

    def buscar(self, nombre: str, *, limite: int = 5) -> list[Coincidencia]:
        """Busca contactos parecidos a un nombre dicho en voz alta.

        Args:
            nombre: Lo que ha dicho la persona, tal cual se transcribió.
            limite: Cuántos devolver como mucho.

        Returns:
            Las coincidencias ordenadas de mejor a peor. Lista vacía si no hay
            nada que se parezca lo suficiente.
        """
        if not nombre.strip():
            return []

        puntuadas = [
            Coincidencia(contacto=c, puntuacion=p)
            for c in self.contactos
            if (p := puntuar(nombre, c.nombre)) >= UMBRAL_PARECIDO
        ]
        # Se desempata por nombre para que dos ejecuciones con los mismos datos
        # den siempre el mismo orden; si no, "¿Ana Pérez o Ana Gómez?" cambiaría
        # de orden entre llamadas y los tests serían inestables.
        puntuadas.sort(key=lambda c: (-c.puntuacion, c.contacto.nombre))
        return puntuadas[:limite]

    def por_numero(self, numero: str) -> Contacto | None:
        """Averigua a quién pertenece un número entrante.

        Es lo que convierte "te llama el 300 123 45 67" en "te llama Ana".
        """
        if not numero:
            return None
        for contacto in self.contactos:
            for suyo in contacto.numeros:
                if mismo_numero(numero, suyo.numero):
                    return contacto
        return None


def hay_ganador_claro(coincidencias: list[Coincidencia]) -> bool:
    """Decide si se puede actuar sobre la primera coincidencia sin preguntar.

    Ver el docstring del módulo para por qué el listón está donde está.
    """
    if not coincidencias:
        return False
    if coincidencias[0].puntuacion < UMBRAL_GANADOR:
        return False
    if len(coincidencias) == 1:
        return True
    return coincidencias[0].puntuacion - coincidencias[1].puntuacion >= VENTAJA_GANADOR


def redactar_pregunta(coincidencias: list[Coincidencia]) -> str:
    """Escribe la pregunta de desambiguación que el agente dirá en voz alta.

    Se redacta aquí, y no se deja al modelo, por dos motivos. Uno, para que
    distinga a la gente por su nombre completo y no por el número, que hablado
    es incomprensible. Y dos, porque un modelo al que se le pasa una lista
    tiende a leerla entera, con números incluidos.
    """
    nombres = [c.contacto.nombre for c in coincidencias]
    if len(nombres) == 1:
        return f"¿Te refieres a {nombres[0]}?"
    return "¿" + ", ".join(nombres[:-1]) + f" o {nombres[-1]}?"


__all__ = [
    "UMBRAL_GANADOR",
    "UMBRAL_PARECIDO",
    "VENTAJA_GANADOR",
    "Agenda",
    "hay_ganador_claro",
    "redactar_pregunta",
]
