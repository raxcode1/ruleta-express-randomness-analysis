from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd

import config_alertas
import state_recovery
import horario_loteka
from config_sync import INTERVALO_SINCRONIZACION, descripcion
from live_monitor import LiveMonitor, json_file_store
from m4_engine.kelly import KellyConfig
from papel import registro as papel_registro
from papel import web as papel_web
from recencia import web as recencia_web
from quant import integration as quant_integration
from sync_ruleta_express import CSV_PATH, run_once


HOST = "127.0.0.1"
SYNC_INTERVAL = INTERVALO_SINCRONIZACION
TIME_ZONE = ZoneInfo("America/Santo_Domingo")
WEB_PATH = Path(__file__).with_name("web")
REGISTRO_PATH = Path(__file__).with_name(config_alertas.ARCHIVO_REGISTRO)


class DashboardState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.last_draw: str | None = None
        self.last_sync: str | None = None
        self.sync_error: str | None = None
        self.recovery: dict | None = None
        self.recovery_error: str | None = None
        self.monitor_error: str | None = None
        self.quant: dict | None = None
        self.quant_error: str | None = "informe cuantitativo pendiente de cálculo"
        self.monitor = LiveMonitor(
            *json_file_store(REGISTRO_PATH),
            spec=config_alertas.spec(),
            payout=config_alertas.modelo_pago(),
            kelly_config=KellyConfig(
                fraction=config_alertas.FRACCION_KELLY,
                max_fraction=config_alertas.TOPE_EXPOSICION,
                min_observations=config_alertas.OBSERVACIONES_MINIMAS,
                min_hits=config_alertas.ACIERTO_MINIMO,
            ),
            bankroll=config_alertas.BANCO_INICIAL,
            backfill_rounds=config_alertas.RONDAS_BACKFILL,
            max_records=config_alertas.RONDAS_GUARDADAS,
            reglas_activas=getattr(config_alertas, "M4_ACTIVO", False),
        )

    def update_recovery(self) -> None:
        recovery = state_recovery.build_state_recovery(CSV_PATH)
        with self.lock:
            self.recovery = recovery
            self.recovery_error = None

    def update_quant(self) -> None:
        """Recalcula el informe cuantitativo (cacheado por hash del dataset)."""
        summary, error = quant_integration.analysis_for_rows(read_results())
        with self.lock:
            self.quant = summary
            self.quant_error = error

    def update_monitor(self) -> None:
        """Actualiza el registro vivo de predicciones y la alerta M4."""
        with self.lock:
            self.monitor.update(read_results())
            self.monitor_error = None

    def snapshot(self) -> dict:
        with self.lock:
            payload = {
                "results": read_results(),
                "last_sync": self.last_sync,
                "sync_error": self.sync_error,
                "recovery": self.recovery,
                "recovery_error": self.recovery_error,
                "predictions_error": self.monitor_error,
                "refresh_interval_seconds": SYNC_INTERVAL,
                "horario": horario_loteka.estado(),
            }
            # Modo papel: mismo bloque que en Vercel, con el libro en disco local.
            payload["papel"] = papel_web.panel(payload["results"], papel_registro.AlmacenArchivos())
            payload["recencia"] = recencia_web.panel(payload["results"], papel_registro.AlmacenArchivos())
            payload["storage_restaurado"] = []
            payload.update(self.monitor.payload())
            return quant_integration.attach(
                payload, summary=self.quant, error=self.quant_error
            )

    def analysis(self) -> dict:
        with self.lock:
            return {
                "api_version": quant_integration.API_VERSION,
                "quant": self.quant,
                "quant_error": self.quant_error,
            }

    def readiness(self) -> tuple[bool, dict]:
        with self.lock:
            checks = {
                "csv": CSV_PATH.exists(),
                "monitor": self.monitor_error is None,
                "quant": self.quant is not None,
            }
        return all(checks.values()), checks


