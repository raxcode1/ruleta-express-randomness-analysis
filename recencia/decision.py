"""Qué números jugar y cuánto (sólo sugerencia; nunca apuesta).

Para k números con probabilidades p₁..p_k y el mismo stake f en cada uno:
    g(f) = S·log(1 + f·(pago − k)) + (1 − S)·log(1 − k·f),   S = Σ pᵢ
Se maximiza g en f (minimize_scalar acotado), se aplica la fracción de Kelly y
el tope por ronda.

Defensas reutilizadas de `m4_engine/kelly.py`: la jugada de k números es una
apuesta binaria con cuota neta (pago − k)/k, así que `KellyStakeSizer.size()`
recibe el historial fuera de muestra del top-k y aplica encogimiento hacia el
equilibrio, extremo inferior de Wilson, ventaja mínima y tope. Si lo rechaza
no se juega; si lo acepta, S se acota por esa tasa defendida.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np
from scipy import optimize

from m4_engine.config import KellyConfig, PayoutModel
from m4_engine.kelly import KellyStakeSizer

from .config import ConfigRecencia, config

ACTIVO, ALERTA, PAUSADO = "activo", "alerta", "pausado"
#: Estados del monitor que permiten sugerir stake. «alerta» avisa pero no pausa.
PERMITEN_APUESTA = (ACTIVO, ALERTA)


@dataclass
class Decision:
    apostar: bool
    regimen: str
    k: int = 0
    numeros: list[int] = field(default_factory=list)
    probabilidades: list[float] = field(default_factory=list)
    prob_conjunta: float = 0.0          # S del modelo
    prob_usada: float = 0.0             # S tras las defensas
    stake_por_numero: float = 0.0       # fracción del banco en cada número
    stake_total: float = 0.0            # k · stake_por_numero
    monto_por_numero: float = 0.0
    crecimiento_esperado: float = 0.0   # g(f) con prob_usada
    motivo: str = ""

    def a_dict(self) -> dict:
        return asdict(self)


def crecimiento(S: float, k: int, f: float, pago: float) -> float:
    if f <= 0:
        return 0.0
    if k * f >= 1:
        return float("-inf")
    return S * math.log1p(f * (pago - k)) + (1 - S) * math.log1p(-k * f)


def f_kelly(S: float, k: int, pago: float) -> float:
    """f por número que maximiza g(f); 0 si no hay crecimiento positivo."""
    if k <= 0 or S * pago <= k:          # sin ventaja: g'(0) = S·pago − k ≤ 0
        return 0.0
    r = optimize.minimize_scalar(lambda f: -crecimiento(S, k, f, pago), bounds=(0.0, (1 - 1e-9) / k),
                                 method="bounded", options={"xatol": 1e-10})
    return float(r.x) if -r.fun > 0 else 0.0


def _sizer(k: int, c: ConfigRecencia) -> KellyStakeSizer:
    pago_k = PayoutModel(f"recencia_top{k}", pockets=19, net_multiplier=(c.pago - k) / k)
    return KellyStakeSizer(pago_k, KellyConfig(fraction=c.fraccion_kelly, max_fraction=c.tope_ronda,
                                               min_observations=200, min_hits=20))


def decidir(p, regimen: str, k: int, c: ConfigRecencia | None = None, evidencia: dict | None = None,
            estado_monitor: str = ACTIVO) -> Decision:
    """Decisión para un sorteo.

    p: probabilidades (19,) del modelo. k: número de jugadas elegido en validación.
    evidencia: {k: (aciertos, n)} del top-k diurno fuera de muestra; None = sin defensa histórica
    (sólo para simular en validación, donde aún no existe esa evidencia).
    """
    c = c or config()
    p = np.asarray(p, float)
    d = Decision(apostar=False, regimen=regimen)
    if not c.activo:
        d.motivo = "RECENCIA_ACTIVO = False"
        return d
    if regimen != "diurno":
        d.motivo = "fuera de la franja diurna: no se apuesta"
        return d
    if estado_monitor not in PERMITEN_APUESTA:
        d.motivo = f"monitor en «{estado_monitor}»: no se apuesta"
        return d
    k = max(1, min(int(k), c.k_max))
    top = np.argsort(-p, kind="stable")[:k]
    cand = [int(v) for v in top if c.pago * p[v] > 1 + c.margen]
    if not cand:
        d.motivo = f"ningún número con pago·p > {1 + c.margen:.2f}"
        return d
    ke = len(cand)
    S = float(p[cand].sum())
    d.k, d.numeros, d.probabilidades, d.prob_conjunta = ke, cand, [float(p[v]) for v in cand], S
    S_usada = S
    if evidencia is not None:
        ac, n = evidencia.get(ke, evidencia.get(str(ke), (0, 0)))
        r = _sizer(ke, c).size(int(ac), int(n), c.banco)
        if not r.applied:
            d.motivo = "evidencia histórica insuficiente: " + "; ".join(r.notes)
            return d
        S_usada = min(S, r.rate_used)
    d.prob_usada = S_usada
    f = c.fraccion_kelly * f_kelly(S_usada, ke, c.pago)
    f = min(f, c.tope_ronda / ke)
    g = crecimiento(S_usada, ke, f, c.pago)
    if f <= 0 or g <= 0:
        d.motivo = "sin crecimiento esperado positivo"
        return d
    d.apostar, d.stake_por_numero, d.stake_total = True, f, ke * f
    d.monto_por_numero, d.crecimiento_esperado = round(c.banco * f, 2), g
    d.motivo = f"jugar {ke} número(s), ventaja {c.pago * S_usada / ke - 1:+.1%} según el modelo"
    return d


def simular(P: np.ndarray, y: np.ndarray, diurno: np.ndarray, k: int, c: ConfigRecencia | None = None,
            evidencia: dict | None = None) -> dict:
    """Aplica `decidir` sorteo a sorteo y mide el resultado real."""
    c = c or config()
    logs, apuestas, aciertos, apostado, cobrado = [], 0, 0, 0.0, 0.0
    for p, r, di in zip(P, y, diurno):
        d = decidir(p, "diurno" if di else "fuera", k, c, evidencia)
        if not d.apostar:
            logs.append(0.0)
            continue
        hit = int(r) in d.numeros
        apuestas += 1
        aciertos += hit
        apostado += d.stake_total
        cobrado += c.pago * d.stake_por_numero if hit else 0.0
        logs.append(math.log1p(d.stake_por_numero * (c.pago - d.k)) if hit else math.log1p(-d.stake_total))
    logs = np.array(logs)
    return {"k": k, "n": int(len(y)), "apuestas": apuestas, "aciertos": aciertos,
            "acierto": aciertos / apuestas if apuestas else 0.0,
            "retorno": cobrado / apostado if apostado else 0.0,
            "crec_log_medio": float(logs.mean()) if len(logs) else 0.0,
            "crec_log_total": float(logs.sum()), "banco_final_x": float(math.exp(logs.sum()))}


def elegir_k(P, y, diurno, c: ConfigRecencia | None = None) -> tuple[int, dict]:
    """k = 1..k_max con mayor crecimiento logarítmico medio. Llamar SÓLO con validación."""
    c = c or config()
    res = {k: simular(P, y, diurno, k, c) for k in range(1, c.k_max + 1)}
    return max(res, key=lambda k: res[k]["crec_log_medio"]), res
