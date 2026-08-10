"""La forma del corpus: temas, documentos y los nombres que se aceptan.

Un **tema** es una subcarpeta de `corpus/`, y un **documento** un fichero
indexable dentro de ella (o suelto en la raíz, que cuenta como el tema sin
nombre). El corpus tiene exactamente dos niveles::

    corpus/
    ├── como-funciona-el-agente.md      <- tema raíz, TEMA_RAIZ = ""
    └── la-placa/
        └── especificaciones.pdf        <- tema "la-placa"

Este módulo vive en `core` y no en `voice_agent.rag` por una razón concreta: lo
importan **los dos** procesos. El panel crea y borra temas y documentos, y el
agente los indexa, pero el panel no puede importar `voice_agent` —arrastraría
chromadb y su imagen pasaría de 280 MB a más de 1 GB—. Si cada uno tuviera su
propia idea de qué extensiones valen, el panel aceptaría un `.docx`, lo
escribiría en el corpus y la ingesta lo ignoraría **en silencio**: el documento
aparecería en la lista y el agente no sabría nada de él, sin un solo error por
ningún lado. Con una única definición eso es imposible por construcción.

Es también el módulo donde vive la validación de nombres, que desde que existe
la página de Conocimiento ya no es cosmética: los nombres llegan de un formulario
web. Ver `validar_componente` y `resolver`.

Solo biblioteca estándar. `tests/test_core_liviano.py` lo vigila.
"""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: Extensiones que la ingesta sabe leer. Es la lista que comparten el panel (que
#: decide qué se puede subir) y `rag/ingest.py` (que decide qué se indexa).
EXTENSIONES_SOPORTADAS: frozenset[str] = frozenset({".md", ".txt", ".markdown", ".pdf"})

#: El tema sin nombre: los documentos sueltos en la raíz del corpus. No es una
#: carpeta, así que no se puede crear ni borrar, pero sí admite documentos.
TEMA_RAIZ = ""

#: Tope de tamaño de un documento subido desde el panel. No está medido: es un
#: valor elegido para que un despiste no llene la microSD ni haga que el panel
#: —que corre con --memory=256m— se quede sin memoria.
MAX_BYTES_DOCUMENTO = 5 * 1024 * 1024

#: Longitud máxima del nombre de un tema. No es un número redondo por gusto: el
#: nombre del tema acaba formando el de una colección de ChromaDB, que admite
#: entre 3 y 63 caracteres (ver `rag/store.py`). Con el prefijo por defecto
#: quedan 49 libres; 40 deja margen para cambiar CHROMA_COLLECTION sin que se
#: rompa nada. `tests/test_store_colecciones.py` comprueba que se cumple.
MAX_LONGITUD_TEMA = 40

#: Longitud máxima del nombre de un documento. Más holgada porque no viaja a
#: ChromaDB; el límite real es el del sistema de ficheros (255).
MAX_LONGITUD_DOCUMENTO = 120

#: Alfabeto permitido. Es una lista **blanca**: se enumera lo que vale, no lo que
#: no vale. Empieza por alfanumérico para que el nombre sirva también como
#: nombre de colección de Chroma, que lo exige.
_PERMITIDO = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class NombreInvalido(ValueError):
    """El nombre propuesto para un tema o un documento no se acepta."""


class ErrorDeCorpus(Exception):
    """La operación sobre el corpus no se puede hacer tal y como está pedida."""


@dataclass(frozen=True)
class Documento:
    """Un fichero indexable del corpus.

    Attributes:
        tema: El tema al que pertenece, o `TEMA_RAIZ` si está suelto.
        nombre: Nombre del fichero, con extensión.
        ruta_relativa: Su ruta desde la raíz del corpus. Es **exactamente** el
            `origen` con el que la ingesta lo guarda en los metadatos, de modo
            que lo que enseña el panel y lo que hay en el índice se pueden
            comparar sin traducciones por el camino.
        tamano_bytes: Tamaño en disco.
        modificado_en: Fecha de modificación, en segundos desde la época.
    """

    tema: str
    nombre: str
    ruta_relativa: str
    tamano_bytes: int
    modificado_en: float


@dataclass(frozen=True)
class Tema:
    """Un tema del corpus con los documentos que contiene.

    Attributes:
        nombre: El nombre del tema, o `TEMA_RAIZ` para los documentos sueltos.
        documentos: Sus documentos, ordenados por nombre.
    """

    nombre: str
    documentos: tuple[Documento, ...]

    @property
    def es_raiz(self) -> bool:
        """Indica si es el tema sin nombre, que no se puede borrar."""
        return self.nombre == TEMA_RAIZ


# --- Nombres -----------------------------------------------------------------


def validar_componente(nombre: str, *, maximo: int = MAX_LONGITUD_TEMA) -> str:
    """Comprueba que un nombre es un componente de ruta aceptable.

    Es la comprobación **estricta**, la que se usa para buscar y para borrar. No
    transforma nada: o el nombre vale tal cual, o se rechaza. Eso es
    deliberado — ver `normalizar_tema` para el porqué.

    Varias de las comprobaciones son redundantes con la expresión regular del
    final. Están puestas a propósito y por separado: son justo lo que alguien
    relajaría sin darse cuenta el día que quiera "ampliar el alfabeto para
    admitir tildes", y así cada una falla con su propio mensaje.

    Args:
        nombre: El componente a validar.
        maximo: Longitud máxima admitida.

    Returns:
        El mismo nombre, normalizado a NFC.

    Raises:
        NombreInvalido: Si no cumple alguna de las reglas.
    """
    nombre = unicodedata.normalize("NFC", nombre).strip()

    if not nombre:
        raise NombreInvalido("El nombre no puede estar vacío.")
    if len(nombre) > maximo:
        raise NombreInvalido(f"El nombre no puede pasar de {maximo} caracteres.")
    if nombre in (".", ".."):
        raise NombreInvalido(f"'{nombre}' no es un nombre, es una referencia a una carpeta.")
    if nombre.startswith("."):
        raise NombreInvalido("El nombre no puede empezar por un punto.")
    if "/" in nombre or "\\" in nombre:
        raise NombreInvalido("El nombre no puede contener barras: es un solo nivel, no una ruta.")
    if os.sep in nombre or (os.altsep and os.altsep in nombre):
        raise NombreInvalido("El nombre no puede contener separadores de ruta.")
    if any(ord(c) < 32 or c == "\x7f" for c in nombre):
        raise NombreInvalido("El nombre no puede contener caracteres de control.")
    if not _PERMITIDO.fullmatch(nombre):
        raise NombreInvalido(
            f"'{nombre}' tiene caracteres que no se admiten. Usa minúsculas sin "
            "tildes, cifras, guiones y puntos, empezando por letra o cifra."
        )
    return nombre


def _slug(propuesto: str, maximo: int) -> str:
    """Convierte un texto libre en un componente de ruta seguro."""
    # NFKD separa la letra de su diacrítico, y el paso por ASCII se lo lleva:
    # "Guía" -> "Guia". Es lo que permite escribir el tema en español en el
    # navegador y guardarlo como una carpeta manejable desde una terminal.
    base = unicodedata.normalize("NFKD", propuesto).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-zA-Z0-9]+", "-", base).strip("-").lower()
    return base[:maximo].strip("-.")


