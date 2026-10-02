"use strict";

/* ===================== утилиты ===================== */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (sec) => {
  sec = Math.max(0, Math.round(sec || 0));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
};
const fmtDate = (ts) => new Date(ts * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch("/api" + path, init);
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      const d = j.detail;
      msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((e) => e.msg).join("; ") : JSON.stringify(d);
    } catch { /* нет тела */ }
    throw new Error(msg);
  }
  return res.json();
}

/* ===================== состояние ===================== */
const S = {
  presets: null, settings: null, hw: null, engine: null,
  folders: [], totals: { total: 0, unfiled: 0 }, currentFolder: "all",
  tracks: [], trackCache: {}, jobs: [], prevJobStatus: {},
  queue: [], queueIdx: -1, playingId: null,
  genres: new Set(), moods: new Set(), neg: new Set(), vocal: "auto",
};

/* ===================== диалоги ===================== */
/* Модальный диалог. Не полагается на событие dialog.close (в части webview оно не приходит):
   результат берётся из submit формы, кнопки «Отмена» и клавиши Esc. */
function dialog({ title, text = "", input = null, ok = "OK", cancel = "Отмена", danger = false }) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.innerHTML = `<form class="dlg">
      <h3>${esc(title)}</h3>${text ? `<p>${esc(text)}</p>` : ""}
      ${input !== null ? `<input type="text" class="dlg-input" maxlength="80" value="${esc(input)}">` : ""}
      <div class="form-actions">
        <button type="submit" class="btn ${danger ? "danger" : "primary"}">${esc(ok)}</button>
        <button type="button" class="btn dlg-cancel">${esc(cancel)}</button>
      </div></form>`;
    document.body.appendChild(dlg);
    let done = false;
    const finish = (val) => {
      if (done) return;
      done = true;
      try { dlg.close(); } catch { /* уже закрыт */ }
      dlg.remove();
      resolve(val);
    };
    const field = $(".dlg-input", dlg);
    $("form", dlg).addEventListener("submit", (e) => { e.preventDefault(); finish(field ? field.value : true); });
    $(".dlg-cancel", dlg).addEventListener("click", () => finish(null));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); finish(null); });
    dlg.addEventListener("keydown", (e) => { if (e.key === "Escape") finish(null); });
    dlg.addEventListener("close", () => finish(null));
    dlg.showModal();
    if (field) { field.focus(); field.select(); }
  });
}

/* ===================== вкладки ===================== */
function showView(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "library") loadLibrary();
  if (name === "settings") loadSettingsView();
  try { localStorage.setItem("view", name); } catch { /* ok */ }
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest(".tab");
  if (b) showView(b.dataset.view);
});

/* ===================== «Создать» ===================== */
function chipGroup(container, items, set, { onToggle } = {}) {
  container.innerHTML = items.map((i) => `<button type="button" class="chip" data-id="${esc(i.id)}">${esc(i.label)}</button>`).join("");
  container.addEventListener("click", (e) => {
    const c = e.target.closest(".chip");
    if (!c) return;
    const id = c.dataset.id;
    set.has(id) ? set.delete(id) : set.add(id);
    onToggle && onToggle(id);
    syncChips();
  });
}
/* Предупреждения о противоречивых тегах — «модель плохо разрешает конфликты» (документация ACE-Step) */
function renderTagWarnings() {
  const c = S.presets.conflicts, box = $("#tagWarn");
  if (!c || !box) return;
  const gl = Object.fromEntries(S.presets.genres.map((g) => [g.id, g.label]));
  const ml = Object.fromEntries(S.presets.moods.map((m) => [m.id, m.label]));
  const out = [];
  for (const [a, b] of c.genres) if (S.genres.has(a) && S.genres.has(b)) out.push(`Жанры «${gl[a]}» и «${gl[b]}» плохо сочетаются`);
  for (const [a, b] of c.moods) if (S.moods.has(a) && S.moods.has(b)) out.push(`Настроения «${ml[a]}» и «${ml[b]}» противоречат друг другу`);
  if (S.genres.size > c.max_genres) out.push(`Выбрано жанров: ${S.genres.size} — надёжнее не больше ${c.max_genres}`);
  box.hidden = out.length === 0;
  box.innerHTML = out.map((t) => `<div>⚠ ${esc(t)}</div>`).join("");
}

