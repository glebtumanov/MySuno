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
  if (name === "samples") loadSamples();
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

/* ===================== жанры в тексте описания: «+жанр» и «-жанр» ===================== */
/* Жанр выбирается прямо в поле описания: «+» открывает список жанров (сужается по мере набора), «-» — список
   жанров, которые надо исключить. В тексте жанр остаётся токеном «+Rock» / «-Metal»; при отправке токены
   вырезаются из описания и уходят списками genres / negative_genres. */
const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
let genreTokenRe = null, genreByLabel = null;

function genreTokens() {
  if (!genreTokenRe) {
    const labels = S.presets.genres.map((g) => g.label).sort((a, b) => b.length - a.length).map(reEsc);
    genreTokenRe = new RegExp(`(^|[\\s,;(])([+-])(${labels.join("|")})(?=$|[\\s,.;:!?)])`, "giu");
    genreByLabel = Object.fromEntries(S.presets.genres.map((g) => [g.label.toLowerCase(), g]));
  }
  return genreTokenRe;
}

/* текст поля → { genres, neg, prompt }: prompt — описание без токенов (его переводят и отдают модели) */
function parseGenres(text, allowNeg = true) {
  const genres = [], neg = [];
  const cut = (text || "").replace(genreTokens(), (all, pre, sign, label) => {
    const g = genreByLabel[label.toLowerCase()];
    if (sign === "-" && !allowNeg) return all;   // в каверах исключать жанры нельзя — минус остаётся текстом
    const [add, del] = sign === "+" ? [genres, neg] : [neg, genres];
    if (del.includes(g.id)) del.splice(del.indexOf(g.id), 1);
    if (!add.includes(g.id)) add.push(g.id);
    return pre;
  });
  // убираем «дыры» на месте токенов: двойные пробелы, пробел перед знаком, «, .» и висящие запятые по краям
  const prompt = cut.replace(/[ \t]+/g, " ").replace(/ *\n */g, "\n").replace(/ +([,.;:!?])/g, "$1")
    .replace(/[,;]+(?=[,.;:!?])/g, "").replace(/^[\s,;.]+|[\s,;]+$/g, "");
  return { genres, neg, prompt };
}

/* запрос → текст поля: токены жанров перед описанием */
function genresToText(req, allowNeg = true) {
  if (typeof req.prompt_text === "string") return req.prompt_text;   // сохранённое состояние формы — как было набрано
  const label = Object.fromEntries(S.presets.genres.map((g) => [g.id, g.label]));
  const tokens = [...(req.genres || []).filter((g) => label[g]).map((g) => "+" + label[g]),
    ...(allowNeg ? (req.negative_genres || []) : []).filter((g) => label[g]).map((g) => "-" + label[g])];
  return [tokens.join(" "), req.prompt || ""].filter(Boolean).join(" ");
}

/* убрать токены жанра (с любым знаком) из текста */
function removeGenreToken(text, id) {
  return text.replace(genreTokens(), (all, pre, sign, label) => (genreByLabel[label.toLowerCase()].id === id ? pre : all))
    .replace(/[ \t]{2,}/g, " ").replace(/^ +/, "");
}

/* добавить токен жанра в начало текста (токен с другим знаком убирается) */
function addGenreToken(text, sign, id) {
  const g = S.presets.genres.find((x) => x.id === id);
  return `${sign}${g.label} ${removeGenreToken(text, id).replace(/^\s+/, "")}`;
}

