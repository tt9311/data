# PPI market-data lab

Captura de opciones BYMA por WebSocket autenticado de PPI, con SQLite, tiempos
de recepción y herramientas offline de calidad y replay. El valor inicial es
GGAL: prefijo `GFG`, todas las opciones del catálogo y el subyacente, plazo
`A-24HS`. Se pueden seleccionar símbolos, vencimiento y otros plazos.

El proyecto no envía órdenes ni consulta saldos, movimientos o tenencias.

## Qué datos guarda

- Cada mensaje original de libro u operación, sin descartar repeticiones.
- Bid/ask y cantidades de todos los niveles que entregue PPI; en la validación
  anterior de la integración se observaron hasta cinco niveles.
- Precio y cantidad de las operaciones recibidas, OHLC y campos de volumen tal
  como los publica el proveedor.
- Símbolo, subyacente, plazo, strike y vencimiento. Strike y vencimiento se leen
  de la descripción del catálogo; no se deducen solo del ticker.
- Timestamp del proveedor y UTC/monotónico local al entrar al callback.
- Espera en la cola del recolector, identificador de sesión, huella del mensaje,
  reconexiones, errores y desbordamientos observados.

Libro y operaciones actualizan campos diferentes. Un mensaje de libro con
`Price=0` no borra el último trade. Un trade con `Bids=[]` no borra las puntas.
Los plazos no se mezclan. Los eventos fuera de orden se conservan pero no
retroceden el último estado del mismo tipo. No se inventan precios ausentes.

## Ejecutar

Python 3.12 recomendado:

```sh
pip install -r requirements.txt
```

Configurar `PPI_PUBLIC_KEY` y `PPI_PRIVATE_KEY` en el entorno del proceso o en el
gestor de secretos del servidor. `.env.example` contiene únicamente los nombres;
Python no carga `.env` automáticamente. Usar claves renovadas: no reutilizar una
clave privada que se haya compartido en una conversación.

```sh
# Captura de diez minutos; código de salida distinto de cero si no hubo eventos.
python capture.py --seconds 600 --require-events

# Captura continua hasta Ctrl+C.
python capture.py

# Selección de contratos y plazos.
python capture.py --symbol GFGC5800DI --symbol GFGV5800DI --settlement A-24HS
python capture.py --expiry 2026-12-18 --settlement INMEDIATA --settlement A-24HS
```

Los símbolos anteriores son ejemplos de una sesión de octubre de 2026; consultar
el catálogo actual. La base se guarda en `data/options.sqlite`; `OPTIONS_DATA_DIR`
permite elegir otro directorio. Las claves y tokens se mantienen en memoria y no
se escriben en la base ni en el código. Los logs muestran tipos de errores, no
cabeceras de autenticación. La verificación TLS permanece activa.

El SDK 1.3.0 necesita dos adaptaciones comprobadas en la integración anterior:
WebSocket directo autenticado y callback de cierre compatible con SignalR. La
captura vuelve a autenticar y suscribir al reconectar. No recupera mensajes que
el proveedor no haya enviado durante un corte.

## Calidad, exportación y replay

```sh
python analyze.py --output reports/quality.json
python export.py --output data/events.jsonl
python replay.py --latencies-ms 0,10,50,100,500 --horizon-ms 1000 --fee-bps 1 --output reports/markouts.json
```

El informe incluye percentiles p50/p95/p99 de espera local y tiempos entre
mensajes, repeticiones, eventos fuera de orden y cortes. Las distribuciones usan
como máximo los últimos 100.000 eventos con mediciones. Una repetición de payload
no prueba que dos operaciones sean duplicadas: no se eliminan eventos por hash.

El replay es un ejemplo reproducible de **markout hipotético de cotizaciones**:
una señal de desequilibrio visible se evalúa con entrada demorada y cruce de
spread, salida contra la otra punta y comisión por lado. Los horizontes usan el
reloj monotónico y no cruzan sesiones ni plazos; si faltan cotizaciones próximas
al horizonte, la muestra se descarta. Los valores superpuestos no son el PnL de
una cartera. No modela fills, prioridad en cola, capital, rechazos o impacto.

La API de lectura opcional:

