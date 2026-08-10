"""Formularios del panel.

Los de MCP y hooks **no repiten las reglas**: construyen el modelo de pydantic
que consume el agente y dejan que sea él quien valide. Así una regla nueva en
`voice_agent_core.runtime` aparece sola en el panel, y es imposible que el panel
acepte algo que el agente rechazaría al arrancar.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime
from typing import Any

from django import forms
from django.conf import settings as django_settings
from pydantic import ValidationError

from voice_agent_core import corpus
from voice_agent_core.cron import ErrorDeCron, ExpresionCron
from voice_agent_core.runtime import (
    AccionHook,
    HookConfig,
    MCPServerConfig,
    PromptConfig,
)
from voice_agent_core.tareas import TareaProgramada as TareaProgramadaConfig
from voice_agent_core.tareas import TipoTarea
from voice_agent_panel.introspection import CampoAjuste, campos_editables, texto_a_valor
from voice_agent_panel.models import Hook, Perfil, ServidorMCP, TareaProgramada, VersionPrompt

#: Tope de longitud de una expresión regular. Un patrón con cuantificadores
#: anidados sobre una transcripción larga puede tardar una eternidad, y aquí eso
#: significa congelar el bucle de eventos que mueve el audio del agente.
MAX_LONGITUD_PATRON = 200


def _mensaje_de_pydantic(e: ValidationError) -> str:
    """Convierte los errores de pydantic en algo legible en un formulario."""
    return "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())


class PromptForm(forms.ModelForm):
    """Edición del prompt del sistema, el alma y los textos fijos."""

    muletillas_texto = forms.CharField(
        label="Muletillas",
        required=False,
        widget=forms.Textarea(attrs={"rows": 8}),
        help_text=(
            "Una frase por línea, con las categorías 'consulta:' y 'pensando:' "
            "como encabezado. Las de consulta suenan al buscar en la base de "
            "conocimiento; las de pensando, cuando el modelo tarda."
        ),
    )

    class Meta:
        model = VersionPrompt
        fields = ["mensaje", "prompt_sistema", "alma", "saludo_inicial"]
        widgets = {
            "prompt_sistema": forms.Textarea(attrs={"rows": 22}),
            "alma": forms.Textarea(attrs={"rows": 8}),
            "saludo_inicial": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Rellena el campo de texto de muletillas a partir del JSON."""
        super().__init__(*args, **kwargs)
        if not self.is_bound and self.instance and self.instance.muletillas:
            self.fields["muletillas_texto"].initial = _muletillas_a_texto(self.instance.muletillas)

    def clean_muletillas_texto(self) -> str:
        """Comprueba que el bloque de muletillas se puede interpretar."""
        texto = self.cleaned_data["muletillas_texto"]
        try:
            _texto_a_muletillas(texto)
        except ValueError as e:
            raise forms.ValidationError(str(e)) from e
        return texto

    def clean(self) -> dict[str, Any]:
        """Valida el conjunto con el mismo modelo que leerá el agente."""
        datos = super().clean()
        try:
            PromptConfig(
                prompt_sistema=datos.get("prompt_sistema", ""),
                alma=datos.get("alma", ""),
                saludo_inicial=datos.get("saludo_inicial", ""),
                muletillas=_texto_a_muletillas(datos.get("muletillas_texto", "")),
            )
        except ValidationError as e:
            raise forms.ValidationError(_mensaje_de_pydantic(e)) from e
        return datos

    @property
    def muletillas(self) -> dict[str, list[str]]:
        """Las muletillas ya convertidas a la estructura que guarda el modelo."""
        return _texto_a_muletillas(self.cleaned_data.get("muletillas_texto", ""))


def _muletillas_a_texto(muletillas: dict[str, list[str]]) -> str:
    lineas: list[str] = []
    for categoria, frases in muletillas.items():
        lineas.append(f"{categoria}:")
        lineas.extend(f"  {frase}" for frase in frases)
    return "\n".join(lineas)


