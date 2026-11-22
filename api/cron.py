"""Latido sin sesiones: /api/cron

Lo llama un planificador externo (el Cron Trigger del Worker de Cloudflare)
cada minuto. Ejecuta el mismo ciclo que el panel —sincronizar resultados,
evaluar y registrar la predicción en papel— aunque nadie tenga la web abierta.

Protegido con la variable de entorno CRON_SECRET: la petición debe traer
`Authorization: Bearer <CRON_SECRET>`. Sin la variable configurada responde
503 y no ejecuta nada.
"""
from __future__ import annotations

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler

try:
    import dashboard as panel
except ImportError:
    from api import dashboard as panel


def autorizado(cabecera: str | None, secreto: str | None) -> bool:
    if not secreto or not cabecera:
        return False
    return hmac.compare_digest(cabecera.strip().encode("utf-8"), f"Bearer {secreto}".encode("utf-8"))


def ejecutar() -> dict:
    """Un ciclo completo; devuelve un resumen pequeño (no el histórico)."""
    datos = panel.dashboard(ligero=True)
    resultados = datos.get("results") or []
    papel = datos.get("papel") or {}
    proxima = papel.get("proxima") or {}
    recencia = datos.get("recencia") or {}
    rp = recencia.get("proxima") or {}
    return {
        "ok": not datos.get("sync_error") and not papel.get("error") and not recencia.get("error"),
        "recencia_error": recencia.get("error"),
        "recencia": {"estado": (recencia.get("monitor") or {}).get("estado"),
                     **{k: rp.get(k) for k in ("sorteo_objetivo", "regimen", "numeros", "stake_sugerido", "valida")}} if rp else None,
        "ultimo_sorteo": resultados[0].get("No_Sorteo") if resultados else None,
        "sync_error": datos.get("sync_error"),
        "papel_error": papel.get("error"),
        "prediccion": {k: proxima.get(k) for k in ("sorteo", "hora_prevista", "pick_regla", "apostar", "tardia")} if proxima else None,
        "persistente": papel.get("persistente"),
        "restaurado": datos.get("storage_restaurado") or [],
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        secreto = os.environ.get("CRON_SECRET", "").strip()
        if not secreto:
            estado, cuerpo = 503, {"error": "Falta la variable de entorno CRON_SECRET."}
        elif not autorizado(self.headers.get("Authorization"), secreto):
            estado, cuerpo = 401, {"error": "No autorizado."}
        else:
            try:
                estado, cuerpo = 200, ejecutar()
            except Exception as error:
                estado, cuerpo = 500, {"ok": False, "error": str(error)}
        body = json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
        self.send_response(estado)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


handler = Handler
