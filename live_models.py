"""Modelos predictivos incrementales para el monitor en vivo.

La página pública de Loteka solo publica el número que salió. Los diez
submodelos de la «Especificación de Estrategia Cuantitativa» (M1..M5 y
H1..H5) no son públicos, de modo que el motor en vivo opera con una **matriz
sustituta** de diez modelos calculados con el propio histórico local: cinco
variantes de Markov y cinco mezclas de transición/frecuencia.

La matriz sustituta conserva la estructura que el motor de alertas necesita
(diez submodelos, cada uno con un número y una probabilidad) y, sobre todo,
permite medir en vivo si la lógica de la especificación (ventana de
probabilidad, filtro de dispersión, anclaje y stake) produce o no ventaja. Es
un sustituto operativo, no una reproducción del ensemble del PDF: por eso el
panel web lo etiqueta siempre como «matriz en vivo».

El motor es **incremental**: mantiene los conteos de transiciones en una sola
pasada por el histórico, de modo que cada ronda nueva se resuelve en tiempo
constante y sin look-ahead (los conteos nunca incluyen el resultado que se
intenta predecir).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

#: Número de casillas del espacio observado en Ruleta Express (0..18).
OUTPUT_MODULUS = 19

#: Longitud de la ventana de recencia usada por la familia heurística.
DEFAULT_WINDOW = 500


@dataclass(frozen=True)
class LiveModel:
    """Definición de un submodelo de la matriz en vivo."""

    name: str
    order: int
    alpha: float
    forbid_repeat: bool
    frequency_weight: float
    window: int | None
    family: str

    def describe(self) -> str:
        parts = [f"orden={self.order}", f"alpha={self.alpha}"]
        if self.forbid_repeat:
            parts.append("anti-repetición")
        if self.frequency_weight:
            parts.append(f"peso_frecuencia={self.frequency_weight}")
        if self.window:
            parts.append(f"ventana={self.window}")
        return ", ".join(parts)


#: Familia de diez submodelos que sustituye al ensemble del PDF.
LIVE_MODELS: tuple[LiveModel, ...] = (
    LiveModel("M1", 1, 1.0, False, 0.0, None, "markov"),
    LiveModel("M2", 2, 1.0, False, 0.0, None, "markov"),
    LiveModel("M3", 3, 1.0, False, 0.0, None, "markov"),
    LiveModel("M4", 1, 1.0, True, 0.0, None, "markov"),
    LiveModel("M5", 2, 0.5, True, 0.0, None, "markov"),
    LiveModel("H1", 1, 1.0, False, 0.0, DEFAULT_WINDOW, "blend"),
    LiveModel("H2", 1, 1.0, False, 0.25, DEFAULT_WINDOW, "blend"),
    LiveModel("H3", 1, 1.0, False, 0.50, DEFAULT_WINDOW, "blend"),
    LiveModel("H4", 1, 1.0, False, 0.75, DEFAULT_WINDOW, "blend"),
    LiveModel("H5", 1, 1.0, False, 1.00, DEFAULT_WINDOW, "blend"),
    # R1: versión incremental del modelo de recencia (paquete `recencia/`).
    # El motor sólo ve la secuencia de números, así que la antigüedad se mide
    # en filas y no separa régimen diurno/fuera: es una referencia en la
    # matriz, no el modelo operativo. No entra en la regla M4, que sólo mira
    # los diez modelos de `m4_engine.config.MODEL_ORDER`.
    LiveModel("R1", 0, 1.0, False, 0.0, None, "recencia"),
)

#: Primer valor de antigüedad de cada cubeta de R1: 1,2,3,4,5,6-8,9-14,15-30,>30
#: (las mismas cubetas que `recencia.modelo.BORDES`).
RECENCY_EDGES = (1, 2, 3, 4, 5, 6, 9, 15, 31)


def _recency_bucket(age: int) -> int:
    bucket = 0
    for index, edge in enumerate(RECENCY_EDGES):
        if age >= edge:
            bucket = index
    return bucket

#: Nombre del submodelo que el motor de alertas vigila (M4 en la especificación).
PRIMARY_MODEL_NAME = "M4"


@dataclass
class LiveForecast:
    """Predicción de una ronda: matriz de submodelos y rankings de apoyo."""

    last_number: int
    numbers: dict[str, int] = field(default_factory=dict)
    probabilities: dict[str, float] = field(default_factory=dict)
    markov: list[dict] = field(default_factory=list)
    heuristic: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "last_number": self.last_number,
            "numbers": dict(self.numbers),
            "probabilities": dict(self.probabilities),
            "markov": list(self.markov),
            "heuristic": list(self.heuristic),
        }


def _smooth(counts: list[int], alpha: float) -> list[float]:
    """Convierte conteos en probabilidades con suavizado de Laplace."""
    total = sum(counts)
    denominator = total + alpha * OUTPUT_MODULUS
    return [(count + alpha) / denominator for count in counts]


def _normalize(probabilities: list[float]) -> list[float]:
    total = sum(probabilities)
    if total <= 0.0:
        return [1.0 / OUTPUT_MODULUS] * OUTPUT_MODULUS
    return [probability / total for probability in probabilities]


def _top(probabilities: list[float], limit: int = 5) -> list[dict]:
    ranking = sorted(range(OUTPUT_MODULUS), key=probabilities.__getitem__, reverse=True)
    return [
        {"number": number, "probability": probabilities[number]}
        for number in ranking[:limit]
    ]


class LiveModelEngine:
    """Motor incremental de modelos para el monitor en vivo.

    Se alimenta con los resultados observados en orden cronológico. Tras cada
    resultado, `forecast()` devuelve la predicción para la ronda siguiente.
    """

    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        self.window = window
        self._values: list[int] = []
        self._order1 = [[0] * OUTPUT_MODULUS for _ in range(OUTPUT_MODULUS)]
        self._order1_window = [[0] * OUTPUT_MODULUS for _ in range(OUTPUT_MODULUS)]
        self._order2: dict[tuple[int, int], list[int]] = {}
        self._order3: dict[tuple[int, int, int], list[int]] = {}
        self._frequency_window = [0] * OUTPUT_MODULUS
        self._recent_transitions: deque[tuple[int, int]] = deque()
        self._recent_values: deque[int] = deque()
        # R1: última fila en que salió cada número y conteos por cubeta de antigüedad.
        self._last_seen: list[int | None] = [None] * OUTPUT_MODULUS
        self._recency_hits = [0] * len(RECENCY_EDGES)
        self._recency_exposed = [0] * len(RECENCY_EDGES)

    # ------------------------------------------------------------------
    # Ingesta
    # ------------------------------------------------------------------
    def advance(self, value: int) -> None:
        """Registra un resultado observado y actualiza los conteos."""
        number = int(value)
        if not 0 <= number < OUTPUT_MODULUS:
            raise ValueError(
                f"El resultado {number} está fuera del rango 0..{OUTPUT_MODULUS - 1}."
            )

        if self._values:
            previous = self._values[-1]
            self._order1[previous][number] += 1
            self._order1_window[previous][number] += 1
            self._recent_transitions.append((previous, number))
            if len(self._recent_transitions) > self.window:
                old_previous, old_number = self._recent_transitions.popleft()
                self._order1_window[old_previous][old_number] -= 1

            if len(self._values) >= 2:
                key = (self._values[-2], previous)
                counts = self._order2.get(key)
                if counts is None:
                    counts = [0] * OUTPUT_MODULUS
                    self._order2[key] = counts
                counts[number] += 1

            if len(self._values) >= 3:
                key = (self._values[-3], self._values[-2], previous)
                counts = self._order3.get(key)
                if counts is None:
                    counts = [0] * OUTPUT_MODULUS
                    self._order3[key] = counts
                counts[number] += 1

        # R1: antes de registrar `number`, se cuenta en qué cubeta estaba cada candidato.
        if self._values:
            position = len(self._values)
            for candidate in range(OUTPUT_MODULUS):
                bucket = self._recency_bucket_of(candidate, position)
                self._recency_exposed[bucket] += 1
                if candidate == number:
                    self._recency_hits[bucket] += 1
        self._last_seen[number] = len(self._values)

        self._frequency_window[number] += 1
        self._recent_values.append(number)
        if len(self._recent_values) > self.window:
            self._frequency_window[self._recent_values.popleft()] -= 1

        self._values.append(number)

    def _recency_bucket_of(self, candidate: int, position: int) -> int:
        seen = self._last_seen[candidate]
        return len(RECENCY_EDGES) - 1 if seen is None else _recency_bucket(position - seen)

    def recency_distribution(self) -> list[float]:
        """R1: P(v) ∝ (aciertos + 1) / (expuestos + 19) de la cubeta de antigüedad de v."""
        weights = [
            (hits + 1.0) / (exposed + OUTPUT_MODULUS)
            for hits, exposed in zip(self._recency_hits, self._recency_exposed)
        ]
        position = len(self._values)
        return _normalize([weights[self._recency_bucket_of(v, position)] for v in range(OUTPUT_MODULUS)])

    def run(self, values: list[int]) -> None:
        """Reproduce una secuencia completa de resultados."""
        for value in values:
            self.advance(value)

    # ------------------------------------------------------------------
    # Predicción
    # ------------------------------------------------------------------
    def forecast(self) -> LiveForecast:
        """Devuelve la predicción para la ronda siguiente."""
        if not self._values:
            raise ValueError("El motor necesita al menos un resultado observado.")

        last = self._values[-1]
        uniform = [1.0 / OUTPUT_MODULUS] * OUTPUT_MODULUS

        order1_full = _smooth(self._order1[last], 1.0)
        order1_window = _smooth(self._order1_window[last], 1.0)
        if len(self._values) >= 2:
            order2_counts = self._order2.get((self._values[-2], last))
            order2 = (
                _smooth(order2_counts, 1.0)
                if order2_counts is not None
                else list(uniform)
            )
        else:
            order2 = list(uniform)
        if len(self._values) >= 3:
            order3_counts = self._order3.get((self._values[-3], self._values[-2], last))
            order3 = (
                _smooth(order3_counts, 1.0)
                if order3_counts is not None
                else list(uniform)
            )
        else:
            order3 = list(uniform)
        frequency = _smooth(self._frequency_window, 1.0)

        numbers: dict[str, int] = {}
        probabilities: dict[str, float] = {}
        recency = self.recency_distribution()
        for model in LIVE_MODELS:
            if model.family == "recencia":
                distribution = list(recency)
            elif model.family == "markov":
                if model.order == 1:
                    distribution = list(order1_full)
                elif model.order == 2:
                    distribution = list(order2)
                else:
                    distribution = list(order3)
            else:
                transition = list(order1_window)
                weight = model.frequency_weight
                distribution = [
                    (1.0 - weight) * transition[index] + weight * frequency[index]
                    for index in range(OUTPUT_MODULUS)
                ]
                distribution = _normalize(distribution)

            if model.forbid_repeat:
                distribution[last] = 0.0
                distribution = _normalize(distribution)

            candidate = max(range(OUTPUT_MODULUS), key=distribution.__getitem__)
            numbers[model.name] = candidate
            probabilities[model.name] = distribution[candidate]

        markov = _top(order1_full)
        heuristic = _top(
            _normalize(
                [
                    0.75 * order1_window[index] + 0.25 * frequency[index]
                    for index in range(OUTPUT_MODULUS)
                ]
            )
        )
        return LiveForecast(
            last_number=last,
            numbers=numbers,
            probabilities=probabilities,
            markov=markov,
            heuristic=heuristic,
        )


def build_forecasts(
    values: list[int],
    rounds: int,
    window: int = DEFAULT_WINDOW,
) -> list[LiveForecast]:
    """Calcula las predicciones de las últimas `rounds` transiciones.

    Devuelve una lista en orden cronológico donde el elemento `i` es la
    predicción feita tras observar `values[0..i]` y antes de conocer
    `values[i+1]`. Si el histórico es más corto que `rounds`, se devuelve
    todo lo disponible.
    """
    if rounds <= 0 or len(values) < 2:
        return []
    engine = LiveModelEngine(window=window)
    first_index = max(0, len(values) - 1 - rounds)
    forecasts: list[LiveForecast] = []
    for index, value in enumerate(values):
        engine.advance(value)
        if index >= first_index:
            forecasts.append(engine.forecast())
    return forecasts
