"""Qué publica el puente cuando una llamada aparece ya en curso.

Pasa cuando el puente arranca o se reengancha con una conversación empezada:
`LLAMADA_CONTESTADA` se publicaba solo al detectar una *transición* a `EN_CURSO`,
y una llamada que nace en curso no viene de ningún estado anterior, así que el
evento no salía nunca.

**Cuidado con extrapolar esto a las llamadas de app.** Su `EN_CURSO` es mentira:
el móvil lo declara a los ~140 ms mientras la aplicación sigue timbrando y nadie
la ha cogido —medido con dos llamadas que se dejaron sonar—. El puente publica el
evento igual, porque no puede saberlo, y el que reacciona es quien tiene que
comprobarlo: el agente lo hace en `_saludar_en_llamada`. Ver `Llamada.es_de_app`.

No hay bus de D-Bus ni móvil: se le inyecta al `Servicio` un teléfono falso y se
lee lo que publica en su bus de eventos.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from voice_agent_core.telefonia import EstadoLlamada, EventoTelefonia, Llamada, TipoEvento
from voice_agent_telefonia.servicio import Servicio

ID = "voicecall01"

#: El relleno que manda Android en vez del número cuando la llamada es de una
#: app. Ver `RELLENOS_SIN_IDENTIFICAR`.
NUMERO_DE_APP = "10000000"


class TelefonoFalso:
    """Lo mínimo que `_refrescar_llamadas` le pide a un teléfono."""

    def __init__(self, llamadas: list[Llamada]) -> None:
        self.llamadas = llamadas
        self.marcadas_por_nosotros: set[str] = set()
        self.direccion = "AA:BB:CC:DD:EE:FF"
        self.nombre = "Móvil de prueba"

    @property
    def listo(self) -> bool:
        return True

    async def listar(self) -> list[Llamada]:
        # Copias, no las mismas instancias. El teléfono de verdad construye una
        # `Llamada` nueva en cada `GetCalls`, y el puente se guarda la anterior
        # para detectar transiciones: si el falso devolviera el mismo objeto,
        # cambiarle el estado en el test cambiaría también el "anterior" y
        # ninguna transición se vería jamás.
        return [llamada.model_copy() for llamada in self.llamadas]


def _llamada_de_app(estado: EstadoLlamada = EstadoLlamada.EN_CURSO) -> Llamada:
    return Llamada(id=ID, estado=estado, numero=NUMERO_DE_APP, entrante=True)


def _servicio(tmp_path: Path, llamadas: list[Llamada]) -> Servicio:
    servicio = Servicio(directorio_datos=tmp_path)
    servicio.telefono = TelefonoFalso(llamadas)  # type: ignore[assignment]
    return servicio


async def _recoger(servicio: Servicio) -> list[EventoTelefonia]:
    """Refresca una vez y devuelve lo que se haya publicado."""
    with servicio.eventos.suscripcion() as cola:
        await servicio._refrescar_llamadas("org.ofono.VoiceCallManager", "/")
        recogidos: list[EventoTelefonia] = []
        while not cola.empty():
            recogidos.append(cola.get_nowait())
        return recogidos


async def test_una_llamada_que_nace_en_curso_tambien_se_da_por_contestada(
    tmp_path: Path,
) -> None:
    """El arreglo: sin esto el agente anuncia la llamada y luego no saluda a nadie."""
    servicio = _servicio(tmp_path, [_llamada_de_app()])

    tipos = [evento.tipo for evento in await _recoger(servicio)]

    assert tipos == [TipoEvento.LLAMADA_ENTRANTE, TipoEvento.LLAMADA_CONTESTADA]


async def test_el_anuncio_va_antes_que_el_saludo(tmp_path: Path) -> None:
    """El orden importa: primero se avisa a la habitación, luego se saluda a la línea."""
    servicio = _servicio(tmp_path, [_llamada_de_app()])

    eventos = await _recoger(servicio)

    assert eventos[0].tipo is TipoEvento.LLAMADA_ENTRANTE
    assert eventos[1].tipo is TipoEvento.LLAMADA_CONTESTADA


async def test_no_se_repite_el_contestada_en_el_siguiente_refresco(tmp_path: Path) -> None:
    """`_refrescar_llamadas` corre una vez por señal y las señales se repiten."""
    servicio = _servicio(tmp_path, [_llamada_de_app()])

    await _recoger(servicio)
    segundos = await _recoger(servicio)

    assert segundos == []


async def test_una_llamada_normal_sigue_avisando_solo_de_la_entrada(tmp_path: Path) -> None:
    """Una del operador nace en `ENTRANTE`: ahí sí hay algo que descolgar."""
    llamada = Llamada(id=ID, estado=EstadoLlamada.ENTRANTE, numero="3001234567", entrante=True)
    servicio = _servicio(tmp_path, [llamada])

    tipos = [evento.tipo for evento in await _recoger(servicio)]

    assert tipos == [TipoEvento.LLAMADA_ENTRANTE]


async def test_la_transicion_a_en_curso_sigue_publicando_contestada(tmp_path: Path) -> None:
    """El camino de siempre —suena, alguien descuelga— no puede haberse roto."""
    llamada = Llamada(id=ID, estado=EstadoLlamada.ENTRANTE, numero="3001234567", entrante=True)
    servicio = _servicio(tmp_path, [llamada])

    await _recoger(servicio)
    llamada.estado = EstadoLlamada.EN_CURSO
    tipos = [evento.tipo for evento in await _recoger(servicio)]

    assert tipos == [TipoEvento.LLAMADA_CONTESTADA]


async def test_una_llamada_de_app_no_dispara_el_autocontestar(tmp_path: Path) -> None:
    """Ya está descolgada: programar un descuelgue sería una tarea para nada."""
    servicio = _servicio(tmp_path, [_llamada_de_app()])
    servicio.fijar_autocontestar(True)

    antes = len(asyncio.all_tasks())
    await _recoger(servicio)

    assert len(asyncio.all_tasks()) == antes
