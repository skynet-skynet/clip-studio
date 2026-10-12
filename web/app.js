/* Clip Studio GUI — без сборки и внешних библиотек. Весь текст из модели и файлов вставляется через textContent. */
"use strict";

// ---------- утилиты ----------
const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") el.setAttribute("style", v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === undefined || kid === null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
    delete init.json;
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch (_) { /* пустой ответ */ }
  if (!res.ok) throw new Error((data && data.detail) || `Ошибка ${res.status}`);
  return data;
}

let toastTimer = null;
function toast(msg, bad = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (bad ? " bad" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), bad ? 7000 : 3500);
}

const fmtTime = (s) => {
  s = Math.max(0, Math.round(Number(s) || 0));
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return hh ? `${hh}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${mm}:${String(ss).padStart(2, "0")}`;
};
const fmtSec = (s) => `${(Number(s) || 0).toFixed(1)} с`;

function busy(btn, label) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = label;
  return () => { btn.disabled = false; btn.textContent = old; };
}

const STATUS_RU = {
  queued: ["В очереди", "wait"], transcribing: ["Транскрибация", "wait"], analyzing: ["Менторы смотрят", "wait"],
  waiting_manual: ["Ждёт ответа Claude", "wait"], review: ["Выбор моментов", ""], rendering: ["Рендер", "wait"],
  done: ["Готово", "ok"], error: ["Ошибка", "bad"], cancelled: ["Отменено", ""],
};
const ACTIVE = new Set(["queued", "transcribing", "analyzing", "rendering"]);
const LICENSE_RU = { own: "Мои видео", permission: "Разрешение автора", "cc-by": "CC BY", "public-domain": "Общественное достояние" };
const PUB_RU = { published: ["Опубликовано", "ok"], draft: ["Черновик", "wait"], pack_ready: ["Пакет готов", "ok"], error: ["Ошибка", "bad"], skipped: ["Пропущено", ""] };
const badge = (map, key) => { const [t, c] = map[key] || [key, ""]; return h("span", { class: `badge ${c}`, text: t }); };

function field(label, input, hint) {
  return h("label", { class: "field" }, h("span", { text: label }), input, hint ? h("small", { text: hint }) : null);
}
function select(name, options, value) {
  return h("select", { name }, options.map(([v, t]) => h("option", { value: v, selected: String(v) === String(value) }, t)));
}

function dialog(build) {
  const dlg = $("#dialog");
  const body = $("#dialog-body");
  body.replaceChildren(...build(() => dlg.close()));
  dlg.showModal();
}

// ---------- состояние ----------
const state = { status: null, videos: [], sources: [], currentJob: null, timer: null, config: null };

async function loadStatus() {
  try {
    state.status = await api("/api/status");
  } catch (e) {
    state.status = null;
  }
  renderHealth();
}

function renderHealth() {
  const s = state.status;
  const box = $("#health");
  if (!s) { box.replaceChildren(h("div", { class: "row" }, h("i", { class: "dot bad" }), "Сервер не отвечает")); return; }
  const rows = [
    ["Claude", s.llm.ready, `${s.llm.message}`],
    ["ffmpeg", s.ffmpeg.ready, s.ffmpeg.message],
    ["Распознавание речи", s.transcribe.ready, s.transcribe.message],
  ];
  const pubs = s.publishers.filter((p) => p.enabled);
  box.replaceChildren(
    ...rows.map(([n, ok, msg]) => h("div", { class: "row", title: msg }, h("i", { class: `dot ${ok ? "ok" : "bad"}` }), n)),
    ...pubs.map((p) => h("div", { class: "row", title: p.message }, h("i", { class: `dot ${p.configured ? "ok" : "bad"}` }), p.title)),
  );
}

// ---------- навигация ----------
const VIEWS = { studio: viewStudio, videos: viewVideos, sources: viewSources, clips: viewClips, pubs: viewPubs, settings: viewSettings, guide: viewGuide };

function route() {
  const [path] = (location.hash.slice(1) || "studio").split("?");
  const [name, arg] = path.split("/");
  const view = VIEWS[name] ? name : "studio";
  document.querySelectorAll(".nav button").forEach((b) => b.setAttribute("aria-current", b.dataset.view === view ? "page" : "false"));
  clearInterval(state.timer);
  const main = $("#main");
  const box = h("div", { class: "view" });
  main.replaceChildren(box);
  VIEWS[view](box, arg).catch((e) => box.append(h("p", { class: "risk", text: e.message })));
  window.scrollTo(0, 0);
}

document.querySelectorAll(".nav button").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.view; }));
window.addEventListener("hashchange", route);

// тема
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  $("#theme-toggle").textContent = "Тема: " + { auto: "авто", light: "светлая", dark: "тёмная" }[t];
}
let theme = "auto";
try { theme = localStorage.getItem("cs-theme") || "auto"; } catch (_) { /* нет доступа к хранилищу */ }
applyTheme(theme);
$("#theme-toggle").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  applyTheme(theme);
  try { localStorage.setItem("cs-theme", theme); } catch (_) { /* ничего */ }
});

async function refreshLists() {
  const [v, s] = await Promise.all([api("/api/videos"), api("/api/sources")]);
  state.videos = v.videos;
  state.sources = s.sources;
  $("#nav-videos").textContent = state.videos.length || "";
}