def _texto_a_muletillas(texto: str) -> dict[str, list[str]]:
    """Interpreta el bloque de texto de muletillas.

    Raises:
        ValueError: Si hay frases antes de la primera categoría.
    """
    resultado: dict[str, list[str]] = {}
    categoria: str | None = None
    for linea_cruda in texto.splitlines():
        linea = linea_cruda.strip()
        if not linea:
            continue
        if linea.endswith(":"):
            categoria = linea[:-1].strip()
            resultado.setdefault(categoria, [])
            continue
        if categoria is None:
            raise ValueError(f"La frase '{linea}' no está bajo ninguna categoría.")
        resultado[categoria].append(linea)
    return resultado


class PerfilForm(forms.ModelForm):
    """Alta y edición de un perfil. El contenido se edita en sus páginas."""

    class Meta:
        model = Perfil
        fields = ["nombre", "descripcion"]


class AjustesForm(forms.Form):
    """Formulario generado a partir de los campos de `Settings`.

    Ver `introspection.py`: los campos, sus rangos y sus textos de ayuda salen
    del propio modelo de pydantic, no de una lista escrita a mano aquí.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Añade un campo por cada ajuste editable."""
        self.campos_ajuste: list[CampoAjuste] = campos_editables()
        super().__init__(*args, **kwargs)
        for ajuste in self.campos_ajuste:
            self.fields[ajuste.nombre] = ajuste.campo

    def por_secciones(self) -> list[tuple[str, list[Any]]]:
        """Agrupa los campos para pintarlos, conservando el orden."""
        agrupados: dict[str, list[Any]] = {}
        for ajuste in self.campos_ajuste:
            agrupados.setdefault(ajuste.seccion, []).append(self[ajuste.nombre])
        return list(agrupados.items())

    def valores_a_guardar(self) -> dict[str, str]:
        """Los ajustes rellenados, ya serializados a JSON.

        Un campo vacío significa "no lo fijes": no aparece en el resultado, y su
        fila se borra, de modo que vuelve a mandar el .env.

        Raises:
            forms.ValidationError: Si algún valor no encaja con su tipo.
        """
        valores: dict[str, str] = {}
        for ajuste in self.campos_ajuste:
            crudo = self.cleaned_data.get(ajuste.nombre)
            if crudo is None or crudo == "":
                continue
            try:
                valores[ajuste.nombre] = json.dumps(
                    texto_a_valor(ajuste.nombre, str(crudo)), ensure_ascii=False
                )
            except ValueError as e:
                raise forms.ValidationError(f"{ajuste.nombre}: {e}") from e
        return valores


