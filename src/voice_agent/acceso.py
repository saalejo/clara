"""La puerta de la interfaz de llamada.

`clara.voz-digital.com` está publicada en internet y detrás hay una placa de
4 GB sin GPU, una cuota gratuita de modelo y **una sola sesión de conversación**
(`ConnectionMode.SINGLE`). Sin puerta, cualquiera que dé con la URL gasta todo
eso; y como una oferta nueva desaloja a la anterior, además puede cortarle la
palabra a quien esté hablando.

El trato es: quien tiene que entrar recibe un enlace con el código dentro
(`https://clara.voz-digital.com/?c=CODIGO`), la puerta lo canjea por una galleta
firmada y no vuelve a pedir nada. Quien llegue sin él ve una portada con un
campo. El resto de internet se queda fuera.

Decisiones que no son obvias mirando el código:

* **Middleware ASGI puro, no `@app.middleware("http")`.** Ese azúcar es
  `BaseHTTPMiddleware`, que abre un grupo de tareas de anyio y una tarea extra
  **por petición**, y esto corre en cada fichero estático de la interfaz. Una
  clase ASGI es una llamada a función. De regalo, se puede envolver cualquier
  aplicación de juguete y probar la puerta sin importar `web.py` —y por tanto
  sin cargar pipecat ni chromadb— en milisegundos.
* **La clave de firma se deriva del propio código.** No hay una segunda
  variable de entorno que alguien deje vacía sin enterarse: si no hay código,
  la puerta ni se monta. Y rotar el código invalida todas las galletas ya
  emitidas, que es exactamente lo que se quiere si el enlace se filtra.
* **La galleta no lleva el código**, solo una fecha de caducidad y su MAC.
  Manipular la fecha cambia la firma.
* **El formulario de la portada es un GET a `/`**, no un POST a una ruta
  propia. Al enviarlo, el navegador construye `/?c=…`, que es exactamente el
  mismo camino que el enlace: un solo flujo que probar y cero CSRF.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import time
from collections.abc import Callable
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from voice_agent_core.limitador import LimitadorDeIntentos, ip_del_cliente

#: Versión del formato de la galleta. Cambiarla invalida las ya emitidas.
VERSION = "c1"

#: Nombre de la galleta en el navegador.
NOMBRE_GALLETA = "clara_acceso"

#: El parámetro del enlace que se reparte: `?c=CODIGO`.
PARAMETRO_CODIGO = "c"

#: Lo único que se sirve sin código. `/salud` es la sonda del túnel: cerrarla
#: haría que cloudflared diera el origen por caído y tumbaría la demostración
#: entera por defendernos de nadie.
RUTAS_PUBLICAS: frozenset[str] = frozenset({"/salud"})

#: Un código más largo que esto ni se compara: es basura, o un intento de
#: hacernos gastar CPU en HMACs.
MAX_LARGO_CODIGO = 128

_ETIQUETA_CLAVE = b"clara/puerta-de-acceso/v1"


def _clave_de_firma(codigo: str) -> bytes:
    """Deriva la clave HMAC del propio código de acceso.

    Es un HMAC con etiqueta fija y no `blake2s(person=...)`: `person` admite
    **ocho bytes** y lanza `ValueError` si te pasas, un fallo que solo
    aparecería con un código configurado de verdad, o sea en la placa y no en
    los tests.
    """
    return hmac.new(_ETIQUETA_CLAVE, codigo.encode("utf-8"), hashlib.sha256).digest()


def firmar(codigo: str, expira: int) -> str:
    """Construye el valor de la galleta: `c1.<caducidad>.<firma>`."""
    cuerpo = f"{VERSION}.{expira}"
    firma = hmac.new(_clave_de_firma(codigo), cuerpo.encode("ascii"), hashlib.sha256).digest()
    return f"{cuerpo}.{base64.urlsafe_b64encode(firma).decode('ascii').rstrip('=')}"


def galleta_valida(codigo: str, valor: str | None, ahora: float | None = None) -> bool:
    """Comprueba una galleta sin fiarse de nada de lo que trae.

    La caducidad viaja en claro dentro del cuerpo firmado, así que se
    **recalcula la galleta entera** a partir de lo que dice traer y se comparan
    las dos cadenas con `hmac.compare_digest`, que no se rinde en el primer
    byte distinto: comparar con `==` filtraría la firma byte a byte a quien
    cronometre las respuestas.
    """
    if not valor or valor.count(".") != 2:
        return False
    version, texto_expira, _ = valor.split(".")
    if version != VERSION or not texto_expira.isdigit():
        return False
    expira = int(texto_expira)
    if not hmac.compare_digest(firmar(codigo, expira), valor):
        return False
    return (time.time() if ahora is None else ahora) < expira


def codigo_correcto(codigo: str, recibido: str) -> bool:
    """Compara el código tecleado con el configurado, en tiempo constante."""
    if not recibido or len(recibido) > MAX_LARGO_CODIGO:
        return False
    return hmac.compare_digest(codigo, recibido)


def pagina_de_puerta(error: str | None = None, minutos_bloqueo: int = 0) -> str:
    """La portada que ve quien llega sin código.

    Autocontenida a propósito: sin JS, sin fuentes y sin imágenes. Es lo
    primero que carga alguien que quizá esté en una red mala y con prisa.
    """
    aviso = ""
    if minutos_bloqueo > 0:
        plural = "s" if minutos_bloqueo != 1 else ""
        aviso = (
            f'<p class="error">Demasiados intentos fallidos. Vuelve a probar '
            f"en {minutos_bloqueo} minuto{plural}.</p>"
        )
    elif error:
        aviso = f'<p class="error">{html.escape(error)}</p>'
    campo = "" if minutos_bloqueo > 0 else _FORMULARIO
    return _PLANTILLA.format(aviso=aviso, formulario=campo)


_FORMULARIO = """    <form method="get" action="/">
      <label for="c">Código de acceso</label>
      <input id="c" name="c" autocomplete="off" autofocus required
             spellcheck="false" maxlength="128">
      <button type="submit">Entrar</button>
    </form>"""

_PLANTILLA = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Clara — acceso a la demostración</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center;
         font: 16px/1.55 system-ui, sans-serif; padding: 1.5rem;
         background: Canvas; color: CanvasText; }}
  main {{ max-width: 26rem; width: 100%; }}
  h1 {{ font-size: 1.3rem; margin: 0 0 .35rem; }}
  p {{ margin: 0 0 1rem; }}
  .apunte {{ opacity: .75; font-size: .92rem; }}
  .error {{ color: #b3261e; font-weight: 600; }}
  form {{ display: grid; gap: .5rem; }}
  label {{ font-size: .9rem; font-weight: 600; }}
  input, button {{ font: inherit; padding: .6rem .7rem; border-radius: .5rem;
                  border: 1px solid color-mix(in srgb, CanvasText 35%, transparent); }}
  input {{ background: Canvas; color: CanvasText; }}
  button {{ border: 0; background: #1f6feb; color: #fff; font-weight: 600;
           cursor: pointer; }}
  footer {{ margin-top: 1.75rem; font-size: .82rem; opacity: .6; }}
</style>
</head>
<body>
<main>
  <h1>Clara — acceso a la demostración</h1>
  <p class="apunte">Esta interfaz de voz corre en una placa con recursos
  limitados y atiende una llamada cada vez, así que el acceso va con el código
  del enlace que recibiste.</p>
  {aviso}
{formulario}
  <footer>Seguimiento postoperatorio · voz-digital.com</footer>
</main>
</body>
</html>
"""


