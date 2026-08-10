"""Expresiones cron de cinco campos, sin dependencias.

El panel valida el horario de una tarea al guardarla y el agente calcula el
próximo disparo al planificarla. Si cada uno usara su propia librería, el
contrato podría derivar: una expresión que el panel acepta y el agente no
entiende es una tarea que nunca suena y nadie sabe por qué. Por eso el parser
vive aquí, en core, y son ~100 líneas de Python puro en vez de `croniter`
(que arrastraría `python-dateutil` a la imagen ligera del panel).

Semántica vixie estándar: `minuto hora día-del-mes mes día-de-la-semana`, con
`*`, listas (`8,20`), rangos (`1-5`), pasos (`*/15`, `1-5/2`) y domingo como
0 o 7. Cuando día-del-mes y día-de-la-semana están **ambos** restringidos, el
día coincide si cumple **cualquiera** de los dos (la regla OR de vixie: `0 8
1 * 1` es "el día 1 O los lunes", no "los lunes que caigan en día 1").

Los datetimes son naive en hora local de la placa. Colombia no tiene cambio
de hora, así que no hay huecos ni minutos repetidos que tratar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

__all__ = ["ErrorDeCron", "ExpresionCron"]

#: Sin coincidencia en este plazo, la expresión se da por imposible (p. ej.
#: `0 0 31 2 *`, el 31 de febrero). Cuatro años cubren cualquier bisiesto.
_DIAS_DE_BUSQUEDA = 4 * 366

#: (mínimo, máximo, nombre legible) de cada campo, en orden.
_CAMPOS = (
    (0, 59, "minuto"),
    (0, 23, "hora"),
    (1, 31, "día del mes"),
    (1, 12, "mes"),
    (0, 7, "día de la semana"),
)


class ErrorDeCron(ValueError):
    """Expresión cron que no se puede interpretar o que nunca se cumple."""


def _parsear_campo(texto: str, minimo: int, maximo: int, nombre: str) -> frozenset[int]:
    """Convierte un campo (`*/15`, `1-5`, `8,20`...) en su conjunto de valores."""
    valores: set[int] = set()
    for parte in texto.split(","):
        cuerpo, _, paso_texto = parte.partition("/")
        if paso_texto:
            if not paso_texto.isdigit() or int(paso_texto) == 0:
                raise ErrorDeCron(f"Paso inválido '{parte}' en el campo {nombre}.")
            paso = int(paso_texto)
        else:
            paso = 1
        if cuerpo == "*":
            inicio, fin = minimo, maximo
        elif "-" in cuerpo:
            inicio_texto, _, fin_texto = cuerpo.partition("-")
            if not (inicio_texto.isdigit() and fin_texto.isdigit()):
                raise ErrorDeCron(f"Rango inválido '{parte}' en el campo {nombre}.")
            inicio, fin = int(inicio_texto), int(fin_texto)
            if inicio > fin:
                raise ErrorDeCron(
                    f"Rango al revés '{parte}' en el campo {nombre}: {inicio} > {fin}."
                )
        elif cuerpo.isdigit():
            inicio = fin = int(cuerpo)
        else:
            raise ErrorDeCron(f"No entiendo '{parte}' en el campo {nombre}.")
        if inicio < minimo or fin > maximo:
            raise ErrorDeCron(f"El campo {nombre} admite {minimo}-{maximo}, y '{parte}' se sale.")
        valores.update(range(inicio, fin + 1, paso))
    return frozenset(valores)


@dataclass(frozen=True)
class ExpresionCron:
    """Una expresión de cinco campos ya interpretada.

    Se construye con `parse`, nunca directamente: los conjuntos tienen que
    salir de un texto validado para que `siguiente` pueda fiarse de ellos.
    """

    texto: str
    minutos: frozenset[int]
    horas: frozenset[int]
    dias_mes: frozenset[int]
    meses: frozenset[int]
    dias_semana: frozenset[int]  # 0=domingo ... 6=sábado (el 7 ya se plegó a 0)
    dom_restringido: bool
    dow_restringido: bool

    @classmethod
    def parse(cls, texto: str) -> ExpresionCron:
        """Interpreta una expresión como `0 8 * * 1-5`.

        Solo valida la sintaxis; una expresión bien formada pero imposible
        (`0 0 31 2 *`) se detecta en `siguiente`, que es quien busca fechas.

        Args:
            texto: Los cinco campos separados por espacios.

        Returns:
            La expresión interpretada.

        Raises:
            ErrorDeCron: Si el texto no son cinco campos válidos.
        """
        campos = texto.split()
        if len(campos) != 5:
            raise ErrorDeCron(
                f"Una expresión cron son 5 campos (minuto hora día-mes mes "
                f"día-semana) y aquí hay {len(campos)}."
            )
        conjuntos = [
            _parsear_campo(campo, minimo, maximo, nombre)
            for campo, (minimo, maximo, nombre) in zip(campos, _CAMPOS, strict=True)
        ]
        # Domingo se escribe 0 o 7; internamente siempre 0.
        dias_semana = frozenset(0 if v == 7 else v for v in conjuntos[4])
        return cls(
            texto=texto,
            minutos=conjuntos[0],
            horas=conjuntos[1],
            dias_mes=conjuntos[2],
            meses=conjuntos[3],
            dias_semana=dias_semana,
            dom_restringido=campos[2] != "*",
            dow_restringido=campos[4] != "*",
        )

    def _dia_coincide(self, fecha: date) -> bool:
        if fecha.month not in self.meses:
            return False
        en_dom = fecha.day in self.dias_mes
        # Python cuenta lunes=0; cron cuenta domingo=0.
        en_dow = (fecha.weekday() + 1) % 7 in self.dias_semana
        if self.dom_restringido and self.dow_restringido:
            return en_dom or en_dow
        if self.dom_restringido:
            return en_dom
        return en_dow

    def siguiente(self, despues: datetime) -> datetime:
        """Primer disparo estrictamente posterior a `despues`.

        Estrictamente: si `despues` cae clavado en un disparo, se devuelve el
        siguiente. Así el planificador puede encadenar
        `proxima = siguiente(ahora)` sin dispararse dos veces en el mismo
        minuto.

        Args:
            despues: Momento de referencia, naive en hora local.

        Returns:
            El próximo disparo, con segundos a cero.

        Raises:
            ErrorDeCron: Si no hay coincidencia en ~4 años (expresión imposible).
        """
        base = despues.replace(second=0, microsecond=0) + timedelta(minutes=1)
        horas = sorted(self.horas)
        minutos = sorted(self.minutos)
        dia = base.date()
        for _ in range(_DIAS_DE_BUSQUEDA):
            if self._dia_coincide(dia):
                for hora in horas:
                    for minuto in minutos:
                        candidato = datetime.combine(dia, time(hora, minuto))
                        if candidato >= base:
                            return candidato
            dia += timedelta(days=1)
        raise ErrorDeCron(f"La expresión '{self.texto}' no se cumple nunca.")

    def proximas(self, despues: datetime, n: int = 3) -> list[datetime]:
        """Los próximos `n` disparos, para la vista previa del panel."""
        momentos: list[datetime] = []
        momento = despues
        for _ in range(n):
            momento = self.siguiente(momento)
            momentos.append(momento)
        return momentos