function syncChips() {
  renderTagWarnings();
  $$("#genreChips .chip").forEach((c) => c.classList.toggle("on", S.genres.has(c.dataset.id)));
  $$("#moodChips .chip").forEach((c) => c.classList.toggle("on", S.moods.has(c.dataset.id)));
  $$("#negChips .chip").forEach((c) => c.classList.toggle("on", S.neg.has(c.dataset.id)));
  $$("#vocalSeg button").forEach((b) => b.classList.toggle("on", b.dataset.id === S.vocal));
  $("#f-lyrics").disabled = S.vocal === "none";
  $("#f-autolyrics").disabled = S.vocal === "none";
}

/* Режимы качества: сколько вариантов генерировать и выбирать ли лучший автоматически */
const QUALITY = {
  fast: { batch: 1, rank: false, hint: "Один вариант. Если получится «каша» или статичный дрон — движок сам перегенерирует." },
  good: { batch: 4, rank: true, hint: "4 варианта, лучший выбирается автоматически: оценка качества звука + соответствие описанию + разборчивость текста." },
  max: { batch: 8, rank: true, hint: "8 вариантов с автоматическим выбором лучшего. Дольше и требовательнее к видеопамяти, но надёжнее всего против «каши»." },
};
S.quality = "fast";

function setQuality(q, fromUser = true) {
  S.quality = q;
  $$("#qualitySeg button").forEach((b) => b.classList.toggle("on", b.dataset.q === q));
  $("#qualityHint").textContent = QUALITY[q].hint;
  $("#f-batch").value = QUALITY[q].batch;
  if (fromUser) { try { localStorage.setItem("quality", q); } catch { /* ok */ } saveAdv(); }
}

function initCreate() {
  const p = S.presets;
  $("#qualitySeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setQuality(b.dataset.q); });
  let q = "good";
  try { q = localStorage.getItem("quality") || "good"; } catch { /* ok */ }
  setQuality(QUALITY[q] ? q : "good", false);
  restoreAdv();
  syncQualityFromBatch();
  $("#f-batch").addEventListener("input", syncQualityFromBatch);
  for (const sel of [...Object.values(ADV_FIELDS), ...Object.values(ADV_CHECKS)]) {
    $(sel).addEventListener("change", () => {
      if (sel === "#f-folder") S.prefFolder = $(sel).value;
      saveAdv();
    });
  }
  chipGroup($("#genreChips"), p.genres, S.genres, { onToggle: (id) => { if (S.genres.has(id)) S.neg.delete(id); } });
  chipGroup($("#moodChips"), p.moods, S.moods);
  chipGroup($("#negChips"), p.genres, S.neg, { onToggle: (id) => { if (S.neg.has(id)) S.genres.delete(id); } });
  $("#vocalSeg").innerHTML = p.vocals.map((v) => `<button type="button" data-id="${esc(v.id)}">${esc(v.label)}</button>`).join("");
  $("#vocalSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b) { S.vocal = b.dataset.id; syncChips(); }
  });
  const dur = $("#f-duration"), temp = $("#f-temp");
  dur.max = S.settings.max_duration;
  const updDur = () => ($("#durLabel").textContent = fmtTime(dur.value));
  dur.addEventListener("input", updDur); updDur();
  temp.addEventListener("input", () => ($("#tempLabel").textContent = Number(temp.value).toFixed(2)));
  syncChips();
  $("#genForm").addEventListener("submit", onGenerate);
}

$("#lyricsFixBtn").addEventListener("click", async () => {
  const area = $("#f-lyrics"), warn = $("#lyricsWarn");
  if (!area.value.trim()) { warn.textContent = "Поле пустое"; return; }
  try {
    const r = await api("/lyrics/normalize", { method: "POST", body: { text: area.value } });
    area.value = r.text;
    warn.style.color = r.warnings.length ? "var(--warn)" : "var(--good)";
    warn.textContent = r.warnings.length ? r.warnings.join(" · ") : "Текст готов: теги секций и интервалы в порядке";
  } catch (err) { warn.style.color = "var(--bad)"; warn.textContent = err.message; }
});

/* Расширенные параметры запоминаются при каждом изменении и подставляются в новые композиции */
const ADV_FIELDS = {
  batch: "#f-batch", seed: "#f-seed", steps: "#f-steps", guidance: "#f-guidance",
  bpm: "#f-bpm", format: "#f-format", folder: "#f-folder",
};
const ADV_CHECKS = { keep_all: "#f-keepall", thinking: "#f-thinking" };

