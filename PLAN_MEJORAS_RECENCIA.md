# Plan de mejoras: modelo de recencia para Ruleta Express

> Documento para Claude Code. Léelo completo antes de tocar código.
> Repositorio: `raxcode1/ruleta-express-randomness-analysis`.
> Autor del análisis: auditoría estadística del 21-nov-2026 sobre 36,003 sorteos.

## 0. Reglas generales

- **No romper lo que ya funciona.** El dashboard (`api/dashboard.py`, `render_app.py`, `web_app.py`), el sincronizador y Supabase siguen operando igual. Todo cambio se agrega detrás de configuración o como módulo nuevo, y tiene tests.
- **Cero look-ahead.** Ninguna característica ni ningún entrenamiento puede usar el resultado que se predice, ni sorteos posteriores.
- **No ajustar parámetros mirando el conjunto de prueba.** Ventanas, cubetas y número de jugadas se eligen con datos de entrenamiento o validación. El tramo final de prueba se mira una sola vez.
- **Ventaja solo por estadística.** No implementar nada que use desfases del scraper para apostar con el resultado ya conocido. Eso sería fraude.
- **Secretos.** Las claves de Supabase y los tokens nunca van al frontend ni al repositorio. Leerlos solo de variables de entorno, como ya hace `api/dashboard.py`.
- Ejecutar `pytest` (y `node --test tests/worker.test.mjs` si aplica) antes y después de cada paso. Todos los tests existentes tienen que seguir pasando.

---

## 1. Hallazgos que motivan los cambios

Datos: `Resultados_RuletaExpress.csv`, columnas `Fecha,Hora,No_Sorteo,Numero`, del 27-may-2026 al 21-nov-2026. Son 36,003 sorteos únicos; faltan 334 identificadores (huecos). Los resultados van de 0 a 18, y la base equiprobable es 1/19 = 5.26%.

### 1.1 El generador no es independiente

| Prueba | Resultado |
|---|---|
| Uniformidad (χ², 18 gl) | χ² = 43.5, p = 0.0007 (el 0 sale de menos: 1,729 frente a ~1,895 esperados) |
| Transiciones x(t-1) → x(t) (χ², 324 gl) | χ² = 1,707, p < 1e-4 |
| P(x(t) = x(t-1)) | **1.16%** (esperado 5.26%) |
| P(x(t) = x(t-2)) / (t-3) / (t-4) / (t-5) | 6.78% / 6.13% / 5.62% / 5.62% |

### 1.2 El sesgo depende de la hora (lo más importante)

Tasa de repetición inmediata por hora (solo sorteos con el anterior consecutivo):

| Hora | 05 | 06 | 07 | **08–19** | 20 | 21 |
|---|---|---|---|---|---|---|
| Repetición | 5.9% | 5.2% | 3.1% | **0.0%–0.09%** | 0.8% | 4.6% |

Entre ~08:30 y ~20:30 el generador **prácticamente prohíbe repetir** el número anterior y favorece los de hace 2 a 5 sorteos. Fuera de esa franja se comporta casi como azar.

> El paquete `papel/` ya detectó esto: `ventana_diurna()` y `regimen_vigente()` en `papel/modelo.py`. Hay que reutilizarlo, no duplicarlo.

### 1.3 Modelo de recencia (multiplicador frente a 1/19)

El modelo se ajustó con el 70% inicial y se evaluó en el 30% final. Antigüedad = `No_Sorteo(t) − No_Sorteo(última aparición del número)`.

| Antigüedad | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| Diurno 08:30–20:30 | ×0.01 | ×1.36 | ×1.30 | ×1.20 | ×1.09 | ×0.93 | ×0.88 | ×0.81 |
| Fuera de franja | ×0.71 | ×1.05 | ×1.17 | ×1.01 | ×1.16 | ×0.98 | ×1.00 | ×1.05 |

### 1.4 Backtest fuera de muestra (30% final, pago 18 por peso en total)

| Jugada (top-k por probabilidad) | Diurno: acierto | Diurno: retorno | Fuera: retorno |
|---|---|---|---|
| 1 número | 7.68% | **138%** | 97% |
| 2 números | 14.34% | **129%** | 101% |
| 3 números | 19.76% | 119% | 98% |
| 5 números | 30.95% | 111% | 94% |

