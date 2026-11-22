const number = (value) => String(value).padStart(2, "0");
const percent = (value) => `${(value * 100).toFixed(2)}%`;
const units = (value) => `${Number(value).toFixed(2)} u`;
const isBlank = (value) => value === null || value === undefined;
const escapeHtml = (value) =>
  String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtP = (value) => (isBlank(value) ? "-" : Number(value) < 1e-4 ? Number(value).toExponential(1) : Number(value).toFixed(4));

// El intervalo real llega en la respuesta de la API (config_sync.py).
// Este valor sólo se usa hasta la primera respuesta.
let refreshIntervalMs = 45000;
let refreshTimer = null;

function render(data) {
  const results = data.results || [];
  const latest = results[0];
  document.querySelector("#result-count").textContent = `${results.length} resultados`;
  document.querySelector("#last-sync").textContent = data.last_sync
    ? `sync: ${new Date(data.last_sync).toLocaleTimeString()}`
    : "sin sincronización";

  const intervaloSeconds = Math.round(refreshIntervalMs / 1000);
  const cerrado = Boolean(data.horario) && data.horario.abierto === false;
  document.querySelector("#sync-dot").classList.toggle("error", Boolean(data.sync_error));
  document.querySelector("#sync-status").textContent = data.sync_error
    ? `Error: ${data.sync_error}`
    : cerrado
    ? "Scraping en pausa · fuera del horario de Loteka"
    : `Sincronización activa · cada ${intervaloSeconds} s`;

  renderHorario(data.horario);

  document.querySelector("#latest-result").textContent = latest ? number(latest.Numero) : "-";
  document.querySelector("#latest-meta").textContent = latest
    ? `${latest.No_Sorteo} · ${latest.Fecha} ${latest.Hora}`
    : "No hay resultados guardados.";

  renderRecovery(data.recovery, data.recovery_error);
  renderResults(results);
  try {
    renderRecencia(data);
  } catch (error) {
    // la tarjeta nueva nunca debe impedir que se pinten las demás
    const box = document.querySelector("#recencia-error");
    box.classList.remove("hidden");
    box.textContent = `Recencia: ${error.message}`;
  }
  renderPapel(data);
  renderQuant(data);
  renderAlert(data);
  renderPredictions(data);
  renderPauses(data.pauses);
}

function renderHorario(horario) {
  const target = document.querySelector("#horario-status");
  if (!horario) {
    target.textContent = "";
    return;
  }
  const ventana = `${horario.apertura}-${horario.cierre} DR`;
  if (horario.abierto) {
    const cierra = Math.max(0, Math.round((horario.segundos_hasta_cierre || 0) / 60));
    target.textContent = `Loteka abierto · cierra en ${cierra} min`;
    return;
  }
  const abre = Math.max(0, Math.round((horario.segundos_hasta_apertura || 0) / 60));
  target.textContent = `Loteka cerrado (${ventana}) · abre en ${abre} min`;
}

function renderRecovery(recovery, recoveryError) {
  const errorBox = document.querySelector("#recovery-error");
  errorBox.textContent = recoveryError || "";
  errorBox.classList.toggle("hidden", !recoveryError);
  if (!recovery) return;
  const model = recovery.selected_model;
  document.querySelector("#sample-size").textContent = `${recovery.sample_size} observaciones`;
  document.querySelector("#top1").textContent = percent(model.top1);
  document.querySelector("#top3").textContent = percent(model.top3);
  document.querySelector("#log-loss").textContent = model.log_loss.toFixed(4);
  document.querySelector("#markov-forecast").innerHTML = recovery.markov_forecast
    .map((item, index) => `<li><span>${index + 1}. ${number(item.number)}</span><strong>${percent(item.probability)}</strong></li>`)
    .join("");
  document.querySelector("#heuristic-forecast").innerHTML = recovery.heuristic_forecast
    .map((item, index) => `<li><span>${index + 1}. ${number(item.number)}</span><strong>${percent(item.probability)}</strong></li>`)
    .join("");
}