def normalizar_tema(propuesto: str) -> str:
    """Convierte lo que se escribió en el navegador en el nombre de una carpeta.

    Se slugifica **solo al crear**, nunca al buscar ni al borrar. Si se
    slugificara también para buscar, el día que cambiaran las reglas del slug
    dejaría de encontrarse una carpeta creada con las reglas de ayer, y el
    síntoma sería un "ese tema no existe" sobre un tema que está ahí delante.

    Que termine llamando a `validar_componente` es la propiedad que sostiene
    toda la seguridad de rutas de este módulo: para cualquier entrada, esta
    función **o lanza, o devuelve algo que pasa la validación estricta**.

    Args:
        propuesto: Texto libre, tal y como lo escribió una persona.

    Returns:
        El nombre de la carpeta.

    Raises:
        NombreInvalido: Si no queda nada utilizable.
    """
    base = _slug(propuesto, MAX_LONGITUD_TEMA)
    if not base:
        raise NombreInvalido(
            f"'{propuesto}' no deja ningún carácter utilizable para el nombre de un tema."
        )
    return validar_componente(base, maximo=MAX_LONGITUD_TEMA)


def normalizar_documento(propuesto: str) -> str:
    """Convierte el nombre de un fichero subido en uno seguro, con su extensión.

    Args:
        propuesto: El nombre original del fichero.

    Returns:
        El nombre con el que se guardará.

    Raises:
        NombreInvalido: Si la extensión no se admite o no queda nombre.
    """
    # `PurePath.suffix` sobre un nombre ya saneado por el navegador basta aquí:
    # la ruta completa no se usa, solo la última extensión.
    extension = Path(propuesto).suffix.lower()
    if extension not in EXTENSIONES_SOPORTADAS:
        admitidas = ", ".join(sorted(EXTENSIONES_SOPORTADAS))
        raise NombreInvalido(
            f"'{propuesto}' no es un documento indexable. Se admiten: {admitidas}."
        )

    base = _slug(Path(propuesto).stem, MAX_LONGITUD_DOCUMENTO - len(extension))
    if not base:
        raise NombreInvalido(
            f"'{propuesto}' no deja ningún carácter utilizable para el nombre del fichero."
        )
    return validar_componente(base + extension, maximo=MAX_LONGITUD_DOCUMENTO)


