"""Bloque `recencia` de la respuesta de /api/dashboard.

Se llama en cada petición del panel y en el latido /api/cron: evalúa lo
pendiente, actualiza el monitor, registra la predicción del próximo sorteo
(una sola vez) y devuelve el resumen. Nunca lanza: un fallo vuelve como
{"error": ...} para no tumbar el panel.
"""
from __future__ import annotations

from papel.datos import desde_filas, hora_de_turno

from . import registro as R
from .integridad import informe_huecos
from .config import config

ULTIMAS = 30

ADVERTENCIAS = [
    "La ventaja depende de que Loteka no cambie el generador. Si el monitor pausa, no apostar.",
    "Aun con ventaja, la varianza es alta: con Kelly fraccional, una mala racha de cientos de sorteos es normal.",
    "El pago asumido es 18 por peso en total (MODELO_PAGO = loteka180). Confírmalo en la agencia.",
    "El stake es sólo una sugerencia: este programa no apuesta.",
]


def panel(rows, almacen, persistente: bool = True) -> dict:
    try:
        cfg = config()
        if not cfg.activo:
            return {"activo": False, "advertencias": ADVERTENCIAS}
        d = desde_filas(rows)
        if len(d) < 400:
            return {"error": "Histórico insuficiente para el modelo de recencia."}
        c = R.ciclo(d, almacen, cfg)
        m, dec = c["modelo"], c["decision"]
        filas = R.leer(almacen)
        objetivo = str(int(d.sorteo[-1]) + 1)
        proxima = next((f for f in reversed(filas) if f["sorteo_objetivo"] == objetivo), None)
        evaluadas = [f for f in filas if f["resultado"] != ""]
        return {
            "activo": True,
            "regimen": m.regimen(d, len(d)),
            "ventana": None if m.ventana is None else [hora_de_turno(m.ventana[0]), hora_de_turno(m.ventana[1])],
            "monitor": c["monitor"],
            "proxima": proxima,
            "aciertos": c["aciertos"],
            "ultimas": list(reversed(evaluadas[-ULTIMAS:])),
            "modelo": {"metodo": m.metodo, "hasta_sorteo": m.hasta_sorteo, "ajustado_utc": m.ajustado_utc,
                       "multiplicadores": m.multiplicadores(), "reajustado_ahora": c["reajustado"]},
            "decision": {"k": dec["k"], "origen": dec.get("origen"), "con_evidencia": dec.get("evidencia") is not None},
            "config": {"k_max": cfg.k_max, "fraccion_kelly": cfg.fraccion_kelly, "margen": cfg.margen,
                       "tope_ronda": cfg.tope_ronda, "pago": cfg.pago, "banco": cfg.banco},
            "integridad": informe_huecos(d),
            "persistente": persistente,
            "advertencias": ADVERTENCIAS,
        }
    except Exception as error:  # el panel principal no debe caer por la recencia
        return {"error": f"{type(error).__name__}: {error}"}