// ================= СТУДИЯ =================
async function viewStudio(box, jobArg) {
  await refreshLists();
  const ready = state.videos.filter((v) => v.rights_status === "cleared" && v.status !== "error" && v.status !== "downloading");
  box.append(
    h("h1", { text: "Что ищем?" }),
    h("p", { class: "lede", text: "Опишите, какие моменты нужны, — менторы найдут их в выбранных видео. Точную фразу возьмите в кавычки: «ну это база»." }),
  );
  const picked = new Set(ready.length === 1 ? [ready[0].id] : []);
  const preselect = new URLSearchParams(location.hash.split("?")[1] || "").get("video");
  if (preselect) picked.add(Number(preselect));

  const req = h("textarea", { placeholder: "Например: 5 клипов, где он уверен в себе, а потом всё идёт не так", "aria-label": "Запрос" });
  const picks = h("div", { class: "video-picks" });
  const drawPicks = () => picks.replaceChildren(
    ...(ready.length ? ready.map((v) => h("button", {
      type: "button", class: "pick", "aria-pressed": picked.has(v.id) ? "true" : "false",
      onclick: () => { picked.has(v.id) ? picked.delete(v.id) : picked.add(v.id); drawPicks(); },
    }, `${v.title} · ${fmtTime(v.duration)}`)) : [h("span", { class: "muted" }, "Сначала добавьте видео в разделе «Видео».")]),
  );
  drawPicks();
  const count = h("input", { type: "number", min: 1, max: 50, value: 5 });
  const minS = h("input", { type: "number", min: 5, max: 600, placeholder: "из настроек" });
  const maxS = h("input", { type: "number", min: 5, max: 900, placeholder: "из настроек" });
  const fmt = select("format", [["", "Как в настройках"], ["vertical", "Вертикально 9:16"], ["horizontal", "Горизонтально 16:9"], ["square", "Квадрат 1:1"]], "");
  const reframe = select("reframe", [["", "Как в настройках"], ["blur", "Размытый фон"], ["center", "Обрезка по центру"], ["face", "Следить за лицом"]], "");
  const subs = select("subs", [["", "Как в настройках"], ["karaoke", "Подсветка слов"], ["plain", "Обычные"], ["none", "Без субтитров"]], "");
  const comp = h("input", { type: "checkbox" });
  const auto = h("input", { type: "checkbox" });
  const jump = h("input", { type: "checkbox" });
  const go = h("button", { class: "btn marker", type: "button" }, "Найти моменты");
  go.addEventListener("click", async () => {
    if (!picked.size) return toast("Выберите хотя бы одно видео", true);
    const done = busy(go, "Запускаю…");
    try {
      const { job } = await api("/api/jobs", { json: {
        request: req.value, video_ids: [...picked], count: Number(count.value) || 5,
        min_seconds: minS.value ? Number(minS.value) : null, max_seconds: maxS.value ? Number(maxS.value) : null,
        format: fmt.value || null, reframe: reframe.value || null, subtitle_style: subs.value || null,
        compilation: comp.checked, auto: auto.checked, jumpcut: jump.checked,
      } });
      location.hash = `studio/${job.id}`;
    } catch (e) { toast(e.message, true); } finally { done(); }
  });
  box.append(h("section", { class: "composer" },
    req,
    h("div", { class: "field" }, h("span", { text: "Видео" }), picks),
    h("div", { class: "grid cols-4" }, field("Сколько клипов", count), field("Мин. длина, с", minS), field("Макс. длина, с", maxS), field("Формат", fmt)),
    h("div", { class: "grid cols-4" }, field("Кадрирование", reframe), field("Субтитры", subs),
      h("label", { class: "check" }, comp, "Склеить в один ролик"),
      h("label", { class: "check", title: "Сам одобрит моменты с оценкой от 7 и отрендерит. Публикация всё равно ручная." }, auto, "Авторежим")),
    h("label", { class: "check", title: "Паузы длиннее 0,6 с внутри клипа вырезаются по таймкодам слов" }, jump, "Вырезать паузы внутри клипов"),
    h("div", { class: "actions" }, go, h("span", { class: "note", text: "Поиск идёт на Sonnet, разбор готовых клипов — на Opus." })),
  ));

  const { jobs } = await api("/api/jobs");
  const list = h("div", { class: "jobs" });
  const detail = h("div");
  box.append(h("h2", { text: "Задачи" }), jobs.length ? list : h("div", { class: "empty", text: "Задач пока нет. Опишите запрос выше и нажмите «Найти моменты»." }), detail);
  const current = Number(jobArg) || (jobs[0] && jobs[0].id);
  jobs.slice(0, 12).forEach((j) => list.append(h("button", {
    class: "job-row", type: "button", "aria-current": j.id === current ? "true" : "false",
    onclick: () => { location.hash = `studio/${j.id}`; },
  },
  h("span", { class: "req", text: `#${j.id} ${j.request || "Без описания"}` }), badge(STATUS_RU, j.status),
  h("span", { class: "note", text: j.stage || "" }), h("span", { class: "note", text: j.cost_usd ? `≈ $${j.cost_usd}` : "" }),
  ACTIVE.has(j.status) ? h("div", { class: "progress" }, h("i", { style: `width:${Math.round((j.progress || 0) * 100)}%` })) : null)));
  if (current) await drawJob(detail, current);
}

async function drawJob(box, jobId) {
  let last = "";
  const draw = async () => {
    let data;
    try { data = await api(`/api/jobs/${jobId}`); } catch (e) { box.replaceChildren(h("p", { class: "risk", text: e.message })); return; }
    const { job, candidates, clips } = data;
    const sig = `${job.status}|${job.progress}|${candidates.length}|${clips.length}|${(job.log || []).length}`;
    if (sig === last) return;
    const keepCands = last && job.status === "review" && last.startsWith("review|");
    last = sig;
    if (keepCands) return; // пользователь размечает — не перерисовываем
    box.replaceChildren(...jobPanel(job, candidates, clips, draw));
    if (!ACTIVE.has(job.status) && job.status !== "waiting_manual") clearInterval(state.timer);
  };
  await draw();
  clearInterval(state.timer);
  state.timer = setInterval(draw, 2500);
}

function jobPanel(job, cands, clips, redraw) {
  const out = [];
  const head = h("div", { class: "panel" },
    h("div", { class: "cand-head" },
      h("div", {}, h("h3", { text: `Задача #${job.id}` }), h("p", { class: "note", text: job.request })),
      badge(STATUS_RU, job.status)),
    h("p", { class: "note", text: job.stage || "" }),
    ACTIVE.has(job.status) ? h("div", { class: "progress" }, h("i", { style: `width:${Math.round((job.progress || 0) * 100)}%` })) : null,
    job.error ? h("p", { class: "risk", text: job.error }) : null,
    h("details", {}, h("summary", { class: "note", text: "Журнал" }), h("div", { class: "log", text: (job.log || []).join("\n") })),
  );
  const acts = h("div", { class: "actions", style: "margin-top:12px" });
  if (ACTIVE.has(job.status)) acts.append(h("button", { class: "btn ghost small", onclick: async () => { await api(`/api/jobs/${job.id}/cancel`, { method: "POST" }); redraw(); } }, "Отменить"));
  if (job.status === "waiting_manual") acts.append(h("button", { class: "btn marker", onclick: () => manualDialog(job.id, redraw) }, "Открыть промпт для Claude"));
  const approved = cands.filter((c) => c.status === "approved").length;
  if (["review", "done"].includes(job.status) && approved) {
    acts.append(h("button", { class: "btn marker", onclick: async (e) => {
      const done = busy(e.target, "Ставлю в очередь…");
      try { await api(`/api/jobs/${job.id}/render`, { method: "POST" }); toast("Рендер начался"); route(); }
      catch (err) { toast(err.message, true); } finally { done(); }
    } }, job.params?.compilation ? `Склеить ${approved} в ролик` : `Отрендерить ${approved}`));
  }
  if (cands.length) {
    acts.append(
      h("a", { class: "btn ghost small", href: `/api/jobs/${job.id}/export?format=edl`, title: "Таймлайн для DaVinci Resolve и Premiere" }, "EDL для монтажки"),
      h("a", { class: "btn ghost small", href: `/api/jobs/${job.id}/export?format=csv`, title: "Отрезки для LosslessCut" }, "CSV для LosslessCut"));
  }
  head.append(acts);
  out.push(head);

  if (clips.length) {
    out.push(h("h2", { text: "Готовые клипы" }), h("div", { class: "clips" }, clips.map(clipTile)));
  }
  if (cands.length) {
    out.push(h("h2", { text: `Кандидаты (${cands.length})` }));
    const list = h("div", { class: "cands" });
    cands.forEach((c) => list.append(candCard(c, job)));
    out.push(list);
  } else if (job.status === "review") {
    out.push(h("div", { class: "empty", text: "Менторы не нашли подходящих моментов. Переформулируйте запрос или выберите другое видео." }));
  }
  return out;
}

