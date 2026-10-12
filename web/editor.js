/* Clip Studio — монтажный стол: плеер, таймлайн с раскадровкой и волной, транскрипт, метки входа/выхода.
   Использует общие помощники из app.js (h, api, toast, busy, fmtTime, fmtSec, select, field, dialog, state). */
"use strict";

const FPS_STEP = 1 / 30;

function fmtTc(s) {
  s = Math.max(0, Number(s) || 0);
  const m = Math.floor(s / 60), sec = s - m * 60;
  return `${m}:${sec.toFixed(2).padStart(5, "0")}`;
}

async function viewEditor(box, arg) {
  await refreshLists();
  const params = new URLSearchParams(location.hash.split("?")[1] || "");
  const playable = state.videos.filter((v) => v.media_url && v.status !== "downloading");
  const vid = Number(arg) || (playable[0] && playable[0].id);
  box.classList.add("wide");
  box.append(h("h1", { text: "Монтаж" }));
  if (!vid) {
    box.append(h("div", { class: "empty" }, "Сначала добавьте видео в разделе ", h("a", { href: "#videos", text: "«Видео»" }), "."));
    return;
  }
  const video = state.videos.find((v) => v.id === vid);
  if (!video) { box.append(h("p", { class: "risk", text: "Видео не найдено" })); return; }

  const picker = select("video", playable.map((v) => [v.id, `#${v.id} ${v.title}`]), vid);
  picker.addEventListener("change", () => { location.hash = `edit/${picker.value}`; });
  box.append(h("p", { class: "lede", text: "Выделите отрезок на таймлайне или в транскрипте и вырежьте клип. Горячие клавиши — в подсказке под плеером." }),
    h("div", { class: "actions", style: "margin-bottom:14px" }, field("Видео", picker)));

  // ---------- состояние редактора ----------
  const ed = { vid, dur: Number(video.duration) || 0, inT: null, outT: null, zoom: 1, cands: [], segs: [], stopAt: null };
  const selFromCand = Number(params.get("cand")) || 0;

  // ---------- плеер ----------
  const player = h("video", { src: video.media_url, preload: "auto", playsinline: true, class: "ed-video" });
  const timeLbl = h("span", { class: "ed-time", text: "0:00.00" });
  const speed = select("speed", [["0.5", "0.5×"], ["1", "1×"], ["1.5", "1.5×"], ["2", "2×"]], "1");
  speed.addEventListener("change", () => { player.playbackRate = Number(speed.value); });
  const seek = (t) => { player.currentTime = Math.max(0, Math.min(ed.dur || player.duration || 0, t)); };
  const nudge = (d) => h("button", { class: "btn ghost small", type: "button", onclick: () => seek(player.currentTime + d) }, d > 0 ? `+${d}` : `${d}`);
  const playBtn = h("button", { class: "btn small", type: "button", onclick: () => (player.paused ? player.play() : player.pause()) }, "Пуск");
  player.addEventListener("play", () => { playBtn.textContent = "Пауза"; });
  player.addEventListener("pause", () => { playBtn.textContent = "Пуск"; });
  player.addEventListener("loadedmetadata", () => { if (!ed.dur) ed.dur = player.duration; drawAll(); });

  // ---------- таймлайн ----------
  const strip = h("img", { class: "tl-strip", alt: "", src: `/api/videos/${vid}/filmstrip?n=48`, draggable: "false" });
  const wave = h("canvas", { class: "tl-wave", height: 64 });
  const regions = h("div", { class: "tl-regions" });
  const selEl = h("div", { class: "tl-sel", hidden: true }, h("i", { class: "tl-handle l" }), h("i", { class: "tl-handle r" }));
  const head = h("div", { class: "tl-head" });
  const inner = h("div", { class: "tl-inner" }, strip, wave, regions, selEl, head);
  const ruler = h("div", { class: "tl-ruler" });
  const tl = h("div", { class: "tl", tabindex: "0", "aria-label": "Таймлайн" }, h("div", { class: "tl-scroll" }, ruler, inner));
  const zoom = h("input", { type: "range", min: 1, max: 30, value: 1, "aria-label": "Масштаб" });
  zoom.addEventListener("input", () => { ed.zoom = Number(zoom.value); drawAll(); centerOn(player.currentTime); });

  let peaks = [];
  api(`/api/videos/${vid}/waveform?points=2000`).then((w) => { peaks = w.peaks || []; drawWave(); }).catch(() => {});

  const pct = (t) => (ed.dur ? (t / ed.dur) * 100 : 0);
  const timeAt = (clientX) => {
    const r = inner.getBoundingClientRect();
    return Math.max(0, Math.min(ed.dur, ((clientX - r.left) / r.width) * ed.dur));
  };
  function centerOn(t) {
    const sc = tl.querySelector(".tl-scroll");
    const x = (pct(t) / 100) * inner.offsetWidth - sc.clientWidth / 2;
    sc.scrollLeft = Math.max(0, x);
  }
  function drawWave() {
    const w = Math.min(inner.offsetWidth || 800, 8000);
    wave.width = w;
    const ctx = wave.getContext("2d");
    ctx.clearRect(0, 0, w, wave.height);
    if (!peaks.length) return;
    ctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--ink-3") || "#888";
    const mid = wave.height / 2;
    for (let x = 0; x < w; x++) {
      const i0 = Math.floor((x / w) * peaks.length), i1 = Math.max(i0 + 1, Math.floor(((x + 1) / w) * peaks.length));
      let m = 0;
      for (let i = i0; i < i1 && i < peaks.length; i++) m = Math.max(m, peaks[i]);
      const hgt = Math.max(1, Math.sqrt(m) * (wave.height - 4));
      ctx.fillRect(x, mid - hgt / 2, 1, hgt);
    }
  }
  function drawRuler() {
    ruler.replaceChildren();
    if (!ed.dur) return;
    const visible = ed.dur / ed.zoom;
    const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800];
    const step = steps.find((s) => visible / s <= 12) || 3600;
    for (let t = 0; t <= ed.dur; t += step) ruler.append(h("span", { style: `left:${pct(t)}%`, text: fmtTime(t) }));
  }
  function drawSel() {
    if (ed.inT === null || ed.outT === null || ed.outT <= ed.inT) { selEl.hidden = true; } else {
      selEl.hidden = false;
      selEl.style.left = `${pct(ed.inT)}%`;
      selEl.style.width = `${pct(ed.outT - ed.inT)}%`;
    }
    inIn.value = ed.inT === null ? "" : ed.inT.toFixed(2);
    outIn.value = ed.outT === null ? "" : ed.outT.toFixed(2);
    durLbl.textContent = ed.inT !== null && ed.outT !== null && ed.outT > ed.inT ? fmtSec(ed.outT - ed.inT) : "—";
    cutBtn.disabled = !(ed.inT !== null && ed.outT !== null && ed.outT - ed.inT >= 0.5);
    highlightTranscript();
  }
  function drawRegions() {
    regions.replaceChildren(...ed.cands.map((c) => h("i", {
      class: `tl-region ${c.status}`, style: `left:${pct(c.start)}%;width:${pct(c.end - c.start)}%`,
      title: `${c.title || "Кандидат"} (${(c.total || 0).toFixed(1)})`,
    })));
  }
  function drawAll() {
    inner.style.width = `${ed.zoom * 100}%`;
    drawRuler(); drawRegions(); drawSel(); drawWave();
    head.style.left = `${pct(player.currentTime)}%`;
  }
  window.addEventListener("resize", drawWave, { passive: true });

  // мышь: клик — переход, протяжка — выделение, ручки — правка границ
  let drag = null;
  inner.addEventListener("pointerdown", (e) => {
    const t = timeAt(e.clientX);
    if (e.target.classList.contains("tl-handle")) drag = { mode: e.target.classList.contains("l") ? "in" : "out" };
    else drag = { mode: "new", from: t, moved: false };
    inner.setPointerCapture(e.pointerId);
  });
  inner.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const t = timeAt(e.clientX);
    if (drag.mode === "in") ed.inT = Math.min(t, (ed.outT ?? ed.dur) - 0.1);
    else if (drag.mode === "out") ed.outT = Math.max(t, (ed.inT ?? 0) + 0.1);
    else if (Math.abs(t - drag.from) > ed.dur / 2000) { drag.moved = true; ed.inT = Math.min(drag.from, t); ed.outT = Math.max(drag.from, t); }
    drawSel();
  });
  inner.addEventListener("pointerup", (e) => {
    if (drag && drag.mode === "new" && !drag.moved) seek(timeAt(e.clientX));
    drag = null;
  });

  // ---------- выделение ----------
  const inIn = h("input", { type: "number", step: "0.01", "aria-label": "Вход, сек" });
  const outIn = h("input", { type: "number", step: "0.01", "aria-label": "Выход, сек" });
  const durLbl = h("b", { text: "—" });
  inIn.addEventListener("change", () => { ed.inT = inIn.value === "" ? null : Number(inIn.value); drawSel(); });
  outIn.addEventListener("change", () => { ed.outT = outIn.value === "" ? null : Number(outIn.value); drawSel(); });
  const setIn = () => { ed.inT = player.currentTime; if (ed.outT !== null && ed.outT <= ed.inT) ed.outT = null; drawSel(); };
  const setOut = () => { ed.outT = player.currentTime; if (ed.inT !== null && ed.inT >= ed.outT) ed.inT = null; drawSel(); };
  const playSel = () => { if (ed.inT === null) return; seek(ed.inT); ed.stopAt = ed.outT; player.play(); };

  // ---------- параметры клипа ----------
  const title = h("input", { type: "text", placeholder: "Название клипа" });
  const hook = h("input", { type: "text", placeholder: "Хук до 5 слов, например: ПЛАН БЫЛ ИДЕАЛЬНЫЙ" });
  const fmt = select("format", [["vertical", "9:16"], ["horizontal", "16:9"], ["square", "1:1"]], "vertical");
  const reframe = select("reframe", [["blur", "Размытый фон"], ["center", "По центру"], ["face", "По лицу"]], "blur");
  const subs = select("subs", [["karaoke", "Подсветка слов"], ["plain", "Обычные"], ["none", "Без субтитров"]], "karaoke");
  const jump = h("input", { type: "checkbox" });
  const cutBtn = h("button", { class: "btn marker", type: "button", disabled: true }, "Вырезать клип");
  const saveCand = h("button", { class: "btn ghost", type: "button", hidden: true }, "Сохранить время кандидата");
  const lookBtn = h("button", { class: "btn ghost", type: "button" }, "Кадры выделения");
  const result = h("div", { class: "ed-result" });
  let activeCand = null;

  cutBtn.addEventListener("click", async () => {
    const done = busy(cutBtn, "Отправляю…");
    try {
      const { job } = await api(`/api/videos/${vid}/clip`, { json: {
        start: ed.inT, end: ed.outT, title: title.value, hook: hook.value.toUpperCase(), format: fmt.value,
        reframe: reframe.value, subtitle_style: subs.value, jumpcut: jump.checked,
      } });
      watchJob(job.id);
    } catch (e) { toast(e.message, true); } finally { done(); }
  });
  saveCand.addEventListener("click", async () => {
    if (!activeCand) return;
    try {
      await api(`/api/candidates/${activeCand.id}`, { json: { start: ed.inT, end: ed.outT, title: title.value || undefined, hook: hook.value || undefined } });
      toast("Время кандидата сохранено");
      loadCands();
    } catch (e) { toast(e.message, true); }
  });
  lookBtn.addEventListener("click", () => {
    if (ed.inT === null || ed.outT === null) return toast("Сначала выделите отрезок", true);
    const src = `/api/videos/${vid}/frames?start=${ed.inT.toFixed(2)}&end=${ed.outT.toFixed(2)}&n=9`;
    dialog((close) => [h("h3", { text: `Кадры ${fmtTc(ed.inT)}–${fmtTc(ed.outT)}` }), h("img", { src, alt: "Кадры выделенного отрезка", class: "ed-sheet" }),
      h("div", { class: "actions" }, h("button", { class: "btn ghost", onclick: close }, "Закрыть"))]);
  });

  async function watchJob(jobId) {
    result.replaceChildren(h("div", { class: "progress" }, h("i", { style: "width:10%" })), h("p", { class: "note", text: "Режу клип…" }));
    const tick = async () => {
      const { job, clips } = await api(`/api/jobs/${jobId}`);
      result.querySelector(".progress i").style.width = `${Math.max(10, Math.round((job.progress || 0) * 100))}%`;
      result.querySelector(".note").textContent = job.stage || "";
      if (job.status === "done" && clips.length) {
        clearInterval(timer);
        result.replaceChildren(h("p", { class: "note", text: "Готово:" }), h("div", { class: "clips" }, clips.map(clipTile)));
        loadClips();
      } else if (["error", "cancelled", "review"].includes(job.status)) {
        clearInterval(timer);
        result.replaceChildren(h("p", { class: "risk", text: job.error || job.stage || "Не получилось" }));
      }
    };
    const timer = setInterval(() => tick().catch(() => {}), 1500);
    state.timer = timer;
  }

  // ---------- транскрипт ----------
  const tsearch = h("input", { type: "search", placeholder: "Поиск по тексту" });
  const tlist = h("div", { class: "ed-transcript", tabindex: "0" });
  const tbox = h("aside", { class: "ed-side" }, h("div", { class: "actions" }, tsearch), tlist);
  async function loadTranscript() {
    try {
      const res = await fetch(`/api/videos/${vid}/transcript`);
      if (!res.ok) throw new Error("none");
      const t = await res.json();
      ed.segs = t.segments || [];
      drawTranscript();
    } catch (_) {
      const btn = h("button", { class: "btn", type: "button" }, "Распознать речь");
      btn.addEventListener("click", async () => {
        const done = busy(btn, "Запускаю…");
        try { await api(`/api/videos/${vid}/transcribe`, { method: "POST" }); pollTranscript(); } catch (e) { toast(e.message, true); done(); }
      });
      tlist.replaceChildren(h("p", { class: "note", text: "Транскрипта ещё нет. Он нужен для субтитров и поиска по словам." }), btn);
    }
  }
  function pollTranscript() {
    const t = setInterval(async () => {
      const { videos } = await api("/api/videos");
      const v = videos.find((x) => x.id === vid);
      tlist.replaceChildren(h("p", { class: "note", text: (v && v.status_message) || "Распознаю речь…" }));
      if (v && v.status !== "transcribing") { clearInterval(t); loadTranscript(); }
    }, 2000);
  }
  function drawTranscript() {
    const q = tsearch.value.trim().toLowerCase();
    const rows = ed.segs.filter((s) => !q || s.text.toLowerCase().includes(q));
    tlist.replaceChildren(...rows.slice(0, 1500).map((s) => {
      const row = h("div", { class: "seg", "data-start": s.start, "data-end": s.end },
        h("button", { class: "seg-t", type: "button", title: "Выделить фразу", onclick: () => { ed.inT = s.start; ed.outT = s.end; drawSel(); seek(s.start); } }, fmtTime(s.start)),
        h("span", { class: "seg-w" }, (s.words && s.words.length ? s.words : [{ word: s.text, start: s.start, end: s.end }]).map((w) =>
          h("span", { class: "w", "data-start": w.start, onclick: (e) => {
            if (e.shiftKey && ed.inT !== null) { ed.outT = Math.max(w.end, ed.inT + 0.1); drawSel(); } else seek(w.start);
          } }, w.word + " "))));
      return row;
    }), rows.length ? null : h("p", { class: "note", text: q ? "Ничего не найдено" : "Пусто" }));
    highlightTranscript();
  }
  tsearch.addEventListener("input", drawTranscript);
  let lastSeg = null;
  function highlightTranscript() {
    tlist.querySelectorAll(".seg").forEach((el) => {
      const a = Number(el.dataset.start), b = Number(el.dataset.end);
      el.classList.toggle("in-sel", ed.inT !== null && ed.outT !== null && b > ed.inT && a < ed.outT);
    });
  }
  function followTranscript(t) {
    const el = [...tlist.querySelectorAll(".seg")].find((x) => Number(x.dataset.start) <= t && Number(x.dataset.end) >= t);
    if (el && el !== lastSeg) {
      if (lastSeg) lastSeg.classList.remove("now");
      el.classList.add("now");
      lastSeg = el;
      if (!player.paused) el.scrollIntoView({ block: "nearest" });
    }
  }

  player.addEventListener("timeupdate", () => {
    const t = player.currentTime;
    timeLbl.textContent = `${fmtTc(t)} / ${fmtTime(ed.dur)}`;
    head.style.left = `${pct(t)}%`;
    if (ed.stopAt !== null && t >= ed.stopAt) { player.pause(); ed.stopAt = null; }
    followTranscript(t);
  });

  // ---------- кандидаты и клипы этого видео ----------
  const candBox = h("div", { class: "ed-cands" });
  const clipsBox = h("div");
  async function loadCands() {
    const { candidates } = await api(`/api/videos/${vid}/candidates`);
    ed.cands = candidates;
    drawRegions();
    candBox.replaceChildren(...(candidates.length ? candidates.slice(0, 60).map((c) => h("button", {
      type: "button", class: `pick${activeCand && activeCand.id === c.id ? " on" : ""}`, title: c.quote || "",
      onclick: () => pickCand(c),
    }, `${fmtTime(c.start)} · ${(c.total || 0).toFixed(1)} · ${c.title || "без названия"}`)) : [h("span", { class: "note", text: "Кандидатов нет — найдите их в Студии или выделите отрезок вручную." })]));
    if (selFromCand && !activeCand) {
      const c = candidates.find((x) => x.id === selFromCand);
      if (c) pickCand(c);
    }
  }
  function pickCand(c) {
    activeCand = c;
    ed.inT = c.start; ed.outT = c.end;
    title.value = c.title || ""; hook.value = c.hook || "";
    saveCand.hidden = false;
    drawSel(); seek(c.start);
    if (ed.zoom > 1) centerOn(c.start);
    candBox.querySelectorAll(".pick").forEach((b) => b.classList.remove("on"));
  }
  async function loadClips() {
    const { clips } = await api(`/api/videos/${vid}/clips`);
    clipsBox.replaceChildren(clips.length ? h("div", { class: "clips" }, clips.slice(0, 18).map(clipTile)) : h("p", { class: "note", text: "Клипов из этого видео пока нет." }));
  }

  // ---------- клавиатура ----------
  const onKey = (e) => {
    if (!document.body.contains(player)) { document.removeEventListener("keydown", onKey); return; }
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    const k = e.key.toLowerCase();
    if (k === " ") { e.preventDefault(); player.paused ? player.play() : player.pause(); }
    else if (k === "arrowleft") { e.preventDefault(); seek(player.currentTime - (e.shiftKey ? 5 : 1)); }
    else if (k === "arrowright") { e.preventDefault(); seek(player.currentTime + (e.shiftKey ? 5 : 1)); }
    else if (k === "," ) seek(player.currentTime - FPS_STEP);
    else if (k === ".") seek(player.currentTime + FPS_STEP);
    else if (k === "i" || k === "ш") setIn();
    else if (k === "o" || k === "щ") setOut();
    else if (k === "p" || k === "з") playSel();
    else if (k === "enter" && !cutBtn.disabled) cutBtn.click();
  };
  document.addEventListener("keydown", onKey);

  // ---------- раскладка ----------
  box.append(
    h("div", { class: "ed-grid" },
      h("section", { class: "ed-main" },
        player,
        h("div", { class: "ed-bar" }, playBtn, nudge(-5), nudge(-1), nudge(1), nudge(5), timeLbl, h("span", { class: "spacer" }), speed),
        tl,
        h("div", { class: "ed-bar" }, h("span", { class: "note", text: "Масштаб" }), zoom,
          h("span", { class: "note", text: "Протяните мышью по таймлайну, чтобы выделить отрезок" })),
        h("p", { class: "note ed-keys", text: "Пробел — пуск/пауза · ←/→ — 1 с (Shift — 5 с) · , . — кадр · I / O — вход / выход · P — проиграть выделение · Enter — вырезать · Shift+клик по слову — конец выделения" })),
      tbox),
    h("section", { class: "panel grid", style: "margin-top:16px" },
      h("div", { class: "grid cols-4" },
        field("Вход, с", inIn), field("Выход, с", outIn),
        h("div", { class: "field" }, h("span", { text: "Длительность" }), durLbl),
        h("div", { class: "actions", style: "align-self:end" },
          h("button", { class: "btn ghost small", type: "button", onclick: setIn }, "Вход здесь (I)"),
          h("button", { class: "btn ghost small", type: "button", onclick: setOut }, "Выход здесь (O)"),
          h("button", { class: "btn ghost small", type: "button", onclick: playSel }, "Проиграть (P)"))),
      h("div", { class: "grid cols-2" }, field("Название", title), field("Хук на экране", hook)),
      h("div", { class: "grid cols-4" }, field("Формат", fmt), field("Кадрирование", reframe), field("Субтитры", subs),
        h("label", { class: "check", title: "Паузы длиннее 0,6 с внутри клипа вырезаются по таймкодам слов" }, jump, "Вырезать паузы")),
      h("div", { class: "actions" }, cutBtn, saveCand, lookBtn),
      result),
    h("h2", { text: "Кандидаты этого видео" }), candBox,
    h("h2", { text: "Клипы из этого видео" }), clipsBox,
  );
  drawAll();
  loadTranscript();
  loadCands();
  loadClips();
}

VIEWS.edit = viewEditor;