```sh
python serve.py --port 8766
# /health
# /quotes?symbol=GFGC5800DI&settlement=A-24HS
# /instruments?underlying=GGAL
# /events?after=0&limit=1000
# /metrics
```

Escucha en localhost por defecto. Para otra interfaz exige `API_TOKEN`; las
consultas usan `Authorization: Bearer ...`. Poner un proxy HTTPS delante si se
publica en Internet. El servidor incluido es una API ligera de investigación.

## GitHub Actions

`Tests offline` ejecuta las pruebas en cada push y PR sin claves ni acceso a PPI.
`Capture PPI and analyze` ofrece ejecución manual de 60 a 3600 segundos y una
captura programada de 600 segundos a las 14:17 de Argentina, lunes a viernes.
La programación solo se activa al configurar `CAPTURE_ENABLED=true` en Variables.
No incorpora todavía un calendario de feriados ni garantiza puntualidad de inicio.

Para capturar en GitHub:

1. Copiar este proyecto a un repositorio **privado**.
2. Configurar `PPI_PUBLIC_KEY` y `PPI_PRIVATE_KEY` en Settings → Secrets and
   variables → Actions. Son claves de PPI, no la contraseña de la cuenta.
3. Ejecutar manualmente el workflow de captura y revisar el resultado antes de
   habilitar la variable de programación.
4. Descargar el artefacto `ppi-data-*`: SQLite, JSONL, informe de calidad y markouts.
   Los artefactos tienen retención de siete días.

La captura de Actions se limita a repositorios privados. El código puede estar
en un repositorio público, pero ese workflow de captura se omite allí. Las
capturas no se hacen commits ni se suben con el código. GitHub Pages puede
mostrar una interfaz; no ejecuta este recolector. GitHub Actions sirve para
sesiones acotadas, no para un proceso de captura ininterrumpido.

En `tt9311/data`, el proyecto se entrega bajo `ppi-market-data-lab/`, y los dos
workflows tienen nombres `ppi-lab-*` en el directorio `.github/workflows` del
repositorio. Allí la variable de programación se llama `PPI_LAB_CAPTURE_ENABLED`.
Los comandos anteriores se ejecutan dentro de ese subdirectorio.

## Servidor permanente / nube

El Dockerfile y `compose.yaml` preparan un recolector y una API con reinicio
automático y un volumen persistente. Docker Compose puede cargar un `.env` local
creado a partir de los nombres de `.env.example`:

```sh
docker compose up -d --build
docker compose logs -f collector
```

Configurar también `API_TOKEN`. El puerto de Compose queda ligado a localhost
del servidor; agregar HTTPS por el proxy del proveedor. No requiere Chrome.
El volumen debe ser local a la máquina y compatible con WAL de SQLite; no es
un diseño de múltiples máquinas compartiendo una base por almacenamiento de objetos.

## Límites para investigación HFT

El feed de un broker permite estudiar señales y calidad del flujo **observado**.
Estos mensajes no proporcionan secuencia del mercado, libro de órdenes individual,
prioridad, cancelaciones identificadas ni el ciclo de una orden propia. Tampoco
se verificó la sincronización de los relojes. Nanosegundos en el reloj local no
implican exactitud de nanosegundos ni latencia equivalente del feed.

La diferencia entre UTC local y el timestamp PPI se llama desfase aparente: puede
incluir retraso del proveedor, semántica de timestamp y error de reloj. No es una
medición aislada de latencia de red ni de ejecución. Para validar ejecución HFT
haría falta el feed y acceso de mercado correspondientes y un registro de órdenes
y ejecuciones. Este proyecto no los suplanta ni automatiza operaciones.

## Validación

```sh
python -m unittest discover -s tests -v
```

15 pruebas locales sobre normalización, recepción, replay sin información futura,
separación de sesiones/plazos, datos escasos, API y exportación. Docker y workflows
se entregan para verificación en Linux; no se dispone de Docker en este equipo.
La nueva captura instrumentada no obtuvo eventos en la verificación del
19/10/2026: PPI devolvió respuestas no interpretables por el SDK y luego un timeout.
No se presenta esa ejecución como una captura exitosa ni como un benchmark HFT.

Fuentes: [PPI](https://itatppi.github.io/ppi-official-api-docs/),
[SDK](https://github.com/itAtPPI/ppi-connector-python),
[GitHub Actions](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows).