function renderQuant(data) {
  const decision = data.decision || {};
  const quant = data.quant;
  const badge = document.querySelector("#decision-state");
  if (!badge) return;
  const status = decision.status || "SIN VENTAJA";
  badge.textContent = status;
  badge.className = `alert-badge ${decision.signals_allowed ? "active" : "idle"}`;
  const errorBox = document.querySelector("#quant-error");
  errorBox.textContent = data.quant_error || "";
  errorBox.classList.toggle("hidden", !data.quant_error);
  document.querySelector("#quant-mode").textContent = decision.trading_mode === "live" ? "real" : "paper-trading";
  document.querySelector("#quant-reasons").innerHTML = (decision.reasons || [])
    .map((reason) => `<li>${escapeHtml(reason)}</li>`)
    .join("");
  if (!quant) return;
  const ds = quant.dataset || {};
  const age = quant.generated_at ? Math.round((Date.now() - new Date(quant.generated_at).getTime()) / 60000) : null;
  document.querySelector("#quant-null").textContent = `Hipótesis nula: ${quant.null_hypothesis}.`;
  document.querySelector("#quant-data").textContent =
    `${ds.rows} sorteos · ${ds.missing_draws} ausentes · informe hace ${age === null ? "-" : age} min`;
  const model = quant.selected_model || {};
  document.querySelector("#quant-model").textContent = `${model.name} · λ=${Number(quant.recalibration_lambda).toFixed(3)}`;
  const test = quant.test || {};
  const dm = quant.diebold_mariano || {};
  document.querySelector("#quant-logloss").textContent =
    `${Number(test.log_loss).toFixed(4)} vs ${Number(test.log_loss_uniform).toFixed(4)} (p=${fmtP(dm.p_value)})`;
  const binom = quant.top1_binomial_test || {};
  document.querySelector("#quant-top1").textContent =
    `${percent(test.top1)} vs azar ${percent(test.baseline_top1)} · break-even ${percent(quant.break_even || 0)} (p=${fmtP(binom.p_value)}, n=${test.n})`;
  document.querySelector("#quant-ece").textContent = Number(test.ece_top_label).toFixed(4);
  document.querySelector("#quant-tests").innerHTML = (quant.goodness_of_fit || [])
    .map((t) => `<tr><td>${escapeHtml(t.name)}</td><td>${Number(t.statistic).toFixed(3)}</td><td>${isBlank(t.df) ? "-" : t.df}</td>
      <td>${fmtP(t.p_value)}</td><td>${fmtP(t.p_value_holm)}</td><td>${t.reject_holm ? "sí" : "no"}</td></tr>`)
    .join("");
  const mc = quant.monte_carlo || {};
  const bank = mc.bankroll_null_1pct || {};
  document.querySelector("#quant-mc").textContent =
    `Monte Carlo: p nula log-loss=${fmtP(mc.null_log_loss_p_value)} · p nula top-1=${fmtP(mc.null_top1_p_value)} · ` +
    `apostando 1% bajo la nula: ROI mediano ${percent(bank.roi_q50 || 0)}, drawdown q95 ${percent(bank.max_drawdown_q95 || 0)}. ` +
    `${quant.goodness_interpretation || ""}`;
  const prng = quant.prng || {};
  document.querySelector("#quant-prng").textContent =
    `PRNG: ${prng.verdict || "-"} · ` + (prng.hypotheses || []).map((h) => `${h.generator}: ${h.status}`).join(" · ");
}