def tema_de(ruta_relativa: str) -> str:
    """Deduce el tema al que pertenece una ruta relativa al corpus.

    Args:
        ruta_relativa: Por ejemplo `la-placa/especificaciones.pdf`.

    Returns:
        El nombre del tema, o `TEMA_RAIZ` si el documento está suelto.
    """
    partes = Path(ruta_relativa).parts
    return partes[0] if len(partes) > 1 else TEMA_RAIZ


# --- Rutas -------------------------------------------------------------------


def resolver(corpus_dir: Path, tema: str, nombre: str | None = None) -> Path:
    """Compone una ruta dentro del corpus y garantiza que no se sale de él.

    **Ninguna ruta del corpus se compone fuera de esta función.** Es la regla de
    una línea que hay que poder repetir al revisar este módulo: la validación de
    nombres es la primera barrera, y esto es la segunda, la que sigue en pie
    aunque la primera se relaje por descuido.

    `resolve()` sigue los enlaces simbólicos, de modo que un `corpus/tema -> /etc`
    creado a mano se detecta aquí aunque el nombre fuese impecable.

    Args:
        corpus_dir: Raíz del corpus.
        tema: Nombre del tema, o `TEMA_RAIZ` para la raíz.
        nombre: Nombre del documento, o None para apuntar a la carpeta del tema.

    Returns:
        La ruta absoluta, exista o no todavía.

    Raises:
        NombreInvalido: Si algún componente no vale o la ruta se sale del corpus.
    """
    raiz = corpus_dir.resolve()
    destino = raiz
    if tema != TEMA_RAIZ:
        destino = destino / validar_componente(tema, maximo=MAX_LONGITUD_TEMA)
    if nombre is not None:
        destino = destino / validar_componente(nombre, maximo=MAX_LONGITUD_DOCUMENTO)

    if not destino.resolve().is_relative_to(raiz):
        etiqueta = f"{tema}/{nombre}" if nombre else tema
        raise NombreInvalido(f"'{etiqueta}' apunta fuera del corpus.")
    return destino


# --- Inventario --------------------------------------------------------------


def _es_documento(ruta: Path) -> bool:
    """Indica si un fichero cuenta como documento indexable.

    Los enlaces simbólicos se descartan a propósito. Desde que el panel escribe
    en el corpus conviene que lo que se lista y lo que se indexa sea lo mismo, y
    un enlace es la única forma de que el corpus apunte a un fichero de fuera.
    El panel no crea ninguno; los que haya son de alguien con acceso por SSH.
    """
    return (
        ruta.is_file()
        and not ruta.is_symlink()
        and not ruta.name.startswith(".")
        and ruta.suffix.lower() in EXTENSIONES_SOPORTADAS
    )


