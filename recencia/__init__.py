"""Modelo de recencia para Ruleta Express (ver PLAN_MEJORAS_RECENCIA.md).

P(v) ∝ w[régimen][cubeta(antigüedad de v)], con la antigüedad medida en
`No_Sorteo` (respeta los huecos) y el régimen diurno tomado de
`papel.modelo.ventana_diurna()`. No apuesta: sólo predice y mide.

Uso:
    python -m recencia backtest     # walk-forward 60/10/30 sobre el CSV
"""

from papel import K, PAGO  # noqa: F401  (una sola fuente para la base 1/19 y el pago)