function candCard(c, job) {
  const video = state.videos.find((v) => v.id === c.video_id);
  const dur = video ? video.duration : 0;
  const card = h("article", { class: `cand ${c.status}` });
  const startIn = h("input", { type: "number", step: "0.1", value: c.start, "aria-label": "Начало, сек" });
  const endIn = h("input", { type: "number", step: "0.1", value: c.end, "aria-label": "Конец, сек" });
  const set = async (fields) => {
    try {
      const { candidate } = await api(`/api/candidates/${c.id}`, { json: fields });
      card.replaceWith(candCard(candidate, job));
    } catch (e) { toast(e.message, true); }
  };
  const s = c.scores || {};
  const scoreEl = (k, label) => h("div", { class: "score" }, label, " ", h("b", { text: s[k] ?? "—" }),
    h("div", { class: "bar" }, h("i", { style: `width:${(Number(s[k]) || 0) * 10}%` })));
  card.append(
    h("div", {},
      c.preview_url ? h("video", { src: c.preview_url, controls: true, preload: "none", playsinline: true }) : null,
      dur ? h("div", { class: "strip", title: "Где момент в видео" }, h("i", { style: `left:${(c.start / dur) * 100}%;width:${((c.end - c.start) / dur) * 100}%` })) : null,
      h("div", { class: "times" }, startIn, "–", endIn,
        h("button", { class: "btn ghost small", onclick: () => set({ start: Number(startIn.value), end: Number(endIn.value) }) }, "Сохранить время")),
      h("p", { class: "note", text: `${video ? video.title : "видео " + c.video_id} · ${fmtTime(c.start)}–${fmtTime(c.end)} · ${fmtSec(c.end - c.start)}` }),
    ),
    h("div", {},
      h("div", { class: "cand-head" },
        h("div", {}, c.hook ? h("span", { class: "hook", text: c.hook }) : null, h("p", { class: "title", text: c.title })),
        h("div", { class: "total", title: "Итоговая оценка" }, (c.total || 0).toFixed(1))),
      c.quote ? h("p", { class: "quote" }, h("mark", { text: c.quote })) : null,
      c.why ? h("p", { class: "why", text: c.why }) : null,
      c.risk ? h("p", { class: "risk", text: c.risk }) : null,
      h("div", { class: "scores" }, scoreEl("editor", "Монтаж"), scoreEl("comedian", "Юмор"), scoreEl("retention", "Удержание"), scoreEl("compliance", "Право")),
      c.status === "rendered" ? h("span", { class: "badge ok", text: "Отрендерен" }) : h("div", { class: "actions" },
        c.status !== "approved" ? h("button", { class: "btn", onclick: () => set({ status: "approved" }) }, "Взять") : h("button", { class: "btn ghost", onclick: () => set({ status: "proposed" }) }, "Не брать"),
        c.status !== "rejected" ? h("button", { class: "btn ghost", onclick: () => set({ status: "rejected" }) }, "Отклонить") : h("button", { class: "btn ghost", onclick: () => set({ status: "proposed" }) }, "Вернуть"),
        h("button", { class: "btn ghost", onclick: () => { location.hash = `edit/${c.video_id}?cand=${c.id}`; } }, "В монтаж"),
        c.origin === "phrase" ? h("span", { class: "badge", text: "Точная фраза" }) : null),
    ),
  );
  return card;
}

async function manualDialog(jobId, redraw) {
  let st;
  try { st = await api(`/api/jobs/${jobId}/manual`); } catch (e) { return toast(e.message, true); }
  if (!st.prompt) return toast("Задача не ждёт ответа");
  const answer = h("textarea", { rows: 8, placeholder: "Вставьте сюда JSON-ответ Claude целиком" });
  dialog((close) => [
    h("h3", { text: `Ручной режим: кусок ${st.chunk} из ${st.chunks_total}` }),
    h("p", { class: "note", text: "1. Скопируйте промпт. 2. Вставьте его в новый чат на claude.ai или в Claude Code. 3. Скопируйте ответ (JSON) и вставьте ниже." }),
    h("pre", { text: st.prompt }),
    h("div", { class: "actions" },
      h("button", { class: "btn", onclick: async () => { await navigator.clipboard.writeText(st.prompt); toast("Промпт скопирован"); } }, "Скопировать промпт"),
      h("a", { class: "btn ghost", href: "https://claude.ai/new", target: "_blank", rel: "noopener" }, "Открыть claude.ai")),
    answer,
    h("div", { class: "actions" },
      h("button", { class: "btn marker", onclick: async (e) => {
        const done = busy(e.target, "Отправляю…");
        try {
          const r = await api(`/api/jobs/${jobId}/manual`, { json: { answer: answer.value } });
          close();
          if (r.next) manualDialog(jobId, redraw); else { toast("Ответ принят, собираю кандидатов"); redraw(); }
        } catch (err) { toast(err.message, true); } finally { done(); }
      } }, "Отправить ответ"),
      h("button", { class: "btn ghost", onclick: close }, "Закрыть")),
  ]);
}

