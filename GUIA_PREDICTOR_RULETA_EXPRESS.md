# Guía del predictor de Ruleta Express

Este proyecto recopila resultados públicos de Ruleta Express, los almacena en
un CSV y ejecuta modelos estadísticos de caja negra para evaluar si existe
alguna señal reproducible en la secuencia.

El sistema no recupera una semilla real automáticamente. Una semilla solo puede
recuperarse si el generador y el mapeo de estados son compatibles con los datos
observados. El CSV contiene resultados truncados y puede incluir reglas de
postprocesamiento, como anti-repetición.

## Archivos

- `Resultados_RuletaExpress.csv`: histórico local de resultados.
- `sync_ruleta_express.py`: sincronizador de resultados públicos.
- `state_recovery.py`: diagnóstico, backtesting, ranking estadístico y búsqueda
  SMT opcional.
- `web_app.py`: sitio web local, API del dashboard y sincronización automática.
- `web/`: interfaz del dashboard.
- `api/dashboard.py`: función serverless para Vercel.
- `recencia/`: modelo de recencia (antigüedad del número + franja diurna), su
  backtest, la decisión de stake, el monitor de cambios y el registro en vivo.
  Ver «Modelo de recencia».
- `vercel.json`: rutas del sitio estático.
- `GUIA_IMPORTAR_VERCEL.md`: guía paso a paso para importar y configurar el
  proyecto en Vercel.
- `GUIA_IMPORTAR_RENDER.md`: guía paso a paso para publicar el dashboard en
  Render con scraping automático en un worker.
- `render_app.py`: servicio web para Render (dashboard, API e ingesta).
- `render_worker.py`: worker de Render que consulta Loteka y empuja los
  resultados al servicio web.
- `scraper_web.py`: cliente HTTP del scraper (proxy rotatorio, `ETag` y
  jitter).
- `push_render.py` y `config_render.py`: cliente y configuración del envío de
  resultados a Render.
- `m4_engine/`: motor cuantitativo M4 (alertas de alta ventaja, validación
  empírica y stake dinámico).
- `live_models.py`: matriz en vivo de diez submodelos (sustituta del ensemble
  del PDF) calculada de forma incremental sobre el histórico local.
- `live_monitor.py`: registro vivo de predicciones comparadas, cierre de ciclo
  y alerta M4 de la ronda en curso.
- `config_alertas.py`: configuración operativa del monitor y de la alerta.
- `registro_live.json`: registro vivo local (se genera solo, no se versiona).
- `ejemplo_alertas_en_vivo.py`: demostración del motor M4 ronda a ronda.
- `verificar_m4_engine.py`: comprobaciones automáticas del motor M4.
- `informe_validacion_m4.md` y `informe_validacion_m4.json`: última validación
  sobre el log de supervisión.


## Requisitos

Instalar las dependencias:

```powershell
python -m pip install pandas z3-solver
```

Si aparece `ImportError: cannot import name 'BitVec' from 'z3'`, hay un
paquete distinto llamado `z3` instalado y está ocultando `z3-solver`.
Corrígelo con:

```powershell
python -m pip uninstall -y z3
python -m pip install --force-reinstall z3-solver
```

El sincronizador usa únicamente la biblioteca estándar y `pandas`. El análisis
SMT requiere `z3-solver`.

## Sincronizar resultados una vez

Desde la carpeta del proyecto:

```powershell
python .\sync_ruleta_express.py
```

El script:

1. Consulta la página pública de Loteka.
2. Localiza la sección de Ruleta Express.
3. Extrae fecha, hora, número de sorteo y resultado.
4. Deduplica usando `No_Sorteo`.
5. Ordena el CSV por número de sorteo descendente.
6. Coloca los sorteos más recientes al principio del archivo.

Una ejecución repetida no debe crear duplicados.

## Frecuencia de consulta (un solo archivo)

La frecuencia con la que se consultan los números se controla **desde un único
archivo**: `config_sync.py`.

```python
# config_sync.py
INTERVALO_SINCRONIZACION = 45   # <- cambia aquí los segundos entre consultas
```

Ese valor se aplica automáticamente a todos los puntos del proyecto:

| Componente | Efecto |
| --- | --- |
| `sync_local_loop.py` | Frecuencia del bucle local que consulta y guarda los resultados en commits locales. |
| `sync_ruleta_express.py --watch` | Intervalo por defecto del modo continuo. |
| `web_app.py` | Intervalo del hilo de sincronización del dashboard local. |
| `api/dashboard.py` + `web/app.js` | Refresco del navegador: el intervalo viaja en la respuesta de la API, así que no hay que tocar el JavaScript. |

