"""Operación en vivo del modelo de recencia: predecir ANTES, evaluar DESPUÉS, medir.

Objetos (mismo almacén que el modo papel: Supabase si está configurado; si no,
archivos en una carpeta local):

- recencia_modelo.json      parámetros ajustados + bloque de decisión (k, evidencia)
- recencia_registro.csv     libro de predicciones; sólo crece. Una fila por sorteo
                            objetivo; al evaluar sólo se rellenan las columnas de
                            resultado, nunca se reescribe la predicción.
- recencia_pendientes.json  vectores de 19 probabilidades de las predicciones aún
                            sin resultado (para p_resultado / log-loss)
- recencia_monitor.json     estado del monitor (paso 4)
- recencia_aciertos.json    resumen de aciertos y retorno, recalculado del libro

`almacen` es cualquier objeto con leer(nombre) -> str | None y guardar(nombre, texto),
como `papel.registro.AlmacenArchivos` / `AlmacenRemoto`.
"""
from __future__ import annotations

import csv
import io
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy import stats

from papel import K
from papel.datos import Datos, siguiente_instante
from papel.registro import AlmacenArchivos

from . import decision as DE
from . import monitor as MO
from .config import ConfigRecencia, config
from .modelo import VERSION, ModeloRecencia

RAIZ = Path(__file__).resolve().parents[1]
MODELO = "recencia_modelo.json"
LIBRO = "recencia_registro.csv"
PENDIENTES = "recencia_pendientes.json"
ACIERTOS = "recencia_aciertos.json"
INFORME = RAIZ / "informe_recencia.json"     # versionado: da k y evidencia si aún no hay modelo publicado
ZONA = ZoneInfo("America/Santo_Domingo")
REAJUSTE_DIAS = 7
ULTIMAS = 30

CAMPOS = ["sorteo_objetivo", "ts_prediccion", "hora_prevista", "regimen", "estado_monitor", "k",
          "numeros_topk", "probabilidades_topk", "numeros", "probabilidades", "stake_sugerido",
          "monto_por_numero", "motivo", "version_modelo", "valida",
          "resultado", "acierto_topk", "acierto", "retorno", "p_resultado",
          # Paso 7: verificación contra la hora PUBLICADA del sorteo objetivo (se rellena al evaluar).
          "hora_publicada", "valida_publicada"]


def es_valida(f: dict) -> bool:
    """Registrada antes de la hora prevista y, si ya se verificó, antes de la publicada."""
    return f.get("valida") == "1" and f.get("valida_publicada", "") != "0"


def _verificar_publicada(f: dict, publicada: datetime) -> None:
    """ts_prediccion (UTC) debe ser anterior al minuto publicado del sorteo (hora de RD)."""
    pred = datetime.fromisoformat(f["ts_prediccion"]).astimezone(ZONA).replace(tzinfo=None)
    f["hora_publicada"] = publicada.strftime("%d/%m/%Y %H:%M")
    f["valida_publicada"] = str(int(pred < publicada))


# --------------------------------------------------------------- almacén
def _leer_json(almacen, nombre: str, defecto=None):
    texto = almacen.leer(nombre)
    if not texto:
        return defecto
    return json.loads(texto)


def _guardar_json(almacen, nombre: str, data) -> None:
    almacen.guardar(nombre, json.dumps(data, ensure_ascii=False, indent=1))


def leer(almacen) -> list[dict[str, str]]:
    texto = almacen.leer(LIBRO)
    return list(csv.DictReader(io.StringIO(texto))) if texto else []


def escribir(filas: list[dict[str, str]], almacen, previas: int | None = None) -> None:
    """Guarda el libro. Se niega a guardar una versión con menos filas que la leída."""
    if previas is not None and len(filas) < previas:
        raise RuntimeError(f"El libro tendría {len(filas)} filas y tenía {previas}: no se sobrescribe.")
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=CAMPOS, lineterminator="\n", extrasaction="ignore")
    w.writeheader()
    w.writerows(filas)
    almacen.guardar(LIBRO, out.getvalue())


def almacen_local(carpeta: Path | None = None):
    return AlmacenArchivos(carpeta)     # PAPEL_DIR o la raíz del repo


# ------------------------------------------------------- modelo y decisión
def decision_por_defecto(cfg: ConfigRecencia) -> dict:
    """k y evidencia fuera de muestra tomados del último backtest versionado."""
    try:
        inf = json.loads(INFORME.read_text(encoding="utf-8"))
        de = inf["decision"]
        return {"k": int(de["k_elegido"]), "evidencia": {int(k): list(v) for k, v in de["evidencia_fuera_de_muestra"].items()},
                "origen": f"informe_recencia.json del repositorio ({inf.get('generado_utc', '?')})"}
    except Exception:
        return {"k": cfg.k_max, "evidencia": None, "origen": "sin backtest: no se sugiere stake"}


