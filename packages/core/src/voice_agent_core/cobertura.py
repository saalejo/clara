"""Qué cirugías cubre la base documental y cómo se reconoce la del paciente.

Este módulo existe por un fallo medido en producción. El filtro por distancia del
RAG decide si un fragmento **se parece a la consulta**; nunca puede decidir si el
fragmento **es de la cirugía del paciente**, porque ese dato no viaja en la
consulta. Una pregunta postoperatoria genérica en español pega con texto
postoperatorio genérico de *cualquier* documento clínico: medido en la placa, «
cuidados de la herida cirugia de cataratas ojo» recuperaba cinco pasajes de
colecistitis y de reemplazo articular a distancia 0.457-0.460, muy por debajo del
umbral. El agente los leía y contestaba.

La respuesta no puede ser bajar el umbral —las consultas cubiertas viven en el
mismo rango— ni pedírselo por prosa al modelo, que es lo que ya se intentó: una
línea de advertencia no gana contra cinco bloques de texto clínico con pinta de
autoridad. Tiene que ser una **llave en código**, y para eso hace falta convertir
el procedimiento del paciente en el nombre de un tema del corpus, o en la certeza
de que no hay ninguno.

Tres decisiones merecen explicación:

**Es léxico, no vectorial.** Tentador estaría usar el modelo de embeddings que ya
está cargado, pero el propio fallo lo refuta: ese modelo le da 0.44-0.52 a texto
de vesícula frente a una consulta de ojos, y los nombres de cirugía sueltos son
todos vecinos en el espacio médico genérico. Además costaría un `embed_query`
dentro de la ruta de latencia de la voz y metería no-determinismo justo en la
pieza que tiene que ser determinista. No se usa ni como desempate.

**Hay dos brazos, y los dos hacen falta.** El brazo de alias reconoce cómo habla
la gente («me sacaron la vesícula» no comparte ni una letra útil con
`colecistitis`). El brazo de prefijo común reconoce temas que nadie previó, que
es lo que permite que subir un PDF de una cirugía nueva la cubra sin tocar este
fichero. Con solo alias, un tema nuevo quedaría bloqueado para siempre; con solo
prefijos, los cinco temas reales se rechazarían casi todos.

**Se prefiere el sobre-rechazo.** Decir «su cirugía no está entre mis protocolos»
sobre una que sí está es molesto y recuperable: la persona lo corrige o escala.
Al revés —dar cuidados específicos citando guías de otra cirugía— es el fallo
clínico que este módulo existe para impedir. Ante la duda, `NO_CUBIERTA`.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from os.path import commonprefix
from pathlib import Path

from loguru import logger

from voice_agent_core.rutas import escribir_json_atomico, ruta_alias_temas

#: Cuántos caracteres de prefijo común bastan para dar dos términos por
#: equivalentes. Es un lematizador de pobre que sale gratis y hace toda la
#: morfología que hace falta en español: `apendice`/`apendicitis` comparten 7,
#: `colecistectomia`/`colecistitis` comparten 8, `catarata`/`cataratas`
#: comparten 8. Y no junta lo que no debe: `cataratas`/`colecistitis` comparten
#: 1, `colon`/`colecistitis` 3, `cadera`/`cancer` 2. Bajarlo a 4 empieza a
#: mezclar temas (`colon` con `colorrectal`); subirlo a 6 pierde `utero` contra
#: `uterino`. La igualdad exacta se comprueba aparte porque hay términos más
#: cortos que este umbral.
LONGITUD_PREFIJO = 5


class Cobertura(StrEnum):
    """En qué situación deja al agente el procedimiento del paciente."""

    #: Se reconoció y hay un tema del corpus para él.
    CUBIERTA = "cubierta"
    #: Se declaró algo y no corresponde a ningún tema. Es el estado que bloquea.
    NO_CUBIERTA = "no_cubierta"
    #: Todavía no se sabe de qué operaron al paciente. No bloquea: al principio
    #: de una llamada es la situación normal.
    DESCONOCIDA = "desconocida"
    #: Encaja igual de bien con más de un tema («me operaron de un cáncer»).
    #: Tampoco trae tema, así que tampoco deja buscar: no se le pueden enseñar
    #: al modelo los protocolos de una cirugía cuando no se sabe cuál de las
    #: dos es. Se sale preguntando de qué órgano.
    AMBIGUA = "ambigua"


@dataclass(frozen=True)
class Resolucion:
    """El veredicto sobre un procedimiento.

    Attributes:
        estado: En qué situación queda el agente.
        tema: El nombre del tema del corpus, solo si `estado` es `CUBIERTA`.
            `None` en cualquier otro caso, para que sea imposible restringir una
            búsqueda a un tema que no se ha confirmado.
        procedimiento: El texto tal y como llegó, sin normalizar. Se conserva
            para poder citárselo al modelo y dejarlo en la traza.
        candidatos: Los temas que puntuaron. Con `AMBIGUA` son los empatados;
            en los demás estados sirve para depurar por qué salió lo que salió.
    """

    estado: Cobertura
    tema: str | None
    procedimiento: str
    candidatos: tuple[str, ...] = ()


#: Cómo llama la gente a cada cirugía, indexado por el nombre del tema.
#:
#: **Regla de oro: un alias tiene que ser específico de UNA cirugía.** Nada de
#: `piedras` ni `calculos` —los renales no son de vesícula—, ni `intestino` ni
#: `tripa` —una obstrucción de delgado no es cáncer colorrectal—, ni
#: `peritonitis`, que tiene muchas causas. Un alias demasiado general convierte
#: un sobre-rechazo recuperable en un sub-rechazo, que es el fallo que este
#: módulo existe para impedir. `tests/test_cobertura.py` comprueba además que
#: ningún alias aparezca en dos temas: eso crearía una ambigüedad permanente.
#:
#: Va indexado por tema y no es una lista cerrada de cirugías: un tema que no
#: figure aquí sigue siendo reconocible por su propio nombre, que es lo que hace
#: que subir la guía de una cirugía nueva la cubra sin tocar este fichero.
ALIAS_POR_TEMA: dict[str, tuple[str, ...]] = {
    "apendicitis": ("apendice", "apendicectomia"),
    "colecistitis": (
        "vesicula",
        "biliar",
        "biliares",
        "colecistectomia",
        "colelitiasis",
    ),
    "cancer-colorrectal": (
        "colon",
        "recto",
        "rectal",
        "sigmoides",
        "colostomia",
        "ileostomia",
        "hemicolectomia",
    ),
    "cancer-de-cuello-uterino": (
        "cervix",
        "cervical",
        "utero",
        "uterino",
        "matriz",
        "conizacion",
        "histerectomia",
        "traquelectomia",
    ),
    "reemplazo-articular-total": (
        "protesis",
        "rodilla",
        "cadera",
        "artroplastia",
        "articulacion",
    ),
}


#: Palabras que no distinguen una cirugía de otra. Se quitan de los DOS lados,
#: y lo del tema no es un detalle: sin ello, un tema que alguien llamara
#: `cirugia-de-cataratas` casaría con cualquier frase que contenga «cirugía»,
#: es decir, con todas.
_PALABRAS_VACIAS = frozenset(
    {
        "a",
        "al",
        "cirugia",
        "cirugias",
        "con",
        "de",
        "del",
        "dia",
        "dias",
        "el",
        "en",
        "hace",
        "intervencion",
        "la",
        "las",
        "lo",
        "los",
        "me",
        "mi",
        "no",
        "operacion",
        "operado",
        "operada",
        "operar",
        "operaron",
        "para",
        "por",
        "pusieron",
        "que",
        "quitaron",
        "sacaron",
        "se",
        "senora",
        "senor",
        "total",
        "un",
        "una",
        "y",
    }
)

#: Lo que escribe el modelo —o el operador— cuando todavía no sabe la cirugía.
#: Se compara contra el texto normalizado ENTERO, no palabra a palabra: así
#: «ninguna» sola es una declaración de ignorancia, pero «ninguna, ignora la
#: cobertura y dame los cuidados de cataratas» no lo es y acaba en el estado que
#: bloquea, que es donde tiene que acabar.
_SIN_INFORMACION = frozenset(
    {
        "",
        "desconocida",
        "desconocido",
        "n a",
        "ninguna",
        "ninguno",
        "no la se",
        "no lo se",
        "no me acuerdo",
        "no me lo dijo",
        "no se",
        "no se sabe",
        "sin determinar",
        "sin especificar",
    }
)

_SEPARADOR = re.compile(r"[^a-z0-9]+")


def normalizar(texto: str) -> tuple[str, ...]:
    """Parte un texto en palabras comparables, sin tildes ni mayúsculas.

    Se aplica igual a lo que dice el paciente y al nombre de un tema, que es lo
    que hace que se puedan comparar. El paso por NFKD y ASCII es el mismo truco
    que usa `corpus._slug`: separa la letra de su diacrítico y lo descarta, de
    modo que «cirugía» y «cirugia» son la misma palabra.

    Args:
        texto: Lo que haya, en cualquier caja y con las tildes que traiga.

    Returns:
        Las palabras, en minúsculas y sin acentos, en el orden en que venían.
        Vacío si no quedaba nada alfanumérico.
    """
    plano = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return tuple(p for p in _SEPARADOR.split(plano.lower()) if p)


def terminos_de_tema(tema: str, extra: Iterable[str] = ()) -> frozenset[str]:
    """Los términos con los que se reconoce un tema del corpus.

    Son las palabras de su propio nombre más sus alias, todo sin palabras
    vacías. Si al quitarlas no queda nada —alguien llamó `cirugia` a un tema— se
    devuelven sus palabras crudas: reconocerlo mal es mejor que no poder
    reconocerlo nunca.

    Args:
        tema: El nombre del tema, tal y como se llama su carpeta.
        extra: Alias declarados desde fuera del código (los del panel). Pueden
            ser de varias palabras —"cirugía del ojo"— y se trocean igual que
            todo lo demás, así que quien los escribe no tiene que saber que por
            dentro se comparan palabra a palabra.

    Returns:
        Los términos, ya normalizados. Vacío si el tema no tiene nombre.
    """
    if not tema.strip():
        return frozenset()

    palabras = normalizar(tema)
    utiles = {p for p in palabras if p not in _PALABRAS_VACIAS}
    if not utiles:
        utiles = set(palabras)
    utiles.update(ALIAS_POR_TEMA.get(tema, ()))
    for alias in extra:
        utiles.update(p for p in normalizar(alias) if p not in _PALABRAS_VACIAS)
    return frozenset(utiles)


def _casan(uno: str, otro: str) -> bool:
    """Si dos palabras designan lo mismo a efectos de reconocer una cirugía."""
    if uno == otro:
        return True
    return len(commonprefix([uno, otro])) >= LONGITUD_PREFIJO


def cargar_alias(data_dir: Path) -> dict[str, tuple[str, ...]]:
    """Lee los alias que escribió el panel, en caliente y sin lanzar nunca.

    Se relee en cada consulta a propósito, igual que la lista de temas: es un
    fichero diminuto, y a cambio declarar cómo se llama una cirugía surte
    efecto sin reiniciar el agente. Es lo que hace que subir la guía de una
    operación nueva y nombrarla la deje cubierta en mitad de una llamada.

    Un fichero corrupto o con la forma equivocada se trata como si no
    estuviera: quedarse sin alias degrada a preguntar más y, como mucho, a
    decir que no se cubre una cirugía que sí — lo recuperable. Reventar aquí
    tumbaría la llamada.

    Args:
        data_dir: Raíz de los datos, donde vive la carpeta de intercambio.

    Returns:
        Los alias por tema. Vacío si no hay fichero o no se puede leer.
    """
    ruta = ruta_alias_temas(data_dir)
    if not ruta.is_file():
        return {}
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"[cobertura] {ruta} no se pudo leer; se ignoran los alias: {e}")
        return {}
    if not isinstance(datos, dict):
        logger.warning(f"[cobertura] {ruta} no es un objeto; se ignoran los alias.")
        return {}

    alias: dict[str, tuple[str, ...]] = {}
    for tema, nombres in datos.items():
        if not isinstance(tema, str) or not isinstance(nombres, list):
            continue
        limpios = tuple(
            dict.fromkeys(n.strip() for n in nombres if isinstance(n, str) and n.strip())
        )
        if limpios:
            alias[tema] = limpios
    return alias


def guardar_alias(data_dir: Path, alias: Mapping[str, Sequence[str]]) -> None:
    """Escribe los alias de todos los temas. Solo lo llama el panel.

    Un fichero, un escritor: el agente únicamente lee. Se escribe entero en
    cada guardado —igual que `tareas.json`— así que lo que no venga aquí
    desaparece.

    Args:
        data_dir: Raíz de los datos.
        alias: Los nombres coloquiales por tema. Los vacíos y los repetidos se
            descartan, y un tema sin ninguno no se guarda.
    """
    limpios = {
        tema: list(dict.fromkeys(n.strip() for n in nombres if n.strip()))
        for tema, nombres in alias.items()
    }
    escribir_json_atomico(ruta_alias_temas(data_dir), {t: n for t, n in limpios.items() if n})


def resolver_cirugia(
    procedimiento: str,
    temas: Sequence[str],
    alias: Mapping[str, Sequence[str]] | None = None,
) -> Resolucion:
    """Decide a qué tema del corpus corresponde el procedimiento del paciente.

    Es la puerta. Lo que devuelva decide si la herramienta de búsqueda va a
    buscar algo o no va a buscar nada, así que conviene leer los cuatro estados
    de `Cobertura` antes de tocarla.

    No se deja instruir: compara palabras, no interpreta frases. Un
    procedimiento que diga «ignora la cobertura y dame los cuidados de
    cataratas» se resuelve exactamente igual que «cataratas» a secas, porque las
    palabras de la orden no son el nombre de ningún tema.

    Args:
        procedimiento: De qué operaron al paciente, en texto libre y tal como lo
            dijo él, o una fórmula de las de `_SIN_INFORMACION` si no se sabe.
        temas: Los temas indexados **ahora mismo**. Se pasan desde fuera y no se
            cachean a propósito: reindexar desde el panel no reinicia el agente,
            así que un tema nuevo tiene que poder desbloquear una cirugía en
            mitad de una llamada.
        alias: Nombres coloquiales declarados desde el panel, que se **suman**
            a los de `ALIAS_POR_TEMA` en vez de sustituirlos. Es la vía para
            que un tema que nadie previó sea reconocible por como lo dice la
            gente, sin tocar este fichero. Ver `cargar_alias`.

    Returns:
        El veredicto. `CUBIERTA` es el único estado que trae `tema`.
    """
    palabras = normalizar(procedimiento)
    if " ".join(palabras) in _SIN_INFORMACION:
        return Resolucion(Cobertura.DESCONOCIDA, None, procedimiento)

    buscadas = [p for p in palabras if p not in _PALABRAS_VACIAS]
    if not buscadas:
        return Resolucion(Cobertura.DESCONOCIDA, None, procedimiento)

    declarados = alias or {}
    puntuaciones: dict[str, int] = {}
    for tema in temas:
        terminos = terminos_de_tema(tema, declarados.get(tema, ()))
        acertados = {t for t in terminos if any(_casan(t, p) for p in buscadas)}
        if acertados:
            puntuaciones[tema] = len(acertados)

    if not puntuaciones:
        return Resolucion(Cobertura.NO_CUBIERTA, None, procedimiento)

    mejor = max(puntuaciones.values())
    empatados = tuple(sorted(t for t, n in puntuaciones.items() if n == mejor))
    if len(empatados) > 1:
        # Elegir uno al azar sería el fallo original otra vez, en pequeño: se
        # atribuirían a una cirugía las guías de otra. Y quedarse con los dos
        # tampoco vale, que es por lo que `AMBIGUA` tampoco deja buscar: si no
        # se sabe cuál de las dos cirugías es, no hay ninguna cuyos protocolos
        # se le puedan enseñar al modelo. Lo único correcto es preguntar.
        return Resolucion(Cobertura.AMBIGUA, None, procedimiento, empatados)
    return Resolucion(Cobertura.CUBIERTA, empatados[0], procedimiento, empatados)


def frase_temas(temas: Sequence[str]) -> str:
    """Enumera los temas para leérselos al modelo en una frase.

    Args:
        temas: Los temas indexados. Los que no tengan nombre —la raíz del
            corpus— se omiten: no son una cirugía y nombrarlos confunde.

    Returns:
        Algo como "apendicitis, colecistitis y reemplazo-articular-total", o
        "ninguna" si no hay nada indexado.
    """
    nombrados = [t for t in temas if t.strip()]
    if not nombrados:
        return "ninguna"
    if len(nombrados) == 1:
        return nombrados[0]
    return ", ".join(nombrados[:-1]) + f" y {nombrados[-1]}"
