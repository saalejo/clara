"""Troceado de documentos en fragmentos aptos para un índice vectorial.

Un modelo de embeddings comprime cada fragmento en un único vector, así que el
tamaño del fragmento decide la calidad del RAG:

* **Demasiado grande**: el vector mezcla varios temas, se vuelve un promedio
  difuso y deja de parecerse a ninguna pregunta concreta.
* **Demasiado pequeño**: el fragmento pierde el contexto que lo hacía
  comprensible ("cuesta 40 euros" sin decir el qué).

La estrategia implementada es un *troceado recursivo por separadores*: se
intenta cortar primero por los límites más semánticos que existan (encabezados
Markdown, después párrafos, después líneas, después frases) y solo se recurre a
cortar por palabras cuando no queda más remedio. Así los cortes caen casi
siempre donde un humano los haría.

Se implementa a mano, en unas pocas decenas de líneas, en lugar de traer una
librería tipo LangChain: es menos dependencia, es fácil de leer y —sobre todo—
es fácil de probar.
"""

from __future__ import annotations

from dataclasses import dataclass

# Ordenados de más a menos semántico. El troceador baja por la lista hasta
# encontrar uno que permita partir el texto en piezas manejables.
SEPARADORES_POR_DEFECTO: tuple[str, ...] = (
    "\n## ",  # encabezado Markdown de nivel 2
    "\n### ",  # encabezado Markdown de nivel 3
    "\n\n",  # párrafo
    "\n",  # línea
    ". ",  # frase
    " ",  # palabra
    "",  # carácter (último recurso)
)


@dataclass(frozen=True)
class Fragmento:
    """Un fragmento de texto listo para indexar.

    Attributes:
        texto: El contenido del fragmento.
        indice: Su posición dentro del documento de origen, empezando en 0.
    """

    texto: str
    indice: int


def _partir(texto: str, separadores: tuple[str, ...], tamano: int) -> list[str]:
    """Parte el texto recursivamente en piezas que no superen `tamano`.

    Args:
        texto: Texto a partir.
        separadores: Separadores por orden de preferencia.
        tamano: Longitud máxima de cada pieza, en caracteres.

    Returns:
        Las piezas resultantes, todas de longitud menor o igual a `tamano`
        salvo que ni siquiera cortando por caracteres se pueda (imposible en la
        práctica, porque el último separador es la cadena vacía).
    """
    if len(texto) <= tamano:
        return [texto] if texto else []

    if not separadores:
        # Sin separadores que probar, se corta en seco.
        return [texto[i : i + tamano] for i in range(0, len(texto), tamano)]

    separador, resto = separadores[0], separadores[1:]

    if separador == "":
        return [texto[i : i + tamano] for i in range(0, len(texto), tamano)]

    if separador not in texto:
        # Este separador no aparece; se prueba el siguiente sin cortar nada.
        return _partir(texto, resto, tamano)

    piezas: list[str] = []
    for trozo in texto.split(separador):
        # Se restituye el separador para no perder los saltos de línea ni los
        # puntos: el fragmento tiene que seguir leyéndose bien.
        candidato = trozo if not piezas else separador + trozo
        if len(candidato) <= tamano:
            piezas.append(candidato)
        else:
            piezas.extend(_partir(candidato, resto, tamano))
    return [p for p in piezas if p.strip()]


def _cola_para_solape(texto: str, solape: int) -> str:
    """Devuelve la cola de `texto` que arrancará el siguiente fragmento.

    Cortar sin más por número de caracteres parte palabras por la mitad y deja
    fragmentos que empiezan por "ra es monofónica...". Además de leerse mal,
    perjudica al modelo de embeddings, que tokeniza esos restos como basura. Se
    avanza hasta el primer espacio para empezar en palabra completa.

    Args:
        texto: Fragmento que se acaba de cerrar.
        solape: Número de caracteres a repetir, como máximo.

    Returns:
        La cola, empezando en un límite de palabra.
    """
    if solape <= 0:
        return ""
    cola = texto[-solape:]
    espacio = cola.find(" ")
    return cola[espacio + 1 :] if espacio != -1 else cola


def _agrupar(piezas: list[str], tamano: int, solape: int) -> list[str]:
    """Junta piezas pequeñas en fragmentos cercanos a `tamano`, con solape.

    El solape hace que una frase que caiga justo en la frontera entre dos
    fragmentos aparezca completa al menos en uno de ellos. Sin él, la
    información que cruza un corte se vuelve irrecuperable.

    Args:
        piezas: Piezas ya suficientemente pequeñas.
        tamano: Tamaño objetivo del fragmento resultante.
        solape: Cuántos caracteres del final de un fragmento se repiten al
            principio del siguiente.

    Returns:
        Los fragmentos agrupados.
    """
    fragmentos: list[str] = []
    actual = ""
    # Indica si desde el último volcado se ha añadido contenido nuevo. Sin
    # esto, un documento cuya última pieza provoque un volcado dejaría como
    # fragmento final la pura cola de solape, es decir, texto ya indexado.
    hay_contenido_nuevo = False

    for pieza in piezas:
        if actual and len(actual) + len(pieza) > tamano:
            fragmentos.append(actual.strip())
            actual = _cola_para_solape(actual, solape)
            hay_contenido_nuevo = False
        actual += pieza
        hay_contenido_nuevo = True

    if hay_contenido_nuevo and actual.strip():
        fragmentos.append(actual.strip())

    return fragmentos


def trocear(
    texto: str,
    *,
    tamano: int = 700,
    solape: int = 120,
    separadores: tuple[str, ...] = SEPARADORES_POR_DEFECTO,
) -> list[Fragmento]:
    """Trocea un documento en fragmentos indexables.

    Args:
        texto: Contenido completo del documento.
        tamano: Tamaño objetivo de cada fragmento, en caracteres.
        solape: Caracteres compartidos entre fragmentos consecutivos.
        separadores: Separadores por orden de preferencia.

    Returns:
        La lista de fragmentos, numerados por su orden de aparición.

    Raises:
        ValueError: Si el solape es mayor o igual que el tamaño, lo que haría
            que el troceado no avanzase nunca.
    """
    if solape >= tamano:
        raise ValueError(f"El solape ({solape}) debe ser menor que el tamaño ({tamano}).")

    normalizado = texto.replace("\r\n", "\n").strip()
    if not normalizado:
        return []

    piezas = _partir(normalizado, separadores, tamano)
    fragmentos = _agrupar(piezas, tamano, solape)
    return [Fragmento(texto=f, indice=i) for i, f in enumerate(fragmentos) if f]