class ServidorMCPForm(forms.ModelForm):
    """Alta y edición de un servidor MCP."""

    argumentos_texto = forms.CharField(
        label="Argumentos",
        required=False,
        help_text="Como se escribirían en una terminal; se trocean respetando las comillas.",
    )
    entorno_texto = forms.CharField(
        label="Variables de entorno",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Una por línea, CLAVE=valor. Admite ${VARIABLE}, que resuelve el agente.",
    )
    cabeceras_texto = forms.CharField(
        label="Cabeceras HTTP",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Una por línea, Cabecera: valor. Admite ${VARIABLE}.",
    )
    herramientas_texto = forms.CharField(
        label="Herramientas permitidas",
        required=False,
        help_text="Separadas por comas. Vacío significa todas las que ofrezca.",
    )

    class Meta:
        model = ServidorMCP
        fields = ["nombre", "transporte", "comando", "url", "timeout_secs"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Vuelca los campos JSON a sus equivalentes en texto."""
        super().__init__(*args, **kwargs)
        if not self.is_bound and self.instance.pk:
            self.fields["argumentos_texto"].initial = shlex.join(self.instance.argumentos or [])
            self.fields["entorno_texto"].initial = "\n".join(
                f"{k}={v}" for k, v in (self.instance.entorno or {}).items()
            )
            self.fields["cabeceras_texto"].initial = "\n".join(
                f"{k}: {v}" for k, v in (self.instance.cabeceras or {}).items()
            )
            self.fields["herramientas_texto"].initial = ", ".join(
                self.instance.herramientas_permitidas or []
            )

    def clean(self) -> dict[str, Any]:
        """Trocea los campos de texto y los valida contra `MCPServerConfig`."""
        datos = super().clean()
        try:
            argumentos = shlex.split(datos.get("argumentos_texto", ""))
        except ValueError as e:
            raise forms.ValidationError(f"Los argumentos no se pueden trocear: {e}") from e

        entorno = _pares(datos.get("entorno_texto", ""), "=")
        cabeceras = _pares(datos.get("cabeceras_texto", ""), ":")
        herramientas = [
            h.strip() for h in datos.get("herramientas_texto", "").split(",") if h.strip()
        ]

        try:
            # La validación de verdad la hace el mismo modelo que leerá el
            # agente: si aquí pasa, allí no puede fallar.
            # `habilitado` ya no es un campo del formulario: lo decide cada
            # perfil. Para validar da igual su valor.
            MCPServerConfig(
                nombre=datos.get("nombre") or "sin-nombre",
                habilitado=False,
                transporte=datos.get("transporte"),
                comando=datos.get("comando") or None,
                argumentos=argumentos,
                entorno=entorno,
                url=datos.get("url") or None,
                cabeceras=cabeceras,
                herramientas_permitidas=herramientas,
                timeout_secs=datos.get("timeout_secs") or 20.0,
            )
        except ValidationError as e:
            raise forms.ValidationError(_mensaje_de_pydantic(e)) from e

        datos["_argumentos"] = argumentos
        datos["_entorno"] = entorno
        datos["_cabeceras"] = cabeceras
        datos["_herramientas"] = herramientas
        return datos

    def save(self, commit: bool = True) -> ServidorMCP:
        """Guarda, volcando los campos de texto a sus columnas JSON."""
        servidor = super().save(commit=False)
        servidor.argumentos = self.cleaned_data["_argumentos"]
        servidor.entorno = self.cleaned_data["_entorno"]
        servidor.cabeceras = self.cleaned_data["_cabeceras"]
        servidor.herramientas_permitidas = self.cleaned_data["_herramientas"]
        if commit:
            servidor.save()
        return servidor


def _pares(texto: str, separador: str) -> dict[str, str]:
    """Interpreta un bloque de `clave<sep>valor`, una por línea."""
    resultado: dict[str, str] = {}
    for linea_cruda in texto.splitlines():
        linea = linea_cruda.strip()
        if not linea:
            continue
        clave, _, valor = linea.partition(separador)
        if clave.strip():
            resultado[clave.strip()] = valor.strip()
    return resultado


class HookForm(forms.ModelForm):
    """Alta y edición de un hook."""

    comando_texto = forms.CharField(
        label="Comando",
        required=False,
        help_text=(
            "Como se escribiría en una terminal. NO pasa por una shell: sin "
            "comodines, sin tuberías. El contexto del evento llega por la "
            "entrada estándar en JSON."
        ),
    )
    entorno_texto = forms.CharField(
        label="Variables de entorno",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Una por línea, CLAVE=valor.",
    )

    class Meta:
        model = Hook
        fields = [
            "nombre",
            "orden",
            "evento",
            "accion",
            "timeout_secs",
            "bloqueante",
            "patron",
            "reemplazo",
        ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Vuelca el comando y el entorno a texto, y avisa de los límites."""
        super().__init__(*args, **kwargs)
        if not self.is_bound and self.instance.pk:
            self.fields["comando_texto"].initial = shlex.join(self.instance.comando or [])
            self.fields["entorno_texto"].initial = "\n".join(
                f"{k}={v}" for k, v in (self.instance.entorno or {}).items()
            )
        self.fields[
            "reemplazo"
        ].help_text = "Solo para reescribir. Admite \\1, \\2... para los grupos del patrón."
        self.fields["bloqueante"].help_text = (
            "Si el turno espera al comando. Actívalo solo si de verdad hace "
            "falta: su duración se suma a CADA turno de la conversación."
        )

    def clean(self) -> dict[str, Any]:
        """Trocea el comando y valida el conjunto contra `HookConfig`."""
        datos = super().clean()
        try:
            comando = shlex.split(datos.get("comando_texto", ""))
        except ValueError as e:
            raise forms.ValidationError(f"El comando no se puede trocear: {e}") from e

        accion = datos.get("accion")
        if accion == AccionHook.EJECUTAR_COMANDO and not django_settings.HOOKS_COMANDO_PERMITIDOS:
            raise forms.ValidationError(
                "Los hooks que ejecutan comandos están desactivados en esta "
                "instalación (PANEL_HOOKS_COMANDO=0)."
            )

        patron = datos.get("patron", "")
        if patron:
            if len(patron) > MAX_LONGITUD_PATRON:
                raise forms.ValidationError(
                    f"El patrón no puede pasar de {MAX_LONGITUD_PATRON} caracteres: uno muy "
                    "complejo puede bloquear el bucle de audio del agente."
                )
            try:
                re.compile(patron)
            except re.error as e:
                raise forms.ValidationError(
                    f"El patrón no es una expresión regular válida: {e}"
                ) from e

        entorno = _pares(datos.get("entorno_texto", ""), "=")
        try:
            HookConfig(
                nombre=datos.get("nombre") or "sin-nombre",
                habilitado=False,
                orden=datos.get("orden") or 100,
                evento=datos.get("evento"),
                accion=accion,
                comando=comando,
                entorno=entorno,
                timeout_secs=datos.get("timeout_secs") or 5.0,
                bloqueante=datos.get("bloqueante", False),
                patron=patron,
                reemplazo=datos.get("reemplazo", ""),
            )
        except ValidationError as e:
            raise forms.ValidationError(_mensaje_de_pydantic(e)) from e

        datos["_comando"] = comando
        datos["_entorno"] = entorno
        return datos

    def save(self, commit: bool = True) -> Hook:
        """Guarda, volcando comando y entorno a sus columnas JSON."""
        hook = super().save(commit=False)
        hook.comando = self.cleaned_data["_comando"]
        hook.entorno = self.cleaned_data["_entorno"]
        if commit:
            hook.save()
        return hook


class TareaForm(forms.ModelForm):
    """Alta y edición de una tarea programada.

    Como los de MCP y hooks, no repite las reglas: valida construyendo el
    modelo de `voice_agent_core.tareas` que consumirá el agente. La expresión
    cron se valida aparte para poder enseñar las próximas ejecuciones en la
    plantilla (`self.proximas`).
    """

    class Meta:
        model = TareaProgramada
        fields = [
            "nombre",
            "titulo",
            "tipo",
            "cron",
            "mision",
            "guardar_respuestas",
            "contacto_nombre",
            "contacto_numero",
        ]
        widgets = {"mision": forms.Textarea(attrs={"rows": 6})}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Prepara los textos de ayuda y el hueco para la vista previa."""
        super().__init__(*args, **kwargs)
        self.proximas: list[Any] = []
        self.fields["cron"].help_text = (
            "Cinco campos: minuto hora día-del-mes mes día-de-la-semana. "
            "Ej.: '0 8 * * 1-5' = laborables a las 8:00; '*/30 9-21 * * *' = "
            "cada media hora de 9 a 21. Domingo es 0 o 7. Hora local de la placa."
        )
        self.fields["mision"].help_text = (
            "El encargo, redactado para el modelo y en segunda persona: "
            "'Recuérdale a Nora la pastilla y pregúntale cómo durmió'."
        )
        self.fields["guardar_respuestas"].help_text = (
            "Para cuestionarios: la misión pedirá al modelo que guarde lo "
            "respondido, y saldrá en la página de resultados de la tarea."
        )
        self.fields["contacto_numero"].help_text = (
            "Se marca tal cual a la hora del disparo. Usa el buscador para "
            "copiarlo de la agenda del móvil, o tecléalo con prefijo."
        )

    def clean_cron(self) -> str:
        """Valida la expresión y calcula la vista previa de disparos."""
        texto = self.cleaned_data["cron"].strip()
        try:
            expresion = ExpresionCron.parse(texto)
            # También caza las bien formadas pero imposibles (31 de febrero).
            self.proximas = expresion.proximas(datetime.now(), 3)
        except ErrorDeCron as e:
            raise forms.ValidationError(str(e)) from e
        return texto

    def clean(self) -> dict[str, Any]:
        """Valida el conjunto contra el modelo que consumirá el agente."""
        datos = super().clean()
        if not datos.get("cron"):
            return datos  # clean_cron ya puso su error
        try:
            TareaProgramadaConfig(
                id=datos.get("nombre") or "sin-nombre",
                nombre=datos.get("titulo", ""),
                habilitada=False,
                tipo=TipoTarea(datos.get("tipo") or TipoTarea.SALA),
                cron=datos["cron"],
                mision=datos.get("mision", ""),
                guardar_respuestas=datos.get("guardar_respuestas", False),
                contacto_nombre=datos.get("contacto_nombre", ""),
                contacto_numero=datos.get("contacto_numero", ""),
            )
        except ValidationError as e:
            raise forms.ValidationError(_mensaje_de_pydantic(e)) from e
        return datos


class TemaForm(forms.Form):
    """Alta de un tema de la base de conocimiento.

    El nombre se slugifica —lo que se crea es una carpeta dentro de `corpus/`,
    y además acaba siendo el nombre de una colección de ChromaDB—, así que
    "Guía de la Placa" se guarda como `guia-de-la-placa`. La vista enseña el
    nombre resultante para que no haya sorpresas.
    """

    nombre = forms.CharField(
        label="Nombre del tema",
        max_length=200,
        help_text=(
            "Se convierte en una carpeta de corpus/: sin tildes, en minúsculas y "
            "con guiones. Los documentos se suben dentro."
        ),
    )

    def clean_nombre(self) -> str:
        """Slugifica el nombre y comprueba que queda algo utilizable."""
        try:
            return corpus.normalizar_tema(self.cleaned_data["nombre"])
        except corpus.NombreInvalido as e:
            raise forms.ValidationError(str(e)) from e


class DocumentoForm(forms.Form):
    """Subida de un documento a un tema, o a la raíz del corpus.

    Las reglas —qué extensiones valen, qué nombres se aceptan— **no se repiten
    aquí**: son las de `voice_agent_core.corpus`, que es también quien las
    aplica al escribir en disco y quien decide qué indexa el agente. Duplicarlas
    llevaría a aceptar en el navegador algo que la ingesta ignoraría en silencio.
    """

    tema = forms.CharField(required=False, widget=forms.HiddenInput)
    archivo = forms.FileField(
        label="Documento",
        help_text=(
            f"Se admiten {', '.join(sorted(corpus.EXTENSIONES_SOPORTADAS))}, "
            f"hasta {corpus.MAX_BYTES_DOCUMENTO // (1024 * 1024)} MB."
        ),
    )

    def clean_tema(self) -> str:
        """Comprueba el tema de destino sin transformarlo.

        Aquí no se slugifica: el tema tiene que existir ya, y buscar por un
        nombre "arreglado" dejaría de encontrar carpetas creadas con otras
        reglas. La cadena vacía es válida y significa la raíz del corpus.
        """
        tema = self.cleaned_data.get("tema", "")
        if not tema:
            return corpus.TEMA_RAIZ
        try:
            return corpus.validar_componente(tema, maximo=corpus.MAX_LONGITUD_TEMA)
        except corpus.NombreInvalido as e:
            raise forms.ValidationError(str(e)) from e

    def clean_archivo(self) -> Any:
        """Rechaza extensiones no indexables y ficheros demasiado grandes.

        El tamaño se comprueba también al volcar el fichero, y no sobra:
        `DATA_UPLOAD_MAX_MEMORY_SIZE` de Django **no** limita los ficheros
        subidos, y `size` viene de la cabecera, que es de quien sube.
        """
        archivo = self.cleaned_data["archivo"]
        try:
            corpus.normalizar_documento(archivo.name)
        except corpus.NombreInvalido as e:
            raise forms.ValidationError(str(e)) from e

        if archivo.size > corpus.MAX_BYTES_DOCUMENTO:
            tope = corpus.MAX_BYTES_DOCUMENTO // (1024 * 1024)
            raise forms.ValidationError(f"El documento pasa del máximo de {tope} MB.")
        return archivo
