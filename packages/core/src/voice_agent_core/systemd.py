"""Arranque, parada y estado de unidades de systemd, por su bus de sesión.

Vive en `core` porque lo usan dos piezas: el **panel**, desde su contenedor, y el
**mando físico** de la tarjeta de sonido, nativo. Duplicarlo garantizaría que
dentro de seis meses una copia se corrigiera y la otra no — y lo que hay aquí de
valor no es el código, son los dos porqués de abajo, que costaron medirlos.

`jeepney` es Python puro y pesa 49 KB, así que traerlo a `core` no engorda la
imagen ligera del panel. Aun así, **este módulo no se importa desde ningún otro
de `core`**: `tests/test_core_liviano.py` comprueba que quien no lo pida
explícitamente no lo pague.

## Por qué D-Bus y no la API de Podman

La unidad del agente la genera Quadlet, y lo que ejecuta es `podman run ... --rm
-d --sdnotify=conmon`, con `ExecStop` y `ExecStopPost` que hacen `podman rm -f`.
El proceso que systemd vigila es **conmon**, no el agente.

Si se reiniciara el contenedor por la API de Podman, conmon moriría, systemd daría
el servicio por caído y su `ExecStopPost` **destruiría el contenedor que Podman
acaba de rearrancar**. Además el estado que enseñara el panel y el de `systemctl
status` discreparían, que es lo último que puede permitirse un panel de control.

Hablando con systemd por su bus de sesión, el dueño del ciclo de vida sigue siendo
systemd y no hay dos fuentes de verdad.

## El detalle que hace falta para que esto funcione dentro de un contenedor

La autenticación EXTERNAL de D-Bus manda el uid que el cliente cree tener y el
servidor lo contrasta con `SO_PEERCRED`. En un contenedor rootless por defecto el
proceso es uid 0 dentro pero el kernel reporta 1000 fuera, y el bus responde
`REJECTED EXTERNAL`. Por eso la unidad del panel lleva `--userns=keep-id`. Medido
en la placa: sin `keep-id` rechaza, con `keep-id` acepta.

Un proceso **nativo** no tiene ese problema: hereda el bus de sesión del gestor de
usuario y no hay remapeo de uid ninguno.

## Bloquea, y eso importa

`jeepney.io.blocking` es síncrono, y `estado()` hace cuatro viajes de ida y vuelta.
En el panel da igual —es una vista de Django—, pero en un proceso asíncrono hay que
envolver **todas** las llamadas en `asyncio.to_thread`, o cada consulta congela el
bucle de eventos. Ver `voice_agent_botones.servicios`.

## Y una asimetría que sorprende

`StartUnit` **no espera**: vuelve en centésimas de segundo en cuanto systemd
encola el trabajo, y devuelve la ruta del *trabajo*, no el resultado. El agente
tarda unos 25 segundos en estar listo, y su `active` llega 24 antes que eso.
Quien necesite saber si de verdad arrancó tiene que sondear `estado()`, y en el
caso del agente algo más: ver `voice_agent_botones.demonio._testigo_de`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jeepney import DBusAddress, MessageType, new_method_call
from jeepney.io.blocking import open_dbus_connection

GESTOR = DBusAddress(
    object_path="/org/freedesktop/systemd1",
    bus_name="org.freedesktop.systemd1",
    interface="org.freedesktop.systemd1.Manager",
)

INTERFAZ_UNIDAD = "org.freedesktop.systemd1.Unit"
INTERFAZ_SERVICIO = "org.freedesktop.systemd1.Service"


class ErrorDeControl(Exception):
    """No se pudo hablar con systemd o la unidad no responde."""


@dataclass(frozen=True)
class EstadoUnidad:
    """Foto del estado de una unidad de systemd."""

    unidad: str
    active_state: str
    sub_state: str
    resultado: str

    @property
    def activo(self) -> bool:
        """Indica si la unidad está corriendo."""
        return self.active_state == "active"

    @property
    def arrancando(self) -> bool:
        """Indica si la unidad está en pleno arranque."""
        return self.active_state == "activating"

    @property
    def fallido(self) -> bool:
        """Indica si la unidad murió con error."""
        return self.active_state == "failed"

    @property
    def parado(self) -> bool:
        """Indica si la unidad está parada del todo.

        No es lo contrario de `activo`, y confundirlos es un error medido: entre
        una orden de parada y el final hay un `deactivating` que no es ninguna de
        las dos cosas. Dar la parada por hecha ahí anuncia el final cuarenta
        milisegundos después de pedirlo, cuando el contenedor sigue muriéndose.
        """
        return self.active_state == "inactive"

    @property
    def legible(self) -> str:
        """El estado en una palabra, para enseñarlo en la portada."""
        if self.activo:
            return "en marcha"
        if self.arrancando:
            return "arrancando"
        if self.fallido:
            return f"fallido ({self.resultado})"
        return "parado"


class ControlSystemd:
    """Gobierna un conjunto acotado de unidades del gestor de usuario.

    La lista blanca es estado del objeto y no configuración global a propósito: la
    política de qué es lícito tocar pertenece a quien usa esto —el panel permite
    cuatro unidades, el mando físico dos—, y `core` se queda sin opinión.
    """

    def __init__(self, permitidas: frozenset[str]) -> None:
        """Fija qué unidades se pueden gobernar.

        Args:
            permitidas: Nombres completos de unidad, con su `.service`.
        """
        self._permitidas = permitidas

    @property
    def permitidas(self) -> frozenset[str]:
        """Las unidades que este control acepta."""
        return self._permitidas

    # -- Consulta -------------------------------------------------------------

    def estado(self, unidad: str) -> EstadoUnidad:
        """Consulta el estado de una unidad.

        Args:
            unidad: La unidad a consultar.

        Returns:
            Su estado.

        Raises:
            ErrorDeControl: Si la unidad no está permitida o systemd no responde.
        """
        self._comprobar(unidad)
        basicas = self._propiedades(unidad, INTERFAZ_UNIDAD, ("ActiveState", "SubState"))
        resultado = self._propiedades(unidad, INTERFAZ_SERVICIO, ("Result",))
        return EstadoUnidad(
            unidad=unidad,
            active_state=str(basicas.get("ActiveState", "unknown")),
            sub_state=str(basicas.get("SubState", "")),
            resultado=str(resultado.get("Result", "")),
        )

    # -- Acciones -------------------------------------------------------------

    def arrancar(self, unidad: str) -> str:
        """Arranca la unidad. Devuelve la ruta del trabajo en systemd."""
        return self._accion("StartUnit", unidad)

    def parar(self, unidad: str) -> str:
        """Para la unidad."""
        return self._accion("StopUnit", unidad)

    def reiniciar(self, unidad: str) -> str:
        """Reinicia la unidad, que es como se aplica un cambio de configuración."""
        return self._accion("RestartUnit", unidad)

    # -- Interno --------------------------------------------------------------

    def _comprobar(self, unidad: str) -> None:
        """Rechaza cualquier unidad que no esté en la lista blanca.

        El socket de D-Bus da poder sobre *todos* los servicios del usuario, no
        solo sobre los de este proyecto. El nombre de la unidad no puede venir
        nunca de una petición HTTP ni de una pulsación, y esto lo garantiza aunque
        alguien se despiste al escribir una vista.
        """
        if unidad not in self._permitidas:
            raise ErrorDeControl(f"La unidad '{unidad}' no está permitida.")

    def _accion(self, metodo: str, unidad: str) -> str:
        """Encola un trabajo sobre una unidad permitida."""
        self._comprobar(unidad)
        # "replace" es el modo estándar: encola el trabajo sustituyendo cualquier
        # otro pendiente sobre la misma unidad.
        (trabajo,) = self._llamar(metodo, "ss", (unidad, "replace"))
        return str(trabajo)

    # `_llamar` y `_propiedades` son métodos de instancia y no estáticos aunque no
    # usen `self`: es el punto por donde los tests sustituyen el bus, y una
    # jerarquía mezclando estáticos y de instancia hace que un doble de test se
    # rompa de formas difíciles de leer.
    def _llamar(self, metodo: str, firma: str, argumentos: tuple[Any, ...]) -> Any:
        """Hace una llamada al gestor de systemd y devuelve su respuesta."""
        try:
            with open_dbus_connection(bus="SESSION") as conexion:
                respuesta = conexion.send_and_get_reply(
                    new_method_call(GESTOR, metodo, firma, argumentos)
                )
        except Exception as e:
            raise ErrorDeControl(f"No se pudo hablar con systemd: {e}") from e

        if respuesta.header.message_type is MessageType.error:
            raise ErrorDeControl(f"systemd rechazó {metodo}: {respuesta.body}")
        return respuesta.body

    def _propiedades(self, unidad: str, interfaz: str, nombres: tuple[str, ...]) -> dict[str, Any]:
        """Lee propiedades de una unidad ya cargada."""
        (ruta,) = self._llamar("GetUnit", "s", (unidad,))
        propiedades = DBusAddress(
            object_path=ruta,
            bus_name="org.freedesktop.systemd1",
            interface="org.freedesktop.DBus.Properties",
        )
        valores: dict[str, Any] = {}
        try:
            with open_dbus_connection(bus="SESSION") as conexion:
                for nombre in nombres:
                    respuesta = conexion.send_and_get_reply(
                        new_method_call(propiedades, "Get", "ss", (interfaz, nombre))
                    )
                    if respuesta.header.message_type is MessageType.error:
                        continue
                    # Viene envuelto en una variante: (firma, valor).
                    valores[nombre] = respuesta.body[0][1]
        except Exception as e:
            raise ErrorDeControl(f"No se pudieron leer las propiedades: {e}") from e
        return valores


__all__ = [
    "GESTOR",
    "INTERFAZ_SERVICIO",
    "INTERFAZ_UNIDAD",
    "ControlSystemd",
    "ErrorDeControl",
    "EstadoUnidad",
]
