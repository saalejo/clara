# Marco legal de Voz Digital

Investigación del marco legal colombiano aplicable al negocio (corte: agosto
de 2026): qué leyes rigen a una empresa que vende agentes de voz con IA —que
graban conversaciones, guardan datos de prospectos, contactan por teléfono y
WhatsApp, y pueden tratar datos de salud— y qué hay que hacer, en orden, para
cumplirlas. Cada afirmación va con su norma; lo no verificado en fuente
oficial está marcado.

> **Descargo**: esto es investigación informativa, no asesoría legal. Antes de
> firmar el primer contrato con una clínica, constituir la sociedad o
> responder un requerimiento de la SIC, validar estos puntos con un abogado
> colombiano.

## 1. Lo urgente: incumplimientos de hoy

El producto ya está publicado en internet y ya trata datos personales, así que
hay obligaciones que aplican **desde ya**, no desde la primera venta:

1. **[INCUMPLIMIENTO] No hay política de tratamiento de datos publicada** en
   voz-digital.com. Tenerla y publicarla es obligatorio (Ley 1581/2012
   art. 25; Decreto 1377/2013 art. 13) y su ausencia es infracción autónoma
   sancionable — además invalida cualquier autorización «informada» que Clara
   recoja.
2. **[INCUMPLIMIENTO] Clara graba y transcribe voz sin anunciarlo ni pedir
   autorización.** La autorización debe ser previa e informada (arts. 9 y 12
   Ley 1581); la oral vale, pero hay que conservar la prueba (D. 1377
   arts. 7-8). La voz es dato biométrico —sensible— en la doctrina de la SIC.
3. **[INCUMPLIMIENTO] La galleta identificadora de 180 días no se informa.**
   Una cookie que enlaza al visitante con su nombre, teléfono y
   transcripciones es un mecanismo de recolección de datos personales: exige
   aviso y autorización (doctrina SIC 2016 y Guía de marketing 2019).
4. **[INCUMPLIMIENTO] El audio y el texto viajan a EE. UU. (Deepgram, Google,
   Groq) sin contrato de transmisión ni informarlo.** EE. UU. tiene nivel
   adecuado declarado (Circular SIC 005/2017), así que el envío es viable —
   pero exige contratos del art. 25 del D. 1377 con cada proveedor y
   declararlo en la política y el aviso.
5. **[CORREGIR YA] Clara no siempre se identifica como IA.** Hoy no hay
   obligación literal de decir «soy un asistente de IA», pero es la lectura
   conservadora de la transparencia que exige la Circular SIC 002/2024, evita
   el flanco de práctica engañosa (Ley 1480 arts. 30 y 48) y anticipa el
   proyecto de ley de IA en trámite, que sí lo ordena.

La vara sancionatoria es real: multas de hasta 2.000 SMLMV (≈ $3.500 millones
con el mínimo de 2026) y, para tratamiento no autorizado de datos
*sensibles*, cierre inmediato y definitivo (Ley 1581 art. 23). Precedentes
recientes: cierre definitivo de Worldcoin en Colombia por biometría mal
consentida (2025-2026) y $670 millones a Movistar por prospección comercial
sin autorización (2025).

## 2. Datos personales e inteligencia artificial

### Ley 1581 de 2012 + Decreto 1377 de 2013 (habeas data)

El régimen general: **autorización previa e informada** antes de recolectar
(art. 9), deber de informar finalidad y derechos al recolectar (art. 12),
política de tratamiento publicada (art. 25), y derechos del titular a conocer,
actualizar, suprimir y revocar (art. 8). La autorización puede ser **oral o
por conducta inequívoca** —el silencio nunca vale— y hay que **conservar la
prueba** (D. 1377 arts. 7-8): en un agente de voz, el fragmento de audio del
consentimiento asociado a la sesión.

