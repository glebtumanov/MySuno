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
  if (name === "cover") loadSourcePanel();
  if (name === "settings") loadSettingsView();
  try { localStorage.setItem("view", name); } catch { /* ok */ }
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest(".tab");
  if (b) showView(b.dataset.view);
});

/* ===================== «Создать» ===================== */
function chipGroup(container, items, set, { onToggle, sync = syncChips } = {}) {
  container.innerHTML = items.map((i) => `<button type="button" class="chip" data-id="${esc(i.id)}">${esc(i.label)}</button>`).join("");
  container.addEventListener("click", (e) => {
    const c = e.target.closest(".chip");
    if (!c) return;
    const id = c.dataset.id;
    set.has(id) ? set.delete(id) : set.add(id);
    onToggle && onToggle(id);
    sync();
  });
}
/* Предупреждения о противоречивых тегах — «модель плохо разрешает конфликты» (документация ACE-Step) */
function renderTagWarnings(genres = S.genres, moods = S.moods, box = $("#tagWarn")) {
  const c = S.presets.conflicts;
  if (!c || !box) return;
  const gl = Object.fromEntries(S.presets.genres.map((g) => [g.id, g.label]));
  const ml = Object.fromEntries(S.presets.moods.map((m) => [m.id, m.label]));
  const out = [];
  for (const [a, b] of c.genres) if (genres.has(a) && genres.has(b)) out.push(`Жанры «${gl[a]}» и «${gl[b]}» плохо сочетаются`);
  for (const [a, b] of c.moods) if (moods.has(a) && moods.has(b)) out.push(`Настроения «${ml[a]}» и «${ml[b]}» противоречат друг другу`);
  if (genres.size > c.max_genres) out.push(`Выбрано жанров: ${genres.size} — надёжнее не больше ${c.max_genres}`);
  box.hidden = out.length === 0;
  box.innerHTML = out.map((t) => `<div>⚠ ${esc(t)}</div>`).join("");
}

/* ===================== сворачиваемые поля ===================== */
/* .fold[data-fold] — заголовок сворачивает тело; состояние запоминается; в свёрнутом виде .fold-sum показывает выбор */
function initFolds() {
  $$(".fold").forEach((f) => {
    let collapsed = f.dataset.default === "collapsed";
    try {
      const v = localStorage.getItem("fold:" + f.dataset.fold);
      if (v !== null) collapsed = v === "1";
    } catch { /* ok */ }
    setFold(f, collapsed, false);
    $(".fold-head", f).addEventListener("click", () => setFold(f, !f.classList.contains("collapsed")));
  });
}
function setFold(f, collapsed, remember = true) {
  f.classList.toggle("collapsed", collapsed);
  $(".fold-body", f).hidden = collapsed;
  $(".fold-head", f).setAttribute("aria-expanded", String(!collapsed));
  if (remember) { try { localStorage.setItem("fold:" + f.dataset.fold, collapsed ? "1" : "0"); } catch { /* ok */ } }
}
/* «Рок, Джаз» / «не выбрано» — подписи выбранных чипов по порядку пресета */
function setChipSummary(el, items, set) {
  const names = items.filter((i) => set.has(i.id)).map((i) => i.label);
  el.textContent = names.length ? names.join(", ") : "не выбрано";
  el.classList.toggle("none", !names.length);
  el.title = el.textContent;
}

function syncChips() {
  setChipSummary($("#genreSum"), S.presets.genres, S.genres);
  setChipSummary($("#moodSum"), S.presets.moods, S.moods);
  setChipSummary($("#negSum"), S.presets.genres, S.neg);
  renderTagWarnings();
  $$("#genreChips .chip").forEach((c) => c.classList.toggle("on", S.genres.has(c.dataset.id)));
  $$("#moodChips .chip").forEach((c) => c.classList.toggle("on", S.moods.has(c.dataset.id)));
  $$("#negChips .chip").forEach((c) => {
    const pos = S.genres.has(c.dataset.id);   // жанр уже выбран как желаемый — исключить его нельзя
    c.disabled = pos;
    c.title = pos ? "Выбран в «Жанрах» — снимите его там, чтобы исключить" : "";
    c.classList.toggle("on", S.neg.has(c.dataset.id));
  });
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
  const opts = `<option value="">Без папки</option>` + S.folders.map((f) => `<option value="${f.id}">${esc(f.name)}</option>`).join("");
  const has = (v) => S.folders.some((f) => String(f.id) === v);
  const sel = $("#f-folder");
  const cur = S.prefFolder !== undefined ? S.prefFolder : sel.value;
  sel.innerHTML = opts;
  sel.value = has(cur) ? cur : "";   // удалённая папка → «Без папки»
  const csel = $("#c-folder");
  const ccur = C.prefFolder !== undefined ? C.prefFolder : csel.value;
  csel.innerHTML = opts;
  csel.value = has(ccur) ? ccur : "";
}

const numOrNull = (id) => {
  const v = $(id).value.trim();
  return v === "" ? null : Number(v);
};