def _documento(raiz: Path, ruta: Path) -> Documento:
    """Construye el `Documento` de un fichero ya comprobado.

    `raiz` tiene que venir **ya resuelta**, porque `ruta` lo está: sale de
    `resolver`, que devuelve rutas absolutas. Compararla contra un `corpus_dir`
    relativo —que es lo que llega cuando el panel corre con `make panel` desde la
    raíz del repositorio— hace que `relative_to` falle.
    """
    relativa = ruta.relative_to(raiz)
    estado = ruta.stat()
    return Documento(
        tema=tema_de(str(relativa)),
        nombre=ruta.name,
        ruta_relativa=str(relativa),
        tamano_bytes=estado.st_size,
        modificado_en=estado.st_mtime,
    )


def listar_documentos(corpus_dir: Path, tema: str) -> list[Documento]:
    """Lista los documentos de un tema, ordenados por nombre.

    Args:
        corpus_dir: Raíz del corpus.
        tema: El tema, o `TEMA_RAIZ` para los documentos sueltos.

    Returns:
        Sus documentos. Lista vacía si el tema no existe.

    Raises:
        NombreInvalido: Si el nombre del tema no vale.
    """
    carpeta = resolver(corpus_dir, tema)
    if not carpeta.is_dir():
        return []
    raiz = corpus_dir.resolve()
    return sorted(
        (_documento(raiz, r) for r in carpeta.iterdir() if _es_documento(r)),
        key=lambda d: d.nombre,
    )


def listar_temas(corpus_dir: Path) -> list[str]:
    """Lista los nombres de los temas existentes, ordenados.

    No incluye `TEMA_RAIZ`, que no es una carpeta. Se saltan las carpetas ocultas
    y los enlaces simbólicos, por el mismo motivo que en `_es_documento`.

    Args:
        corpus_dir: Raíz del corpus.

    Returns:
        Los nombres de los temas.
    """
    if not corpus_dir.is_dir():
        return []
    return sorted(
        r.name
        for r in corpus_dir.iterdir()
        if r.is_dir() and not r.is_symlink() and not r.name.startswith(".")
    )


def inventario(corpus_dir: Path) -> list[Tema]:
    """Devuelve el corpus entero: el tema raíz primero y luego los demás.

    Args:
        corpus_dir: Raíz del corpus.

    Returns:
        Los temas con sus documentos. El primero es siempre el tema raíz, aunque
        esté vacío: es donde se sueltan los documentos que no encajan en ninguno.
    """
    temas = [Tema(nombre=TEMA_RAIZ, documentos=tuple(listar_documentos(corpus_dir, TEMA_RAIZ)))]
    temas.extend(
        Tema(nombre=nombre, documentos=tuple(listar_documentos(corpus_dir, nombre)))
        for nombre in listar_temas(corpus_dir)
    )
    return temas


def descubrir_documentos(corpus_dir: Path) -> list[Path]:
    """Lista los documentos indexables del corpus, en sus dos niveles.

    Recorre la raíz y **un solo nivel** de subcarpetas, que es exactamente lo que
    el panel enseña. Recorrer más hondo sería peor que inútil: se indexarían
    documentos que la página de Conocimiento no lista ni deja borrar, y ese
    desajuste entre lo que se ve y lo que sabe el agente es justo lo que este
    módulo existe para evitar.

    Args:
        corpus_dir: Carpeta raíz del corpus.

    Returns:
        Rutas ordenadas de los ficheros soportados: primero las de la raíz y
        luego las de cada tema.

    Raises:
        FileNotFoundError: Si la carpeta del corpus no existe.
    """
    if not corpus_dir.is_dir():
        raise FileNotFoundError(
            f"No existe la carpeta del corpus: {corpus_dir.resolve()}\n"
            f"Créala y mete dentro los documentos (.md, .txt, .pdf) que quieras que el agente conozca."
        )
    rutas: list[Path] = [corpus_dir / d.nombre for d in listar_documentos(corpus_dir, TEMA_RAIZ)]
    for tema in listar_temas(corpus_dir):
        rutas.extend(corpus_dir / tema / d.nombre for d in listar_documentos(corpus_dir, tema))
    return rutas