**Datos sensibles** (art. 5): salud y biometría. Su tratamiento exige
autorización *explícita*, informando que responder es facultativo, y no se
puede condicionar el servicio a entregarlos (art. 6; D. 1377 art. 6). La voz
usada para identificar al hablante es biométrica; las transcripciones clínicas
son datos de salud sin discusión.

**Responsable vs encargado** (art. 3): quien decide finalidades es
responsable; quien trata por cuenta de otro, encargado. Voz Digital es
*responsable* de su base de prospectos y *encargado* de los datos de los
pacientes de sus clientes clínicos. La SIC sanciona a ambos (art. 23).

*Qué lo dispara en el producto*: la base de prospectos (nombre, empresa,
contacto, transcripciones), la grabación de cada conversación, y el historial
clínico por número de teléfono del perfil postoperatorio.

### Circular Externa SIC 002 de 2024 (datos personales en sistemas de IA)

Instrucción administrativa de obligatorio cumplimiento para responsables *y*
encargados que desarrollen o usen IA. Exige: ponderación previa (idoneidad,
necesidad, proporcionalidad), principio de **precaución**, gestión de riesgos,
**estudio de impacto de privacidad documentado** cuando haya alto riesgo,
privacidad desde el diseño, transparencia («en cualquier momento y sin
restricciones»), seguridad auditable, y prohibición de recolectar datos
«públicos» de internet para entrenar sin autorización.

No contiene el deber literal de anunciar «soy una máquina», pero sin esa
revelación es difícil sostener que la autorización fue informada. Los
conceptos SIC 25-86338 y 25-167843 (2025) sobre llamadas con IA en contact
centers confirman: la automatización no exime de nada.

*Qué lo dispara en el producto*: todo el producto es un sistema de IA que
trata datos personales. El perfil clínico (sensibles + IA) amerita el estudio
de impacto documentado.

### Transferencias internacionales (art. 26 Ley 1581 · Circular SIC 005/2017)

La lista de países con nivel adecuado de la SIC **incluye a EE. UU.**, así que
enviar audio y texto a Deepgram, Google AI Studio y Groq no está prohibido.
Como actúan por cuenta de Voz Digital, es una *transmisión* a encargados: con
**contrato del art. 25 del D. 1377** (confidencialidad, seguridad, sujeción a
la política) no hace falta consentimiento adicional — pero sí hay que declarar
la circulación internacional en la política y el aviso, y verificar en los DPA
que los proveedores **no entrenen con los datos** (o autorizarlo expresamente:
Circular 002/2024).

### Registro Nacional de Bases de Datos (Decreto 090 de 2018)

Solo obligatorio para sociedades con **activos totales superiores a 100.000
UVT** (≈ $5.237 millones con la UVT 2026) y entidades públicas. Una empresa
pequeña está **exenta del registro** — pero solo del registro: todo lo demás
del régimen aplica igual.

### Estado de la regulación de IA (agosto 2026)

**No hay ley de IA aprobada en Colombia.** El CONPES 4144 (feb-2025) es
política pública sin obligaciones directas para empresas. El proyecto de ley
de IA (PL 043/2025 Senado – 324/2025 Cámara, con mensaje de urgencia) sigue en
trámite; su texto ordena que chatbots y asistentes virtuales **informen que
son IA** — si se aprueba, la obligación será explícita. Lo exigible hoy: Ley
1581 + decretos + Circular 002/2024 + doctrina SIC.

## 3. Contacto comercial y consumidor

### Ley 2300 de 2023 («dejen de fregar») + Circular SIC 001/2024 + RNE

Su art. 5 la extiende al **contacto comercial por SMS, mensajería de
aplicaciones (WhatsApp incluido), correo y llamadas**. Reglas: solo canales
**autorizados previamente** por el titular; horarios lunes a viernes
7:00–19:00 y sábados 8:00–15:00, **nunca domingos ni festivos**; máximo **un
contacto al día** y no multicanal en la misma semana; y consulta obligatoria
del **Registro de Números Excluidos** (RNE, operado por la CRC, con API) antes
de enviar publicidad — inscribirse en el RNE revoca las autorizaciones
previas.