function renderRecencia(data) {
  const rec = data.recencia || {};
  const badge = document.querySelector("#recencia-state");
  const errorBox = document.querySelector("#recencia-error");
  errorBox.classList.toggle("hidden", !rec.error);
  errorBox.textContent = rec.error ? `Recencia: ${rec.error}` : "";
  document.querySelector("#recencia-advertencias").innerHTML = (rec.advertencias || [])
    .map((a) => `<li>${escapeHtml(a)}</li>`).join("");
  if (rec.error || !rec.activo || !rec.aciertos) {
    badge.textContent = rec.activo === false ? "DESACTIVADO" : "SIN DATOS";
    badge.className = "alert-badge idle";
    return;
  }
  const mon = rec.monitor || {};
  const p = rec.proxima;
  const stake = p ? Number(p.stake_sugerido || 0) : 0;
  badge.textContent = mon.estado === "pausado" ? "PAUSADO" : stake > 0 ? "JUGADA SUGERIDA" : "NO APOSTAR";
  badge.className = `alert-badge ${stake > 0 && mon.estado !== "pausado" ? "active" : "idle"}`;
  const lista = (s) => (s ? s.split(" ").map(number).join(" · ") : "");
  document.querySelector("#recencia-numbers").textContent = p && p.numeros ? lista(p.numeros) : "-";
  const detalle = [];
  if (p) {
    detalle.push(`Sorteo ${p.sorteo_objetivo} · previsto ${p.hora_prevista}`);
    const probs = (p.probabilidades_topk || "").split(" ").map((x) => percent(Number(x)));
    detalle.push(`top-${p.k}: ` + (p.numeros_topk || "").split(" ").map((n, i) => `${number(n)} (${probs[i]})`).join(", "));
    if (stake > 0) detalle.push(`stake ${percent(stake)} del banco · ${p.monto_por_numero} por número`);
    detalle.push(p.motivo);
  }
  document.querySelector("#recencia-detail").textContent = detalle.join(" · ") || "Sin predicción abierta.";
  const ventana = rec.ventana ? `${rec.ventana[0]}–${rec.ventana[1]}` : "no reconocida";
  document.querySelector("#recencia-regimen").textContent =
    `Régimen actual: ${rec.regimen} · franja diurna ${ventana} · modelo ${rec.modelo.metodo} hasta No. ${rec.modelo.hasta_sorteo}` +
    (rec.persistente ? "" : " · SIN SUPABASE: el registro no se conserva");
  const cu = mon.cusum || {};
  const cal = mon.calibracion || {};
  document.querySelector("#recencia-monitor").textContent =
    `Monitor: ${String(mon.estado || "-").toUpperCase()}` + ((mon.motivos || []).length ? ` (${mon.motivos.join("; ")})` : "") +
    ` · repetición: ${(mon.repeticion || {}).detalle || "-"} · CUSUM ${cu.estadistico ?? "-"}/${cu.umbral ?? "-"} (n=${cu.n ?? 0})` +
    (cal.log_loss != null ? ` · log-loss 500: ${Number(cal.log_loss).toFixed(4)} vs ${Number(cal.uniforme).toFixed(4)}` : "");
  const a = rec.aciertos;
  const k = String(rec.decision.k);
  const medida = (m) => (m && m.n ? `${m.aciertos}/${m.n} · ${percent(m.tasa)} (equilibrio ${percent(m.equilibrio)}) · retorno ${(m.retorno_por_peso * 100).toFixed(1)}%` : "sin datos");
  document.querySelector("#recencia-topk").textContent = medida(((a.top_k || {}).diurno || {})[k]);
  document.querySelector("#recencia-fuera").textContent = medida(((a.top_k || {}).fuera || {})[k]);
  const j = a.jugadas || {};
  document.querySelector("#recencia-jugadas").textContent = j.n
    ? `${j.aciertos}/${j.n} · retorno ${(j.retorno_por_peso * 100).toFixed(1)}% · banco ×${Number(j.banco_x).toFixed(3)}`
    : "sin datos";
  document.querySelector("#recencia-refs").textContent =
    `${a.evaluadas} evaluadas · ${a.pendientes} pendientes · ${a.tardias} tardías · racha sin acierto diurno: ${a.racha_fallos_diurno}` +
    ` · k=${k} (${rec.decision.origen || "-"})`;
  const ig = rec.integridad || {};
  const ultimo = (ig.ultimos_tramos || [])[0];
  document.querySelector("#recencia-integridad").textContent = ig.sorteos
    ? `Integridad: ${ig.sorteos} sorteos · faltan ${ig.faltan} en ${ig.tramos} tramo(s) (${ig.faltan_recientes} en los últimos ${ig.dias_recientes} días)` +
      (ultimo ? ` · último hueco No. ${ultimo.desde}–${ultimo.hasta} (${ultimo.entre[0]} → ${ultimo.entre[1]})` : "") +
      ` · predicciones inválidas por hora publicada: ${a.invalidas_por_hora_publicada || 0} · reparar: ${ig.reparar}`
    : "";
  const marca = (s, ok) => (!s ? "-" : `<span class="${ok === "1" ? "hit" : "miss"}">${lista(s)}</span>`);
  document.querySelector("#recencia-body").innerHTML = (rec.ultimas || [])
    .map((f) => `<tr><td>${escapeHtml(f.sorteo_objetivo)}</td><td>${escapeHtml(f.hora_prevista)}</td><td>${escapeHtml(f.regimen)}</td>` +
      `<td>${marca(f.numeros_topk, f.acierto_topk)}</td><td>${marca(f.numeros, f.acierto)}</td>` +
      `<td>${Number(f.stake_sugerido || 0) > 0 ? percent(Number(f.stake_sugerido)) : f.valida === "1" ? "0" : "tardía"}</td><td>${number(f.resultado)}</td></tr>`)
    .join("");
}

