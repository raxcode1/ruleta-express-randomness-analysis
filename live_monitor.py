"""Monitor en vivo de Ruleta Express.

El monitor es el componente que convierte el histórico en un registro
persistente y auditable:

1. **Predicciones comparadas**: cada ronda queda asociada al sorteo que
   sirvió de base y, en cuanto aparece el siguiente resultado, se resuelve
   con el número real, la posición que ocupaba en el ranking Markov y en el
   heurístico, y si acertó o falló. El registro se guarda en disco (local) o
   en Vercel Blob (producción), de modo que la evidencia se acumula entre
   reinicios y se puede seguir la tasa de acierto ronda a ronda.

2. **Alerta M4**: antes de cada sorteo se evalúa la ronda con el
   `AlertEngine` del paquete `m4_engine` usando la matriz de diez submodelos
   en vivo. La decisión indica si se cumple la ventana de probabilidad, el
   filtro de dispersión y el anclaje, y qué stake dimensiona el módulo de
   Kelly defensivo.

3. **Contadores acumulados**: aciertos y fallos de las alertas y de las
   predicciones, con su tasa y su comparación frente a la línea base de
   1/19 = 5,26%.

El monitor nunca usa el resultado de la ronda que está evaluando: los
estimadores sólo se actualizan con sorteos ya resueltos.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from m4_engine.alerts import AlertDecision, AlertEngine
from m4_engine.config import PayoutModel, StrategySpec
from m4_engine.kelly import KellyConfig

from live_models import OUTPUT_MODULUS, LiveForecast, build_forecasts
from state_recovery import build_recovery_rankings, select_markov_model

STORE_VERSION = 3

#: Sin corte por sorteo: nunca se eliminan predicciones ya guardadas.
#: Los registros nuevos se abren en vivo y se conservan tal como se tomaron.
PREDICTIONS_START_DRAW = 0

STATUS_PENDIENTE = "pendiente"
STATUS_ACIERTO = "acierto"
STATUS_FALLO = "fallo"
STATUS_OMITIDA = "omitida_pausa"

#: Línea base equiprobable del espacio observado (0..18).
BASELINE = 1.0 / OUTPUT_MODULUS

#: Línea base de los cinco primeros candidatos de un ranking.
BASELINE_TOP5 = 5.0 / OUTPUT_MODULUS

StoreLoader = Callable[[], dict]
StoreSaver = Callable[[dict], None]


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def draw_number(draw: str) -> int:
    """Extrae el número de sorteo de una etiqueta tipo «No. 249491»."""
    return int(str(draw).rsplit(" ", 1)[-1])


def parse_draws(rows: Iterable[dict[str, str]]) -> list[tuple[int, str, int]]:
    """Normaliza las filas del CSV en una lista cronológica de sorteos."""
    draws: dict[int, tuple[int, str, int]] = {}
    for row in rows:
        label = str(row.get("No_Sorteo", "")).strip()
        if not label:
            continue
        try:
            number = draw_number(label)
            value = int(str(row.get("Numero", "")).strip())
        except ValueError:
            continue
        draws[number] = (number, label, value)
    return [draws[number] for number in sorted(draws)]


def _rank_of(ranking: list[dict], actual: int) -> int | None:
    for index, item in enumerate(ranking):
        if int(item.get("number", -1)) == int(actual):
            return index + 1
    return None


def _decision_from_dict(data: dict[str, Any]) -> AlertDecision:
    """Reconstruye una decisión serializada para poder cerrar el ciclo."""
    return AlertDecision(
        fired=bool(data.get("fired")),
        model=str(data.get("model", "M4")),
        number=data.get("number"),
        probability=data.get("probability"),
        spread=data.get("spread"),
        confidence=str(data.get("confidence", "NINGUNA")),
        anchor=bool(data.get("anchor")),
        stake=None,
        failures=tuple(data.get("failures") or ()),
        diagnostics={},
    )


def json_file_store(path: Path) -> tuple[StoreLoader, StoreSaver]:
    """Devuelve un par (cargar, guardar) sobre un archivo JSON local."""

    def load() -> dict:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"No se pudo leer {path.name}; no se sobrescribe el registro."
            ) from error
        if not isinstance(data, dict):
            raise RuntimeError(
                f"{path.name} no tiene un formato válido; no se sobrescribe."
            )
        return data

    def save(data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=path.stem,
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            Path(handle.name).replace(path)
        except OSError:
            Path(handle.name).unlink(missing_ok=True)
            raise

    return load, save


def memory_store() -> tuple[StoreLoader, StoreSaver]:
    """Store en memoria (respaldo cuando no hay almacenamiento persistente)."""
    state: dict[str, Any] = {}

    def load() -> dict:
        return state

    def save(data: dict) -> None:
        state.clear()
        state.update(data)

    return load, save


def _empty_stats() -> dict[str, int]:
    return {
        "rounds": 0,
        "alerts": 0,
        "alert_hits": 0,
        "window_rounds": 0,
        "window_hits": 0,
        "anchor_alerts": 0,
        "anchor_hits": 0,
        "markov_hits": 0,
        "markov_trials": 0,
        "markov_top1_hits": 0,
        "heuristic_hits": 0,
        "heuristic_trials": 0,
        "heuristic_top1_hits": 0,
        "pauses": 0,
    }


def _normalize_stats(raw: Any) -> dict[str, int]:
    stats = _empty_stats()
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in stats:
                try:
                    stats[key] = int(value)
                except (TypeError, ValueError):
                    continue
    return stats


def _stats_from_predictions(
    predictions: list[dict],
    pauses: list[dict],
    spec: StrategySpec,
) -> dict[str, int]:
    """Recalcula los contadores a partir de los registros conservados."""
    stats = _empty_stats()
    for record in predictions:
        if record.get("status") not in (STATUS_ACIERTO, STATUS_FALLO):
            continue

        markov_rank = record.get("markov_rank")
        heuristic_rank = record.get("heuristic_rank")
        stats["rounds"] += 1
        stats["markov_trials"] += 1
        stats["markov_hits"] += int(markov_rank is not None)
        stats["markov_top1_hits"] += int(markov_rank == 1)
        stats["heuristic_trials"] += 1
        stats["heuristic_hits"] += int(heuristic_rank is not None)
        stats["heuristic_top1_hits"] += int(heuristic_rank == 1)

        decision = record.get("decision") or {}
        probability = decision.get("probability")
        number = decision.get("number")
        actual = record.get("actual_number")
        if probability is not None:
            try:
                probability = float(probability)
            except (TypeError, ValueError):
                probability = None
            if probability is not None and spec.probability_min < probability <= spec.probability_max:
                stats["window_rounds"] += 1
                try:
                    hit = actual is not None and number is not None and int(actual) == int(number)
                except (TypeError, ValueError):
                    hit = False
                stats["window_hits"] += int(hit)

        if decision.get("fired"):
            hit = bool(record.get("alert_hit"))
            stats["alerts"] += 1
            stats["alert_hits"] += int(hit)
            if decision.get("anchor"):
                stats["anchor_alerts"] += 1
                stats["anchor_hits"] += int(hit)

    stats["pauses"] = len(pauses)
    return stats


def _ranking_entries(raw: Any) -> list[dict]:
    """Convierte un ranking guardado en la lista que usa el dashboard."""
    entries = []
    for item in list(raw or [])[:5]:
        if isinstance(item, dict):
            number = item.get("number")
            probability = item.get("probability", 0.0)
        else:
            try:
                number, probability = item[0], item[1]
            except (TypeError, IndexError, KeyError):
                continue
        try:
            entries.append({"number": int(number), "probability": float(probability)})
        except (TypeError, ValueError):
            continue
    return entries


def _matrix_parts(record: dict) -> tuple[dict, dict]:
    matrix = record.get("matrix")
    if not isinstance(matrix, dict):
        matrix = {}
    numbers = matrix.get("numbers")
    probs = matrix.get("probabilities")
    return (
        numbers if isinstance(numbers, dict) else {},
        probs if isinstance(probs, dict) else {},
    )


def _has_live_matrix(record: dict) -> bool:
    numbers, probs = _matrix_parts(record)
    return bool(numbers) and bool(probs)


def _rebuild_forecasts_from_lists(record: dict) -> None:
    """Rellena m1-p5/h1-ph5 desde markov/heuristic si faltan."""
    numbers, probs = _matrix_parts(record)

    markov_forecast = list(record.get("markov_forecast") or [])
    if (not markov_forecast or any(m.get("number") is None for m in markov_forecast)):
        rebuilt = _ranking_entries(record.get("markov"))
        if not rebuilt:
            for name in ["M1", "M2", "M3", "M4", "M5"]:
                n = numbers.get(name)
                if n is None:
                    continue
                p = probs.get(name)
                try:
                    rebuilt.append({"number": int(n), "probability": float(p) if p is not None else 0.0})
                except Exception:
                    continue
        if rebuilt:
            markov_forecast = rebuilt
    if markov_forecast:
        record["markov_forecast"] = markov_forecast

    heuristic_forecast = list(record.get("heuristic_forecast") or [])
    if (not heuristic_forecast or any(h.get("number") is None for h in heuristic_forecast)):
        rebuilt = _ranking_entries(record.get("heuristic"))
        if not rebuilt:
            for name in ["H1", "H2", "H3", "H4", "H5"]:
                n = numbers.get(name)
                if n is None:
                    continue
                p = probs.get(name)
                try:
                    rebuilt.append({"number": int(n), "probability": float(p) if p is not None else 0.0})
                except Exception:
                    continue
        if rebuilt:
            heuristic_forecast = rebuilt
    if heuristic_forecast:
        record["heuristic_forecast"] = heuristic_forecast

    mf = record.get("markov_forecast") or []
    hf = record.get("heuristic_forecast") or []
    if record.get("m1") is None and len(mf) > 0:
        try: record["m1"] = mf[0].get("number"); record["p1"] = mf[0].get("probability")
        except Exception: pass
    if record.get("m2") is None and len(mf) > 1:
        try: record["m2"] = mf[1].get("number"); record["p2"] = mf[1].get("probability")
        except Exception: pass
    if record.get("m3") is None and len(mf) > 2:
        try: record["m3"] = mf[2].get("number"); record["p3"] = mf[2].get("probability")
        except Exception: pass
    if record.get("m4") is None and len(mf) > 3:
        try: record["m4"] = mf[3].get("number"); record["p4"] = mf[3].get("probability")
        except Exception: pass
    if record.get("m5") is None and len(mf) > 4:
        try: record["m5"] = mf[4].get("number"); record["p5"] = mf[4].get("probability")
        except Exception: pass
    if record.get("h1") is None and len(hf) > 0:
        try: record["h1"] = hf[0].get("number"); record["ph1"] = hf[0].get("probability")
        except Exception: pass
    if record.get("h2") is None and len(hf) > 1:
        try: record["h2"] = hf[1].get("number"); record["ph2"] = hf[1].get("probability")
        except Exception: pass
    if record.get("h3") is None and len(hf) > 2:
        try: record["h3"] = hf[2].get("number"); record["ph3"] = hf[2].get("probability")
        except Exception: pass
    if record.get("h4") is None and len(hf) > 3:
        try: record["h4"] = hf[3].get("number"); record["ph4"] = hf[3].get("probability")
        except Exception: pass
    if record.get("h5") is None and len(hf) > 4:
        try: record["h5"] = hf[4].get("number"); record["ph5"] = hf[4].get("probability")
        except Exception: pass


def _migrate_records(records: list[Any]) -> tuple[list[dict], int]:
    """Conserva todos los registros; nunca elimina predicciones guardadas."""
    migrated: list[dict] = []
    dropped = 0
    for record in records:
        if not isinstance(record, dict):
            dropped += 1
            continue
        item = dict(record)
        if item.get("source_number") is None:
            try:
                item["source_number"] = draw_number(str(item.get("source_draw", "")))
            except (TypeError, ValueError):
                pass
        migrated.append(item)
    return migrated, dropped


@dataclass
class _State:
    predictions: list[dict] = field(default_factory=list)
    pauses: list[dict] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=_empty_stats)
    dirty: bool = False


# ----------------------------------------------------------------------
# Monitor
# ----------------------------------------------------------------------
class LiveMonitor:
    """Registro vivo de predicciones comparadas y alerta M4."""

    def __init__(
        self,
        load_store: StoreLoader,
        save_store: StoreSaver,
        spec: StrategySpec | None = None,
        payout: PayoutModel | None = None,
        kelly_config: KellyConfig | None = None,
        bankroll: float = 1000.0,
        backfill_rounds: int = 0,
        max_records: int = 0,
        reglas_activas: bool = True,
    ) -> None:
        self.load_store = load_store
        #: False = las reglas M4 se siguen evaluando y registrando (trazabilidad),
        #: pero la alerta nunca se dispara ni sugiere stake (config_alertas.M4_ACTIVO).
        self.reglas_activas = bool(reglas_activas)
        self.save_store = save_store
        self.spec = spec or StrategySpec()
        self.payout = payout
        self.kelly_config = kelly_config or KellyConfig()
        self.bankroll = bankroll
        self.backfill_rounds = max(0, int(backfill_rounds))
        self.max_records = max(0, int(max_records))
        self._state = _State()
        self._loaded = False
        self._loaded_predictions = 0
        self._last_saved_count = 0
        self._last_saved_resolved = 0
        self._last_saved_pauses = 0

    # ------------------------------------------------------------------
    # Almacén
    # ------------------------------------------------------------------
    def _load(self) -> _State:
        if not self._loaded:
            stored = self.load_store() or {}
            raw_predictions = list(stored.get("predictions") or [])
            predictions, dropped = _migrate_records(raw_predictions)
            self._loaded_predictions = len(predictions)
            for record in predictions:
                _rebuild_forecasts_from_lists(record)
            pauses = list(stored.get("pauses") or [])
            stats = _stats_from_predictions(predictions, pauses, self.spec)
            stored_stats = _normalize_stats(stored.get("stats"))
            self._state = _State(
                predictions=predictions,
                pauses=pauses,
                stats=stats,
                dirty=bool(dropped) or stats != stored_stats,
            )
            self._last_saved_count = len(predictions)
            self._last_saved_resolved = sum(
                1
                for record in predictions
                if record.get("status") in (STATUS_ACIERTO, STATUS_FALLO, STATUS_OMITIDA)
            )
            self._last_saved_pauses = len(pauses)
            # Nunca se reemplaza un registro existente por uno reconstruido.
            # Solo se siembra desde el CSV cuando todavía no hay predicciones.
            self._loaded = True
        return self._state

    def _persist(self, state: _State) -> None:
        if not state.dirty:
            return
        # Protección contra borrados accidentales: si el almacén tenía
        # predicciones, no se guarda un estado vacío ni una lista más corta.
        total = len(state.predictions)
        if self._loaded_predictions and not state.predictions:
            state.dirty = False
            return
        if self._loaded_predictions and total < self._loaded_predictions:
            state.dirty = False
            return
        if self._last_saved_count > 0 and total < self._last_saved_count:
            state.dirty = False
            return

        resolved = sum(
            1
            for record in state.predictions
            if record.get("status") in (STATUS_ACIERTO, STATUS_FALLO, STATUS_OMITIDA)
        )
        pauses = len(state.pauses)

        payload = {
            "version": STORE_VERSION,
            "updated_at": _now(),
            "stats": dict(state.stats),
            "predictions": state.predictions,
            "pauses": state.pauses,
        }
        try:
            self.save_store(payload)
        except Exception:
            return
        self._loaded_predictions = len(state.predictions)
        self._last_saved_count = total
        self._last_saved_resolved = resolved
        self._last_saved_pauses = pauses
        state.dirty = False
    # ------------------------------------------------------------------
    # Motor de alertas
    # ------------------------------------------------------------------
    def _build_engine(self, stats: dict[str, int]) -> AlertEngine:
        engine = AlertEngine(
            spec=self.spec,
            payout=self.payout,
            kelly_config=self.kelly_config,
            bankroll=self.bankroll,
            half_life=None,
        )
        engine.seed_cohorts(
            window=(stats["window_hits"], stats["window_rounds"]),
            alert=(stats["alert_hits"], stats["alerts"]),
            anchor=(stats["anchor_hits"], stats["anchor_alerts"]),
        )
        return engine

    def _evaluate(self, engine: AlertEngine, record: dict) -> dict:
        numbers, probabilities = _matrix_parts(record)
        if not numbers or not probabilities:
            return {}
        decision = engine.evaluate(
            {
                "draw_id": record.get("source_number"),
                "last_number": record.get("last_number"),
                "numbers": numbers,
                "probabilities": probabilities,
            }
        )
        return decision.as_dict()

    def _observe(
        self,
        engine: AlertEngine,
        decision: AlertDecision,
        actual: int,
        state: _State,
    ) -> bool:
        """Cierra el ciclo de una ronda y actualiza los estimadores."""
        engine.observe(decision, actual)
        hit = bool(decision.number is not None and int(actual) == int(decision.number))
        probability = decision.probability
        if (
            probability is not None
            and self.spec.probability_min < probability <= self.spec.probability_max
        ):
            state.stats["window_rounds"] += 1
            state.stats["window_hits"] += int(hit)
        if decision.fired:
            state.stats["alerts"] += 1
            state.stats["alert_hits"] += int(hit)
            if decision.anchor:
                state.stats["anchor_alerts"] += 1
                state.stats["anchor_hits"] += int(hit)
        return hit

    # ------------------------------------------------------------------
    # Construcción de registros
    # ------------------------------------------------------------------
    def _build_record(
        self,
        label: str,
        number: int,
        forecast: LiveForecast,
    ) -> dict:
        markov_forecast = []
        for item in list(forecast.markov or [])[:5]:
            if isinstance(item, dict):
                n = item.get("number")
                p = item.get("probability")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                n, p = item[0], item[1]
            else:
                continue
            try:
                markov_forecast.append({"number": int(n), "probability": float(p)})
            except Exception:
                continue

        heuristic_forecast = []
        for item in list(forecast.heuristic or [])[:5]:
            if isinstance(item, dict):
                n = item.get("number")
                p = item.get("probability")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                n, p = item[0], item[1]
            else:
                continue
            try:
                heuristic_forecast.append({"number": int(n), "probability": float(p)})
            except Exception:
                continue

        record = {
            "source_draw": label,
            "source_number": number,
            "created_at": _now(),
            "last_number": forecast.last_number,
            "matrix": {
                "numbers": dict(forecast.numbers),
                "probabilities": dict(forecast.probabilities),
            },
            "markov": list(forecast.markov or []),
            "heuristic": list(forecast.heuristic or []),
            "markov_forecast": markov_forecast,
            "heuristic_forecast": heuristic_forecast,
            "m1": markov_forecast[0]["number"] if len(markov_forecast)>0 else None,
            "p1": markov_forecast[0]["probability"] if len(markov_forecast)>0 else None,
            "m2": markov_forecast[1]["number"] if len(markov_forecast)>1 else None,
            "p2": markov_forecast[1]["probability"] if len(markov_forecast)>1 else None,
            "m3": markov_forecast[2]["number"] if len(markov_forecast)>2 else None,
            "p3": markov_forecast[2]["probability"] if len(markov_forecast)>2 else None,
            "m4": markov_forecast[3]["number"] if len(markov_forecast)>3 else None,
            "p4": markov_forecast[3]["probability"] if len(markov_forecast)>3 else None,
            "m5": markov_forecast[4]["number"] if len(markov_forecast)>4 else None,
            "p5": markov_forecast[4]["probability"] if len(markov_forecast)>4 else None,
            "h1": heuristic_forecast[0]["number"] if len(heuristic_forecast)>0 else None,
            "ph1": heuristic_forecast[0]["probability"] if len(heuristic_forecast)>0 else None,
            "h2": heuristic_forecast[1]["number"] if len(heuristic_forecast)>1 else None,
            "ph2": heuristic_forecast[1]["probability"] if len(heuristic_forecast)>1 else None,
            "h3": heuristic_forecast[2]["number"] if len(heuristic_forecast)>2 else None,
            "ph3": heuristic_forecast[2]["probability"] if len(heuristic_forecast)>2 else None,
            "h4": heuristic_forecast[3]["number"] if len(heuristic_forecast)>3 else None,
            "ph4": heuristic_forecast[3]["probability"] if len(heuristic_forecast)>3 else None,
            "h5": heuristic_forecast[4]["number"] if len(heuristic_forecast)>4 else None,
            "ph5": heuristic_forecast[4]["probability"] if len(heuristic_forecast)>4 else None,
            "decision": {},
            "status": STATUS_PENDIENTE,
            "observed_draw": None,
            "actual_number": None,
            "markov_rank": None,
            "heuristic_rank": None,
            "matched": "pendiente",
            "alert_hit": None,
            "origin": "backfill",
        }
        return record

    def _close(
        self,
        engine: AlertEngine,
        record: dict,
        observed_draw: str,
        actual: int,
        state: _State,
    ) -> None:
        """Resuelve una ronda con el resultado observado."""
        markov_rank = _rank_of(record.get("markov") or [], actual)
        heuristic_rank = _rank_of(record.get("heuristic") or [], actual)
        if markov_rank and heuristic_rank:
            matched = "ambos"
        elif markov_rank:
            matched = "markov"
        elif heuristic_rank:
            matched = "heuristico"
        else:
            matched = "ninguno"

        decision = _decision_from_dict(record.get("decision") or {})
        hit = self._observe(engine, decision, actual, state)

        record.update(
            {
                "status": STATUS_ACIERTO if (markov_rank or heuristic_rank) else STATUS_FALLO,
                "observed_draw": observed_draw,
                "actual_number": int(actual),
                "markov_rank": markov_rank,
                "heuristic_rank": heuristic_rank,
                "matched": matched,
                "alert_hit": hit if decision.fired else None,
            }
        )
        state.stats["rounds"] += 1
        state.stats["markov_trials"] += 1
        state.stats["markov_hits"] += int(markov_rank is not None)
        state.stats["markov_top1_hits"] += int(markov_rank == 1)
        state.stats["heuristic_trials"] += 1
        state.stats["heuristic_hits"] += int(heuristic_rank is not None)
        state.stats["heuristic_top1_hits"] += int(heuristic_rank == 1)
        state.dirty = True

    def _register_pause(self, state: _State, from_number: int, to_number: int) -> None:
        if to_number < from_number:
            return
        from_draw = f"No. {from_number}"
        to_draw = f"No. {to_number}"
        for pause in state.pauses:
            if pause.get("from_draw") == from_draw and pause.get("to_draw") == to_draw:
                return
        state.pauses.append(
            {
                "from_draw": from_draw,
                "to_draw": to_draw,
                "skipped_count": to_number - from_number + 1,
                "detected_at": _now(),
            }
        )
        state.stats["pauses"] += 1
        state.dirty = True

    # ------------------------------------------------------------------
    # Flujo principal
    # ------------------------------------------------------------------
    def update(self, rows: Iterable[dict[str, str]]) -> dict:
        """Actualiza el registro con los resultados conocidos y devuelve el estado."""
        state = self._load()
        draws = parse_draws(rows)
        engine = self._build_engine(state.stats)

        if len(draws) >= 2:
            values = [value for _, _, value in draws]
            # Si hay predicciones guardadas, calcular cuántos sorteos nuevos
            # llegaron desde la última fuente: hay que abrir una predicción
            # por cada uno para que ninguno se salte (p. ej. tras una pausa
            # nocturna llegan muchos sorteos de golpe).
            previous_source = max(
                (
                    int(record["source_number"])
                    for record in self._load().predictions
                    if record.get("source_number") is not None
                ),
                default=None,
            )
            if previous_source is None:
                forecast_rounds = self.backfill_rounds or 1
            else:
                nuevos = sum(1 for numero, _, _ in draws if numero > previous_source)
                forecast_rounds = max(self.backfill_rounds, nuevos, 1)
            forecasts = build_forecasts(values, forecast_rounds)
            if forecasts:
                # La tabla debe comparar el mismo ranking que el panel principal:
                # modelo Markov seleccionado por holdout + heurístico ponderado.
                selected_model, _ = select_markov_model(values)
                rankings = build_recovery_rankings(
                    values,
                    forecast_rounds,
                    model=selected_model,
                )
                for forecast, ranking in zip(forecasts, rankings):
                    forecast.markov = ranking["markov"]
                    forecast.heuristic = ranking["heuristic"]
            if not state.predictions:
                state.stats = _empty_stats()
                if self.backfill_rounds > 0:
                    self._backfill(state, draws, engine, forecasts)
                else:
                    self._open(state, draws, engine, forecasts)
            else:
                self._resolve(state, draws, engine)
                self._open(state, draws, engine, forecasts)
                self._reopen_omitted(state, draws, engine)
                self._prune_pauses(state, draws)
            # Las predicciones pendientes se conservan tal como se guardaron.
            self._trim(state)

        if self._loaded_predictions and len(state.predictions) < self._loaded_predictions:
            state.dirty = False
        self._persist(state)
        return self.payload()

    def _backfill(
        self,
        state: _State,
        draws: list[tuple[int, str, int]],
        engine: AlertEngine,
        forecasts: list[LiveForecast],
    ) -> None:
        """Retroanaliza las últimas rondas del histórico."""
        first_index = len(draws) - len(forecasts)
        for offset, forecast in enumerate(forecasts):
            index = first_index + offset
            number, label, _ = draws[index]
            if number < PREDICTIONS_START_DRAW:
                continue
            record = self._build_record(label, number, forecast)
            record["decision"] = self._evaluate(engine, record)
            state.predictions.append(record)

            if index + 1 < len(draws):
                next_number, next_label, next_value = draws[index + 1]
                if next_number > number + 1:
                    self._register_pause(state, number + 1, next_number - 1)
                    record.update(
                        {
                            "status": STATUS_OMITIDA,
                            "observed_draw": next_label,
                            "actual_number": None,
                            "matched": "no evaluada",
                        }
                    )
                    state.dirty = True
                else:
                    self._close(engine, record, next_label, next_value, state)
        state.dirty = True

    def _resolve(
        self,
        state: _State,
        draws: list[tuple[int, str, int]],
        engine: AlertEngine,
    ) -> None:
        """Cierra las rondas pendientes cuyo resultado ya estÃ¡ publicado."""
        for record in state.predictions:
            if record.get("status") != STATUS_PENDIENTE:
                continue
            try:
                source = int(record["source_number"])
            except (KeyError, TypeError, ValueError):
                continue
            if not _has_live_matrix(record):
                continue
            following = next((item for item in draws if item[0] > source), None)
            if following is None:
                continue
            number, label, value = following
            if number > source + 1:
                self._register_pause(state, source + 1, number - 1)
                record.update(
                    {
                        "status": STATUS_OMITIDA,
                        "observed_draw": label,
                        "actual_number": None,
                        "matched": "no evaluada",
                        "alert_hit": None,
                    }
                )
                state.dirty = True
                continue
            _rebuild_forecasts_from_lists(record)
            self._close(engine, record, label, value, state)

    def _open(
        self,
        state: _State,
        draws: list[tuple[int, str, int]],
        engine: AlertEngine,
        forecasts: list[LiveForecast],
    ) -> None:
        """Abre una predicción por cada sorteo nuevo (ninguno se salta).

        Antes sólo se abría la ronda del último sorteo: si llegaban varios
        sorteos de una vez (pausa nocturna, hueco recuperado), los
        intermedios quedaban sin predicción comparada.
        """
        if not forecasts:
            return
        previous_sources = [
            int(record["source_number"])
            for record in state.predictions
            if record.get("source_number") is not None
        ]
        previous = max(previous_sources) if previous_sources else None
        first_index = len(draws) - len(forecasts)
        opened = False
        for index in range(max(first_index, 0), len(draws)):
            number, label, _ = draws[index]
            if number < PREDICTIONS_START_DRAW:
                continue
            if previous is not None and number <= previous:
                continue
            if any(
                record.get("source_number") == number
                for record in state.predictions
            ):
                continue
            record = self._build_record(label, number, forecasts[index - first_index])
            record["origin"] = "live"
            record["decision"] = self._evaluate(engine, record)
            state.predictions.append(record)
            if index + 1 < len(draws):
                next_number, next_label, next_value = draws[index + 1]
                if next_number == number + 1:
                    self._close(engine, record, next_label, next_value, state)
                else:
                    self._register_pause(state, number + 1, next_number - 1)
                    record.update(
                        {
                            "status": STATUS_OMITIDA,
                            "observed_draw": next_label,
                            "actual_number": None,
                            "matched": "no evaluada",
                        }
                    )
            opened = True
        if opened:
            state.dirty = True

    def _reopen_omitted(
        self,
        state: _State,
        draws: list[tuple[int, str, int]],
        engine: AlertEngine,
    ) -> None:
        """Cierra rondas que quedaron «no evaluadas» por un hueco ya reparado."""
        by_number = {number: (label, value) for number, label, value in draws}
        reopened = False
        for record in state.predictions:
            if record.get("status") != STATUS_OMITIDA:
                continue
            try:
                source = int(record["source_number"])
            except (TypeError, ValueError):
                continue
            following = by_number.get(source + 1)
            if following is None or not _has_live_matrix(record):
                continue
            label, value = following
            _rebuild_forecasts_from_lists(record)
            self._close(engine, record, label, value, state)
            reopened = True
        if reopened:
            state.dirty = True

    def _prune_pauses(self, state: _State, draws: list[tuple[int, str, int]]) -> None:
        """Elimina pausas cuyos sorteos ya fueron recuperados del origen."""
        present = {number for number, _, _ in draws}
        kept = []
        for pause in state.pauses:
            try:
                start = draw_number(pause.get("from_draw", ""))
                end = draw_number(pause.get("to_draw", ""))
            except (TypeError, ValueError):
                kept.append(pause)
                continue
            if start > end or all(number in present for number in range(start, end + 1)):
                continue
            kept.append(pause)
        if len(kept) != len(state.pauses):
            state.pauses = kept
            state.stats["pauses"] = len(kept)
            state.dirty = True

    def _trim(self, state: _State) -> None:
        """Conserva todas las rondas cuando max_records es 0."""
        if self.max_records <= 0 or len(state.predictions) <= self.max_records:
            return
        state.predictions = state.predictions[-self.max_records :]
        state.dirty = True

    # ------------------------------------------------------------------
    # Payload para la interfaz
    # ------------------------------------------------------------------
    def payload(self) -> dict:
        """Estado completo del monitor, listo para la API y la interfaz."""
        state = self._state
        pending = next(
            (
                record
                for record in reversed(state.predictions)
                if record.get("status") == STATUS_PENDIENTE
            ),
            None,
        )
        return {
            "alert": self._alert_payload(pending),
            "alert_stats": self._alert_stats(),
            "alert_history": self._alert_history(),
            "predictions": [self._public_record(record) for record in reversed(state.predictions)],
            "pauses": list(reversed(state.pauses)),
            "prediction_stats": self._prediction_stats(),
        }

    def _alert_payload(self, pending: dict | None) -> dict:
        decision = dict(pending.get("decision") or {}) if pending else {}
        stake = decision.get("stake") or None
        fired_m4 = bool(decision.get("fired"))
        failures = list(decision.get("failures") or [])
        if not self.reglas_activas:
            stake = None
            failures.insert(0, "Reglas M4 desactivadas (config_alertas.M4_ACTIVO = False): sin evidencia que las sostenga.")
        fired = fired_m4 and self.reglas_activas
        return {
            "available": pending is not None,
            "fired": fired,
            "disabled": not self.reglas_activas,
            "rule_fired_m4": fired_m4,
            "state": "DESACTIVADA" if not self.reglas_activas else "ACTIVA" if fired else "SIN SEÑAL",
            "model": decision.get("model", self.spec.model),
            "number": decision.get("number"),
            "probability": decision.get("probability"),
            "spread": decision.get("spread"),
            "confidence": decision.get("confidence", "NINGUNA"),
            "anchor": bool(decision.get("anchor")),
            "failures": failures,
            "stake": stake,
            "stake_fraction": float(stake.get("stake_fraction", 0.0)) if stake else 0.0,
            "stake_amount": float(stake.get("stake_amount", 0.0)) if stake else 0.0,
            "stake_applied": bool(stake.get("applied")) if stake else False,
            "stake_notes": list(stake.get("notes") or []) if stake else [],
            "source_draw": pending.get("source_draw") if pending else None,
            "last_number": pending.get("last_number") if pending else None,
            "bankroll": self.bankroll,
            "payout": self.payout.name if self.payout else None,
            "window": [self.spec.probability_min, self.spec.probability_max],
            "max_spread": self.spec.max_spread,
            "baseline": BASELINE,
        }

    def _alert_stats(self) -> dict:
        stats = self._state.stats
        alerts = stats["alerts"]
        window = stats["window_rounds"]
        anchor = stats["anchor_alerts"]
        return {
            "rounds": stats["rounds"],
            "alerts": alerts,
            "alert_hits": stats["alert_hits"],
            "alert_rate": (stats["alert_hits"] / alerts) if alerts else 0.0,
            "window_rounds": window,
            "window_hits": stats["window_hits"],
            "window_rate": (stats["window_hits"] / window) if window else 0.0,
            "anchor_alerts": anchor,
            "anchor_hits": stats["anchor_hits"],
            "anchor_rate": (stats["anchor_hits"] / anchor) if anchor else 0.0,
            "pauses": stats["pauses"],
            "baseline": BASELINE,
        }

    def _prediction_stats(self) -> dict:
        stats = self._state.stats
        markov_trials = stats["markov_trials"]
        heuristic_trials = stats["heuristic_trials"]
        return {
            "rounds": stats["rounds"],
            "start_draw": PREDICTIONS_START_DRAW,
            "markov_hits": stats["markov_hits"],
            "markov_trials": markov_trials,
            "markov_rate": (stats["markov_hits"] / markov_trials) if markov_trials else 0.0,
            "markov_top1_hits": stats["markov_top1_hits"],
            "markov_top1_rate": (
                stats["markov_top1_hits"] / markov_trials if markov_trials else 0.0
            ),
            "heuristic_hits": stats["heuristic_hits"],
            "heuristic_trials": heuristic_trials,
            "heuristic_rate": (
                stats["heuristic_hits"] / heuristic_trials if heuristic_trials else 0.0
            ),
            "heuristic_top1_hits": stats["heuristic_top1_hits"],
            "heuristic_top1_rate": (
                stats["heuristic_top1_hits"] / heuristic_trials if heuristic_trials else 0.0
            ),
            "baseline": BASELINE,
            "baseline_top5": BASELINE_TOP5,
        }

    def _alert_history(self) -> list[dict]:
        history: list[dict] = []
        for record in reversed(self._state.predictions):
            decision = record.get("decision") or {}
            if not decision.get("fired"):
                continue
            stake = decision.get("stake") or {}
            try:
                num = decision.get("number")
                num = num if num is not None and num != "number" else None
                hist_num = int(num) if num is not None else None
            except Exception:
                hist_num = None
            history.append(
                {
                    "source_draw": record.get("source_draw"),
                    "observed_draw": record.get("observed_draw"),
                    "number": hist_num,
                    "probability": decision.get("probability"),
                    "confidence": decision.get("confidence"),
                    "anchor": bool(decision.get("anchor")),
                    "status": record.get("status"),
                    "actual_number": record.get("actual_number"),
                    "hit": record.get("alert_hit"),
                    "stake_fraction": float(stake.get("stake_fraction", 0.0)),
                    "stake_applied": bool(stake.get("applied")),
                }
            )
            if len(history) >= 100:
                break
        return history

    def _public_record(self, record: dict) -> dict:
        """Versión ligera del registro para el navegador."""
        decision = record.get("decision") or {}
        markov_forecast = record.get("markov_forecast") or []
        heuristic_forecast = record.get("heuristic_forecast") or []

        m1 = record.get("m1"); p1 = record.get("p1")
        m2 = record.get("m2"); p2 = record.get("p2")
        m3 = record.get("m3"); p3 = record.get("p3")
        m4 = record.get("m4"); p4 = record.get("p4")
        m5 = record.get("m5"); p5 = record.get("p5")
        h1 = record.get("h1"); ph1 = record.get("ph1")
        h2 = record.get("h2"); ph2 = record.get("ph2")
        h3 = record.get("h3"); ph3 = record.get("ph3")
        h4 = record.get("h4"); ph4 = record.get("ph4")
        h5 = record.get("h5"); ph5 = record.get("ph5")

        return {
            "source_draw": record.get("source_draw"),
            "created_at": record.get("created_at"),
            "status": record.get("status"),
            "observed_draw": record.get("observed_draw"),
            "actual_number": record.get("actual_number"),
            "markov_rank": record.get("markov_rank"),
            "heuristic_rank": record.get("heuristic_rank"),
            "markov_forecast": markov_forecast,
            "heuristic_forecast": heuristic_forecast,
            "m1": m1, "p1": p1,
            "m2": m2, "p2": p2,
            "m3": m3, "p3": p3,
            "m4": m4, "p4": p4,
            "m5": m5, "p5": p5,
            "h1": h1, "ph1": ph1,
            "h2": h2, "ph2": ph2,
            "h3": h3, "ph3": ph3,
            "h4": h4, "ph4": ph4,
            "h5": h5, "ph5": ph5,
            "matched": record.get("matched"),
            "alert_fired": bool(decision.get("fired")),
            "alert_hit": record.get("alert_hit"),
            "alert_number": decision.get("number"),
            "alert_probability": decision.get("probability"),
        }
