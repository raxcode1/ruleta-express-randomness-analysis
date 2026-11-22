"""De dónde salen los sorteos: el CSV local o el de Supabase Storage.

Supabase se lee con las mismas funciones que `api/dashboard.py`
(`blob_request`, `read_csv_bytes`) y con las mismas variables de entorno
(`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_BUCKET`). Las claves
nunca se imprimen ni se guardan.

A diferencia de `dashboard.read_rows()`, que vuelve en silencio al CSV local si
Supabase falla, aquí `fuente="supabase"` falla con un error: un backtest debe
decir con qué datos se hizo.
"""
from __future__ import annotations

from pathlib import Path

from papel.datos import CSV_PATH, Datos, cargar, desde_filas

FUENTES = ("local", "supabase", "auto")
OBJETO_CSV = "Resultados_RuletaExpress.csv"


def _dashboard():
    from api import dashboard   # import perezoso: trae dependencias del panel
    return dashboard


def supabase_configurado() -> bool:
    D = _dashboard()
    return bool(D.SUPABASE_URL and D.SUPABASE_KEY and D.SUPABASE_BUCKET)


def filas_supabase() -> list[dict[str, str]]:
    D = _dashboard()
    if not supabase_configurado():
        raise RuntimeError("Supabase no configurado: faltan SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY o SUPABASE_BUCKET.")
    return D.read_csv_bytes(D.blob_request("GET", OBJETO_CSV))


def cargar_datos(fuente: str = "local", csv: Path = CSV_PATH) -> tuple[Datos, str]:
    """Devuelve (Datos, descripción de la fuente). 'auto' = Supabase si está configurado."""
    if fuente not in FUENTES:
        raise ValueError(f"fuente debe ser una de {FUENTES}")
    if fuente == "auto":
        fuente = "supabase" if supabase_configurado() else "local"
    if fuente == "supabase":
        return desde_filas(filas_supabase()), f"supabase:{_dashboard().SUPABASE_BUCKET}/{OBJETO_CSV}"
    return cargar(csv), f"local:{Path(csv).name}"