def read_results() -> list[dict[str, str]]:
    if not CSV_PATH.exists():
        return []
    frame = pd.read_csv(CSV_PATH, dtype=str).fillna("")
    expected = ("Fecha", "Hora", "No_Sorteo", "Numero")
    missing = [column for column in expected if column not in frame.columns]
    if missing:
        raise ValueError(f"Faltan columnas requeridas en el CSV: {missing}")
    return frame[list(expected)].to_dict(orient="records")


def latest_draw() -> str | None:
    results = read_results()
    return results[0]["No_Sorteo"] if results else None


def sync_loop(state: DashboardState) -> None:
    while True:
        # Horario de publicación de Loteka (07:00-22:00, hora de R. Dominicana).
        # Fuera de esa ventana no se consulta el origen: el panel sigue
        # sirviendo los datos guardados y se reanuda al abrir.
        if not horario_loteka.abierto():
            local = horario_loteka.ahora()
            with state.lock:
                state.sync_error = None
            print(
                f"Loteka cerrado ({local.strftime('%H:%M')} DR): "
                f"se espera hasta las {horario_loteka.HORA_APERTURA.strftime('%H:%M')} DR.",
                flush=True,
            )
            time.sleep(min(horario_loteka.segundos_hasta_apertura(), 300))
            continue

        try:
            with state.lock:
                previous_draw = state.last_draw
            run_once(CSV_PATH)
            current_draw = latest_draw()
            with state.lock:
                state.last_sync = datetime.now(TIME_ZONE).isoformat(timespec="seconds")
                state.sync_error = None
                state.last_draw = current_draw
            if current_draw and current_draw != previous_draw:
                try:
                    state.update_recovery()
                except Exception as error:
                    with state.lock:
                        state.recovery_error = str(error)
                try:
                    state.update_monitor()
                except Exception as error:
                    with state.lock:
                        state.monitor_error = str(error)
                state.update_quant()
        except Exception as error:
            with state.lock:
                state.sync_error = str(error)
                state.last_sync = datetime.now(TIME_ZONE).isoformat(timespec="seconds")
        time.sleep(SYNC_INTERVAL)


class RequestHandler(BaseHTTPRequestHandler):
    state: DashboardState

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/dashboard":
            self.send_json(self.state.snapshot())
            return
        if path == "/api/v2/analysis":
            self.send_json(self.state.analysis())
            return
        if path == "/healthz":
            self.send_json({"ok": True})
            return
        if path == "/readyz":
            ready, checks = self.state.readiness()
            self.send_json(
                {"ready": ready, "checks": checks},
                HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        if path == "/" or path == "/index.html":
            self.send_file(WEB_PATH / "index.html", "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self.send_file(WEB_PATH / "app.js", "text/javascript; charset=utf-8")
            return
        if path == "/styles.css":
            self.send_file(WEB_PATH / "styles.css", "text/css; charset=utf-8")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str) -> None:
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Muestra resultados de Ruleta Express y los sincroniza automáticamente."
    )
    parser.add_argument("--host", default=HOST, help="Interfaz donde escuchar.")
    parser.add_argument("--port", type=int, default=8000, help="Puerto HTTP.")
    args = parser.parse_args()

    state = DashboardState()
    try:
        state.last_draw = latest_draw()
        state.update_recovery()
    except Exception as error:
        state.recovery_error = str(error)
    try:
        state.update_monitor()
    except Exception as error:
        state.monitor_error = str(error)
    state.update_quant()

    RequestHandler.state = state
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    threading.Thread(target=sync_loop, args=(state,), daemon=True).start()
    print(
        f"Dashboard disponible en http://{args.host}:{args.port} "
        f"(sincronización {descripcion()})",
        flush=True,
    )
    print(
        f"Registro vivo: {REGISTRO_PATH} · stake sobre "
        f"{config_alertas.BANCO_INICIAL:.0f} unidades con el modelo de pago "
        f"{config_alertas.MODELO_PAGO}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