function loadAdv() {
  try { return JSON.parse(localStorage.getItem("advanced") || "{}") || {}; } catch { return {}; }
}
function saveAdv() {
  const v = {};
  for (const [k, sel] of Object.entries(ADV_FIELDS)) v[k] = $(sel).value;
  for (const [k, sel] of Object.entries(ADV_CHECKS)) v[k] = $(sel).checked;
  try { localStorage.setItem("advanced", JSON.stringify(v)); } catch { /* ok */ }
}
function restoreAdv() {
  const v = loadAdv();
  for (const [k, sel] of Object.entries(ADV_FIELDS)) {
    if (v[k] === undefined) continue;
    if (k === "folder") S.prefFolder = v[k];   // папки ещё не загружены — применит fillFolderSelect
    else $(sel).value = v[k];
  }
  for (const [k, sel] of Object.entries(ADV_CHECKS)) if (v[k] !== undefined) $(sel).checked = !!v[k];
}
function syncQualityFromBatch() {
  const n = Number($("#f-batch").value) || 1;
  const match = Object.keys(QUALITY).find((k) => QUALITY[k].batch === n);
  S.quality = match || "custom";
  $$("#qualitySeg button").forEach((b) => b.classList.toggle("on", b.dataset.q === match));
}

function setDefaultFolder(id) {
  S.prefFolder = id == null ? "" : String(id);
  fillFolderSelect();
  saveAdv();
}

function fillFolderSelect() {
  const sel = $("#f-folder");
  const cur = S.prefFolder !== undefined ? S.prefFolder : sel.value;
  sel.innerHTML = `<option value="">Без папки</option>` + S.folders.map((f) => `<option value="${f.id}">${esc(f.name)}</option>`).join("");
  sel.value = S.folders.some((f) => String(f.id) === cur) ? cur : "";   // удалённая папка → «Без папки»
}

const numOrNull = (id) => {
  const v = $(id).value.trim();
  return v === "" ? null : Number(v);
};

async function onGenerate(e) {
  e.preventDefault();
  const msg = $("#formMsg");
  msg.className = "form-msg"; msg.textContent = "";
  const body = {
    title: $("#f-title").value.trim(),
    prompt: $("#f-prompt").value.trim(),
    genres: [...S.genres], moods: [...S.moods], negative_genres: [...S.neg],
    vocal: S.vocal,
    lyrics: S.vocal === "none" ? "" : $("#f-lyrics").value.trim(),
    auto_lyrics: $("#f-autolyrics").checked,
    thinking: $("#f-thinking").checked,
    duration: Number($("#f-duration").value),
    temperature: Number($("#f-temp").value),
    steps: numOrNull("#f-steps"),
    guidance: Number($("#f-guidance").value) || 7,
    seed: numOrNull("#f-seed"),
    batch: Number($("#f-batch").value) || 1,
    rank: (Number($("#f-batch").value) || 1) > 1,
    keep_all: $("#f-keepall").checked,
    bpm: numOrNull("#f-bpm"),
    format: $("#f-format").value || null,
    folder_id: $("#f-folder").value ? Number($("#f-folder").value) : null,
  };
  saveAdv();   // параметры этой композиции — по умолчанию для следующих
  $("#genBtn").disabled = true;
  try {
    const job = await api("/generate", { method: "POST", body });
    msg.className = "form-msg ok"; msg.textContent = "Задача добавлена в очередь";
    if (job.warnings && job.warnings.length) {
      msg.className = "form-msg"; msg.style.color = "var(--warn)";
      msg.textContent = "Принято. Замечания: " + job.warnings.join(" · ");
    } else { msg.style.color = ""; }
    pollNow();
  } catch (err) {
    msg.className = "form-msg err"; msg.textContent = err.message;
  } finally {
    $("#genBtn").disabled = false;
  }
}

function applyRequestToForm(req) {
  $("#f-title").value = req.title || "";
  $("#f-prompt").value = req.prompt || "";
  S.genres = new Set(req.genres || []); S.moods = new Set(req.moods || []); S.neg = new Set(req.negative_genres || []);
  S.vocal = req.vocal || "auto";
  $("#f-lyrics").value = req.lyrics || "";
  $("#f-autolyrics").checked = req.auto_lyrics !== false;
  $("#f-thinking").checked = req.thinking !== false;
  $("#f-duration").value = req.duration || 60; $("#durLabel").textContent = fmtTime($("#f-duration").value);
  $("#f-temp").value = req.temperature ?? 0.85; $("#tempLabel").textContent = Number($("#f-temp").value).toFixed(2);
  $("#f-steps").value = req.steps ?? ""; $("#f-guidance").value = req.guidance ?? 7;
  $("#f-seed").value = req.seed ?? ""; $("#f-batch").value = req.batch || 1; $("#f-bpm").value = req.bpm ?? "";
  $("#f-keepall").checked = !!req.keep_all;
  { const n = req.batch || 1; const m = Object.keys(QUALITY).find((k) => QUALITY[k].batch === n);
    S.quality = m || "custom"; $$("#qualitySeg button").forEach((b) => b.classList.toggle("on", b.dataset.q === m)); }
  $("#f-format").value = req.format || "";
  syncChips();
  showView("create");
}