def _fecha(d: Datos, sorteo: int | None) -> date | None:
    if sorteo is None:
        return None
    i = int(np.searchsorted(d.sorteo, sorteo))
    return d.ts[i].date() if i < len(d) and d.sorteo[i] == sorteo else None


def modelo_vigente(d: Datos, almacen, cfg: ConfigRecencia | None = None, forzar: bool = False) -> tuple[ModeloRecencia, dict, bool]:
    """Carga recencia_modelo.json; lo reajusta si falta, es de otra versión o tiene ≥7 jornadas.

    Sólo entrena con jornadas cerradas (hasta el inicio de la jornada del último sorteo).
    Devuelve (modelo, bloque_decision, reajustado).
    """
    cfg = cfg or config()
    data = None
    try:
        data = _leer_json(almacen, MODELO)
    except json.JSONDecodeError:
        data = None
    if data and not forzar:
        try:
            m = ModeloRecencia.desde_json(data["modelo"])
            f = _fecha(d, m.hasta_sorteo)
            if f is not None and (d.ts[-1].date() - f).days < REAJUSTE_DIAS:
                return m, data.get("decision") or decision_por_defecto(cfg), False
        except Exception:
            pass
    hasta = int(np.searchsorted(d.dia, d.dia[-1], side="left"))     # inicio de la jornada en curso
    m = ModeloRecencia("conteo")
    m.ajustar(d, hasta)
    dec = (data or {}).get("decision") or decision_por_defecto(cfg)
    _guardar_json(almacen, MODELO, {"version": VERSION, "modelo": m.a_json(), "decision": dec})
    return m, dec, True


def publicar_modelo(d: Datos, almacen, informe: dict) -> dict:
    """Guarda en el almacén el modelo ajustado y la decisión de un backtest recién hecho."""
    de = informe["decision"]
    dec = {"k": int(de["k_elegido"]), "evidencia": {int(k): list(v) for k, v in de["evidencia_fuera_de_muestra"].items()},
           "origen": f"backtest {informe['generado_utc']} ({informe['fuente']})"}
    hasta = int(np.searchsorted(d.dia, d.dia[-1], side="left"))
    m = ModeloRecencia(informe["seleccion"]["elegido"])
    m.ajustar(d, hasta)
    data = {"version": VERSION, "modelo": m.a_json(), "decision": dec}
    _guardar_json(almacen, MODELO, data)
    return data


def evidencia_total(dec: dict, filas: list[dict]) -> dict | None:
    """Evidencia del backtest + aciertos del top-k diurno registrados en vivo."""
    if dec.get("evidencia") is None:
        return None
    ev = {int(k): [int(v[0]), int(v[1])] for k, v in dec["evidencia"].items()}
    k = int(dec["k"])
    vivas = [f for f in filas if f["regimen"] == "diurno" and es_valida(f) and f["acierto_topk"] != "" and f["k"] == str(k)]
    if k in ev:
        ev[k] = [ev[k][0] + sum(f["acierto_topk"] == "1" for f in vivas), ev[k][1] + len(vivas)]
    return ev


def esperado_monitor(dec: dict, cfg: ConfigRecencia) -> dict | None:
    k = int(dec["k"])
    ev = (dec.get("evidencia") or {})
    ac, n = ev.get(k, ev.get(str(k), (0, 0)))
    if not n or ac / n <= k / cfg.pago:
        return None
    return {"k": k, "p_esperada": ac / n, "pago": cfg.pago}


