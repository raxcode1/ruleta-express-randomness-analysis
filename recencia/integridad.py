"""Integridad de datos: huecos en No_Sorteo, orden y duplicados, validez del libro.

- `informe_huecos(d)`: cuántos sorteos faltan y en qué tramos (para el panel).
- `reparar(filas)`: pide a Loteka (histórico diario, `backfill_missing_draws`)
  los sorteos que faltan en TODO el rango y devuelve las filas combinadas,
  ordenadas y sin duplicados por número de sorteo. Nunca devuelve menos filas
  ni pierde un sorteo que ya estaba.
- `numero_sorteo(texto)`: «No. 123», «No.123» o «123» -> 123. Es la única
  clave con la que se ordena y se quitan duplicados.

Muchos huecos antiguos no los publica Loteka; quedarán como huecos aunque se
intente repararlos.
"""
from __future__ import annotations

import re
from datetime import timedelta

import numpy as np

from papel.datos import Datos

TRAMOS_PANEL = 10
_NUM = re.compile(r"(\d+)\s*$")


def numero_sorteo(texto) -> int | None:
    m = _NUM.search(str(texto or ""))
    return int(m.group(1)) if m else None


def ordenar_sin_duplicados(filas: list[dict[str, str]]) -> list[dict[str, str]]:
    """Del más reciente al más antiguo, una fila por número de sorteo (gana la última vista)."""
    por_id: dict[int, dict[str, str]] = {}
    for f in filas:
        n = numero_sorteo(f.get("No_Sorteo"))
        if n is not None:
            por_id[n] = f
    return [por_id[n] for n in sorted(por_id, reverse=True)]


def tramos(d: Datos) -> list[dict]:
    """Tramos de No_Sorteo ausentes, del más reciente al más antiguo."""
    if len(d) < 2:
        return []
    salto = np.flatnonzero(np.diff(d.sorteo) > 1)
    out = []
    for i in salto[::-1]:
        a, b = int(d.sorteo[i]) + 1, int(d.sorteo[i + 1]) - 1
        out.append({"desde": a, "hasta": b, "faltan": b - a + 1,
                    "entre": [d.ts[i].strftime("%d/%m/%Y %H:%M"), d.ts[i + 1].strftime("%d/%m/%Y %H:%M")]})
    return out


def informe_huecos(d: Datos, dias_recientes: int = 30) -> dict:
    if not len(d):
        return {"sorteos": 0, "faltan": 0, "tramos": 0, "ultimos_tramos": []}
    t = tramos(d)
    corte = d.ts[-1] - timedelta(days=dias_recientes)
    recientes = [x for x in t if d.ts[int(np.searchsorted(d.sorteo, x["hasta"] + 1))] >= corte]
    return {"sorteos": int(len(d)), "primero": int(d.sorteo[0]), "ultimo": int(d.sorteo[-1]),
            "faltan": int(sum(x["faltan"] for x in t)), "tramos": len(t),
            "faltan_recientes": int(sum(x["faltan"] for x in recientes)), "dias_recientes": dias_recientes,
            "ultimos_tramos": t[:TRAMOS_PANEL],
            "reparar": "python -m recencia integridad --reparar [--fuente local|supabase]"}


def reparar(filas: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    """Consulta a Loteka los huecos de todo el rango. Devuelve (filas combinadas, recuperados)."""
    import pandas as pd

    from sync_ruleta_express import EXPECTED_COLUMNS, backfill_missing_draws

    base = ordenar_sin_duplicados(filas)
    frame = pd.DataFrame(base, columns=EXPECTED_COLUMNS)
    recuperadas = backfill_missing_draws(frame, [], recent_days=10**5)
    combinadas = ordenar_sin_duplicados(base + list(recuperadas))
    antes = {numero_sorteo(f["No_Sorteo"]) for f in base}
    despues = {numero_sorteo(f["No_Sorteo"]) for f in combinadas}
    if not antes <= despues or len(combinadas) < len(base):
        raise RuntimeError("La reparación perdería sorteos; no se guarda nada.")
    return combinadas, len(despues - antes)