Aplica **aunque la llamada la haga un bot** (conceptos SIC de 2025 sobre IA en
contact centers). Un chat que *inicia el prospecto* (el botón de WhatsApp) no
es contacto no solicitado, pero los seguimientos comerciales posteriores sí
quedan bajo el régimen. El alcance en B2B puro es zona gris: si se marca al
celular de una persona natural, asumir que aplica.

*Qué lo dispara en el producto*: el botón de WhatsApp (los seguimientos), y
sobre todo las llamadas salientes programadas — propias o de clientes que usen
la plataforma. Vale construir en la plataforma el bloqueo de horarios, el tope
de frecuencia y la consulta al RNE: la SIC exige medidas «demostrables».

### Ley 1480 de 2011 (Estatuto del Consumidor, reformada por Ley 2439/2024)

Una empresa que compra el agente para operar su propio negocio lo liga a su
actividad económica → **en principio no es «consumidor»** y el Estatuto no
rige esa venta (art. 5.3; doctrina SIC) — pero la duda se resuelve a favor del
consumidor, así que no hay blindaje absoluto. Si el comprador califica como
consumidor: información precontractual completa, **retracto de 5 días
hábiles** en ventas a distancia (con la excepción clave de servicios cuya
prestación ya comenzó con acuerdo del consumidor) y garantía por condiciones
de calidad (obligación de medio).

**El sitio web comercial** debe cumplir el art. 50: identificación completa
del proveedor (razón social, NIT, dirección, contacto), términos y condiciones
descargables, aceptación verificable, canal de PQR y **enlace visible al sitio
de la SIC**. Y el sitio que comercializa debe **inscribirse en el registro
mercantil** como establecimiento virtual (Ley 633/2000 art. 91, vigente).

### Ley 527 de 1999 + Decreto 2364 de 2012 (comercio electrónico, firma)

Un «acepto» por WhatsApp o un clic **cierra válidamente** un contrato B2B: los
mensajes de datos tienen plena validez y fuerza probatoria. Condiciones
prácticas: que la cuenta sea atribuible a quien obliga a la empresa, conservar
el **hilo nativo con metadatos** (los pantallazos son solo indicio —
T-043/2020), y pactar el mecanismo en un acuerdo marco: la firma electrónica
*pactada* se presume confiable (D. 2364/2012).

## 4. Vender a clínicas: la capa de salud

### Encargo de tratamiento de datos de salud (D. 1377/2013 art. 25)

La clínica es **responsable**; Voz Digital, **encargado** — y responde
directamente ante la SIC por seguridad, confidencialidad y reporte de
incidentes. El contrato de encargo es obligatorio y debe fijar: alcances y
actividades, sujeción a la política de la clínica, subencargados
(Deepgram/LLM/nube) autorizados por escrito con inventario de a qué países
viaja el dato, y devolución/borrado certificado al terminar.

### Historia clínica (Ley 23/1981 · Res. 1995/1999 · Ley 2015/2020 · Res. 1888/2025)

Los síntomas, triajes, alertas y transcripciones del agente **son registros de
la atención** y hacen parte de la historia clínica. La **custodia es siempre
de la IPS**, por mínimo **15 años** (Res. 839/2017); la tercerización es de
infraestructura, no de responsabilidad. Con la Res. 1888/2025 (Resumen Digital
de Atención, HL7 FHIR) la interoperabilidad ya es exigible: el sistema debe
poder **exportar sus registros a la HCE de la clínica**, no quedárselos en un
silo.

### Telesalud (Ley 1419/2010 · Res. 2654/2019)