Sin separar por horario, el mismo modelo da 125% (top-1) y 109% (top-5). Las probabilidades que predice coinciden con las reales: en la cubeta donde predice 6.52% salen 6.35%, y donde predice 1.13% salen 1.24%.

Comparación con lo que hay ahora:
- El Markov de orden 1 (walk-forward) da 105% con top-5. Los de orden 2 y 3 se sobreajustan: hay 361 y 6,859 contextos.
- Las reglas M4 (ventana 5–7%, Δ ≤ 4.7%, anclaje {1, 7, 8}) salen de un log de 80 filas útiles. Según `informe_validacion_m4.md`, el anclaje tiene Fisher p = 0.639 («NO CONFIRMADO») y el 29% de las filas del log son contradictorias. **No hay evidencia que las sostenga.**

### 1.5 Pago

`config_alertas.py` ya usa `MODELO_PAGO = "loteka180"`: 180 en total por cada 10 apostados, es decir, **18 por peso en total** (17:1 neto). Todo el análisis usa ese valor. El punto de equilibrio es 1/18 por número.

---

## 2. Arquitectura actual relevante

| Pieza | Rol |
|---|---|
| `sync_ruleta_express.py`, `scraper_web.py`, `render_worker.py`, `cloudflare-worker-loteka.js` | Ingesta desde Loteka (a través del Worker de Cloudflare) |
| `api/dashboard.py` | Función serverless (Vercel, límite de **60 s**). Lee y escribe en **Supabase Storage** |
| `papel/` | Modo papel: logit condicional con antigüedad, ventana diurna, interruptor de régimen, libro `registro_papel.csv` |
| `live_models.py` + `live_monitor.py` | Matriz de 10 submodelos (Markov y mezclas) y alertas M4 |
| `m4_engine/` | Alertas, Kelly defensivo (`kelly.py`), validación |
| `quant/` | Modelos, validación y decisión del informe cuantitativo |
| `web/` | Frontend (`app.js` hace polling a `/api/dashboard`) |

### 2.1 Supabase (Storage, no Postgres)

- Variables de entorno (ver `api/dashboard.py`): `SUPABASE_URL` (o `NEXT_PUBLIC_SUPABASE_URL` / `SUPABASE_PROJECT_URL`), `SUPABASE_SERVICE_ROLE_KEY` (o sus alias) y `SUPABASE_BUCKET` (por defecto `ruleta-data`).
- Objetos del bucket que ya existen:
  - `Resultados_RuletaExpress.csv`: histórico (fuente de verdad en producción)
  - `ruleta_predictions.json`: registro del `LiveMonitor`
  - `registro_papel.csv`, `papel_modelo.json`, `papel_ultima_prob.json`: modo papel
- Acceso con `blob_request()` / `storage_request()`. Respetar `objeto_ausente()`: un fallo de red **no** es lo mismo que un objeto inexistente. Nunca sobrescribir un objeto con una versión más corta.
- **Objetos nuevos que se agregan** (mismo bucket, mismas funciones):
  - `recencia_modelo.json`: parámetros ajustados y metadatos (fecha, n de entrenamiento, versión)
  - `recencia_registro.csv`: libro de predicciones del modelo nuevo; solo crece
  - `recencia_monitor.json`: estado del detector de cambios

---

## 3. Cambios a implementar (en orden)

### Paso 1. Módulo `recencia/` (modelo nuevo)

Crear el paquete `recencia/` con `__init__.py`, `modelo.py`, `backtest.py`, `decision.py`, `monitor.py` y `__main__.py`.

**Datos.** Reutilizar `papel.datos.Datos` (ya trae `sorteo`, `num`, `ts`, `turno`, `dia`, `consec`). No escribir otro cargador.

**Característica principal.** Antigüedad de cada número v en el sorteo t:
`antig(v, t) = sorteo[t] − sorteo[última aparición de v antes de t]`.
Se calcula **con `No_Sorteo`, no con el índice de fila**, para respetar los huecos. Si el hueco entre t y t−1 es mayor que 0, la antigüedad 1 no está observada; marcarlo y excluir esos sorteos del entrenamiento de la cubeta 1.