# -------------------------------------------------------------- ciclo
def evaluar(d: Datos, almacen, cfg: ConfigRecencia | None = None) -> int:
    """Rellena resultado/acierto/retorno/p_resultado de las filas cuyo sorteo ya salió."""
    cfg = cfg or config()
    filas = leer(almacen)
    previas = len(filas)
    pos = {int(s): i for i, s in enumerate(d.sorteo)}
    pend = _leer_json(almacen, PENDIENTES, {}) or {}
    n = 0
    for f in filas:
        obj = int(f["sorteo_objetivo"])
        if obj in pos and f["resultado"] != "" and f.get("valida_publicada", "") == "":
            _verificar_publicada(f, d.ts[pos[obj]])     # filas evaluadas antes de existir la verificación
            n += 1
        if f["resultado"] != "" or obj not in pos:
            continue
        _verificar_publicada(f, d.ts[pos[obj]])
        r = int(d.num[pos[obj]])
        f["resultado"] = str(r)
        top = [int(x) for x in f["numeros_topk"].split()] if f["numeros_topk"] else []
        jug = [int(x) for x in f["numeros"].split()] if f["numeros"] else []
        f["acierto_topk"] = str(int(r in top)) if top else ""
        f["acierto"] = str(int(r in jug)) if jug else ""
        stake = float(f["stake_sugerido"] or 0)
        if jug and stake > 0:
            por_num = stake / len(jug)
            f["retorno"] = f"{(cfg.pago * por_num if r in jug else 0.0) - stake:.6f}"
        else:
            f["retorno"] = ""
        p = pend.pop(str(obj), None)
        f["p_resultado"] = f"{p[r]:.5f}" if p else ""
        n += 1
    if n:
        escribir(filas, almacen, previas)
        _guardar_json(almacen, PENDIENTES, pend)
    return n


def predecir(d: Datos, almacen, cfg: ConfigRecencia | None = None, ahora: datetime | None = None,
             estado_monitor: str = DE.ACTIVO, vigente=None) -> dict | None:
    """Registra la predicción del sorteo siguiente al último del histórico (una sola vez)."""
    cfg = cfg or config()
    if len(d) < 400:
        return None
    filas = leer(almacen)
    objetivo = int(d.sorteo[-1]) + 1
    if any(f["sorteo_objetivo"] == str(objetivo) for f in filas):
        return None
    m, dec, _ = vigente or modelo_vigente(d, almacen, cfg)
    p = m.probabilidades(d, len(d))
    regimen = m.regimen(d, len(d))
    k = int(dec["k"])
    top = [int(v) for v in np.argsort(-p, kind="stable")[:k]]
    de = DE.decidir(p, regimen, k, cfg, evidencia_total(dec, filas), estado_monitor)
    prevista = siguiente_instante(d.ts[-1])
    ahora_utc = (ahora or datetime.now(timezone.utc)).astimezone(timezone.utc)
    valida = ahora_utc.astimezone(ZONA).replace(tzinfo=None) < prevista
    fila = {
        "sorteo_objetivo": str(objetivo), "ts_prediccion": ahora_utc.isoformat(timespec="seconds"),
        "hora_prevista": prevista.strftime("%d/%m/%Y %H:%M"), "regimen": regimen, "estado_monitor": estado_monitor,
        "k": str(k), "numeros_topk": " ".join(map(str, top)), "probabilidades_topk": " ".join(f"{p[v]:.5f}" for v in top),
        "numeros": " ".join(map(str, de.numeros)) if de.apostar else "",
        "probabilidades": " ".join(f"{x:.5f}" for x in de.probabilidades) if de.apostar else "",
        "stake_sugerido": f"{de.stake_total:.6f}" if de.apostar and valida else "0",
        "monto_por_numero": f"{de.monto_por_numero:.2f}" if de.apostar and valida else "0",
        "motivo": de.motivo if valida else "predicción tardía: el sorteo pudo haber salido ya",
        "version_modelo": f"{VERSION}/{m.metodo}/{m.hasta_sorteo}", "valida": str(int(valida)),
        "resultado": "", "acierto_topk": "", "acierto": "", "retorno": "", "p_resultado": "",
        "hora_publicada": "", "valida_publicada": "",
    }
    previas = len(filas)
    filas.append(fila)
    escribir(filas, almacen, previas)
    pend = _leer_json(almacen, PENDIENTES, {}) or {}
    pend[str(objetivo)] = [round(float(x), 6) for x in p]
    for clave in sorted(pend, key=int)[:-50]:      # sólo se guardan las 50 más recientes
        pend.pop(clave)
    _guardar_json(almacen, PENDIENTES, pend)
    return fila


def _sin_hora(e: dict | None) -> dict | None:
    return None if e is None else {k: v for k, v in e.items() if k != "actualizado_utc"}


def actualizar_monitor(d: Datos, almacen, cfg: ConfigRecencia | None = None, vigente=None) -> dict:
    cfg = cfg or config()
    filas = leer(almacen)
    m, dec, _ = vigente or modelo_vigente(d, almacen, cfg)
    previo = _leer_json(almacen, MO.OBJETO, None)
    esp = esperado_monitor(dec, cfg)
    if esp is None:
        estado = MO.evaluar(d, [], {"k": int(dec["k"]), "p_esperada": 0.999, "pago": cfg.pago}, previo)
        estado["cusum"]["nota"] = "sin evidencia del backtest: CUSUM desactivado"
    else:
        estado = MO.evaluar(d, filas, esp, previo)
    if _sin_hora(estado) != _sin_hora(previo):      # sólo se escribe si algo cambió
        _guardar_json(almacen, MO.OBJETO, estado)
    return estado