// ================= ВИДЕО =================
async function viewVideos(box) {
  await refreshLists();
  box.append(h("h1", { text: "Видео" }), h("p", { class: "lede", text: "Всё, что попадает сюда, проходит юридический шлюз: видео берётся только из источника в белом списке." }));
  const srcOpts = state.sources.filter((s) => s.enabled !== false).map((s) => [s.id, `${s.name} (${LICENSE_RU[s.license] || s.license})`]);
  if (!srcOpts.length) {
    box.append(h("div", { class: "empty" }, "Белый список пуст. ", h("a", { href: "#sources" }, "Добавьте источник"), ", чтобы загружать видео."));
  }

  // загрузка файла
  const file = h("input", { type: "file", accept: "video/*,.mkv,.mov" });
  const subsF = h("input", { type: "file", accept: ".srt,.vtt" });
  const src1 = select("source_id", srcOpts, srcOpts[0]?.[0]);
  const title = h("input", { type: "text", placeholder: "По имени файла" });
  const origin = h("input", { type: "url", placeholder: "https://… (для CC BY обязательно)" });
  const ccConfirm = h("input", { type: "checkbox" });
  const ccRow = h("label", { class: "check" }, ccConfirm, "Я проверил: оригинал опубликован под CC BY (не BY-ND)");
  const syncCC = () => { const s = state.sources.find((x) => x.id === src1.value); ccRow.hidden = !(s && s.license === "cc-by"); };
  src1.addEventListener("change", syncCC); syncCC();
  const bar = h("div", { class: "progress", hidden: true }, h("i", { style: "width:0" }));
  const up = h("button", { class: "btn", type: "button" }, "Загрузить");
  up.addEventListener("click", () => {
    if (!file.files[0]) return toast("Выберите видеофайл", true);
    const fd = new FormData();
    fd.append("file", file.files[0]);
    fd.append("source_id", src1.value);
    fd.append("title", title.value);
    fd.append("origin_url", origin.value);
    fd.append("license_confirmed", ccConfirm.checked ? "true" : "false");
    if (subsF.files[0]) fd.append("subs", subsF.files[0]);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/videos/upload");
    bar.hidden = false;
    const done = busy(up, "Загружаю…");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.firstChild.style.width = `${(e.loaded / e.total) * 100}%`; };
    xhr.onload = () => {
      done(); bar.hidden = true;
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* ничего */ }
      if (xhr.status >= 400) return toast(data.detail || "Не загрузилось", true);
      toast("Видео добавлено"); route();
    };
    xhr.onerror = () => { done(); toast("Сервер недоступен", true); };
    xhr.send(fd);
  });
  box.append(h("h2", { text: "С компьютера" }), h("div", { class: "panel grid" },
    h("div", { class: "grid cols-2" }, field("Видеофайл", file), field("Субтитры (необязательно)", subsF, "Если есть .srt/.vtt — распознавание речи не понадобится")),
    h("div", { class: "grid cols-3" }, field("Источник", src1), field("Название", title), field("Ссылка на оригинал", origin)),
    ccRow, bar, h("div", { class: "actions" }, up)));

  // по ссылке
  const url = h("input", { type: "url", placeholder: "https://vkvideo.ru/… или https://www.youtube.com/watch?v=…" });
  const src2 = select("source_id", srcOpts, srcOpts[0]?.[0]);
  const fetchBtn = h("button", { class: "btn", type: "button" }, "Проверить права и скачать");
  fetchBtn.addEventListener("click", async () => {
    const done = busy(fetchBtn, "Проверяю права…");
    try {
      const r = await api("/api/videos/url", { json: { url: url.value, source_id: src2.value } });
      toast(r.video ? "Права подтверждены, загрузка идёт" : r.message);
      route();
    } catch (e) { toast(e.message, true); } finally { done(); }
  });
  box.append(h("h2", { text: "По ссылке" }), h("div", { class: "panel grid" },
    h("div", { class: "grid cols-2" }, field("Ссылка", url), field("Источник из белого списка", src2)),
    h("p", { class: "note", text: "Шлюз сверит лицензию и канал по данным площадки. Если основания нет — скачивания не будет." }),
    h("div", { class: "actions" }, fetchBtn)));

  // поиск
  const q = h("input", { type: "text", placeholder: "Например: неудачный ремонт" });
  const found = h("div");
  const findBtn = h("button", { class: "btn ghost", type: "button" }, "Искать");
  findBtn.addEventListener("click", async () => {
    const done = busy(findBtn, "Ищу…");
    try {
      const r = await api("/api/discover", { json: { query: q.value, limit: 15 } });
      found.replaceChildren(
        ...r.warnings.map((w) => h("p", { class: "note", text: w })),
        r.results.length ? h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, ...["Видео", "Длина", "Качество", "Просмотры", "Права", ""].map((t) => h("th", { text: t })))),
          h("tbody", {}, r.results.map((it) => h("tr", {},
            h("td", {}, h("a", { href: it.url, target: "_blank", rel: "noopener", text: it.title }), h("div", { class: "note", text: it.channel_title })),
            h("td", { text: fmtTime(it.duration_s) }),
            h("td", { text: it.max_height ? `${it.max_height}p${it.note ? " · " + it.note : ""}` : "—" }),
            h("td", { text: (it.views || 0).toLocaleString("ru-RU") }),
            h("td", {}, h("span", { class: `badge ${it.gate.allowed ? "ok" : "bad"}`, text: it.gate.allowed ? "Можно" : "Нельзя", title: it.gate.reason })),
            h("td", {}, h("button", { class: "btn small", disabled: !it.gate.allowed, onclick: async (e) => {
              const done2 = busy(e.target, "…");
              try { await api("/api/videos/url", { json: { url: it.url, source_id: it.source_id } }); toast("Добавлено, загрузка идёт"); }
              catch (err) { toast(err.message, true); } finally { done2(); }
            } }, "Добавить"))))))) : h("p", { class: "muted", text: "Ничего не найдено в разрешённых источниках." }));
    } catch (e) { toast(e.message, true); } finally { done(); }
  });
  box.append(h("h2", { text: "Поиск в разрешённых источниках" }), h("div", { class: "panel grid" },
    h("p", { class: "note", text: "Ищет только по каналам из белого списка (VK и YouTube) и по видео YouTube с лицензией CC BY." }),
    h("div", { class: "actions" }, q, findBtn), found));

  // библиотека
  box.append(h("h2", { text: `Библиотека (${state.videos.length})` }));
  if (!state.videos.length) { box.append(h("div", { class: "empty", text: "Пока пусто. Загрузите видео выше." })); return; }
  box.append(h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ...["Видео", "Длина", "Кадр", "Права", "Статус", ""].map((t) => h("th", { text: t })))),
    h("tbody", {}, state.videos.map((v) => h("tr", {},
      h("td", {}, h("b", { text: v.title }), h("div", { class: "note", text: `#${v.id} · ${(state.sources.find((s) => s.id === v.source_id) || {}).name || v.source_id}` })),
      h("td", { text: fmtTime(v.duration) }),
      h("td", { text: v.height ? `${v.width}×${v.height}` : "—" }),
      h("td", {}, h("span", { class: `badge ${v.rights_status === "cleared" ? "ok" : "bad"}`, text: v.rights_status === "cleared" ? "Можно" : "Нельзя", title: v.rights_reason })),
      h("td", {}, h("span", { class: "note", text: v.status === "ready" ? (v.has_transcript ? "Транскрипт есть" : v.has_subs ? "Есть субтитры" : "Готово") : (v.status_message || v.status) })),
      h("td", {}, h("div", { class: "actions" },
        h("button", { class: "btn small", disabled: v.status !== "ready", onclick: () => { location.hash = `studio?video=${v.id}`; } }, "Нарезать"),
        h("button", { class: "btn ghost small", disabled: !v.media_url || v.status === "downloading", onclick: () => { location.hash = `edit/${v.id}`; } }, "Монтаж"),
        h("button", { class: "btn ghost small", onclick: async () => {
          if (!confirm(`Удалить «${v.title}» из библиотеки? Файл будет удалён.`)) return;
          await api(`/api/videos/${v.id}`, { method: "DELETE" }); route();
        } }, "Удалить")))))))));
}

