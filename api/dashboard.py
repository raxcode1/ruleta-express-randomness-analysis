from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import config_alertas
import state_recovery
import horario_loteka
try:
    from auth_utils import authorized
except ImportError:
    from api.auth_utils import authorized
from config_sync import INTERVALO_SINCRONIZACION
from live_monitor import LiveMonitor, json_file_store, memory_store
from m4_engine.kelly import KellyConfig
from papel import registro as papel_registro
from papel import web as papel_web
from quant import integration as quant_integration
from recencia import web as recencia_web
from recencia.integridad import ordenar_sin_duplicados
from sync_ruleta_express import (
    backfill_missing_draws,
    extract_results,
    fetch_history_date,
    fetch_page,
)

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None


# Configurar URL usando Worker si está disponible
WORKER_URL = os.environ.get("CLOUDFLARE_WORKER_URL", "").strip().rstrip("/")
if WORKER_URL:
    os.environ["SCRAPER_URL"] = f"{WORKER_URL}/"
    import sync_ruleta_express
    sync_ruleta_express.URL = f"{WORKER_URL}/"
    import scraper_web
    scraper_web.URL = f"{WORKER_URL}/"
PREDICTIONS_PATH = "ruleta_predictions.json"
LOCAL_CSV = Path(__file__).resolve().parents[1] / "Resultados_RuletaExpress.csv"
EXPECTED_COLUMNS = ["Fecha", "Hora", "No_Sorteo", "Numero"]

# Compatibilidad con nombres alternativos
def _normalizar_url_supabase(valor: str) -> str:
    """Normaliza la URL del proyecto Supabase a su raíz (https://xyz.supabase.co).

    Si el usuario configuró una URL con sufijos como /rest/v1, /storage/v1 o
    /pg (endpoint del pooler de base de datos), el Storage API respondería
    PGRST125 'Invalid path' porque la petición no llegaría a /storage/v1.
    """
    url = valor.strip().rstrip("/")
    for sufijo in ("/rest/v1", "/storage/v1", "/auth/v1", "/realtime/v1", "/pg"):
        if url.lower().endswith(sufijo):
            url = url[: -len(sufijo)].rstrip("/")
    return url


SUPABASE_URL = _normalizar_url_supabase(
    os.environ.get("SUPABASE_URL")
    or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    or os.environ.get("SUPABASE_PROJECT_URL")
    or ""
)
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
    or os.environ.get("SUPABASE_SERVICE_ROLE")
    or os.environ.get("SUPABASE_ANON_KEY")
    or os.environ.get("SUPABASE_KEY")
    or ""
).strip()
SUPABASE_BUCKET = (
    os.environ.get("SUPABASE_BUCKET")
    or os.environ.get("SUPABASE_STORAGE_BUCKET")
    or os.environ.get("STORAGE_BUCKET")
    or "ruleta-data"
).strip()


_cached_draw: str | None = None
_cached_recovery: dict | None = None
_monitor: LiveMonitor | None = None
_last_csv_hash: str | None = None
#: True cuando Supabase confirmó que el CSV no existe en el bucket (se perdió).
_csv_remoto_ausente: bool = False


def objeto_ausente(mensaje: str) -> bool:
    """¿El error de Storage significa «ese objeto no existe»?

    Un bucket inexistente, una ruta inválida o un fallo de red NO cuentan:
    en esos casos no se debe restaurar nada, porque el archivo puede seguir
    ahí y se pisaría con una copia más corta.
    """
    if "NoSuchBucket" in mensaje or "Bucket not found" in mensaje or "PGRST125" in mensaje:
        return False
    return "NoSuchKey" in mensaje or "Object not found" in mensaje or '"not_found"' in mensaje