def _es_https(peticion: Request) -> bool:
    """Decide el flag `Secure` de la galleta leyendo la petición.

    En la placa la conexión entra por el túnel: uvicorn la ve como `http` sobre
    loopback y el esquema real solo está en `X-Forwarded-Proto`, que pone
    cloudflared. En `make run-web` no hay ni túnel ni cabecera. Marcar `Secure`
    siempre haría que el navegador tirase la galleta en local y la puerta
    pidiera el código en bucle; no marcarlo nunca la dejaría viajar en claro.
    Por eso no es un ajuste, sino una lectura por petición.
    """
    reenviado = peticion.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return reenviado == "https" or peticion.url.scheme == "https"


class PuertaDeAcceso:
    """Middleware ASGI: sin galleta firmada, no se pasa de la portada."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        codigo: str,
        duracion_secs: int,
        limitador: LimitadorDeIntentos,
        publicas: frozenset[str] = RUTAS_PUBLICAS,
        al_evento: Callable[[str, str], None] | None = None,
    ) -> None:
        """Envuelve una aplicación ASGI.

        Args:
            app: La aplicación protegida.
            codigo: El código que abre. Nunca vacío: quien monta la puerta ya
                ha comprobado que hay uno.
            duracion_secs: Lo que vale la galleta.
            limitador: Freno de intentos, compartido con el resto del proceso.
            publicas: Rutas que se sirven sin código.
            al_evento: Gancho opcional `(tipo, ip)` para las métricas. Se
                inyecta en vez de importar `voice_agent.metrica` porque ese
                módulo arrastra pipecat y dejaría este a su merced.
        """
        self.app = app
        self._codigo = codigo
        self._duracion = duracion_secs
        self._limitador = limitador
        self._publicas = publicas
        self._al_evento = al_evento

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Deja pasar, canjea el código o cierra el paso."""
        if scope["type"] != "http" or scope.get("path") in self._publicas:
            await self.app(scope, receive, send)
            return

        peticion = Request(scope)
        # Es el camino más común y el más barato: quien ya entró paga un HMAC.
        if galleta_valida(self._codigo, peticion.cookies.get(NOMBRE_GALLETA)):
            await self.app(scope, receive, send)
            return

        ip = ip_del_cliente(peticion.headers, peticion.client.host if peticion.client else None)
        recibido = peticion.query_params.get(PARAMETRO_CODIGO)
        # El canje solo en GET/HEAD: redirigir un POST perdería el cuerpo, y el
        # navegador siempre carga la página antes de llamar a /api/offer.
        if recibido is not None and scope.get("method") in ("GET", "HEAD"):
            respuesta = self._canjear(peticion, ip, recibido)
        else:
            respuesta = self._cerrar_el_paso(peticion, ip)
        await respuesta(scope, receive, send)

    def _canjear(self, peticion: Request, ip: str, recibido: str) -> Response:
        """Comprueba el código y, si vale, emite la galleta."""
        restantes = self._limitador.segundos_restantes(ip)
        if restantes > 0:
            # Durante el bloqueo tampoco entra el código correcto: si no,
            # bastaría con seguir probando hasta acertar.
            self._anotar("acceso_bloqueado", ip)
            return self._respuesta_bloqueado(restantes)
        if not codigo_correcto(self._codigo, recibido):
            self._limitador.anotar_fallo(ip)
            self._anotar("acceso_denegado", ip)
            return self._respuesta_portada("El código no es válido.", estado=401)
        self._limitador.olvidar(ip)
        self._anotar("acceso_concedido", ip)
        return self._respuesta_de_canje(peticion)

    def _cerrar_el_paso(self, peticion: Request, ip: str) -> Response:
        """Portada para quien navega, JSON para quien programa."""
        restantes = self._limitador.segundos_restantes(ip)
        if restantes > 0:
            return self._respuesta_bloqueado(restantes)
        if peticion.method in ("GET", "HEAD") and "text/html" in peticion.headers.get("accept", ""):
            return self._respuesta_portada(None, estado=401)
        # Quien llama aquí es JavaScript (`/api/offer`), no una persona. 401 y
        # no 403: falta la credencial, no es que sobre autorización. Y sin
        # `WWW-Authenticate`, que abriría el cuadro de diálogo nativo del
        # navegador, que aquí no sirve para nada y confunde.
        self._anotar("acceso_denegado", ip)
        return JSONResponse(
            {
                "error": "sin_acceso",
                "mensaje": (
                    "Esta demostración necesita el enlace con código de acceso. "
                    "Abre la dirección que recibiste y vuelve a pulsar Conectar."
                ),
            },
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )

    def _respuesta_de_canje(self, peticion: Request) -> Response:
        """Fija la galleta y reenvía a la misma página sin el `?c=`.

        La redirección es lo que impide que el código se quede en la barra de
        direcciones de una captura de pantalla, en el historial o en el
        `Referer` de la primera llamada a `/api/offer`. Los demás parámetros de
        la query se conservan: se llega a la URL que se habría tecleado.
        """
        restantes = [
            (k, v) for k, v in peticion.query_params.multi_items() if k != PARAMETRO_CODIGO
        ]
        destino = peticion.url.path + (f"?{urlencode(restantes)}" if restantes else "")
        respuesta = RedirectResponse(destino, status_code=303)
        respuesta.set_cookie(
            NOMBRE_GALLETA,
            firmar(self._codigo, int(time.time()) + self._duracion),
            max_age=self._duracion,
            httponly=True,
            # Lax y no Strict: con Strict, volver a pulsar el enlace desde el
            # correo o WhatsApp NO manda la galleta —es una navegación entre
            # sitios— y se vería la portada teniendo ya acceso. Lax no debilita
            # nada aquí: no hay ninguna acción con efectos que dispare un GET.
            samesite="lax",
            secure=_es_https(peticion),
            path="/",
        )
        respuesta.headers["Cache-Control"] = "no-store"
        return respuesta

    def _respuesta_portada(self, error: str | None, estado: int) -> Response:
        return HTMLResponse(
            pagina_de_puerta(error),
            status_code=estado,
            headers={"Cache-Control": "no-store"},
        )

    def _respuesta_bloqueado(self, restantes: int) -> Response:
        minutos = max(1, -(-restantes // 60))
        return HTMLResponse(
            pagina_de_puerta(None, minutos_bloqueo=minutos),
            status_code=429,
            headers={"Retry-After": str(restantes), "Cache-Control": "no-store"},
        )

    def _anotar(self, tipo: str, ip: str) -> None:
        if self._al_evento is not None:
            self._al_evento(tipo, ip)


__all__ = [
    "MAX_LARGO_CODIGO",
    "NOMBRE_GALLETA",
    "PARAMETRO_CODIGO",
    "RUTAS_PUBLICAS",
    "VERSION",
    "PuertaDeAcceso",
    "codigo_correcto",
    "firmar",
    "galleta_valida",
    "pagina_de_puerta",
]