function renderPapel(data) {
  const papel = data.papel || {};
  const badge = document.querySelector("#papel-state");
  const errorBox = document.querySelector("#papel-error");
  errorBox.classList.toggle("hidden", !papel.error);
  errorBox.textContent = papel.error ? `Modo papel: ${papel.error}` : "";
  document.querySelector("#papel-restaurado").textContent = (data.storage_restaurado || []).join(" · ");
  if (papel.error || !papel.resumen) {
    badge.textContent = "SIN DATOS";
    badge.className = "alert-badge idle";
    return;
  }
  const proxima = papel.proxima;
  const apostar = Boolean(proxima) && proxima.apostar === "1";
  badge.textContent = !papel.regimen_ok ? "RÉGIMEN ROTO" : apostar ? "APOSTABLE (PAPEL)" : "NO APOSTAR";
  badge.className = `alert-badge ${apostar ? "active" : "idle"}`;
  document.querySelector("#papel-number").textContent =
    proxima && proxima.pick_regla !== "" ? number(proxima.pick_regla) : "-";
  const detalle = [];
  if (proxima) {
    detalle.push(`Sorteo ${proxima.sorteo} · previsto ${proxima.hora_prevista}`);
    if (proxima.pick_modelo !== "") detalle.push(`modelo: ${number(proxima.pick_modelo)} (p=${Number(proxima.p_modelo).toFixed(4)})`);
    if (proxima.tardia === "1") detalle.push("registrada tarde: no cuenta");
    else if (proxima.en_ventana !== "1") detalle.push("fuera de la ventana diurna");
  }
  document.querySelector("#papel-detail").textContent = detalle.join(" · ") || "Sin predicción abierta.";
  const ventana = papel.ventana ? `${papel.ventana[0]}–${papel.ventana[1]}` : "no reconocida";
  document.querySelector("#papel-regimen").textContent =
    `Ventana diurna: ${ventana} · ${papel.regimen_detalle}` + (papel.persistente ? "" : " · SIN SUPABASE: el registro no se conserva");
  const r = papel.resumen;
  const medida = (m) => (m && m.n ? `${m.aciertos}/${m.n} · ${percent(m.tasa)} · ROI ${(m.roi * 100).toFixed(1)}%` : "sin datos");
  document.querySelector("#papel-regla").textContent = medida(r.regla);
  document.querySelector("#papel-modelo").textContent = medida(r.modelo);
  document.querySelector("#papel-control").textContent = medida(r.control);
  document.querySelector("#papel-refs").textContent =
    `Azar ${percent(r.azar)} · equilibrio a ${r.pago}x ${percent(r.equilibrio)}` +
    (r.regla && r.regla.n ? ` · IC95% regla ${percent(r.regla.ic_bajo)}–${percent(r.regla.ic_alto)} · p(>equilibrio) ${fmtP(r.regla.p_equilibrio)}` : "");
  document.querySelector("#papel-veredicto").textContent = r.veredicto;
  const marca = (pick, ok) => (pick === "" ? "-" : `<span class="${ok === "1" ? "hit" : "miss"}">${number(pick)}</span>`);
  document.querySelector("#papel-body").innerHTML = (papel.ultimas || [])
    .map((f) => `<tr><td>${escapeHtml(f.sorteo)}</td><td>${escapeHtml(f.hora_prevista)}</td><td>${f.apostar === "1" ? "sí" : f.tardia === "1" ? "tardía" : "no"}</td><td>${marca(f.pick_regla, f.acierto_regla)}</td><td>${marca(f.pick_modelo, f.acierto_modelo)}</td><td>${number(f.resultado)}</td></tr>`)
    .join("");
}

