"""Genera el formulario de ajustes a partir de la clase `Settings`.

Escribir a mano un formulario con los cuarenta campos de `Settings` sería
duplicar una lista que rota en cuanto alguien añada un ajuste, y el síntoma
—"lo cambié en el panel y no aparece"— es de los que cuestan una tarde. Aquí se
deriva del propio modelo de pydantic: tipos, rangos, opciones de los enums y,
lo más valioso, las descripciones.

Esas descripciones en `config.py` son excelentes y llevan cifras medidas en la
placa ("tiny 2.9 s, base 5.1 s, small 16.6 s"). Al leerlas de ahí, **el panel
hereda esa documentación sin copiarla**, y no puede quedarse desfasada.

`tests/panel/test_introspeccion.py` comprueba que todo campo de `Settings` acaba
en un campo de formulario o está explícitamente protegido, que es lo que atrapa
al que añade un ajuste y se olvida del panel.
"""

from __future__ import annotations

import json
import types
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from annotated_types import Ge, Gt, Le, Lt
from django import forms
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings

from voice_agent_core.config import CAMPOS_PROTEGIDOS, Settings

#: Secciones del formulario, en el orden en que se enseñan. Cada campo cae en la
#: primera cuyo prefijo case; el resto va a "Otros".
SECCIONES: list[tuple[str, tuple[str, ...]]] = [
    ("Modelo de lenguaje", ("gemini_", "groq_", "llm_")),
    ("Transcripción (STT)", ("stt_", "whisper_", "deepgram_")),
    ("Voz (TTS)", ("tts_",)),
    ("Audio y detección de voz", ("audio_", "vad_", "user_speech_", "mic_gate_")),
    ("Muletillas", ("filler_",)),
    ("Base de conocimiento (RAG)", ("chroma_", "embedding_", "rag_", "chunk_")),
]


@dataclass(frozen=True)
class CampoAjuste:
    """Un campo de `Settings` listo para pintarse en un formulario."""

    nombre: str
    seccion: str
    campo: forms.Field
    por_defecto: Any

    @property
    def por_defecto_json(self) -> str:
        """El valor por defecto, como se escribiría en la instantánea."""
        return _a_json(self.por_defecto)


def _a_json(valor: Any) -> str:
    """Serializa un valor a JSON, tolerando enums y rutas."""
    if isinstance(valor, Enum):
        valor = valor.value
    elif isinstance(valor, Path):
        valor = str(valor)
    return json.dumps(valor, ensure_ascii=False)


def _seccion_de(nombre: str) -> str:
    for titulo, prefijos in SECCIONES:
        if nombre.startswith(prefijos):
            return titulo
    return "Otros"


def _limites(info: FieldInfo) -> dict[str, Any]:
    """Traduce las restricciones de pydantic a los límites del widget.

    `gt`/`lt` se convierten en `min_value`/`max_value` inclusivos porque los
    formularios de Django no distinguen; para los rangos que hay aquí —todos con
    márgenes anchos— la diferencia es irrelevante y la validación de verdad la
    hace pydantic al exportar.
    """
    limites: dict[str, Any] = {}
    for restriccion in info.metadata:
        if isinstance(restriccion, Ge | Gt):
            limites["min_value"] = restriccion.ge if isinstance(restriccion, Ge) else restriccion.gt
        elif isinstance(restriccion, Le | Lt):
            limites["max_value"] = restriccion.le if isinstance(restriccion, Le) else restriccion.lt
    return limites


def _tipo_efectivo(anotacion: Any) -> tuple[Any, bool]:
    """Desenvuelve `X | None` y dice si el campo admite vacío.

    Returns:
        El tipo de dentro y si era opcional.
    """
    if get_origin(anotacion) in (Union, types.UnionType):
        argumentos = [a for a in get_args(anotacion) if a is not type(None)]
        if len(argumentos) == 1:
            return argumentos[0], True
    return anotacion, False


def _construir_campo(nombre: str, info: FieldInfo) -> forms.Field | None:
    """Crea el campo de formulario para un campo de `Settings`.

    Returns:
        El campo, o `None` si ese ajuste no tiene sentido en el panel.
    """
    tipo, opcional = _tipo_efectivo(info.annotation)
    comun: dict[str, Any] = {
        "label": nombre,
        # Aquí es donde el panel hereda la documentación de config.py.
        "help_text": info.description or "",
        "required": False,
    }

    if isinstance(tipo, type) and issubclass(tipo, Enum):
        return forms.ChoiceField(
            choices=[("", "— por defecto —")] + [(m.value, m.value) for m in tipo], **comun
        )
    if tipo is bool:
        # Tres estados, no dos: sí, no, y "no lo fijes y deja mandar al .env".
        return forms.ChoiceField(
            choices=[("", "— por defecto —"), ("true", "sí"), ("false", "no")], **comun
        )
    if tipo is int:
        return forms.IntegerField(**comun, **_limites(info))
    if tipo is float:
        return forms.FloatField(**comun, **_limites(info))
    if tipo is str:
        return forms.CharField(**comun)

    # Rutas y secretos. Las primeras cambian entre el anfitrión y el contenedor
    # y los segundos no pasan por aquí: ambos están en CAMPOS_PROTEGIDOS, así
    # que esto solo se alcanza si alguien añade un tipo nuevo a Settings.
    _ = opcional
    return None


def campos_editables(modelo: type[BaseSettings] = Settings) -> list[CampoAjuste]:
    """Devuelve todos los ajustes que el panel puede tocar, ya por secciones."""
    campos: list[CampoAjuste] = []
    for nombre, info in modelo.model_fields.items():
        if nombre in CAMPOS_PROTEGIDOS:
            continue
        campo = _construir_campo(nombre, info)
        if campo is None:
            continue
        campos.append(
            CampoAjuste(
                nombre=nombre,
                seccion=_seccion_de(nombre),
                campo=campo,
                por_defecto=info.get_default(call_default_factory=True),
            )
        )
    return campos


def texto_a_valor(nombre: str, texto: str, modelo: type[BaseSettings] = Settings) -> Any:
    """Convierte lo que se tecleó en el formulario al valor que va al JSON.

    Args:
        nombre: El campo de `Settings`.
        texto: Lo introducido, ya como cadena.
        modelo: La clase de configuración, inyectable para los tests.

    Returns:
        El valor tipado.

    Raises:
        ValueError: Si el texto no encaja con el tipo del campo.
    """
    info = modelo.model_fields[nombre]
    tipo, _ = _tipo_efectivo(info.annotation)

    if isinstance(tipo, type) and issubclass(tipo, Enum):
        return tipo(texto).value
    if tipo is bool:
        if texto not in ("true", "false"):
            raise ValueError(f"'{texto}' no es un booleano.")
        return texto == "true"
    if tipo is int:
        return int(texto)
    if tipo is float:
        return float(texto)
    return texto