// ================= ИСТОЧНИКИ =================
async function viewSources(box) {
  await refreshLists();
  box.append(h("h1", { text: "Источники и права" }),
    h("p", { class: "lede", text: "Белый список — единственное, из чего приложение разрешает резать. У каждого источника должно быть основание: вы автор, письменное разрешение, CC BY или общественное достояние." }));
  const form = h("div");
  const drawForm = (src = {}) => {
    const f = {
      id: h("input", { type: "text", value: src.id || "", placeholder: "латиница, например ivan_streams", readonly: !!src.id }),
      name: h("input", { type: "text", value: src.name || "" }),
      platform: select("platform", [["vk", "VK"], ["youtube", "YouTube"], ["local", "Файлы с диска"], ["other", "Другое"]], src.platform || "vk"),
      license: select("license", Object.entries(LICENSE_RU), src.license || "permission"),
      channel_url: h("input", { type: "url", value: src.channel_url || "" }),
      vk_owner_id: h("input", { type: "text", value: src.vk_owner_id || "", placeholder: "-123456 для сообщества" }),
      youtube_channel_id: h("input", { type: "text", value: src.youtube_channel_id || "", placeholder: "UC…" }),
      proof: h("input", { type: "text", value: src.proof || "", placeholder: "proofs/скриншот.png или ссылка на пост с разрешением" }),
      proof_date: h("input", { type: "text", value: src.proof_date || "", placeholder: "2026-10-12" }),
      attribution: h("input", { type: "text", value: src.attribution || "", placeholder: "Автор: Имя — ссылка на канал" }),
      rules: h("textarea", { rows: 2, placeholder: "Условия автора: что нельзя, что обязательно" }, src.rules || ""),
      monetization_allowed: h("input", { type: "checkbox", checked: !!src.monetization_allowed }),
      enabled: h("input", { type: "checkbox", checked: src.enabled !== false }),
    };
    const save = h("button", { class: "btn", type: "button" }, src.id ? "Сохранить источник" : "Добавить источник");
    save.addEventListener("click", async () => {
      const body = Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.type === "checkbox" ? el.checked : el.value.trim()]));
      try { await api("/api/sources", { json: body }); toast("Источник сохранён"); route(); } catch (e) { toast(e.message, true); }
    });
    form.replaceChildren(h("h2", { text: src.id ? `Источник «${src.name}»` : "Новый источник" }), h("div", { class: "panel grid" },
      h("div", { class: "grid cols-4" }, field("ID", f.id), field("Название", f.name), field("Площадка", f.platform), field("Основание", f.license)),
      h("div", { class: "grid cols-3" }, field("Ссылка на канал", f.channel_url), field("VK owner_id", f.vk_owner_id), field("YouTube channel ID", f.youtube_channel_id)),
      h("div", { class: "grid cols-2" }, field("Доказательство разрешения", f.proof, "Обязательно для «Разрешение автора»"), field("Дата разрешения", f.proof_date)),
      field("Атрибуция", f.attribution, "Попадёт в описание и титр. Можно использовать {channel} и {url}"),
      field("Правила автора", f.rules, "Менторы учитывают их при отборе моментов"),
      h("div", { class: "actions" }, h("label", { class: "check" }, f.monetization_allowed, "Автор разрешил монетизацию"), h("label", { class: "check" }, f.enabled, "Источник включён")),
      h("div", { class: "actions" }, save, src.id ? h("button", { class: "btn ghost", onclick: () => drawForm() }, "Новый источник") : null)));
  };
  box.append(h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ...["Источник", "Основание", "Доказательство", "Монетизация", ""].map((t) => h("th", { text: t })))),
    h("tbody", {}, state.sources.map((s) => h("tr", {},
      h("td", {}, h("b", { text: s.name }), h("div", { class: "note", text: `${s.id} · ${s.platform || ""}` }), s.enabled === false ? h("span", { class: "badge", text: "Выключен" }) : null),
      h("td", {}, h("span", { class: "badge", text: LICENSE_RU[s.license] || s.license })),
      h("td", { class: "note", text: s.proof || "—" }),
      h("td", {}, h("span", { class: `badge ${s.monetization_allowed ? "ok" : ""}`, text: s.monetization_allowed ? "Можно" : "Нет" })),
      h("td", {}, h("div", { class: "actions" },
        h("button", { class: "btn ghost small", onclick: () => { drawForm(s); form.scrollIntoView({ behavior: "smooth" }); } }, "Изменить"),
        h("button", { class: "btn ghost small", onclick: async () => {
          if (!confirm(`Удалить источник «${s.name}»? Уже загруженные видео останутся.`)) return;
          await api(`/api/sources/${encodeURIComponent(s.id)}`, { method: "DELETE" }); route();
        } }, "Удалить")))))))), form);
  drawForm();

  const { rows } = await api("/api/ledger");
  box.append(h("h2", { text: "Реестр прав" }),
    h("p", { class: "note", text: "Каждое решение шлюза записывается сюда. Пригодится, если правообладатель пришлёт жалобу." }),
    h("div", { class: "actions", style: "margin-bottom:10px" }, h("button", { class: "btn ghost small", onclick: () => downloadCSV(rows) }, "Скачать CSV")),
    rows.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Когда", "Видео", "Источник", "Решение", "Основание"].map((t) => h("th", { text: t })))),
      h("tbody", {}, rows.slice(0, 50).map((r) => h("tr", {},
        h("td", { class: "note", text: (r.checked_at || "").replace("T", " ") }), h("td", { text: r.video_id ? `#${r.video_id}` : r.origin_url }),
        h("td", { text: r.source_id }), h("td", {}, h("span", { class: `badge ${r.decision === "allowed" ? "ok" : "bad"}`, text: r.decision === "allowed" ? "Разрешено" : "Заблокировано" })),
        h("td", { class: "note", text: r.reason })))))) : h("div", { class: "empty", text: "Записей пока нет." }));
}

function downloadCSV(rows) {
  const cols = ["checked_at", "video_id", "source_id", "origin_url", "license", "proof", "decision", "reason"];
  const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const csv = "﻿" + [cols.join(";"), ...rows.map((r) => cols.map((c) => esc(r[c])).join(";"))].join("\n");
  const a = h("a", { href: URL.createObjectURL(new Blob([csv], { type: "text/csv" })), download: "rights_ledger.csv" });
  a.click();
}

// ================= КЛИПЫ =================
function clipTile(c) {
  const wide = c.width > c.height;
  return h("button", { class: `clip-tile${wide ? " wide" : ""}`, type: "button", onclick: () => { location.hash = `clips/${c.id}`; } },
    c.thumb_url ? h("img", { src: c.thumb_url, alt: "", loading: "lazy" }) : h("div", { class: "empty", text: "нет превью" }),
    h("span", { class: "t", text: c.title || `Клип ${c.id}` }),
    h("span", { class: "note", text: `${fmtSec(c.duration)}${c.review && c.review.verdict ? " · разобран" : ""}` }));
}

async function viewClips(box, clipId) {
  if (clipId) return viewClip(box, Number(clipId));
  const { clips } = await api("/api/clips");
  $("#nav-clips").textContent = clips.length || "";
  box.append(h("h1", { text: "Клипы" }), h("p", { class: "lede", text: "Откройте клип, чтобы отдать его на разбор менторам, поправить название и опубликовать." }));
  box.append(clips.length ? h("div", { class: "clips" }, clips.map(clipTile)) : h("div", { class: "empty" }, "Клипов пока нет. ", h("a", { href: "#studio" }, "Найдите моменты в студии"), "."));
}

