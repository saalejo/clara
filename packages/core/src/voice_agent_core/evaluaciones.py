"""El contrato de las evaluaciones clínicas: triaje, alertas y resúmenes.

El reto pide dos cosas que tienen que sobrevivir a la llamada: una **alerta
persistida y estructurada** cuando el agente decide escalar, y un **resumen de
llamada** con cinco campos concretos (paciente y procedimiento, síntomas,
decisión, referencias y próximos pasos). Este módulo define esos modelos y
vive en `core` por el mismo motivo que `tareas.py`: el agente los escribe y el
panel los lee, y el panel no puede importar `voice_agent`.

La asimetría clínica gobierna el diseño: el falso negativo —no alertar cuando
tocaba— es la falla catastrófica. Por eso la alerta se escribe **en cuanto se
decide**, no al colgar: una llamada que se cae después de detectar una bandera
roja tiene que dejar la alerta ya en disco.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class NivelAlerta(StrEnum):
    """Nivel de criticidad del triaje, el vocabulario del dataset del reto.

    Attributes:
        VERDE: Evolución normal. Se maneja con indicaciones de autocuidado y
            los signos de alarma a vigilar.
        AMARILLO: Hallazgo que requiere valoración médica no urgente: se
            escala al equipo para contacto en las próximas veinticuatro horas.
        ROJO: Signo de alarma. Se escala de inmediato y se le indica al
            paciente acudir a urgencias.
    """

    VERDE = "verde"
    AMARILLO = "amarillo"
    ROJO = "rojo"


class Alerta(BaseModel):
    """Una decisión de escalamiento, tal y como queda persistida.

    Attributes:
        id_llamada: La llamada en la que se decidió, para poder cruzarla con
            su traza y su resumen.
        momento: Fecha y hora ISO de la decisión.
        nivel: El nivel de triaje decidido.
        sintomas: Los síntomas reportados que sustentan la decisión, en frases
            completas.
        justificacion: Por qué ese nivel y no otro, citando el protocolo o
            documento que lo respalda cuando lo haya.
        procedimiento: De qué se operó el paciente, tal y como consta. Lo pone
            el sistema, no el modelo.
        cobertura: Si el corpus cubría esa cirugía cuando se decidió
            ("cubierta", "no_cubierta", "desconocida" o "ambigua"). Es lo que
            deja auditar si un triaje se apoyó en protocolos de verdad o se
            escaló por precaución general.
    """

    id_llamada: str
    momento: str
    nivel: NivelAlerta
    sintomas: str
    justificacion: str
    procedimiento: str = ""
    cobertura: str = ""


class ResumenLlamada(BaseModel):
    """El resumen estructurado que queda al terminar una llamada.

    Los cinco primeros campos son exactamente los que la rúbrica del reto
    exige; `documentos_consultados` es la traza real de la llamada —qué
    documentos respaldaron las respuestas—, que se adjunta desde el registro
    del RAG y no de la memoria del modelo.
    """

    id_llamada: str
    momento: str
    #: El color del triaje de la llamada ("verde", "amarillo" o "rojo"),
    #: copiado por el sistema de la última alerta registrada — no de la
    #: memoria del modelo. Vacío solo si la llamada terminó sin triaje.
    nivel: str = ""
    paciente_y_procedimiento: str
    #: La cirugía sola, sin el nombre del paciente y sin pasar por la prosa del
    #: modelo: la pone el sistema desde lo que armó la puerta de cobertura. Es
    #: la que se puede cruzar con los temas del corpus; `paciente_y_procedimiento`
    #: es texto libre y no se puede.
    procedimiento: str = ""
    #: Si el corpus cubría esa cirugía: "cubierta", "no_cubierta", "desconocida"
    #: o "ambigua". Un resumen con "no_cubierta" y ningún documento consultado
    #: es un "no lo sé" bien hecho, no una llamada que falló.
    cobertura: str = ""
    sintomas: str
    decision: str
    referencias: str = ""
    proximos_pasos: str
    documentos_consultados: list[str] = Field(default_factory=list)
    #: Solo en los resúmenes de respaldo (llamada cortada sin despedida): la
    #: transcripción cruda, para que el equipo no pierda lo dicho aunque el
    #: modelo no llegara a redactar el resumen.
    transcripcion: list[str] = Field(default_factory=list)
