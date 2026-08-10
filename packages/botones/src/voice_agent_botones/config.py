"""Configuración del demonio de botones.

Se lee del entorno con el prefijo `BOTONES_`, salvo `DATA_DIR` y `LOG_LEVEL`,
que son del proyecto entero y llegan sin prefijo. El fichero de referencia es
`.env.botones.example`.

A diferencia de `voice_agent_core.config.Settings`, aquí **no** hay instantánea
del panel: estos ajustes son de hardware y de ergonomía, se tocan una vez al
instalar y no tiene sentido exponerlos en un formulario web. Si algún día hace
falta, el sitio es `Settings`, no este fichero.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# La ruta estable del device HID de la tarjeta. Se usa el enlace de
# `/dev/input/by-id/` y no `/dev/input/event0` porque la numeración de los
# `eventN` depende del orden de enumeración del USB: basta reconectar el hub para
# que la tarjeta pase a `event1` y el demonio se quede leyendo el botón de
# recovery de la placa, que es un fallo silencioso y desconcertante.
DISPOSITIVO_POR_DEFECTO = Path(
    "/dev/input/by-id/usb-C-Media_Electronics_Inc._USB_PnP_Sound_Device-event-if03"
)


class ModoMicrofono(StrEnum):
    """Cómo se silencia el micrófono en el mezclador.

    Attributes:
        SWITCH: Con el interruptor de captura (`sset Mic nocap`). Es el modo
            bueno: está medido que deja la grabación en silencio digital
            absoluto (pico 0) incluso con la captura ya abierta por `dsnoop`.
        GANANCIA: Bajando la ganancia de captura a cero (`sset Mic 0`). Existe
            como escape para una tarjeta cuyo control no tenga `cswitch`; en la
            del proyecto no hace falta.
    """

    SWITCH = "switch"
    GANANCIA = "ganancia"


class Ajustes(BaseSettings):
    """Ajustes del demonio de botones."""

    model_config = SettingsConfigDict(
        env_prefix="BOTONES_",
        env_file=".env.botones",
        extra="ignore",
        case_sensitive=False,
        # Sin esto, un campo con `validation_alias` se puede poblar SOLO por el
        # alias: `Ajustes(directorio_datos=...)` se descartaría en silencio (por
        # `extra="ignore"`) y ganaría la variable de entorno. Es exactamente lo
        # que rompió el primer test que se escribió de esta clase.
        populate_by_name=True,
    )

    # --- Dónde vive todo -----------------------------------------------------

    directorio_datos: Path = Field(
        default=Path("data"),
        validation_alias="DATA_DIR",
        description="Volumen compartido del proyecto. Nativo: <proyecto>/data.",
    )

    log_level: str = Field(
        default="INFO",
        validation_alias="LOG_LEVEL",
        description="Nivel de registro, como en el resto del proyecto.",
    )

    # --- El dispositivo de entrada -------------------------------------------

    dispositivo: Path = Field(
        default=DISPOSITIVO_POR_DEFECTO,
        description="Device HID de la tarjeta. Usa siempre una ruta de /dev/input/by-id/.",
    )

    acaparar: bool = Field(
        default=True,
        description=(
            "Tomar el device en exclusiva con EVIOCGRAB. Necesario porque tiene "
            "handler `kbd` y sin esto las teclas se inyectan además en la consola "
            "virtual. Ponlo a 0 para poder depurar con la unidad en marcha."
        ),
    )

    espera_dispositivo_segundos: float = Field(
        default=2.0,
        gt=0.0,
        le=60.0,
        description=(
            "Cada cuánto se reintenta abrir el device cuando no está. Pasa en el "
            "arranque de la placa, antes de que el USB enumere, y cuando el hub se "
            "reenumera y la tarjeta desaparece un momento."
        ),
    )

    # --- Ergonomía de los gestos ---------------------------------------------

    ventana_agrupacion_ms: int = Field(
        default=120,
        ge=0,
        le=1000,
        description=(
            "Una soltada no es definitiva hasta que pasan estos milisegundos sin "
            "otra pulsación de la misma tecla. Hace de antirrebote y absorbe una "
            "autorrepetición que este device no manda hoy pero podría mandar."
        ),
    )

    umbral_largo_ms: int = Field(
        default=700,
        ge=100,
        description="A partir de aquí, un mantenido del rocker es de nivel 2.",
    )

    umbral_muy_largo_ms: int = Field(
        default=2500,
        ge=200,
        description="A partir de aquí, nivel 3. Ha de ser mayor que umbral_largo_ms.",
    )

    confirmacion_segundos: float = Field(
        default=10.0,
        gt=0.0,
        le=30.0,
        description=(
            "Ventana para confirmar un verbo destructivo con un clic de MUTE. "
            "Cualquier gesto del rocker cancela. Diez segundos, y no cuatro como se "
            "había planeado, porque probándolo caducaron cinco confirmaciones "
            "seguidas: entre mantener el rocker cinco segundos, soltarlo, oír el "
            "pitido y encontrar el otro botón, cuatro no dan. Y el clic que llega "
            "tarde no se pierde: cae como «silenciar micrófono»."
        ),
    )

    # --- El mezclador --------------------------------------------------------

    tarjeta_alsa: str = Field(
        default="Device",
        description="Nombre de la tarjeta para `amixer -c`. Es el de la USB del proyecto.",
    )

    control_micro: str = Field(
        default="Mic",
        description="Control simple del micrófono. Tiene cswitch y cvolume (0-16).",
    )

    control_altavoz: str = Field(
        default="Speaker",
        description="Control simple de la salida. Tiene pvolume (0-151) y pswitch.",
    )

    modo_micro: ModoMicrofono = Field(
        default=ModoMicrofono.SWITCH,
        description="Cómo se silencia el micrófono. Ver ModoMicrofono.",
    )

    ganancia_micro_abierto: int = Field(
        default=14,
        ge=0,
        description=(
            "Ganancia de captura a la que se vuelve al reabrir el micrófono en modo "
            "`ganancia`. 14 de 16 es el valor que documenta docs/audio.md."
        ),
    )

    paso_volumen: int = Field(
        default=6,
        ge=1,
        le=50,
        description=(
            "Porcentaje que sube o baja cada pulsación, en volumen MAPEADO (`amixer -M`). "
            "Con 6 hacen falta 16 pulsaciones de fondo a tope, medido en la placa."
        ),
    )

    timeout_amixer_segundos: float = Field(
        default=2.0,
        gt=0.0,
        le=30.0,
        description="Tope para una orden de amixer. Si vence, se pita error y se sigue.",
    )

    recordatorio_micro_minutos: int = Field(
        default=0,
        ge=0,
        description=(
            "Si es mayor que cero, un pip discreto cada tantos minutos mientras el "
            "micrófono siga silenciado. Existe porque el agente no sabe que está mudo "
            "y desde fuera parece averiado. Cero lo desactiva."
        ),
    )

    # --- Las unidades que se gobiernan ---------------------------------------

    unidad_agente: str = Field(
        default="voice-agent.service",
        description="Unidad del agente: la que arrancan y paran los gestos de nivel 3.",
    )

    unidad_telefonia: str = Field(
        default="voice-agent-telefonia.service",
        description="Unidad del puente Bluetooth: la que apaga el modo `solo tarjeta`.",
    )

    espera_unidad_segundos: float = Field(
        default=90.0,
        gt=0.0,
        le=300.0,
        description=(
            "Cuánto se espera a que una unidad esté lista antes de pitar error. "
            "`StartUnit` vuelve en 0,01 s, pero para el agente ni eso ni el `active` de "
            "systemd significan nada: lo que cuenta es que escriba su "
            "`estado_arranque.json`, y desde la orden hasta ahí se han medido unos 25 s. "
            "Noventa da margen a un arranque con la caché fría sin pitar error tras un "
            "éxito; el propio `TimeoutStartSec` de la unidad son 300."
        ),
    )

    # --- Derivados -----------------------------------------------------------

    @property
    def socket_telefonia(self) -> Path:
        """Socket unix del puente de telefonía, derivado del directorio de datos.

        Se deriva y no se configura por el mismo motivo que en el puente: dentro
        del contenedor `DATA_DIR` es `/data` y en nativo es `<proyecto>/data`, y
        un valor fijo por defecto reproduciría una trampa que este proyecto ya
        pagó, cuyo síntoma es un servicio en marcha, logs limpios y nadie capaz
        de hablar con él.
        """
        return self.directorio_datos / "run" / "telefonia.sock"

    @property
    def dir_pitidos(self) -> Path:
        """Directorio donde se generan y cachean los WAV de feedback."""
        return self.directorio_datos / "pitidos"

    @property
    def unidades_gobernadas(self) -> frozenset[str]:
        """Lista blanca que se le pasa al control de systemd.

        El demonio solo puede tocar estas dos. La política vive aquí, en el
        borde, y no dentro de `voice_agent_core.systemd`, que se mantiene sin
        opinión sobre qué es lícito.
        """
        return frozenset({self.unidad_agente, self.unidad_telefonia})

    # --- Validación ----------------------------------------------------------

    @field_validator("directorio_datos", "dispositivo")
    @classmethod
    def _expandir(cls, valor: Path) -> Path:
        """Expande `~` para que una ruta de un `.env` escrito a mano funcione."""
        return valor.expanduser()

    @model_validator(mode="after")
    def _umbrales_ordenados(self) -> Ajustes:
        """Comprueba que los umbrales de nivel crecen.

        Si se invirtieran, el nivel 3 sería inalcanzable y los gestos que cuelgan
        de él —parar y reiniciar el agente— dejarían de existir en silencio. Mejor
        no arrancar.
        """
        if self.umbral_muy_largo_ms <= self.umbral_largo_ms:
            raise ValueError(
                f"BOTONES_UMBRAL_MUY_LARGO_MS ({self.umbral_muy_largo_ms}) tiene que ser "
                f"mayor que BOTONES_UMBRAL_LARGO_MS ({self.umbral_largo_ms}); si no, el "
                "nivel 3 es inalcanzable y no habría forma de parar el agente."
            )
        return self


__all__ = ["DISPOSITIVO_POR_DEFECTO", "Ajustes", "ModoMicrofono"]
