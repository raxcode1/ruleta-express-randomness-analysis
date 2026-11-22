from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np
import pytest

from papel.datos import Datos, TURNOS_DIA
from recencia import modelo as RM
from recencia.modelo import ModeloRecencia

DIURNO = (42, 179)   # 08:33–19:58 en turnos, como el CSV sintético de test_papel


def _datos(num, ids=None) -> Datos:
    """Datos con 204 sorteos por jornada desde las 05:03; `ids` permite huecos."""
    num = np.asarray(num, np.int64)
    ids = np.arange(1000, 1000 + len(num)) if ids is None else np.asarray(ids, np.int64)
    pos = ids - ids[0]                       # el hueco también consume un turno
    t0 = datetime(2026, 1, 1, 5, 3)
    ts = [t0 + timedelta(days=int(p // TURNOS_DIA), minutes=5 * int(p % TURNOS_DIA)) for p in pos]
    turno = (pos % TURNOS_DIA).astype(np.int64)
    dia = (pos // TURNOS_DIA).astype(np.int64)
    consec = np.r_[False, np.diff(ids) == 1]
    return Datos(ids, num, ts, turno, dia, consec)


def _anti_repeticion(n, seed=3, solo_diurno=True):
    rng = np.random.default_rng(seed)
    out, prev = np.empty(n, np.int64), -1
    for t in range(n):
        x = int(rng.integers(19))
        if not solo_diurno or DIURNO[0] <= t % TURNOS_DIA <= DIURNO[1]:
            while x == prev:
                x = int(rng.integers(19))
        out[t] = prev = x
    return out


@pytest.fixture(scope="module")
def datos_anti():
    return _datos(_anti_repeticion(40 * TURNOS_DIA))


@pytest.fixture(scope="module")
def modelo_anti(datos_anti):
    m = ModeloRecencia()
    m.ajustar(datos_anti, len(datos_anti) - TURNOS_DIA)
    return m


def test_cubetas():
    assert RM.cubeta([1, 2, 5, 6, 8, 9, 14, 15, 30, 31, RM.SIN_VER]).tolist() == [0, 1, 4, 5, 5, 6, 6, 7, 7, 8, 8]


@pytest.mark.parametrize("metodo", RM.METODOS)
def test_probabilidades_suman_uno_y_no_negativas(datos_anti, metodo):
    m = ModeloRecencia(metodo)
    m.ajustar(datos_anti, len(datos_anti) - TURNOS_DIA)
    for t in (len(datos_anti) - 150, len(datos_anti) - 1, len(datos_anti)):
        p = m.probabilidades(datos_anti, t)
        assert p.shape == (19,)
        assert np.all(p >= 0)
        assert p.sum() == pytest.approx(1.0)


@pytest.mark.parametrize("metodo", RM.METODOS)
def test_sin_look_ahead(datos_anti, metodo):
    t = len(datos_anti) - 100
    m = ModeloRecencia(metodo)
    m.ajustar(datos_anti, t)
    base = m.probabilidades(datos_anti, t)
    num = datos_anti.num.copy()
    num[t:] = (num[t:] + 7) % 19                   # se cambia el objetivo y todo lo posterior
    d2 = Datos(datos_anti.sorteo, num, datos_anti.ts, datos_anti.turno, datos_anti.dia, datos_anti.consec)
    assert np.allclose(m.probabilidades(d2, t), base)
    m2 = ModeloRecencia(metodo)
    m2.ajustar(d2, t)                              # el ajuste tampoco ve el sorteo t ni posteriores
    assert m2.a_json()["w"] == m.a_json()["w"]
    assert np.allclose(m2.probabilidades(d2, t), base)


def test_lote_coincide_con_individual(datos_anti, modelo_anti):
    idx = np.arange(len(datos_anti) - 300, len(datos_anti) + 1)
    P, _ = modelo_anti.probabilidades_lote(datos_anti, idx)
    for j in (0, 150, len(idx) - 1):
        assert np.allclose(P[j], modelo_anti.probabilidades(datos_anti, int(idx[j])))


def test_huecos_en_no_sorteo_se_reflejan_en_la_antiguedad():
    #           ids: 1000 1001 1002 [1003-1004 faltan] 1005
    d = _datos([3, 5, 7, 9], ids=[1000, 1001, 1002, 1005])
    a = RM.antiguedad_en(d, 3)
    assert a[7] == 3 and a[5] == 4 and a[3] == 5          # por No_Sorteo, no por fila
    assert a.min() > 1 and not RM.antig_observada_1(d, 3)
    assert RM.antig_observada_1(d, 2)
    A = RM.antiguedades(d)
    assert np.array_equal(A[3], a)
    assert A[4][9] == 1                                    # el próximo (1006) sí ve al 9 en antig 1


def test_huecos_se_excluyen_del_entreno():
    v = _anti_repeticion(40 * TURNOS_DIA, seed=5)
    ids = np.arange(1000, 1000 + len(v))
    ids[5000:] += 1                                        # un hueco en la fila 5000
    d = _datos(v, ids)
    m = ModeloRecencia()
    m.ajustar(d, len(d))
    assert sum(m.n_entreno.values()) == len(d) - RM.MIN_HISTORIA - 1


def test_anti_repeticion_asigna_menos_de_2pct_al_ultimo(datos_anti, modelo_anti):
    t = len(datos_anti) - TURNOS_DIA + 100                 # turno 100: dentro de la franja
    assert modelo_anti.regimen(datos_anti, t) == "diurno"
    p = modelo_anti.probabilidades(datos_anti, t)
    assert p[datos_anti.num[t - 1]] < 0.02
    assert modelo_anti.multiplicadores()["diurno"]["1"] < 0.1
    assert modelo_anti.multiplicadores()["fuera"]["1"] > 0.6    # fuera de franja no hay sesgo


def test_iid_uniforme_multiplicadores_cerca_de_1():
    d = _datos(np.random.default_rng(11).integers(0, 19, 40 * TURNOS_DIA))
    m = ModeloRecencia()
    m.ajustar(d, len(d))
    for r, w in m.w.items():
        assert np.all(np.abs(w - 1) < 0.15), (r, w)
    p = m.probabilidades(d, len(d))
    assert np.abs(p - 1 / 19).max() < 0.01


def test_json_ida_y_vuelta(datos_anti, modelo_anti):
    m2 = ModeloRecencia.desde_json(modelo_anti.a_json())
    t = len(datos_anti)
    assert np.allclose(m2.probabilidades(datos_anti, t), modelo_anti.probabilidades(datos_anti, t))
    with pytest.raises(ValueError):
        ModeloRecencia.desde_json({**modelo_anti.a_json(), "version": "0"})


# ------------------------------------------------------------- backtest y fuente
from recencia import backtest as B
from recencia import fuente as FU


@pytest.mark.parametrize("S,k", [(0.08, 1), (0.15, 2), (0.30, 5), (0.04, 1), (0.25, 6)])
def test_kelly_cerrado_coincide_con_minimize_scalar(S, k):
    assert float(B.kelly_k(S, k)) == pytest.approx(B.kelly_k_numerico(S, k), abs=1e-6)


def test_cortes_alineados_a_jornada(datos_anti):
    i_val, i_test = B.cortes(datos_anti)
    assert 0 < i_val < i_test < len(datos_anti)
    assert datos_anti.dia[i_val] != datos_anti.dia[i_val - 1] and datos_anti.dia[i_test] != datos_anti.dia[i_test - 1]


def test_walk_forward_sin_look_ahead(datos_anti):
    i_val, i_test = B.cortes(datos_anti)
    base = B.walk_forward(datos_anti, i_test, len(datos_anti), "conteo")
    num = datos_anti.num.copy()
    ult = base["idx"][-1]
    num[ult] = (num[ult] + 5) % 19                         # cambiar el último resultado
    d2 = Datos(datos_anti.sorteo, num, datos_anti.ts, datos_anti.turno, datos_anti.dia, datos_anti.consec)
    otro = B.walk_forward(d2, i_test, len(d2), "conteo")
    assert np.allclose(base["P"], otro["P"])
    assert np.array_equal(base["idx"], np.arange(i_test, len(datos_anti)))


def test_ejecutar_y_guardar(datos_anti, tmp_path):
    r = B.ejecutar(datos_anti, "conteo", fuente="sintetico")
    md, js = B.guardar(r, tmp_path)
    assert md.exists() and js.exists()
    # sólo se prohíbe repetir: log(19/18) ≈ 0.054 de mejora teórica en el régimen diurno
    assert 0.03 < r["prueba"]["diurno"]["mejora_log_loss"] < 0.08
    assert abs(r["prueba"]["fuera"]["mejora_log_loss"]) < 0.03
    assert r["prueba"]["total"]["n"] == len(datos_anti) - B.cortes(datos_anti)[1]


def test_fuente_local_y_supabase(tmp_path, monkeypatch):
    from api import dashboard as D
    csv_txt = "Fecha,Hora,No_Sorteo,Numero\n01/01/2026,10:05 AM,No. 2,07\n01/01/2026,10:00 AM,No. 1,03\n"
    ruta = tmp_path / "r.csv"
    ruta.write_text(csv_txt, encoding="utf-8")
    d, origen = FU.cargar_datos("local", ruta)
    assert origen.startswith("local:") and d.num.tolist() == [3, 7]
    monkeypatch.setattr(D, "SUPABASE_URL", "")
    monkeypatch.setattr(D, "SUPABASE_KEY", "")
    assert FU.cargar_datos("auto", ruta)[1].startswith("local:")      # sin Supabase -> local
    with pytest.raises(RuntimeError):
        FU.cargar_datos("supabase", ruta)                             # pedido explícito: no cae a local
    monkeypatch.setattr(D, "SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setattr(D, "SUPABASE_KEY", "k")
    monkeypatch.setattr(D, "SUPABASE_BUCKET", "ruleta-data")
    monkeypatch.setattr(D, "blob_request", lambda m, p, *a: csv_txt.encode() if p == FU.OBJETO_CSV else b"")
    d, origen = FU.cargar_datos("auto", ruta)
    assert origen == "supabase:ruleta-data/Resultados_RuletaExpress.csv" and d.num.tolist() == [3, 7]


# ----------------------------------------------------------------- decisión (paso 3)
from recencia import decision as DE
from recencia.config import ConfigRecencia, config as config_recencia


def _p_con(top: dict[int, float]) -> np.ndarray:
    p = np.zeros(19)
    for v, x in top.items():
        p[v] = x
    resto = [v for v in range(19) if v not in top]
    p[resto] = (1 - sum(top.values())) / len(resto)
    return p


@pytest.mark.parametrize("S,k", [(0.08, 1), (0.15, 2), (0.21, 3)])
def test_f_kelly_coincide_con_formula_cerrada(S, k):
    assert DE.f_kelly(S, k, 18.0) == pytest.approx(float(B.kelly_k(S, k, 18.0)), abs=1e-6)
    assert DE.f_kelly(k / 18 - 1e-4, k, 18.0) == 0.0


def test_config_lee_config_alertas():
    c = config_recencia()
    assert (c.k_max, c.fraccion_kelly, c.margen, c.tope_ronda, c.pago) == (3, 0.25, 0.05, 0.03, 18.0)


def test_no_apuesta_fuera_de_franja_ni_con_monitor_pausado():
    p = _p_con({4: 0.08, 9: 0.075})
    c = ConfigRecencia()
    assert DE.decidir(p, "diurno", 2, c).apostar
    for d in (DE.decidir(p, "fuera", 2, c), DE.decidir(p, "diurno", 2, c, estado_monitor=DE.PAUSADO),
              DE.decidir(p, "diurno", 2, ConfigRecencia(activo=False))):
        assert not d.apostar and d.stake_total == 0 and d.monto_por_numero == 0


def test_margen_filtra_numeros_y_tope_limita_la_ronda():
    p = _p_con({4: 0.09, 9: 0.057})               # 18·0.057 = 1.026 < 1.05 -> fuera
    d = DE.decidir(p, "diurno", 3, ConfigRecencia())
    assert d.apostar and d.numeros == [4] and d.k == 1
    d = DE.decidir(_p_con({1: 0.2, 2: 0.2, 3: 0.2}), "diurno", 3, ConfigRecencia(fraccion_kelly=1.0))
    assert d.stake_total == pytest.approx(0.03)   # Kelly pleno sería enorme: lo corta el tope


def test_defensas_de_kelly_con_evidencia():
    p = _p_con({4: 0.08, 9: 0.075})
    c = ConfigRecencia()
    pobre = DE.decidir(p, "diurno", 2, c, evidencia={2: (230, 2000)})   # 11.5%: Wilson inferior < 1/9
    assert not pobre.apostar and "evidencia" in pobre.motivo
    buena = DE.decidir(p, "diurno", 2, c, evidencia={2: (1100, 8000)})  # 13.75%
    sin = DE.decidir(p, "diurno", 2, c)
    assert buena.apostar and buena.prob_usada < sin.prob_usada and buena.stake_total < sin.stake_total


def test_elegir_k_y_simular():
    rng = np.random.default_rng(0)
    P = np.tile(_p_con({4: 0.09, 9: 0.08, 2: 0.07}), (3000, 1))
    y = rng.choice(19, size=3000, p=P[0])
    di = np.ones(3000, bool)
    k, res = DE.elegir_k(P, y, di, ConfigRecencia())
    assert k in res and res[k]["crec_log_medio"] == max(r["crec_log_medio"] for r in res.values())
    fuera = DE.simular(P, y, ~di, 2, ConfigRecencia())
    assert fuera["apuestas"] == 0 and fuera["crec_log_medio"] == 0


# ------------------------------------------------------------------- monitor (paso 4)
from recencia import monitor as MO

ESPERADO = {"k": 3, "p_esperada": 0.1976, "pago": 18.0}


def _filas(aciertos, p_res=0.06, desde=5000):
    return [{"sorteo_objetivo": str(desde + i), "regimen": "diurno", "valida": "1",
             "acierto_topk": str(int(a)), "p_resultado": f"{p_res:.5f}"} for i, a in enumerate(aciertos)]


def test_monitor_activo_con_datos_sanos(datos_anti):
    rng = np.random.default_rng(1)
    e = MO.evaluar(datos_anti, _filas(rng.random(3000) < 0.1976), ESPERADO)
    assert e["estado"] == DE.ACTIVO and e["repeticion"]["ok"] and not e["cusum"]["alarma"]
    assert e["calibracion"]["mejora"] > 0


def test_monitor_pausa_si_el_acierto_cae_al_equilibrio_y_es_pegajoso(datos_anti):
    rng = np.random.default_rng(2)
    filas = _filas(rng.random(4000) < 3 / 18)
    e = MO.evaluar(datos_anti, filas, ESPERADO)
    assert e["estado"] == DE.PAUSADO and e["cusum"]["alarma"] and e["pausa_cusum"]
    assert not DE.decidir(_p_con({4: 0.09}), "diurno", 3, ConfigRecencia(), estado_monitor=e["estado"]).apostar
    sanas = filas + _filas(np.ones(50, bool), desde=9000)        # aunque luego acierte, sigue pausado
    assert MO.evaluar(datos_anti, sanas, ESPERADO, previo=e)["estado"] == DE.PAUSADO
    r = MO.reanudar(e, 9049)
    e2 = MO.evaluar(datos_anti, sanas, ESPERADO, previo=r)
    assert e2["estado"] == DE.ACTIVO and e2["cusum"]["n"] == 0


def test_monitor_pausa_si_vuelven_las_repeticiones(datos_anti):
    num = datos_anti.num.copy()
    ult = np.flatnonzero((datos_anti.dia > datos_anti.dia.max() - 7) & (datos_anti.turno >= 60) & (datos_anti.turno <= 160))
    num[ult[1::10]] = num[ult[1::10] - 1]                        # ~10 % de repeticiones diurnas
    d2 = Datos(datos_anti.sorteo, num, datos_anti.ts, datos_anti.turno, datos_anti.dia, datos_anti.consec)
    e = MO.evaluar(d2, [], ESPERADO)
    assert e["estado"] == DE.PAUSADO and not e["repeticion"]["ok"]


def test_monitor_alerta_por_calibracion(datos_anti):
    rng = np.random.default_rng(3)
    e = MO.evaluar(datos_anti, _filas(rng.random(600) < 0.1976, p_res=0.04), ESPERADO)   # -ln 0.04 > ln 19
    assert e["estado"] == DE.ALERTA and e["calibracion"]["alerta"]
    assert DE.decidir(_p_con({4: 0.09}), "diurno", 3, ConfigRecencia(), estado_monitor=e["estado"]).apostar


def test_cusum_no_alarma_con_h0_cierta():
    rng = np.random.default_rng(4)
    falsas = sum(MO.cusum(list(rng.random(5000) < 0.1976), 0.1976, 3 / 18)["alarma_en"] is not None for _ in range(40))
    assert falsas <= 4


# ------------------------------------------------------------- en vivo (paso 5)
from datetime import timezone as _tz
from zoneinfo import ZoneInfo

from papel.datos import siguiente_instante
from papel.registro import AlmacenArchivos, AlmacenRemoto
from recencia import registro as RR
from recencia import web as RW

_RD = ZoneInfo("America/Santo_Domingo")


def _prefijo(d: Datos, n: int) -> Datos:
    return Datos(d.sorteo[:n], d.num[:n], d.ts[:n], d.turno[:n], d.dia[:n], d.consec[:n])


def _antes_de(d: Datos, minutos=2):
    """Instante UTC unos minutos antes de la hora prevista del próximo sorteo."""
    return (siguiente_instante(d.ts[-1]) - timedelta(minutes=minutos)).replace(tzinfo=_RD).astimezone(_tz.utc)


class _Bucket:
    """Almacén en memoria con la semántica de Supabase: None sólo si el objeto no existe."""

    def __init__(self):
        self.objetos: dict[str, str] = {}

    def leer(self, nombre):
        return self.objetos.get(nombre)

    def guardar(self, nombre, texto):
        self.objetos[nombre] = texto


def _dia_turno(d: Datos, turno: int) -> int:
    """Índice del sorteo `turno` de la última jornada completa."""
    return int(np.flatnonzero((d.dia == d.dia.max()) & (d.turno == turno))[0])


def test_predecir_una_vez_y_evaluar_sin_reescribir(datos_anti, tmp_path):
    al = AlmacenArchivos(tmp_path)
    i = _dia_turno(datos_anti, 100)                   # el próximo es el turno 100: diurno
    d0 = _prefijo(datos_anti, i)
    f = RR.predecir(d0, al, ahora=_antes_de(d0))
    assert f and f["sorteo_objetivo"] == str(int(d0.sorteo[-1]) + 1) and f["valida"] == "1"
    assert f["regimen"] == "diurno" and len(f["numeros_topk"].split()) == int(f["k"])
    assert int(d0.num[-1]) not in map(int, f["numeros_topk"].split())   # anti-repetición
    assert RR.predecir(d0, al, ahora=_antes_de(d0)) is None             # una sola vez por sorteo
    antes = dict(RR.leer(al)[0])
    d1 = _prefijo(datos_anti, i + 1)
    assert RR.evaluar(d1, al) == 1
    despues = RR.leer(al)[0]
    assert despues["resultado"] == str(int(datos_anti.num[i])) and despues["p_resultado"] != ""
    assert despues["acierto_topk"] == str(int(int(datos_anti.num[i]) in map(int, antes["numeros_topk"].split())))
    for c in RR.CAMPOS[: RR.CAMPOS.index("resultado")]:
        assert despues[c] == antes[c], c                                   # la predicción no se toca
    if float(antes["stake_sugerido"]) > 0:
        assert despues["retorno"] != ""
    assert RR.evaluar(d1, al) == 0


def test_stake_cero_fuera_de_franja_y_tardia(datos_anti, tmp_path):
    al = AlmacenArchivos(tmp_path)
    d0 = _prefijo(datos_anti, _dia_turno(datos_anti, 5))                  # 05:28: fuera de franja
    f = RR.predecir(d0, al, ahora=_antes_de(d0))
    assert f["regimen"] == "fuera" and f["stake_sugerido"] == "0" and f["numeros"] == ""
    d1 = _prefijo(datos_anti, _dia_turno(datos_anti, 100))
    tarde = RR.predecir(d1, al, ahora=_antes_de(d1, minutos=-3))          # 3 min DESPUÉS de la hora prevista
    assert tarde["valida"] == "0" and tarde["stake_sugerido"] == "0" and "tardía" in tarde["motivo"]


def test_stake_cero_con_monitor_pausado(datos_anti, tmp_path):
    al = AlmacenArchivos(tmp_path)
    d0 = _prefijo(datos_anti, _dia_turno(datos_anti, 100))
    f = RR.predecir(d0, al, ahora=_antes_de(d0), estado_monitor=DE.PAUSADO)
    assert f["stake_sugerido"] == "0" and "pausado" in f["motivo"]


def test_no_sobrescribe_el_libro_con_menos_filas(tmp_path):
    al = AlmacenArchivos(tmp_path)
    with pytest.raises(RuntimeError):
        RR.escribir([], al, previas=3)


def test_modelo_se_guarda_y_se_reutiliza(datos_anti):
    b = _Bucket()
    m, dec, nuevo = RR.modelo_vigente(datos_anti, b)
    assert nuevo and RR.MODELO in b.objetos
    assert m.hasta_sorteo is not None and m.hasta_sorteo < int(datos_anti.sorteo[-1])
    hoy = datos_anti.dia == datos_anti.dia.max()
    assert m.hasta_sorteo == int(datos_anti.sorteo[~hoy][-1])          # sólo jornadas cerradas
    assert RR.modelo_vigente(datos_anti, b)[2] is False                # misma semana: no reajusta
    assert dec["k"] >= 1


def test_ciclo_y_panel_con_almacen_tipo_supabase(datos_anti):
    b = _Bucket()
    rem = AlmacenRemoto(b.leer, b.guardar, semilla=None)
    rows = [{"Fecha": t.strftime("%d/%m/%Y"), "Hora": t.strftime("%I:%M %p"), "No_Sorteo": f"No. {s}", "Numero": f"{v:02d}"}
            for s, v, t in zip(datos_anti.sorteo, datos_anti.num, datos_anti.ts)]
    out = RW.panel(rows, rem, persistente=True)
    assert "error" not in out, out
    assert out["regimen"] in ("diurno", "fuera") and out["monitor"]["estado"] in (DE.ACTIVO, DE.ALERTA, DE.PAUSADO)
    assert out["proxima"]["sorteo_objetivo"] == str(int(datos_anti.sorteo[-1]) + 1)
    for nombre in (RR.MODELO, RR.LIBRO, RR.ACIERTOS, RR.PENDIENTES):
        assert nombre in b.objetos, nombre
    assert json.loads(b.objetos[RR.ACIERTOS])["predicciones"] == 1
    assert RW.panel([], rem)["error"]                                     # nunca lanza


def test_resumen_de_aciertos(datos_anti, tmp_path):
    al = AlmacenArchivos(tmp_path)
    i = _dia_turno(datos_anti, 60)
    for t in range(i, i + 40):                       # 40 sorteos diurnos "en vivo"
        d = _prefijo(datos_anti, t)
        RR.evaluar(d, al)
        RR.predecir(d, al, ahora=_antes_de(d))
    RR.evaluar(_prefijo(datos_anti, i + 40), al)
    r = RR.guardar_resumen(al)
    assert r["evaluadas"] == 40 and r["pendientes"] == 0
    dk = r["top_k"]["diurno"]
    k = next(iter(dk))
    assert dk[k]["n"] == 40 and 0 <= dk[k]["aciertos"] <= 40
    assert r["log_loss"]["n"] == 40
    assert json.loads((tmp_path / RR.ACIERTOS).read_text(encoding="utf-8"))["evaluadas"] == 40


def test_r1_en_la_matriz_en_vivo_sin_tocar_la_regla_m4():
    from live_models import LIVE_MODELS, LiveModelEngine
    from m4_engine.config import MODEL_ORDER
    vals = _anti_repeticion(3000, solo_diurno=False).tolist()
    e = LiveModelEngine()
    e.run(vals)
    p = e.recency_distribution()
    assert sum(p) == pytest.approx(1.0) and p[vals[-1]] < 0.01         # aprende la anti-repetición
    fc = e.forecast()
    assert "R1" in fc.numbers and set(MODEL_ORDER) <= set(fc.numbers)
    assert [m.name for m in LIVE_MODELS[:10]] == list(MODEL_ORDER)
    e2 = LiveModelEngine()
    e2.run(vals[:-1])
    antes = e2.recency_distribution()
    e3 = LiveModelEngine()
    e3.run(vals[:-1])
    assert e3.recency_distribution() == antes                             # determinista y sin el futuro