/* ===================== очередь ===================== */
const STATUS_RU = { queued: "в очереди", running: "выполняется", done: "готово", error: "ошибка", cancelled: "отменено" };

function renderJobs() {
  const box = $("#jobList");
  if (!S.jobs.length) {
    box.innerHTML = `<p class="muted">Пока пусто. Заполните форму и нажмите «Сгенерировать».</p>`;
    return;
  }
  box.innerHTML = S.jobs.map((j) => {
    const pct = Math.round(j.progress * 100);
    const tracks = (j.track_ids || []).map((id) => {
      const t = S.trackCache[id];
      return t ? `<div class="job-track" data-track="${esc(id)}">
        <button class="btn jt-play" data-qact="play" title="Воспроизвести">▶ ${esc(t.title)} · ${fmtTime(t.duration)}</button>
        ${rateButtons(t)}
        <button class="btn icon danger" data-qact="del" title="Удалить насовсем">🗑</button>
      </div>` : "";
    }).join("");
    // готовые задачи убираются дизлайком/удалением треков; кнопка остаётся только у задач без результата
    const stoppable = j.status === "running" && j.kind === "generate";
    const canCancel = stoppable || j.status === "queued" || j.status === "error" || j.status === "cancelled";
    const cancelLabel = stoppable ? "Остановить" : j.status === "queued" ? "Отменить" : "Скрыть";
    return `<div class="job ${esc(j.status)}" data-job="${esc(j.id)}">
      <div class="job-head"><div class="job-title">${esc(j.title)}</div>
        <div class="job-state">${esc(STATUS_RU[j.status] || j.status)}${j.elapsed ? ` · ${fmtTime(j.elapsed)}` : ""}</div></div>
      ${j.status === "running" || j.status === "queued" ? `<div class="bar"><i style="width:${pct}%"></i></div><div class="job-stage">${esc(j.stage)}${j.status === "running" ? ` · ${pct}%` : ""}</div>` : ""}
      ${j.error ? `<div class="job-err">${esc(j.error)}</div>` : ""}
      ${tracks ? `<div class="job-tracks">${tracks}</div>` : ""}
      ${canCancel ? `<div class="job-actions"><button class="btn${stoppable ? " danger" : ""}" data-dismiss="${esc(j.id)}">${cancelLabel}</button></div>` : ""}
    </div>`;
  }).join("");
}

/* ===================== оценки и удаление треков (общие для очереди и библиотеки) ===================== */
function rateButtons(t) {
  const r = t.rating || 0;
  return `<button class="btn icon rate like${r === 1 ? " on" : ""}" data-qact="like" title="${r === 1 ? "Снять лайк" : "Нравится"}">👍</button>` +
    `<button class="btn icon rate dislike${r === -1 ? " on" : ""}" data-qact="dislike" title="${r === -1 ? "Снять дизлайк" : "Не нравится — убрать из очереди и скрыть в библиотеке"}">👎</button>`;
}

async function rateTrack(t, value) {
  const rating = (t.rating || 0) === value ? 0 : value;   // повторное нажатие снимает оценку
  try {
    const upd = await api(`/tracks/${t.id}`, { method: "PATCH", body: { rating } });
    S.trackCache[t.id] = upd;
  } catch (err) { alert(err.message); return; }
  pollNow();
  if ($("#view-library").classList.contains("active")) loadLibrary();
}

async function deleteTrack(t) {
  const ok = await dialog({ title: "Удалить трек?", text: `«${t.title}» будет удалён безвозвратно.`, ok: "Удалить", danger: true });
  if (!ok) return;
  try { await api(`/tracks/${t.id}`, { method: "DELETE" }); } catch (err) { alert(err.message); }
  delete S.trackCache[t.id];
  if (S.playingId === t.id) { audio.pause(); audio.removeAttribute("src"); S.playingId = null; $("#pTitle").textContent = "Ничего не играет"; }
  pollNow();
  if ($("#view-library").classList.contains("active")) loadLibrary();
}

/* общий обработчик кнопок трека: play / like / dislike / del; true — если клик обработан */
function trackAction(act, t) {
  if (act === "like") { rateTrack(t, 1); return true; }
  if (act === "dislike") { rateTrack(t, -1); return true; }
  if (act === "del") { deleteTrack(t); return true; }
  return false;
}

