"""Motor de alertas de alta ventaja (orientado a objetos).

`AlertEngine` consume una ronda en tiempo real, decide si cumple las tres
condiciones booleanas del núcleo de la especificación y, cuando las cumple,
dimensiona el stake con el módulo de Kelly. El motor mantiene un estimador
online que sólo se actualiza con resultados ya resueltos: nunca se usa el
resultado de la ronda que se está evaluando.

Ciclo de vida recomendado:

    engine = AlertEngine(spec, payout)
    decision = engine.evaluate(record)      # antes del sorteo
    engine.observe(decision, resultado)      # después del sorteo
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import pandas as pd

from .config import LOTEKA_180, KellyConfig, PayoutModel, StrategySpec
from .io import normalize_record
from .kelly import KellyStakeSizer, StakeDecision


@dataclass(frozen=True)
class AlertDecision:
    """Resultado de evaluar una ronda contra el motor de alertas."""

    fired: bool
    model: str
    number: int | None
    probability: float | None
    spread: float | None
    confidence: str
    anchor: bool
    stake: StakeDecision | None
    failures: tuple[str, ...]
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fired": self.fired,
            "model": self.model,
            "number": self.number,
            "probability": self.probability,
            "spread": self.spread,
            "confidence": self.confidence,
            "anchor": self.anchor,
            "stake": self.stake.as_dict() if self.stake else None,
            "failures": list(self.failures),
            "diagnostics": self.diagnostics,
        }


class OnlineHitRate:
    """Estimador online de la tasa de acierto con decaimiento exponencial.

    El decaimiento permite que el motor se adapte a cambios de régimen del
    generador sin depender de una ventana fija, y hace comparables cohortes
    con distinta antigüedad.
    """

    def __init__(self, half_life: float | None = None) -> None:
        self.half_life = half_life
        self._weight = 1.0
        self._weighted_hits = 0.0
        self._weighted_trials = 0.0
        self._hits = 0
        self._trials = 0

    def update(self, hit: bool) -> None:
        if self.half_life:
            self._weight *= 0.5 ** (1.0 / self.half_life)
        self._weighted_hits += self._weight * (1.0 if hit else 0.0)
        self._weighted_trials += self._weight
        self._hits += int(hit)
        self._trials += 1

    def seed(self, hits: int, trials: int) -> None:
        """Fija el estado acumulado del estimador.

        Se usa para rehidratar el motor desde un registro persistido: el
        monitor en vivo guarda los contadores de aciertos y fallos y los
        restaura al arrancar, de modo que el stake dimensionado por Kelly no
        dependa de que el proceso lleve tiempo en memoria.
        """
        if hits < 0 or trials < 0:
            raise ValueError("hits y trials no pueden ser negativos.")
        if hits > trials:
            raise ValueError("hits no puede superar a trials.")
        self._hits = int(hits)
        self._trials = int(trials)
        self._weight = 1.0
        self._weighted_hits = float(hits)
        self._weighted_trials = float(trials)

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def trials(self) -> int:
        return self._trials

    @property
    def rate(self) -> float:
        if self._weighted_trials <= 0.0:
            return 0.0
        return self._weighted_hits / self._weighted_trials


class AlertEngine:
    """Motor de alertas de alta ventaja con cierre de ciclo y stake dinámico."""

    def __init__(
        self,
        spec: StrategySpec | None = None,
        payout: PayoutModel | None = None,
        kelly_config: KellyConfig | None = None,
        stake_sizer: KellyStakeSizer | None = None,
        half_life: float | None = None,
        bankroll: float = 1.0,
    ) -> None:
        self.spec = spec or StrategySpec()
        # Sin pago explícito se usa el real de Loteka (18 por peso, equilibrio 1/18),
        # no el supuesto europeo de 37 casillas del PDF.
        self.payout = payout or LOTEKA_180
        self.kelly_config = kelly_config or KellyConfig()
        self.stake_sizer = stake_sizer or KellyStakeSizer(self.payout, self.kelly_config)
        self.bankroll = bankroll
        self.half_life = half_life
        self.window_estimator = OnlineHitRate(half_life)
        self.alert_estimator = OnlineHitRate(half_life)
        self.anchor_estimator = OnlineHitRate(half_life)
        self.history: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Evaluación
    # ------------------------------------------------------------------
    def evaluate(self, record: Mapping[str, Any] | pd.Series) -> AlertDecision:
        """Evalúa una ronda en tiempo real y devuelve la decisión de alerta."""
        normalized = normalize_record(record)
        spec = self.spec
        failures: list[str] = []
        model_key = spec.model.lower()

        numbers = normalized["numbers"]
        probabilities = normalized["probabilities"]
        missing = [
            model
            for model in spec.models_in_matrix
            if model not in numbers or model not in probabilities
        ]
        if missing:
            failures.append(f"datos incompletos: faltan {missing}")
            return self._empty_decision(failures, normalized)

        model_number = numbers[spec.model]
        model_probability = probabilities[spec.model]
        matrix = [probabilities[model] for model in spec.models_in_matrix]
        p_max = max(matrix)
        p_min = min(matrix)
        spread = p_max - p_min

        tied_models = [
            model
            for model, value in zip(spec.models_in_matrix, matrix)
            if abs(value - p_max) <= 1e-9
        ]
        primary_model = spec.models_in_matrix[matrix.index(p_max)]

        if not spec.probability_min < model_probability <= spec.probability_max:
            failures.append(
                f"P(M4)={model_probability:.2%} fuera de la ventana operativa "
                f"{spec.zone_label}"
            )
        if spread > spec.max_spread:
            failures.append(
                f"spread Δ={spread:.2%} supera el máximo permitido {spec.max_spread:.2%}"
            )
        if spec.primary_mode == "tie" and spec.model not in tied_models:
            failures.append(f"el modelo principal es {primary_model}, no {spec.model}")
        if spec.primary_mode == "strict" and tied_models != [spec.model]:
            failures.append(
                f"la probabilidad máxima la emite {primary_model}"
                + (f" (empate con {', '.join(tied_models)})" if len(tied_models) > 1 else "")
            )

        anchor = bool(normalized["last_number"] in spec.anchor_numbers)
        diagnostics: dict[str, Any] = {
            "draw_id": normalized["draw_id"],
            "last_number": normalized["last_number"],
            "primary_model": primary_model,
            "tied_max_models": tied_models,
            "p_max": p_max,
            "p_min": p_min,
            "numbers": numbers,
            "probabilities": probabilities,
            "window_hits": self.window_estimator.hits,
            "window_trials": self.window_estimator.trials,
            "alert_hits": self.alert_estimator.hits,
            "alert_trials": self.alert_estimator.trials,
        }

        if failures:
            return AlertDecision(
                fired=False,
                model=spec.model,
                number=model_number,
                probability=model_probability,
                spread=spread,
                confidence="NINGUNA",
                anchor=anchor,
                stake=None,
                failures=tuple(failures),
                diagnostics=diagnostics,
            )

        confidence = "CRITICO" if anchor else "ALTO"
        stake = self.stake_sizer.size(
            successes=self.alert_estimator.hits,
            trials=self.alert_estimator.trials,
            bankroll=self.bankroll,
            confidence=confidence,
        )
        diagnostics["stake"] = stake.as_dict()
        return AlertDecision(
            fired=True,
            model=spec.model,
            number=model_number,
            probability=model_probability,
            spread=spread,
            confidence=confidence,
            anchor=anchor,
            stake=stake,
            failures=(),
            diagnostics=diagnostics,
        )

    def evaluate_frame(self, frame: pd.DataFrame) -> list[AlertDecision]:
        """Evalúa un `DataFrame` de supervisión fila a fila, sin usar el resultado."""
        decisions: list[AlertDecision] = []
        for _, row in frame.iterrows():
            record = {
                "last_number": row.get("last_number"),
                "draw_id": row.get("draw_id"),
                "numbers": {
                    model: row.get(f"n_{model.lower()}")
                    for model in self.spec.models_in_matrix
                },
                "probabilities": {
                    model: row.get(f"p_{model.lower()}")
                    for model in self.spec.models_in_matrix
                },
            }
            decisions.append(self.evaluate(record))
        return decisions

    # ------------------------------------------------------------------
    # Cierre de ciclo
    # ------------------------------------------------------------------
    def observe(self, decision: AlertDecision, result: int | None) -> None:
        """Registra el resultado resuelto de la ronda y actualiza los estimadores."""
        if result is None or decision.number is None:
            return
        hit = bool(int(result) == int(decision.number))
        in_window = (
            decision.probability is not None
            and self.spec.probability_min < decision.probability <= self.spec.probability_max
        )
        if in_window:
            self.window_estimator.update(hit)
        if decision.fired:
            self.alert_estimator.update(hit)
            if decision.anchor:
                self.anchor_estimator.update(hit)
        self.history.append(
            {
                "fired": decision.fired,
                "hit": hit,
                "number": decision.number,
                "result": int(result),
                "confidence": decision.confidence,
            }
        )

    def backfill(self, frame: pd.DataFrame) -> None:
        """Carga un histórico para que el motor disponga de evidencia previa."""
        for _, row in frame.iterrows():
            record = {
                "last_number": row.get("last_number"),
                "draw_id": row.get("draw_id"),
                "numbers": {
                    model: row.get(f"n_{model.lower()}")
                    for model in self.spec.models_in_matrix
                },
                "probabilities": {
                    model: row.get(f"p_{model.lower()}")
                    for model in self.spec.models_in_matrix
                },
            }
            decision = self.evaluate(record)
            self.observe(decision, row.get("result"))

    def reset(self) -> None:
        self.window_estimator = OnlineHitRate(self.half_life)
        self.alert_estimator = OnlineHitRate(self.half_life)
        self.anchor_estimator = OnlineHitRate(self.half_life)
        self.history.clear()

    def seed_cohorts(
        self,
        window: tuple[int, int] = (0, 0),
        alert: tuple[int, int] = (0, 0),
        anchor: tuple[int, int] = (0, 0),
    ) -> None:
        """Restaura las tres cohortes desde contadores acumulados.

        `window` es la cohorte de rondas con P(M4) dentro de la ventana
        operativa, `alert` la de alertas emitidas y `anchor` la de alertas
        con anclaje. Cada par es ``(aciertos, rondas)``.
        """
        self.window_estimator.seed(*window)
        self.alert_estimator.seed(*alert)
        self.anchor_estimator.seed(*anchor)
        self.history.clear()

    def _empty_decision(
        self, failures: list[str], normalized: Mapping[str, Any]
    ) -> AlertDecision:
        return AlertDecision(
            fired=False,
            model=self.spec.model,
            number=None,
            probability=None,
            spread=None,
            confidence="NINGUNA",
            anchor=False,
            stake=None,
            failures=tuple(failures),
            diagnostics={"draw_id": normalized.get("draw_id")},
        )