El seguimiento postoperatorio automatizado que recopila datos clínicos y
dispara alertas es materialmente **telemonitoreo** — categoría de telemedicina
que exige que **la IPS tenga habilitado el servicio en el REPS**. Voz Digital
no se habilita (no es prestador), pero debe entregar la **evidencia técnica de
la plataforma** (seguridad, disponibilidad, trazabilidad, validación del
triaje) que la IPS necesita para sustentar la habilitación. El consentimiento
informado del paciente (art. 7) debe incluir el protocolo ante emergencias y
el canal humano de respaldo.

*[VIGILAR]* En junio de 2026 MinSalud puso en consulta un proyecto que deroga
la Res. 2654 e introduce reglas expresas para plataformas con IA (supervisión
humana, trazabilidad, validación de algoritmos). Confirmar si ya fue expedido
antes de cerrar contratos.

### ¿Dispositivo médico? (Decreto 4725/2005 · INVIMA)

La definición de dispositivo médico incluye expresamente el **software** usado
en diagnóstico, prevención o **supervisión** de la salud, y el INVIMA
clasifica por la **finalidad prevista objetiva** — que el prompt diga «no
diagnostica» no basta. Un software que clasifica síntomas postoperatorios en
verde/amarillo/rojo y alerta al equipo médico tiene **riesgo real y no
descartable** de ser tratado como dispositivo médico con registro sanitario.
La vía seria: documentar la finalidad prevista (herramienta de comunicación y
priorización bajo protocolo médico de la IPS) y elevar **consulta formal de
clasificación al INVIMA** antes de escalar comercialmente. *[VIGILAR]* Hay un
nuevo decreto de régimen único de dispositivos médicos en borrador (alineado
con IMDRF, transición prevista de 18 meses).

### Responsabilidad por una indicación errada (C.C. 1602-1604 · 2341 · 1522 · 63)

Frente al paciente responde la IPS (la atención es del prestador habilitado),
pero Voz Digital puede responder por vía contractual frente a la clínica
(defectos, caídas, alertas no entregadas) y ante la SIC como encargado. En el
contrato B2B caben: obligación de medio, tope de responsabilidad, exclusión de
daños indirectos, indemnidades recíprocas y seguro RC/cyber — pero **ninguna
cláusula exonera el dolo ni la culpa grave** (C.C. 1522 y 63), y nada de eso
es oponible al paciente. Pieza clave: las guías del RAG y el protocolo de
triaje deben estar **aprobados y versionados por la dirección médica de la
IPS**.

## 5. Formalización y tributación

El marco vigente en agosto de 2026 sigue siendo el de la Ley 2277 de 2022 (la
reforma de 2025 se hundió; las emergencias de 2026 no tocaron esto): renta de
sociedades 35 %, IVA 19 %, UVT 2026 = $52.374.

### SAS unipersonal (Ley 1258/2008)

La forma indicada: un solo accionista, documento privado, sin capital mínimo,
**responsabilidad limitada al aporte** — relevante vendiendo software con SLA
a clínicas. Las empresas B2B suelen exigir contratar con persona jurídica.
Costos de constitución del orden de $200.000–300.000 en tasas + impuesto de
registro (~0,7 % del capital); trámite de 1–3 días con NIT/RUT incluido vía
Cámara de Comercio.

### DIAN: RUT, facturación y retenciones (CIIU 6201/6202/6209)

Persona jurídica → **facturación electrónica obligatoria desde la primera
factura** (habilitarse antes; el facturador gratuito de la DIAN basta al
comienzo). Retención que le practicarán: **3,5 % por
desarrollo/licenciamiento de software** (Decreto 2499/2012) — conviene citarlo
en contrato y factura para que no apliquen el 11 % de honorarios. Una SAS es
**responsable de IVA desde el inicio** (el umbral de no responsable es solo
para personas naturales).

### IVA e impuesto de renta (art. 476 ET · Régimen Simple)

Desarrollo, implementación e integración: **gravados al 19 %** (el cliente
empresarial lo descuenta). Pero el art. 476 ET **excluye de IVA el hosting, la
computación en la nube y el mantenimiento a distancia** de programas — vale la
pena facturar por separado la operación/mantenimiento del agente.