/* Координаты каретки в textarea (зеркальный div с теми же стилями) — список жанров открывается под ней */
function caretXY(ta, pos) {
  const cs = getComputedStyle(ta), m = document.createElement("div");
  for (const p of ["boxSizing", "width", "fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing", "wordSpacing",
    "paddingTop", "paddingRight", "paddingBottom", "paddingLeft", "borderTopWidth", "borderRightWidth", "borderBottomWidth",
    "borderLeftWidth", "tabSize"]) m.style[p] = cs[p];
  Object.assign(m.style, { position: "absolute", visibility: "hidden", whiteSpace: "pre-wrap", overflowWrap: "break-word", top: "0", left: "-9999px" });
  m.textContent = ta.value.slice(0, pos);
  const span = document.createElement("span");
  span.textContent = "​";
  m.appendChild(span);
  document.body.appendChild(m);
  const r = ta.getBoundingClientRect();
  const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.45;
  const xy = { x: r.left + span.offsetLeft - ta.scrollLeft, y: r.top + span.offsetTop - ta.scrollTop, lh };
  m.remove();
  return xy;
}

/* Автодополнение жанров в поле описания; onChange(parsed) — после каждого изменения текста */
function genreAutocomplete(ta, { allowNeg, onChange, noteText = "" }) {
  const box = document.createElement("div");
  box.className = "gac";
  box.hidden = true;
  box.setAttribute("role", "listbox");
  document.body.appendChild(box);
  let ctx = null;   // { start, sign, items, idx }

  // ближайший к каретке «+» / «-», стоящий в начале строки или после пробела; между ним и кареткой — запрос
  function findContext() {
    const pos = ta.selectionStart;
    if (pos !== ta.selectionEnd) return null;
    const before = ta.value.slice(0, pos);
    for (let i = pos - 1; i >= 0 && i >= pos - 32; i--) {
      const ch = before[i];
      if (ch === "\n") return null;
      if ((ch === "+" || ch === "-") && (i === 0 || /[\s,;(]/.test(before[i - 1]))) {
        const q = before.slice(i + 1);
        if (/^\s/.test(q)) return null;   // «текст - текст» — обычное тире, не жанр
        // исключать жанры здесь нельзя — подсказываем почему (только сразу после минуса, дальше не мешаем)
        if (ch === "-" && !allowNeg) return q.length <= 2 ? { start: i, sign: ch, q, note: true } : null;
        return { start: i, sign: ch, q: q.toLowerCase() };
      }
    }
    return null;
  }

  function matches(q, sign) {
    const chosen = parseGenres(ta.value, allowNeg);
    const taken = new Set(sign === "+" ? chosen.genres : chosen.neg);
    const scored = [];
    for (const g of byAlphabet(S.presets.genres)) {
      if (taken.has(g.id)) continue;
      const label = g.label.toLowerCase(), alias = (g.alias || "").toLowerCase();
      let score = -1;
      if (!q || label.startsWith(q)) score = 0;
      else if (label.split(/[\s-]+/).some((w) => w.startsWith(q)) || alias.split(/\s+/).some((w) => w.startsWith(q)) || g.id.startsWith(q)) score = 1;
      else if (label.includes(q) || alias.includes(q)) score = 2;
      if (score >= 0) scored.push([score, g]);
    }
    return scored.sort((a, b) => a[0] - b[0]).map((x) => x[1]);
  }

  function render() {
    if (ctx.note) {
      box.innerHTML = `<div class="gac-note">${esc(noteText)}</div>`;
      return place();
    }
    box.innerHTML = `<div class="gac-head">${ctx.sign === "+" ? "Добавить жанр" : "Исключить жанр"}</div>` +
      ctx.items.map((g, i) => `<div class="gac-item${i === ctx.idx ? " on" : ""}" role="option" data-i="${i}">` +
        `<span class="gac-sign ${ctx.sign === "+" ? "plus" : "minus"}">${ctx.sign === "+" ? "+" : "−"}</span>${esc(g.label)}` +
        `${g.alias ? `<em>${esc(g.alias)}</em>` : ""}</div>`).join("");
    place();
  }

  function place() {
    const xy = caretXY(ta, ctx.start);
    box.hidden = false;
    const w = box.offsetWidth, h = box.offsetHeight;
    const left = Math.max(8, Math.min(xy.x - 4, innerWidth - w - 8));
    let top = xy.y + xy.lh + 4;
    if (top + h > innerHeight - 90) top = Math.max(8, xy.y - h - 4);   // снизу плеер — тогда открываем вверх
    box.style.left = left + "px";
    box.style.top = top + "px";
    const on = $(".gac-item.on", box);
    if (on) on.scrollIntoView({ block: "nearest" });
  }

  function update() {
    const c = findContext();
    if (c && c.note) { ctx = { ...c, items: [], idx: 0 }; render(); return; }
    const items = c ? matches(c.q, c.sign) : [];
    if (!c || !items.length) { close(); return; }
    const keep = ctx && ctx.start === c.start ? ctx.items[ctx.idx] : null;
    ctx = { ...c, items, idx: Math.max(0, keep ? items.indexOf(keep) : 0) };
    render();
  }

  function close() { ctx = null; box.hidden = true; }

  function choose(g) {
    const pos = ta.selectionStart, v = ta.value;
    let rest = v.slice(pos);
    // жанр не бывает одновременно желаемым и исключённым: токен с другим знаком убираем
    let head = v.slice(0, ctx.start);
    const other = ctx.sign === "+" ? parseGenres(v, allowNeg).neg : parseGenres(v, allowNeg).genres;
    if (other.includes(g.id)) { head = removeGenreToken(head, g.id); rest = removeGenreToken(rest, g.id); }
    const token = ctx.sign + g.label + (/^\s/.test(rest) ? "" : " ");
    ta.value = head + token + rest;
    const caret = head.length + token.length + (/^\s/.test(rest) ? 1 : 0);
    ta.setSelectionRange(caret, caret);
    close();
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    ta.focus();
  }

  ta.addEventListener("input", () => { onChange(parseGenres(ta.value, allowNeg)); update(); });
  ta.addEventListener("click", update);
  ta.addEventListener("keyup", (e) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) update(); });
  ta.addEventListener("keydown", (e) => {
    if (!ctx) return;
    if (ctx.note) { if (e.key === "Escape") { e.preventDefault(); close(); } return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      ctx.idx = (ctx.idx + (e.key === "ArrowDown" ? 1 : -1) + ctx.items.length) % ctx.items.length;
      render();
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      choose(ctx.items[ctx.idx]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  });
  ta.addEventListener("blur", () => setTimeout(close, 150));
  ta.addEventListener("scroll", () => { if (ctx) render(); });
  window.addEventListener("resize", () => { if (ctx) render(); });
  document.addEventListener("scroll", () => { if (ctx) render(); }, true);
  box.addEventListener("mousedown", (e) => {
    e.preventDefault();   // фокус остаётся в поле
    const it = e.target.closest(".gac-item");
    if (it && ctx) choose(ctx.items[Number(it.dataset.i)]);
  });
  box.addEventListener("mousemove", (e) => {
    const it = e.target.closest(".gac-item");
    if (it && ctx && Number(it.dataset.i) !== ctx.idx) { ctx.idx = Number(it.dataset.i); $$(".gac-item", box).forEach((x, i) => x.classList.toggle("on", i === ctx.idx)); }
  });
}

/* Строка под полем: выбранные жанры (и исключённые) с крестиком — убрать токен из текста */
function renderGenreLine(box, ta, parsed, allowNeg) {
  const label = Object.fromEntries(S.presets.genres.map((g) => [g.id, g.label]));
  const chip = (id, cls) => `<span class="gchip ${cls}">${cls === "neg" ? "−" : "+"}${esc(label[id])}` +
    `<button type="button" data-gdel="${esc(id)}" title="Убрать">✕</button></span>`;
  const parts = parsed.genres.map((id) => chip(id, "pos")).concat(allowNeg ? parsed.neg.map((id) => chip(id, "neg")) : []);
  box.innerHTML = parts.length ? parts.join("") : `<span class="hint">Наберите <b>+</b>, чтобы добавить жанр${allowNeg ? ", <b>-</b> — чтобы исключить" : ""}</span>`;
  box.onclick = (e) => {
    const b = e.target.closest("[data-gdel]");
    if (!b) return;
    ta.value = removeGenreToken(ta.value, b.dataset.gdel);
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  };
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

const setTo = (set, ids) => { set.clear(); ids.forEach((id) => set.add(id)); };

/* жанры «Создать» берутся из токенов в поле описания */
function syncCreateGenres() {
  const p = parseGenres($("#f-prompt").value, true);
  setTo(S.genres, p.genres);
  setTo(S.neg, p.neg);
}

function syncChips() {
  setChipSummary($("#moodSum"), S.presets.moods, S.moods);
  renderTagWarnings();
  renderGenreLine($("#f-genreLine"), $("#f-prompt"), { genres: [...S.genres], neg: [...S.neg] }, true);
  $("#f-negHint").hidden = !S.neg.size;
  $$("#moodChips .chip").forEach((c) => c.classList.toggle("on", S.moods.has(c.dataset.id)));
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
  genreAutocomplete($("#f-prompt"), { allowNeg: true, onChange: () => { syncCreateGenres(); syncChips(); } });
  chipGroup($("#moodChips"), p.moods, S.moods);
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
    ...(({ prompt, genres, neg }) => ({ prompt, genres, negative_genres: neg }))(parseGenres($("#f-prompt").value, true)),
    prompt_text: $("#f-prompt").value,   // как набрано, с токенами жанров — для восстановления формы
    moods: [...S.moods],
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
  $("#f-prompt").value = genresToText(req, true);
  syncCreateGenres();
  // множества меняем на месте: обработчики чипов (chipGroup) держат ссылки именно на эти объекты
  setTo(S.moods, req.moods || []);
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
    const sk = j.sample && smpKey(j.sample.kind, j.sample.id);
    const sm = j.status === "done" && sk && LIB.samples[sk];
    const sampleRow = sm ? `<div class="job-track" data-sample="${esc(sk)}">
        <button class="btn jt-play" data-qact="splay" title="Воспроизвести сэмпл">▶ ${esc(j.title)} · ${fmtTime(sm.duration)}</button>
        <button class="btn icon" data-qact="sgo" title="Открыть в библиотеке">↗</button>
      </div>` : "";
    const tracks = sampleRow + (j.track_ids || []).map((id) => {
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
    const canCancel = stoppable || j.status === "queued" || j.status === "error" || j.status === "cancelled" || !!sm;
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

/* ===================== оценки и удаление треков (общие для очереди и архива) ===================== */
function rateButtons(t) {
  const r = t.rating || 0;
  return `<button class="btn icon rate like${r === 1 ? " on" : ""}" data-qact="like" title="${r === 1 ? "Снять лайк" : "Нравится"}">👍</button>` +
    `<button class="btn icon rate dislike${r === -1 ? " on" : ""}" data-qact="dislike" title="${r === -1 ? "Снять дизлайк" : "Не нравится — убрать из очереди и скрыть в архиве"}">👎</button>`;
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
  const srow = btn && btn.closest("[data-sample]");
  if (srow) {
    const k = srow.dataset.sample, sep = k.indexOf("-"), kind = k.slice(0, sep), id = k.slice(sep + 1);
    const item = smpSection(kind).items().find((i) => i.id === id);
    if (btn.dataset.qact === "splay" && item && LIB.samples[k]) {
      const t = smpTrack(kind, item), list = smpPlayList();
      playTrack(t, list.some((x) => x.id === t.id) ? list : [t]);
    }
    if (btn.dataset.qact === "sgo") showView("samples");
    return;
  }
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
    if (S.jobs.some((j) => j.sample) || Object.keys(LIB.pending).length) syncSamplesFromJobs();
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
  audio.src = track.url || `/api/tracks/${track.id}/audio`;
  $("#pTitle").textContent = track.title;
  audio.play().catch(() => {});
  markPlaying();
}
function markPlaying() {
  $$(".track, .smp.have").forEach((r) => {
    const on = (r.dataset.pid || r.dataset.id) === S.playingId;
    r.classList.toggle("playing", on);
    const b = $(".playbtn, .smp-play", r);
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
audio.addEventListener("ended", () => {
  // сэмплы библиотеки: дальше по списку — только с галочкой «Переходить на следующий сэмпл»
  if (S.playingId && S.playingId.startsWith("smp:") && !$("#smpAuto").checked) return;
  step(1);
});
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

/* ===================== «Архив» ===================== */
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

/* ===================== «Библиотека» звучаний ===================== */
/* Минутные сэмплы жанров и настроений. Генерируются по одному по запросу и хранятся отдельно
   от архива (data/samples). Ключ сэмпла — «вид-id»: genre-rock, mood-sad. */
const LIB = { samples: {}, pending: {}, seconds: 60, sig: "", loaded: false, fresh: {}, freshTimer: null };
const SMP_SECTIONS = [
  { kind: "genre", title: "Жанры", items: () => byAlphabet(S.presets.genres), set: () => S.genres },
  { kind: "mood", title: "Настроение", items: () => S.presets.moods, set: () => S.moods },
];
/* по алфавиту: сначала латинские названия, за ними русские */
function byAlphabet(items) {
  const cyr = (s) => /^[а-яё]/i.test(s);
  return [...items].sort((a, b) => cyr(a.label) - cyr(b.label) || a.label.localeCompare(b.label, "ru", { sensitivity: "base" }));
}
const smpKey = (kind, id) => `${kind}-${id}`;
const smpSection = (kind) => SMP_SECTIONS.find((s) => s.kind === kind);

function smpTrack(kind, item) {
  const k = smpKey(kind, item.id), m = LIB.samples[k];
  return {
    id: "smp:" + k, duration: m.duration,
    url: `/api/samples/${kind}/${encodeURIComponent(item.id)}/audio?f=${encodeURIComponent(m.filename)}`,
    title: `Сэмпл · ${item.label}`,
  };
}

const FRESH_MS = 8000;
async function loadSamples() {
  let r;
  try { r = await api("/samples"); } catch { return; }
  if (LIB.loaded) {   // новый или пересозданный сэмпл (не при первой загрузке страницы) — подсветить
    for (const [k, m] of Object.entries(r.samples)) {
      if (!LIB.samples[k] || LIB.samples[k].created_at !== m.created_at) LIB.fresh[k] = Date.now() + FRESH_MS;
    }
    if (Object.keys(LIB.fresh).length) { clearTimeout(LIB.freshTimer); LIB.freshTimer = setTimeout(expireFresh, FRESH_MS + 50); }
  }
  LIB.samples = r.samples; LIB.pending = r.pending; LIB.seconds = r.seconds;
  LIB.loaded = true;
  LIB.sig = Object.keys(LIB.pending).sort().join();
  renderSamples();
  renderJobs();   // у готовых сэмплов в очереди появляется кнопка прослушивания
}

function expireFresh() {
  const now = Date.now();
  for (const [k, until] of Object.entries(LIB.fresh)) {
    if (until > now) continue;
    delete LIB.fresh[k];
    const el = $(`.smp[data-pid="smp:${CSS.escape(k)}"]`);
    if (el) el.classList.remove("fresh");
  }
  if (Object.keys(LIB.fresh).length) LIB.freshTimer = setTimeout(expireFresh, 500);
}

/* Сэмплы в работе известны и из опроса очереди: новый или завершённый сэмпл → перечитываем библиотеку */
function syncSamplesFromJobs() {
  const pending = {};
  for (const j of S.jobs) if (j.sample && (j.status === "queued" || j.status === "running")) pending[smpKey(j.sample.kind, j.sample.id)] = j.id;
  const sig = Object.keys(pending).sort().join();
  if (sig !== LIB.sig || !LIB.loaded) { LIB.pending = pending; LIB.sig = sig; LIB.loaded = true; loadSamples(); }
  else if ($("#view-samples").classList.contains("active")) updateSampleProgress();
}

/* последняя попытка этого сэмпла упала — покажем ошибку на карточке */
function smpError(k) {
  const j = S.jobs.find((x) => x.sample && smpKey(x.sample.kind, x.sample.id) === k);
  return j && j.status === "error" ? j.error : "";
}

function smpJobText(j) {
  if (!j || j.status !== "running") return "в очереди";
  return `${esc(j.stage)} · ${Math.round(j.progress * 100)}%`;
}

function smpTile(sec, item) {
  const k = smpKey(sec.kind, item.id), m = LIB.samples[k], jobId = LIB.pending[k];
  const inForm = sec.set().has(item.id);
  const useBtn = `<button class="btn icon smp-use${inForm ? " on" : ""}" data-sact="use" title="${inForm ? "Убрать из формы «Создать»"
    : "Добавить в форму «Создать»"}">${inForm ? "✓" : "⊕"}</button>`;
  let state, left, sub, actions;
  if (jobId) {
    state = "pending";
    left = `<div class="sp-spin"></div>`;
    sub = smpJobText(S.jobs.find((x) => x.id === jobId));
    actions = useBtn;
  } else if (m) {
    state = "have";
    left = `<button class="smp-play" data-sact="play" title="Слушать">▶</button>`;
    const bpm = posNum(m.bpm), key = realKey(m.keyscale);
    // язык вокала (у сэмплов, созданных до выбора языка, — русский) или «без вокала»
    sub = esc([fmtTime(m.duration), m.instrumental ? "без вокала" : (m.language || "ru").toUpperCase(), bpm ? `${bpm} BPM` : "", key,
      m.dit ? ditLabel(m.dit).replace("ACE-Step 1.5 / ", "") : ""]
      .filter(Boolean).join(" · "));
    actions = `${useBtn}<button class="btn icon" data-sact="regen" title="Сгенерировать заново (заменит этот сэмпл)">↻</button>`
      + `<button class="btn icon danger" data-sact="del" title="Удалить сэмпл">🗑</button>`;
  } else {
    const err = smpError(k);
    state = "missing";
    left = `<div class="smp-dot">♪</div>`;
    sub = err ? `<span class="smp-err" title="${esc(err)}">ошибка: ${esc(err)}</span>` : "сэмпла ещё нет";
    actions = `${useBtn}<button class="btn smp-make" data-sact="make" title="Сгенерировать сэмпл на ${fmtTime(LIB.seconds)}">Создать</button>`;
  }
  const tip = m ? [m.caption && `Описание: ${m.caption}`, m.negative && m.negative !== "NO USER INPUT" && `Негативные: ${m.negative}`,
    m.seed != null && `Seed: ${m.seed}`, m.gen_seconds && `Генерация ${m.gen_seconds} с`, m.created_at && `Создан ${fmtDate(m.created_at)}`]
    .filter(Boolean).join("\n") : "";
  if (LIB.fresh[k] && m) state += " fresh";
  return `<div class="smp ${state}" data-kind="${sec.kind}" data-id="${esc(item.id)}" data-pid="smp:${esc(k)}"${tip ? ` title="${esc(tip)}"` : ""}>
    ${left}
    <div class="smp-meta"><div class="smp-name">${esc(item.label)}</div><div class="smp-sub">${sub}</div></div>
    <div class="smp-actions">${actions}</div>
  </div>`;
}

/* пресеты раздела, видимые при текущих поиске и фильтре */
function smpShown(sec) {
  const q = $("#smpSearch").value.trim().toLowerCase(), filter = $("#smpFilter").value;
  return sec.items().filter((i) => {
    const k = smpKey(sec.kind, i.id);
    if (q && ![i.label.toLowerCase(), i.id, i.alias || ""].some((v) => v.includes(q))) return false;
    if (filter === "have") return !!LIB.samples[k];
    if (filter === "missing") return !LIB.samples[k];
    return true;
  });
}

/* очередь плеера — готовые сэмплы в порядке показа (с учётом поиска и фильтра); сквозная по разделам */
function smpPlayList() {
  return SMP_SECTIONS.flatMap((sec) => smpShown(sec).filter((i) => LIB.samples[smpKey(sec.kind, i.id)])
    .map((i) => smpTrack(sec.kind, i)));
}

function renderSamples() {
  if (!S.presets) return;
  let have = 0, total = 0;
  const html = SMP_SECTIONS.map((sec) => {
    const all = sec.items();
    const ready = all.filter((i) => LIB.samples[smpKey(sec.kind, i.id)]).length;
    have += ready; total += all.length;
    const shown = smpShown(sec);
    if (!shown.length) return "";
    return `<section class="smp-sec ${sec.kind}">
      <h3>${esc(sec.title)} <span class="smp-sec-count">${ready} из ${all.length}</span></h3>
      ${sec.hint ? `<p class="hint">${esc(sec.hint)}</p>` : ""}
      <div class="smp-grid">${shown.map((i) => smpTile(sec, i)).join("")}</div>
    </section>`;
  }).join("");
  $("#smpSections").innerHTML = html || `<div class="empty">Ничего не найдено.</div>`;
  $("#smpCount").textContent = `готово ${have} из ${total}`;
  markPlaying();
}

function updateSampleProgress() {
  for (const [k, jobId] of Object.entries(LIB.pending)) {
    const el = $(`.smp[data-pid="smp:${CSS.escape(k)}"] .smp-sub`);
    if (el) el.innerHTML = smpJobText(S.jobs.find((x) => x.id === jobId));
  }
}

async function makeSample(kind, id, replace = false) {
  try {
    const job = await api("/samples", { method: "POST", body: { kind, id, replace } });
    LIB.pending[smpKey(kind, id)] = job.id;
  } catch (err) { alert(err.message); return; }
  renderSamples();
  pollNow();
}

/* ⊕ — добавить пресет в форму «Создать» (или убрать); жанр — токеном «+жанр» в поле описания */
function toggleInForm(kind, id) {
  if (kind === "genre") {
    const ta = $("#f-prompt");
    ta.value = S.genres.has(id) ? removeGenreToken(ta.value, id) : addGenreToken(ta.value, "+", id);
    syncCreateGenres();
  } else {
    const set = smpSection(kind).set();
    set.has(id) ? set.delete(id) : set.add(id);
  }
  syncChips();
  scheduleUiSave();
  renderSamples();
}

$("#smpSections").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-sact]"), tile = e.target.closest(".smp");
  if (!btn || !tile) return;
  const { kind, id } = tile.dataset, sec = smpSection(kind);
  const item = sec.items().find((i) => i.id === id);
  const label = `«${item.label}»`;
  switch (btn.dataset.sact) {
    case "play": {
      const t = smpTrack(kind, item);
      if (S.playingId === t.id && audio.src) { audio.paused ? audio.play() : audio.pause(); break; }
      playTrack(t, smpPlayList());
      break;
    }
    case "make": makeSample(kind, id); break;
    case "regen": {
      const ok = await dialog({ title: "Сгенерировать заново?", text: `Новый сэмпл ${label} заменит текущий.`, ok: "Сгенерировать" });
      if (ok) makeSample(kind, id, true);
      break;
    }
    case "del": {
      const ok = await dialog({ title: "Удалить сэмпл?", text: `Сэмпл ${label} будет удалён; его можно будет создать снова.`, ok: "Удалить", danger: true });
      if (!ok) break;
      if (S.playingId === "smp:" + smpKey(kind, id)) { audio.pause(); audio.removeAttribute("src"); S.playingId = null; $("#pTitle").textContent = "Ничего не играет"; }
      try { await api(`/samples/${kind}/${encodeURIComponent(id)}`, { method: "DELETE" }); } catch (err) { alert(err.message); }
      loadSamples();
      break;
    }
    case "use": toggleInForm(kind, id); break;
  }
});
$("#smpRegenAll").addEventListener("click", async () => {
  const n = SMP_SECTIONS.reduce((a, sec) => a + sec.items().length, 0);
  const ok = await dialog({ title: "Пересоздать все сэмплы?",
    text: `В очередь встанут ${n} сэмплов (все жанры и настроения), готовые будут заменены новыми по мере генерации. `
      + `Это займёт порядка ${Math.ceil((n * 10) / 60)} мин; очередь можно остановить.`, ok: "Пересоздать" });
  if (!ok) return;
  try { await api("/samples/all", { method: "POST", body: { replace: true } }); } catch (err) { alert(err.message); return; }
  loadSamples();
  pollNow();
});
$("#smpDeleteAll").addEventListener("click", async () => {
  const n = Object.keys(LIB.samples).length;
  const ok = await dialog({ title: "Удалить все сэмплы?",
    text: `Будут удалены все сэмплы библиотеки (${n}), ожидающие в очереди сэмплы — отменены.`, ok: "Удалить всё", danger: true });
  if (!ok) return;
  if (S.playingId && S.playingId.startsWith("smp:")) { audio.pause(); audio.removeAttribute("src"); S.playingId = null; $("#pTitle").textContent = "Ничего не играет"; }
  try { await api("/samples", { method: "DELETE" }); } catch (err) { alert(err.message); }
  pollNow();
  loadSamples();
});
try { $("#smpAuto").checked = localStorage.getItem("smpAuto") !== "0"; } catch { /* ok */ }
$("#smpAuto").addEventListener("change", () => {
  try { localStorage.setItem("smpAuto", $("#smpAuto").checked ? "1" : "0"); } catch { /* ok */ }
});
let smpSearchTimer = null;
$("#smpSearch").addEventListener("input", () => { clearTimeout(smpSearchTimer); smpSearchTimer = setTimeout(renderSamples, 150); });
try { $("#smpFilter").value = localStorage.getItem("smpFilter") || "all"; } catch { /* ok */ }
if (!$("#smpFilter").value) $("#smpFilter").value = "all";
$("#smpFilter").addEventListener("change", () => {
  try { localStorage.setItem("smpFilter", $("#smpFilter").value); } catch { /* ok */ }
  renderSamples();
});

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
  setChipSummary($("#c-moodSum"), S.presets.moods, C.moods);
  renderTagWarnings(C.genres, C.moods, $("#c-tagWarn"));
  renderGenreLine($("#c-genreLine"), $("#c-prompt"), { genres: [...C.genres], neg: [] }, false);
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
  genreAutocomplete($("#c-prompt"), { allowNeg: false,
    noteText: "В каверах исключить жанр нельзя: мелодию и структуру задаёт исходник, языковая модель не участвует, "
      + "а исключение жанра в ACE работает только через неё. Просто не добавляйте ненужный жанр.", onChange: (pg) => { setTo(C.genres, pg.genres); syncCoverChips(); } });
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
    : "В архиве пока нет треков";
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
    ? ` Сделанные из него каверы (${it.covers}) останутся в архиве, но повторить их с этим исходником не получится.` : "");
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
  $("#c-srcInfo").textContent = `${src.kind === "upload" ? "загруженный файл" : "трек из архива"} · ${fmtTime(src.duration)}`;
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
    ...(({ prompt, genres }) => ({ prompt, genres }))(parseGenres($("#c-prompt").value, false)),
    prompt_text: $("#c-prompt").value,
    moods: [...C.moods], vocal: C.vocal,
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
  $("#c-prompt").value = genresToText(req, false);
  setTo(C.genres, parseGenres($("#c-prompt").value, false).genres);
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
    else { setCoverSource(null); if (!quiet) coverMsg("Трек-исходник удалён из архива", "err"); }
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
      await loadSourcePanel();   // трек-исходник из архива должен быть в кэше
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