async function onGenerate(e) {
  e.preventDefault();
  const msg = $("#formMsg");
  msg.className = "form-msg"; msg.textContent = "";
  const body = { ...S.extra, ...createFormState() };   // S.extra — параметры загруженного трека, которых нет в форме
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

/* Содержимое формы «Создать» в виде запроса генерации (оно же сохраняется как состояние формы) */
function createFormState() {
  return {
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
}

function applyRequestToForm(req, { show = true } = {}) {
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
  if (req.folder_id !== undefined) {
    S.prefFolder = req.folder_id == null ? "" : String(req.folder_id);
    fillFolderSelect();   // удалённая папка → «Без папки»
  }
  syncChips();
  if (show) showView("create");
}

/* ===================== трек → редактор со всеми параметрами ===================== */
/* Параметры запроса, которых нет в формах (их задают API и скрипты лаборатории): переносятся из трека и уходят
   со следующими генерациями, пока их не сбросят крестиком на плашке. Значения — умолчания сервера, их не переносим. */
const HIDDEN = {
  create: { instrumental: false, lm_cfg_scale: null, cot_caption: true, guard: true, lm_rep_penalty: null,
    lm_top_p: null, adg: null, shift: null, keyscale: "" },
  cover: { negative_genres: [], bpm: null, keyscale: "", guard: true, adg: null, shift: null },
};
const HIDDEN_LABELS = {
  instrumental: "инструментал", lm_cfg_scale: "CFG LM", cot_caption: "LM переписывает описание", guard: "страж от брака",
  lm_rep_penalty: "штраф повторов LM", lm_top_p: "top-p LM", adg: "ADG", shift: "shift", keyscale: "тональность",
  negative_genres: "исключить жанры", bpm: "BPM",
};
S.extra = {};

function hiddenParams(req, kind) {
  const out = {};
  for (const [k, def] of Object.entries(HIDDEN[kind])) {
    const v = req[k];
    if (v === undefined || v === null || v === "" || JSON.stringify(v) === JSON.stringify(def)) continue;
    out[k] = v;
  }
  return out;
}

function hiddenText(extra) {
  const genre = Object.fromEntries(S.presets.genres.map((g) => [g.id, g.label]));
  return Object.entries(extra).map(([k, v]) => `${HIDDEN_LABELS[k] || k} ${
    Array.isArray(v) ? v.map((x) => genre[x] || x).join(", ") : v === true ? "да" : v === false ? "нет" : v}`).join(" · ");
}

const posNum = (v) => (Number(v) > 0 ? Math.round(Number(v)) : null);
const realKey = (k) => (k && !/^(n\/?a|none|unknown)$/i.test(String(k).trim()) ? String(k).trim() : "");

/* Что известно о треке: запрос + фактические seed, текст, BPM и тональность (их могла выбрать LM) */
function trackFacts(t) {
  const p = t.params || {}, g = p.generation || {}, ace = g.ace || {}, lm = g.lm_metadata || {};
  const lyr = (t.lyrics || "").trim();
  const req = p.request || { prompt: t.prompt, lyrics: lyr };
  return {
    p, req,
    exact: !!g.reproducible,   // одиночная генерация: запрос + её seed повторяют трек
    seed: t.seed ?? ace.seed ?? null,
    lyrics: lyr && !lyr.startsWith("[Instrumental]") ? lyr : "",
    bpm: posNum(req.bpm) || posNum(ace.bpm) || posNum(ace.cot_bpm) || posNum(lm.bpm),
    keyscale: realKey(req.keyscale) || realKey(ace.keyscale) || realKey(ace.cot_keyscale) || realKey(lm.keyscale),
  };
}

const reuseTitle = (t) => (isCover(t) ? "Загрузить в кавер-редактор со всеми параметрами этого кавера"
  : "Загрузить в редактор со всеми параметрами этой композиции");

function loadTrack(t) {
  if (isCover(t)) applyTrackToCover(t); else applyTrackToForm(t);
}

/* Повторимый трек — его seed и один вариант (точная копия). Вариант из пакета так не повторить: тогда — запрос
   как был (то же число вариантов) плюс BPM, тональность и текст этого трека */
function applyTrackToForm(t) {
  const f = trackFacts(t), req = f.req;
  const instrumental = req.vocal === "none" || !!req.instrumental;
  applyRequestToForm({
    ...req,
    lyrics: req.lyrics || (instrumental ? "" : f.lyrics),   // текст, который реально спет, в т.ч. написанный LM
    ...(f.exact ? { seed: f.seed, batch: 1 } : {}),
    bpm: f.bpm,
  });
  S.extra = hiddenParams({ ...req, keyscale: f.keyscale }, "create");
  setLoaded("f", t, f, S.extra);
}

async function applyTrackToCover(t) {
  const f = trackFacts(t), req = f.req, method = f.p.method || (f.p.cover || {}).method;
  // для точного повтора — способ, которым сделан именно этот вариант (в «Авто» были оба)
  await applyRequestToCover(f.exact ? { ...req, seed: f.seed, batch: 1, cover_mode: method || req.cover_mode } : req);
  C.extra = hiddenParams(req, "cover");
  setLoaded("c", t, f, C.extra, method);
}

function setLoaded(pre, t, f, extra, method = null) {
  const box = $(`#${pre}-loaded`);
  const facts = [
    f.exact ? `seed ${f.seed}` : "",
    pre === "f" && f.bpm ? `BPM ${f.bpm}` : "",
    method ? `способ «${method === "edit" ? "перекраска" : "по нотам"}»` : "",
  ].filter(Boolean).join(" · ");
  const lines = [`Загружены параметры ${pre === "c" ? "кавера" : "трека"} «${esc(t.title)}»${facts ? `: ${esc(facts)}` : ""}`];
  if (f.exact) lines.push("Один вариант с seed этого трека — получится точно он. Для новых вариантов очистите seed.");
  else lines.push(`Точно повторить этот трек нельзя: ${f.p.generation ? "он один из вариантов пакета" : "он создан до сохранения всех параметров"}. `
    + `Загружен запрос как был${pre === "f" ? " плюс BPM, тональность и текст этого трека" : ""} — получатся новые варианты в том же духе.`);
  if (Object.keys(extra).length) lines.push(`Также уйдут параметры, которых нет в форме: ${esc(hiddenText(extra))}`);
  $(".lf-text", box).innerHTML = lines.map((l) => `<div>${l}</div>`).join("");
  box.hidden = false;
}

for (const pre of ["f", "c"]) {
  $(`#${pre}-loaded [data-lf=clear]`).addEventListener("click", () => {
    (pre === "f" ? S : C).extra = {};
    $(`#${pre}-loaded`).hidden = true;
  });
}

/* ===================== очередь ===================== */
const STATUS_RU = { queued: "в очереди", running: "выполняется", done: "готово", error: "ошибка", cancelled: "отменено" };

function renderJobs() {
  const html = jobsHtml();
  $$(".job-list").forEach((box) => (box.innerHTML = html));
}

function jobsHtml() {
  if (!S.jobs.length) return `<p class="muted">Пока пусто. Заполните форму и нажмите «Сгенерировать».</p>`;
  return S.jobs.map((j) => {
    const pct = Math.round(j.progress * 100);
    const tracks = (j.track_ids || []).map((id) => {
      const t = S.trackCache[id];
      return t ? `<div class="job-track" data-track="${esc(id)}">
        <button class="btn jt-play" data-qact="play" title="Воспроизвести">▶ ${esc(t.title)} · ${fmtTime(t.duration)}</button>
        ${rateButtons(t)}
        <button class="btn icon" data-qact="reuse" title="${reuseTitle(t)}">↻</button>
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

/* общий обработчик кнопок трека: like / dislike / del / reuse; true — если клик обработан */
function trackAction(act, t) {
  if (act === "like") { rateTrack(t, 1); return true; }
  if (act === "dislike") { rateTrack(t, -1); return true; }
  if (act === "del") { deleteTrack(t); return true; }
  if (act === "reuse") { loadTrack(t); return true; }
  return false;
}

$$(".job-list").forEach((list) => list.addEventListener("click", async (e) => {
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
}));

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
  if (e.fatal) txt = "Сбой видеокарты — перезапустите run.bat";
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
audio.addEventListener("play", () => { $("#pPlay").textContent = "⏸"; markPlaying(); coverPrev.pause(); srcPrev.pause(); });
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
        <div class="t-title">${esc(t.title)}${isCover(t) ? `<span class="badge">кавер</span>` : ""}</div>
        <div class="t-meta">${fmtTime(t.duration)} · ${esc((t.fmt || "").toUpperCase())} · ${fmtDate(t.created_at)} · ${esc(t.caption || t.prompt || "")}</div>
      </div>
      <div class="t-actions">
        ${rateButtons(t).replaceAll("data-qact", "data-act")}
        <select data-act="move" title="Переместить в папку">${folderOpts(t.folder_id)}</select>
        <button class="btn icon" data-act="rename" title="Переименовать">✎</button>
        <button class="btn icon" data-act="info" title="Подробности">ⓘ</button>
        <button class="btn icon" data-act="reuse" title="${reuseTitle(t)}">↻</button>
        <button class="btn icon" data-act="cover" title="Сделать кавер на этот трек">🎤</button>
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
    case "cover": coverFromTrack(t); break;
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
  if (s.retention !== undefined) parts.push(`узнаваемость оригинала ${s.retention}`);
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

/* Фактические параметры, с которыми ACE сделал трек (есть у треков, созданных после появления params.generation) */
function genText(p) {
  const a = p.generation && p.generation.ace;
  if (!a) return "";
  const parts = [`шагов ${a.inference_steps}`, `guidance ${a.guidance_scale}`, `shift ${a.shift}`];
  if (a.thinking) parts.push(`CFG LM ${a.lm_cfg_scale}`, `температура LM ${a.lm_temperature}`);
  const bpm = posNum(a.bpm) || posNum(a.cot_bpm), key = realKey(a.keyscale) || realKey(a.cot_keyscale);
  if (bpm) parts.push(`BPM ${bpm}`);
  if (key) parts.push(`тональность ${key}`);
  if (a.flow_edit_morph) parts.push(`окно перекраски ${a.flow_edit_n_min}–${a.flow_edit_n_max}`);
  else if (a.task_type === "cover") parts.push(`cover strength ${a.audio_cover_strength}`, `шум исходника ${a.cover_noise_strength}`);
  const plan = p.plan || {};
  if (plan.dit) parts.push(`${ditLabel(plan.dit)}${plan.lm ? ` + LM ${plan.lm.replace("acestep-5Hz-lm-", "")}` : ""}`);
  return `<div>Параметры: ${esc(parts.join(" · "))}</div>`;
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
    ${isCover(t) ? `<div>Кавер на «${esc(p.request.source_name || "исходник")}» · фрагмент ${fmtTime(p.request.start || 0)}–${fmtTime(p.request.end ?? (p.request.start || 0) + (t.duration || 0))} · близость ${p.request.cover_strength}${p.method ? ` · способ: ${p.method === "edit" ? "перекраска" : "по нотам"}` : ""}${p.request.cover_noise && p.method !== "edit" ? ` · звучание оригинала ${p.request.cover_noise}` : ""}</div>` : ""}
    ${p.cover && p.cover.source_caption ? `<div>LM слышит в исходнике: ${esc(p.cover.source_caption)}</div>` : ""}
    ${scoreText(p)}
    <div>Seed: ${esc(t.seed ?? "—")} · генерация ${esc(t.gen_seconds ?? "?")} с${timingText(p)}</div>
    ${genText(p)}
    ${t.lyrics ? `<pre>${esc(t.lyrics)}</pre>` : ""}
    <details class="all-params"><summary>Все параметры генерации</summary><pre>${esc(JSON.stringify(p, null, 2))}</pre></details>`;
  row.appendChild(div);
}

/* ===================== «Каверы» ===================== */
/* source: { kind: "upload" | "track", id, name, duration, url, lyrics } */
const C = { genres: new Set(), moods: new Set(), vocal: "auto", source: null, peaks: null, start: 0, end: 0, quality: "good",
  extra: {} };   // extra — параметры загруженного кавера, которых нет в форме
const COVER_MIN = 5;
const isCover = (t) => !!(t && t.params && t.params.request && t.params.request.task === "cover");
const coverPrev = new Audio();
coverPrev.preload = "none";
let audioCtx = null;

function syncCoverChips() {
  setChipSummary($("#c-genreSum"), S.presets.genres, C.genres);
  setChipSummary($("#c-moodSum"), S.presets.moods, C.moods);
  renderTagWarnings(C.genres, C.moods, $("#c-tagWarn"));
  $$("#c-genreChips .chip").forEach((c) => c.classList.toggle("on", C.genres.has(c.dataset.id)));
  $$("#c-moodChips .chip").forEach((c) => c.classList.toggle("on", C.moods.has(c.dataset.id)));
  $$("#c-vocalSeg button").forEach((b) => b.classList.toggle("on", b.dataset.id === C.vocal));
  $("#c-lyrics").disabled = C.vocal === "none";
  const noText = !$("#c-lyrics").value.trim();
  $("#c-vocalHint").textContent = C.vocal === "none" ? "Инструментальная версия — текст не используется."
    : noText ? "Текста нет — получится инструментал. Тип голоса применится, когда добавите текст слева."
    : "Голос, которым будет спет новый текст.";
}

/* Способы кавера (замеры: scripts/cover_lab.py) */
const COVER_MODES = {
  auto: "Варианты делаются обоими способами, лучший выбирается автоматически — по качеству звука, новому стилю и узнаваемости оригинала. Подходит для любых исходников.",
  cover: "Модель заново исполняет музыку по «нотам» исходника: мелодия, ритм, структура. Сильная смена стиля; хорошо работает на треках, сгенерированных MySuno, а на живых сложных записях (оркестр) может дать кашу.",
  edit: "FlowEdit: исходник плавно «перекрашивается» из своего описания (его составляет LM) в новый стиль. Чистый звук, сохраняются гармония и тайминг живых записей; стиль меняется мягче.",
};

function setCoverMode(m, fromUser = true) {
  C.mode = COVER_MODES[m] ? m : "auto";
  $$("#c-modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === C.mode));
  $("#c-modeHint").textContent = COVER_MODES[C.mode];
  setCoverQuality(C.quality, false);
  if (fromUser) saveCoverPrefs();
}

function coverQualityHint(q) {
  if (C.mode !== "auto") return QUALITY[q].hint;
  const n = Math.max(2, QUALITY[q].batch);
  return `${n} ${plural(n, "вариант", "варианта", "вариантов")}: по ${n / 2} каждым способом, лучший выбирается автоматически.`;
}

function setCoverQuality(q, fromUser = true) {
  if (!QUALITY[q]) q = "good";
  C.quality = q;
  $$("#c-qualitySeg button").forEach((b) => b.classList.toggle("on", b.dataset.q === q));
  $("#c-qualityHint").textContent = coverQualityHint(q);
  $("#c-batch").value = QUALITY[q].batch;
  if (fromUser) saveCoverPrefs();
}

function setStrength(v, fromUser = true) {
  v = Math.min(1, Math.max(0.05, Number(v) || 0.6));
  $("#c-strength").value = v;
  $("#c-strengthLabel").textContent = v.toFixed(2);
  $$("#c-strengthSeg button").forEach((b) => b.classList.toggle("on", Math.abs(Number(b.dataset.v) - v) < 0.001));
  if (fromUser) saveCoverPrefs();
}

function saveCoverPrefs() {
  const v = { mode: C.mode, quality: C.quality, strength: Number($("#c-strength").value), noise: Number($("#c-noise").value),
    folder: $("#c-folder").value, format: $("#c-format").value, keep_all: $("#c-keepall").checked };
  try { localStorage.setItem("coverPrefs", JSON.stringify(v)); } catch { /* ok */ }
}

function initCover() {
  const p = S.presets;
  chipGroup($("#c-genreChips"), p.genres, C.genres, { sync: syncCoverChips });
  chipGroup($("#c-moodChips"), p.moods, C.moods, { sync: syncCoverChips });
  $("#c-vocalSeg").innerHTML = p.vocals.map((v) => `<button type="button" data-id="${esc(v.id)}">${esc(v.label)}</button>`).join("");
  $("#c-vocalSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { C.vocal = b.dataset.id; syncCoverChips(); } });
  $("#c-lyrics").addEventListener("input", syncCoverChips);
  $("#c-qualitySeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setCoverQuality(b.dataset.q); });
  $("#c-batch").addEventListener("input", () => {
    const n = Number($("#c-batch").value) || 1, m = Object.keys(QUALITY).find((k) => QUALITY[k].batch === n);
    C.quality = m || "custom";
    $$("#c-qualitySeg button").forEach((b) => b.classList.toggle("on", b.dataset.q === m));
  });
  $("#c-strengthSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setStrength(b.dataset.v); });
  $("#c-strength").addEventListener("input", () => setStrength($("#c-strength").value));
  $("#c-noise").addEventListener("input", () => { $("#c-noiseLabel").textContent = Number($("#c-noise").value).toFixed(2); saveCoverPrefs(); });
  for (const id of ["#c-folder", "#c-format", "#c-keepall"]) $(id).addEventListener("change", () => {
    if (id === "#c-folder") C.prefFolder = $(id).value;
    saveCoverPrefs();
  });

  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem("coverPrefs") || "{}") || {}; } catch { /* ok */ }
  C.mode = COVER_MODES[prefs.mode] ? prefs.mode : "auto";
  setCoverQuality(QUALITY[prefs.quality] ? prefs.quality : "good", false);
  setCoverMode(C.mode, false);
  $("#c-modeSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setCoverMode(b.dataset.mode); });
  setStrength(prefs.strength ?? 0.6, false);
  $("#c-noise").value = prefs.noise ?? 0; $("#c-noiseLabel").textContent = Number($("#c-noise").value).toFixed(2);
  if (prefs.folder !== undefined) C.prefFolder = prefs.folder;
  if (prefs.format !== undefined) $("#c-format").value = prefs.format;
  $("#c-keepall").checked = !!prefs.keep_all;
  syncCoverChips();

  initSourcePanel();
  $("#c-selAll").addEventListener("click", () => { if (C.source) { C.start = 0; C.end = C.source.duration; updateSel(); } });
  for (const [id, key] of [["#c-start", "start"], ["#c-end", "end"]]) {
    $(id).addEventListener("change", () => {
      const v = parseTime($(id).value);
      if (v !== null && C.source) {
        if (key === "start") C.start = Math.max(0, Math.min(v, C.end - COVER_MIN));
        else C.end = Math.min(C.source.duration, Math.max(v, C.start + COVER_MIN));
      }
      updateSel();
    });
  }
  $("#c-play").addEventListener("click", () => {
    if (!coverPrev.paused) { coverPrev.pause(); return; }
    const t = coverPrev.currentTime;
    playPreview(t > C.start && t < C.end - 0.3 && coverPrev.src ? t : C.start);
  });
  initWave();
  $("#c-lyricsFix").addEventListener("click", async () => {
    const area = $("#c-lyrics"), warn = $("#c-lyricsWarn");
    if (!area.value.trim()) { warn.style.color = ""; warn.textContent = "Поле пустое — будет инструментал"; return; }
    try {
      const r = await api("/lyrics/normalize", { method: "POST", body: { text: area.value } });
      area.value = r.text;
      warn.style.color = r.warnings.length ? "var(--warn)" : "var(--good)";
      warn.textContent = r.warnings.length ? r.warnings.join(" · ") : "Текст готов";
    } catch (err) { warn.style.color = "var(--bad)"; warn.textContent = err.message; }
    syncCoverChips();
  });
  $("#c-lyricsOrig").addEventListener("click", () => {
    if (C.source && C.source.lyrics) { $("#c-lyrics").value = C.source.lyrics; syncCoverChips(); }
  });
  $("#coverForm").addEventListener("submit", onCover);
}

function coverMsg(text, cls = "") {
  const m = $("#c-formMsg");
  m.className = "form-msg" + (cls ? " " + cls : ""); m.style.color = ""; m.textContent = text;
}

/* «1:23», «1:23.5», «83» → секунды */
function parseTime(s) {
  s = String(s).trim().replace(",", ".");
  const m = /^(?:(\d+):)?(\d+(?:\.\d+)?)$/.exec(s);
  return m ? Number(m[1] || 0) * 60 + Number(m[2]) : null;
}
const fmtFine = (sec) => {
  sec = Math.max(0, sec || 0);
  const m = Math.floor(sec / 60), r = sec - m * 60;
  return `${m}:${r < 10 ? "0" : ""}${r.toFixed(1)}`;
};

async function decodeAudio(arrayBuffer) {
  audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  return await audioCtx.decodeAudioData(arrayBuffer);
}

/* AudioBuffer → WAV 16 бит (для форматов, которые сервер сам не читает: M4A/AAC/WebM) */
function encodeWav(buf) {
  const ch = Math.min(2, buf.numberOfChannels), n = buf.length, sr = buf.sampleRate;
  const out = new DataView(new ArrayBuffer(44 + n * ch * 2));
  const str = (o, s) => [...s].forEach((c, i) => out.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF"); out.setUint32(4, 36 + n * ch * 2, true); str(8, "WAVE"); str(12, "fmt ");
  out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, ch, true); out.setUint32(24, sr, true);
  out.setUint32(28, sr * ch * 2, true); out.setUint16(32, ch * 2, true); out.setUint16(34, 16, true);
  str(36, "data"); out.setUint32(40, n * ch * 2, true);
  const data = [...Array(ch).keys()].map((c) => buf.getChannelData(c));
  let o = 44;
  for (let i = 0; i < n; i++) for (let c = 0; c < ch; c++) {
    const v = Math.max(-1, Math.min(1, data[c][i]));
    out.setInt16(o, v < 0 ? v * 0x8000 : v * 0x7fff, true); o += 2;
  }
  return out.buffer;
}

async function uploadSource(blob, name) {
  const res = await fetch("/api/sources", { method: "POST", body: blob, headers: { "X-Filename": encodeURIComponent(name) } });
  const j = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(typeof j.detail === "string" ? j.detail : res.statusText); e.status = res.status; throw e; }
  return j;
}

/* ---- панель исходников: загрузка, каталог, прослушивание, выбор ---- */
const srcPrev = new Audio();
srcPrev.preload = "none";

/* Громкость прослушивания исходников (список + фрагмент на волне), отдельно от основного плеера */
function initSourceVolume() {
  const vol = $("#c-vol"), mute = $("#c-volMute");
  let saved = 0.8, lastNonZero = 0.8;
  try { const v = parseFloat(localStorage.getItem("srcVolume")); if (v >= 0 && v <= 1) saved = v; } catch { /* ok */ }
  const apply = (v, remember = true) => {
    v = Math.min(1, Math.max(0, v));
    if (v > 0) lastNonZero = v;
    srcPrev.volume = coverPrev.volume = v;
    vol.value = v;
    $("#c-volVal").textContent = `${Math.round(v * 100)}%`;
    mute.textContent = v === 0 ? "🔇" : v < 0.4 ? "🔈" : "🔊";
    mute.title = v === 0 ? "Включить звук" : "Выключить звук";
    if (remember) { try { localStorage.setItem("srcVolume", String(v)); } catch { /* ok */ } }
  };
  vol.addEventListener("input", () => apply(Number(vol.value)));
  vol.addEventListener("wheel", (e) => { e.preventDefault(); apply(Number(vol.value) + (e.deltaY < 0 ? 0.05 : -0.05)); }, { passive: false });
  mute.addEventListener("click", (e) => { e.preventDefault(); apply(Number(vol.value) > 0 ? 0 : lastNonZero || 0.8); });
  apply(saved, false);
}
const SP = { tab: "uploads", uploads: [], tracks: [], uploading: [], playing: null, collapsed: false };
const AUDIO_RE = /\.(mp3|wav|flac|ogg|oga|opus|m4a|aac|mp4|webm|aiff?)$/i;
const plural = (n, one, few, many) => {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many;
};
const fmtSize = (b) => (b >= 2 ** 20 ? `${(b / 2 ** 20).toFixed(1)} МБ` : `${Math.max(1, Math.round(b / 1024))} КБ`);

function uploadItem(s) {
  return { key: `u:${s.id}`, kind: "upload", id: s.id, name: s.name, duration: s.duration, url: `/api/sources/${s.id}/audio`,
    covers: s.covers || 0,
    sub: [s.fmt, fmtTime(s.duration), fmtDate(s.created_at), s.size ? fmtSize(s.size) : "",
      s.covers ? `${s.covers} ${plural(s.covers, "кавер", "кавера", "каверов")}` : ""].filter(Boolean).join(" · ") };
}
function trackItem(t) {
  return { ...trackSource(t), key: `t:${t.id}`, cover: isCover(t),
    sub: [fmtTime(t.duration), fmtDate(t.created_at), t.caption || t.prompt || ""].filter(Boolean).join(" · ") };
}
const spItems = () => (SP.tab === "uploads" ? SP.uploads.map(uploadItem) : SP.tracks.map(trackItem));
const spFind = (key) => [...SP.uploads.map(uploadItem), ...SP.tracks.map(trackItem)].find((i) => i.key === key);
const currentKey = () => (C.source ? `${C.source.kind === "upload" ? "u" : "t"}:${C.source.id}` : "");

function initSourcePanel() {
  try { SP.tab = localStorage.getItem("spTab") === "library" ? "library" : "uploads"; } catch { /* ok */ }
  $("#c-spTabs").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    SP.tab = b.dataset.tab;
    try { localStorage.setItem("spTab", SP.tab); } catch { /* ok */ }
    renderSourcePanel();
  });
  $("#c-spSearch").addEventListener("input", renderSourcePanel);
  try { SP.collapsed = localStorage.getItem("spCollapsed") === "1"; } catch { /* ok */ }
  $("#c-spToggle").addEventListener("click", () => setPanelCollapsed(!SP.collapsed));
  $("#c-srcChange").addEventListener("click", () => { setPanelCollapsed(false); loadSourcePanel(); $("#c-panel").scrollIntoView({ block: "nearest", behavior: "smooth" }); });
  $("#c-srcDelete").addEventListener("click", () => {
    const it = C.source && C.source.kind === "upload" && spFind(`u:${C.source.id}`);
    if (it) spDelete(it);
  });
  $("#c-srcClear").addEventListener("click", () => setCoverSource(null));

  const drop = $("#c-drop"), file = $("#c-file"), panel = $("#c-panel");
  drop.addEventListener("click", () => file.click());
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } });
  file.addEventListener("change", () => { uploadFiles([...file.files]); file.value = ""; });
  // файлы можно бросить на любую часть панели
  panel.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  panel.addEventListener("dragleave", (e) => { if (!panel.contains(e.relatedTarget)) drop.classList.remove("over"); });
  panel.addEventListener("drop", (e) => {
    e.preventDefault(); drop.classList.remove("over");
    setPanelCollapsed(false);
    uploadFiles([...(e.dataTransfer.files || [])]);
  });

  $("#c-spList").addEventListener("click", (e) => {
    const row = e.target.closest(".sp-row[data-key]"), btn = e.target.closest("[data-sp]");
    if (!row || !btn) return;
    const it = spFind(row.dataset.key);
    if (!it) return;
    const act = btn.dataset.sp;
    if (act === "play") spPlay(it);
    else if (act === "seek") {
      const r = btn.getBoundingClientRect();
      if (SP.playing === it.key && srcPrev.duration) srcPrev.currentTime = ((e.clientX - r.left) / r.width) * srcPrev.duration;
    } else if (act === "use") spUse(it);
    else if (act === "rename") spRename(it);
    else if (act === "del") spDelete(it);
  });
  $("#c-spList").addEventListener("dblclick", (e) => {
    const row = e.target.closest(".sp-row[data-key]");
    if (row && !e.target.closest("button")) { const it = spFind(row.dataset.key); if (it) spUse(it); }
  });
  srcPrev.addEventListener("play", () => { audio.pause(); coverPrev.pause(); spSyncPlaying(); });
  srcPrev.addEventListener("pause", spSyncPlaying);
  srcPrev.addEventListener("ended", spSyncPlaying);
  srcPrev.addEventListener("timeupdate", spSyncPlaying);
  syncPanelVisibility();
  initSourceVolume();
}

/* Панель всегда на месте; сворачивается по заголовку (запоминается) и сама — когда исходник выбран */
function setPanelCollapsed(v, remember = true) {
  SP.collapsed = v;
  if (remember) { try { localStorage.setItem("spCollapsed", v ? "1" : "0"); } catch { /* ok */ } }
  syncPanelVisibility();
}

function syncPanelVisibility() {
  $("#c-spBody").hidden = SP.collapsed;
  $("#c-panel").classList.toggle("collapsed", SP.collapsed);
  $("#c-spToggle").setAttribute("aria-expanded", String(!SP.collapsed));
  $("#c-srcChange").hidden = !C.source || !SP.collapsed;
  $("#c-srcDelete").hidden = !(C.source && C.source.kind === "upload");
  const n = SP.uploads.length;
  const files = n ? `${n} ${plural(n, "файл", "файла", "файлов")}` : "нет загруженных файлов";
  const sum = $("#c-spSummary");
  // свёрнуто: главное — какой файл выбран; развёрнуто: сколько всего файлов
  sum.textContent = SP.collapsed ? (C.source ? C.source.name : `не выбран · ${files}`) : files;
  sum.classList.toggle("chosen", SP.collapsed && !!C.source);
}

async function loadSourcePanel() {
  requestAnimationFrame(drawWave);   // вкладка могла быть скрыта — у холста не было ширины
  try {
    const [{ sources }, { tracks }] = await Promise.all([api("/sources"), api("/tracks?folder=all&rating=visible")]);
    SP.uploads = sources; SP.tracks = tracks;
    tracks.forEach((t) => (S.trackCache[t.id] = t));
  } catch { /* сервер недоступен */ }
  renderSourcePanel();
}

function renderSourcePanel() {
  $$("#c-spTabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === SP.tab));
  $("#c-spCount").textContent = SP.uploads.length ? `· ${SP.uploads.length}` : "";
  syncPanelVisibility();
  $("#c-drop").hidden = SP.tab !== "uploads";
  const q = $("#c-spSearch").value.trim().toLowerCase();
  const items = spItems().filter((i) => !q || i.name.toLowerCase().includes(q) || i.sub.toLowerCase().includes(q));
  const cur = currentKey();
  const pending = SP.tab === "uploads" ? SP.uploading.map((n) => `
    <div class="sp-row pending"><span class="sp-spin"></span>
      <div class="sp-meta"><div class="sp-name">${esc(n)}</div><div class="sp-sub">загружаю…</div></div></div>`).join("") : "";
  const rows = items.map((i) => {
    const on = i.key === cur, playing = SP.playing === i.key;
    return `<div class="sp-row${on ? " current" : ""}${playing ? " playing" : ""}" data-key="${esc(i.key)}" title="Двойной щелчок — использовать">
      <button type="button" class="sp-play" data-sp="play" title="Прослушать">${playing && !srcPrev.paused ? "⏸" : "▶"}</button>
      <div class="sp-meta">
        <div class="sp-name">${esc(i.name)}${i.cover ? `<span class="badge">кавер</span>` : ""}</div>
        <div class="sp-sub">${esc(i.sub)}</div>
        <div class="sp-prog" data-sp="seek"><i></i></div>
      </div>
      <div class="sp-actions">
        ${i.kind === "upload" ? `<button type="button" class="btn icon" data-sp="rename" title="Переименовать">✎</button>
          <button type="button" class="btn icon danger" data-sp="del" title="Удалить файл">🗑</button>` : ""}
        <button type="button" class="btn${on ? "" : " primary"} sp-use" data-sp="use"${on ? " disabled" : ""}>${on ? "✓ Выбран" : "Использовать"}</button>
      </div>
    </div>`;
  }).join("");
  const empty = q ? "Ничего не найдено"
    : SP.tab === "uploads" ? "Загруженных файлов пока нет — перетащите сюда песню, на которую хотите сделать кавер"
    : "В библиотеке пока нет треков";
  $("#c-spList").innerHTML = pending + rows || `<div class="sp-empty">${esc(empty)}</div>`;
  spSyncPlaying();
}

/* обновляет только состояние воспроизведения (без перерисовки списка) */
function spSyncPlaying() {
  $$("#c-spList .sp-row[data-key]").forEach((row) => {
    const on = row.dataset.key === SP.playing;
    row.classList.toggle("playing", on);
    $(".sp-play", row).textContent = on && !srcPrev.paused ? "⏸" : "▶";
    if (on) $(".sp-prog i", row).style.width = `${srcPrev.duration ? (srcPrev.currentTime / srcPrev.duration) * 100 : 0}%`;
  });
}

function spPlay(it) {
  if (SP.playing === it.key) { srcPrev.paused ? srcPrev.play().catch(() => {}) : srcPrev.pause(); return; }
  SP.playing = it.key;
  srcPrev.src = it.url;
  srcPrev.play().catch(() => {});
  spSyncPlaying();
}

function spUse(it) {
  srcPrev.pause();
  if (it.kind === "upload") setCoverSource({ kind: "upload", id: it.id, name: it.name, duration: it.duration, url: it.url });
  else setCoverSource(trackSource(S.trackCache[it.id]));
  coverMsg("");
}

async function spRename(it) {
  const name = await dialog({ title: "Переименовать исходник", input: it.name, ok: "Сохранить" });
  if (!name) return;
  try {
    await api(`/sources/${it.id}`, { method: "PATCH", body: { name } });
    if (C.source && C.source.kind === "upload" && C.source.id === it.id) { C.source.name = name.trim(); $("#c-srcName").textContent = C.source.name; }
  } catch (err) { alert(err.message); }
  loadSourcePanel();
}

async function spDelete(it) {
  const text = `Файл «${it.name}» будет удалён с диска.` + (it.covers
    ? ` Сделанные из него каверы (${it.covers}) останутся в библиотеке, но повторить их с этим исходником не получится.` : "");
  if (!(await dialog({ title: "Удалить исходник?", text, ok: "Удалить", danger: true }))) return;
  try { await api(`/sources/${it.id}`, { method: "DELETE" }); } catch (err) { alert(err.message); }
  if (SP.playing === it.key) { srcPrev.pause(); srcPrev.removeAttribute("src"); SP.playing = null; }
  if (C.source && C.source.kind === "upload" && C.source.id === it.id) setCoverSource(null);
  loadSourcePanel();
}

/* загрузка одного файла: сначала как есть; формат, который сервер не читает (M4A/AAC…), браузер перекодирует в WAV */
async function uploadOne(f) {
  try {
    return { meta: await uploadSource(f, f.name), decoded: null };
  } catch (err) {
    if (err.status !== 415) throw err;
    let decoded;
    try { decoded = await decodeAudio(await f.arrayBuffer()); } catch { throw new Error(`«${f.name}»: формат не поддерживается`); }
    const meta = await uploadSource(new Blob([encodeWav(decoded)], { type: "audio/wav" }), f.name.replace(/\.[^.]+$/, "") + ".wav");
    return { meta, decoded };
  }
}

async function uploadFiles(files) {
  const audioFiles = files.filter((f) => f.type.startsWith("audio/") || AUDIO_RE.test(f.name));
  if (!audioFiles.length) { if (files.length) coverMsg("Это не аудиофайлы", "err"); return; }
  SP.tab = "uploads";
  SP.uploading.push(...audioFiles.map((f) => f.name));
  renderSourcePanel();
  const done = [], errors = [];
  for (const f of audioFiles) {
    try { done.push(await uploadOne(f)); } catch (err) { errors.push(err.message); }
    SP.uploading.splice(SP.uploading.indexOf(f.name), 1);
    renderSourcePanel();
  }
  await loadSourcePanel();
  if (errors.length) coverMsg(errors.join(" · "), "err");
  else coverMsg(done.length > 1 ? `Загружено файлов: ${done.length} — выберите нужный в списке` : "");
  if (done.length === 1 && !errors.length) {   // один файл — сразу берём в работу
    const { meta, decoded } = done[0];
    setCoverSource({ kind: "upload", id: meta.id, name: meta.name, duration: meta.duration, url: `/api/sources/${meta.id}/audio` }, decoded);
  }
}

function trackSource(t) {
  const lyr = (t.lyrics || "").trim();
  return { kind: "track", id: t.id, name: t.title, duration: t.duration, url: `/api/tracks/${t.id}/audio`,
    lyrics: lyr && !lyr.startsWith("[Instrumental]") ? lyr : "" };
}

function coverFromTrack(t) {
  showView("cover");
  setCoverSource(trackSource(t));
}

/* sel: {start, end} — восстановить выделение (повтор кавера) */
async function setCoverSource(src, decoded = null, sel = null) {
  coverPrev.pause();
  C.source = src; C.peaks = null;
  $("#c-src").hidden = !src;
  $("#c-lyricsOrig").hidden = !(src && src.lyrics);
  setPanelCollapsed(!!src, false);   // выбрали — сворачиваем, освобождая место под волну; убрали — показываем каталог
  syncPanelVisibility();
  renderSourcePanel();
  if (!src) { coverPrev.removeAttribute("src"); return; }
  $("#c-srcName").textContent = src.name;
  $("#c-srcInfo").textContent = `${src.kind === "upload" ? "загруженный файл" : "трек из библиотеки"} · ${fmtTime(src.duration)}`;
  coverPrev.src = src.url;
  const max = (S.settings && S.settings.max_duration) || 300;
  C.start = sel ? sel.start || 0 : 0;
  C.end = sel && sel.end ? Math.min(sel.end, src.duration) : Math.min(src.duration, max);
  updateSel();
  $("#c-waveMsg").textContent = "Строю волну…";
  try {
    if (!decoded) decoded = await decodeAudio(await (await fetch(src.url)).arrayBuffer());
    if (C.source !== src) return;   // пока декодировали, выбрали другой
    C.peaks = computePeaks(decoded, 1600);
    if (Math.abs(decoded.duration - src.duration) > 0.5 && !src.duration) src.duration = decoded.duration;
    $("#c-waveMsg").textContent = "";
  } catch {
    if (C.source === src) $("#c-waveMsg").textContent = "Волну построить не удалось — фрагмент можно задать числами ниже";
  }
  drawWave();
}

function computePeaks(buf, n) {
  const chans = [...Array(buf.numberOfChannels).keys()].map((c) => buf.getChannelData(c));
  const len = buf.length, step = Math.max(1, Math.floor(len / n)), peaks = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let m = 0;
    const a = i * step, b = Math.min(len, a + step);
    for (const d of chans) for (let j = a; j < b; j += 4) { const v = Math.abs(d[j]); if (v > m) m = v; }
    peaks[i] = m;
  }
  const top = Math.max(...peaks) || 1;
  return peaks.map((v) => v / top);
}

function updateSel() {
  const s = C.source;
  if (!s) return;
  $("#c-start").value = fmtFine(C.start);
  $("#c-end").value = fmtFine(C.end);
  const len = C.end - C.start, max = (S.settings && S.settings.max_duration) || 300, lab = $("#c-selLen");
  lab.className = "sel-len" + (len > 240 || len > max ? " warn" : "");
  lab.textContent = `Фрагмент ${fmtTime(len)}` + (len > max ? ` — длиннее лимита (${fmtTime(max)}), сократите`
    : len > 240 ? " — длинные фрагменты чаще дают «кашу», надёжнее 1–3 мин" : "");
  drawWave();
}

/* ---- волна с выделением ---- */
let waveDrag = null;
const waveX = (t) => (C.source ? (t / C.source.duration) * $("#c-wave").clientWidth : 0);
const waveT = (x) => (C.source ? Math.max(0, Math.min(C.source.duration, (x / $("#c-wave").clientWidth) * C.source.duration)) : 0);

function drawWave() {
  const cv = $("#c-wave"), w = cv.clientWidth, h = 96;
  if (!w || !C.source) return;
  const dpr = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = h * dpr; }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const css = getComputedStyle(document.documentElement);
  const accent = css.getPropertyValue("--accent").trim(), accent2 = css.getPropertyValue("--accent-2").trim(),
    line = css.getPropertyValue("--line").trim(), muted = css.getPropertyValue("--muted").trim();
  const sx = waveX(C.start), ex = waveX(C.end);
  ctx.fillStyle = "rgba(124,92,255,.16)";
  ctx.fillRect(sx, 0, ex - sx, h);
  if (C.peaks) {
    const n = C.peaks.length;
    for (let x = 0; x < w; x += 2) {
      const a = Math.floor((x / w) * n), b = Math.max(a + 1, Math.floor(((x + 2) / w) * n));
      let m = 0;
      for (let i = a; i < b && i < n; i++) m = Math.max(m, C.peaks[i]);
      const bh = Math.max(1, m * (h - 10));
      ctx.fillStyle = x >= sx && x <= ex ? accent2 : muted;
      ctx.globalAlpha = x >= sx && x <= ex ? 1 : 0.45;
      ctx.fillRect(x, (h - bh) / 2, 1.4, bh);
    }
    ctx.globalAlpha = 1;
  } else {
    ctx.fillStyle = line;
    ctx.fillRect(0, h / 2 - 0.5, w, 1);
  }
  ctx.fillStyle = accent;
  for (const x of [sx, ex]) { ctx.fillRect(x - 1, 0, 2, h); ctx.fillRect(x - 4, 0, 8, 6); ctx.fillRect(x - 4, h - 6, 8, 6); }
  if (coverPrev.src && (!coverPrev.paused || coverPrev.currentTime > 0)) {
    ctx.fillStyle = "#fff";
    ctx.fillRect(waveX(coverPrev.currentTime) - 0.5, 0, 1, h);
  }
}

function initWave() {
  const cv = $("#c-wave");
  const near = (x) => (Math.abs(x - waveX(C.start)) < 8 ? "start" : Math.abs(x - waveX(C.end)) < 8 ? "end" : null);
  cv.addEventListener("pointerdown", (e) => {
    if (!C.source) return;
    const x = e.offsetX;
    waveDrag = { mode: near(x) || "new", x0: x, t0: waveT(x), moved: false };
    cv.setPointerCapture(e.pointerId);
  });
  cv.addEventListener("pointermove", (e) => {
    const x = e.offsetX;
    if (!waveDrag) { cv.style.cursor = C.source && near(x) ? "ew-resize" : "crosshair"; return; }
    if (Math.abs(x - waveDrag.x0) > 3) waveDrag.moved = true;
    if (!waveDrag.moved) return;
    const t = waveT(x), d = C.source.duration;
    if (waveDrag.mode === "start") C.start = Math.max(0, Math.min(t, C.end - COVER_MIN));
    else if (waveDrag.mode === "end") C.end = Math.min(d, Math.max(t, C.start + COVER_MIN));
    else { C.start = Math.min(waveDrag.t0, t); C.end = Math.max(waveDrag.t0, t); }
    updateSel();
  });
  const up = (e) => {
    if (!waveDrag) return;
    const d = waveDrag;
    waveDrag = null;
    if (!d.moved) { playPreview(d.t0); return; }   // щелчок — прослушать с этого места
    if (C.end - C.start < COVER_MIN) {
      C.end = Math.min(C.source.duration, C.start + COVER_MIN);
      C.start = Math.min(C.start, C.end - COVER_MIN);
    }
    updateSel();
    if (d.mode !== "end") playPreview(C.start);
  };
  cv.addEventListener("pointerup", up);
  cv.addEventListener("pointercancel", () => (waveDrag = null));
  window.addEventListener("resize", drawWave);
  coverPrev.addEventListener("timeupdate", () => { if (coverPrev.currentTime >= C.end) coverPrev.pause(); });
  coverPrev.addEventListener("play", () => { $("#c-play").textContent = "⏸"; srcPrev.pause(); tickWave(); });
  coverPrev.addEventListener("pause", () => { $("#c-play").textContent = "▶"; drawWave(); });
}

function tickWave() {
  drawWave();
  if (!coverPrev.paused) requestAnimationFrame(tickWave);
}

function playPreview(from) {
  if (!C.source) return;
  audio.pause();   // основной плеер не должен звучать поверх
  if (!coverPrev.src) coverPrev.src = C.source.url;
  const go = () => { coverPrev.currentTime = from; coverPrev.play().catch(() => {}); };
  if (coverPrev.readyState >= 1) go(); else { coverPrev.addEventListener("loadedmetadata", go, { once: true }); coverPrev.load(); }
}

async function onCover(e) {
  e.preventDefault();
  const s = C.source;
  if (!s) { coverMsg("Сначала добавьте исходник: перетащите файл или выберите трек", "err"); return; }
  const body = { ...C.extra, ...coverFormState() };
  saveCoverPrefs();
  $("#c-genBtn").disabled = true;
  try {
    const job = await api("/cover", { method: "POST", body });
    if (job.warnings && job.warnings.length) {
      coverMsg("Принято. Замечания: " + job.warnings.join(" · "));
      $("#c-formMsg").style.color = "var(--warn)";
    } else coverMsg(body.lyrics ? "Кавер с новым текстом добавлен в очередь" : "Инструментальный кавер добавлен в очередь", "ok");
    pollNow();
  } catch (err) {
    coverMsg(err.message, "err");
  } finally {
    $("#c-genBtn").disabled = false;
  }
}

/* Содержимое формы кавера в виде запроса (без исходника — поля источника пустые) */
function coverFormState() {
  const s = C.source, batch = Number($("#c-batch").value) || 1;
  return {
    source_id: s && s.kind === "upload" ? s.id : null,
    source_track_id: s && s.kind === "track" ? s.id : null,
    source_name: s ? s.name : "",
    start: s ? Math.round(C.start * 100) / 100 : 0,
    end: !s || C.end >= s.duration - 0.05 ? null : Math.round(C.end * 100) / 100,
    title: $("#c-title").value.trim(),
    prompt: $("#c-prompt").value.trim(),
    genres: [...C.genres], moods: [...C.moods], vocal: C.vocal,
    lyrics: C.vocal === "none" ? "" : $("#c-lyrics").value.trim(),
    cover_mode: C.mode,
    cover_strength: Number($("#c-strength").value),
    cover_noise: Number($("#c-noise").value),
    batch, rank: batch > 1, keep_all: $("#c-keepall").checked,
    seed: numOrNull("#c-seed"), steps: numOrNull("#c-steps"),
    guidance: Number($("#c-guidance").value) || 7,
    format: $("#c-format").value || null,
    folder_id: $("#c-folder").value ? Number($("#c-folder").value) : null,
  };
}

async function applyRequestToCover(req, { show = true, quiet = false } = {}) {
  if (show) showView("cover");
  $("#c-title").value = req.title || "";
  $("#c-prompt").value = req.prompt || "";
  C.genres.clear(); (req.genres || []).forEach((g) => C.genres.add(g));
  C.moods.clear(); (req.moods || []).forEach((m) => C.moods.add(m));
  C.vocal = req.vocal || "auto";
  $("#c-lyrics").value = req.lyrics || "";
  setStrength(req.cover_strength ?? 0.6, false);
  setCoverMode(req.cover_mode || "cover", false);   // старые каверы (до выбора способа) делались «по нотам»
  $("#c-noise").value = req.cover_noise || 0; $("#c-noiseLabel").textContent = Number($("#c-noise").value).toFixed(2);
  $("#c-batch").value = req.batch || 1; $("#c-batch").dispatchEvent(new Event("input"));
  $("#c-seed").value = req.seed ?? ""; $("#c-steps").value = req.steps ?? ""; $("#c-guidance").value = req.guidance ?? 7;
  $("#c-keepall").checked = !!req.keep_all;
  $("#c-format").value = req.format || "";
  if (req.folder_id !== undefined) {
    C.prefFolder = req.folder_id == null ? "" : String(req.folder_id);
    fillFolderSelect();
  }
  syncCoverChips();
  const sel = { start: req.start || 0, end: req.end };
  if (req.source_id) {
    const { sources } = await api("/sources");
    const s = sources.find((x) => x.id === req.source_id);
    if (s) setCoverSource({ kind: "upload", id: s.id, name: s.name, duration: s.duration, url: `/api/sources/${s.id}/audio` }, null, sel);
    else { setCoverSource(null); if (!quiet) coverMsg("Исходный файл этого кавера удалён — загрузите его заново", "err"); }
  } else if (req.source_track_id) {
    const t = S.trackCache[req.source_track_id];
    if (t) setCoverSource(trackSource(t), null, sel);
    else { setCoverSource(null); if (!quiet) coverMsg("Трек-исходник удалён из библиотеки", "err"); }
  }
}

/* ===================== состояние форм на сервере ===================== */
/* Всё, что введено в «Создать» и «Каверы», сохраняется в data/ui_state.json (через ~0.8 с после изменения и при
   закрытии страницы) и подставляется при открытии — переживает перезапуск сервера и смену браузера.
   Скрытые параметры загруженного трека (S.extra/C.extra) не сохраняются: без плашки они действовали бы незаметно. */
let uiSaveTimer = null, uiRestoring = true, uiLastSaved = "";

function uiStateNow() {
  return JSON.stringify({ create: createFormState(), cover: coverFormState() });
}
function saveUiState(keepalive = false) {
  clearTimeout(uiSaveTimer);
  if (uiRestoring) return;
  const body = uiStateNow();
  if (body === uiLastSaved) return;
  uiLastSaved = body;
  fetch("/api/ui-state", { method: "PUT", body, keepalive, headers: { "Content-Type": "application/json" } })
    .catch(() => { uiLastSaved = ""; });
}
function scheduleUiSave() {
  clearTimeout(uiSaveTimer);
  uiSaveTimer = setTimeout(saveUiState, 800);
}

async function restoreUiState() {
  try {
    const st = await api("/ui-state");
    if (st.create) applyRequestToForm(st.create, { show: false });
    if (st.cover) {
      await loadSourcePanel();   // трек-исходник из библиотеки должен быть в кэше
      await applyRequestToCover(st.cover, { show: false, quiet: true });
    }
  } catch { /* нет состояния — значения по умолчанию */ }
  uiRestoring = false;
  uiLastSaved = uiStateNow();
  for (const sel of ["#view-create", "#view-cover"]) {
    for (const ev of ["input", "change", "click"]) $(sel).addEventListener(ev, scheduleUiSave);
  }
  window.addEventListener("pagehide", () => saveUiState(true));
  document.addEventListener("visibilitychange", () => { if (document.hidden) saveUiState(true); });
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
  initCover();
  initFolds();
  try { const f = await api("/folders"); S.folders = f.folders; fillFolderSelect(); } catch { /* ok */ }
  await restoreUiState();
  let view = "create";
  try { view = localStorage.getItem("view") || "create"; } catch { /* ok */ }
  showView(view);
  poll();
})();