def almacen_papel():
    """Libro del modo papel: Supabase si está configurado; si no, carpeta temporal."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return papel_registro.AlmacenArchivos(Path(tempfile.gettempdir())), False

    def leer(nombre: str) -> str | None:
        try:
            return blob_request("GET", nombre).decode("utf-8-sig")
        except Exception as error:
            if objeto_ausente(str(error)):
                return None
            raise

    def guardar(nombre: str, texto: str) -> None:
        tipo = "text/csv; charset=utf-8" if nombre.endswith(".csv") else "application/json; charset=utf-8"
        blob_request("PUT", nombre, texto.encode("utf-8"), tipo)

    return papel_registro.AlmacenRemoto(leer, guardar), True


def restaurar_csv_perdido(rows: list[dict[str, str]]) -> str | None:
    """Si el CSV desapareció del bucket, lo vuelve a subir. Devuelve un aviso o None."""
    global _csv_remoto_ausente, _last_csv_hash
    if not (_csv_remoto_ausente and rows and SUPABASE_URL and SUPABASE_KEY):
        return None
    payload_csv = serialize_rows(rows)
    blob_request("PUT", "Resultados_RuletaExpress.csv", payload_csv, "text/csv; charset=utf-8")
    _csv_remoto_ausente = False
    _last_csv_hash = str(hash(payload_csv))
    return f"Resultados_RuletaExpress.csv no estaba en Supabase: restaurado con {len(rows)} sorteos."


def storage_object_url(pathname: str) -> str:
    if not SUPABASE_URL or not SUPABASE_BUCKET:
        raise RuntimeError("Faltan SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY o SUPABASE_BUCKET.")
    from urllib.parse import quote
    clean = pathname.strip().lstrip("/")
    # Algunos deployments de Supabase Storage requieren /public/ o rutas alternativas; probar ruta objeto directa
    return f"{SUPABASE_URL}/storage/v1/object/{quote(SUPABASE_BUCKET, safe='')} /{quote(clean)}".replace(" /", "/")


def storage_request(method: str, pathname: str, body: bytes | None = None, content_type: str = "application/json; charset=utf-8") -> bytes:
    if not SUPABASE_URL:
        raise RuntimeError("Falta SUPABASE_URL.")
    if not SUPABASE_KEY:
        raise RuntimeError("Falta SUPABASE_SERVICE_ROLE_KEY.")
    if not SUPABASE_BUCKET:
        raise RuntimeError("Falta SUPABASE_BUCKET.")
    from urllib.parse import quote
    clean = pathname.strip().lstrip("/")
    base = SUPABASE_URL.rstrip("/")
    bucket = SUPABASE_BUCKET
    candidates = []
    # Probar con bucket tal cual sin codificar primero (algunos proyectos lo requieren)
    # Probar primero formato más común con service role key
    # Usar URL encode minimal
    import urllib.parse as up
    path_enc = up.quote(clean)
    candidates.append(f"{base}/storage/v1/object/{bucket}/{path_enc}")
    last_error = None
    for url in candidates:
        headers = {
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "apikey": SUPABASE_KEY,
            "Content-Type": content_type,
            "User-Agent": "ruleta-express-vercel",
        }
        upload_method = method
        if method == "PUT":
            headers["x-upsert"] = "true"
            headers["cache-control"] = "no-cache"
        request = Request(url, data=body, method=upload_method, headers=headers)
        try:
            with urlopen(request, timeout=60) as response:
                return response.read()
        except HTTPError as error:
            try:
                detail = error.read().decode("utf-8", errors="replace")
            except Exception:
                detail = ""
            if error.code == 404 and "NoSuchBucket" in detail:
                raise RuntimeError(
                    f"El bucket de Supabase '{bucket}' no existe. "
                    "Créalo en Supabase Studio -> Storage y reintenta."
                )
            last_error = RuntimeError(f"Supabase Storage {method} {pathname} -> HTTP {error.code}: {detail[:500]}")
            if "PGRST125" in detail or error.code in (400, 404):
                continue
            raise last_error
        except Exception as error:
            last_error = RuntimeError(f"Supabase Storage {method} {pathname} -> {error}")
            continue
    if last_error:
        raise last_error
    raise RuntimeError("Supabase Storage: sin candidatos válidos")


def blob_request(
    method: str,
    pathname: str = "",
    body: bytes | None = None,
    content_type: str = "application/octet-stream",
) -> bytes:
    return storage_request(method, pathname, body, content_type)


def read_csv_bytes(data: bytes) -> list[dict[str, str]]:
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    missing = [column for column in EXPECTED_COLUMNS if column not in (rows[0] if rows else {})]
    if rows and missing:
        raise ValueError(f"Faltan columnas requeridas en el CSV: {missing}")
    return [{column: row.get(column, "") for column in EXPECTED_COLUMNS} for row in rows]


#: Segundos que una instancia caliente reutiliza el CSV ya leído de Supabase.
#: Descargar ~1,3 MB en cada latido (1 080 al día) agota la cuota de salida
#: del plan gratuito de Supabase en pocos días.
CSV_CACHE_SECONDS = 600

#: Días recientes en los que se buscan huecos. Los huecos viejos que Loteka
#: nunca publicó se consultarían en cada llamada sin recuperar nada.
BACKFILL_DIAS_VERCEL = 1

_rows_cache: list[dict[str, str]] | None = None
_rows_cache_at: float = 0.0
#: True si la última lectura del CSV remoto falló por algo distinto de «no existe».
_lectura_remota_fallida: bool = False


def _recordar_filas(rows: list[dict[str, str]]) -> None:
    global _rows_cache, _rows_cache_at
    _rows_cache = list(rows)
    _rows_cache_at = time.monotonic()


def read_rows() -> list[dict[str, str]]:
    "Carga el CSV remoto y combina el histórico versionado como respaldo/base."
    global _csv_remoto_ausente, _lectura_remota_fallida
    if _rows_cache is not None and time.monotonic() - _rows_cache_at < CSV_CACHE_SECONDS:
        return list(_rows_cache)
    local_rows: list[dict[str, str]] = []
    if LOCAL_CSV.exists():
        try:
            local_rows = read_csv_bytes(LOCAL_CSV.read_bytes())
        except Exception:
            local_rows = []
    try:
        remote_rows = read_csv_bytes(blob_request("GET", "Resultados_RuletaExpress.csv"))
    except Exception as error:
        _csv_remoto_ausente = objeto_ausente(str(error))
        _lectura_remota_fallida = not _csv_remoto_ausente
        return local_rows
    _csv_remoto_ausente = False
    _lectura_remota_fallida = False
    rows = merge_results(local_rows, remote_rows)
    _recordar_filas(rows)
    return rows


def serialize_rows(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=EXPECTED_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def prediction_store():
    """Almacén del registro vivo: Supabase Storage."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Faltan SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY o SUPABASE_BUCKET.")

    def load() -> dict:
        try:
            raw = blob_request("GET", PREDICTIONS_PATH)
        except Exception as error:
            message = str(error)
            # Solo se considera "aún no existe" si Storage responde que la
            # clave no está. Un 404 genérico o PGRST125 significa ruta
            # inválida/bucket ausente: si se tratara como lista vacía se
            # reiniciaría el historial de predicciones comparadas.
            if "NoSuchKey" in message:
                return {}
            if "PGRST125" in message or "Invalid path" in message:
                raise RuntimeError(
                    "Supabase Storage: ruta inválida (revisa SUPABASE_URL/SUPABASE_BUCKET). "
                    "No se reinicia el historial de predicciones."
                ) from error
            raise
        try:
            stored = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "El registro de predicciones está dañado."
            ) from error
        if isinstance(stored, list):
            return {"predictions": stored, "pauses": []}
        if not isinstance(stored, dict):
            raise RuntimeError(
                "El registro de predicciones no tiene un formato válido."
            )
        return stored

    def save(data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        blob_request(
            "PUT",
            PREDICTIONS_PATH,
            payload,
            "application/json; charset=utf-8",
        )

    return load, save


def live_monitor() -> LiveMonitor:
    """Monitor vivo compartido por las invocaciones de la función."""
    global _monitor
    if _monitor is None:
        _monitor = LiveMonitor(
            *prediction_store(),
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
    return _monitor


def backfill_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Recupera del histórico diario de Loteka los sorteos que faltan.

    El homepage de Loteka a veces se congela y deja de listar los sorteos
    de las últimas horas de la noche; el endpoint histórico sí los publica.
    Sin este relleno, el monitor vivo interpreta el hueco como una «pausa»
    y marca predicciones como «no evaluadas» que en realidad sí tenían
    resultado. Limitado a los huecos que tocan los últimos días
    (BACKFILL_DIAS_VERCEL): un corte largo que llega hasta hoy se rellena
    entero, pero los huecos viejos que Loteka nunca publicó no se vuelven a
    consultar en cada llamada (gastaban segundos del límite de 60 s).
    """
    if pd is None or not rows:
        return rows
    try:
        frame = pd.DataFrame(rows, columns=EXPECTED_COLUMNS)
        recovered = backfill_missing_draws(frame, [], recent_days=BACKFILL_DIAS_VERCEL)
    except Exception:
        return rows
    if not recovered:
        return rows
    return merge_results(rows, recovered)


def merge_results(rows: list[dict[str, str]], new_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Combina por NÚMERO de sorteo («No. 123» y «No.123» son el mismo) y ordena del más reciente."""
    return ordenar_sin_duplicados(list(rows) + list(new_rows))


def recovery_for_rows(rows: list[dict[str, str]]) -> dict:
    global _cached_draw, _cached_recovery
    current_draw = rows[0]["No_Sorteo"] if rows else None
    if current_draw == _cached_draw and _cached_recovery is not None:
        return _cached_recovery
    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".csv", delete=False
    ) as temporary:
        temporary.write(serialize_rows(rows))
        temporary_path = Path(temporary.name)
    try:
        recovery = state_recovery.build_state_recovery(temporary_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    _cached_draw = current_draw
    _cached_recovery = recovery
    return recovery


def dashboard(ligero: bool = False) -> dict:
    """Ciclo completo del panel.

    ``ligero=True`` (lo usa /api/cron) omite la recuperación de estado y el
    análisis cuantitativo, que sólo sirven para mostrar el panel y son lo más
    costoso: el latido tiene que sincronizar, guardar y registrar la
    predicción en papel con holgura dentro de los 60 s de Vercel.
    """
    try:
        rows = read_rows()
    except Exception as e:
        rows = []
    error: str | None = None
    horario = horario_loteka.estado()
    # Fuera del horario de publicación (07:00-22:00, hora de R. Dominicana)
    # no se consulta el origen: se sirven los datos guardados.
    if horario["abierto"]:
        try:
            try:
                fresh = extract_results(fetch_page())
            except Exception as portada_error:
                # Si la portada falla (bloqueo, cambio de HTML, Worker caído),
                # el histórico del día publica los mismos sorteos: así el
                # panel no se queda congelado en el último sorteo guardado.
                hoy = horario_loteka.ahora().date()
                fresh = fetch_history_date(hoy)
                if not fresh:
                    raise portada_error
                error = f"Portada de Loteka no disponible; usando el histórico del día. ({portada_error})"
            current = merge_results(rows, fresh)
            # El homepage puede quedarse corto (se congela por horas): si hay
            # huecos respecto al histórico, se recuperan antes de alimentar
            # al monitor, para que no se registren pausas falsas.
            current = backfill_rows(current)
            if SUPABASE_URL and SUPABASE_KEY and _lectura_remota_fallida:
                # Sin leer el CSV de Supabase, `rows` es sólo la copia del
                # repositorio (más corta): subirla pisaría el histórico.
                error = error or "No se pudo leer el CSV de Supabase; no se guarda para no pisarlo."
            elif SUPABASE_URL and SUPABASE_KEY and len(current) > len(rows):
                try:
                    global _last_csv_hash
                    payload_csv = serialize_rows(current)
                    current_hash = str(hash(payload_csv))
                    if current_hash != _last_csv_hash:
                        blob_request(
                            "PUT",
                            "Resultados_RuletaExpress.csv",
                            payload_csv,
                            "text/csv; charset=utf-8",
                        )
                        _last_csv_hash = current_hash
                    # Sólo se cachea lo que ya está en Supabase: si el guardado
                    # falla, la siguiente llamada vuelve a leer y reintenta.
                    _recordar_filas(current)
                except Exception as ex:
                    error = f"Error guardando CSV en Supabase: {ex}"
            rows = current
        except Exception as exception:
            error = str(exception)

    storage_restaurado: list[str] = []
    try:
        aviso = restaurar_csv_perdido(rows)
        if aviso:
            storage_restaurado.append(aviso)
    except Exception as exception:
        error = error or f"No se pudo restaurar el CSV en Supabase: {exception}"

    recovery_error: str | None = None
    recovery = None
    if not ligero:
        try:
            recovery = recovery_for_rows(rows)
        except Exception as exception:
            recovery_error = str(exception)

    monitor_error: str | None = None
    monitor_payload: dict = {}
    try:
        m = live_monitor()
        monitor_payload = m.update(rows)
    except Exception as e:
        monitor_error = str(e)
        monitor_payload = {}

    predictions = monitor_payload.get("predictions", [])

    payload = {
        "results": rows,
        "last_sync": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sync_error": error,
        "recovery": recovery,
        "recovery_error": recovery_error,
        "predictions": predictions,
        "pauses": monitor_payload.get("pauses", []),
        "predictions_error": monitor_error,
        "alert": monitor_payload.get("alert"),
        "alert_stats": monitor_payload.get("alert_stats"),
        "alert_history": monitor_payload.get("alert_history", []),
        "prediction_stats": monitor_payload.get("prediction_stats"),
        "refresh_interval_seconds": INTERVALO_SINCRONIZACION,
        "horario": horario,
    }
    # Modo papel (paquete `papel`): predicción registrada antes del sorteo.
    try:
        almacen, persistente = almacen_papel()
        payload["papel"] = papel_web.panel(rows, almacen, persistente)
        storage_restaurado.extend(
            f"{nombre} no estaba en Supabase: restaurado desde el repositorio."
            for nombre in payload["papel"].get("restaurados", [])
        )
    except Exception as exception:
        payload["papel"] = {"error": str(exception)}
    # Modelo de recencia (paquete `recencia`): mismo almacén que el modo papel.
    # Objetos: recencia_modelo.json, recencia_registro.csv, recencia_monitor.json,
    # recencia_pendientes.json y recencia_aciertos.json.
    try:
        almacen, persistente = almacen_papel()
        payload["recencia"] = recencia_web.panel(rows, almacen, persistente)
    except Exception as exception:
        payload["recencia"] = {"error": str(exception)}
    payload["storage_restaurado"] = storage_restaurado
    # Análisis cuantitativo, decisión (SIN VENTAJA por defecto) y puerta de
    # señales; el informe se cachea por hash del dataset.
    if ligero:
        return payload
    return quant_integration.attach(payload, rows=rows)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not authorized(self):
            body = b'{"error":"Autenticacion requerida"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            body = json.dumps(dashboard(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        except Exception as exception:
            body = json.dumps({"results": [], "error": str(exception), "sync_error": str(exception)}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


handler = Handler