**Régimen Simple de Tributación**: para servicios profesionales, 5,9 % sobre
ingresos brutos en el primer rango (hasta 6.000 UVT), sustituye renta e ICA y
**elimina las retenciones**. Para una empresa de servicios con margen alto y
pocos costos, al empezar normalmente conviene; se opta al inscribir el RUT. La
renta exenta de economía naranja fue derogada (Ley 2277 art. 96) — no hay
reemplazo.

### Propiedad intelectual (Ley 23/1982 art. 20 · DNDA)

En obra por encargo, **si el contrato calla, se presume que los derechos
patrimoniales pasan al cliente** en lo necesario para su operación. Por eso el
contrato marco debe pactar expresamente: el desarrollo específico se cede; **la
plataforma (el motor de agentes) se licencia, no se cede**. El registro de
soporte lógico ante la DNDA es gratuito, en línea, y da presunción de
titularidad y fecha cierta.

## 6. Checklist priorizada

En orden de ejecución. Las etiquetas dicen por qué cada cosa está donde está:
`[INCUMPLIMIENTO]` es exposición actual, `[AL FORMALIZAR]` va con la
constitución, `[ANTES DE CLÍNICAS]` condiciona la primera venta al sector
salud, y `[VIGILAR]` es normativa en movimiento.

1. **Redactar y publicar la política de tratamiento de datos y el aviso de
   privacidad en voz-digital.com** `[INCUMPLIMIENTO]` — con: identidad del
   responsable, finalidades (atención comercial, memoria de prospectos, mejora
   del servicio), circulación internacional (EE. UU.: Deepgram, Google, Groq),
   derechos y canal de contacto, y aviso de la galleta identificadora. Ley
   1581 art. 25; D. 1377 arts. 13-15.
2. **Añadir el anuncio al saludo de Clara: quién es, que es una IA, que la
   conversación se graba, y dónde está la política** `[INCUMPLIMIENTO]` —
   seguir conversando tras el aviso opera como conducta inequívoca (D. 1377
   art. 7); conservar el audio del momento como prueba (art. 8). Resuelve de
   un golpe la autorización, la transparencia de la Circular 002/2024 y el
   flanco de práctica engañosa.
3. **Dar salida a los derechos del titular: canal de contacto y capacidad de
   consultar/suprimir una ficha de prospecto** `[INCUMPLIMIENTO]` — correo de
   contacto en la política + procedimiento interno (el almacén de prospectos
   ya permite localizar y borrar por id). Ley 1581 art. 8; Circular 002/2024
   num. VII y X.
4. **Revisar y archivar los términos de datos (DPA) de Deepgram, Google AI
   Studio y Groq** `[INCUMPLIMIENTO]` — verificar que no entrenen con los
   datos enviados (o desactivarlo), y que cumplan lo que exige el art. 25 del
   D. 1377. Documentarlo: la SIC pide responsabilidad «demostrable».
5. **Escribir el estudio de impacto de privacidad del producto**
   `[ANTES DE CLÍNICAS]` — obligado por la Circular 002/2024 cuando hay alto
   riesgo (sensibles + IA): descripción del tratamiento, riesgos, medidas.
   Para el perfil comercial es corto; para el clínico, imprescindible antes de
   la primera venta.
6. **Constituir la SAS y montar la carpintería DIAN** `[AL FORMALIZAR]` — SAS
   unipersonal (CIIU 6201 principal), decidir Régimen Simple al inscribir el
   RUT, habilitarse como facturador electrónico antes de la primera factura,
   seguridad social como independiente, y registrar la plataforma en la DNDA
   (gratis). Reservar en los estatutos y contratos la titularidad del motor.
