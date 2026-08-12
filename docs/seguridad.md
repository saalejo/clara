# La superficie expuesta

Dos servicios de esta placa están publicados en internet por el túnel de
Cloudflare, y conviene tener claro qué protege cada capa y qué no.

| Superficie | URL | Qué la protege |
|---|---|---|
| Interfaz de llamada | `clara.voz-digital.com` | Código de acceso en el enlace, límites de sesión y cuota por IP |
| Consola de administración | `panel.voz-digital.com` | Usuario y contraseña de Django, con freno de fuerza bruta |

Ninguna de las dos estaba protegida hasta que se documentó esto. Lo que sigue
explica por qué cada pieza es como es; el «qué» está en el código.

## Por qué hay puerta

Detrás de `clara.voz-digital.com` hay una placa de 4 GB sin GPU, una cuota
gratuita de Gemini que ronda las diez peticiones por minuto, minutos de
transcripción de Deepgram que se facturan por tiempo **conectado**, y relay de
audio de Cloudflare. Y solo cabe **una conversación a la vez**
(`ConnectionMode.SINGLE`): la placa no da para dos pipelines.

Con la interfaz abierta, cualquiera que diera con el dominio podía agotar la
cuota, ocupar la única sesión, o —lo más grave— **cortarle la palabra a quien
estuviera hablando**, porque una oferta nueva desalojaba a la anterior.

## La puerta: un código dentro del enlace

`src/voice_agent/acceso.py`. Quien tiene que entrar recibe el enlace ya
montado; la aplicación canjea el código por una galleta firmada y no vuelve a
pedir nada:

```
https://clara.voz-digital.com/?c=EL-CODIGO
   → 303 a /  + Set-Cookie: clara_acceso=c1.<caducidad>.<HMAC>
```

Cinco decisiones que no se leen en el código:

- **La clave de firma se deriva del propio código.** No hay un segundo secreto
  que alguien deje vacío sin enterarse: sin código, la puerta ni se monta. Y
  rotar el código **invalida todas las galletas ya emitidas**, que es
  exactamente lo que se quiere si el enlace se filtra. El precio: cambiarlo
  echa a quien esté dentro, así que no se rota con una llamada en curso.
- **El código desaparece de la URL.** Por eso el canje responde con una
  redirección y no sirve la página directamente: si no, el código se quedaría
  en la barra de direcciones de cualquier captura de pantalla, en el historial
  y en el `Referer` de la primera llamada a `/api/offer`.
- **La galleta no contiene el código**, solo una fecha de caducidad y su MAC.
  La fecha viaja en claro pero está dentro del cuerpo firmado, así que
  estirarla invalida la firma. Se compara con `hmac.compare_digest`, que no se
  rinde en el primer byte distinto.
- **`SameSite=Lax`, no `Strict`.** Con `Strict`, volver a pulsar el enlace
  desde el correo no manda la galleta —es una navegación entre sitios— y se
  vería la portada teniendo ya acceso. `Lax` no debilita nada aquí: ninguna
  acción con efectos se dispara con un GET.
- **`/salud` sigue pública.** Es la sonda del túnel: cerrarla haría que
  cloudflared diera el origen por caído y tumbaría la demostración entera por
  defendernos de nadie.

### La salida para quien no tiene código

Una puerta sin timbre deja a la gente en una pantalla sin salida, y en una
demostración eso se parece demasiado a que la solución no funciona. Con
`WEB_WHATSAPP` puesto, la portada ofrece un enlace `wa.me` con el mensaje ya
redactado; quien lo pulsa escribe y recibe el enlace completo por respuesta.

- **Vacío por defecto.** El número no está en el repositorio ni hace falta para
  que la puerta funcione: es un `.env`, como el código.
- **También se ofrece con la puerta bloqueada.** Quien ha fallado cinco veces
  suele ser justo quien no tiene el código. A quien lo esté probando a la
  fuerza, un enlace de WhatsApp no le sirve de nada.
