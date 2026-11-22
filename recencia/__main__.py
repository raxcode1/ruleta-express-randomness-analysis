"""CLI del modelo de recencia.

    python -m recencia backtest   [--fuente local|supabase|auto]   walk-forward 60/10/30 + informe
    python -m recencia publicar   [--fuente ...]   backtest + guarda recencia_modelo.json en el almacén
    python -m recencia ciclo      [--fuente ...]   evaluar + monitor + predecir, una vez
    python -m recencia vivo       [--fuente local --sincronizar]   bucle: lo anterior cada --cada segundos
    python -m recencia estado     [--fuente ...]   monitor, próxima predicción y aciertos
    python -m recencia reanudar   [--fuente ...]   levanta la pausa del CUSUM
    python -m recencia integridad [--fuente ...] [--reparar]   huecos en No_Sorteo; --reparar los pide a Loteka

Con --fuente local los objetos se guardan como archivos en la raíz del repo (o
en PAPEL_DIR); con supabase, en el bucket, con las mismas funciones que el panel.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from papel.datos import CSV_PATH

from . import backtest as B
from . import registro as R
from .fuente import FUENTES, cargar_datos, supabase_configurado


def _almacen(fuente: str):
    if fuente == "auto":
        fuente = "supabase" if supabase_configurado() else "local"
    if fuente == "supabase":
        from api import dashboard
        almacen, persistente = dashboard.almacen_papel()
        if not persistente:
            raise RuntimeError("Supabase no configurado.")
        return almacen, "supabase"
    return R.almacen_local(), f"local:{R.almacen_local().carpeta}"


def _estado(c: dict) -> str:
    mo, f, a = c["monitor"], c.get("nueva") or c.get("proxima"), c["aciertos"]
    L = [f"Monitor: {mo['estado'].upper()}" + (f" · {'; '.join(mo['motivos'])}" if mo["motivos"] else ""),
         f"  repetición: {mo['repeticion']['detalle']} · CUSUM {mo['cusum']['estadistico']}/{mo['cusum']['umbral']} "
         f"(n={mo['cusum']['n']}) · log-loss 500: {mo['calibracion']['log_loss']}"]
    if f:
        L.append(f"Sorteo {f['sorteo_objetivo']} ({f['hora_prevista']}, {f['regimen']}): top-{f['k']} = {f['numeros_topk']} "
                 f"[{f['probabilidades_topk']}] · jugar: {f['numeros'] or '—'} · stake {float(f['stake_sugerido']):.2%} "
                 f"({f['monto_por_numero']} por número) · {f['motivo']}")
    j = a["jugadas"]
    L.append(f"Aciertos: {a['evaluadas']} evaluadas · top-k diurno: "
             + ", ".join(f"k={k}: {x['aciertos']}/{x['n']}" for k, x in a["top_k"].get("diurno", {}).items())
             + f" · jugadas: {j['aciertos']}/{j['n']}" + (f" retorno {j['retorno_por_peso']:.1%}" if j["retorno_por_peso"] else ""))
    return "\n".join(L)


def _integridad(a) -> None:
    from papel.datos import desde_filas

    from .fuente import filas_supabase
    from .integridad import informe_huecos, reparar

    fuente = ("supabase" if supabase_configurado() else "local") if a.fuente == "auto" else a.fuente
    if fuente == "supabase":
        filas = filas_supabase()
    else:
        from api import dashboard
        filas = dashboard.read_csv_bytes(Path(a.csv).read_bytes())
    inf = informe_huecos(desde_filas(filas))
    print(f"{fuente}: {inf['sorteos']} sorteos (No. {inf['primero']}–{inf['ultimo']}) · faltan {inf['faltan']} "
          f"en {inf['tramos']} tramos · {inf['faltan_recientes']} en los últimos {inf['dias_recientes']} días")
    for t in inf["ultimos_tramos"]:
        print(f"  No. {t['desde']}–{t['hasta']} ({t['faltan']}) entre {t['entre'][0]} y {t['entre'][1]}")
    if not a.reparar:
        print("Para pedirlos a Loteka: python -m recencia integridad --reparar" + (" --fuente supabase" if fuente == "supabase" else ""))
        return
    combinadas, nuevos = reparar(filas)
    if not nuevos:
        print("Loteka no devolvió ninguno de los sorteos que faltan: no se escribe nada.")
        return
    from api import dashboard
    datos = dashboard.serialize_rows(combinadas)
    if fuente == "supabase":
        dashboard.blob_request("PUT", "Resultados_RuletaExpress.csv", datos, "text/csv; charset=utf-8")
    else:
        destino = Path(a.csv)
        tmp = destino.with_suffix(".csv.tmp")
        tmp.write_bytes(datos)
        tmp.replace(destino)
    print(f"Recuperados {nuevos} sorteos; {len(combinadas)} en total. Guardado en {fuente}.")


def main() -> None:
    ap = argparse.ArgumentParser(prog="recencia", description="Modelo de recencia de Ruleta Express (no apuesta).")
    ap.add_argument("orden", choices=["backtest", "publicar", "ciclo", "vivo", "estado", "reanudar", "integridad"])
    ap.add_argument("--fuente", choices=FUENTES, default="local",
                    help="local = CSV y archivos del repo; supabase = bucket (variables SUPABASE_*); auto = supabase si está configurado")
    ap.add_argument("--csv", type=Path, default=CSV_PATH, help="CSV a usar con --fuente local")
    ap.add_argument("--metodo", choices=["auto", "conteo", "logit"], default="auto", help="auto = el de menor log-loss en validación")
    ap.add_argument("--fraccion", type=float, default=B.FRACCION_KELLY, help="fracción de Kelly del backtest")
    ap.add_argument("--salida", type=Path, default=B.RAIZ, help="carpeta de informe_recencia.md/.json")
    ap.add_argument("--cada", type=int, default=45, help="segundos entre pasadas en 'vivo'")
    ap.add_argument("--reparar", action="store_true", help="en 'integridad': consultar a Loteka los sorteos que faltan y guardarlos")
    ap.add_argument("--sincronizar", action="store_true", help="en 'vivo' con fuente local: actualizar el CSV desde Loteka en cada pasada")
    a = ap.parse_args()
    try:   # la consola de Windows (cp1252) no imprime → ni ⁴
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    if a.orden in ("backtest", "publicar"):
        d, origen = cargar_datos(a.fuente, a.csv)
        print(f"Datos: {origen} · {len(d)} sorteos · {d.ts[0]:%d/%m/%Y} → {d.ts[-1]:%d/%m/%Y %H:%M}", flush=True)
        r = B.ejecutar(d, a.metodo, a.fraccion, origen)
        md, js = B.guardar(r, a.salida)
        if a.orden == "backtest":
            print(B.informe_md(r))
        else:
            almacen, donde = _almacen(a.fuente)
            data = R.publicar_modelo(d, almacen, r)
            print(f"Modelo publicado en {donde}: k={data['decision']['k']} · hasta No. {data['modelo']['hasta_sorteo']}")
        print(f"Informe: {md} y {js}")
        return

    if a.orden == "integridad":
        _integridad(a)
        return

    almacen, donde = _almacen(a.fuente)
    if a.orden == "vivo":
        visto = None
        while True:
            try:
                if a.sincronizar and a.fuente == "local":
                    import horario_loteka
                    import sync_ruleta_express
                    if horario_loteka.abierto():
                        sync_ruleta_express.run_once(a.csv)
                d, _ = cargar_datos(a.fuente, a.csv)
                if int(d.sorteo[-1]) != visto:
                    visto = int(d.sorteo[-1])
                    print(_estado(R.ciclo(d, almacen)), flush=True)
            except Exception as error:   # el bucle no debe morir por un CSV a medio escribir o un corte de red
                print(f"Aviso: {type(error).__name__}: {error}", flush=True)
            time.sleep(max(15, a.cada))

    d, origen = cargar_datos(a.fuente, a.csv)
    if a.orden == "reanudar":
        print(json.dumps(R.reanudar_monitor(d, almacen), ensure_ascii=False, indent=1))
        return
    c = R.ciclo(d, almacen)
    if a.orden == "estado":
        objetivo = str(int(d.sorteo[-1]) + 1)
        c["proxima"] = next((f for f in reversed(R.leer(almacen)) if f["sorteo_objetivo"] == objetivo), None)
    print(f"Datos: {origen} · almacén: {donde}")
    print(_estado(c))


if __name__ == "__main__":
    main()