async function viewClip(box, id) {
  const [{ clip, publications }, status] = await Promise.all([api(`/api/clips/${id}`), api("/api/status")]);
  state.status = status; renderHealth();
  box.append(h("p", {}, h("a", { href: "#clips", text: "Все клипы" })), h("h1", { text: clip.title || `Клип ${clip.id}` }));
  const title = h("input", { type: "text", value: clip.title || "" });
  const desc = h("textarea", { rows: 5 }, clip.description || "");
  const tags = h("input", { type: "text", value: (clip.tags || []).join(", ") });
  const saveMeta = h("button", { class: "btn ghost", type: "button" }, "Сохранить текст");
  saveMeta.addEventListener("click", async () => {
    try {
      await api(`/api/clips/${id}`, { json: { title: title.value, description: desc.value, tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean) } });
      toast("Сохранено");
    } catch (e) { toast(e.message, true); }
  });

  const reviewBox = h("div", { class: "review" });
  const drawReview = (rv) => {
    if (!rv || !rv.verdict) { reviewBox.replaceChildren(h("p", { class: "note", text: "Менторы (Opus) посмотрят клип целиком: скажут, выпускать ли, предложат названия, хуки и подрезку." })); return; }
    const verdictRu = { publish: "Выпускать", fix: "Выпускать после правок", drop: "Не выпускать" };
    const titleChoice = h("div");
    (rv.title_options || []).forEach((t, i) => titleChoice.append(h("label", { class: "option" }, h("input", { type: "radio", name: "t", value: t, checked: i === 0 }), t)));
    const hookChoice = h("div");
    hookChoice.append(h("label", { class: "option" }, h("input", { type: "radio", name: "hk", value: "", checked: true }), "Оставить текущий хук"));
    (rv.hook_options || []).forEach((t) => hookChoice.append(h("label", { class: "option" }, h("input", { type: "radio", name: "hk", value: t }), t)));
    const trim = rv.trim || {};
    const hasTrim = Number(trim.start_delta) || Number(trim.end_delta);
    const trimBox = h("input", { type: "checkbox", checked: !!hasTrim });
    const apply = h("button", { class: "btn marker", type: "button" }, "Применить правки");
    apply.addEventListener("click", async () => {
      const t = reviewBox.querySelector("input[name=t]:checked");
      const hk = reviewBox.querySelector("input[name=hk]:checked");
      const needRender = (hk && hk.value) || (hasTrim && trimBox.checked);
      const done = busy(apply, needRender ? "Перерендериваю…" : "Сохраняю…");
      try {
        await api(`/api/clips/${id}/apply`, { json: {
          title: t ? t.value : undefined, description: rv.description || undefined, tags: rv.tags || undefined,
          hook: hk && hk.value ? hk.value : undefined, apply_trim: !!(hasTrim && trimBox.checked),
        } });
        toast("Правки применены"); route();
      } catch (e) { toast(e.message, true); } finally { done(); }
    });
    reviewBox.replaceChildren(
      h("div", { class: `verdict ${rv.verdict}`, text: verdictRu[rv.verdict] || rv.verdict }),
      h("ul", {}, (rv.notes || []).map((n) => h("li", { text: n }))),
      h("h3", { text: "Название" }), titleChoice,
      h("h3", { text: "Хук на экране" }), hookChoice,
      hasTrim ? h("label", { class: "check" }, trimBox, `Подрезать: начало ${trim.start_delta >= 0 ? "+" : ""}${trim.start_delta || 0} с, конец ${trim.end_delta >= 0 ? "+" : ""}${trim.end_delta || 0} с`) : null,
      rv.platform_tips ? h("details", {}, h("summary", { class: "note", text: "Советы по площадкам" }), h("ul", {}, rv.platform_tips.map((t) => h("li", { text: t })))) : null,
      h("div", { class: "actions" }, apply, h("span", { class: "note", text: "Описание и теги тоже возьмутся из разбора." })),
    );
  };
  drawReview(clip.review);
  const reviewBtn = h("button", { class: "btn", type: "button" }, clip.review && clip.review.verdict ? "Разобрать заново" : "Разбор менторами");
  reviewBtn.addEventListener("click", async () => {
    const done = busy(reviewBtn, "Менторы смотрят…");
    try {
      const r = await api(`/api/clips/${id}/review`, { method: "POST" });
      if (r.manual_prompt) return manualReviewDialog(id, r.manual_prompt, drawReview);
      drawReview(r.review);
    } catch (e) { toast(e.message, true); } finally { done(); }
  });

  // публикация
  const pubs = status.publishers;
  const checks = pubs.map((p) => ({ p, el: h("input", { type: "checkbox", checked: p.enabled && p.configured, disabled: !p.configured }) }));
  const privacy = select("privacy", [["private", "Приватно / черновик"], ["unlisted", "По ссылке"], ["public", "Публично"]], "private");
  const pubBtn = h("button", { class: "btn marker", type: "button" }, "Опубликовать");
  const pubResults = h("div");
  pubBtn.addEventListener("click", async () => {
    const platforms = checks.filter((c) => c.el.checked).map((c) => c.p.name);
    if (!platforms.length) return toast("Отметьте площадки", true);
    if (privacy.value === "public" && !confirm("Опубликовать публично прямо сейчас?")) return;
    const done = busy(pubBtn, "Публикую…");
    try {
      const { results } = await api(`/api/clips/${id}/publish`, { json: { platforms, privacy: privacy.value } });
      pubResults.replaceChildren(pubTable(results));
      toast("Готово: смотрите результаты ниже");
    } catch (e) { toast(e.message, true); } finally { done(); }
  });

  box.append(h("div", { class: "clip-detail" },
    h("div", {}, h("video", { src: clip.media_url, controls: true, playsinline: true, preload: "metadata", poster: clip.thumb_url || undefined }),
      h("p", { class: "note", text: `${fmtSec(clip.duration)} · ${clip.width}×${clip.height}` }),
      h("div", { class: "actions" },
        h("a", { class: "btn ghost small", href: clip.media_url, download: `clip_${clip.id}.mp4` }, "Скачать"),
        h("button", { class: "btn danger small", onclick: async () => {
          if (!confirm("Удалить клип? Файл будет удалён.")) return;
          await api(`/api/clips/${id}`, { method: "DELETE" }); location.hash = "clips";
        } }, "Удалить"))),
    h("div", { class: "grid" },
      h("section", { class: "panel grid" }, field("Название", title), field("Описание", desc), field("Теги через запятую", tags),
        clip.attribution ? h("p", { class: "note", text: `Атрибуция: ${clip.attribution}` }) : null,
        clip.monetization_allowed ? h("span", { class: "badge ok", text: "Монетизация разрешена" }) : h("span", { class: "badge bad", text: "Монетизация не разрешена источником" }),
        h("div", { class: "actions" }, saveMeta)),
      h("section", { class: "panel grid" }, h("div", { class: "cand-head" }, h("h3", { text: "Разбор менторами" }), reviewBtn), reviewBox),
      h("section", { class: "panel grid" }, h("h3", { text: "Публикация" }),
        h("div", { class: "grid" }, checks.map(({ p, el }) => h("label", { class: "check", title: p.message }, el, p.title, !p.configured ? h("span", { class: "note", text: ` — ${p.message}` }) : null))),
        field("Видимость", privacy, "По умолчанию всё уходит приватно — вы проверяете и публикуете на площадке."),
        h("div", { class: "actions" }, pubBtn), pubResults,
        publications.length ? h("details", {}, h("summary", { class: "note", text: `История (${publications.length})` }), pubTable(publications)) : null))));
}

function manualReviewDialog(id, prompt, onDone) {
  const answer = h("textarea", { rows: 8, placeholder: "JSON-ответ Claude" });
  dialog((close) => [
    h("h3", { text: "Разбор менторами вручную" }),
    h("p", { class: "note", text: "Скопируйте промпт в Claude (лучше Opus), вставьте ответ ниже." }),
    h("pre", { text: prompt }),
    h("div", { class: "actions" }, h("button", { class: "btn", onclick: async () => { await navigator.clipboard.writeText(prompt); toast("Скопировано"); } }, "Скопировать промпт")),
    answer,
    h("div", { class: "actions" }, h("button", { class: "btn marker", onclick: async () => {
      try { const r = await api(`/api/clips/${id}/review/manual`, { json: { answer: answer.value } }); close(); onDone(r.review); }
      catch (e) { toast(e.message, true); }
    } }, "Принять ответ"), h("button", { class: "btn ghost", onclick: close }, "Закрыть")),
  ]);
}

function pubTable(rows) {
  const names = Object.fromEntries((state.status?.publishers || []).map((p) => [p.name, p.title]));
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ...["Площадка", "Статус", "Ссылка", "Сообщение"].map((t) => h("th", { text: t })))),
    h("tbody", {}, rows.map((r) => h("tr", {},
      h("td", { text: names[r.platform] || r.platform }), h("td", {}, badge(PUB_RU, r.status)),
      h("td", {}, r.url && r.url.startsWith("http") ? h("a", { href: r.url, target: "_blank", rel: "noopener", text: "Открыть" }) : h("span", { class: "note", text: r.url ? "папка на диске" : "—" })),
      h("td", { class: "note", text: r.message || "" }))))));
}

