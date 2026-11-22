"""Punto de entrada de la línea de comandos: `python -m m4_engine`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .alerts import AlertEngine
from .config import EUROPEAN_37, EXPRESS_19, LOTEKA_180, PAYOUT_MODELS, KellyConfig, StrategySpec
from .features import build_features, collapse_conflicts
from .io import load_supervision_csv
from .report import render_report, write_report
from .validation import validate_strategy

DEFAULT_CSV = (
    Path(__file__).resolve().parent.parent
    / "registros_supervision_probs_120 - registros_supervision_probs_120.csv"
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="m4_engine",
        description=(
            "Valida la estrategia cuantitativa M4 sobre el log de supervisión y "
            "emite alertas de alta ventaja con stake dinámico."
        ),
    )
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="CSV de supervisión.")
    parser.add_argument(
        "--primary-mode",
        choices=("any", "tie", "strict"),
        default="any",
        help=(
            "Cómo se exige que M4 sea el modelo principal: 'any' (sólo M4 conduce la "
            "alerta), 'tie' (M4 debe alcanzar la probabilidad máxima) o 'strict' "
            "(M4 debe ser el único máximo)."
        ),
    )
    parser.add_argument(
        "--probability-min", type=float, default=0.05, help="Umbral inferior de la ventana."
    )
    parser.add_argument(
        "--probability-max", type=float, default=0.07, help="Umbral superior de la ventana."
    )
    parser.add_argument(
        "--max-spread", type=float, default=0.047, help="Dispersión máxima admitida."
    )
    parser.add_argument(
        "--payout",
        choices=tuple(PAYOUT_MODELS),
        default=LOTEKA_180.name,
        help="Modelo de pago usado para el dimensionamiento del stake (por defecto el real de Loteka).",
    )
    parser.add_argument(
        "--kelly-fraction", type=float, default=0.25, help="Fracción de Kelly aplicada."
    )
    parser.add_argument(
        "--kelly-max-fraction", type=float, default=0.02, help="Tope de exposición por ronda."
    )
    parser.add_argument(
        "--bankroll", type=float, default=1000.0, help="Bankroll de referencia en unidades."
    )
    parser.add_argument("--output", type=Path, help="Escribe el informe en Markdown.")
    parser.add_argument("--json", type=Path, help="Escribe el informe en JSON.")
    parser.add_argument(
        "--quiet", action="store_true", help="No imprime el informe por pantalla."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        # La consola de Windows usa cp1252: se fuerza UTF-8 y, si algún carácter
        # no es representable, se sustituye en lugar de abortar la ejecución.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)

    spec = StrategySpec(
        probability_min=args.probability_min,
        probability_max=args.probability_max,
        max_spread=args.max_spread,
        primary_mode=args.primary_mode,
    )
    payout = PAYOUT_MODELS[args.payout]
    frame = load_supervision_csv(args.csv)
    report = validate_strategy(frame, spec, payouts=(EUROPEAN_37, EXPRESS_19))

    kelly_config = KellyConfig(
        fraction=args.kelly_fraction,
        max_fraction=args.kelly_max_fraction,
    )
    engine = AlertEngine(spec=spec, payout=payout, kelly_config=kelly_config, bankroll=args.bankroll)
    clean = collapse_conflicts(build_features(frame, spec))
    engine.backfill(clean)

    rendered = render_report(report, engine, args.csv)
    if not args.quiet:
        print(rendered)
    if args.output:
        write_report(args.output, report, engine, args.csv)
        if not args.quiet:
            print(f"Informe escrito en {args.output}")
    if args.json:
        payload = {
            "validation": report.as_dict(),
            "engine": {
                "window_hits": engine.window_estimator.hits,
                "window_trials": engine.window_estimator.trials,
                "alert_hits": engine.alert_estimator.hits,
                "alert_trials": engine.alert_estimator.trials,
            },
        }
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not args.quiet:
            print(f"Informe JSON escrito en {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
