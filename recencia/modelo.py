"""ModeloRecencia: probabilidad de cada número según cuánto hace que salió.

Antigüedad de v en el sorteo t:
    antig(v, t) = sorteo[t] − sorteo[última aparición de v antes de t]
medida en `No_Sorteo`, no en filas, para que un hueco no acorte la distancia.

Dos estimadores, mismos datos y misma interfaz:
  - "conteo": w[r][c] = (aciertos + 1) / (expuestos + 19), con cubetas
    1, 2, 3, 4, 5, 6–8, 9–14, 15–30, >30 y P(v) ∝ w[r][cubeta(v)].
  - "logit": el logit condicional de `papel.modelo.ajustar()` con las mismas
    características de `papel.modelo._fila()`, pero con la antigüedad en
    `No_Sorteo`. Se ajusta un vector por régimen.

Sin look-ahead: `ajustar(d, hasta_indice)` sólo usa sorteos < hasta_indice y la
ventana diurna de jornadas < la jornada de hasta_indice; `probabilidades(d, t)`
sólo mira sorteos < t (y la hora del propio sorteo t, que se conoce antes).
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from papel import K
from papel import modelo as PM
from papel.datos import Datos, siguiente_instante, turno_de

VERSION = "1"
METODOS = ("conteo", "logit")
REGIMENES = ("diurno", "fuera")
#: Primer valor de antigüedad de cada cubeta: 1,2,3,4,5,6–8,9–14,15–30,>30.
BORDES = np.array([1, 2, 3, 4, 5, 6, 9, 15, 31])
NOMBRES_CUBETA = ["1", "2", "3", "4", "5", "6-8", "9-14", "15-30", ">30"]
NC = len(BORDES)
SIN_VER = 10**6            # antigüedad de un número que aún no ha salido
MIN_HISTORIA = 300         # los primeros sorteos no tienen antigüedades fiables
MIN_REGIMEN = 1000         # menos sorteos que esto en un régimen -> usar el otro
MAX_ATRAS = 3000           # tope de la búsqueda hacia atrás para un solo índice


def cubeta(antig: np.ndarray) -> np.ndarray:
    """Antigüedad (>=1) -> índice de cubeta 0..NC-1."""
    return np.searchsorted(BORDES, np.asarray(antig), side="right") - 1


def sorteo_objetivo(d: Datos, indice: int) -> int:
    """No_Sorteo del sorteo `indice`; `len(d)` es el próximo, que aún no salió."""
    return int(d.sorteo[indice]) if indice < len(d) else int(d.sorteo[-1]) + 1


def turno_objetivo(d: Datos, indice: int) -> int:
    return int(d.turno[indice]) if indice < len(d) else turno_de(siguiente_instante(d.ts[-1]))


def antig_observada_1(d: Datos, indice: int) -> bool:
    """¿Se conoce el sorteo inmediatamente anterior? Con hueco, la antigüedad 1 no está observada."""
    return True if indice >= len(d) else bool(d.consec[indice])


def antiguedades(d: Datos) -> np.ndarray:
    """Matriz (len(d)+1, K): fila t = antigüedades vistas antes del sorteo t.

    La última fila corresponde al próximo sorteo (No_Sorteo = último + 1).
    """
    n = len(d)
    A = np.empty((n + 1, K), np.int64)
    ultimo = np.full(K, -SIN_VER, np.int64)
    for t in range(n):
        A[t] = d.sorteo[t] - ultimo
        ultimo[d.num[t]] = d.sorteo[t]
    A[n] = (int(d.sorteo[-1]) + 1 if n else 0) - ultimo
    return np.minimum(A, SIN_VER)


def antiguedad_en(d: Datos, indice: int) -> np.ndarray:
    """Antigüedades (K,) antes del sorteo `indice`, mirando sólo hacia atrás."""
    obj = sorteo_objetivo(d, indice)
    a = np.full(K, SIN_VER, np.int64)
    faltan = K
    for j in range(indice - 1, max(-1, indice - 1 - MAX_ATRAS), -1):
        v = int(d.num[j])
        if a[v] == SIN_VER:
            a[v] = obj - int(d.sorteo[j])
            faltan -= 1
            if not faltan:
                break
    return a


class ModeloRecencia:
    def __init__(self, metodo: str = "conteo") -> None:
        if metodo not in METODOS:
            raise ValueError(f"metodo debe ser uno de {METODOS}")
        self.metodo = metodo
        self.ventana: tuple[int, int] | None = None
        self.w: dict[str, np.ndarray] = {}        # conteo: (NC,) multiplicador frente a 1/19
        self.beta: dict[str, np.ndarray] = {}     # logit: (NF,)
        self.n_entreno: dict[str, int] = {}
        self.aciertos: dict[str, list[int]] = {}
        self.expuestos: dict[str, list[int]] = {}
        self.hasta_sorteo: int | None = None
        self.ajustado_utc: str | None = None

    # ------------------------------------------------------------------ ajuste
    def ajustar(self, d: Datos, hasta_indice: int, A: np.ndarray | None = None) -> None:
        """Ajusta con los sorteos [MIN_HISTORIA, hasta_indice). `A` es opcional (caché de antiguedades)."""
        hasta_indice = int(min(hasta_indice, len(d)))
        hasta_dia = int(d.dia[hasta_indice]) if hasta_indice < len(d) else int(d.dia.max()) + 1
        self.ventana = PM.ventana_diurna(d, hasta_dia=hasta_dia)
        A = antiguedades(d) if A is None else A
        idx = np.arange(MIN_HISTORIA, hasta_indice)
        idx = idx[d.consec[idx]]     # con hueco justo antes, la antigüedad 1 y el anterior no se conocen
        diurno = self._es_diurno(d.turno[idx])
        grupos = {"diurno": idx[diurno], "fuera": idx[~diurno]}
        self.w, self.beta, self.n_entreno, self.aciertos, self.expuestos = {}, {}, {}, {}, {}
        for r, ix in grupos.items():
            self.n_entreno[r] = int(len(ix))
            if len(ix) < MIN_REGIMEN:
                continue
            C = cubeta(A[ix])                                   # (m, K)
            hit = C[np.arange(len(ix)), d.num[ix]]
            ac = np.bincount(hit, minlength=NC)
            ex = np.bincount(C.ravel(), minlength=NC)
            self.aciertos[r], self.expuestos[r] = ac.tolist(), ex.tolist()
            self.w[r] = K * (ac + 1) / (ex + K)
            if self.metodo == "logit":
                F = np.stack([PM._fila(int(d.num[t - 1]), A[t]) for t in ix])
                self.beta[r] = PM.ajustar(F, d.num[ix])
        for r, otro in (("diurno", "fuera"), ("fuera", "diurno")):   # régimen sin datos -> el otro
            if r not in self.w and otro in self.w:
                self.w[r] = self.w[otro]
                if otro in self.beta:
                    self.beta[r] = self.beta[otro]
        if not self.w:
            raise ValueError(f"Historia insuficiente para ajustar ({len(idx)} sorteos útiles).")
        self.hasta_sorteo = int(d.sorteo[hasta_indice - 1])
        self.ajustado_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _es_diurno(self, turnos) -> np.ndarray:
        turnos = np.asarray(turnos)
        if self.ventana is None:
            return np.zeros(turnos.shape, bool)
        return (turnos >= self.ventana[0]) & (turnos <= self.ventana[1])

    def regimen(self, d: Datos, indice: int) -> str:
        return "diurno" if bool(self._es_diurno(turno_objetivo(d, indice))) else "fuera"

    # ------------------------------------------------------------ predicción
    def _p(self, r: str, antig: np.ndarray, num_prev: np.ndarray) -> np.ndarray:
        """antig (m, K), num_prev (m,) -> probabilidades (m, K)."""
        if self.metodo == "logit":
            F = np.stack([PM._fila(int(u), a) for u, a in zip(num_prev, antig)])
            return PM.probabilidades(F, self.beta[r])
        p = self.w[r][cubeta(antig)]
        return p / p.sum(-1, keepdims=True)

    def probabilidades(self, d: Datos, indice: int) -> np.ndarray:
        """P(resultado del sorteo `indice` = v), v = 0..18. `indice = len(d)` es el próximo."""
        if not self.w:
            raise RuntimeError("Modelo sin ajustar.")
        a = antiguedad_en(d, indice)
        prev = np.array([d.num[indice - 1] if indice > 0 else 0])
        return self._p(self.regimen(d, indice), a[None], prev)[0]

    def probabilidades_lote(self, d: Datos, indices: np.ndarray, A: np.ndarray | None = None):
        """Versión vectorizada para el backtest: devuelve (P (m, K), diurno (m,))."""
        indices = np.asarray(indices)
        A = antiguedades(d) if A is None else A
        turnos = np.array([turno_objetivo(d, int(i)) for i in indices])
        diurno = self._es_diurno(turnos)
        prev = d.num[np.maximum(indices - 1, 0)]
        P = np.empty((len(indices), K))
        for r, m in (("diurno", diurno), ("fuera", ~diurno)):
            if m.any():
                P[m] = self._p(r, A[indices[m]], prev[m])
        return P, diurno

    # ----------------------------------------------------------- persistencia
    def multiplicadores(self) -> dict[str, dict[str, float]]:
        return {r: dict(zip(NOMBRES_CUBETA, np.round(self.w[r], 4).tolist())) for r in REGIMENES if r in self.w}

    def a_json(self) -> dict:
        return {
            "version": VERSION, "metodo": self.metodo,
            "ventana": None if self.ventana is None else list(self.ventana),
            "cubetas": NOMBRES_CUBETA,
            "w": {r: v.tolist() for r, v in self.w.items()},
            "beta": {r: v.tolist() for r, v in self.beta.items()},
            "n_entreno": self.n_entreno, "aciertos": self.aciertos, "expuestos": self.expuestos,
            "hasta_sorteo": self.hasta_sorteo, "ajustado_utc": self.ajustado_utc,
        }

    @classmethod
    def desde_json(cls, data: dict) -> "ModeloRecencia":
        if str(data.get("version")) != VERSION:
            raise ValueError(f"Versión de modelo {data.get('version')!r} incompatible con {VERSION!r}.")
        m = cls(data["metodo"])
        m.ventana = None if data.get("ventana") is None else tuple(int(x) for x in data["ventana"])
        m.w = {r: np.array(v, float) for r, v in data["w"].items()}
        m.beta = {r: np.array(v, float) for r, v in data.get("beta", {}).items()}
        m.n_entreno = dict(data.get("n_entreno", {}))
        m.aciertos, m.expuestos = dict(data.get("aciertos", {})), dict(data.get("expuestos", {}))
        m.hasta_sorteo, m.ajustado_utc = data.get("hasta_sorteo"), data.get("ajustado_utc")
        return m