**Modelo.**
- `P(v) ∝ w[régimen][cubeta(antig(v))]`, con cubetas 1, 2, 3, 4, 5, 6–8, 9–14, 15–30 y >30.
- Dos regímenes: `diurno` (dentro de `papel.modelo.ventana_diurna()`) y `fuera`.
- Ajuste por conteo con suavizado de Laplace: `w = (aciertos + 1) / (expuestos + 19)`, luego normalizar.
- Alternativa: reutilizar el logit de `papel.modelo.ajustar()`. Elegir la que tenga mejor log-loss en validación y documentar la elección.

**Interfaz.**
```python
class ModeloRecencia:
    def ajustar(self, d: Datos, hasta_indice: int) -> None: ...
    def probabilidades(self, d: Datos, indice: int) -> np.ndarray:  # shape (19,), suma 1
    def a_json(self) -> dict: ...
    @classmethod
    def desde_json(cls, data: dict) -> "ModeloRecencia": ...
```

**Tests** (`tests/test_recencia.py`):
- Las probabilidades suman 1 y no hay valores negativos.
- Sin look-ahead: cambiar `num[t]` no altera `probabilidades(d, t)`.
- Los huecos en `No_Sorteo` se reflejan en la antigüedad.
- Con una secuencia sintética anti-repetición, el modelo asigna menos de 2% al último número.
- Con una secuencia i.i.d. uniforme, todos los multiplicadores quedan cerca de 1.

### Paso 2. Backtest walk-forward con el CSV completo

`python -m recencia backtest` debe:
1. Dividir cronológicamente en **entrenamiento 60% / validación 10% / prueba 30%**.
2. Reajustar el modelo cada 7 jornadas usando solo datos pasados (como `papel.backtest`).
3. Reportar por régimen (`diurno`, `fuera`, `total`) y para k = 1..6: acierto, IC 95% de Wilson, retorno por peso, p-valor binomial contra 1/19 y contra k/18 (equilibrio), log-loss frente a uniforme, calibración por deciles y **crecimiento logarítmico medio** con Kelly fraccional.
4. Guardar `informe_recencia.md` y `informe_recencia.json`.

**Criterio de aceptación:** reproducir aproximadamente la tabla 1.4 (diurno top-1 ≈ 7.7%, top-2 ≈ 14.3%). Si sale muy distinto, investigar antes de seguir.

### Paso 3. Decisión: qué números jugar y cuánto

`recencia/decision.py`:
- **Elegir k** (cuántos números jugar) con el mayor crecimiento logarítmico **en validación**, no por acierto ni por retorno. Para pago 18 y probabilidades p₁..p_k, con fracción f por número:
  `g(f) = Σᵢ pᵢ · log(1 + f·(18 − k)) + (1 − Σ pᵢ) · log(1 − k·f)`.
  Maximizar en f numéricamente (`scipy.optimize.minimize_scalar`, acotado) y aplicar **¼ a ½ de Kelly**.
- Jugar solo números con `18·pᵢ > 1 + margen` (margen configurable, por defecto 0.05).
- **No apostar fuera del régimen diurno**, ni cuando el monitor (paso 4) diga que el régimen está roto.
- Reutilizar las defensas de `m4_engine/kelly.py` (encogimiento hacia el equilibrio, Wilson inferior, tope de exposición por ronda). Si conviene, extender `KellyConfig` para apuestas a varios números en vez de duplicar la lógica.
- Parámetros nuevos en `config_alertas.py` (o en `recencia/config.py`): `RECENCIA_ACTIVO`, `RECENCIA_K_MAX = 3`, `RECENCIA_FRACCION_KELLY = 0.25`, `RECENCIA_MARGEN = 0.05`, `RECENCIA_TOPE_RONDA = 0.03`.

### Paso 4. Monitor de cambios (interruptor automático)

