"""Freno a la fuerza bruta en el login del panel.

El panel da ejecución de comandos en la placa y su usuario se siembra en cada
arranque desde `PANEL_ADMIN_PASSWORD`: una contraseña que alguien teclea, no
una que se genera. Desde que responde en `panel.voz-digital.com`, está a un
script de distancia de cualquiera.

El freno se aplica **antes** de autenticar, y ahí está la mitad de su valor:
comprobar una contraseña en Django es un PBKDF2 de cientos de miles de
iteraciones, o sea decenas de milisegundos de la misma CPU que está
sintetizando la voz de Clara. Un bucle de intentos no solo acabaría adivinando
la clave: mientras tanto le robaría los núcleos al agente.

Se usa `LimitadorDeIntentos` y no `django.core.cache` —que serviría, porque el
panel corre con un solo worker y el backend por defecto es `LocMemCache`—
porque el reloj de la caché no es inyectable: probar un castigo de quince
minutos exigiría dormirlos o manosear sus claves.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import views as auth_views
from django.http import HttpRequest, HttpResponse

from voice_agent_core.limitador import LimitadorDeIntentos, ip_del_cliente

#: Un solo limitador para todo el proceso: el panel corre con `workers=1`
#: (ver `__main__.servir`), así que no hay estado que compartir entre procesos.
_LIMITADOR = LimitadorDeIntentos(max_intentos=5, ventana_secs=300.0, bloqueo_secs=900.0)


def limitador_de_entrada() -> LimitadorDeIntentos:
    """El limitador del proceso.

    Es una función y no una constante importada para que los tests puedan
    vaciarlo entre casos sin quedarse con una referencia obsoleta.
    """
    return _LIMITADOR


def _ip(peticion: HttpRequest) -> str:
    """Quién llama, mirando primero las cabeceras del túnel."""
    return ip_del_cliente(peticion.headers, peticion.META.get("REMOTE_ADDR"))


class VistaEntrar(auth_views.LoginView):
    """El login de siempre, con un cubo de fichas por IP.

    Se sobreescriben `post`, `form_valid` y `form_invalid`, **nunca**
    `dispatch`. `LoginView` lleva `@method_decorator(login_not_required,
    name="dispatch")` y `View.as_view()` copia los atributos de `dispatch` al
    callable de la vista: una subclase que redefina `dispatch` sin volver a
    decorarlo pierde la exención, y entonces el `LoginRequiredMiddleware`
    manda el login… al login. El síntoma es un bucle de redirecciones y un
    panel inaccesible, y no se parece en nada a su causa.
    """

    template_name = "panel/entrar.html"

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """Rechaza sin autenticar si la IP ha agotado sus intentos."""
        limitador = limitador_de_entrada()
        restantes = limitador.segundos_restantes(_ip(request))
        if restantes > 0:
            contexto = self.get_context_data(
                form=self.get_form(),
                bloqueado=True,
                minutos=max(1, -(-restantes // 60)),
            )
            return self.render_to_response(contexto, status=429)
        return super().post(request, *args, **kwargs)

    def form_valid(self, form: Any) -> HttpResponse:
        """Acertar limpia el historial de esa IP."""
        limitador_de_entrada().olvidar(_ip(self.request))
        return super().form_valid(form)

    def form_invalid(self, form: Any) -> HttpResponse:
        """Fallar lo apunta."""
        limitador_de_entrada().anotar_fallo(_ip(self.request))
        return super().form_invalid(form)


__all__ = ["VistaEntrar", "limitador_de_entrada"]
