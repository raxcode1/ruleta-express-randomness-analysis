"""Backtest walk-forward del modelo de recencia (60/10/30 cronológico).

- Entrenamiento: primer 60 % de los sorteos; validación: 10 %; prueba: 30 %.
  Los cortes se ajustan al inicio de una jornada.
- Cada 7 jornadas se reajusta el modelo con TODO lo anterior al bloque
  (como `papel.__main__.backtest`). Nada del bloque entra en su ajuste.
- El estimador (conteo / logit) se elige por log-loss en VALIDACIÓN; la prueba
  se ejecuta después, una sola vez, con la elección ya fija.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import optimize, stats

from m4_engine.stats import wilson_interval
from papel import K, PAGO
from papel.datos import Datos, hora_de_turno

from . import decision as DE
from .config import config as config_recencia
from .modelo import METODOS, ModeloRecencia, antiguedades

FRACCIONES = (0.6, 0.1, 0.3)
REAJUSTE = 7
KS = tuple(range(1, 7))
FRACCION_KELLY = 0.25
RAIZ = Path(__file__).resolve().parents[1]

#: Tabla 1.4 del plan (30 % final, pago 18): k -> (acierto diurno, retorno diurno, retorno fuera).
REFERENCIA_PLAN = {1: (0.0768, 1.38, 0.97), 2: (0.1434, 1.29, 1.01), 3: (0.1976, 1.19, 0.98), 5: (0.3095, 1.11, 0.94)}


# ----------------------------------------------------------------- Kelly
def kelly_k(S, k: int, pago: float = PAGO):
    """Fracción de Kelly pleno POR NÚMERO al jugar k números con prob. conjunta S.

    Con el mismo stake f en cada número: g(f) = S·log(1+f(pago−k)) + (1−S)·log(1−k·f),
    que es un Kelly binario con stake total F = k·f y cuota neta b = (pago−k)/k.
    Óptimo exacto: F* = S − (1−S)/b (0 si no es positivo).
    """
    b = (pago - k) / k
    F = np.asarray(S, float) - (1 - np.asarray(S, float)) / b
    return np.clip(F, 0, None) / k


def kelly_k_numerico(S: float, k: int, pago: float = PAGO) -> float:
    """Mismo óptimo por búsqueda numérica (para comprobar la fórmula cerrada)."""
    def menos_g(f):
        return -(S * math.log1p(f * (pago - k)) + (1 - S) * math.log1p(-k * f))
    r = optimize.minimize_scalar(menos_g, bounds=(0, (1 - 1e-9) / k), method="bounded", options={"xatol": 1e-10})
    return float(r.x) if -r.fun > 0 else 0.0


# --------------------------------------------------------------- cortes
def cortes(d: Datos, fracciones=FRACCIONES) -> tuple[int, int]:
    """Índices de inicio de validación y de prueba, alineados al inicio de jornada."""
    n = len(d)
    def inicio_jornada(i):
        return int(np.searchsorted(d.dia, d.dia[min(i, n - 1)], side="left"))
    return inicio_jornada(int(n * fracciones[0])), inicio_jornada(int(n * (fracciones[0] + fracciones[1])))


def walk_forward(d: Datos, ini: int, fin: int, metodo: str, A: np.ndarray | None = None, reajuste: int = REAJUSTE) -> dict:
    """Predice los sorteos [ini, fin) reajustando cada `reajuste` jornadas con el pasado."""
    A = antiguedades(d) if A is None else A
    P, diurno, idx, bloques = [], [], [], []
    d0 = int(d.dia[ini])
    while True:
        a = max(ini, int(np.searchsorted(d.dia, d0, side="left")))
        b = min(fin, int(np.searchsorted(d.dia, d0 + reajuste, side="left")))
        if a >= fin:
            break
        if b > a:
            m = ModeloRecencia(metodo)
            m.ajustar(d, a, A)                      # sólo sorteos < a
            ix = np.arange(a, b)
            p, di = m.probabilidades_lote(d, ix, A)
            P.append(p); diurno.append(di); idx.append(ix)
            bloques.append({"desde": int(d.sorteo[a]), "hasta": int(d.sorteo[b - 1]), "n": int(b - a),
                            "ventana": None if m.ventana is None else [hora_de_turno(m.ventana[0]), hora_de_turno(m.ventana[1])]})
        d0 += reajuste
    idx = np.concatenate(idx)
    return {"P": np.vstack(P), "diurno": np.concatenate(diurno), "idx": idx, "y": d.num[idx], "bloques": bloques}


# -------------------------------------------------------------- métricas
def _calibracion(P: np.ndarray, y: np.ndarray, bins: int = 10) -> list[dict]:
    p = P.ravel()
    obs = np.zeros_like(P)
    obs[np.arange(len(y)), y] = 1
    obs = obs.ravel()
    bordes = np.quantile(p, np.linspace(0, 1, bins + 1))
    c = np.clip(np.searchsorted(bordes, p, side="right") - 1, 0, bins - 1)
    out = []
    for i in range(bins):
        m = c == i
        if m.any():
            out.append({"decil": i + 1, "n": int(m.sum()), "p_media": float(p[m].mean()), "frecuencia": float(obs[m].mean())})
    return out


def metricas(P: np.ndarray, y: np.ndarray, ks=KS, fraccion: float = FRACCION_KELLY, pago: float = PAGO) -> dict:
    n = len(y)
    if n == 0:
        return {"n": 0}
    ll = float(-np.log(np.clip(P[np.arange(n), y], 1e-12, None)).mean())
    orden = np.argsort(-P, axis=1, kind="stable")
    res = {"n": int(n), "log_loss": ll, "log_loss_uniforme": math.log(K), "mejora_log_loss": math.log(K) - ll, "k": {}}
    for k in ks:
        top = orden[:, :k]
        hit = (top == y[:, None]).any(1)
        h = int(hit.sum())
        ic = wilson_interval(h, n)
        S = np.take_along_axis(P, top, 1).sum(1)
        f = fraccion * kelly_k(S, k, pago)
        g = np.where(hit, np.log1p(f * (pago - k)), np.log1p(-k * f))
        res["k"][k] = {
            "aciertos": h, "acierto": h / n, "ic_bajo": ic.low, "ic_alto": ic.high,
            "retorno": pago * h / (k * n),
            "p_vs_azar": float(stats.binomtest(h, n, k / K, alternative="greater").pvalue),
            "p_vs_equilibrio": float(stats.binomtest(h, n, min(1.0, k / pago), alternative="greater").pvalue),
            "prob_predicha_media": float(S.mean()),
            "crec_log_medio": float(g.mean()), "apostado_pct": float((f > 0).mean()),
            "stake_medio_total": float((k * f).mean()),
        }
    cal = _calibracion(P, y)
    res["calibracion"] = cal
    res["calibracion_max_desvio_pts"] = float(max(abs(c["frecuencia"] - c["p_media"]) for c in cal) * 100)
    return res


def por_regimen(wf: dict, **kw) -> dict:
    P, y, di = wf["P"], wf["y"], wf["diurno"]
    return {"diurno": metricas(P[di], y[di], **kw), "fuera": metricas(P[~di], y[~di], **kw), "total": metricas(P, y, **kw)}


# ------------------------------------------------------------ orquestación
def evidencia_topk(wf: dict, k_max: int) -> dict:
    """{k: (aciertos, n)} del top-k en el régimen diurno de un walk-forward (fuera de muestra)."""
    P, y = wf["P"][wf["diurno"]], wf["y"][wf["diurno"]]
    orden = np.argsort(-P, axis=1, kind="stable")
    return {k: (int((orden[:, :k] == y[:, None]).any(1).sum()), int(len(y))) for k in range(1, k_max + 1)}


def ejecutar(d: Datos, metodo: str = "auto", fraccion: float = FRACCION_KELLY, fuente: str = "", cfg=None) -> dict:
    cfg = cfg or config_recencia()
    A = antiguedades(d)
    i_val, i_test = cortes(d)
    seleccion = {}
    metodos = METODOS if metodo == "auto" else (metodo,)
    val = {}
    for m in metodos:
        wf = walk_forward(d, i_val, i_test, m, A)
        val[m] = wf
        seleccion[m] = metricas(wf["P"], wf["y"], ks=(1,))["log_loss"]
    elegido = min(seleccion, key=seleccion.get)
    # --- la prueba se mira aquí por primera y única vez, con `elegido` ya fijo ---
    test = walk_forward(d, i_test, len(d), elegido, A)
    # --- decisión (paso 3): k se elige en validación; en prueba, la evidencia es sólo la de validación ---
    wv = val[elegido]
    k_elegido, sim_val = DE.elegir_k(wv["P"], wv["y"], wv["diurno"], cfg)
    ev_val = evidencia_topk(wv, cfg.k_max)
    sim_test = DE.simular(test["P"], test["y"], test["diurno"], k_elegido, cfg, ev_val)
    sim_test_sin_ev = DE.simular(test["P"], test["y"], test["diurno"], k_elegido, cfg)
    ev_total = evidencia_topk({k: np.concatenate([wv[k], test[k]]) for k in ("P", "y", "diurno")}, cfg.k_max)
    final = ModeloRecencia(elegido)
    final.ajustar(d, i_test, A)                      # modelo al inicio de la prueba (train+val)
    def tramo(a, b):
        return {"desde_sorteo": int(d.sorteo[a]), "hasta_sorteo": int(d.sorteo[b - 1]), "n": int(b - a),
                "desde": d.ts[a].strftime("%d/%m/%Y %H:%M"), "hasta": d.ts[b - 1].strftime("%d/%m/%Y %H:%M")}
    return {
        "generado_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fuente": fuente, "n_sorteos": len(d), "huecos": int(d.sorteo[-1] - d.sorteo[0] + 1 - len(d)),
        "pago": PAGO, "fraccion_kelly": fraccion, "reajuste_jornadas": REAJUSTE,
        "tramos": {"entrenamiento": tramo(0, i_val), "validacion": tramo(i_val, i_test), "prueba": tramo(i_test, len(d))},
        "seleccion": {"log_loss_validacion": seleccion, "elegido": elegido},
        "validacion": por_regimen(val[elegido], fraccion=fraccion),
        "prueba": por_regimen(test, fraccion=fraccion),
        "bloques_prueba": test["bloques"],
        "decision": {"config": cfg.__dict__, "k_elegido": k_elegido, "validacion": sim_val,
                     "evidencia_validacion": ev_val, "prueba": sim_test, "prueba_sin_evidencia": sim_test_sin_ev,
                     "evidencia_fuera_de_muestra": ev_total},
        "modelo_inicio_prueba": {"ventana": test["bloques"][0]["ventana"], "multiplicadores": final.multiplicadores(),
                                 "n_entreno": final.n_entreno},
    }


# ---------------------------------------------------------------- informe
def _pct(x):
    return f"{100 * x:.2f}%"


def tabla_regimenes(r: dict) -> list[str]:
    out = ["| Régimen | k | n | Acierto | IC 95% Wilson | Retorno | p vs k/19 | p vs k/18 | Crec. log ¼K (×10⁴) | % apostado |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for reg in ("diurno", "fuera", "total"):
        m = r[reg]
        if not m.get("n"):
            continue
        for k, x in m["k"].items():
            out.append(f"| {reg} | {k} | {m['n']} | {_pct(x['acierto'])} | [{_pct(x['ic_bajo'])}, {_pct(x['ic_alto'])}] | "
                       f"{100 * x['retorno']:.1f}% | {x['p_vs_azar']:.2g} | {x['p_vs_equilibrio']:.2g} | "
                       f"{1e4 * x['crec_log_medio']:+.2f} | {100 * x['apostado_pct']:.0f}% |")
    return out


def tabla_calibracion(r: dict) -> list[str]:
    out = ["| Régimen | " + " | ".join(f"D{i}" for i in range(1, 11)) + " | Máx. desvío |", "|---|" + "---|" * 11]
    for reg in ("diurno", "fuera", "total"):
        m = r[reg]
        if not m.get("n"):
            continue
        celdas = [f"{100 * c['p_media']:.2f}→{100 * c['frecuencia']:.2f}" for c in m["calibracion"]]
        out.append(f"| {reg} | " + " | ".join(celdas) + f" | {m['calibracion_max_desvio_pts']:.2f} pts |")
    return out


def informe_md(r: dict) -> str:
    t = r["tramos"]
    L = ["# Informe del modelo de recencia", "",
         f"Generado {r['generado_utc']} · fuente `{r['fuente']}` · {r['n_sorteos']} sorteos ({r['huecos']} huecos) · "
         f"pago {r['pago']:.0f} por peso en total · Kelly ×{r['fraccion_kelly']} · reajuste cada {r['reajuste_jornadas']} jornadas.", ""]
    for nombre, x in t.items():
        L.append(f"- **{nombre}**: {x['desde']} → {x['hasta']} (No. {x['desde_sorteo']}–{x['hasta_sorteo']}, n={x['n']})")
    s = r["seleccion"]
    L += ["", "## Elección del estimador (sólo validación)", "",
          " · ".join(f"{m}: log-loss {v:.5f}" for m, v in s["log_loss_validacion"].items()) +
          f" → **{s['elegido']}** (uniforme: {math.log(K):.5f}).", ""]
    mi = r["modelo_inicio_prueba"]
    L += ["## Multiplicadores frente a 1/19 (ajuste con entrenamiento + validación)", "",
          f"Ventana diurna aprendida: {mi['ventana'][0]}–{mi['ventana'][1]}" if mi["ventana"] else "Sin ventana diurna.", "",
          "| Régimen | " + " | ".join(next(iter(mi["multiplicadores"].values())).keys()) + " |",
          "|---|" + "---|" * len(next(iter(mi["multiplicadores"].values())))]
    for reg, w in mi["multiplicadores"].items():
        L.append(f"| {reg} | " + " | ".join(f"×{v:.2f}" for v in w.values()) + " |")
    for nombre in ("prueba", "validacion"):
        x = r[nombre]
        L += ["", f"## {'Prueba (30 % final)' if nombre == 'prueba' else 'Validación (10 %)'}", "",
              "Log-loss: " + " · ".join(f"{reg} {x[reg]['log_loss']:.5f} (mejora {x[reg]['mejora_log_loss']:+.5f})"
                                        for reg in ("diurno", "fuera", "total") if x[reg].get("n")) + f" · uniforme {math.log(K):.5f}", ""]
        L += tabla_regimenes(x)
        L += ["", "Calibración por deciles (predicho → observado, en %):", ""] + tabla_calibracion(x)
    de = r["decision"]
    c = de["config"]
    L += ["", "## Decisión (paso 3)", "",
          f"Regla: sólo régimen diurno · top-k con pago·p > {1 + c['margen']:.2f} · Kelly ×{c['fraccion_kelly']} · "
          f"tope {100 * c['tope_ronda']:.1f}% del banco por ronda · defensas de `m4_engine/kelly.py` con la evidencia de validación.", "",
          "| Tramo | k | Sorteos | Apuestas | Acierto | Retorno | Crec. log medio (×10⁴) | Banco final (×) |", "|---|---|---|---|---|---|---|---|"]
    filas = [(f"validación (k={k})", s_) for k, s_ in de["validacion"].items()] + [
        (f"**prueba (k={de['k_elegido']})**", de["prueba"]), (f"prueba sin defensa histórica (k={de['k_elegido']})", de["prueba_sin_evidencia"])]
    for nombre, s_ in filas:
        L.append(f"| {nombre} | {s_['k']} | {s_['n']} | {s_['apuestas']} | {_pct(s_['acierto'])} | {100 * s_['retorno']:.1f}% | "
                 f"{1e4 * s_['crec_log_medio']:+.2f} | {s_['banco_final_x']:.3f} |")
    L += ["", f"k elegido por crecimiento logarítmico en validación: **{de['k_elegido']}**."]
    p = r["prueba"]
    L += ["", "## Comparación con la tabla 1.4 del plan", "",
          "| k | Diurno acierto (plan → aquí) | Diurno retorno (plan → aquí) | Fuera retorno (plan → aquí) |", "|---|---|---|---|"]
    for k, (ac, rd, rf) in REFERENCIA_PLAN.items():
        a, b = p["diurno"]["k"][k], p["fuera"]["k"][k]
        L.append(f"| {k} | {_pct(ac)} → {_pct(a['acierto'])} | {100 * rd:.0f}% → {100 * a['retorno']:.1f}% | {100 * rf:.0f}% → {100 * b['retorno']:.1f}% |")
    L += ["", "Retorno = pago · aciertos / (k · n): lo que vuelve por cada peso apostado repartido en k números. "
          "Crecimiento log = media por sorteo de log(riqueza_t/riqueza_{t−1}) apostando ¼ de Kelly cuando hay ventaja según el modelo.", ""]
    return "\n".join(L)


def _json_seguro(o):
    if isinstance(o, dict):
        return {str(k): _json_seguro(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_seguro(v) for v in o]
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    return o


def guardar(r: dict, carpeta: Path = RAIZ) -> tuple[Path, Path]:
    carpeta = Path(carpeta)
    md, js = carpeta / "informe_recencia.md", carpeta / "informe_recencia.json"
    md.write_text(informe_md(r), encoding="utf-8")
    js.write_text(json.dumps(_json_seguro(r), ensure_ascii=False, indent=2), encoding="utf-8")
    return md, js
