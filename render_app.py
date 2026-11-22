"""Servicio web para Render: dashboard, API y ingesta de resultados.

Render **no** consulta Loteka en ningún momento. El scraper corre en el PC
del usuario (IP residencial) y empuja los resultados a `/api/ingest` con un
token compartido. Así el único tráfico hacia Loteka sale desde una IP
residencial, que es la condición necesaria para que el scraping sea
sostenible: los bloqueos de Loteka se producen contra los rangos de IP de los
centros de datos (Vercel, Render, AWS, etc.), no contra las IP residenciales
de los usuarios.

El estado del servicio (histórico y registro vivo) se guarda en disco cuando
hay un disco persistente montado (`RENDER_DATA_DIR`). Sin disco, el servicio
usa la copia del repositorio como base y reconstruye el registro vivo al
arrancar.
"""

from __future__ import annotations

import csv
import hmac
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, make_response, request, send_file

import config_alertas
import state_recovery

try:
    from auth_utils import SESSION_COOKIE, create_session, session_username
except ImportError:
    from api.auth_utils import SESSION_COOKIE, create_session, session_username

from live_monitor import LiveMonitor, json_file_store, memory_store
from m4_engine.kelly import KellyConfig
from papel import registro as papel_registro
from papel import web as papel_web
from recencia import web as recencia_web
from recencia.integridad import ordenar_sin_duplicados
from quant import integration as quant_integration

import os

# Cloudflare Worker URL para evitar bloqueos
CLOUDFLARE_WORKER_URL = os.environ.get("CLOUDFLARE_WORKER_URL", "").strip().rstrip("/")
LOTTEKA_URL = f"{CLOUDFLARE_WORKER_URL}/" if CLOUDFLARE_WORKER_URL else "https://loteka.com.do/"

CSV_NAME = "Resultados_RuletaExpress.csv"
EXPECTED_COLUMNS = ["Fecha", "Hora", "No_Sorteo", "Numero"]

ROOT = Path(__file__).resolve().parent
WEB_PATH = ROOT / "web"

DATA_DIR = os.environ.get("RENDER_DATA_DIR", "").strip()
PUSH_TOKEN = os.environ.get("PUSH_TOKEN", "").strip()
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1").strip() not in ("0", "false", "False", "")
REFRESH_SECONDS = int(os.environ.get("REFRESH_INTERVAL_SECONDS", "30") or 30)
SESSION_DAYS = 7


def draw_number(draw: str) -> int:
    """Extrae el número de sorteo de una etiqueta tipo «No. 249491»."""
    try:
        return int(str(draw).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return -1


def huella(rows: list[dict[str, str]]) -> str:
    """Identifica el conjunto de sorteos (cantidad + suma de identificadores).

    Sirve para detectar huecos intermedios: dos históricos pueden tener el
    mismo número de filas y aun así no contener los mismos sorteos. Si la
    huella del servidor difiere de la local, el PC reenvía todo el histórico.
    """
    numeros = [draw_number(row.get("No_Sorteo", "")) for row in rows if row.get("No_Sorteo")]
    return f"{len(numeros)}:{sum(n for n in numeros if n >= 0)}"


def leer_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        {column: str(row.get(column) or "").strip() for column in EXPECTED_COLUMNS}
        for row in rows
    ]


