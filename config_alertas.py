"""Configuración operativa del monitor en vivo y de la alerta M4.

Este archivo es el único punto de edición para los parámetros de operación del
monitor: qué modelo de pago se usa, cuánto capital se toma como referencia,
qué ventana de probabilidad se vigila y cuántas rondas se guardan.

Los umbrales por defecto reproducen la «Especificación de Estrategia
Cuantitativa» con la corrección que impose el espacio real de resultados
(0..18): la línea base de acierto es 1/19 = 5,26% y no 1/37 = 2,70%.
"""

from __future__ import annotations

from m4_engine.config import EUROPEAN_37, EXPRESS_19, LOTEKA_180, StrategySpec

#: Capital de referencia, en unidades, para dimensionar el stake del alert engine.
BANCO_INICIAL = 1000.0

#: Modelo de pago del stake: 'loteka180' (pago real: 180 en total por cada
#: 10 apostados, 17:1 neto), 'express19' (18:1 teórico) o 'european37' (PDF).
MODELO_PAGO = "loteka180"

#: Reglas M4 (ventana 5–7 %, Δ ≤ 4,7 %, anclaje {1, 7, 8}). DESACTIVADAS por
#: defecto: salen de un log de 80 filas útiles, el anclaje tiene Fisher p = 0,639
#: («NO CONFIRMADO», informe_validacion_m4.md) y el 29 % del log es contradictorio.
#: Con False se siguen evaluando y registrando, pero la alerta no se dispara ni
#: sugiere stake. El código no se borra, por trazabilidad.
M4_ACTIVO = False

#: Lectura del aislamiento de modelo: 'any' | 'tie' | 'strict'.
MODO_PRIMARIO = "any"

#: Ventana operativa de probabilidad de M4 (5,0% < P(M4) <= 7,0%).
LIMITE_INFERIOR = 0.05
LIMITE_SUPERIOR = 0.07

#: Dispersión máxima admitida entre el modelo más y el menos probable.
DISPERSION_MAXIMA = 0.047

#: Números de anclaje (factor acelerador de la especificación).
NUMEROS_ANCLA = (1, 7, 8)

#: Rondas retroanalizadas al crear el registro. 0 = no reconstruir;
#: solo se guardan las predicciones tomadas en vivo.
RONDAS_BACKFILL = 0

#: Rondas que se conservan en el registro. 0 = conservar todas.
RONDAS_GUARDADAS = 0

#: Archivo local donde se guarda el registro.
ARCHIVO_REGISTRO = "registro_live.json"

#: Stake: fracción de Kelly, tope de exposición y mínimos de evidencia.
FRACCION_KELLY = 0.25
TOPE_EXPOSICION = 0.02
OBSERVACIONES_MINIMAS = 20
ACIERTO_MINIMO = 4

#: Modelo de recencia (paquete `recencia/`, ver PLAN_MEJORAS_RECENCIA.md).
#: Sólo sugiere: nunca apuesta. Fuera de la franja diurna o con el monitor en
#: «pausado» el stake sugerido es 0.
RECENCIA_ACTIVO = True
RECENCIA_K_MAX = 3              # máximo de números por ronda (k se elige en validación)
RECENCIA_FRACCION_KELLY = 0.25  # ¼ de Kelly (el plan admite de ¼ a ½)
RECENCIA_MARGEN = 0.05          # jugar v sólo si pago·p(v) > 1 + margen
RECENCIA_TOPE_RONDA = 0.03      # exposición máxima por ronda (suma de los k números)

#: Modelos de pago disponibles para el dimensionamiento del stake.
MODELOS_PAGO = {
    "loteka180": LOTEKA_180,
    "express19": EXPRESS_19,
    "european37": EUROPEAN_37,
}


def modelo_pago():
    """Devuelve el `PayoutModel` configurado para el stake."""
    return MODELOS_PAGO[MODELO_PAGO]


def spec() -> StrategySpec:
    """Devuelve la `StrategySpec` operativa del monitor en vivo."""
    return StrategySpec(
        probability_min=LIMITE_INFERIOR,
        probability_max=LIMITE_SUPERIOR,
        max_spread=DISPERSION_MAXIMA,
        anchor_numbers=frozenset(NUMEROS_ANCLA),
        primary_mode=MODO_PRIMARIO,
    )
