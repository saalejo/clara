"""Lo que el panel guarda.

Nada de esto lo lee el agente directamente: la base de datos es del panel, y lo
que cruza al agente es la exportación a JSON (ver `exporter.py`). Esa separación
es lo que permite cambiar el esquema aquí sin tocar el contrato con el agente.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models, transaction

from voice_agent_core.runtime import AccionHook, EventoHook, TransporteMCP
from voice_agent_core.tareas import TipoTarea


class Perfil(models.Model):
    """Un juego completo de configuración del agente.

    Cada perfil tiene su propio historial de prompt, sus ajustes y sus
    interruptores de herramientas; los servidores MCP y los hooks son un
    catálogo compartido del que cada perfil elige cuáles usa (pertenecer al
    M2M significa "habilitado en este perfil").

    Solo uno está activo, y activar no despliega: los cambios llegan al agente
    por el flujo de exportar y reiniciar de siempre.
    """

    nombre = models.CharField(max_length=100, unique=True)
    descripcion = models.CharField(max_length=200, blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)
    activo = models.BooleanField(default=False)

    mcp_habilitados = models.ManyToManyField("ServidorMCP", blank=True, related_name="perfiles")
    hooks_habilitados = models.ManyToManyField("Hook", blank=True, related_name="perfiles")

    class Meta:
        ordering = ["nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["activo"],
                condition=models.Q(activo=True),
                name="un_solo_perfil_activo",
            )
        ]

    def __str__(self) -> str:
        marca = " (activo)" if self.activo else ""
        return f"{self.nombre}{marca}"

    @classmethod
    def activo_o_none(cls) -> Perfil | None:
        """El perfil en vigor, si hay alguno."""
        return cls.objects.filter(activo=True).first()

    @classmethod
    def predeterminado(cls) -> Perfil:
        """El activo, o el primero, o uno recién creado. Nunca falla.

        La misma filosofía que `cargar_runtime`: una base de datos sin
        perfiles no debe dejar el panel inutilizable.
        """
        perfil = cls.activo_o_none() or cls.objects.first()
        if perfil is None:
            perfil, _ = cls.objects.get_or_create(nombre="Por defecto", defaults={"activo": True})
        return perfil

    def activar(self) -> None:
        """Pone este perfil en vigor, desactivando el anterior.

        En una transacción porque la restricción de unicidad parcial no admite
        dos activos ni un instante.
        """
        with transaction.atomic():
            Perfil.objects.filter(activo=True).exclude(pk=self.pk).update(activo=False)
            self.activo = True
            self.save(update_fields=["activo"])

    def duplicar(self, nombre: str) -> Perfil:
        """Crea una copia completa de este perfil, sin activarla."""
        with transaction.atomic():
            copia = Perfil.objects.create(nombre=nombre, descripcion=self.descripcion, activo=False)
            version = VersionPrompt.activa_de(self)
            if version is not None:
                version.duplicar(None, f"Copia del perfil '{self.nombre}'", perfil=copia).activar()
            for ajuste in self.ajustes.all():
                AjusteAgente.objects.create(
                    perfil=copia,
                    clave=ajuste.clave,
                    valor=ajuste.valor,
                    actualizado_por=ajuste.actualizado_por,
                )
            for herramienta in self.herramientas.all():
                Herramienta.objects.create(
                    perfil=copia, nombre=herramienta.nombre, habilitada=herramienta.habilitada
                )
            copia.mcp_habilitados.set(self.mcp_habilitados.all())
            copia.hooks_habilitados.set(self.hooks_habilitados.all())
        return copia


class VersionPrompt(models.Model):
    """Una versión del prompt del sistema, con su alma y sus muletillas.

    El historial es **inmutable**: volver a una versión anterior no la reactiva,
    crea una copia nueva. Así la secuencia de lo que estuvo puesto en cada
    momento nunca se reescribe, que es justo lo que uno quiere consultar cuando
    el agente empieza a comportarse raro.
    """

    perfil = models.ForeignKey(Perfil, on_delete=models.CASCADE, related_name="versiones")
    creado_en = models.DateTimeField(auto_now_add=True)
    autor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    mensaje = models.CharField(max_length=200, blank=True, help_text="Qué cambiaste y por qué.")

    prompt_sistema = models.TextField()
    alma = models.TextField(
        blank=True,
        help_text="Personalidad y tono. Se añade al final del prompt del sistema.",
    )
    saludo_inicial = models.TextField()
    muletillas = models.JSONField(default=dict)

    activa = models.BooleanField(default=False)

    class Meta:
        ordering = ["-creado_en"]
        constraints = [
            models.UniqueConstraint(
                fields=["perfil"],
                condition=models.Q(activa=True),
                name="una_sola_version_activa_por_perfil",
            )
        ]

    def __str__(self) -> str:
        marca = " (activa)" if self.activa else ""
        return f"{self.creado_en:%Y-%m-%d %H:%M} {self.mensaje}{marca}"

    @classmethod
    def activa_de(cls, perfil: Perfil) -> VersionPrompt | None:
        """La versión en vigor de un perfil, si hay alguna."""
        return cls.objects.filter(perfil=perfil, activa=True).first()

    def activar(self) -> None:
        """Pone esta versión en vigor en su perfil, desactivando la anterior.

        En una transacción porque la restricción de unicidad parcial no admite
        dos activas ni un instante.
        """
        with transaction.atomic():
            VersionPrompt.objects.filter(perfil=self.perfil, activa=True).exclude(
                pk=self.pk
            ).update(activa=False)
            self.activa = True
            self.save(update_fields=["activa"])

    def duplicar(
        self, autor: object | None, mensaje: str, perfil: Perfil | None = None
    ) -> VersionPrompt:
        """Crea una copia de esta versión, que es como se vuelve atrás.

        Con `perfil` la copia aterriza en otro perfil; es lo que usa
        `Perfil.duplicar`.
        """
        return VersionPrompt.objects.create(
            perfil=perfil or self.perfil,
            autor=autor if autor and getattr(autor, "is_authenticated", False) else None,
            mensaje=mensaje,
            prompt_sistema=self.prompt_sistema,
            alma=self.alma,
            saludo_inicial=self.saludo_inicial,
            muletillas=self.muletillas,
        )


class AjusteAgente(models.Model):
    """Un campo de `Settings` sobreescrito desde el panel.

    Una fila por campo, y no una tabla ancha con una columna por ajuste. La
    diferencia importa: `Settings` gana campos con el tiempo, y una tabla ancha
    obligaría a una migración cada vez. Así el formulario se genera por
    introspección del modelo de pydantic y el esquema de la base de datos no
    tiene que seguirle el ritmo.

    Que no exista fila significa "usa lo que diga el .env o el valor por
    defecto", que no es lo mismo que guardar ese valor: si mañana cambia el
    .env, el ajuste sin fila lo sigue.
    """

    perfil = models.ForeignKey(Perfil, on_delete=models.CASCADE, related_name="ajustes")
    clave = models.CharField(max_length=100)
    valor = models.TextField(help_text="El valor serializado como JSON.")
    actualizado_en = models.DateTimeField(auto_now=True)
    actualizado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["clave"]
        constraints = [
            models.UniqueConstraint(fields=["perfil", "clave"], name="ajuste_unico_por_perfil")
        ]

    def __str__(self) -> str:
        return f"{self.clave} = {self.valor}"


class Herramienta(models.Model):
    """Si el modelo llega a ver una herramienta.

    Solo guarda el interruptor, y por perfil: la misma herramienta puede estar
    encendida en un perfil y apagada en otro. El nombre, la descripción y el
    esquema JSON se leen de `estado_arranque.json`, que publica el agente: es
    él quien sabe qué tiene cargado de verdad, incluidas las que vengan de un
    servidor MCP. Que no exista fila significa "habilitada".
    """

    perfil = models.ForeignKey(Perfil, on_delete=models.CASCADE, related_name="herramientas")
    nombre = models.CharField(max_length=100)
    habilitada = models.BooleanField(default=True)

    class Meta:
        ordering = ["nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["perfil", "nombre"], name="herramienta_unica_por_perfil"
            )
        ]

    def __str__(self) -> str:
        return f"{self.nombre} ({'activa' if self.habilitada else 'desactivada'})"


class ServidorMCP(models.Model):
    """Un servidor MCP cuyas herramientas se le ofrecen al modelo.

    La definición es global; si está habilitado o no lo decide cada perfil con
    `Perfil.mcp_habilitados`. Un servidor recién creado no pertenece a ningún
    perfil, así que nace apagado.
    """

    TRANSPORTES = [(t.value, t.value) for t in TransporteMCP]

    nombre = models.SlugField(max_length=60, unique=True)
    transporte = models.CharField(max_length=10, choices=TRANSPORTES, default=TransporteMCP.STDIO)

    comando = models.CharField(max_length=500, blank=True)
    argumentos = models.JSONField(default=list, blank=True)
    entorno = models.JSONField(
        default=dict,
        blank=True,
        help_text="Admite ${VARIABLE}: la resuelve el agente, así el panel no ve el secreto.",
    )

    url = models.URLField(max_length=500, blank=True)
    cabeceras = models.JSONField(default=dict, blank=True)

    herramientas_permitidas = models.JSONField(
        default=list, blank=True, help_text="Vacío significa todas las que ofrezca."
    )
    timeout_secs = models.FloatField(default=20.0)

    ultimo_sondeo = models.DateTimeField(null=True, blank=True)
    ultimo_error = models.TextField(blank=True)
    herramientas_descubiertas = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["nombre"]

    def __str__(self) -> str:
        return f"{self.nombre} ({self.transporte})"


class Hook(models.Model):
    """Una regla enganchada a un punto de la conversación.

    La definición es global; si está activo o no lo decide cada perfil con
    `Perfil.hooks_habilitados`. Un hook recién escrito no pertenece a ningún
    perfil, así que sigue naciendo apagado: no debe estrenarse solo en mitad
    de una conversación.
    """

    EVENTOS = [(e.value, e.value) for e in EventoHook]
    ACCIONES = [(a.value, a.value) for a in AccionHook]

    nombre = models.SlugField(max_length=60, unique=True)
    orden = models.IntegerField(default=100)

    evento = models.CharField(max_length=40, choices=EVENTOS)
    accion = models.CharField(max_length=30, choices=ACCIONES)

    comando = models.JSONField(default=list, blank=True)
    entorno = models.JSONField(default=dict, blank=True)
    timeout_secs = models.FloatField(default=5.0)
    bloqueante = models.BooleanField(default=False)

    patron = models.CharField(max_length=500, blank=True)
    reemplazo = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["orden", "nombre"]

    def __str__(self) -> str:
        return f"{self.nombre}: {self.accion} en {self.evento}"


class TareaProgramada(models.Model):
    """Una misión con horario: un prompt que el agente ejecuta solo.

    Como los servidores MCP, la definición es global y no depende del perfil:
    son encargos de la instalación ("recuérdale la pastilla a las ocho"), no
    del carácter del agente. A diferencia de ellos lleva su interruptor plano,
    y nace apagada: una misión no debe estrenarse sola a la hora en punto sin
    que nadie la haya repasado.

    El número de teléfono se **congela al guardar**: se busca en la agenda del
    móvil desde el formulario y se copia aquí, porque a la hora del disparo no
    hay nadie delante con quien desambiguar un "llama a Luis".
    """

    TIPOS = [(t.value, t.value) for t in TipoTarea]

    nombre = models.SlugField(
        max_length=60,
        unique=True,
        help_text="Identificador estable; nombra la carpeta de resultados.",
    )
    titulo = models.CharField(max_length=120, blank=True)
    habilitada = models.BooleanField(default=False)
    tipo = models.CharField(max_length=10, choices=TIPOS, default=TipoTarea.SALA)
    cron = models.CharField(max_length=100)
    mision = models.TextField()
    guardar_respuestas = models.BooleanField(default=False)

    contacto_nombre = models.CharField(max_length=120, blank=True)
    contacto_numero = models.CharField(max_length=40, blank=True)
    #: De qué se operó el paciente. Es el dato que arma la puerta de cobertura
    #: **antes del primer turno**: con él, el agente consulta solo los
    #: protocolos de esa cirugía, y si el corpus no la cubre no consulta
    #: ninguno en vez de contestar con los de otra. En blanco deja que sea el
    #: agente quien lo pregunte durante la llamada.
    procedimiento = models.CharField(max_length=120, blank=True)

    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["nombre"]

    def __str__(self) -> str:
        return f"{self.nombre} ({self.cron})"


class Reindexado(models.Model):
    """Bitácora de cada reindexación lanzada desde el panel.

    Existe para poder contestar a una sola pregunta: **¿lo que hay en el corpus
    está ya en el índice?** El panel escribe ficheros y otro contenedor los
    indexa, así que hace falta guardar en qué estado estaba el corpus la última
    vez que se pidió indexarlo.

    Lo que se guarda es `marca_indexada`, la fecha del último cambio del corpus
    tomada **justo antes** de lanzar la ingesta, y no `creado_en`. La diferencia
    importa por dos motivos: las fechas de los ficheros y el reloj del panel no
    tienen por qué compartir referencia, y así el resultado no depende de si la
    llamada a systemd espera a que la ingesta termine o vuelve enseguida. Un
    documento subido *durante* la reindexación queda con fecha posterior y el
    panel dirá que falta reindexar: un falso positivo, que es el lado seguro.
    """

    class Resultado(models.TextChoices):
        LANZADO = "lanzado", "Lanzado"
        ERROR = "error", "No se pudo lanzar"

    creado_en = models.DateTimeField(auto_now_add=True)
    autor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    # "Lanzado" y no "hecho": el panel encola el trabajo en systemd y no es
    # quien para afirmar que terminó bien. Cómo acabó se lee en vivo del estado
    # de la unidad.
    resultado = models.CharField(max_length=20, choices=Resultado.choices)
    detalle = models.TextField(blank=True)
    marca_indexada = models.FloatField(
        default=0.0,
        help_text="Fecha del último cambio del corpus cuando se lanzó la reindexación.",
    )

    class Meta:
        ordering = ["-creado_en"]

    def __str__(self) -> str:
        return f"{self.creado_en:%Y-%m-%d %H:%M} {self.resultado}"


class LanzamientoCalidad(models.Model):
    """Bitácora de cada lote de pruebas de calidad lanzado desde el panel.

    Es el gemelo de `Reindexado` para la sección Calidad: "Lanzado" y no
    "hecho", porque el panel encola el trabajo en systemd y no es quien afirma
    que terminó. Los resultados de cada ensayo son ficheros que el runner deja en
    `data/calidad/resultados/`, no filas de aquí.
    """

    class Resultado(models.TextChoices):
        LANZADO = "lanzado", "Lanzado"
        ERROR = "error", "No se pudo lanzar"

    creado_en = models.DateTimeField(auto_now_add=True)
    autor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    resultado = models.CharField(max_length=20, choices=Resultado.choices)
    detalle = models.TextField(blank=True)
    # Los ids de escenario que componían el lote, para poder mirar atrás qué se
    # pidió ejecutar aunque el catálogo cambie después.
    escenarios = models.JSONField(default=list)

    class Meta:
        ordering = ["-creado_en"]

    def __str__(self) -> str:
        return f"{self.creado_en:%Y-%m-%d %H:%M} {self.resultado}"


class RevisionCalidad(models.Model):
    """La opinión manual del panel sobre una ejecución de calidad.

    El veredicto del juez vive en el fichero de la ejecución (verdad del runner)
    y no se toca. Cuando el juez se equivoca, el panel puede superponer aquí un
    veredicto propio, atado a la ejecución por su id. Es una superposición: si se
    borra, vuelve a mandar el juez.
    """

    class Veredicto(models.TextChoices):
        APROBADO = "aprobado", "Aprobado"
        FALLO = "fallo", "Fallo"

    id_ejecucion = models.CharField(max_length=120, unique=True)
    veredicto = models.CharField(max_length=20, choices=Veredicto.choices)
    nota = models.TextField(blank=True)
    autor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-actualizado_en"]

    def __str__(self) -> str:
        return f"{self.id_ejecucion}: {self.veredicto}"


class Despliegue(models.Model):
    """Bitácora de cada `guardar -> exportar -> reiniciar`.

    Guarda las dos instantáneas **tal y como se enviaron**, que es lo que
    contesta la pregunta que siempre acaba apareciendo: "cambié algo y no pasó
    nada". Con esto se puede comparar lo que hay en la base de datos con lo que
    de verdad se le mandó al agente.
    """

    class Resultado(models.TextChoices):
        EXPORTADO = "exportado", "Exportado"
        REINICIADO = "reiniciado", "Exportado y reiniciado"
        ERROR = "error", "Error"

    creado_en = models.DateTimeField(auto_now_add=True)
    autor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    resultado = models.CharField(max_length=20, choices=Resultado.choices)
    detalle = models.TextField(blank=True)

    instantanea_settings = models.JSONField(default=dict)
    instantanea_runtime = models.JSONField(default=dict)

    class Meta:
        ordering = ["-creado_en"]

    def __str__(self) -> str:
        return f"{self.creado_en:%Y-%m-%d %H:%M} {self.resultado}"