def reanudar_monitor(d: Datos, almacen) -> dict:
    previo = _leer_json(almacen, MO.OBJETO, {}) or {}
    nuevo = MO.reanudar(previo, int(d.sorteo[-1]))
    _guardar_json(almacen, MO.OBJETO, nuevo)
    return nuevo


# --------------------------------------------------------------- medida
def _medida(aciertos: int, n: int, k: int, pago: float) -> dict:
    if n == 0:
        return {"n": 0, "aciertos": 0}
    t = aciertos / n
    return {"n": n, "aciertos": aciertos, "tasa": t, "azar": k / K, "equilibrio": k / pago,
            "retorno_por_peso": pago * t / k,
            "p_vs_azar": float(stats.binomtest(aciertos, n, k / K, alternative="greater").pvalue),
            "p_vs_equilibrio": float(stats.binomtest(aciertos, n, min(1.0, k / pago), alternative="greater").pvalue)}


def resumen(filas: list[dict], cfg: ConfigRecencia | None = None) -> dict:
    """Aciertos del top-k por régimen y resultado de las jugadas sugeridas (sólo filas válidas)."""
    cfg = cfg or config()
    ev = [f for f in filas if f["resultado"] != "" and es_valida(f) and f["acierto_topk"] != ""]
    out = {"actualizado_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pago": cfg.pago,
           "predicciones": len(filas), "evaluadas": len(ev), "tardias": sum(f["valida"] != "1" for f in filas),
           "invalidas_por_hora_publicada": sum(f.get("valida_publicada") == "0" for f in filas),
           "pendientes": sum(f["resultado"] == "" for f in filas), "top_k": {}}
    for reg in ("diurno", "fuera", "total"):
        sub = ev if reg == "total" else [f for f in ev if f["regimen"] == reg]
        por_k = {}
        for f in sub:
            por_k.setdefault(int(f["k"]), []).append(f["acierto_topk"] == "1")
        out["top_k"][reg] = {str(k): _medida(sum(v), len(v), k, cfg.pago) for k, v in sorted(por_k.items())}
    ap = [f for f in ev if f["retorno"] != ""]
    apostado = sum(float(f["stake_sugerido"]) for f in ap)
    ganancia = sum(float(f["retorno"]) for f in ap)
    logs = [math.log1p(float(f["retorno"])) for f in ap]
    out["jugadas"] = {"n": len(ap), "aciertos": sum(f["acierto"] == "1" for f in ap),
                      "tasa": (sum(f["acierto"] == "1" for f in ap) / len(ap)) if ap else None,
                      "apostado_fraccion": apostado, "ganancia_fraccion": ganancia,
                      "retorno_por_peso": (apostado + ganancia) / apostado if apostado else None,
                      "banco_x": math.exp(sum(logs)) if logs else 1.0}
    pr = [float(f["p_resultado"]) for f in ev if f["p_resultado"] != ""]
    out["log_loss"] = {"n": len(pr), "modelo": (-sum(map(math.log, pr)) / len(pr)) if pr else None, "uniforme": math.log(K)}
    racha = 0
    for f in reversed([f for f in ev if f["regimen"] == "diurno"]):
        if f["acierto_topk"] == "1":
            break
        racha += 1
    out["racha_fallos_diurno"] = racha
    return out


def guardar_resumen(almacen, cfg: ConfigRecencia | None = None) -> dict:
    r = resumen(leer(almacen), cfg)
    _guardar_json(almacen, ACIERTOS, r)
    return r


def ciclo(d: Datos, almacen, cfg: ConfigRecencia | None = None) -> dict:
    """Una pasada completa: evaluar -> monitor -> predecir -> resumen. Devuelve lo necesario para el panel."""
    cfg = cfg or config()
    evaluadas = evaluar(d, almacen, cfg)
    vigente = modelo_vigente(d, almacen, cfg)
    estado = actualizar_monitor(d, almacen, cfg, vigente)
    nueva = predecir(d, almacen, cfg, estado_monitor=estado["estado"], vigente=vigente)
    res = guardar_resumen(almacen, cfg) if (evaluadas or nueva) else (_leer_json(almacen, ACIERTOS) or guardar_resumen(almacen, cfg))
    return {"evaluadas": evaluadas, "nueva": nueva, "monitor": estado, "aciertos": res,
            "modelo": vigente[0], "decision": vigente[1], "reajustado": vigente[2]}