$("#jobList").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-qact]");
  if (btn) {
    const t = S.trackCache[btn.closest("[data-track]").dataset.track];
    if (!t) return;
    if (btn.dataset.qact === "play") playTrack(t, [t]);
    else trackAction(btn.dataset.qact, t);
    return;
  }
  const d = e.target.closest("[data-dismiss]");
  if (d) {
    try { await api(`/jobs/${d.dataset.dismiss}`, { method: "DELETE" }); } catch (err) { alert(err.message); }
    pollNow();
  }
});

/* ===================== polling ===================== */
let pollTimer = null;
function pollNow() { clearTimeout(pollTimer); poll(); }

async function poll() {
  let busy = false;
  try {
    const data = await api("/jobs");
    S.engine = data.engine;
    const finishedNow = data.jobs.some((j) => j.status === "done" && S.prevJobStatus[j.id] && S.prevJobStatus[j.id] !== "done");
    S.jobs = data.jobs;
    S.jobs.forEach((j) => (S.prevJobStatus[j.id] = j.status));
    busy = S.jobs.some((j) => j.status === "running" || j.status === "queued");
    if (finishedNow || S.jobs.some((j) => (j.track_ids || []).some((id) => !S.trackCache[id]))) await refreshTrackCache();
    renderJobs();
    renderEngineBadge();
    if (finishedNow && $("#view-library").classList.contains("active")) loadLibrary();
    if ($("#view-settings").classList.contains("active")) renderEngineInfo();
  } catch { /* сервер недоступен — повторим */ }
  pollTimer = setTimeout(poll, busy ? 1000 : 3000);
}

async function refreshTrackCache() {
  const { tracks } = await api("/tracks?folder=all");
  S.trackCache = Object.fromEntries(tracks.map((t) => [t.id, t]));
}

// "acestep-v15-xl-turbo" → "ACE-Step 1.5 / XL Turbo"
function ditLabel(name) {
  const m = /^acestep-v(\d)(\d+)-?(.*)$/.exec(name || "");
  if (!m) return name;
  const words = { turbo: "Turbo", sft: "SFT", base: "Base", xl: "XL" };
  const variant = m[3].split("-").filter(Boolean).map((w) => words[w] || w.toUpperCase()).join(" ");
  return `ACE-Step ${m[1]}.${m[2]}` + (variant ? ` / ${variant}` : "");
}

function renderEngineBadge() {
  const b = $("#engineBadge"), e = S.engine;
  if (!e) return;
  b.className = "engine-badge " + e.state;
  const labels = { unloaded: "Модели не загружены", loading: "Загрузка моделей…", ready: "Модели готовы", error: "Ошибка моделей" };
  let txt = labels[e.state] || e.state;
  if (e.state === "ready" && e.plan) txt += ` · ${ditLabel(e.plan.dit)} · ${e.plan.device.toUpperCase()}${e.plan.lm ? " · LM " + e.plan.lm.replace("acestep-5Hz-lm-", "") : " · без LM"}`;
  b.textContent = txt;
  b.title = e.error || "";
}

/* ===================== плеер ===================== */
const audio = $("#audio");
let seeking = false;

function playTrack(track, list) {
  S.queue = list && list.length ? list : [track];
  S.queueIdx = S.queue.findIndex((t) => t.id === track.id);
  S.playingId = track.id;
  audio.src = `/api/tracks/${track.id}/audio`;
  $("#pTitle").textContent = track.title;
  audio.play().catch(() => {});
  markPlaying();
}
function markPlaying() {
  $$(".track").forEach((r) => {
    const on = r.dataset.id === S.playingId;
    r.classList.toggle("playing", on);
    const b = $(".playbtn", r);
    if (b) b.textContent = on && !audio.paused ? "⏸" : "▶";
  });
}
function step(delta) {
  if (!S.queue.length) return;
  const i = S.queueIdx + delta;
  if (i >= 0 && i < S.queue.length) playTrack(S.queue[i], S.queue);
}
$("#pPlay").addEventListener("click", () => { if (audio.src) audio.paused ? audio.play() : audio.pause(); });
$("#pPrev").addEventListener("click", () => step(-1));
$("#pNext").addEventListener("click", () => step(1));
audio.addEventListener("play", () => { $("#pPlay").textContent = "⏸"; markPlaying(); });
audio.addEventListener("pause", () => { $("#pPlay").textContent = "▶"; markPlaying(); });
audio.addEventListener("ended", () => step(1));
audio.addEventListener("loadedmetadata", () => ($("#pDur").textContent = fmtTime(audio.duration)));
audio.addEventListener("timeupdate", () => {
  $("#pCur").textContent = fmtTime(audio.currentTime);
  if (!seeking && audio.duration) $("#pSeek").value = Math.round((audio.currentTime / audio.duration) * 1000);
});
$("#pSeek").addEventListener("input", () => { seeking = true; });
$("#pSeek").addEventListener("change", () => {
  if (audio.duration) audio.currentTime = (Number($("#pSeek").value) / 1000) * audio.duration;
  seeking = false;
});
$("#pVol").addEventListener("input", () => (audio.volume = Number($("#pVol").value)));
audio.volume = 0.9;