function renderResults(results) {
  document.querySelector("#results-body").innerHTML = results
    .map((row) => `<tr><td>${row.Fecha}</td><td>${row.Hora}</td><td>${row.No_Sorteo}</td><td><b>${number(row.Numero)}</b></td></tr>`)
    .join("");
}

function renderAlert(data) {
  const badge = document.querySelector("#alert-state");
  const errorBox = document.querySelector("#alert-error");
  const alert = data.alert;
  errorBox.textContent = data.predictions_error || "";
  errorBox.classList.toggle("hidden", !data.predictions_error);

  if (!alert || !alert.available) {
    badge.textContent = "SIN DATOS";
    badge.className = "alert-badge idle";
    document.querySelector("#alert-number").textContent = "-";
    document.querySelector("#alert-detail").textContent = "Sin ronda abierta todavía.";
    document.querySelector("#alert-failures").innerHTML = "";
    document.querySelector("#alert-stake").textContent = "-";
    document.querySelector("#alert-stake-note").textContent = "";
    document.querySelector("#alert-counters").textContent = "-";
    document.querySelector("#alert-rate").textContent = "-";
    document.querySelector("#alert-baseline").textContent = "-";
    document.querySelector("#alert-history-body").innerHTML = "";
    return;
  }

  const stats = data.alert_stats || {};
  const fired = Boolean(alert.fired);
  const blocked = Boolean(alert.signal_blocked);
  badge.textContent = alert.disabled ? "M4 DESACTIVADA" : fired ? "SEÑAL ACTIVA" : blocked ? "SIN VENTAJA" : "SIN SEÑAL";
  badge.className = `alert-badge ${fired ? "active" : "idle"}`;

  document.querySelector("#alert-number").textContent = isBlank(alert.number) ? "00" : number(alert.number);

  const details = [];
  if (!isBlank(alert.probability)) details.push(`P(M4)=${percent(alert.probability)}`);
  if (!isBlank(alert.spread)) details.push(`Δ=${percent(alert.spread)}`);
  details.push(`confianza ${alert.confidence}`);
  details.push(alert.anchor ? "anclaje activo" : "sin anclaje");
  if (alert.source_draw) details.push(`base ${alert.source_draw}`);
  document.querySelector("#alert-detail").textContent = details.join(" · ");

  const failures = (alert.failures || []).slice();
  if (blocked) {
    if (alert.rule_fired) failures.unshift("La regla M4 se cumplió, pero la señal está bloqueada (registro sólo en papel).");
    (alert.block_reasons || []).forEach((reason) => failures.push(`Bloqueo: ${reason}`));
  }
  document.querySelector("#alert-failures").innerHTML = failures
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");

  const applied = Boolean(alert.stake_applied);
  const notes = alert.stake_notes || [];
  document.querySelector("#alert-stake").textContent = applied
    ? `${percent(alert.stake_fraction)} · ${units(alert.stake_amount)}`
    : `${units(0)} · sin stake`;
  document.querySelector("#alert-stake-note").textContent = applied
    ? `Capital de referencia ${units(alert.bankroll)}${notes.length ? ` · ${notes.join("; ")}` : ""}`
    : notes.length
    ? notes.join("; ")
    : "Sin stake: la evidencia acumulada no respalda la apuesta.";

  document.querySelector("#alert-counters").textContent = `${stats.alerts || 0} de ${stats.rounds || 0} rondas`;
  document.querySelector("#alert-rate").textContent = stats.alerts
    ? `${percent(stats.alert_rate)} (${stats.alert_hits}/${stats.alerts})`
    : "sin alertas";
  document.querySelector("#alert-baseline").textContent = percent(stats.baseline || 0);

  document.querySelector("#alert-history-body").innerHTML = (data.alert_history || [])
    .map((item) => {
      const result = isBlank(item.actual_number) ? "-" : number(item.actual_number);
      const signal = isBlank(item.hit) ? "-" : item.hit ? "Acierto" : "Fallo";
      const stake = item.stake_applied ? percent(item.stake_fraction) : "0";
      const num = isBlank(item.number) ? "00" : number(item.number);
      return `<tr>
        <td>${item.source_draw || "-"}</td>
        <td>${num}</td>
        <td>${result}</td>
        <td class="${item.hit ? "hit" : "miss"}">${signal}</td>
        <td>${isBlank(item.probability) ? "-" : percent(item.probability)}</td>
        <td>${item.confidence || "-"}</td>
        <td>${stake}</td>
      </tr>`;
    })
    .join("");
}