7. **Dotar el sitio para vender: identificación del comerciante, términos y
   condiciones, PQR, enlace a la SIC; matricular el sitio como establecimiento
   virtual** `[AL FORMALIZAR]` — Ley 1480 art. 50 (basta un
   cliente-consumidor para que aplique) y Ley 633/2000 art. 91. El acuerdo
   marco de servicios define el mecanismo de aceptación electrónica
   (D. 2364/2012) y la cláusula de PI (se cede el desarrollo, se licencia la
   plataforma).
8. **Construir en la plataforma los frenos de la Ley 2300 para llamadas y
   mensajes salientes** `[ANTES DE CLÍNICAS]` — bloqueo duro de horarios y
   festivos colombianos, tope de un contacto al día, y consulta al RNE de la
   CRC (API con tokens de 6 meses) antes de todo contacto publicitario. Es
   además un argumento de venta: la plataforma que cumple sola.
9. **Armar el paquete clínico estándar** `[ANTES DE CLÍNICAS]` — contrato de
   encargo (D. 1377 art. 25) con subencargados e inventario de países ·
   cláusula de datos: todo registro es de la IPS, exportable a su HCE (FHIR,
   Res. 1888/2025), retención de 15 años a su cargo, borrado certificado ·
   declaración de habilitación REPS de la IPS + dossier técnico de la
   plataforma · kit de consentimiento del paciente (telemedicina art. 7 +
   sensibles + grabación) · protocolo de triaje y guías RAG aprobados por la
   dirección médica · obligación de medio, tope de responsabilidad,
   indemnidades, seguro RC/cyber.
10. **Elevar la consulta de clasificación al INVIMA por el triaje**
    `[ANTES DE CLÍNICAS]` — documentar la finalidad prevista con precisión y
    preguntar formalmente si el software de triaje requiere registro sanitario
    (D. 4725/2005). Hacerlo antes de escalar evita construir un negocio sobre
    una incógnita regulatoria.
11. **Vigilar cuatro frentes normativos en movimiento** `[VIGILAR]` — el
    proyecto de ley de IA (ordenaría anunciar «soy una IA»); la nueva
    resolución de telesalud (reglas para plataformas con IA, en consulta en
    jun-2026); el nuevo decreto de dispositivos médicos (en borrador); y el
    litigio del salario mínimo 2026 (mueve los topes sancionatorios en pesos).

## 7. Zonas grises y pendientes de confirmar

- Ningún concepto de la SIC que se pudo abrir enumera **textualmente la voz**
  como dato biométrico; lo afirman fuentes secundarias consistentes. Tratarla
  como sensible es la posición prudente.
- El alcance de la Ley 2300 en **B2B puro** («consumidor comercial») no está
  delimitado por la SIC.
- La aplicación de la Ley 1480 a un proveedor SaaS como «productor» o «medio»
  es interpretación, no doctrina oficial.
- Numeración exacta de las exclusiones de IVA del art. 476 ET (el contenido
  —cloud, hosting, mantenimiento a distancia— está corroborado; el numeral,
  verificarlo con el contador).
- Estado, a la fecha exacta de lectura, del proyecto de ley de IA, de la nueva
  resolución de telesalud y del decreto de dispositivos médicos: los tres
  estaban en trámite/borrador en las fuentes más recientes (mayo-julio 2026).
- Tarifas puntuales 2026 de cámara de comercio, SMMLV definitivo (hay litigio
  en el Consejo de Estado) y convocatorias CTeI de MinCiencias.

## 8. Fuentes principales

Verificadas en fuente oficial o compilación estatal (los portales
secretariasenado y suin-juriscol rechazaron conexiones en varias consultas; se
usaron los normogramas oficiales de MinTIC, CRC, DIAN, Función Pública y el
Régimen Legal de Bogotá):

