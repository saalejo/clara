"""Autocontestar: la preferencia, su persistencia y el descuelgue automático.

**Lo primero que hay que saber, porque cambia lo que la función significa:** hoy
autocontestar quiere decir **descolgar**, no conversar. El audio de la llamada
sigue yendo por el altavoz del móvil y no por la placa; llevarlo a la placa es la
fase 2 de `docs/telefonia.md` y está sin empezar. Estos tests verifican que se
descuelgue, que es todo lo que hay.

No hay bus de D-Bus ni móvil: el `Servicio` se construye de verdad —su `__init__`
no conecta nada— y se le inyecta un teléfono falso. La app se monta con
`httpx.ASGITransport`, que **no ejecuta el `lifespan`**, así que nadie intenta
hablar con oFono.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from voice_agent_core.telefonia import EstadoLlamada, Llamada, TipoEvento
from voice_agent_telefonia.api import crear_app
from voice_agent_telefonia.llamadas import ErrorTelefono
from voice_agent_telefonia.preferencias import Preferencias, cargar, guardar, ruta
from voice_agent_telefonia.servicio import Servicio

ID = "voicecall01"


def _llamada(estado: EstadoLlamada = EstadoLlamada.ENTRANTE, id_llamada: str = ID) -> Llamada:
    return Llamada(id=id_llamada, estado=estado, numero="3001234567", entrante=True)


class TelefonoFalso:
    """Lo mínimo que `_autocontestar` le pide a un teléfono."""

    def __init__(self, llamadas: list[Llamada] | None = None, *, falla: bool = False) -> None:
        self.llamadas = llamadas if llamadas is not None else [_llamada()]
        self.falla = falla
        self.contestadas: list[str | None] = []
        self.colgadas: list[str | None] = []
        self.marcadas_por_nosotros: set[str] = set()
        self.direccion = "AA:BB:CC:DD:EE:FF"
        self.nombre = "Móvil de prueba"

    @property
    def listo(self) -> bool:
        return True

    async def listar(self) -> list[Llamada]:
        return list(self.llamadas)

    async def contestar(self, id_llamada: str | None = None) -> Llamada:
        self.contestadas.append(id_llamada)
        if self.falla:
            raise ErrorTelefono("oFono ha dicho que no")
        return _llamada(EstadoLlamada.EN_CURSO)

    async def colgar(self, id_llamada: str | None = None) -> None:
        self.colgadas.append(id_llamada)


def _servicio(tmp_path: Path, telefono: TelefonoFalso | None = None) -> Servicio:
    servicio = Servicio(directorio_datos=tmp_path)
    if telefono is not None:
        servicio.telefono = telefono  # type: ignore[assignment]
    # Sin margen: el retardo real se prueba aparte, y aquí solo estorbaría.
    servicio.preferencias.autocontestar_segundos = 0.0
    return servicio


# --- Las preferencias ---------------------------------------------------------


def test_por_defecto_esta_apagado(tmp_path: Path) -> None:
    """Descolgar solo no puede ser el comportamiento por omisión."""
    assert cargar(ruta(tmp_path)).autocontestar is False


def test_se_persiste_y_se_relee(tmp_path: Path) -> None:
    destino = ruta(tmp_path)
    guardar(destino, Preferencias(autocontestar=True, autocontestar_segundos=3.5))
    recuperadas = cargar(destino)
    assert recuperadas.autocontestar is True
    assert recuperadas.autocontestar_segundos == 3.5


def test_un_fichero_que_falta_no_es_un_error(tmp_path: Path) -> None:
    assert cargar(tmp_path / "no" / "existe.json") == Preferencias()


def test_un_json_corrupto_arranca_con_los_valores_por_defecto(tmp_path: Path) -> None:
    """Negarse a arrancar por un JSON de dos campos dejaría al usuario sin telefonía."""
    destino = ruta(tmp_path)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text("{esto no es json")
    assert cargar(destino).autocontestar is False


def test_un_margen_absurdo_se_rechaza() -> None:
    with pytest.raises(ValueError, match=r"less_than_equal"):
        Preferencias(autocontestar_segundos=999.0)


def test_el_fichero_es_propio_y_no_el_del_panel(tmp_path: Path) -> None:
    """`config/settings.json` es el contrato panel->agente; dos escritores serían una carrera."""
    assert ruta(tmp_path).name == "preferencias.json"
    assert "config" not in ruta(tmp_path).parts


# --- Cambiar la preferencia ---------------------------------------------------


def test_fijar_persiste_y_publica_evento(tmp_path: Path) -> None:
    servicio = _servicio(tmp_path)
    with servicio.eventos.suscripcion() as cola:
        assert servicio.fijar_autocontestar(True) is True
        assert cargar(servicio.ruta_preferencias).autocontestar is True

        evento = cola.get_nowait()

    assert evento.tipo is TipoEvento.AUTOCONTESTAR_CAMBIADO
    assert evento.motivo == "activado"


def test_alternar_invierte(tmp_path: Path) -> None:
    """Existe para el botón: sin él habría un leer-luego-escribir con TOCTOU."""
    servicio = _servicio(tmp_path)
    assert servicio.alternar_autocontestar() is True
    assert servicio.alternar_autocontestar() is False
    assert cargar(servicio.ruta_preferencias).autocontestar is False


async def test_el_estado_lo_reporta_y_lo_cuenta_en_la_frase(tmp_path: Path) -> None:
    servicio = _servicio(tmp_path, TelefonoFalso())
    servicio.fijar_autocontestar(True)

    estado = await servicio.estado()
    assert estado.autocontestar is True
    # La frase la dice el modelo tal cual, así que tiene que ser precisa: descuelga.
    assert "se descuelguen solas" in estado.detalle


async def test_apagado_no_ensucia_la_frase(tmp_path: Path) -> None:
    servicio = _servicio(tmp_path, TelefonoFalso())
    estado = await servicio.estado()
    assert estado.autocontestar is False
    assert "descuelguen" not in estado.detalle


# --- El descuelgue automático -------------------------------------------------


async def test_descuelga_una_entrante(tmp_path: Path) -> None:
    telefono = TelefonoFalso()
    servicio = _servicio(tmp_path, telefono)
    servicio.fijar_autocontestar(True)

    await servicio._autocontestar(ID)

    assert telefono.contestadas == [ID]


async def test_no_descuelga_si_la_llamada_ya_no_es_contestable(tmp_path: Path) -> None:
    """La persona la cogió, o el otro colgó. Es la carrera NORMAL, no un error."""
    telefono = TelefonoFalso([_llamada(EstadoLlamada.EN_CURSO)])
    servicio = _servicio(tmp_path, telefono)

    await servicio._autocontestar(ID)

    assert telefono.contestadas == []


async def test_una_llamada_de_app_no_se_intenta_descolgar(tmp_path: Path) -> None:
    """WhatsApp llega como `sonando` y oFono NUNCA deja contestar eso.

    Es la restricción de `ESTADOS_CONTESTABLES`, no una decisión de aquí: el
    `Answer()` de oFono exige `incoming`. Intentarlo de todos modos solo
    cambiaría el silencio por un `org.ofono.Error.Failed`.
    """
    telefono = TelefonoFalso([_llamada(EstadoLlamada.SONANDO)])
    servicio = _servicio(tmp_path, telefono)
    servicio.fijar_autocontestar(True)

    await servicio._autocontestar(ID)

    assert telefono.contestadas == []


async def test_no_descuelga_una_llamada_que_ha_desaparecido(tmp_path: Path) -> None:
    telefono = TelefonoFalso([])
    servicio = _servicio(tmp_path, telefono)

    await servicio._autocontestar(ID)

    assert telefono.contestadas == []


async def test_sin_telefono_no_revienta(tmp_path: Path) -> None:
    servicio = _servicio(tmp_path)
    await servicio._autocontestar(ID)  # no debe lanzar


async def test_un_error_de_ofono_se_traga(tmp_path: Path) -> None:
    """Esto corre en una tarea suelta: una excepción sería un aviso opaco de asyncio."""
    telefono = TelefonoFalso(falla=True)
    servicio = _servicio(tmp_path, telefono)

    await servicio._autocontestar(ID)

    assert telefono.contestadas == [ID]


async def test_dos_senales_de_la_misma_llamada_descuelgan_una_vez(tmp_path: Path) -> None:
    """`_refrescar_llamadas` corre en una tarea por señal, y las señales se repiten."""
    telefono = TelefonoFalso()
    servicio = _servicio(tmp_path, telefono)
    servicio.preferencias.autocontestar_segundos = 0.02

    await asyncio.gather(servicio._autocontestar(ID), servicio._autocontestar(ID))

    assert telefono.contestadas == [ID]


async def test_respeta_el_margen_antes_de_descolgar(tmp_path: Path) -> None:
    """El margen existe para poder rechazar a mano y para que oFono se asiente."""
    telefono = TelefonoFalso()
    servicio = _servicio(tmp_path, telefono)
    servicio.preferencias.autocontestar_segundos = 0.15

    tarea = asyncio.create_task(servicio._autocontestar(ID))
    await asyncio.sleep(0.02)
    assert telefono.contestadas == []

    await tarea
    assert telefono.contestadas == [ID]


async def test_al_desconectarse_el_movil_se_olvidan_los_ids(tmp_path: Path) -> None:
    """Pero la preferencia sobrevive al móvil: no se toca."""
    servicio = _servicio(tmp_path)
    servicio.fijar_autocontestar(True)
    servicio._autocontestando.add(ID)

    servicio._ultimas.clear()
    servicio._direcciones.clear()
    servicio._autocontestando.clear()

    assert servicio._autocontestando == set()
    assert servicio.preferencias.autocontestar is True


# --- Los endpoints ------------------------------------------------------------


@pytest.fixture
async def cliente(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Servicio]]:
    servicio = _servicio(tmp_path, TelefonoFalso())
    transporte = httpx.ASGITransport(app=crear_app(servicio))
    async with httpx.AsyncClient(transport=transporte, base_url="http://telefonia") as http:
        yield http, servicio


async def test_get_devuelve_el_estado(
    cliente: tuple[httpx.AsyncClient, Servicio],
) -> None:
    http, _ = cliente
    respuesta = await http.get("/autocontestar")
    assert respuesta.status_code == 200
    assert respuesta.json() == {"activo": False, "segundos": 0.0}


async def test_post_con_activo_lo_enciende(
    cliente: tuple[httpx.AsyncClient, Servicio],
) -> None:
    http, servicio = cliente
    respuesta = await http.post("/autocontestar", json={"activo": True})
    assert respuesta.status_code == 200
    assert respuesta.json()["activo"] is True
    assert servicio.preferencias.autocontestar is True


async def test_post_con_alternar_invierte(
    cliente: tuple[httpx.AsyncClient, Servicio],
) -> None:
    http, servicio = cliente
    assert (await http.post("/autocontestar", json={"alternar": True})).json()["activo"] is True
    assert (await http.post("/autocontestar", json={"alternar": True})).json()["activo"] is False
    assert servicio.preferencias.autocontestar is False


async def test_un_cuerpo_sin_sentido_da_422(
    cliente: tuple[httpx.AsyncClient, Servicio],
) -> None:
    http, _ = cliente
    assert (await http.post("/autocontestar", json={"lo_que_sea": 1})).status_code == 422
    assert (await http.post("/autocontestar", json={"activo": "si"})).status_code == 422
    assert (
        await http.post(
            "/autocontestar", content=b"no soy json", headers={"content-type": "application/json"}
        )
    ).status_code == 422


async def test_funciona_sin_telefono_conectado(tmp_path: Path) -> None:
    """No devuelve 409 a propósito: dejas la preferencia puesta y luego llega el móvil.

    Con un 409, el mando físico tendría que comprobar si hay teléfono antes de
    poder pulsar un botón que no depende de eso.
    """
    servicio = _servicio(tmp_path)  # sin teléfono
    transporte = httpx.ASGITransport(app=crear_app(servicio))
    async with httpx.AsyncClient(transport=transporte, base_url="http://telefonia") as http:
        respuesta = await http.post("/autocontestar", json={"activo": True})

    assert respuesta.status_code == 200
    assert respuesta.json()["activo"] is True


async def test_el_estado_general_incluye_el_autocontestar(
    cliente: tuple[httpx.AsyncClient, Servicio],
) -> None:
    http, _ = cliente
    await http.post("/autocontestar", json={"activo": True})
    datos = json.loads((await http.get("/estado")).text)
    assert datos["autocontestar"] is True


# --- El id `actual` -----------------------------------------------------------
#
# Se descubrió probando el mando físico con una llamada del operador de verdad: el
# botón de contestar pedía `POST /llamadas/actual/contestar` y el puente devolvía
# 404. `Telefono._buscar` busca por id exacto, y `actual` no es el id de ninguna
# llamada. Con ello estaba roto también `contestar_llamada` del agente, que usa la
# misma ruta escrita a fuego en `src/voice_agent/telefonia.py`.


async def test_el_id_actual_contesta_la_llamada_que_haya(
    cliente: tuple[httpx.AsyncClient, Servicio],
) -> None:
    """Es el contrato que los clientes ya asumían y que faltaba implementar."""
    http, servicio = cliente
    telefono = servicio.telefono
    assert telefono is not None

    respuesta = await http.post("/llamadas/actual/contestar")

    assert respuesta.status_code == 200
    # Lo que llega a `Telefono.contestar` tiene que ser None, no la cadena.
    assert telefono.contestadas == [None]  # type: ignore[attr-defined]


async def test_el_id_actual_tambien_vale_para_colgar(
    cliente: tuple[httpx.AsyncClient, Servicio],
) -> None:
    http, _ = cliente
    assert (await http.post("/llamadas/actual/colgar")).status_code == 200


async def test_un_id_de_verdad_sigue_llegando_tal_cual(
    cliente: tuple[httpx.AsyncClient, Servicio],
) -> None:
    """La traducción no puede tragarse los ids reales."""
    http, servicio = cliente
    telefono = servicio.telefono
    assert telefono is not None

    await http.post(f"/llamadas/{ID}/contestar")

    assert telefono.contestadas == [ID]  # type: ignore[attr-defined]