function renderPredictions(data) {
  const errorBox = document.querySelector("#predictions-error");
  errorBox.textContent = data.predictions_error || "";
  errorBox.classList.toggle("hidden", !data.predictions_error);

  document.querySelector("#predictions-body").innerHTML = (data.predictions || [])
    .map((prediction) => {
      const result = prediction.status === "pendiente"
        ? "Pendiente"
        : prediction.status === "omitida_pausa"
        ? "No evaluada · pausa"
        : `${number(prediction.actual_number)} · ${prediction.observed_draw}`;
      const markov = prediction.markov_rank ? `Sí · #${prediction.markov_rank}` : "No";
      const heuristic = prediction.heuristic_rank ? `Sí · #${prediction.heuristic_rank}` : "No";
      const matched = prediction.status === "pendiente"
        ? "Pendiente"
        : prediction.matched === "ninguno"
        ? "Ninguno"
        : prediction.matched === "ambos"
        ? "Ambos"
        : prediction.matched === "markov"
        ? "Markov"
        : "Heurístico";
      const signalsAllowed = Boolean(data.decision && data.decision.signals_allowed);
      const alert = !prediction.alert_fired
        ? "—"
        : isBlank(prediction.alert_hit)
        ? signalsAllowed ? "Activa" : "Regla cumplida (papel)"
        : prediction.alert_hit
        ? "Acierto"
        : "Fallo";
      return `<tr>
        <td>${prediction.source_draw}</td>
        <td>${result}</td>
        <td>${markov}</td>
        <td>${heuristic}</td>
        <td>${matched}</td>
        <td>${alert}</td>
      </tr>`;
    })
    .join("");

  const det = document.querySelector("#predictions-body-detailed");
  if (det) {
      det.innerHTML = (data.predictions || [])
      .map((prediction) => {
        const result = prediction.status === "pendiente"
          ? "Pendiente"
          : prediction.status === "omitida_pausa"
          ? "Pausa"
          : `${number(prediction.actual_number)} · ${prediction.observed_draw}`;
        const mf = prediction.markov_forecast || [];
        const hf = prediction.heuristic_forecast || [];
        const get = (arr, i) => arr[i] || { number: prediction[`m${i+1}`], probability: prediction[`p${i+1}`] };
        const m = [get(mf,0), get(mf,1), get(mf,2), get(mf,3), get(mf,4)];
        const h = [get(hf,0), get(hf,1), get(hf,2), get(hf,3), get(hf,4)];
        if (m[0].number === undefined && prediction.m1) { m[0]={number:prediction.m1,probability:prediction.p1}; m[1]={number:prediction.m2,probability:prediction.p2}; m[2]={number:prediction.m3,probability:prediction.p3}; m[3]={number:prediction.m4,probability:prediction.p4}; m[4]={number:prediction.m5,probability:prediction.p5}; }
        if (h[0].number === undefined && prediction.h1) { h[0]={number:prediction.h1,probability:prediction.ph1}; h[1]={number:prediction.h2,probability:prediction.ph2}; h[2]={number:prediction.h3,probability:prediction.ph3}; h[3]={number:prediction.h4,probability:prediction.ph4}; h[4]={number:prediction.h5,probability:prediction.ph5}; }
        const p = (x) => (x.probability == null ? "-" : percent(x.probability));
        const n = (x) => (x.number == null || x.number === "-" ? "-" : (typeof x.number === 'number' ? number(x.number) : x.number));
        return `<tr>
          <td>${prediction.source_draw}</td>
          <td>${result}</td>
          <td>${n(m[0])}</td><td>${p(m[0])}</td>
          <td>${n(m[1])}</td><td>${p(m[1])}</td>
          <td>${n(m[2])}</td><td>${p(m[2])}</td>
          <td>${n(m[3])}</td><td>${p(m[3])}</td>
          <td>${n(m[4])}</td><td>${p(m[4])}</td>
          <td>${n(h[0])}</td><td>${p(h[0])}</td>
          <td>${n(h[1])}</td><td>${p(h[1])}</td>
          <td>${n(h[2])}</td><td>${p(h[2])}</td>
          <td>${n(h[3])}</td><td>${p(h[3])}</td>
          <td>${n(h[4])}</td><td>${p(h[4])}</td>
        </tr>`;
      })
      .join("");
  }

  const stats = data.prediction_stats || {};
  const since = stats.start_draw ? ` desde No. ${stats.start_draw}` : "";
  document.querySelector("#predictions-summary").textContent = stats.rounds
    ? `${stats.rounds} rondas evaluadas guardadas${since} · Markov top-1 ${percent(stats.markov_top1_rate)} (base ${percent(stats.baseline)}) · Markov top-5 ${percent(stats.markov_rate)} (base ${percent(stats.baseline_top5)}) · Heurístico top-1 ${percent(stats.heuristic_top1_rate)}`
    : "Aún no hay rondas reales guardadas. La próxima ronda se guardará en vivo.";

}