- **`rel="noopener noreferrer nofollow"`**: sin `noreferrer`, la primera
  petición le contaría a WhatsApp de dónde viene la visita —y con ella la URL,
  que es precisamente donde puede ir el código.
- No hay endpoint nuevo ni estado que guardar: es un enlace. En una
  demostración, lo que no existe no se puede caer.

**El fallo es abierto a propósito.** Con `WEB_CODIGO_ACCESO` vacío la puerta no
se monta y el servidor se comporta como siempre: una puerta rota no puede
dejar fuera a quien tiene que hacer la demostración. El arranque lo avisa con
un `warning` en el log, y es lo que mantiene `make run-web` usable en local sin
ceremonia.

## Los límites que protegen la placa

Todos en `Settings`, todos ajustables desde el panel salvo el código:

| Límite | Defecto | Qué evita |
|---|---|---|
| `WEB_ACCESO_MAX_INTENTOS` / `WEB_ACCESO_BLOQUEO_SECS` | 5 / 900 s | Probar códigos en bucle. Durante el bloqueo **no entra ni el código correcto**: si no, bastaría con insistir |
| `WEB_LLAMADAS_MAX_POR_HORA` | 12 | Es la red que queda **si el código se filtra**: el enlace compartido no se convierte en barra libre de minutos de transcripción |
| `WEB_LLAMADA_MAX_SECS` | 900 s | Llamadas eternas. Avisa en voz alta un minuto antes, se despide y cuelga; el resumen y la traza se guardan igual |
| `WEB_INACTIVIDAD_SECS` | 300 s | Una pestaña abierta y olvidada, que mantiene viva la conexión de streaming con Deepgram |
| Cuerpo de `/api/offer` | 64 KB | Una oferta SDP real no llega a 10 KB; leer megabytes para descartarlos sería regalar la RAM de la placa |

## No desalojar a quien está hablando

Es el cambio más delicado de todos, porque el comportamiento anterior arreglaba
un fallo real: al recargar la pestaña, la sesión previa quedaba zombi en el
handler y rechazaba toda oferta nueva con un 400 — había que reiniciar el
servicio. El remedio fue desalojar siempre, y eso abrió la puerta a que un
tercero echara al que estuviera hablando, en bucle.

La distinción está en `is_connected()` de pipecat, que **no** mira el estado de
aiortc —que tarda decenas de segundos en enterarse de una pestaña cerrada—
sino la hora del último ping del cliente: es `False` a los tres segundos de que
el navegador deje de dar señales.

Con eso, la regla de `_ofertar`:

1. ¿Nadie vivo? Adelante.
2. ¿Alguien vivo? Se sondea hasta `WEB_ESPERA_SESION_SECS` (8 s). Una pestaña
   recién recargada se delata en ese plazo y el recién llegado entra sin notar
   nada; quien está hablando sigue hablando.
3. ¿Sigue vivo al agotarse la espera? **409** con un mensaje legible.

Solo una petición espera a la vez (un `asyncio.Lock`): si no, veinte peticiones
simultáneas serían veinte tareas dormidas.

**Salida de emergencia**, si esto diera problemas en mitad de una
demostración: `WEB_ESPERA_SESION_SECS=0` en el `.env` rechaza en seco, sin
tocar código.

## De quién es la petición

`voice_agent_core.limitador.ip_del_cliente`. Detrás del túnel,
`request.client.host` es **siempre** `127.0.0.1` y no distingue a nadie: la IP
real llega en `CF-Connecting-IP`, que cloudflared reescribe siempre, así que un
cliente no puede falsificarla *a través del túnel*.

Sí podría hacerlo quien alcance el puerto 7860 directamente por la red local.
Por eso `deploy/clara-web.service` escucha en **loopback**: cloudflared
conecta ahí mismo, y por red local no habría micrófono de todos modos
(`getUserMedia` no existe en orígenes inseguros). Si algún día alguien vuelve a
poner `--host 0.0.0.0`, el limitador se vuelve eludible desde la LAN.