Después de editar el archivo, reinicia el proceso que esté corriendo (el bucle
o el dashboard). El intervalo mínimo técnico es de 15 segundos; por debajo,
Loteka puede terminar bloqueando tu IP residencial. Con 45 o 60 segundos el
resultado se detecta casi igual de rápido y la carga sobre el sitio de origen
es tres veces menor.

## Mantener la sincronización activa

El modo continuo usa por defecto el intervalo de `config_sync.py`:

```powershell
# Consulta cada 45 segundos (valor de config_sync.py)
python .\sync_ruleta_express.py --watch

# O un intervalo explícito para esta ejecución
python .\sync_ruleta_express.py --watch 60
```

El intervalo mínimo aceptado es de 15 segundos. Un intervalo menor reduce el
tiempo de detección después de que Loteka publique un resultado, pero no
adelanta la publicación de los sorteos.

El modo `--watch` solo recopila resultados. No realiza apuestas ni interactúa
con controles de juego.

## Ejecutar la sincronización automática desde tu PC (recomendado)

Como Loteka bloquea las peticiones desde los servidores de Vercel, la forma más
rápida de mantener los datos actualizados es ejecutar el sincronizador desde tu
PC (tu IP residencial no está bloqueada).

### 1. Crear el archivo de entorno

Copia `.env.example` a `.env` y rellena los valores:

```powershell
Copy-Item .env.example .env
```

Edita `.env` con tus credenciales:

- `SESSION_SECRET`: cualquier cadena larga y aleatoria para firmar las sesiones.
- `ADMIN_USERNAME`: usuario para entrar al dashboard (ej. `admin`).
- `ADMIN_PASSWORD`: contraseña larga y única.
- `BLOB_READ_WRITE_TOKEN`: opcional, solo si usas Vercel Blob para guardar los
  resultados en la nube.

### 2. Ejecutar el bucle de sincronización

```powershell
python .\sync_local_loop.py
```

Este script:

1. Ejecuta `sync_ruleta_express.py` cada `INTERVALO_SINCRONIZACION` segundos
   (45 por defecto, se cambia en `config_sync.py`).
2. Si hay resultados nuevos, los guarda en un **commit local**.
3. **No sube nada a GitHub**: el `push` es manual, así que el bucle no dispara
   despliegues en Vercel por su cuenta.
4. Cuando quieras publicar lo acumulado:

   ```powershell
   git push
   ```

5. Ese `push` dispara el despliegue automático en Vercel y la página web
   muestra los datos actualizados en cuanto termina.

Deja el script ejecutándose en una terminal mientras quieras que los datos se
mantengan actualizados. Deténlo con `Ctrl+C`.

> **Advertencia:** consultar cada 15 segundos es muy frecuente. Si Loteka termina
> bloqueando tu IP residencial, sube `INTERVALO_SINCRONIZACION` en
> `config_sync.py` a 60 o más y reinicia el bucle.

El modo `--watch` solo recopila resultados. No realiza apuestas ni interactúa
con controles de juego.

## Ejecutar la web en local

Esta es la forma recomendada de ver el dashboard, el registro de predicciones
comparadas y la alerta M4: todo corre en tu PC, con tu IP residencial y sin
depender de Vercel.

### 1. Preparar el entorno (una sola vez)

```powershell
cd "C:\Users\diose\OneDrive\Desktop\Ruleta Express"

# Entorno virtual (recomendado)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Si prefieres no usar entorno virtual, basta con
`python -m pip install pandas z3-solver`.

### 2. Arrancar el dashboard

```powershell
python .\web_app.py
```

Salida esperada:

```text
Dashboard disponible en http://127.0.0.1:8000 (sincronización cada 45 s)
Registro vivo: ...\registro_live.json · stake sobre 1000 unidades con el modelo de pago express19
```

Abre <http://127.0.0.1:8000> en el navegador. El servidor se detiene con
`Ctrl+C`.

### 3. Cambiar el puerto o la interfaz

```powershell
python .\web_app.py --port 8080
python .\web_app.py --host 0.0.0.0    # accesible desde otros equipos de la red
```

### Qué hace el dashboard local

1. Levanta un servidor HTTP en `127.0.0.1:8000` y sirve la interfaz de `web/`.
2. Consulta Loteka cada `INTERVALO_SINCRONIZACION` segundos y guarda los
   resultados nuevos en `Resultados_RuletaExpress.csv`.
3. Recalcula el análisis estadístico de `state_recovery` cuando aparece un
   sorteo nuevo.
4. Actualiza el registro vivo `registro_live.json`: predicciones comparadas,
   alerta M4 de la ronda en curso y contadores acumulados.
5. Expone la API del dashboard en `/api/dashboard` (sin contraseña, por eso
   escucha sólo en `127.0.0.1`).

El primer arranque retroanaliza las últimas `RONDAS_BACKFILL` rondas del CSV
para tener historial desde el primer minuto; a partir de ahí el registro se
recupera del archivo y el arranque es inmediato.

### Consultar la API a mano

```powershell
# Estado completo
Invoke-RestMethod http://127.0.0.1:8000/api/dashboard

