"""Normalización de nombres y números, y puntuación de parecidos.

Todo lo de este módulo son **funciones puras**: entra texto, sale texto o un
número. No toca D-Bus, ni disco, ni red. Eso lo hace la parte del proyecto más
fácil de probar y, no por casualidad, la que más valor aporta por línea.

## Por qué hace falta algo más que comparar cadenas

El nombre de un contacto no llega escrito: llega **transcrito de la voz**. Con
Whisper `tiny`, que es lo que corre en esta placa, los nombres propios son el
peor caso posible — son justo las palabras que un modelo de lenguaje pequeño no
puede adivinar por contexto. "Bárbara" sale "Varvara", "Jiménez" sale "Giménez",
"Llanos" sale "Yanos".

Contra eso hay tres capas, de más fiable a menos:

1. **Normalizar**: quitar tildes, mayúsculas y ruido. Arregla los desacuerdos
   ortográficos, que son la mayoría.
2. **Comparar por trozos**: "ana pe" tiene que encontrar a "Ana Pérez", y
   "pérez" a secas también, porque la gente busca por apellido.
3. **Clave fonética**: plegar los sonidos que el español escribe de varias
   maneras. Es lo que rescata los errores de transcripción de verdad.

## La clave fonética, y por qué no es Soundex

Soundex está diseñado para el inglés y agrupa mal en español. Aquí se pliegan
las confusiones reales del castellano hablado, que son pocas y conocidas:

| Se pliega | Porque suenan igual |
|---|---|
| `v` → `b` | "Varvara" / "Bárbara" |
| `z`, `ce`, `ci` → `s` | seseo: "Sánchez" / "Sanches" |
| `ll`, `y` → `y` | yeísmo: "Llanos" / "Yanos" |
| `ge`, `gi`, `j` → `j` | "Jiménez" / "Giménez" |
| `qu`, `k` → `k` | "Quique" / "Kike" |
| `h` desaparece | es muda: "Hernán" / "Ernán" |
| `x` → `s` | "Ximena" / "Simena" |
| dobles → simple | "Anna" / "Ana" |

El orden de las reglas importa y por eso están numeradas abajo: si se plegara
`c`→`k` antes de tratar `ce`/`ci`, "Cecilia" acabaría en `kekilia` y dejaría de
parecerse a "Sesilia".
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

#: Todo lo que no sea letra o dígito separa palabras.
_NO_ALFANUMERICO = re.compile(r"[^0-9a-zñ]+")

#: La marca combinante que, sobre una `n`, forma la eñe. Se escribe con el
#: escape y no con el carácter porque es invisible en un editor: pegado tal
#: cual parecería un error tipográfico y alguien lo "arreglaría".
TILDE_DE_LA_EÑE = "̃"

#: Marcador interno para apartar la `ch` mientras se pliegan las demás letras.
#: Sin él, la regla `c -> k` convertiría "Chávez" en "Khávez". Se restituye al
#: final. Es un carácter de control, así que no puede aparecer en un nombre.
_CH = "\x01"

#: Marcador para la `g` dura de "gue"/"gui", que ha de sobrevivir a la regla
#: que convierte `g(e,i)` en jota. Ver el paso 4 de `clave_fonetica`.
_G_DURA = "\x02"

#: Lo que se conserva de un número de teléfono. El `+` solo vale al principio.
_BASURA_EN_NUMERO = re.compile(r"[^0-9+]")

#: Prefijos de marcación internacional que la gente dice de viva voz. Se
#: normalizan a `+` para que "cero cero cincuenta y siete" y "+57" sean el mismo
#: número al compararlos con la agenda.
_PREFIJOS_INTERNACIONALES = ("00", "011")


def normalizar_nombre(texto: str) -> str:
    """Reduce un nombre a su forma comparable: sin tildes, sin ruido, en minúsculas.

    La eñe se conserva a propósito: en español distingue palabras ("caña" no es
    "cana") y ningún sistema de transcripción la confunde con otra letra.

    Args:
        texto: El nombre tal cual, como venga.

    Returns:
        El nombre normalizado, con las palabras separadas por un solo espacio.
        Cadena vacía si no quedaba nada aprovechable.
    """
    # NFD separa la letra de su tilde; descartando las marcas combinantes se
    # quitan las tildes sin tocar la ñ, que se recompone después con NFC.
    descompuesto = unicodedata.normalize("NFD", texto.casefold())
    sin_tildes = "".join(
        c
        for c in descompuesto
        # La tilde de la ñ se conserva; las demás marcas se tiran. Se escribe
        # con el escape y no con el carácter literal porque U+0303 es
        # invisible en un editor: pegado tal cual, parece un error.
        if unicodedata.category(c) != "Mn" or c == TILDE_DE_LA_EÑE
    )
    recompuesto = unicodedata.normalize("NFC", sin_tildes)
    limpio = _NO_ALFANUMERICO.sub(" ", recompuesto)
    return " ".join(limpio.split())


def tokens(texto: str) -> list[str]:
    """Parte un nombre ya normalizado en palabras."""
    return normalizar_nombre(texto).split()


def clave_fonetica(texto: str) -> str:
    """Pliega un nombre a cómo suena en español.

    Sirve para que "Varvara" encuentre a "Bárbara": los errores de
    transcripción del español son casi siempre confusiones entre letras que
    suenan igual, y plegarlas las hace desaparecer.

    Args:
        texto: Un nombre, con o sin normalizar.

    Returns:
        La clave fonética. Dos nombres que suenan igual dan la misma clave.
    """
    t = normalizar_nombre(texto)

    # 1. Apartar la `ch`. Es un sonido propio y tiene que sobrevivir intacta a
    #    la regla `c -> k` del paso 5; se restituye al final.
    t = t.replace("ch", _CH)

    # 2. La hache suelta es muda.
    t = t.replace("h", "")

    # 3. Seseo. Va antes que cualquier regla sobre la `c` suelta: plegar
    #    `c -> k` primero convertiría "Cecilia" en "kekilia" y dejaría de
    #    parecerse a "Sesilia".
    t = re.sub(r"c([ei])", r"s\1", t)
    t = t.replace("z", "s")

    # 4. Las dos ges. "gue"/"gui" suenan con g dura y la u es muda; "ge"/"gi"
    #    suenan como jota. No basta con ordenar las reglas: si `gu(e,i)` se
    #    plegara a `g(e,i)` sin más, la regla siguiente convertiría la g dura
    #    recién obtenida en jota y "Guillermo" acabaría en "jillermo". Por eso
    #    la g dura se aparta con un marcador hasta que la otra regla ha pasado.
    t = re.sub(r"gu([ei])", rf"{_G_DURA}\1", t)
    t = re.sub(r"g([ei])", r"j\1", t)
    t = t.replace(_G_DURA, "g")

    # 5. La ka. `qu` pierde la u muda; la `c` que quede ya solo suena a k.
    t = t.replace("qu", "k")
    t = t.replace("q", "k")
    t = t.replace("c", "k")

    # 6. Yeísmo.
    t = t.replace("ll", "y")

    # 7. B y V son el mismo sonido en español.
    t = t.replace("v", "b")

    # 8. La equis tiende a s en el habla corriente.
    t = t.replace("x", "s")

    # 9. Letras repetidas: ninguna suena doble en español salvo la rr, que a
    #    estos efectos da igual.
    t = re.sub(r"(.)\1+", r"\1", t)

    # 10. Devolver la `ch` a su sitio.
    return t.replace(_CH, "ch")


def normalizar_numero(numero: str) -> str:
    """Deja un número de teléfono en una forma comparable.

    No pretende ser E.164 de verdad —eso exige saber el país y una tabla de
    prefijos—, solo que dos escrituras del mismo número coincidan: "300 123 45
    67", "+57 300 1234567" y "0057-300-1234567" tienen que poder compararse.

    Args:
        numero: El número como venga, con espacios, guiones o paréntesis.

    Returns:
        El número solo con dígitos y, como mucho, un `+` inicial.
    """
    limpio = _BASURA_EN_NUMERO.sub("", numero.strip())
    if not limpio:
        return ""

    # Un `+` en medio no significa nada; solo cuenta el del principio.
    mas = limpio.startswith("+")
    digitos = limpio.replace("+", "")

    if not mas:
        for prefijo in _PREFIJOS_INTERNACIONALES:
            if digitos.startswith(prefijo) and len(digitos) > len(prefijo) + 6:
                return "+" + digitos[len(prefijo) :]

    return ("+" if mas else "") + digitos


def mismo_numero(a: str, b: str) -> bool:
    """Indica si dos números son el mismo, tolerando que falte el prefijo del país.

    La agenda del móvil guarda unos contactos con prefijo y otros sin él, y la
    red entrega el número entrante casi siempre con prefijo. Comparar en crudo
    haría que la mitad de las llamadas no se resolvieran contra la agenda, que
    es justo lo que hace falta para poder decir "te llama Ana".

    Se comparan los últimos siete dígitos, que es el número local más corto que
    existe en los países que nos interesan. Menos que eso empezaría a dar falsos
    positivos.
    """
    na, nb = normalizar_numero(a), normalizar_numero(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    da, db = na.lstrip("+"), nb.lstrip("+")
    if len(da) < 7 or len(db) < 7:
        return da == db
    return da[-7:] == db[-7:]


def puntuar(consulta: str, nombre: str) -> int:
    """Mide cómo de bien encaja lo que han pedido con el nombre de un contacto.

    La escalera va de más fiable a menos, y devuelve en cuanto una regla acierta
    para que una coincidencia buena no quede rebajada por una peor.

    Args:
        consulta: Lo que ha dicho la persona, ya transcrito.
        nombre: El nombre completo del contacto en la agenda.

    Returns:
        De 0 (nada que ver) a 100 (idéntico).
    """
    c = normalizar_nombre(consulta)
    n = normalizar_nombre(nombre)
    if not c or not n:
        return 0

    if c == n:
        return 100

    tc, tn = c.split(), n.split()

    # Todos los trozos de la consulta son principio de las palabras del nombre,
    # en orden: "ana pe" -> "Ana Pérez". Es como busca la gente de viva voz.
    if len(tc) <= len(tn) and all(tn[i].startswith(t) for i, t in enumerate(tc)):
        return 90

    # Todas las palabras están, aunque en otro orden: "pérez ana".
    if all(any(p.startswith(t) for p in tn) for t in tc):
        return 80

    # Parecido general. Cubre las erratas de una o dos letras.
    ratio = SequenceMatcher(None, c, n).ratio()
    if ratio >= 0.80:
        return int(ratio * 78)

    # Suena igual aunque se escriba distinto. Esta es la que salva las
    # transcripciones malas de nombres propios.
    if clave_fonetica(c) == clave_fonetica(n):
        return 60
    # Y palabra a palabra, para "Varvara Pérez" contra "Bárbara Pérez".
    if tc and all(any(clave_fonetica(p) == clave_fonetica(t) for p in tn) for t in tc):
        return 58

    # Lo que ha dicho aparece dentro del nombre.
    if c in n:
        return 55

    return int(ratio * 50)


__all__ = [
    "clave_fonetica",
    "mismo_numero",
    "normalizar_nombre",
    "normalizar_numero",
    "puntuar",
    "tokens",
]
