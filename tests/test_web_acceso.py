"""La puerta de la interfaz de llamada.

Casi todo se prueba envolviendo una aplicación de juguete y no `web.py`: la
puerta es un middleware ASGI puro justamente para eso, y así estos casos corren
en milisegundos sin cargar pipecat ni chromadb. Los dos últimos sí montan la
aplicación de verdad, para comprobar que la puerta está donde tiene que estar.

`httpx.ASGITransport` no ejecuta el lifespan, así que `crear_app` no llega a
construir Piper ni a abrir Chroma: la puerta corta antes del router.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from voice_agent.acceso import (
    NOMBRE_GALLETA,
    PuertaDeAcceso,
    codigo_correcto,
    firmar,
    galleta_valida,
)
from voice_agent.web import crear_app
from voice_agent_core.config import Settings
from voice_agent_core.limitador import LimitadorDeIntentos

CODIGO = "postop-7fqk3d"


# --- La firma de la galleta ---------------------------------------------------


def test_una_galleta_recien_firmada_vale() -> None:
    assert galleta_valida(CODIGO, firmar(CODIGO, int(time.time()) + 60))


def test_una_galleta_caducada_no_vale() -> None:
    assert not galleta_valida(CODIGO, firmar(CODIGO, int(time.time()) - 1))


def test_rotar_el_codigo_invalida_las_galletas_emitidas() -> None:
    # Es la propiedad de diseño que justifica derivar la clave del código: si
    # el enlace se filtra, cambiar el código en el `.env` echa a todo el mundo.
    galleta = firmar(CODIGO, int(time.time()) + 3600)
    assert galleta_valida(CODIGO, galleta)
    assert not galleta_valida("otro-codigo", galleta)


def test_no_se_puede_estirar_la_caducidad() -> None:
    # La fecha viaja en claro, pero está dentro del cuerpo firmado.
    galleta = firmar(CODIGO, int(time.time()) + 60)
    version, expira, firma = galleta.split(".")
    manipulada = f"{version}.{int(expira) + 100_000}.{firma}"
    assert not galleta_valida(CODIGO, manipulada)


@pytest.mark.parametrize(
    "valor",
    ["", "basura", "c1.123", "c1.123.abc.def", "c9.123.abc", "c1.nofecha.abc", None],
)
def test_las_galletas_mal_formadas_se_rechazan_sin_reventar(valor: str | None) -> None:
    assert not galleta_valida(CODIGO, valor)


def test_el_codigo_vacio_nunca_acierta() -> None:
    assert not codigo_correcto(CODIGO, "")
    assert not codigo_correcto(CODIGO, "x" * 500)
    assert codigo_correcto(CODIGO, CODIGO)


# --- La puerta sobre una aplicación de juguete --------------------------------


class AppDeJuguete:
    """Responde 200 y apunta lo que le llega. Si la puerta cierra, no se toca."""

    def __init__(self) -> None:
        self.visitas: list[str] = []
        self.cuerpos: list[bytes] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.visitas.append(scope["path"])
        cuerpo = b""
        while scope["method"] in ("POST", "PUT"):
            mensaje = await receive()
            cuerpo += mensaje.get("body", b"")
            if not mensaje.get("more_body"):
                break
        self.cuerpos.append(cuerpo)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"pase"})


@pytest.fixture
def juguete() -> AppDeJuguete:
    return AppDeJuguete()


@pytest.fixture
def eventos() -> list[tuple[str, str]]:
    return []


@pytest.fixture
async def cliente(
    juguete: AppDeJuguete, eventos: list[tuple[str, str]]
) -> AsyncIterator[httpx.AsyncClient]:
    puerta = PuertaDeAcceso(
        juguete,
        codigo=CODIGO,
        duracion_secs=3600,
        limitador=LimitadorDeIntentos(max_intentos=3, bloqueo_secs=900.0),
        al_evento=lambda tipo, ip: eventos.append((tipo, ip)),
    )
    transporte = httpx.ASGITransport(app=puerta)
    async with httpx.AsyncClient(
        transport=transporte, base_url="http://clara", headers={"accept": "text/html"}
    ) as http:
        yield http


async def test_sin_galleta_se_ve_la_portada(
    cliente: httpx.AsyncClient, juguete: AppDeJuguete
) -> None:
    respuesta = await cliente.get("/")
    assert respuesta.status_code == 401
    assert "Código de acceso" in respuesta.text
    assert juguete.visitas == []


async def test_la_sonda_de_salud_sigue_publica(
    cliente: httpx.AsyncClient, juguete: AppDeJuguete
) -> None:
    # Cerrarla haría que cloudflared diera el origen por caído.
    respuesta = await cliente.get("/salud")
    assert respuesta.status_code == 200
    assert juguete.visitas == ["/salud"]


async def test_el_enlace_con_codigo_canjea_y_borra_el_codigo_de_la_url(
    cliente: httpx.AsyncClient,
) -> None:
    respuesta = await cliente.get("/", params={"c": CODIGO})
    assert respuesta.status_code == 303
    assert respuesta.headers["location"] == "/"
    galleta = respuesta.headers["set-cookie"]
    assert NOMBRE_GALLETA in galleta
    assert "HttpOnly" in galleta
    assert "samesite=lax" in galleta.lower()


async def test_el_canje_conserva_los_demas_parametros(cliente: httpx.AsyncClient) -> None:
    respuesta = await cliente.get("/", params={"c": CODIGO, "tema": "colecistitis"})
    assert respuesta.headers["location"] == "/?tema=colecistitis"


async def test_con_la_galleta_ya_se_pasa(cliente: httpx.AsyncClient, juguete: AppDeJuguete) -> None:
    await cliente.get("/", params={"c": CODIGO})
    respuesta = await cliente.get("/")
    assert respuesta.status_code == 200
    assert juguete.visitas == ["/"]


async def test_un_codigo_erroneo_no_abre(cliente: httpx.AsyncClient, juguete: AppDeJuguete) -> None:
    respuesta = await cliente.get("/", params={"c": "no-es"})
    assert respuesta.status_code == 401
    assert "no es válido" in respuesta.text
    assert juguete.visitas == []


async def test_insistir_bloquea_la_ip(cliente: httpx.AsyncClient) -> None:
    for _ in range(3):
        await cliente.get("/", params={"c": "no-es"})
    respuesta = await cliente.get("/")
    assert respuesta.status_code == 429
    assert respuesta.headers["retry-after"]
    assert "Demasiados intentos" in respuesta.text


async def test_durante_el_bloqueo_tampoco_entra_el_codigo_bueno(
    cliente: httpx.AsyncClient,
) -> None:
    # Si no, bastaría con seguir probando hasta acertar.
    for _ in range(3):
        await cliente.get("/", params={"c": "no-es"})
    respuesta = await cliente.get("/", params={"c": CODIGO})
    assert respuesta.status_code == 429


async def test_el_bloqueo_es_por_ip(cliente: httpx.AsyncClient) -> None:
    for _ in range(3):
        await cliente.get("/", params={"c": "no-es"}, headers={"cf-connecting-ip": "1.1.1.1"})
    bloqueada = await cliente.get("/", headers={"cf-connecting-ip": "1.1.1.1"})
    otra = await cliente.get("/", headers={"cf-connecting-ip": "2.2.2.2"})
    assert bloqueada.status_code == 429
    assert otra.status_code == 401


async def test_la_senalizacion_recibe_json_y_no_una_pagina(
    cliente: httpx.AsyncClient, juguete: AppDeJuguete
) -> None:
    # Quien llama a /api/offer es JavaScript, no una persona.
    respuesta = await cliente.post("/api/offer", json={"sdp": "…"})
    assert respuesta.status_code == 401
    assert respuesta.json()["error"] == "sin_acceso"
    assert "www-authenticate" not in respuesta.headers
    assert juguete.visitas == []


async def test_con_galleta_el_cuerpo_de_la_oferta_llega_intacto(
    cliente: httpx.AsyncClient, juguete: AppDeJuguete
) -> None:
    # Es lo que garantiza que el middleware no se coma el SDP por el camino.
    await cliente.get("/", params={"c": CODIGO})
    await cliente.post("/api/offer", json={"sdp": "v=0", "type": "offer"})
    assert juguete.visitas == ["/api/offer"]
    assert b"v=0" in juguete.cuerpos[0]


async def test_la_galleta_es_segura_solo_tras_el_tunel(cliente: httpx.AsyncClient) -> None:
    # En la placa la petición entra como http por loopback y el esquema real
    # solo está en la cabecera; en `make run-web` no hay ni cabecera ni TLS.
    tras_tunel = await cliente.get(
        "/", params={"c": CODIGO}, headers={"x-forwarded-proto": "https"}
    )
    assert "Secure" in tras_tunel.headers["set-cookie"]
    cliente.cookies.clear()
    en_local = await cliente.get("/", params={"c": CODIGO})
    assert "Secure" not in en_local.headers["set-cookie"]


async def test_los_rechazos_se_anotan(
    cliente: httpx.AsyncClient, eventos: list[tuple[str, str]]
) -> None:
    await cliente.get("/", params={"c": "no-es"}, headers={"cf-connecting-ip": "203.0.113.7"})
    assert ("acceso_denegado", "203.0.113.7") in eventos


# --- Y ahora sobre la aplicación de verdad ------------------------------------


def _ajustes(tmp_path: Path, **kwargs: Any) -> Settings:
    return Settings(_env_file=None, data_dir=tmp_path, **kwargs)  # type: ignore[call-arg]


async def test_la_app_real_queda_detras_de_la_puerta(tmp_path: Path) -> None:
    app = crear_app(_ajustes(tmp_path, web_codigo_acceso=CODIGO))
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transporte, base_url="http://clara") as http:
        assert (await http.get("/salud")).status_code == 200
        assert (await http.get("/", headers={"accept": "text/html"})).status_code == 401
        # Sin `app.state` (el lifespan no corre) y aun así no revienta: prueba
        # de que la puerta corta antes de llegar al router.
        assert (await http.post("/api/offer", json={})).status_code == 401


async def test_sin_codigo_configurado_la_puerta_no_se_monta(tmp_path: Path) -> None:
    # El fallo es ABIERTO a propósito: una puerta rota no puede dejar fuera a
    # quien tiene que hacer la demostración. Queda un warning en el log.
    app = crear_app(_ajustes(tmp_path))
    transporte = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transporte, base_url="http://clara") as http:
        assert (await http.get("/salud")).status_code == 200
        # Se sirve la interfaz de pipecat, no la portada de la puerta. No se
        # prueba con /api/offer: sin lifespan, el handler no existe y lo que
        # se vería sería su error, no la ausencia de puerta.
        respuesta = await http.get("/", headers={"accept": "text/html"})
        assert respuesta.status_code == 200
        assert "Código de acceso" not in respuesta.text