def escribir_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=EXPECTED_COLUMNS, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def mezclar(
    existentes: list[dict[str, str]], nuevas: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Combina y deduplica sorteos, ordenados del más reciente al más antiguo."""
    # Clave = número de sorteo: «No. 123» y «No.123» son el mismo sorteo.
    limpias = [
        {column: str(row.get(column) or "").strip() for column in EXPECTED_COLUMNS}
        for row in nuevas
        if str(row.get("No_Sorteo") or "").strip()
    ]
    return ordenar_sin_duplicados(list(existentes) + limpias)


class Estado:
    """Estado del servicio: histórico, análisis y registro vivo."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.rows: list[dict[str, str]] = []
        self.recovery: dict[str, Any] | None = None
        self.recovery_error: str | None = None
        self.recovery_draw: str | None = None
        self.monitor_error: str | None = None
        self.last_sync: str | None = None
        self.push_error: str | None = None
        self.quant: dict[str, Any] | None = None
        self.quant_error: str | None = "informe cuantitativo pendiente de cálculo"

        self.data_dir = Path(DATA_DIR) if DATA_DIR else None
        if self.data_dir:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = (
            (self.data_dir / CSV_NAME)
            if self.data_dir
            else Path(tempfile.gettempdir()) / "ruleta_render.csv"
        )
        store = (
            json_file_store(self.data_dir / "registro_live.json")
            if self.data_dir
            else memory_store()
        )
        self.monitor = LiveMonitor(
            *store,
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
        self._cargar()

    # ------------------------------------------------------------------
    def _cargar(self) -> None:
        origen = (
            (self.data_dir / CSV_NAME) if self.data_dir and (self.data_dir / CSV_NAME).exists() else None
        )
        if origen is None:
            origen = ROOT / CSV_NAME
        self.rows = leer_csv(origen)
        self._persistir_csv()
        try:
            self.monitor.update(self.rows)
            self.monitor_error = None
        except Exception as error:  # pragma: no cover - defensivo
            self.monitor_error = str(error)
        self.last_sync = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # El informe cuantitativo tarda ~1-2 s: se calcula en segundo plano
        # para no bloquear el arranque del servicio.
        threading.Thread(target=self.actualizar_quant, daemon=True).start()

    def _persistir_csv(self) -> None:
        try:
            escribir_csv(self.csv_path, self.rows)
        except OSError:
            pass

    # ------------------------------------------------------------------
    def ingest(self, rows: list[dict[str, str]], full: bool) -> dict[str, Any]:
        """Incorpora los sorteos recibidos y refresca el registro vivo."""
        with self.lock:
            self.rows = mezclar([] if full else self.rows, rows)
            self._persistir_csv()
            try:
                self.monitor.update(self.rows)
                self.monitor_error = None
            except Exception as error:
                self.monitor_error = str(error)
            self.last_sync = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return {
                "ok": True,
                "rows": len(self.rows),
                "last_draw": self.rows[0]["No_Sorteo"] if self.rows else None,
                "fingerprint": huella(self.rows),
            }

    def actualizar_quant(self) -> None:
        """Recalcula el informe cuantitativo (cacheado por hash del dataset)."""
        with self.lock:
            rows = list(self.rows)
        summary, error = quant_integration.analysis_for_rows(rows)
        with self.lock:
            self.quant = summary
            self.quant_error = error

    def actualizar_recovery(self) -> None:
        """Recalcula el análisis estadístico (tarda unos segundos)."""
        self.actualizar_quant()
        try:
            recovery = state_recovery.build_state_recovery(self.csv_path)
        except Exception as error:
            with self.lock:
                self.recovery_error = str(error)
            return
        with self.lock:
            self.recovery = recovery
            self.recovery_error = None
            self.recovery_draw = (
                self.rows[0]["No_Sorteo"] if self.rows else None
            )

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            payload: dict[str, Any] = {
                "results": list(self.rows),
                "last_sync": self.last_sync,
                "sync_error": self.push_error,
                "recovery": self.recovery,
                "recovery_error": self.recovery_error,
                "predictions_error": self.monitor_error,
                "refresh_interval_seconds": REFRESH_SECONDS,
            }
            # Modo papel: mismo bloque que en Vercel, con el libro en disco local.
            payload["papel"] = papel_web.panel(payload["results"], papel_registro.AlmacenArchivos())
            payload["recencia"] = recencia_web.panel(payload["results"], papel_registro.AlmacenArchivos())
            payload["storage_restaurado"] = []
            payload.update(self.monitor.payload())
            return quant_integration.attach(
                payload, summary=self.quant, error=self.quant_error
            )


estado = Estado()
app = Flask(__name__, static_folder=None)


def autorizado() -> bool:
    return session_username(request.headers.get("Cookie")) is not None


@app.get("/healthz")
def healthz() -> Any:
    return jsonify({"ok": True, "rows": len(estado.rows)})


@app.get("/readyz")
def readyz() -> Any:
    with estado.lock:
        checks = {
            "rows": bool(estado.rows),
            "monitor": estado.monitor_error is None,
            "quant": estado.quant is not None,
        }
    ready = all(checks.values())
    return jsonify({"ready": ready, "checks": checks}), (200 if ready else 503)


@app.get("/api/v2/analysis")
def analysis() -> Any:
    if not autorizado():
        return jsonify({"error": "Autenticación requerida"}), 401
    with estado.lock:
        return jsonify(
            {
                "api_version": quant_integration.API_VERSION,
                "quant": estado.quant,
                "quant_error": estado.quant_error,
            }
        )


@app.get("/")
def index() -> Any:
    pagina = WEB_PATH / ("index.html" if autorizado() else "login.html")
    return send_file(pagina)


@app.get("/login")
def login_page() -> Any:
    return send_file(WEB_PATH / "login.html")


@app.get("/app.js")
def app_js() -> Any:
    return send_file(WEB_PATH / "app.js", mimetype="text/javascript")


@app.get("/styles.css")
def styles() -> Any:
    return send_file(WEB_PATH / "styles.css", mimetype="text/css")


@app.post("/api/auth")
def auth() -> Any:
    data = request.get_json(silent=True) or {}
    if data.get("action") == "logout":
        response = make_response(jsonify({"authenticated": False}))
        response.set_cookie(
            SESSION_COOKIE, "", max_age=0, path="/", httponly=True,
            secure=COOKIE_SECURE, samesite="Lax",
        )
        return response

    expected_username = os.environ.get("ADMIN_USERNAME", "admin")
    expected_password = os.environ.get("ADMIN_PASSWORD", "")
    if not expected_password:
        return jsonify({"error": "ADMIN_PASSWORD no está configurado"}), 503
    if not hmac.compare_digest(str(data.get("username", "")), expected_username):
        return jsonify({"error": "Credenciales inválidas"}), 401
    if not hmac.compare_digest(str(data.get("password", "")), expected_password):
        return jsonify({"error": "Credenciales inválidas"}), 401

    response = make_response(jsonify({"authenticated": True}))
    response.set_cookie(
        SESSION_COOKIE,
        create_session(expected_username),
        max_age=SESSION_DAYS * 86400,
        path="/",
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="Lax",
    )
    return response


@app.get("/api/dashboard")
def dashboard() -> Any:
    if not autorizado():
        return jsonify({"error": "Autenticación requerida"}), 401
    return jsonify(estado.snapshot())


@app.post("/api/ingest")
def ingest() -> Any:
    """Endpoint de ingesta: lo usa el PC del usuario para empujar los sorteos."""
    if not PUSH_TOKEN:
        return jsonify({"ok": False, "error": "PUSH_TOKEN no está configurado"}), 503
    recibido = request.headers.get("X-Push-Token", "")
    if not hmac.compare_digest(recibido, PUSH_TOKEN):
        return jsonify({"ok": False, "error": "token inválido"}), 401

    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "rows debe ser una lista"}), 400
    full = bool(data.get("full"))

    resultado = estado.ingest(rows, full)
    threading.Thread(target=estado.actualizar_recovery, daemon=True).start()
    return jsonify(resultado)


def main() -> None:
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