/* ===================== «Библиотека» ===================== */
async function loadLibrary() {
  const rating = encodeURIComponent($("#libRating").value || "visible");
  const [f, t] = await Promise.all([
    api(`/folders?rating=${rating}`),
    api(`/tracks?folder=${encodeURIComponent(S.currentFolder)}&q=${encodeURIComponent($("#libSearch").value)}&rating=${rating}`),
  ]);
  S.folders = f.folders; S.totals = { total: f.total, unfiled: f.unfiled };
  S.tracks = t.tracks;
  S.tracks.forEach((x) => (S.trackCache[x.id] = x));
  fillFolderSelect();
  renderFolders();
  renderTracks();
}

function renderFolders() {
  const items = [
    { key: "all", name: "Все треки", cnt: S.totals.total },
    { key: "none", name: "Без папки", cnt: S.totals.unfiled },
    ...S.folders.map((f) => ({ key: String(f.id), name: f.name, cnt: f.count, real: true })),
  ];
  $("#folderList").innerHTML = items.map((i) => `
    <li data-key="${esc(i.key)}" class="${S.currentFolder === i.key ? "active" : ""}">
      <span class="name">${esc(i.name)}${i.real && String(i.key) === $("#f-folder").value ? ` <span class="def" title="Новые песни сохраняются сюда (меняется в «Расширенных параметрах»)">★</span>` : ""}</span><span class="cnt">${i.cnt}</span>
      ${i.real ? `<button class="fbtn" data-act="rename" title="Переименовать">✎</button><button class="fbtn" data-act="del" title="Удалить папку">✕</button>` : ""}
    </li>`).join("");
  const cur = items.find((i) => i.key === S.currentFolder);
  $("#libTitle").textContent = cur ? cur.name : "Все треки";
}

$("#folderList").addEventListener("click", async (e) => {
  const li = e.target.closest("li");
  if (!li) return;
  const key = li.dataset.key;
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "rename") {
    const f = S.folders.find((x) => String(x.id) === key);
    const name = await dialog({ title: "Переименовать папку", input: f.name, ok: "Сохранить" });
    if (name) { try { await api(`/folders/${key}`, { method: "PATCH", body: { name } }); } catch (err) { alert(err.message); } loadLibrary(); }
  } else if (act === "del") {
    const f = S.folders.find((x) => String(x.id) === key);
    const ok = await dialog({ title: "Удалить папку?", text: `«${f.name}» будет удалена, треки перейдут в «Без папки».`, ok: "Удалить", danger: true });
    if (ok) {
      try { await api(`/folders/${key}`, { method: "DELETE" }); } catch (err) { alert(err.message); }
      if (S.currentFolder === key) S.currentFolder = "all";
      loadLibrary();
    }
  } else {
    S.currentFolder = key;
    loadLibrary();
  }
});

$("#newFolderBtn").addEventListener("click", async () => {
  const name = await dialog({ title: "Новая папка", input: "", ok: "Создать" });
  if (!name) return;
  try {
    const f = await api("/folders", { method: "POST", body: { name } });
    S.folders.push(f);
    setDefaultFolder(f.id);   // новая папка — по умолчанию для новых песен
  } catch (err) { alert(err.message); }
  loadLibrary();
});

let searchTimer = null;
$("#libSearch").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadLibrary, 250); });
try { $("#libRating").value = localStorage.getItem("libRating") || "visible"; } catch { /* ok */ }
if (!$("#libRating").value) $("#libRating").value = "visible";
$("#libRating").addEventListener("change", () => {
  try { localStorage.setItem("libRating", $("#libRating").value); } catch { /* ok */ }
  loadLibrary();
});