// ================= ПУБЛИКАЦИИ =================
async function viewPubs(box) {
  const [{ publications }] = await Promise.all([api("/api/publications"), loadStatus()]);
  box.append(h("h1", { text: "Публикации" }), h("p", { class: "lede", text: "Всё, что отправлялось на площадки, с результатом." }),
    publications.length ? pubTable(publications) : h("div", { class: "empty", text: "Публикаций пока нет." }));
}

// ================= НАСТРОЙКИ =================
async function viewSettings(box) {
  const [{ config }, status] = await Promise.all([api("/api/config"), api("/api/status")]);
  state.status = status; renderHealth();
  const L = config.llm, T = config.transcribe, C = config.clips, P = config.publish, PL = P.platforms;
  box.append(h("h1", { text: "Настройки" }), h("p", { class: "lede", text: "Ключи хранятся только в файле .env на этом компьютере. Подробные инструкции по каждой площадке — в разделе «Гайд»." }));

  const f = {
    mode: select("mode", [["claude_cli", "Claude Code (подписка Pro/Max)"], ["api", "Anthropic API (оплата по токенам)"], ["manual", "Вручную через claude.ai"]], L.mode),
    mm: select("mm", [["sonnet", "Sonnet — быстро и дёшево"], ["opus", "Opus — точнее, дороже"], ["haiku", "Haiku — самый дешёвый"]], L.model_moments),
    mr: select("mr", [["opus", "Opus"], ["sonnet", "Sonnet"]], L.model_review),
    chunk: h("input", { type: "number", min: 2, max: 30, value: L.chunk_minutes }),
    engine: select("engine", [["faster_whisper", "faster-whisper"], ["subtitles_only", "Только готовые субтитры"]], T.engine),
    wmodel: select("wmodel", ["tiny", "base", "small", "medium", "large-v3"].map((m) => [m, m]), T.model),
    device: select("device", [["auto", "Авто"], ["cpu", "Процессор"], ["cuda", "Видеокарта NVIDIA"]], T.device),
    fmt: select("fmt", [["vertical", "Вертикально 9:16"], ["horizontal", "Горизонтально 16:9"], ["square", "Квадрат 1:1"]], C.format),
    reframe: select("reframe", [["blur", "Размытый фон"], ["center", "Обрезка по центру"], ["face", "Следить за лицом"]], C.reframe),
    subs: select("subs", [["karaoke", "Подсветка слов"], ["plain", "Обычные"], ["none", "Без субтитров"]], C.subtitle_style),
    minS: h("input", { type: "number", value: C.min_seconds }), maxS: h("input", { type: "number", value: C.max_seconds }),
    hook: h("input", { type: "checkbox", checked: !!C.hook_title }), attr: h("input", { type: "checkbox", checked: !!C.attribution_overlay }),
    privacy: select("privacy", [["private", "Приватно"], ["unlisted", "По ссылке"], ["public", "Публично"]], P.default_privacy),
    vkOn: h("input", { type: "checkbox", checked: !!PL.vk.enabled }), vkGroup: h("input", { type: "text", value: PL.vk.group_id || "" }),
    vkAlbum: h("input", { type: "text", value: PL.vk.album_id || "" }), vkWall: h("input", { type: "checkbox", checked: !!PL.vk.wallpost }),
    ytOn: h("input", { type: "checkbox", checked: !!PL.youtube.enabled }),
    tgOn: h("input", { type: "checkbox", checked: !!PL.telegram.enabled }), tgChat: h("input", { type: "text", value: PL.telegram.chat_id || "", placeholder: "@mychannel" }),
    tgReview: h("input", { type: "text", value: PL.telegram.review_chat_id || "", placeholder: "ваш числовой id" }),
    packOn: h("input", { type: "checkbox", checked: !!PL.pack.enabled }),
  };
  const save = h("button", { class: "btn", type: "button" }, "Сохранить настройки");
  save.addEventListener("click", async () => {
    try {
      await api("/api/config", { json: {
        llm: { mode: f.mode.value, model_moments: f.mm.value, model_review: f.mr.value, chunk_minutes: Number(f.chunk.value) },
        transcribe: { engine: f.engine.value, model: f.wmodel.value, device: f.device.value },
        clips: { format: f.fmt.value, reframe: f.reframe.value, subtitle_style: f.subs.value, min_seconds: Number(f.minS.value), max_seconds: Number(f.maxS.value), hook_title: f.hook.checked, attribution_overlay: f.attr.checked },
        publish: { default_privacy: f.privacy.value, platforms: {
          vk: { enabled: f.vkOn.checked, group_id: f.vkGroup.value.trim(), album_id: f.vkAlbum.value.trim(), wallpost: f.vkWall.checked },
          youtube: { enabled: f.ytOn.checked },
          telegram: { enabled: f.tgOn.checked, chat_id: f.tgChat.value.trim(), review_chat_id: f.tgReview.value.trim() },
          pack: { enabled: f.packOn.checked },
        } },
      } });
      toast("Настройки сохранены"); loadStatus();
    } catch (e) { toast(e.message, true); }
  });

  box.append(
    h("h2", { text: "Claude" }), h("div", { class: "panel grid" },
      h("div", { class: "grid cols-4" }, field("Режим", f.mode), field("Поиск моментов", f.mm), field("Разбор клипов", f.mr), field("Кусок транскрипта, мин", f.chunk)),
      h("p", { class: "note", text: status.llm.message })),
    h("h2", { text: "Распознавание речи" }), h("div", { class: "panel grid" },
      h("div", { class: "grid cols-3" }, field("Движок", f.engine), field("Модель Whisper", f.wmodel, "small — для процессора, large-v3 — для видеокарты"), field("Устройство", f.device)),
      h("p", { class: "note", text: status.transcribe.message })),
    h("h2", { text: "Клипы по умолчанию" }), h("div", { class: "panel grid" },
      h("div", { class: "grid cols-3" }, field("Формат", f.fmt), field("Кадрирование", f.reframe), field("Субтитры", f.subs)),
      h("div", { class: "grid cols-4" }, field("Мин. длина, с", f.minS), field("Макс. длина, с", f.maxS),
        h("label", { class: "check" }, f.hook, "Хук-заголовок в начале"), h("label", { class: "check" }, f.attr, "Титр с атрибуцией"))),
    h("h2", { text: "Площадки" }), h("div", { class: "panel grid" },
      field("Видимость по умолчанию", f.privacy),
      h("h3", { text: "VK Видео" }),
      h("div", { class: "grid cols-4" }, h("label", { class: "check" }, f.vkOn, "Включено"), field("ID сообщества", f.vkGroup), field("ID альбома", f.vkAlbum), h("label", { class: "check" }, f.vkWall, "Пост на стену при публикации")),
      h("h3", { text: "YouTube" }), h("label", { class: "check" }, f.ytOn, "Включено"),
      h("h3", { text: "Telegram" }),
      h("div", { class: "grid cols-3" }, h("label", { class: "check" }, f.tgOn, "Включено"), field("Канал", f.tgChat), field("Чат для черновиков", f.tgReview, "Узнать свой id: напишите боту @userinfobot")),
      h("h3", { text: "Пакет для ручной загрузки" }), h("label", { class: "check" }, f.packOn, "Готовить папку для Rutube, Дзена, VK Клипов и OK")),
    h("div", { class: "actions", style: "margin-top:14px" }, save),
  );

  // ключи
  const SECRETS = [
    ["VK_ACCESS_TOKEN", "Токен VK"], ["VK_CLIENT_ID", "ID приложения VK ID"], ["TELEGRAM_BOT_TOKEN", "Токен Telegram-бота"],
    ["YOUTUBE_API_KEY", "Ключ YouTube Data API (поиск)"], ["ANTHROPIC_API_KEY", "Ключ Anthropic API"],
  ];
  const secretRows = SECRETS.map(([name, label]) => {
    const inp = h("input", { type: "password", placeholder: status.secrets[name] ? "задан — введите, чтобы заменить" : "не задан", autocomplete: "off" });
    const btn = h("button", { class: "btn ghost small", type: "button" }, "Сохранить");
    btn.addEventListener("click", async () => {
      try { await api("/api/settings/secret", { json: { name, value: inp.value } }); inp.value = ""; toast(`${label}: сохранено`); route(); }
      catch (e) { toast(e.message, true); }
    });
    return h("div", { class: "grid cols-2", style: "align-items:end" }, field(label, inp), h("div", { class: "actions" }, btn, h("span", { class: `badge ${status.secrets[name] ? "ok" : ""}`, text: status.secrets[name] ? "Задан" : "Нет" })));
  });
  const vkBtn = h("button", { class: "btn", type: "button" }, "Войти через VK ID");
  vkBtn.addEventListener("click", async () => {
    try {
      const r = await api("/api/auth/vk/start");
      if (r.error) return toast(r.error, true);
      window.open(r.url, "_blank", "noopener");
      if (r.legacy_url) legacy.replaceChildren("Если VK ID не пускает: ", h("a", { href: r.legacy_url, target: "_blank", rel: "noopener", text: "старый способ получения токена" }), " — скопируйте access_token из адресной строки в поле «Токен VK».");
    } catch (e) { toast(e.message, true); }
  });
  const legacy = h("p", { class: "note" });
  const ytBtn = h("button", { class: "btn", type: "button" }, "Войти в YouTube");
  ytBtn.addEventListener("click", async () => {
    try { const r = await api("/api/auth/youtube/start"); r.started ? toast(r.message || "Откройте вкладку браузера") : toast(r.error, true); }
    catch (e) { toast(e.message, true); }
  });
  // подключение к Claude как MCP
  const integ = await api("/api/integrations").catch(() => null);
  if (integ) {
    const connect = (target, label) => {
      const b = h("button", { class: "btn ghost", type: "button" }, label);
      b.addEventListener("click", async () => {
        const done = busy(b, "Подключаю…");
        try { const r = await api("/api/integrations/claude", { json: { target } }); toast(r.message, !r.ok); }
        catch (e) { toast(e.message, true); } finally { done(); }
      });
      return b;
    };
    box.append(h("h2", { text: "Подключение к Claude" }), h("div", { class: "panel grid" },
      h("p", { class: "note", text: "Clip Studio работает как MCP-сервер: Claude Code и Claude Desktop могут смотреть кадры, читать транскрипт, резать и публиковать клипы. Юридический шлюз действует и там." }),
      h("div", { class: "actions" },
        h("span", { class: `badge ${integ.claude_code.registered ? "ok" : ""}`, text: `Claude Code: ${integ.claude_code.message}` })),
      h("div", { class: "actions" }, connect("code", "Подключить к Claude Code"), connect("desktop", "Подключить к Claude Desktop"),
        connect("ffmpeg", "Добавить ffmpeg-mcp-lite")),
      h("details", {}, h("summary", { class: "note", text: "Конфиг вручную (для других MCP-клиентов)" }), h("pre", { class: "log", text: integ.mcp_json })),
      h("p", { class: "note" }, "Подробно — в ", h("a", { href: "/help/manual.html#claude", target: "_blank", rel: "noopener", text: "методичке" }), ".")));
  }

  box.append(h("h2", { text: "Ключи и вход" }), h("div", { class: "panel grid" }, ...secretRows,
    h("div", { class: "actions" }, vkBtn, ytBtn), legacy,
    status.youtube_auth && status.youtube_auth.error ? h("p", { class: "risk", text: `YouTube: ${status.youtube_auth.error}` }) : null));
}