def documentos_ignorados(corpus_dir: Path) -> list[Path]:
    """Documentos que existen pero quedan fuera del índice, para poder avisar.

    Son los que están a más de un nivel de profundidad. Devolverlos permite que
    la ingesta los registre en el log en vez de ignorarlos sin decir nada, que es
    como se pierde media hora buscando por qué el agente no conoce un fichero que
    está claramente en el corpus.

    Args:
        corpus_dir: Raíz del corpus.

    Returns:
        Las rutas que no se van a indexar.
    """
    if not corpus_dir.is_dir():
        return []
    indexables = set(descubrir_documentos(corpus_dir))
    return sorted(
        r
        for r in corpus_dir.rglob("*")
        if r.is_file() and r.suffix.lower() in EXTENSIONES_SOPORTADAS and r not in indexables
    )


def marca_de_cambio(corpus_dir: Path) -> float:
    """La fecha del último cambio en el corpus, contando ficheros **y carpetas**.

    Las carpetas cuentan, y no es un detalle: borrar un documento no cambia la
    fecha de ningún fichero —el fichero ya no está— pero sí la del directorio que
    lo contenía. Sin mirar los directorios, el panel no se enteraría de ningún
    borrado y diría que el índice está al día cuando no lo está.

    Limitación conocida: restaurar un fichero antiguo con `cp -p` conserva su
    fecha y puede mover esta marca hacia atrás. No compensa complicarlo.

    Args:
        corpus_dir: Raíz del corpus.

    Returns:
        El máximo de las fechas de modificación, o 0.0 si el corpus no existe.
    """
    if not corpus_dir.is_dir():
        return 0.0
    # lstat y no stat: la fecha que interesa es la del enlace, no la de su
    # destino, que puede estar fuera del corpus.
    marcas = [corpus_dir.stat().st_mtime]
    marcas.extend(r.lstat().st_mtime for r in corpus_dir.rglob("*"))
    return max(marcas)


# --- Operaciones -------------------------------------------------------------


def crear_tema(corpus_dir: Path, propuesto: str) -> str:
    """Crea la carpeta de un tema.

    Args:
        corpus_dir: Raíz del corpus.
        propuesto: El nombre tal y como se escribió; se slugifica.

    Returns:
        El nombre con el que ha quedado, que puede no ser el propuesto.

    Raises:
        NombreInvalido: Si el nombre no vale.
        ErrorDeCorpus: Si ya existe.
    """
    nombre = normalizar_tema(propuesto)
    carpeta = resolver(corpus_dir, nombre)
    if carpeta.exists():
        raise ErrorDeCorpus(f"El tema '{nombre}' ya existe.")
    carpeta.mkdir(parents=True)
    return nombre


def borrar_tema(corpus_dir: Path, tema: str) -> None:
    """Borra la carpeta de un tema, que tiene que estar vacía.

    Se exige que esté vacío a propósito: un formulario web no debe poder
    disparar un borrado recursivo. Vaciarlo obliga a ver antes qué había dentro.

    Args:
        corpus_dir: Raíz del corpus.
        tema: El tema a borrar.

    Raises:
        NombreInvalido: Si el nombre no vale.
        ErrorDeCorpus: Si es el tema raíz, si no existe, si es un enlace o si
            todavía tiene documentos.
    """
    if tema == TEMA_RAIZ:
        raise ErrorDeCorpus(
            "El tema raíz no es una carpeta: son los documentos sueltos del corpus. "
            "Bórralos uno a uno."
        )
    carpeta = resolver(corpus_dir, tema)
    if carpeta.is_symlink():
        raise ErrorDeCorpus(f"'{tema}' es un enlace simbólico; el panel no lo toca.")
    if not carpeta.is_dir():
        raise ErrorDeCorpus(f"El tema '{tema}' no existe.")

    contenido = list(carpeta.iterdir())
    if contenido:
        raise ErrorDeCorpus(
            f"El tema '{tema}' todavía tiene {len(contenido)} fichero(s). Bórralos primero."
        )
    carpeta.rmdir()