- [Ley 1581 de 2012](https://www.alcaldiabogota.gov.co/sisjur/normas/Norma1.jsp?i=49981) · [Decreto 1377 de 2013](https://www.alcaldiabogota.gov.co/sisjur/normas/Norma1.jsp?i=53646)
- [Circular Externa SIC 002 de 2024 (IA)](https://sedeelectronica.sic.gov.co/sites/default/files/normativa/Circular%20Externa%20No.%20002%20del%2021%20de%20agosto%20de%202024.pdf)
- [Circular SIC 005 de 2017 (países adecuados)](https://normograma.dian.gov.co/dian/compilacion/docs/circular_superindustria_0005_2017.htm)
- [Ley 2300 de 2023](https://normograma.crcom.gov.co/crc/compilacion/docs/ley_2300_2023.htm) · [Circular SIC 001 de 2024 (RNE)](https://normograma.mintic.gov.co/mintic/compilacion/docs/circular_superindustria_0001_2024.htm)
- [Ley 1480 de 2011](https://www.funcionpublica.gov.co/eva/gestornormativo/norma.php?i=44306) · [Ley 2439 de 2024](https://www.funcionpublica.gov.co/eva/gestornormativo/norma.php?i=257116)
- [Ley 527 de 1999](https://normograma.mintic.gov.co/mintic/compilacion/docs/ley_0527_1999.htm) · [Decreto 2364 de 2012](https://normograma.mintic.gov.co/mintic/compilacion/docs/decreto_2364_2012.htm)
- [Ley 633 de 2000, art. 91](http://www.secretariasenado.gov.co/senado/basedoc/ley_0633_2000_pr001.html)
- [Resolución 1995 de 1999](https://www.minsalud.gov.co/normatividad_nuevo/resoluci%C3%93n%201995%20de%201999.pdf) · [Resolución 839 de 2017](https://www.minsalud.gov.co/Normatividad_Nuevo/Resolucion%20No%20839%20de%202017.pdf)
- [Ley 2015 de 2020](https://www.suin-juriscol.gov.co/viewDocument.asp?ruta=Leyes/30038770) · [Resolución 1888 de 2025](https://www.minsalud.gov.co/Normatividad_Nuevo/Resolucion%20No%201888%20de%202025.pdf)
- [Resolución 2654 de 2019](https://www.minsalud.gov.co/Normatividad_Nuevo/Resoluci%C3%B3n%20No.%202654%20del%202019.pdf) · [Ley 1419 de 2010](http://www.secretariasenado.gov.co/senado/basedoc/ley_1419_2010.html)
- [Decreto 4725 de 2005](https://www.minsalud.gov.co/sites/rid/lists/bibliotecadigital/ride/de/dij/decreto-4725-de-2005.pdf) · [INVIMA — software como dispositivo](https://www.invima.gov.co/productos-vigilados/dispositivos-medicos/preguntas-frecuentes-dispositivos)
- [Sanción Movistar 2025 (SIC)](https://sedeelectronica.sic.gov.co/comunicado/la-sic-sanciona-movistar-por-tratamiento-indebido-de-datos-personales-con-fines-de-prospeccion-comercial)
- [CONPES 4144 de 2025 (IA)](https://colaboracion.dnp.gov.co/CDT/Conpes/Econ%C3%B3micos/4144.pdf) · [PL 043 de 2025 (IA)](https://leyes.senado.gov.co/proyectos/index.php/textos-radicados-senado/p-ley-2025-2026/3569-proyecto-de-ley-043-de-2025)
- [CCB — constitución virtual SAS](https://www.ccb.org.co/es/servicios/crea-tu-empresa/constituye-tu-empresa/constitucion-virtual-sas) · [DNDA — registro de software](https://www.derechodeautor.gov.co/es/atencion-y-servicios-a-la-ciudadania/registro-de-obras/registro-de-software/registro-de-software)
- [DIAN — obligados a facturar](https://www.dian.gov.co/impuestos/sociedades/Paginas/obligadosfacturar.aspx) · [C-540/2023 (tarifas Régimen Simple)](https://normograma.dian.gov.co/dian/compilacion/docs/c-540_2023.htm)