// ================= ГАЙД =================
function inlineMd(text) {
  const frag = document.createDocumentFragment();
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)\s]+\))|(https?:\/\/[^\s)<]+)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    const t = m[0];
    if (m[1]) frag.append(h("code", { text: t.slice(1, -1) }));
    else if (m[2]) frag.append(h("strong", {}, inlineMd(t.slice(2, -2))));
    else if (m[3]) frag.append(h("em", {}, inlineMd(t.slice(1, -1))));
    else if (m[4]) {
      const mm = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(t);
      const safe = /^(https?:|#|\/)/.test(mm[2]) ? mm[2] : "#";
      frag.append(h("a", { href: safe, target: safe.startsWith("http") ? "_blank" : undefined, rel: "noopener" }, inlineMd(mm[1])));
    } else frag.append(h("a", { href: t, target: "_blank", rel: "noopener", text: t }));
    last = m.index + t.length;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}

function renderMd(md) {
  const root = h("div", { class: "md" });
  const lines = md.replace(/\r\n/g, "\n").split("\n");
  let i = 0, slug = 0;
  const toc = [];
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      root.append(h("pre", {}, h("code", { text: buf.join("\n") })));
      continue;
    }
    const hm = /^(#{1,4})\s+(.*)$/.exec(line);
    if (hm) {
      const id = `s${++slug}`;
      root.append(h(`h${hm[1].length}`, { id }, inlineMd(hm[2])));
      if (hm[1].length === 2) toc.push([id, hm[2]]);
      i++; continue;
    }
    if (/^\|/.test(line) && i + 1 < lines.length && /^\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const cells = (l) => l.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const body = [];
      while (i < lines.length && /^\|/.test(lines[i])) body.push(cells(lines[i++]));
      root.append(h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, inlineMd(c))))),
        h("tbody", {}, body.map((r) => h("tr", {}, r.map((c) => h("td", {}, inlineMd(c)))))))));
      continue;
    }
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
      root.append(h("blockquote", {}, inlineMd(buf.join(" "))));
      continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const list = h(ordered ? "ol" : "ul");
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        let item = lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, "");
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*]|\d+\.)\s+/.test(lines[i])) item += " " + lines[i++].trim();
        list.append(h("li", {}, inlineMd(item)));
      }
      root.append(list);
      continue;
    }
    if (/^---+$/.test(line.trim())) { root.append(h("hr")); i++; continue; }
    if (!line.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|\||>|\s*([-*]|\d+\.)\s)/.test(lines[i])) buf.push(lines[i++]);
    root.append(h("p", {}, inlineMd(buf.join(" "))));
  }
  return { root, toc };
}

async function viewGuide(box) {
  const { markdown } = await api("/api/guide");
  const { root, toc } = renderMd(markdown);
  box.append(h("div", { class: "toc" }, toc.map(([id, t]) => h("button", { class: "pick", type: "button", onclick: () => document.getElementById(id).scrollIntoView({ behavior: "smooth" }) }, t))), root);
}

// ---------- старт ----------
loadStatus().then(route);
setInterval(loadStatus, 30000);