function renderTracks() {
  const box = $("#trackList");
  if (!S.tracks.length) {
    box.innerHTML = `<div class="empty">Здесь пока нет треков.</div>`;
    return;
  }
  const folderOpts = (cur) => `<option value="">Без папки</option>` +
    S.folders.map((f) => `<option value="${f.id}" ${f.id === cur ? "selected" : ""}>${esc(f.name)}</option>`).join("");
  box.innerHTML = S.tracks.map((t) => `
    <div class="track ${t.id === S.playingId ? "playing" : ""}${t.rating === -1 ? " disliked" : ""}" data-id="${esc(t.id)}">
      <button class="playbtn" data-act="play">▶</button>
      <div>
        <div class="t-title">${esc(t.title)}</div>
        <div class="t-meta">${fmtTime(t.duration)} · ${esc((t.fmt || "").toUpperCase())} · ${fmtDate(t.created_at)} · ${esc(t.caption || t.prompt || "")}</div>
      </div>
      <div class="t-actions">
        ${rateButtons(t).replaceAll("data-qact", "data-act")}
        <select data-act="move" title="Переместить в папку">${folderOpts(t.folder_id)}</select>
        <button class="btn icon" data-act="rename" title="Переименовать">✎</button>
        <button class="btn icon" data-act="info" title="Подробности">ⓘ</button>
        <button class="btn icon" data-act="reuse" title="Повторить с этими параметрами">↻</button>
        <a class="btn icon" href="/api/tracks/${esc(t.id)}/download" title="Скачать" download>⬇</a>
        <button class="btn icon danger" data-act="del" title="Удалить">🗑</button>
      </div>
    </div>`).join("");
  markPlaying();
}

$("#trackList").addEventListener("click", async (e) => {
  const row = e.target.closest(".track");
  const btn = e.target.closest("[data-act]");
  if (!row || !btn || btn.tagName === "SELECT") return;
  const t = S.trackCache[row.dataset.id];
  switch (btn.dataset.act) {
    case "play":
      if (S.playingId === t.id && audio.src) audio.paused ? audio.play() : audio.pause();
      else playTrack(t, S.tracks);
      break;
    case "rename": {
      const title = await dialog({ title: "Переименовать трек", input: t.title, ok: "Сохранить" });
      if (title) { try { await api(`/tracks/${t.id}`, { method: "PATCH", body: { title } }); } catch (err) { alert(err.message); } loadLibrary(); }
      break;
    }
    case "info": toggleDetails(row, t); break;
    case "reuse": applyRequestToForm((t.params && t.params.request) || { prompt: t.prompt, lyrics: t.lyrics }); break;
    default: trackAction(btn.dataset.act, t);
  }
});
$("#trackList").addEventListener("change", async (e) => {
  const sel = e.target.closest("select[data-act=move]");
  if (!sel) return;
  const id = sel.closest(".track").dataset.id;
  try {
    await api(`/tracks/${id}`, { method: "PATCH", body: { move: true, folder_id: sel.value ? Number(sel.value) : null } });
  } catch (err) { alert(err.message); }
  loadLibrary();
});

/* Оценка варианта при автоматическом выборе лучшего: «лучший из 8 · итог 8.4 · CE 7.9 · соответствие 0.47 · текст 91%» */
function scoreText(p) {
  const s = p && p.score;
  if (!s) return "";
  const parts = [`итог ${s.total}`, `CE ${s.CE}`, `PQ ${s.PQ}`, `соответствие описанию ${s.clap}`];
  if (s.lyrics_score !== undefined) parts.push(`разборчивость текста ${Math.round(s.lyrics_score * 100)}%`);
  if (s.noise_ratio > 0) parts.push(`шумовых окон ${Math.round(s.noise_ratio * 100)}%`);
  const rank = s.rank ? `вариант №${s.rank} по оценке · ` : "";
  return `<div>Качество: ${rank}${esc(parts.join(" · "))}</div>`;
}

/* «(LM 6.0 · диффузия 0.9 · VAE 1.2)» из таймингов ACE, если они сохранены */
function timingText(p) {
  const c = (p && p.time_costs) || {};
  const parts = [];
  if (c.lm_total_time) parts.push(`LM ${c.lm_total_time}`);
  if (c.dit_diffusion_time_cost) parts.push(`диффузия ${c.dit_diffusion_time_cost}`);
  if (c.dit_vae_decode_time_cost) parts.push(`VAE ${c.dit_vae_decode_time_cost}`);
  return parts.length ? ` (${parts.join(" · ")})` : "";
}

function toggleDetails(row, t) {
  const ex = $(".track-details", row);
  if (ex) { ex.remove(); return; }
  const p = t.params || {};
  const div = document.createElement("div");
  div.className = "track-details";
  div.innerHTML = `
    <div>Запрос: ${esc(t.prompt || "—")}</div>
    <div>Английский caption: ${esc(t.caption || "—")}</div>
    ${p.negative && p.negative !== "NO USER INPUT" ? `<div>Негативные: ${esc(p.negative)}</div>` : ""}
    ${scoreText(p)}
    <div>Seed: ${esc(t.seed ?? "—")} · генерация ${esc(t.gen_seconds ?? "?")} с${timingText(p)}</div>
    ${t.lyrics ? `<pre>${esc(t.lyrics)}</pre>` : ""}`;
  row.appendChild(div);
}