function renderPauses(pauses) {
  const list = pauses || [];
  document.querySelector("#pauses-summary").textContent = list.length
    ? `${list.length} pausa(s) detectada(s); esos sorteos no cuentan como predicciones evaluadas.`
    : "No se han detectado pausas.";
  document.querySelector("#pauses-body").innerHTML = list
    .map((pause) => `<tr><td>${pause.from_draw}</td><td>${pause.to_draw}</td><td>${pause.skipped_count}</td><td>${new Date(pause.detected_at).toLocaleString()}</td></tr>`)
    .join("");
}

async function refresh() {
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (data.refresh_interval_seconds) {
      const configured = Math.max(1, Number(data.refresh_interval_seconds)) * 1000;
      if (configured !== refreshIntervalMs) {
        refreshIntervalMs = configured;
        scheduleRefresh();
      }
    }
    render(data);
  } catch (error) {
    document.querySelector("#sync-dot").classList.add("error");
    document.querySelector("#sync-status").textContent = `Sin conexión: ${error.message}`;
  }
}

function scheduleRefresh() {
  if (refreshTimer !== null) clearTimeout(refreshTimer);
  refreshTimer = setTimeout(runRefresh, refreshIntervalMs);
}

async function runRefresh() {
  await refresh();
  scheduleRefresh();
}

document.querySelector("#logout").addEventListener("click", async () => {
  await fetch("/api/auth", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: "logout"}),
  });
  window.location.href = "/";
});

refresh();
scheduleRefresh();
