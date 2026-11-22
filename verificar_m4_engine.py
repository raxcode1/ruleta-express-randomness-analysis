"""Verificación del motor M4 sin dependencias de test externas.

El proyecto no declara `pytest`, así que las comprobaciones se ejecutan como un
script autónomo con `assert`. Devuelve código de salida 1 si algo falla.

    python .\\verificar_m4_engine.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from m4_engine import (
    EUROPEAN_37,
    EXPRESS_19,
    AlertEngine,
    KellyConfig,
    KellyStakeSizer,
    PayoutModel,
    StrategySpec,
    load_supervision_csv,
    normalize_record,
)
from m4_engine.config import MODEL_ORDER
from m4_engine.features import build_features, collapse_conflicts
from m4_engine.stats import (
    binomial_survival,
    fisher_exact_two_sided,
    holm_bonferroni,
    wilson_interval,
)
from m4_engine.validation import validate_strategy
from live_models import LiveModelEngine
from live_monitor import LiveMonitor, PREDICTIONS_START_DRAW, memory_store

CSV_PATH = (
    Path(__file__).resolve().parent
    / "registros_supervision_probs_120 - registros_supervision_probs_120.csv"
)

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  OK   {name}")
    else:
        FAILED.append(name)
        print(f"  FALLA {name} {detail}")


def approx(value: float, expected: float, tolerance: float = 1e-9) -> bool:
    return abs(value - expected) <= tolerance


def section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def main() -> int:
    frame = load_supervision_csv(CSV_PATH)
    spec = StrategySpec()
    enriched = build_features(frame, spec)
    clean = collapse_conflicts(enriched)
    report = validate_strategy(frame, spec)

    section("1. Carga del CSV de supervisión")
    check("120 filas", len(frame) == 120, f"len={len(frame)}")
    check(
        "19 resultados distintos",
        frame["result"].nunique() == 19,
        f"n={frame['result'].nunique()}",
    )
    check("sin celdas vacías", int(frame.isna().sum().sum()) == 0)
    check(
        "probabilidades en fracción",
        0.0 <= float(frame["p_m4"].min()) and float(frame["p_m4"].max()) <= 1.0,
    )
    check(
        "último número encadenado con el resultado anterior",
        int((frame["result"].shift(1) == frame["last_number"]).sum()) == len(frame) - 1,
    )

    section("2. Reproducción de los hallazgos del PDF")
    zones = {row.zone: row for row in report.zones_pooled}
    check(
        "zona 5-7% global = 6,46%",
        approx(zones["5-7%"].accuracy, 0.0646, 0.0001),
        f"observado={zones['5-7%'].accuracy:.4%}",
    )
    check(
        "zona <5% global = 0%",
        approx(zones["<5%"].accuracy, 0.0, 1e-9),
        f"observado={zones['<5%'].accuracy:.4%}",
    )
    check(
        "zona >8% global ≈ 2,3%",
        abs(zones[">8%"].accuracy - 0.023) <= 0.012,
        f"observado={zones['>8%'].accuracy:.4%}",
    )
    check(
        "ventana M4 = 13,75%",
        approx(report.window["raw"]["accuracy"], 0.1375, 1e-9),
        f"observado={report.window['raw']['accuracy']:.4%}",
    )
    check(
        "M4 alcanza la probabilidad máxima en 6 rondas y ninguna dentro de la ventana",
        report.primary_modes[1]["tied_max_inside_window"] == 0,
    )
    check(
        "modo literal (tie) no emite alertas",
        report.primary_modes[1]["alerts"] == 0 and report.primary_modes[2]["alerts"] == 0,
    )
    boundary = enriched[enriched["model_probability"] <= 0.05]
    check(
        "el borde del 5,0% se clasifica igual en features y en la tabla",
        set(boundary["zone_model"]) <= {row.zone for row in report.zones_model}
        and int((enriched["model_probability"] <= 0.05).sum())
        == next(row.observations for row in report.zones_model if row.zone == "<5%"),
        f"filas con P(M4)<=5%: {len(boundary)}",
    )

    section("3. Integridad del log")
    check("86 grupos de predicción únicos", report.integrity.unique_groups == 86)
    check("6 grupos con etiquetas contradictorias", report.integrity.conflicting_groups == 6)
    check("35 filas descartadas por conflicto", report.integrity.conflicting_rows == 35)
    check("80 filas utilizables", report.integrity.clean_rows == 80)
    check("sorteo obsoleto detectado", report.integrity.stale_draw_ids == (248978,))

    section("4. Estadística")
    interval = wilson_interval(5, 10)
    check("Wilson contiene la estimación", interval.low < 0.5 < interval.high)
    check(
        "Wilson con 0 aciertos no excluye el 0",
        wilson_interval(0, 10).low < 1e-12,
        f"low={wilson_interval(0, 10).low}",
    )
    check(
        "cola binomial coherente",
        approx(binomial_survival(11, 80, 1 / 19), 0.0031, 0.0005),
        f"p={binomial_survival(11, 80, 1 / 19):.5f}",
    )
    check(
        "Fisher: proporciones iguales dan p=1",
        approx(fisher_exact_two_sided(10, 10, 10, 10), 1.0, 1e-9),
    )
    check(
        "Fisher: diferencia amplia da p pequeño",
        fisher_exact_two_sided(9, 1, 1, 9) < 0.01,
    )
    check(
        "Fisher: el anclaje no es significativo",
        fisher_exact_two_sided(2, 7, 6, 32) > 0.05,
        f"p={fisher_exact_two_sided(2, 7, 6, 32):.3f}",
    )
    check(
        "Holm-Bonferroni monótono",
        all(
            adjusted >= raw - 1e-12
            for raw, adjusted in zip(
                [0.01, 0.04, 0.03], holm_bonferroni([0.01, 0.04, 0.03])
            )
        ),
    )

    section("5. Módulo de Kelly")
    sizer = KellyStakeSizer(EUROPEAN_37, KellyConfig())
    without_evidence = sizer.size(successes=1, trials=3, bankroll=1000.0)
    check("sin evidencia suficiente el stake es 0", without_evidence.stake_fraction == 0.0)
    check("sin evidencia suficiente no se aplica", not without_evidence.applied)
    losing = sizer.size(successes=4, trials=100, bankroll=1000.0)
    check(
        "sin ventaja defendible el stake es 0",
        losing.stake_fraction == 0.0 and not losing.applied,
        f"stake={losing.stake_fraction:.4%}",
    )
    winning = sizer.size(successes=20, trials=60, bankroll=1000.0)
    check("con ventaja el stake es positivo", winning.stake_fraction > 0.0)
    check(
        "el stake respeta el tope de exposición",
        winning.stake_fraction <= KellyConfig().max_fraction + 1e-12,
        f"stake={winning.stake_fraction:.4%}",
    )
    check("el crecimiento esperado es positivo", winning.expected_log_growth > 0.0)
    check(
        "la tasa usada es conservadora (extremo inferior o encogido)",
        winning.rate_used <= winning.empirical_rate + 1e-12,
    )
    critical = sizer.size(successes=20, trials=60, bankroll=1000.0, confidence="CRITICO")
    check(
        "la confianza crítica escala el stake",
        critical.stake_fraction >= winning.stake_fraction,
    )
    check(
        "el equilibrio del modelo de 19 casillas es 5,26%",
        approx(EXPRESS_19.break_even, 1 / 19, 1e-12),
    )
    check(
        "la ventaja de la casa europea es 2,70%",
        approx(EUROPEAN_37.house_edge, 1 / 37, 1e-12),
    )

    section("6. Motor de alertas")
    engine = AlertEngine(spec=spec, payout=EUROPEAN_37, bankroll=1000.0)
    base_record = {
        "draw_id": 1,
        "last_number": 3,
        "numbers": {
            "M1": 3, "M2": 11, "M3": 16, "M4": 9, "M5": 0,
            "H1": 14, "H2": 5, "H3": 8, "H4": 12, "H5": 17,
        },
        "probabilities": {
            "M1": 0.0640, "M2": 0.0610, "M3": 0.0595, "M4": 0.0570, "M5": 0.0555,
            "H1": 0.0545, "H2": 0.0535, "H3": 0.0530, "H4": 0.0525, "H5": 0.0520,
        },
    }
    decision = engine.evaluate(base_record)
    check("una ronda en la ventana dispara la alerta", decision.fired)
    check("el número de la alerta es el de M4", decision.number == 9)
    check("la confianza base es ALTO", decision.confidence == "ALTO")
    check("sin ancla no hay escalada", not decision.anchor)

    anchored = dict(base_record)
    anchored["last_number"] = 7
    decision_anchor = engine.evaluate(anchored)
    check("con ancla la confianza es CRITICO", decision_anchor.confidence == "CRITICO")

    outside = dict(base_record)
    outside["probabilities"] = dict(base_record["probabilities"])
    outside["probabilities"]["M4"] = 0.0900
    decision_outside = engine.evaluate(outside)
    check("fuera de la ventana no dispara", not decision_outside.fired)
    check(
        "el motivo es la ventana de probabilidad",
        any("ventana operativa" in reason for reason in decision_outside.failures),
        str(decision_outside.failures),
    )

    wide = dict(base_record)
    wide["probabilities"] = dict(base_record["probabilities"])
    wide["probabilities"]["M5"] = 0.0100
    decision_wide = engine.evaluate(wide)
    check("un spread alto no dispara", not decision_wide.fired)
    check(
        "el motivo es la dispersión",
        any("spread" in reason for reason in decision_wide.failures),
        str(decision_wide.failures),
    )

    incomplete = engine.evaluate({"draw_id": 2, "last_number": 1, "numbers": {"M4": 4}})
    check("datos incompletos no disparan", not incomplete.fired)
    check(
        "el motivo son los datos incompletos",
        any("datos incompletos" in reason for reason in incomplete.failures),
    )

    strict_engine = AlertEngine(spec=StrategySpec(primary_mode="strict"), payout=EUROPEAN_37)
    check(
        "el modo estricto exige que M4 sea el máximo",
        not strict_engine.evaluate(base_record).fired,
    )

    section("7. Ausencia de look-ahead")
    live = AlertEngine(spec=spec, payout=EUROPEAN_37, bankroll=1000.0)
    before = live.alert_estimator.trials
    decision = live.evaluate(base_record)
    check("evaluar no toca el estimador", live.alert_estimator.trials == before)
    live.observe(decision, 9)
    check("observar el acierto actualiza el estimador", live.alert_estimator.trials == before + 1)
    check("el acierto queda registrado", live.alert_estimator.hits == 1)
    live.observe(decision, 4)
    check("el fallo queda registrado", live.alert_estimator.hits == 1)

    section("8. Coherencia de las métricas del informe")
    matrix = next(item for item in report.confusion if item.universe == "log depurado")
    alerts_clean = int(clean["alert"].sum())
    hits_clean = int(clean["hit_model"].sum())
    check(
        "VP + FP = alertas emitidas",
        matrix.true_positive + matrix.false_positive == alerts_clean,
        f"{matrix.true_positive}+{matrix.false_positive} vs {alerts_clean}",
    )
    check(
        "VP + FN = aciertos de M4 en el log depurado",
        matrix.true_positive + matrix.false_negative == hits_clean,
        f"{matrix.true_positive}+{matrix.false_negative} vs {hits_clean}",
    )
    pnl_clean = next(item for item in report.pnl if item.payout == "european37:clean")
    check("43 apuestas sobre señales válidas", pnl_clean.bets == 43)
    check(
        "PnL coherente con 6 aciertos a 35:1",
        approx(pnl_clean.total_units, 6 * 35 - 37, 1e-9),
        f"pnl={pnl_clean.total_units}",
    )
    check(
        "el ROI coincide con el PnL",
        approx(pnl_clean.roi, pnl_clean.total_units / pnl_clean.bets, 1e-12),
    )

    section("9. Normalización de registros en tiempo real")
    normalized = normalize_record(
        {
            "M4": 12,
            "prob.M4": "6,30%",
            "numbers": {"M1": 1},
            "probabilities": {"M1": 0.05},
            "último número": 7,
        }
    )
    check("número normalizado", normalized["numbers"]["M4"] == 12)
    check("probabilidad normalizada", abs(normalized["probabilities"]["M4"] - 0.063) < 1e-9)
    check("último número normalizado", normalized["last_number"] == 7)

    section("10. Monitor en vivo")
    values = [(index * 7 + 3) % 19 for index in range(40)]
    rows = [
        {
            "Fecha": "01/01/2026",
            "Hora": "12:00 PM",
            "No_Sorteo": f"No. {PREDICTIONS_START_DRAW + index}",
            "Numero": f"{value:02d}",
        }
        for index, value in enumerate(values)
    ]

    loader, saver = memory_store()
    monitor = LiveMonitor(
        loader,
        saver,
        spec=StrategySpec(),
        payout=EXPRESS_19,
        kelly_config=KellyConfig(),
        bankroll=1000.0,
        backfill_rounds=40,
        max_records=100,
    )
    live_payload = monitor.update(rows)
    live_predictions = live_payload["predictions"]
    resolved = [
        item for item in live_predictions if item["status"] in ("acierto", "fallo")
    ]
    pending = [item for item in live_predictions if item["status"] == "pendiente"]
    check("el monitor guarda las rondas evaluadas", len(live_predictions) >= 30, f"n={len(live_predictions)}")
    check(
        "no se guardan predicciones anteriores al sorteo de corte",
        all(
            int(item["source_draw"].split()[-1]) >= PREDICTIONS_START_DRAW
            for item in live_predictions
        ),
    )
    check("cada ronda evaluada tiene resultado", all(item["actual_number"] is not None for item in resolved))
    check("queda una sola ronda pendiente", len(pending) == 1, f"n={len(pending)}")
    check(
        "las rondas evaluadas coinciden con el contador",
        live_payload["alert_stats"]["rounds"] == len(resolved),
        f"{live_payload['alert_stats']['rounds']} vs {len(resolved)}",
    )
    check(
        "las alertas emitidas coinciden con el contador",
        live_payload["alert_stats"]["alerts"]
        == sum(1 for item in live_predictions if item["alert_fired"]),
    )
    check("la alerta expone la ronda en curso", live_payload["alert"]["available"] is True)
    check(
        "sin evidencia suficiente el stake es cero",
        live_payload["alert"]["stake_applied"] is False
        and live_payload["alert"]["stake_fraction"] == 0.0,
    )

    reloaded = LiveMonitor(
        loader,
        saver,
        spec=StrategySpec(),
        payout=EXPRESS_19,
        kelly_config=KellyConfig(),
        bankroll=1000.0,
        backfill_rounds=40,
        max_records=100,
    )
    reloaded_payload = reloaded.update(rows)
    check(
        "el registro se conserva al recargar",
        reloaded_payload["alert_stats"]["rounds"] == live_payload["alert_stats"]["rounds"],
        f"{reloaded_payload['alert_stats']['rounds']} vs {live_payload['alert_stats']['rounds']}",
    )
    check(
        "no se duplican rondas al recargar",
        len(reloaded_payload["predictions"]) == len(live_predictions),
        f"{len(reloaded_payload['predictions'])} vs {len(live_predictions)}",
    )

    limited_loader, limited_saver = memory_store()
    limited = LiveMonitor(
        limited_loader,
        limited_saver,
        spec=StrategySpec(),
        payout=EXPRESS_19,
        kelly_config=KellyConfig(),
        bankroll=1000.0,
        backfill_rounds=40,
        max_records=10,
    )
    limited_payload = limited.update(rows)
    check(
        "el límite explícito recorta el registro",
        len(limited_payload["predictions"]) == 10,
        f"n={len(limited_payload['predictions'])}",
    )

    unlimited_loader, unlimited_saver = memory_store()
    unlimited = LiveMonitor(
        unlimited_loader,
        unlimited_saver,
        spec=StrategySpec(),
        payout=EXPRESS_19,
        kelly_config=KellyConfig(),
        bankroll=1000.0,
        backfill_rounds=40,
        max_records=0,
    )
    unlimited_payload = unlimited.update(rows)
    check(
        "sin límite se conservan todas las rondas",
        len(unlimited_payload["predictions"]) == len(live_predictions),
        f"{len(unlimited_payload['predictions'])} vs {len(live_predictions)}",
    )

    fresh_loader, fresh_saver = memory_store()
    fresh = LiveMonitor(
        fresh_loader,
        fresh_saver,
        spec=StrategySpec(),
        payout=EXPRESS_19,
        kelly_config=KellyConfig(),
        bankroll=1000.0,
        backfill_rounds=0,
        max_records=0,
    )
    fresh_payload = fresh.update(rows)
    check(
        "sin backfill sólo se guarda la ronda tomada en vivo",
        len(fresh_payload["predictions"]) == 1
        and fresh_payload["predictions"][0]["status"] == "pendiente"
        and fresh._state.predictions[0].get("origin") == "live"
        and fresh_payload["prediction_stats"]["rounds"] == 0,
        f"n={len(fresh_payload['predictions'])}",
    )

    stale = unlimited_loader()
    stale_store = {
        "version": stale.get("version"),
        "updated_at": stale.get("updated_at"),
        "stats": dict(stale.get("stats") or {}),
        "predictions": [dict(record) for record in stale.get("predictions") or []],
        "pauses": [dict(pause) for pause in stale.get("pauses") or []],
    }
    fake = dict(stale_store["predictions"][0])
    fake.update(
        {
            "source_number": 249983,
            "source_draw": "No. 249983",
            "status": "fallo",
            "actual_number": 0,
            "markov_rank": None,
            "heuristic_rank": None,
            "decision": {},
            "alert_hit": None,
        }
    )
    stale_store["predictions"].append(fake)
    stale_store["stats"]["rounds"] = 999
    stale_loader, stale_saver = memory_store()
    stale_saver(stale_store)
    filtered = LiveMonitor(
        stale_loader,
        stale_saver,
        spec=StrategySpec(),
        payout=EXPRESS_19,
        kelly_config=KellyConfig(),
        bankroll=1000.0,
        backfill_rounds=40,
        max_records=0,
    )
    filtered_payload = filtered.update(rows)
    filtered_resolved = [
        item
        for item in filtered_payload["predictions"]
        if item["status"] in ("acierto", "fallo")
    ]
    check(
        "las predicciones guardadas no se eliminan",
        any(item["source_draw"] == "No. 249983" for item in filtered_payload["predictions"]),
    )
    check(
        "las rondas evaluadas se cuentan desde los registros guardados",
        filtered_payload["prediction_stats"]["rounds"] == len(filtered_resolved),
        f"{filtered_payload['prediction_stats']['rounds']} vs {len(filtered_resolved)}",
    )

    guard_loader, guard_saver = memory_store()
    guard_saver(
        {
            "version": 3,
            "updated_at": None,
            "stats": {},
            "predictions": [dict(stale_store["predictions"][0])],
            "pauses": [],
        }
    )
    guarded = LiveMonitor(
        guard_loader,
        guard_saver,
        spec=StrategySpec(),
        payout=EXPRESS_19,
        kelly_config=KellyConfig(),
        bankroll=1000.0,
        backfill_rounds=0,
        max_records=0,
    )
    guarded._load()
    guarded._state.predictions = []
    guarded._state.dirty = True
    guarded._persist(guarded._state)
    check(
        "no se guarda un estado vacío sobre datos existentes",
        len(guard_loader().get("predictions", [])) == 1,
    )

    shrink_loader, shrink_saver = memory_store()
    first_record = dict(stale_store["predictions"][0])
    second_record = dict(first_record, source_number=249982, source_draw="No. 249982")
    shrink_saver(
        {
            "version": 3,
            "updated_at": None,
            "stats": {},
            "predictions": [first_record, second_record],
            "pauses": [],
        }
    )
    shrink = LiveMonitor(
        shrink_loader,
        shrink_saver,
        spec=StrategySpec(),
        payout=EXPRESS_19,
        kelly_config=KellyConfig(),
        bankroll=1000.0,
        backfill_rounds=0,
        max_records=0,
    )
    shrink._load()
    shrink._state.predictions = shrink._state.predictions[:1]
    shrink._state.dirty = True
    shrink._persist(shrink._state)
    check(
        "no se guarda una lista más corta sobre datos existentes",
        len(shrink_loader().get("predictions", [])) == 2,
        f"n={len(shrink_loader().get('predictions', []))}",
    )

    failed_saves = []

    def failing_loader() -> dict:
        raise RuntimeError("fallo temporal de lectura")

    def failing_saver(data: dict) -> None:
        failed_saves.append(data)

    failed_monitor = LiveMonitor(
        failing_loader,
        failing_saver,
        spec=StrategySpec(),
        payout=EXPRESS_19,
        kelly_config=KellyConfig(),
        bankroll=1000.0,
        backfill_rounds=0,
        max_records=0,
    )
    try:
        failed_monitor.update(rows)
        load_failed = False
    except RuntimeError:
        load_failed = True
    check(
        "un fallo de lectura no sobrescribe el registro",
        load_failed and not failed_saves,
        f"raises={load_failed} saves={len(failed_saves)}",
    )

    stored_live = loader()
    legacy_store = dict(stored_live)
    legacy_store["version"] = 1
    legacy_store["predictions"] = [
        dict(record, preserved=True)
        for record in stored_live.get("predictions", [])
    ]
    preserved_loader, preserved_saver = memory_store()
    preserved_saver(legacy_store)
    preserved_monitor = LiveMonitor(
        preserved_loader,
        preserved_saver,
        spec=StrategySpec(),
        payout=EXPRESS_19,
        kelly_config=KellyConfig(),
        bankroll=1000.0,
        backfill_rounds=40,
        max_records=100,
    )
    preserved_monitor.update(rows)
    expected_sources = {
        int(record["source_number"]) for record in legacy_store["predictions"]
    }
    preserved_sources = {
        int(record["source_number"]): record
        for record in preserved_monitor._state.predictions
    }
    check(
        "las predicciones reales se conservan entre versiones",
        bool(expected_sources)
        and all(
            preserved_sources.get(number, {}).get("preserved")
            for number in expected_sources
        ),
    )

    short_engine = LiveModelEngine()
    for value in values[:20]:
        short_engine.advance(value)
    short_forecast = short_engine.forecast()
    long_engine = LiveModelEngine()
    captured = None
    for index, value in enumerate(values):
        long_engine.advance(value)
        if index == 19:
            captured = long_engine.forecast()
    check(
        "la matriz en vivo tiene los diez submodelos de la especificación (+ R1 de recencia)",
        set(MODEL_ORDER) <= set(short_forecast.numbers) and len(short_forecast.numbers) == 11,
    )
    check(
        "las probabilidades de la matriz son válidas",
        all(0.0 < value < 1.0 for value in short_forecast.probabilities.values()),
    )
    check(
        "la predicción no usa el resultado futuro",
        captured is not None
        and captured.numbers == short_forecast.numbers
        and captured.probabilities == short_forecast.probabilities,
    )

    legacy_loader, legacy_saver = memory_store()
    legacy_saver(
        {
            "predictions": [
                {
                    "source_draw": "No. 1001",
                    "status": "acierto",
                    "markov": [],
                    "heuristic": [],
                }
            ],
            "pauses": [],
        }
    )
    legacy_monitor = LiveMonitor(
        legacy_loader,
        legacy_saver,
        spec=StrategySpec(),
        payout=EXPRESS_19,
        kelly_config=KellyConfig(),
        bankroll=1000.0,
        backfill_rounds=40,
        max_records=100,
    )
    legacy_payload = legacy_monitor.update(rows)
    check(
        "el registro heredado sin matriz se conserva",
        any(
            item.get("source_draw") == "No. 1001"
            for item in legacy_payload["predictions"]
        ),
    )

    print()
    print(f"Comprobaciones superadas: {PASSED}")
    if FAILED:
        print(f"Comprobaciones fallidas: {len(FAILED)}")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("Todas las comprobaciones han pasado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