/* ===================== «Настройки» ===================== */
const SETTING_FIELDS = {
  device: "#s-device", dit_model: "#s-dit", lm_model: "#s-lm", lm_backend: "#s-backend",
  offload: "#s-offload", quantization: "#s-quant", audio_format: "#s-format", max_duration: "#s-maxdur",
};
const SETTING_CHECKS = { translate: "#s-translate", fast_lm: "#s-fastlm", preload: "#s-preload" };

async function loadSettingsView() {
  S.settings = await api("/settings");
  for (const [k, sel] of Object.entries(SETTING_FIELDS)) $(sel).value = S.settings[k];
  for (const [k, sel] of Object.entries(SETTING_CHECKS)) $(sel).checked = !!S.settings[k];
  const info = await api("/info");
  S.hw = info.hardware; S.engine = info.engine;
  renderHardware();
  renderEngineInfo();
}

function renderHardware() {
  const h = S.hw, g = (h.gpus || [])[0];
  $("#hwInfo").innerHTML = `
    <dt>Процессор</dt><dd>${esc(h.cpu_name)} (${h.cores} ядер / ${h.threads} потоков)</dd>
    <dt>ОЗУ</dt><dd>${h.ram_gb} ГБ</dd>
    <dt>Видеокарта</dt><dd>${g ? `${esc(g.name)} · ${g.vram_gb} ГБ (свободно ${g.free_gb ?? "?"})` : "CUDA недоступна — работа на CPU"}</dd>
    <dt>PyTorch</dt><dd>${esc(h.torch)}${h.cuda_version ? ` · CUDA ${esc(h.cuda_version)}` : ""}</dd>`;
}

function renderEngineInfo() {
  const e = S.engine;
  if (!e) return;
  const p = e.plan;
  const labels = { unloaded: "не загружены", loading: "загружаются…", ready: "готовы", error: "ошибка" };
  $("#engineInfo").innerHTML = `Состояние: <b>${esc(labels[e.state] || e.state)}</b>` +
    (p ? `<br>Устройство <code>${esc(p.device)}</code> · DiT <code>${esc(p.dit)}</code> · LM <code>${esc(p.lm || "выкл.")}</code>` +
      `<br>Бэкенд LM <code>${esc(p.backend)}</code>${p.lm ? ` · ускоренный декодер <code>${p.fast_lm ? "да" : "нет"}</code>` : ""}` +
      ` · offload <code>${p.offload ? "да" : "нет"}</code> · квантование <code>${esc(p.quant || "нет")}</code>` : "") +
    (e.load_seconds ? `<br>Загрузка ${e.load_seconds} с${e.warmup_seconds ? `, прогрев ${e.warmup_seconds} с` : ""}` : "") +
    (e.error ? `<br><span style="color:var(--bad)">${esc(e.error)}</span>` : "");
}

$("#settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#settingsMsg");
  const patch = {};
  for (const [k, sel] of Object.entries(SETTING_CHECKS)) patch[k] = $(sel).checked;
  for (const [k, sel] of Object.entries(SETTING_FIELDS)) patch[k] = k === "max_duration" ? Number($(sel).value) : $(sel).value;
  try {
    S.settings = await api("/settings", { method: "PUT", body: patch });
    $("#f-duration").max = S.settings.max_duration;
    msg.className = "form-msg ok"; msg.textContent = "Сохранено. Применится при следующей генерации.";
  } catch (err) { msg.className = "form-msg err"; msg.textContent = err.message; }
});

$("#loadBtn").addEventListener("click", async () => {
  try { await api("/engine/load", { method: "POST" }); } catch (err) { alert(err.message); }
  showView("create"); pollNow();
});
$("#unloadBtn").addEventListener("click", async () => {
  try { S.engine = await api("/engine/unload", { method: "POST" }); } catch (err) { alert(err.message); }
  renderEngineInfo(); renderEngineBadge();
});

/* ===================== старт ===================== */
(async function init() {
  [S.presets, S.settings] = await Promise.all([api("/presets"), api("/settings")]);
  initCreate();
  try { const f = await api("/folders"); S.folders = f.folders; fillFolderSelect(); } catch { /* ok */ }
  let view = "create";
  try { view = localStorage.getItem("view") || "create"; } catch { /* ok */ }
  showView(view);
  poll();
})();