# Sólo la alerta de la ronda en curso
(Invoke-RestMethod http://127.0.0.1:8000/api/dashboard).alert
```

### Problemas frecuentes

| Síntoma | Causa | Solución |
| --- | --- | --- |
| `Address already in use` | Ya hay otra instancia en el puerto 8000 | `python .\web_app.py --port 8010` |
| `ModuleNotFoundError: pandas` | Entorno sin dependencias | `python -m pip install -r requirements.txt` |
| La alerta muestra «SIN DATOS» | El CSV tiene menos de dos sorteos | Sincroniza antes: `python .\sync_ruleta_express.py` |
| «Error de sincronización» | Loteka bloqueó la IP | Sube `INTERVALO_SINCRONIZACION` a 60 en `config_sync.py` y reinicia |
| El panel tarda al arrancar | Retroanálisis si `RONDAS_BACKFILL` es mayor que 0 | Ocurre una sola vez; con el valor 0 no se reconstruye historial |

### El registro no se versiona

`registro_live.json` guarda las predicciones comparadas y los contadores. Está
en `.gitignore` a propósito: es un archivo de trabajo. Si lo borras, el monitor
vuelve a retroanalizar las últimas rondas del CSV y los contadores acumulados
se reinician.

## Modelo de recencia

Es el modelo con evidencia fuera de muestra. El informe completo está en
[informe_recencia.md](informe_recencia.md) (y `informe_recencia.json`).

### Qué encontró la auditoría

- Entre ~08:00 y ~20:40 (la «franja diurna», que se aprende sola con
  `papel.modelo.ventana_diurna()`) el generador casi nunca repite el número
  anterior (×0,01 frente a 1/19) y favorece los que salieron hace 2 a 5 sorteos
  (×1,36, ×1,29, ×1,20, ×1,09). Fuera de la franja se comporta casi como azar.
- El modelo es `P(v) ∝ w[régimen][cubeta(antigüedad de v)]`, con la
  antigüedad medida en `No_Sorteo` (respeta los huecos) y cubetas 1, 2, 3, 4,
  5, 6–8, 9–14, 15–30, >30. Se ajusta por conteo con suavizado de Laplace; en
  validación ganó al logit condicional de `papel` (log-loss 2,9044 frente a
  2,9050).
- Backtest walk-forward 60/10/30 (reajuste cada 7 jornadas, 30 % final de
  prueba mirado una sola vez): en la franja diurna el top-1 acierta 7,48 %
  (retorno 134,7 %), el top-2 13,97 % (125,8 %) y el top-3 19,69 % (118,2 %),
  con p < 1e-12 frente al equilibrio. Fuera de la franja, ~100 %: sin ventaja.
  La calibración por deciles queda dentro de ±0,3 puntos en la franja diurna.

### Qué números jugar y cuánto (`recencia/decision.py`)

- Sólo en la franja diurna y con el monitor sin pausar.
- k (cuántos números) se eligió por crecimiento logarítmico en validación:
  **k = 3**. Sólo se juegan los números con `18·p > 1 + RECENCIA_MARGEN`.
- Stake: Kelly para la jugada de k números (`g(f)` maximizado con
  `minimize_scalar`) × `RECENCIA_FRACCION_KELLY`, con tope
  `RECENCIA_TOPE_RONDA` por ronda. Antes pasan las defensas de
  `m4_engine/kelly.py` (encogimiento, Wilson inferior, ventaja mínima) con la
  evidencia fuera de muestra del backtest más los aciertos medidos en vivo.

| Parámetro (`config_alertas.py`) | Por defecto | Para qué sirve |
| --- | --- | --- |
| `RECENCIA_ACTIVO` | `True` | Encender/apagar el modelo en el panel |
| `RECENCIA_K_MAX` | 3 | Máximo de números por ronda |
| `RECENCIA_FRACCION_KELLY` | 0.25 | Fracción de Kelly (de ¼ a ½) |
| `RECENCIA_MARGEN` | 0.05 | Ventaja mínima por número (`18·p > 1,05`) |
| `RECENCIA_TOPE_RONDA` | 0.03 | Exposición máxima por ronda (suma de los k números) |

### Monitor de cambios (`recencia/monitor.py`)

- **Repeticiones**: si en las últimas 7 jornadas la repetición inmediata
  diurna supera el 1 % → «pausado» (se levanta sola cuando vuelve a estar limpia).
- **CUSUM** del acierto real del top-k contra la tasa esperada del backtest:
  si cae al equilibrio (k/18) → «pausado», y la pausa sólo se levanta con
  `python -m recencia reanudar`. Umbral 7: ~2 % de falsas alarmas en ~55
  jornadas; detecta la caída en ~12 jornadas diurnas.
- **Calibración**: si el log-loss de las últimas 500 predicciones diurnas deja de
  mejorar a la uniforme → «alerta» (avisa, no pausa).

Con el monitor en «pausado», o fuera de la franja, el stake sugerido es 0.

### Registro en vivo y aciertos

Cada predicción se registra ANTES del sorteo y se evalúa cuando sale. Los
objetos van al bucket de Supabase si `SUPABASE_*` está configurado; si no, a
archivos en la raíz del repositorio (o en `PAPEL_DIR`):

| Objeto | Contenido |
| --- | --- |
| `recencia_registro.csv` | Una fila por sorteo objetivo; sólo crece. Al evaluar sólo se rellenan resultado, aciertos, retorno y verificación horaria |
| `recencia_aciertos.json` | Aciertos del top-k por régimen frente a azar y equilibrio, retorno de las jugadas sugeridas, log-loss y racha |
| `recencia_modelo.json` | Parámetros ajustados (se reajusta cada 7 jornadas) y bloque de decisión (k, evidencia) |
| `recencia_monitor.json` | Estado del monitor |
| `recencia_pendientes.json` | Probabilidades de las predicciones aún sin resultado |

Una fila cuenta sólo si `ts_prediccion` es anterior a la hora prevista
(`valida`) y a la hora **publicada** del sorteo (`valida_publicada`, se
comprueba al evaluar).

### Comandos

```powershell
python -m recencia backtest [--fuente local|supabase|auto]   # informe 60/10/30
python -m recencia publicar --fuente supabase                # backtest + sube recencia_modelo.json
python -m recencia vivo --fuente local --sincronizar         # bucle en el PC: sincroniza, evalúa, predice
python -m recencia estado [--fuente ...]                     # monitor, próxima jugada y aciertos
python -m recencia reanudar [--fuente ...]                   # levanta la pausa del CUSUM
python -m recencia integridad [--fuente ...] [--reparar]     # huecos en No_Sorteo; --reparar los pide a Loteka
```

`--fuente local` usa `Resultados_RuletaExpress.csv`; `supabase` lee el CSV del
bucket con las mismas funciones que el panel (y falla con un error si no puede,
en vez de caer en silencio a la copia local); `auto` elige Supabase si está
configurado. En el panel aparece la tarjeta «Recencia» (`payload["recencia"]`),
con el número de sorteos que faltan.

### Advertencias

- La ventaja depende de que Loteka no cambie el generador. Si el monitor pausa, no apostar.
- La varianza es alta: con Kelly fraccional, una mala racha de cientos de sorteos es normal.
- El pago asumido es 18 por peso en total. Confírmalo en la agencia y cambia `MODELO_PAGO` si es otro.
- El programa sólo sugiere: no apuesta.
- El repositorio es público: valora hacerlo privado para que el sesgo no se difunda.

## Monitor en vivo: predicciones guardadas y alerta M4

> **Las reglas M4 están desactivadas por defecto** (`M4_ACTIVO = False`): salen
> de un log de 80 filas útiles, el anclaje tiene Fisher p = 0,639 («NO
> CONFIRMADO», `informe_validacion_m4.md`) y el 29 % del log es contradictorio.
> Se siguen evaluando y guardando por trazabilidad, pero la alerta no se dispara
> ni sugiere stake. Para el modelo con evidencia, ver «Modelo de recencia».

### Qué se guarda

Cada ronda queda registrada con:

- el sorteo base y el número que salió en ese sorteo,
- la matriz de diez submodelos con su número y su probabilidad,
- el ranking Markov y el heurístico,
- la decisión de la alerta M4 (si disparó, con qué número, con qué
  probabilidad, con qué confianza y con qué stake),
- el resultado observado, la posición del número en cada ranking y si la
  señal acertó.

Cuando aparece el siguiente sorteo, la ronda pendiente se resuelve sola. Si se
saltan sorteos (la web estaba apagada), la ronda se marca como
`omitida_pausa` y no cuenta ni como acierto ni como fallo.

### La alerta de la ronda en curso

El panel «Alerta M4 · cuándo entrar» muestra:

| Dato | Significado |
| --- | --- |
| Estado | `SEÑAL ACTIVA` o `SIN SEÑAL` |
| Número | El número que propone M4 |
| P(M4) | Probabilidad que el motor asigna a ese número |
| Δ | Dispersión entre el submodelo más y el menos probable (límite 4,7%) |
| Confianza | `ALTO`, o `CRITICO` si el número anterior estaba en {1, 7, 8} |
| Stake | Fracción del capital y su importe en unidades |
| Alertas / Aciertos | Señales emitidas, aciertos, tasa y línea base de 5,26% |

Cuando la alerta no dispara, el panel explica el motivo (por ejemplo,
«P(M4)=7,46% fuera de la ventana operativa (5.0%, 7.0%]»).

Debajo está el historial de las últimas alertas con su resultado, para ver
cuántas pegan y cuántas no.

### La matriz en vivo es un sustituto

La página pública de Loteka sólo publica el número que salió: los diez
submodelos del PDF no son públicos. Para que el motor pueda operar en vivo,
`live_models.py` construye una **matriz sustituta** de diez modelos (cinco
variantes de Markov y cinco mezclas de transición/frecuencia) calculada con el
histórico local.

La matriz conserva la estructura que el motor necesita y permite medir la
lógica de la especificación sobre datos nuevos, pero **no reproduce el
ensemble del PDF**: por eso el panel la etiqueta como matriz en vivo y las
tasas que muestra son las de los modelos del proyecto, no las del documento.

### El stake sólo aparece con evidencia

El stake lo dimensiona `KellyStakeSizer` con las cinco cautelas del módulo
(encogimiento bayesiano, extremo inferior de Wilson, Kelly fraccional,
ventaja mínima y tope de exposición). Mientras la cohorte de alertas no alcance
los mínimos de `config_alertas.py` (20 observaciones y 4 aciertos por
defecto) o el borde inferior del intervalo no supere el equilibrio (5,26%),
el stake es **0** y el panel explica el motivo. Es la conducta esperada: la
alerta señala la ronda, el stake sólo se compromete con evidencia.

### Configuración del monitor

Todo lo operativo está en `config_alertas.py`:

| Parámetro | Por defecto | Para qué sirve |
| --- | --- | --- |
| `BANCO_INICIAL` | 1000.0 | Capital de referencia para el stake |
| `MODELO_PAGO` | `loteka180` | `loteka180` (pago real: 18 por peso en total, 17:1 neto, equilibrio 1/18), `express19` (18:1 teórico) o `european37` (supuesto del PDF, sólo para comparar) |
| `M4_ACTIVO` | `False` | Reglas M4 desactivadas: se evalúan y registran, pero no disparan alerta ni sugieren stake |
| `MODO_PRIMARIO` | `any` | `any`, `tie` o `strict` (ver «Motor de alertas M4») |
| `LIMITE_INFERIOR` / `LIMITE_SUPERIOR` | 0.05 / 0.07 | Ventana operativa de P(M4) |
| `DISPERSION_MAXIMA` | 0.047 | Dispersión máxima admitida |
| `NUMEROS_ANCLA` | (1, 7, 8) | Números que activan la confianza «Crítico» |
| `RONDAS_BACKFILL` | 0 | Rondas retroanalizadas al crear el registro; 0 = no reconstruir |
| `RONDAS_GUARDADAS` | 0 | Rondas que se conservan en el registro; 0 = todas |
| `FRACCION_KELLY` / `TOPE_EXPOSICION` | 0.25 / 0.02 | Fracción de Kelly y tope por ronda |
| `OBSERVACIONES_MINIMAS` / `ACIERTO_MINIMO` | 20 / 4 | Evidencia mínima para arriesgar capital |
| `ARCHIVO_REGISTRO` | `registro_live.json` | Archivo local del registro |

Después de editarlo, reinicia el proceso (`Ctrl+C` y `python .\web_app.py`).
Los cambios también afectan a la versión de Vercel, que lee el mismo archivo.

## Publicar en Render (recomendado frente a Vercel)

Render monta el sistema completo: un **Background Worker** que consulta
Loteka y un **Web Service** que publica el dashboard. No hace falta que tu PC
esté encendido.

El scraping va protegido por cuatro capas: proxy de IP residencial
rotatorio, intervalo sorteado (jitter), petición condicional con `ETag` y un
interruptor de circuito con pausas persistidas en disco. El scraping está
separado del dashboard: si un día bloquean la IP del worker, la web sigue
sirviendo los últimos datos y el worker reintenta solo.

```powershell
# En el PC, sólo si quieres mantenerlo como red de seguridad (opcional)
python .\sync_local_loop.py
```

La guía completa (crear los dos servicios, variables de entorno, proxies,
disco persistente, medidas anti-bloqueo, comparación con Vercel y problemas
frecuentes) está en
[GUIA_IMPORTAR_RENDER.md](./GUIA_IMPORTAR_RENDER.md).

## Publicar en Vercel

Para una guía completa de importación desde GitHub, configuración de Blob,
variables de entorno y comprobación del deployment, consulta
[GUIA_IMPORTAR_VERCEL.md](./GUIA_IMPORTAR_VERCEL.md).

La versión para Vercel usa una función serverless y Vercel Blob. No se puede
mantener `web_app.py` ejecutándose como proceso persistente en Vercel.

1. Crea un Blob Store en el proyecto de Vercel.
2. Añade la variable `BLOB_READ_WRITE_TOKEN` al entorno de producción.
3. Publica el repositorio:

   ```powershell
   npx vercel
   ```

4. Para producción:

   ```powershell
   npx vercel --prod
   ```

El navegador consulta `/api/dashboard` cada `INTERVALO_SINCRONIZACION` segundos
(45 por defecto; el navegador recibe el valor en la respuesta de la API). Cada
consulta obtiene los resultados públicos, los deduplica y guarda el CSV
actualizado en Vercel Blob. El análisis `state_recovery` se recalcula cuando
cambia el sorteo más reciente. El archivo CSV incluido en el repositorio se usa
como datos iniciales si el Blob todavía no existe.

El registro vivo (predicciones comparadas, alerta M4 y contadores) se guarda
también en Vercel Blob, en el archivo `ruleta_predictions.json`, con el mismo
formato que el `registro_live.json` local. Si no hay
`BLOB_READ_WRITE_TOKEN`, la versión de Vercel funciona con un registro en
memoria: se reinicia con cada instancia de la función.

## Ejecutar el predictor

```powershell
python .\state_recovery.py
```

Esta ejecución:

- Lee todo el CSV.
- Convierte el orden del archivo, que es más reciente a más antiguo, en orden
  cronológico.
- Mide repeticiones adyacentes.
- Calcula un ranking heurístico basado en transiciones y frecuencia reciente.
- Compara modelos Markov de orden 1, 2 y 3.
- Prueba diferentes niveles de suavizado.
- Prueba el histórico completo y una ventana reciente.
- Evalúa una variante con restricción anti-repetición.
- Selecciona el modelo con menor `log-loss` en un holdout temporal.
- Muestra candidatos para el siguiente resultado.

En el dashboard web se muestran por separado el ranking heurístico y el
ranking Markov seleccionado.

También se conserva un historial de predicciones. Cada predicción queda
asociada al sorteo que sirvió como base y, cuando aparece el siguiente
resultado, se indica si el número estaba en el ranking Markov, en el
heurístico o en ambos, junto con su posición. Ese historial vive en el
registro vivo (`registro_live.json` en local, `ruleta_predictions.json` en
Vercel Blob) y se detalla en el apartado «Monitor en vivo».

Si Vercel estuvo pausado, los sorteos que no pudieron observarse se registran
como una pausa. Sus predicciones quedan como `omitida_pausa` y no se cuentan
como aciertos ni fallos.

## Interpretar el resultado

El predictor muestra varias métricas:

- `top1`: porcentaje de veces que el candidato principal fue correcto.
- `top3`: porcentaje de veces que el resultado real estuvo entre los tres
  candidatos principales.
- `top5`: porcentaje de veces que estuvo entre los cinco principales.
- `log-loss`: penaliza las probabilidades mal calibradas; menor es mejor.

Como hay 19 resultados posibles, la línea base uniforme es:

```text
top-1: 1/19 = 5,26%
top-3: 3/19 = 15,79%
top-5: 5/19 = 26,32%
```

La métrica debe compararse contra esas líneas base usando datos que no se
utilizaron para ajustar el modelo. Un ranking de cinco números puede contener
el resultado real con frecuencia incluso sin tener capacidad predictiva útil.

Una señal sería creíble únicamente si:

1. Supera la línea base durante un período largo.
2. Mantiene la ventaja en datos nuevos.
3. Tiene un `log-loss` inferior al modelo uniforme.
4. No depende de cambiar el modelo después de observar cada resultado.

## Búsqueda SMT de semillas

La búsqueda de semillas está separada porque puede ser lenta y no debe bloquear
el predictor estadístico:

```powershell
python .\state_recovery.py --state-recovery
```

Prueba actualmente:

- LCG con parámetros de glibc.
- LCG compatible con `java.util.Random`.
- Diferentes desplazamientos (`shift`).
- Restricciones sobre los primeros 50 resultados cronológicos.

El resultado `sat` indica que el modelo tiene una solución bajo las hipótesis
probadas. Eso no basta para considerar la semilla válida: debe predecir
correctamente resultados posteriores que no participaron en el ajuste.

El resultado `unsat` indica que no existe una semilla compatible con el modelo
probado. El resultado `unknown` indica que Z3 agotó el tiempo configurado sin
poder decidir.

## Limitaciones conocidas

El análisis actual observó una tasa de repeticiones adyacentes muy inferior a la
esperada para 19 resultados independientes. Esto puede indicar una regla
anti-repetición o postprocesamiento. En ese caso, el valor publicado no es
necesariamente una salida directa del estado interno del PRNG.

Con solo el CSV no se puede distinguir de forma fiable entre:

- Un PRNG local con semilla.
- Un generador criptográfico.
- Un resultado producido por un servidor.
- Una secuencia precomputada.
- Un PRNG seguido de filtros o reglas de negocio.

Por ese motivo, los porcentajes son rankings estadísticos y no garantías de
acierto. El proyecto no debe utilizarse para asumir que un número concreto será
el siguiente ni para automatizar apuestas.

## Flujo recomendado

1. Ejecutar el dashboard local, que sincroniza, analiza y guarda el registro
   vivo en un solo proceso:

   ```powershell
   python .\web_app.py
   ```

2. Si se prefiere trabajar por consola, ejecutar el sincronizador:

   ```powershell
   python .\sync_ruleta_express.py
   ```

3. Ejecutar el predictor:

   ```powershell
   python .\state_recovery.py
   ```

4. Guardar los resultados reales posteriores en el CSV mediante el sincronizador.

5. Repetir el backtesting sin cambiar manualmente el modelo por cada resultado.

6. Considerar que existe una mejora solo si las métricas fuera de muestra
   superan de forma estable las líneas base.

## Motor de alertas M4

El paquete `m4_engine` implementa la estrategia descrita en la «Especificación
de Estrategia Cuantitativa» (PDF) como código auditable: carga el log de
supervisión, reproduce los hallazgos del documento, los contrasta con pruebas
estadísticas y expone un motor de alertas que funciona ronda a ronda.

### Validar la estrategia

```powershell
python -m m4_engine --output informe_validacion_m4.md --json informe_validacion_m4.json
```

Opciones útiles:

- `--primary-mode any|tie|strict`: cómo se exige que M4 sea el modelo
  principal. `any` conduce la alerta con M4 y trata el resto de modelos como
  ruido de fondo; `tie` exige que M4 alcance la probabilidad máxima de la
  ronda; `strict` exige que sea el único máximo. La lectura literal del PDF es
  `strict`.
- `--probability-min`, `--probability-max`, `--max-spread`: umbrales del núcleo
  de la especificación.
- `--payout loteka180|express19|european37`: modelo de pago usado para el
  dimensionamiento del stake. Por defecto `loteka180`, el pago real (18 por peso
  en total, equilibrio 1/18 = 5,56%). `express19` es el 18:1 teórico y
  `european37` el supuesto del PDF (37 casillas, 35:1), que sólo sirve para
  contrastar sus afirmaciones: la base del juego es 1/19 = 5,26%, no 1/37.

El informe incluye la auditoría de integridad del log, el contraste de cada
afirmación del PDF, las zonas de probabilidad, las matrices de confusión, el PnL
simulado con apuestas planas, la validación cronológica fuera de muestra, el
mapa de sensibilidad de umbrales y el control de multiplicidad.

### Verificar el motor

```powershell
python .\verificar_m4_engine.py
```

Ejecuta 70 comprobaciones sin dependencias externas: carga del CSV,
reproducción de los números del PDF, integridad del log, estadística (Wilson,
binomial, Fisher, Holm-Bonferroni), módulo de Kelly, motor de alertas,
ausencia de look-ahead, coherencia de las métricas del informe y monitor en
vivo (guardado del registro, cierre de ciclo y persistencia).

### Usar el motor en tiempo real

```python
from m4_engine import AlertEngine, KellyConfig, StrategySpec, EUROPEAN_37

engine = AlertEngine(
    spec=StrategySpec(primary_mode="any"),
    payout=EUROPEAN_37,
    kelly_config=KellyConfig(fraction=0.25, max_fraction=0.02),
    bankroll=1000.0,
)

decision = engine.evaluate({
    "draw_id": 249408,
    "last_number": 7,
    "numbers": {"M1": 3, "M2": 11, "M3": 16, "M4": 9, "M5": 0,
                "H1": 14, "H2": 5, "H3": 8, "H4": 12, "H5": 17},
    "probabilities": {"M1": 0.0640, "M2": 0.0610, "M3": 0.0595, "M4": 0.0570,
                      "M5": 0.0555, "H1": 0.0545, "H2": 0.0535, "H3": 0.0530,
                      "H4": 0.0525, "H5": 0.0520},
})

if decision.fired:
    print(decision.number, decision.confidence, decision.stake.stake_fraction)

engine.observe(decision, resultado_real)  # cierra el ciclo tras el sorteo
```

`evaluate` no usa el resultado de la ronda: sólo `observe` actualiza los
estimadores, de modo que el dimensionamiento del stake se basa siempre en
evidencia ya resuelta.

Este ciclo es exactamente el que automatiza el dashboard: `live_monitor.py`
llama a `evaluate` antes de cada sorteo, guarda la decisión y llama a
`observe` cuando llega el resultado. La demostración completa está en:

```powershell
python .\ejemplo_alertas_en_vivo.py
```

### Stake dinámico (Criterio de Kelly defensivo)

`KellyStakeSizer` no aplica Kelly pleno. Encadena cinco cautelas:

1. Encogimiento bayesiano de la tasa de acierto hacia el punto de equilibrio
   del modelo de pago.
2. Uso del extremo inferior del intervalo de Wilson en lugar del estimador
   puntual.
3. Kelly fraccional (25% por defecto).
4. Exigencia de ventaja mínima frente al equilibrio.
5. Tope duro de exposición por ronda y comprobación de que el crecimiento
   esperado del logaritmo de la riqueza sigue siendo positivo.

Mientras no se acumulen suficientes observaciones y aciertos en la cohorte de
alertas, el motor no arriesga capital: devuelve `stake = 0`.

### Qué permite y qué no permite el histórico actual

El informe de validación sobre `registros_supervision_probs_120` concluye que:

- La calibración por zonas del PDF se reproduce exactamente: 0% por debajo del
  5%, 6,46% entre 5% y 7%, 2,19% por encima del 8%, y 13,75% para M4 dentro de
  la ventana operativa.
- La lectura literal del «aislamiento de modelo» no emite ninguna alerta: M4
  alcanza la probabilidad máxima en seis rondas y en ninguna de ellas P(M4) cae
  dentro de la ventana 5,0%–7,0%.
- El filtro de dispersión (Δ ≤ 4,7%) descarta cuatro rondas de la ventana,
  entre ellas dos aciertos, y reduce la tasa observada de 13,75% a 11,84%.
- El acelerador de anclaje {1, 7, 8} no es estadísticamente significativo
  (Fisher p = 0,64): se conserva la etiqueta «Crítico», no el aumento de stake.
- La línea base correcta es 1/19 = 5,26%, no 1/37 = 2,70%: la ventaja real es
  de 2,6 veces, no de 5 veces.
- El 29% del log tiene etiquetas no verificables (predicciones reescritas con
  resultados contradictorios) y debe excluirse de cualquier cálculo.
- La ventaja está concentrada en el número 2: sin él, la tasa cae a 11,90% y
  deja de ser significativa frente a 1/19.

Por tanto, la estrategia debe operarse en modo papel, con los umbrales
congelados, acumulando evidencia fuera de muestra antes de comprometer capital.
