"""Monitor de cambios del generador: interruptor automático del modelo de recencia.

Tres vigilancias, todas sobre datos ya publicados:

1. Repetición inmediata diurna (7 jornadas): `papel.modelo.regimen_vigente()`.
   Si supera MAX_TASA_REPETICION (1 %) el régimen se da por roto -> «pausado».
   Se levanta solo cuando las 7 jornadas vuelven a estar limpias.
2. CUSUM (Page) del log-cociente de verosimilitud del acierto del top-k diurno
   registrado en vivo: H0 = tasa esperada del backtest, H1 = equilibrio k/pago.
   Si el estadístico supera el umbral -> «pausado». Esta pausa es PEGAJOSA:
   sólo se levanta con `reanudar()`, y el CUSUM vuelve a contar desde ahí.
3. Deriva de calibración: log-loss de las últimas 500 predicciones diurnas
   frente a la uniforme (ln 19). Si deja de mejorarla -> «alerta» (no pausa).

El estado se guarda en `recencia_monitor.json` (Supabase o archivo local).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from papel import K
from papel import modelo as PM
from papel.datos import Datos, hora_de_turno

from .decision import ACTIVO, ALERTA, PAUSADO

OBJETO = "recencia_monitor.json"
#: Simulado con p0 = 19.76 %, p1 = 3/18: h = 7 da ~2 % de falsas alarmas en 7 900
#: sorteos diurnos (~55 jornadas) y detecta el equilibrio en una mediana de
#: ~1 750 sorteos diurnos (~12 jornadas). h = 5 daba ~14 % de falsas alarmas.
UMBRAL_CUSUM = 7.0
MIN_CUSUM = 100          # aciertos/fallos diurnos mínimos para que el CUSUM opine
VENTANA_CALIBRACION = 500


def _filas_validas(filas: list[dict], desde_sorteo: int | None = None) -> list[dict]:
    out = []
    for f in filas:
        if (f.get("regimen") != "diurno" or f.get("valida", "1") != "1" or f.get("valida_publicada", "") == "0"
                or f.get("acierto_topk", "") == ""):
            continue
        if desde_sorteo is not None and int(f["sorteo_objetivo"]) <= desde_sorteo:
            continue
        out.append(f)
    return sorted(out, key=lambda f: int(f["sorteo_objetivo"]))


def cusum(aciertos: list[int], p0: float, p1: float) -> dict:
    """CUSUM de Page a favor de H1 (p1 < p0). Devuelve el estadístico final y el máximo."""
    if not 0 < p1 < p0 < 1:
        raise ValueError("Se requiere 0 < p1 (equilibrio) < p0 (esperada) < 1.")
    lh, lm = math.log(p1 / p0), math.log((1 - p1) / (1 - p0))
    s = smax = 0.0
    alarma_en = None
    for i, a in enumerate(aciertos):
        s = max(0.0, s + (lh if a else lm))
        if s > smax:
            smax = s
        if alarma_en is None and s >= UMBRAL_CUSUM:
            alarma_en = i
    return {"estadistico": s, "maximo": smax, "alarma_en": alarma_en}


def evaluar(d: Datos, filas: list[dict], esperado: dict, previo: dict | None = None) -> dict:
    """Estado nuevo del monitor.

    esperado: {"k": k, "p_esperada": tasa del top-k diurno fuera de muestra, "pago": 18}.
    previo: estado anterior (para la pausa pegajosa y la reanudación manual).
    """
    previo = previo or {}
    motivos, estado = [], ACTIVO

    ventana = PM.ventana_diurna(d)
    ok, detalle = PM.regimen_vigente(d, ventana)
    rep = {"ok": ok, "detalle": detalle,
           "ventana": None if ventana is None else [hora_de_turno(ventana[0]), hora_de_turno(ventana[1])]}
    if not ok:
        estado = PAUSADO
        motivos.append(f"régimen diurno no vigente: {detalle}")

    k, pago = int(esperado["k"]), float(esperado.get("pago", 18.0))
    p0, p1 = float(esperado["p_esperada"]), k / pago
    desde = previo.get("reanudado_en_sorteo")
    val = _filas_validas(filas, desde)
    ac = [int(f["acierto_topk"]) for f in val]
    cu = {"k": k, "p_esperada": p0, "p_equilibrio": p1, "umbral": UMBRAL_CUSUM, "n": len(ac), "aciertos": sum(ac),
          "tasa": sum(ac) / len(ac) if ac else None, "desde_sorteo": desde, "estadistico": 0.0, "alarma": False}
    if len(ac) >= MIN_CUSUM:
        c = cusum(ac, p0, p1)
        cu["estadistico"] = round(c["estadistico"], 4)
        if c["alarma_en"] is not None:
            cu["alarma"] = True
            cu["alarma_en_sorteo"] = int(val[c["alarma_en"]]["sorteo_objetivo"])
    pausa = previo.get("pausa_cusum")
    if cu["alarma"] and not pausa:
        pausa = {"desde_sorteo": cu["alarma_en_sorteo"], "utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if pausa:
        estado = PAUSADO
        motivos.append(f"CUSUM: el acierto del top-{k} cayó hacia el equilibrio ({100 * p1:.2f}%) "
                       f"desde el sorteo {pausa['desde_sorteo']}; requiere reanudar a mano")

    pr = [float(f["p_resultado"]) for f in val if f.get("p_resultado", "") != ""][-VENTANA_CALIBRACION:]
    cal = {"n": len(pr), "ventana": VENTANA_CALIBRACION, "uniforme": math.log(K), "log_loss": None, "mejora": None, "alerta": False}
    if len(pr) >= VENTANA_CALIBRACION:
        ll = -sum(math.log(max(p, 1e-12)) for p in pr) / len(pr)
        cal.update(log_loss=round(ll, 5), mejora=round(math.log(K) - ll, 5), alerta=ll >= math.log(K))
        if cal["alerta"]:
            motivos.append(f"calibración: log-loss de las últimas {len(pr)} ({ll:.4f}) ya no mejora la uniforme")
            if estado == ACTIVO:
                estado = ALERTA

    return {"estado": estado, "motivos": motivos, "actualizado_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ultimo_sorteo": int(d.sorteo[-1]) if len(d) else None,
            "repeticion": rep, "cusum": cu, "calibracion": cal,
            "pausa_cusum": pausa, "reanudado_en_sorteo": desde}


def reanudar(previo: dict, ultimo_sorteo: int) -> dict:
    """Levanta la pausa del CUSUM; las filas ≤ ultimo_sorteo dejan de contar."""
    nuevo = dict(previo or {})
    nuevo["pausa_cusum"] = None
    nuevo["reanudado_en_sorteo"] = int(ultimo_sorteo)
    return nuevo