## El panel

- **Todo cerrado por defecto** con `LoginRequiredMiddleware`; las tres
  excepciones (`healthz`, la portada pública y la raíz) están marcadas una a
  una con `@login_not_required`.
- **Freno de fuerza bruta** en el login (`voice_agent_panel/acceso.py`): cinco
  fallos por IP y quince minutos de castigo. Importa incluso si nadie acierta,
  porque cada intento cuesta un PBKDF2 de cientos de miles de iteraciones de la
  misma CPU que está sintetizando la voz de Clara.
- **Galletas seguras tras el túnel** con `PANEL_TRAS_TLS=1`, que además hace
  que Django lea `X-Forwarded-Proto` (a uvicorn la petición le llega como
  `http` sobre loopback, así que sin eso `request.is_secure()` miente).
- La contraseña la siembra `_sembrar_admin()` en cada arranque desde
  `PANEL_ADMIN_PASSWORD`. **No se publica en el repositorio**: va en el correo
  de entrega.
- Los hooks de comandos (`PANEL_HOOKS_COMANDO=0`) siguen siendo la superficie
  de verdad del panel; ver `docs/panel.md § Seguridad`.

### La trampa de las galletas seguras

Encender `CSRF_COOKIE_SECURE` sin `CSRF_TRUSTED_ORIGINS` **rompe el panel
entero**: en cuanto Django cree que la conexión es HTTPS, activa la
comprobación estricta de Origin en cada POST, y guardar ajustes, reiniciar el
agente o subir un documento y reindexar empiezan a responder 403 mientras los
GET siguen perfectos. El síntoma no apunta a las galletas por ningún lado.

Por eso `settings.py` **falla al arrancar** si se pone `PANEL_TRAS_TLS=1` sin
los orígenes: es preferible un servicio que no levanta a uno que parece bien y
falla en la demostración.

## Lo que esto NO protege

- **Inyección de prompt.** Es otra capa y vive en el prompt, en el blindaje de
  los extractos del RAG y en la matriz de escenarios de la sección Calidad del
  panel. Ver `docs/informe-final.md § 6`.
- **A quien tenga el enlace.** El código es un secreto compartido, no una
  identidad: no distingue entre dos personas que lo tengan. Contra el uso
  excesivo de alguien legítimo está la cuota por IP, no la puerta.
- **El tráfico dentro de la placa.** Todo esto es defensa de borde.

- **Los ficheros estáticos, en la caché de Cloudflare.** Comprobado contra el
  despliegue real: el origen responde `401` a `/assets/index-*.js` sin galleta,
  pero Cloudflare guarda ese fichero al servírselo a alguien que **sí** tenía
  galleta y a partir de ahí lo entrega a cualquiera (`cf-cache-status: HIT`).

  Se midió qué queda expuesto así, ruta por ruta: solo el bundle de
  `pipecat-ai-small-webrtc-prebuilt` —js, css y dos svg—, que es un paquete de
  npm público. La portada de la puerta y `/salud` salen como `DYNAMIC` (la
  primera lleva `Cache-Control: no-store`) y `/api/offer` es un POST, que no se
  cachea nunca. **No hay ninguna ruta con contenido privado que sea
  cacheable**, así que lo que "se escapa" es código abierto que cualquiera
  puede descargar de npm, y encima lo sirve el borde en vez de la placa.

  Se deja así a propósito. Ponerle `Cache-Control: private` a todo lo que pasa
  la puerta lo cerraría, pero obligaría a volver a descargar el bundle en cada
  carga y eso se paga justo donde más duele: la primera impresión, en una red
  mala. Si algún día se sirve por aquí algo que no sea público, hay que
  revisarlo: la regla que hay que mantener es **ninguna respuesta cacheable con
  contenido privado**, no «la caché no existe».