def guardar_documento(
    corpus_dir: Path, tema: str, propuesto: str, trozos: Iterable[bytes]
) -> Documento:
    """Guarda un documento en un tema, de forma atómica.

    Recibe un iterable de trozos y no los bytes enteros para encajar con
    `UploadedFile.chunks()` de Django sin que este módulo sepa nada de Django, y
    para poder cortar en cuanto se pasa del tope sin haber cargado el fichero
    completo en memoria — el contenedor del panel corre con 256 MB.

    Args:
        corpus_dir: Raíz del corpus.
        tema: El tema de destino, o `TEMA_RAIZ`.
        propuesto: Nombre original del fichero; se slugifica conservando la
            extensión.
        trozos: El contenido, en trozos.

    Returns:
        El documento tal y como ha quedado guardado.

    Raises:
        NombreInvalido: Si el nombre o la extensión no valen.
        ErrorDeCorpus: Si el tema no existe, si ya hay un documento con ese
            nombre o si el contenido pasa de `MAX_BYTES_DOCUMENTO`.
    """
    carpeta = resolver(corpus_dir, tema)
    if not carpeta.is_dir():
        # No se crea al vuelo: un tema con una errata se crearía solo y el
        # documento desaparecería de la vista sin que nadie entendiera por qué.
        raise ErrorDeCorpus(f"El tema '{tema or 'raíz'}' no existe.")

    nombre = normalizar_documento(propuesto)
    destino = resolver(corpus_dir, tema, nombre)
    if destino.exists():
        raise ErrorDeCorpus(
            f"Ya hay un documento llamado '{nombre}' en ese tema. Bórralo antes de subir otro."
        )

    # Mismo patrón que `escribir_json_atomico`: temporal en la misma carpeta,
    # fsync y rename. El prefijo con punto y el sufijo .tmp hacen además que un
    # temporal no sea nunca indexable, por si la ingesta corre justo ahora.
    descriptor, temporal = tempfile.mkstemp(dir=carpeta, prefix=f".{nombre}.", suffix=".tmp")
    escritos = 0
    try:
        with os.fdopen(descriptor, "wb") as fichero:
            for trozo in trozos:
                escritos += len(trozo)
                if escritos > MAX_BYTES_DOCUMENTO:
                    raise ErrorDeCorpus(
                        f"El documento pasa del máximo de "
                        f"{MAX_BYTES_DOCUMENTO // (1024 * 1024)} MB."
                    )
                fichero.write(trozo)
            fichero.flush()
            os.fsync(fichero.fileno())
        if escritos == 0:
            raise ErrorDeCorpus("El documento está vacío.")
        # mkstemp crea con permisos 600, correcto para un temporal pero no para
        # un fichero que lee otro contenedor.
        os.chmod(temporal, 0o644)
        os.replace(temporal, destino)
    except BaseException:
        Path(temporal).unlink(missing_ok=True)
        raise

    return _documento(corpus_dir.resolve(), destino)


def borrar_documento(corpus_dir: Path, tema: str, nombre: str) -> None:
    """Borra un documento del corpus.

    Args:
        corpus_dir: Raíz del corpus.
        tema: El tema donde está, o `TEMA_RAIZ`.
        nombre: El nombre del fichero.

    Raises:
        NombreInvalido: Si algún nombre no vale.
        ErrorDeCorpus: Si no existe o no es un documento indexable.
    """
    ruta = resolver(corpus_dir, tema, nombre)
    if not _es_documento(ruta):
        raise ErrorDeCorpus(f"'{nombre}' no es un documento del corpus.")
    ruta.unlink()
