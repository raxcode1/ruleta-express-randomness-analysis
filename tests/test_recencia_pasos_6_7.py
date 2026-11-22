"""Pasos 6 y 7 del plan: reglas M4 desactivadas, base 1/19 e integridad de datos."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

import config_alertas
from live_monitor import LiveMonitor, memory_store
from m4_engine.alerts import AlertEngine
from m4_engine.config import LOTEKA_180
from papel.datos import desde_filas
from papel.registro import AlmacenArchivos
from recencia import integridad as IG
from recencia import registro as RR
from tests.conftest import rows_from_values


# ------------------------------------------------------------------ paso 6
def test_m4_desactivado_por_defecto():
    assert config_alertas.M4_ACTIVO is False


@pytest.mark.parametrize("activas", [True, False])
def test_monitor_m4_con_reglas_desactivadas_no_dispara_ni_sugiere_stake(activas, uniform_values):
    m = LiveMonitor(*memory_store(), payout=LOTEKA_180, reglas_activas=activas)
    alert = m.update(rows_from_values(uniform_values[:400]))["alert"]
    assert alert["available"]
    assert alert["disabled"] is (not activas)
    if not activas:
        assert alert["fired"] is False and alert["state"] == "DESACTIVADA"
        assert alert["stake"] is None and alert["stake_amount"] == 0.0
        assert "M4_ACTIVO" in alert["failures"][0]
        assert "rule_fired_m4" in alert                   # la evaluación original se conserva


def test_alert_engine_usa_el_pago_real_por_defecto():
    assert AlertEngine().payout is LOTEKA_180
    assert LOTEKA_180.break_even == pytest.approx(1 / 18)


# ------------------------------------------------------------------ paso 7
def _filas(ids, fecha="01/01/2026"):
    t0 = datetime(2026, 1, 1, 8, 0)
    return [{"Fecha": fecha, "Hora": (t0 + timedelta(minutes=5 * (i - ids[0]))).strftime("%I:%M %p"),
             "No_Sorteo": f"No. {i}", "Numero": f"{i % 19:02d}"} for i in ids]


def test_numero_sorteo_y_duplicados_por_numero():
    assert IG.numero_sorteo("No. 123") == IG.numero_sorteo("No.123") == IG.numero_sorteo("123") == 123
    filas = _filas([1, 2, 3]) + [{"Fecha": "01/01/2026", "Hora": "09:00 AM", "No_Sorteo": "No.2", "Numero": "07"}]
    out = IG.ordenar_sin_duplicados(filas)
    assert [IG.numero_sorteo(f["No_Sorteo"]) for f in out] == [3, 2, 1]
    assert out[1]["Numero"] == "07"                        # gana la última vista


def test_merge_results_del_panel_normaliza_el_numero():
    from api import dashboard as D
    out = D.merge_results(_filas([1, 2]), [{"Fecha": "01/01/2026", "Hora": "08:05 AM", "No_Sorteo": "No.2", "Numero": "09"}])
    assert len(out) == 2 and out[0]["Numero"] == "09"


def test_sync_csv_quita_duplicados_por_numero(tmp_path):
    import csv
    import sync_ruleta_express as S
    ruta = tmp_path / "r.csv"
    with ruta.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=S.EXPECTED_COLUMNS)
        w.writeheader()
        w.writerows(reversed(_filas([10, 11, 12])))
    S.sync_csv(ruta, [{"Fecha": "01/01/2026", "Hora": "08:10 AM", "No_Sorteo": "No.12", "Numero": "05"}])
    with ruta.open(encoding="utf-8-sig") as fh:
        filas = list(csv.DictReader(fh))
    assert [IG.numero_sorteo(f["No_Sorteo"]) for f in filas] == [12, 11, 10]
    assert filas[0]["Numero"] == "05"


def test_informe_de_huecos():
    d = desde_filas(_filas([1, 2, 5, 6, 7, 10]))
    inf = IG.informe_huecos(d)
    assert inf["faltan"] == 4 and inf["tramos"] == 2 and inf["sorteos"] == 6
    assert inf["ultimos_tramos"][0]["desde"] == 8 and inf["ultimos_tramos"][0]["hasta"] == 9


def test_reparar_nunca_pierde_sorteos(monkeypatch):
    import sync_ruleta_express as S
    base = _filas([1, 2, 5])
    monkeypatch.setattr(S, "backfill_missing_draws", lambda frame, fresh, recent_days=None: _filas([3]))
    out, nuevos = IG.reparar(base)
    assert nuevos == 1 and [IG.numero_sorteo(f["No_Sorteo"]) for f in out] == [5, 3, 2, 1]


def test_registro_valida_contra_la_hora_publicada(tmp_path):
    """Una predicción hecha a la hora (o después) del sorteo PUBLICADO queda inválida."""
    al = AlmacenArchivos(tmp_path)
    vals = np.random.default_rng(0).integers(0, 19, 500)
    rows = rows_from_values(vals)                        # todas a las 10:00 del 01/01/2026
    d = desde_filas(rows)
    publicada = datetime(2026, 1, 1, 10, 0)
    fila = {c: "" for c in RR.CAMPOS}
    objetivo = int(d.sorteo[-1])
    base = dict(fila, sorteo_objetivo=str(objetivo), regimen="diurno", k="3", valida="1",
                numeros_topk="1 2 3", stake_sugerido="0")
    a_tiempo = dict(base, ts_prediccion=(publicada - timedelta(minutes=1)).replace(tzinfo=RR.ZONA).astimezone(timezone.utc).isoformat())
    tarde = dict(base, sorteo_objetivo=str(objetivo - 1),
                 ts_prediccion=(publicada + timedelta(seconds=30)).replace(tzinfo=RR.ZONA).astimezone(timezone.utc).isoformat())
    viejo = dict(a_tiempo, sorteo_objetivo=str(objetivo - 2), resultado="4", acierto_topk="0")   # evaluada sin verificación
    RR.escribir([viejo, tarde, a_tiempo], al)
    assert RR.evaluar(d, al) == 3
    f = {r["sorteo_objetivo"]: r for r in RR.leer(al)}
    assert f[str(objetivo)]["valida_publicada"] == "1" and f[str(objetivo)]["hora_publicada"] == "01/01/2026 10:00"
    assert f[str(objetivo - 1)]["valida_publicada"] == "0"
    assert f[str(objetivo - 2)]["valida_publicada"] == "1" and f[str(objetivo - 2)]["resultado"] == "4"
    r = RR.resumen(RR.leer(al))
    assert r["invalidas_por_hora_publicada"] == 1 and r["evaluadas"] == 2
