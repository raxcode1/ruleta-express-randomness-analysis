"""Parámetros del modelo de recencia. Se editan en `config_alertas.py`."""
from __future__ import annotations

from dataclasses import dataclass

import config_alertas


@dataclass(frozen=True)
class ConfigRecencia:
    activo: bool = True
    k_max: int = 3
    fraccion_kelly: float = 0.25
    margen: float = 0.05          # se juega v sólo si pago·p(v) > 1 + margen
    tope_ronda: float = 0.03      # fracción máxima del banco en una ronda (suma de los k números)
    pago: float = 18.0            # retorno total por peso apostado a un número
    banco: float = 1000.0

    def __post_init__(self) -> None:
        if not 1 <= self.k_max < self.pago:
            raise ValueError("k_max debe estar entre 1 y pago-1.")
        if not 0.0 < self.fraccion_kelly <= 1.0:
            raise ValueError("fraccion_kelly debe estar en (0, 1].")
        if not 0.0 < self.tope_ronda <= 1.0:
            raise ValueError("tope_ronda debe estar en (0, 1].")
        if self.margen < 0:
            raise ValueError("margen no puede ser negativo.")


def config() -> ConfigRecencia:
    c = config_alertas
    return ConfigRecencia(
        activo=bool(getattr(c, "RECENCIA_ACTIVO", True)),
        k_max=int(getattr(c, "RECENCIA_K_MAX", 3)),
        fraccion_kelly=float(getattr(c, "RECENCIA_FRACCION_KELLY", 0.25)),
        margen=float(getattr(c, "RECENCIA_MARGEN", 0.05)),
        tope_ronda=float(getattr(c, "RECENCIA_TOPE_RONDA", 0.03)),
        pago=float(c.modelo_pago().net_multiplier + 1.0),
        banco=float(getattr(c, "BANCO_INICIAL", 1000.0)),
    )