`recencia/monitor.py`, con estado en `recencia_monitor.json` en Supabase:
- **Repetición inmediata diurna** en ventana móvil de 7 jornadas. Alerta si supera 1% (mismo umbral que `papel.modelo.MAX_TASA_REPETICION`). Reutilizar `regimen_vigente()`.
- **CUSUM o SPRT sobre el acierto real del top-k** frente a la tasa esperada del backtest. Si el acierto cae al nivel de equilibrio (k/18), poner `estado = "pausado"` con el motivo.
- **Deriva de calibración:** log-loss móvil de las últimas 500 predicciones frente a la uniforme. Si deja de mejorar a la uniforme, alertar.
- Exponer el estado en el payload de `/api/dashboard`, bajo `payload["recencia"]`.

### Paso 5. Integración en vivo y en el dashboard

- En `api/dashboard.py`, siguiendo el patrón del modo papel (`almacen_papel()`, líneas ~502–505):
  1. Cargar `recencia_modelo.json` desde Supabase. Si no existe, ajustar y subirlo. Si el ajuste no cabe en los 60 s de Vercel, ajustarlo en `render_worker.py` o en un endpoint de cron (`/api/cron`, protegido con `CRON_SECRET`) y en el dashboard solo leerlo.
  2. Predecir el próximo sorteo **antes** de que salga. Guardar la fila en `recencia_registro.csv` con `sorteo_objetivo`, `ts_prediccion` (UTC), `regimen`, `k`, `numeros`, `probabilidades`, `stake_sugerido` y `version_modelo`.
  3. Cuando llegue el resultado, completar `resultado`, `acierto` y `retorno`. Nunca reescribir predicciones ya hechas.
- Agregar en `web/` una tarjeta «Recencia» con: régimen actual, estado del monitor, números sugeridos con su probabilidad, stake sugerido y acierto/retorno acumulados frente al equilibrio.
- `live_models.py`: agregar un submodelo `R1` (recencia) a `LIVE_MODELS` para que aparezca en la matriz comparativa, sin cambiar `PRIMARY_MODEL_NAME` hasta que el paso 6 lo justifique.

### Paso 6. Limpiar lo que no tiene evidencia

Solo después de que el paso 2 esté verde:
- Dejar las reglas M4 (ventana 5–7%, Δ ≤ 4.7%, anclaje {1, 7, 8}) **desactivadas por defecto** con una opción en `config_alertas.py`. No borrar el código, por trazabilidad.
- Corregir cualquier referencia a la base 1/37 que quede; la correcta es 1/19.
- Actualizar `GUIA_PREDICTOR_RULETA_EXPRESS.md` con una sección del modelo de recencia y un enlace a `informe_recencia.md`.

### Paso 7. Integridad de datos

- Reportar en el dashboard cuántos sorteos faltan (334 en el snapshot) y permitir rellenarlos con `reparar_huecos_ruleta.py` / `backfill_missing_draws()`.
- Ordenar y quitar duplicados siempre por `No_Sorteo`.
- Verificar que cada fila de `recencia_registro.csv` tenga `ts_prediccion` anterior a la hora publicada del sorteo objetivo. Marcar como inválidas las que no.

---

## 4. Verificación final (checklist)

- [ ] `pytest` en verde, incluidos los tests nuevos de `tests/test_recencia.py`.
- [ ] `python -m recencia backtest` genera `informe_recencia.md` y reproduce los órdenes de magnitud de la tabla 1.4.
- [ ] En el tramo de prueba, la calibración por deciles queda dentro de ±0.5 puntos.
- [ ] El dashboard muestra la tarjeta «Recencia» y lee y escribe los tres objetos nuevos en Supabase.
- [ ] Con `SUPABASE_*` sin configurar, todo funciona en local con archivos (igual que `AlmacenArchivos`).
- [ ] Fuera de la franja diurna, o con el monitor en «pausado», el stake sugerido es 0.
- [ ] Ninguna clave aparece en `web/` ni en commits.

## 5. Advertencias para el usuario (mostrar en el dashboard)

- La ventaja depende de que Loteka no cambie el generador. Si el monitor pausa, no apostar.
- Incluso con ~29% de ventaja en top-2, la varianza es alta. Con Kelly fraccional, una mala racha de cientos de sorteos es normal.
- El pago asumido es 18 por peso en total. Hay que confirmarlo en la agencia y cambiar `MODELO_PAGO` si es otro.
- El repositorio es público. Valorar hacerlo privado para que el sesgo no se difunda.
