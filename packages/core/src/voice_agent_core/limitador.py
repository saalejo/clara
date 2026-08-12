"""Freno de intentos por IP, en memoria y sin dependencias.

Lo necesitan **los dos** frentes expuestos a internet —la puerta de la interfaz
de llamada (`voice_agent.acceso`) y el login del panel—, y el único paquete que
ambos pueden importar es este: el panel nunca importa `voice_agent` (ver
`CLAUDE.md`). Por eso vive aquí y no en el agente.

No se usa `django.core.cache` ni una librería de *rate limiting* a propósito.
El motivo es probable, no ideológico: aquí el reloj se inyecta, y eso es lo que
permite comprobar un bloqueo de quince minutos sin dormir quince minutos.

Tampoco hay cerrojos. Los dos consumidores corren en un solo proceso y un solo
hilo (uvicorn con `workers=1`) y asyncio no interrumpe a mitad de una
sentencia, así que un `deque` basta.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

#: Cabecera donde cloudflared deja la IP real del visitante.
CABECERA_CLOUDFLARE = "cf-connecting-ip"

#: Cuántas IPs distintas se recuerdan como mucho. Sin este tope, un barrido
#: desde muchas direcciones haría crecer el diccionario sin límite y el freno
#: acabaría siendo el ataque.
MAX_CLAVES = 2048

#: Lo que se devuelve cuando no hay forma de saber quién llama. Todas las
#: peticiones sin identificar comparten cubo, que es lo prudente.
IP_DESCONOCIDA = "desconocida"


def ip_del_cliente(cabeceras: Mapping[str, str], remota: str | None) -> str:
    """La IP real del visitante, mirando primero las cabeceras del proxy.

    Detrás del túnel, `remota` —lo que ve el servidor— es siempre 127.0.0.1 y
    no sirve para distinguir a nadie. cloudflared pone la IP verdadera en
    `CF-Connecting-IP` y la **reescribe siempre**, así que un cliente no puede
    falsificarla *a través del túnel*. Sí podría hacerlo quien alcance el
    puerto directamente por la red local; de ahí que la unidad de systemd
    escuche solo en loopback (ver `deploy/clara-web.service`).

    Args:
        cabeceras: Las de la petición. Se leen sin distinguir mayúsculas.
        remota: La dirección del socket, si se conoce.

    Returns:
        La IP como cadena, o `IP_DESCONOCIDA` si no hay ninguna pista.
    """
    # Se normaliza en vez de confiar en el mapa: Starlette y Django traen
    # cabeceras insensibles a mayúsculas, pero un diccionario corriente —el que
    # se pasa en un test, o el que traiga el próximo framework— no lo es, y el
    # fallo sería silencioso: todo el mundo compartiendo el cubo de
    # "desconocida".
    normalizadas = {clave.lower(): valor for clave, valor in cabeceras.items()}
    directa = normalizadas.get(CABECERA_CLOUDFLARE, "")
    if directa.strip():
        return directa.strip()
    reenviada = normalizadas.get("x-forwarded-for", "")
    if reenviada.split(",")[0].strip():
        return reenviada.split(",")[0].strip()
    return remota.strip() if remota and remota.strip() else IP_DESCONOCIDA


@dataclass
class LimitadorDeIntentos:
    """Cubo de fallos por clave, con ventana deslizante y castigo.

    Mientras una clave acumule menos de `max_intentos` fallos dentro de
    `ventana_secs`, se la deja pasar. Al alcanzar el tope se la bloquea durante
    `bloqueo_secs` contados desde el último fallo.

    Attributes:
        max_intentos: Fallos tolerados dentro de la ventana.
        ventana_secs: Anchura de la ventana deslizante.
        bloqueo_secs: Castigo desde el último fallo, una vez alcanzado el tope.
        reloj: Fuente de tiempo monótona. Inyectable para los tests: es lo que
            permite probar un castigo largo en microsegundos.
    """

    max_intentos: int = 5
    ventana_secs: float = 300.0
    bloqueo_secs: float = 900.0
    reloj: Callable[[], float] = time.monotonic
    _fallos: dict[str, deque[float]] = field(default_factory=dict, repr=False)

    def permitido(self, clave: str) -> bool:
        """¿Se le deja intentarlo otra vez?"""
        return self.segundos_restantes(clave) == 0

    def segundos_restantes(self, clave: str) -> int:
        """Segundos que faltan para levantar el bloqueo; 0 si no lo hay.

        Sirve para la cabecera `Retry-After` y para decírselo a quien mira la
        pantalla: un bloqueo sin plazo se confunde con una avería.
        """
        marcas = self._vigentes(clave)
        # `not marcas` no es redundante con la comparación: con `max_intentos`
        # a 0 —el valor que desactiva un limitador— la comparación es falsa
        # con el cubo vacío y `marcas[-1]` reventaría.
        if not marcas or len(marcas) < self.max_intentos:
            return 0
        restan = marcas[-1] + self.bloqueo_secs - self.reloj()
        return max(0, int(restan) + 1) if restan > 0 else 0

    def anotar_fallo(self, clave: str) -> None:
        """Apunta un intento fallido."""
        marcas = self._vigentes(clave)
        marcas.append(self.reloj())
        self._fallos[clave] = marcas
        self._podar()

    def olvidar(self, clave: str) -> None:
        """Borra el historial de una clave. Se llama tras un acierto."""
        self._fallos.pop(clave, None)

    def limpiar(self) -> None:
        """Vacía el limitador entero. Para los tests y poco más."""
        self._fallos.clear()

    def _vigentes(self, clave: str) -> deque[float]:
        """Las marcas de esa clave que aún cuentan, ya purgadas.

        Un bloqueo en curso NO se purga aunque sus marcas queden fuera de la
        ventana: si no, el castigo duraría lo que la ventana y no lo que dice
        `bloqueo_secs`.
        """
        marcas = self._fallos.get(clave)
        if not marcas:
            return deque()
        ahora = self.reloj()
        if len(marcas) >= self.max_intentos and ahora - marcas[-1] < self.bloqueo_secs:
            return marcas
        limite = ahora - self.ventana_secs
        while marcas and marcas[0] < limite:
            marcas.popleft()
        if not marcas:
            self._fallos.pop(clave, None)
        return marcas

    def _podar(self) -> None:
        """Mantiene el diccionario acotado descartando lo más viejo."""
        if len(self._fallos) <= MAX_CLAVES:
            return
        for clave in list(self._fallos):
            self._vigentes(clave)
        while len(self._fallos) > MAX_CLAVES:
            mas_vieja = min(self._fallos, key=lambda c: self._fallos[c][-1])
            del self._fallos[mas_vieja]


__all__ = [
    "CABECERA_CLOUDFLARE",
    "IP_DESCONOCIDA",
    "MAX_CLAVES",
    "LimitadorDeIntentos",
    "ip_del_cliente",
]
