/* Salon de blind test : connexion WebSocket, réglages (hôte), manches, réponses, résultats.
   Le serveur décide de tout (musiques, chrono, points) ; ici on affiche et on joue l'extrait.
   CSP stricte : aucun style ni handler inline, tout passe par le DOM. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const CSRF = $('meta[name="csrf"]').content;
const CODE = $("#room").dataset.code;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = (k, v) => { try { if (v === undefined) return JSON.parse(localStorage.getItem("bt:" + k)); localStorage.setItem("bt:" + k, JSON.stringify(v)); } catch { return null; } };
const KIND = { OP: "Opening", ED: "Ending", IN: "Insert", OST: "OST" };
const VERSION = { tv: "version TV", full: "version complète" };

let S = null;              // dernier état complet reçu
let ws = null, pingTimer = null, kicked = false;
let round = null;          // manche en cours (données de round_load)
let result = null;         // résultat de la dernière manche / fin de partie
let picked = null;         // ma réponse (id d'anime) pour la manche en cours
let loadedIdx = -1, pendingPlay = null;
let presets = { builtin: {}, mine: [] };
const audio = $("#audio");
let clipUrl = null;

/* ------------------------------------------------------------------ utilitaires */
let toastTimer;
function toast(msg, err = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", !!err);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3800);
}
const isHost = () => S && S.host === S.me;
const inGame = () => S && ["preparing", "loading", "playing", "reveal"].includes(S.phase);
function send(m) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(m)); }

async function api(path, data) {
  const r = await fetch(path, data === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json", "X-CSRF": CSRF }, body: JSON.stringify(data),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `Erreur ${r.status}`);
  return j;
}

/* ------------------------------------------------------------------ connexion */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws/${CODE}`);
  ws.onopen = () => { clearInterval(pingTimer); pingTimer = setInterval(() => send({ t: "ping" }), 25000); };
  ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  ws.onclose = (e) => {
    clearInterval(pingTimer);
    if (kicked) return;
    if (e.code === 4401) { location.href = `/login?next=/room/${CODE}`; return; }
    if (e.code === 4404) { location.reload(); return; }
    toast("Connexion perdue, je me reconnecte…", true);
    setTimeout(connect, 1500);
  };
}

function handle(m) {
  switch (m.t) {
    case "state":
      S = m;
      if (m.phase === "lobby") { round = null; loadedIdx = -1; picked = null; }
      if (m.phase === "reveal" || m.phase === "finished") result = m.result || result;
      if (m.round && ["loading", "playing", "reveal"].includes(m.phase) && (!round || round.idx !== m.round.idx)) {
        // arrivée en cours de partie (ou reconnexion) : on rattrape la manche
        round = m.round;
        picked = m.round.my_answer;
        if (m.phase !== "reveal") prepareClip(round, m.phase === "playing" ? m.round.remaining : null);
      }
      render();
      break;
    case "round_load":
      round = m; picked = null; result = null;
      S.phase = "loading";
      if (S.round) S.round.answered = [];
      prepareClip(m);
      render();
      break;
    case "round_play":
      S.phase = "playing";
      if (round) round.answered = [];
      startPlayback(m.seconds, 0);
      render();
      break;
    case "answered":
      if (round) { round.answered = round.answered || []; if (!round.answered.includes(m.id)) round.answered.push(m.id); }
      renderPlayers();
      renderStatus();
      break;
    case "round_reveal":
      S.phase = "reveal";
      result = m.result;
      S.players = m.players;
      stopTimer();
      render();
      break;
    case "game_end":
      S.phase = "finished";
      result = m;
      stopTimer();
      render();
      break;
    case "toast": toast(m.msg, m.err); break;
    case "error": toast(m.msg, true); break;
    case "kicked":
      kicked = true;
      toast("L'hôte t'a retiré du salon", true);
      setTimeout(() => { location.href = "/"; }, 1800);
      break;
    default: break;
  }
}

/* ------------------------------------------------------------------ audio */
let actx = null, analyser = null, vizData = null, vizRaf = null;
function setupAnalyser() {
  if (analyser || !window.AudioContext) return;
  try {
    actx = new AudioContext();
    const src = actx.createMediaElementSource(audio);
    analyser = actx.createAnalyser();
    analyser.fftSize = 128;
    src.connect(analyser);
    analyser.connect(actx.destination);
    vizData = new Uint8Array(analyser.frequencyBinCount);
  } catch { analyser = null; }
}

// iPhone / Safari : le son n'est autorisé qu'après un geste → on débloque au 1er clic
document.addEventListener("click", () => {
  setupAnalyser();
  if (actx && actx.state === "suspended") actx.resume();
}, { capture: true });

audio.volume = (store("vol") ?? 80) / 100;
$("#vol").value = store("vol") ?? 80;
$("#vol").addEventListener("input", (e) => { audio.volume = e.target.value / 100; store("vol", +e.target.value); });

async function prepareClip(r, remaining = null) {
  const idx = r.idx;
  audio.pause();
  if (clipUrl) { URL.revokeObjectURL(clipUrl); clipUrl = null; }
  try {
    const resp = await fetch(r.clip);
    if (!resp.ok) throw new Error("extrait introuvable");
    const blob = await resp.blob();
    if (!round || round.idx !== idx) return;
    clipUrl = URL.createObjectURL(blob);
    audio.src = clipUrl;
    await new Promise((ok) => {
      const done = () => { audio.removeEventListener("canplaythrough", done); ok(); };
      audio.addEventListener("canplaythrough", done);
      audio.load();
      setTimeout(done, 4000);
    });
  } catch (e) {
    toast(`Extrait non chargé : ${e.message}`, true);
  }
  if (!round || round.idx !== idx) return;
  loadedIdx = idx;
  send({ t: "loaded", idx });
  if (remaining != null) startPlayback(remaining, r.seconds - remaining);
  else if (pendingPlay && pendingPlay.idx === idx) {
    startPlayback(pendingPlay.seconds - (performance.now() - pendingPlay.at) / 1000,
      (performance.now() - pendingPlay.at) / 1000);
  }
  pendingPlay = null;
}

function startPlayback(seconds, offset) {
  startTimer(seconds, round ? round.seconds : seconds);
  if (!round || loadedIdx !== round.idx) { // pas encore chargé : on lancera dès que possible
    pendingPlay = { idx: round && round.idx, seconds, at: performance.now() };
    return;
  }
  try { audio.currentTime = Math.max(0, offset || 0); } catch { /* pas grave */ }
  if (actx && actx.state === "suspended") actx.resume();
  audio.play().catch(() => toast("Clique n'importe où pour activer le son", true));
  drawViz();
}

/* ------------------------------------------------------------------ chrono */
let timerRaf = null, timerEnd = 0, timerTotal = 20;
function startTimer(seconds, total) {
  timerEnd = performance.now() + seconds * 1000;
  timerTotal = total;
  cancelAnimationFrame(timerRaf);
  const tick = () => {
    const left = Math.max(0, (timerEnd - performance.now()) / 1000);
    const el = $("#timer");
    if (el) {
      const bar = $(".bar", el);
      const c = 2 * Math.PI * 60;
      bar.style.strokeDasharray = c;
      bar.style.strokeDashoffset = c * (1 - left / timerTotal);
      $("b", el).textContent = Math.ceil(left);
      el.classList.toggle("low", left <= 5);
    }
    if (left > 0 && S && S.phase === "playing") timerRaf = requestAnimationFrame(tick);
  };
  tick();
}
function stopTimer() { cancelAnimationFrame(timerRaf); }

function drawViz() {
  cancelAnimationFrame(vizRaf);
  const cv = $("#viz");
  if (!cv || !analyser) return;
  const ctx = cv.getContext("2d");
  const draw = () => {
    const w = cv.width = cv.clientWidth * devicePixelRatio;
    const h = cv.height = cv.clientHeight * devicePixelRatio;
    analyser.getByteFrequencyData(vizData);
    ctx.clearRect(0, 0, w, h);
    const n = 48, gap = 4 * devicePixelRatio, bw = (w - gap * (n - 1)) / n;
    for (let i = 0; i < n; i++) {
      const v = vizData[Math.floor(i * vizData.length / n / 1.3)] / 255;
      const bh = Math.max(3, v * h * 0.85);
      const g = ctx.createLinearGradient(0, h, 0, h - bh);
      g.addColorStop(0, "#ff5c8f55");
      g.addColorStop(1, "#8b7bffcc");
      ctx.fillStyle = g;
      ctx.fillRect(i * (bw + gap), h - bh, bw, bh);
    }
    if (!audio.paused && $("#viz") === cv) vizRaf = requestAnimationFrame(draw);
  };
  draw();
}
audio.addEventListener("play", drawViz);

/* ------------------------------------------------------------------ rendu */
function render() {
  if (!S) return;
  $("#room").classList.toggle("playing", inGame());
  $("#settings-col").hidden = inGame();
  renderPlayers();
  renderStage();
  if (!inGame()) renderSettings();
}

function renderPlayers() {
  if (!S) return;
  const answered = new Set((round && round.answered) || []);
  const gains = {};
  if (S.phase === "reveal" && result && result.results) for (const r of result.results) gains[r.id] = r.points;
  $("#players").innerHTML = S.players.map((p, i) => `
    <div class="pl${p.id === S.me ? " me" : ""}${p.connected ? "" : " off"}${S.phase === "playing" && answered.has(p.id) ? " done" : ""}">
      <span class="rank-badge rank-${i + 1}">${i + 1}</span>
      <img src="${esc(p.avatar)}" alt="" referrerpolicy="no-referrer">
      <div><div class="nm">${p.id === S.host ? '<span class="crown" title="Hôte">♛</span> ' : ""}${esc(p.name)}</div>
        <div class="sub">${p.correct} trouvée${p.correct > 1 ? "s" : ""}${p.streak > 1 ? ` · 🔥${p.streak}` : ""}</div></div>
      <div class="sc">${p.score}${gains[p.id] ? `<div class="gain${gains[p.id] < 0 ? " neg" : ""}">${gains[p.id] > 0 ? "+" : ""}${gains[p.id]}</div>` : ""}</div>
      ${isHost() && p.id !== S.me && !inGame() ? `<button class="icon-btn kick" data-kick="${esc(p.id)}" title="Retirer du salon">✕</button>` : ""}
    </div>`).join("");
}

$("#players").addEventListener("click", (e) => {
  const b = e.target.closest("[data-kick]");
  if (b && confirm("Retirer ce joueur du salon ?")) send({ t: "kick", id: b.dataset.kick });
});

function renderStage() {
  const st = $("#stage");
  const ph = S.phase;
  if (ph === "lobby") st.innerHTML = lobbyHTML();
  else if (ph === "preparing") st.innerHTML = `<div class="card code-box"><h2>🎧 Préparation des extraits…</h2>
      <p class="muted">${esc(S.notice || "")}</p></div>`;
  else if (ph === "loading" || ph === "playing") st.innerHTML = roundHTML();
  else if (ph === "reveal") st.innerHTML = revealHTML();
  else if (ph === "finished") st.innerHTML = endHTML();
  if (ph === "playing" || ph === "loading") {
    const left = (timerEnd - performance.now()) / 1000;
    if (ph === "playing" && left > 0) startTimer(left, round ? round.seconds : 20);
    if (!audio.paused) drawViz();
    bindAnswer();
  }
  if (ph === "reveal") animateNext();
}

function poolHTML() {
  const p = S.pool;
  if (!p) return "";
  const warn = [];
  if (p.no_list && p.no_list.length) warn.push(`Sans liste anime : ${p.no_list.map(esc).join(", ")} (ignoré${p.no_list.length > 1 ? "s" : ""} pour « tout le monde »)`);
  if (p.possible < S.settings.rounds) warn.push(`Seulement ${p.possible} manche${p.possible > 1 ? "s" : ""} possible${p.possible > 1 ? "s" : ""} avec ces réglages`);
  const kinds = Object.entries(p.by_kind).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(" · ") || "aucune";
  return `<div class="pool">
      <div class="stat"><b>${p.songs}</b><span>musiques possibles</span></div>
      <div class="stat"><b>${p.animes}</b><span>animes</span></div>
      <div class="stat"><b>${Math.min(p.possible, S.settings.rounds)}</b><span>manches</span></div>
      <div class="stat"><b>${S.players.length}</b><span>joueur${S.players.length > 1 ? "s" : ""}</span></div>
    </div>
    <p class="muted small">${kinds} · ${p.tv_ready} en version TV (les autres en version complète)</p>
    ${warn.map((w) => `<div class="warn">${w}</div>`).join("")}`;
}

function lobbyHTML() {
  const link = `${location.origin}/room/${CODE}`;
  return `<div class="card code-box">
      <div class="muted">Code du salon</div>
      <div class="code">${esc(CODE)}</div>
      <div class="share">
        <button class="btn small" data-copy="${esc(link)}">Copier le lien d'invitation</button>
        <button class="btn small ghost" data-copy="${esc(CODE)}">Copier le code</button>
      </div>
    </div>
    <div class="card">${poolHTML()}</div>
    <div class="card code-box">
      ${isHost()
        ? `<button class="btn accent big" data-act="start" ${S.pool && S.pool.possible ? "" : "disabled"}>▶ Lancer la partie</button>
           <p class="muted small">Tu es l'hôte : les réglages à droite, et c'est toi qui lances.</p>`
        : `<p class="muted">En attente de l'hôte… Les réglages s'affichent à droite.</p>`}
    </div>`;
}

function roundHTML() {
  const r = round || {};
  const answered = (r.answered || []).length;
  const hints = [r.hint_kind ? `<span class="hint">${esc(KIND[r.hint_kind] || r.hint_kind)}</span>` : "",
    r.hint_year ? `<span class="hint">${esc(r.hint_year)}</span>` : ""].join("");
  return `<div class="round-head">
      <span class="no">Manche ${r.idx + 1} / ${r.total}</span>${hints}
      <span class="grow"></span>
      ${isHost() ? `<button class="btn small ghost danger" data-act="stop">Arrêter la partie</button>` : ""}
    </div>
    <div class="listen">
      <canvas id="viz"></canvas>
      <div class="timer" id="timer"><svg viewBox="0 0 136 136"><circle class="track" cx="68" cy="68" r="60"/><circle class="bar" cx="68" cy="68" r="60"/></svg><b>${r.seconds || ""}</b></div>
    </div>
    <div class="status-line" id="status"></div>
    ${r.answer === "search" ? searchHTML() : choicesHTML(r)}
    <p class="muted small">${S.players.length > 1 ? `${answered}/${S.players.filter((p) => p.connected).length} ont répondu` : ""}</p>`;
}

function choicesHTML(r) {
  const cs = r.choices || [];
  const img = r.images;
  return `<div class="choices n${cs.length}${img ? " img" : ""}${picked != null ? " locked" : ""}" id="choices">
    ${cs.map((c, i) => `<button class="choice${picked === c.id ? " picked" : ""}" data-choice="${c.id}" ${picked != null ? "disabled" : ""}>
        ${img ? `<img src="${esc(c.cover)}" alt="" loading="eager">` : ""}
        <span class="key">${i + 1}</span>
        <span class="txt"><span class="t">${esc(c.title)}</span>${c.english ? `<br><span class="e">${esc(c.english)}</span>` : ""}</span>
      </button>`).join("")}
  </div>`;
}

let pickedCard = null;
function searchHTML() {
  if (picked != null && pickedCard) {
    return `<div class="my-pick">${pickedCard.cover ? `<img src="${esc(pickedCard.cover)}" alt="">` : ""}
      <div><div class="muted small">Ta réponse</div><b>${esc(pickedCard.title)}</b></div></div>`;
  }
  return `<div class="search-box">
      <input id="answer-q" placeholder="Tape le nom de l'anime…" autocomplete="off" ${S.phase !== "playing" ? "" : "autofocus"}>
      <div class="sugg" id="sugg" hidden></div>
    </div>`;
}

function renderStatus() {
  const el = $("#status");
  if (!el) return;
  if (S.phase === "loading") el.textContent = "Chargement de l'extrait…";
  else if (picked != null) el.textContent = "Réponse envoyée ✓ — on attend les autres";
  else el.textContent = "Écoute… quel est l'anime ?";
  const n = $(".stage > p.small");
  if (n && round && S.players.length > 1) {
    n.textContent = `${(round.answered || []).length}/${S.players.filter((p) => p.connected).length} ont répondu`;
  }
}

function choose(id, card) {
  if (picked != null || S.phase !== "playing" || !round) return;
  picked = id;
  pickedCard = card || null;
  send({ t: "answer", idx: round.idx, choice: id });
  if (round.answer === "search") renderStage();
  else {
    $$("#choices .choice").forEach((b) => { b.disabled = true; b.classList.toggle("picked", +b.dataset.choice === id); });
    $("#choices").classList.add("locked");
  }
  renderStatus();
}

let suggTimer = null, suggItems = [], suggSel = 0;
function bindAnswer() {
  renderStatus();
  const box = $("#choices");
  if (box) box.addEventListener("click", (e) => {
    const b = e.target.closest("[data-choice]");
    if (b) choose(+b.dataset.choice, (round.choices || []).find((c) => c.id === +b.dataset.choice));
  });
  const q = $("#answer-q");
  if (!q) return;
  if (S.phase === "playing") q.focus();
  q.addEventListener("input", () => {
    clearTimeout(suggTimer);
    suggTimer = setTimeout(async () => {
      const v = q.value.trim();
      if (v.length < 2) { $("#sugg").hidden = true; return; }
      try {
        const j = await api(`/api/search?q=${encodeURIComponent(v)}`);
        suggItems = j.results; suggSel = 0;
        renderSugg();
      } catch { /* réseau */ }
    }, 160);
  });
  q.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { suggSel = Math.min(suggSel + 1, suggItems.length - 1); renderSugg(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { suggSel = Math.max(suggSel - 1, 0); renderSugg(); e.preventDefault(); }
    else if (e.key === "Enter" && suggItems[suggSel]) { choose(suggItems[suggSel].id, suggItems[suggSel]); e.preventDefault(); }
  });
  $("#sugg").addEventListener("click", (e) => {
    const b = e.target.closest("[data-pick]");
    if (b) { const it = suggItems[+b.dataset.pick]; choose(it.id, it); }
  });
}

function renderSugg() {
  const box = $("#sugg");
  if (!box) return;
  box.hidden = !suggItems.length;
  box.innerHTML = suggItems.map((a, i) => `<button class="${i === suggSel ? "sel" : ""}" data-pick="${i}">
      ${a.cover && round && round.images !== false ? `<img src="${esc(a.cover)}" alt="">` : ""}
      <span><b>${esc(a.title)}</b>${a.english ? `<br><span class="muted small">${esc(a.english)}</span>` : ""}${a.year ? ` <span class="muted small">· ${a.year}</span>` : ""}</span>
    </button>`).join("");
}

document.addEventListener("keydown", (e) => {
  if (!S || S.phase !== "playing" || e.target.closest("input, select, textarea")) return;
  const n = parseInt(e.key, 10);
  if (n >= 1 && round && round.choices && round.choices[n - 1]) choose(round.choices[n - 1].id, round.choices[n - 1]);
});

function revealHTML() {
  const r = result;
  if (!r) return "";
  const a = r.anime, s = r.song;
  const mine = (r.results || []).find((x) => x.id === S.me);
  const diff = s.difficulty;
  const label = diff < 25 ? "très connue" : diff < 50 ? "connue" : diff < 75 ? "pointue" : "très pointue";
  return `<div class="round-head"><span class="no">Manche ${r.idx + 1} / ${r.total}</span>
      <span class="grow"></span>
      ${isHost() ? `<button class="btn small" data-act="next">Suivant →</button>
        <button class="btn small ghost danger" data-act="stop">Arrêter</button>` : ""}</div>
    <div class="reveal">
      <img src="${esc(a.cover)}" alt="">
      <div>
        <div class="muted small">${mine ? (mine.correct ? `✓ Bien joué ! +${mine.points}` : mine.answer ? `✗ Tu as répondu ${esc(mine.answer)}` : "Pas de réponse") : ""}</div>
        <h2>${esc(a.title)}</h2>
        ${a.english ? `<div class="en">${esc(a.english)}</div>` : ""}
        <div class="song"><span class="tag ${esc(s.kind)}">${esc(s.slug || s.kind)}</span> <b>${esc(s.title)}</b> — ${esc(s.artist || "artiste inconnu")}</div>
        <div class="meta-line">
          ${s.season ? `<span>${esc(s.season)}</span>·` : ""}
          <span class="diff" title="Difficulté ${diff}/100">${label} <i data-w="${diff}"></i></span>·
          <span>${esc(VERSION[s.version] || "")}</span>
          ${s.youtube ? `· <a href="https://www.youtube.com/watch?v=${esc(s.youtube)}" target="_blank" rel="noopener">YouTube</a>` : ""}
        </div>
        <div class="muted small">${r.found}/${(r.results || []).length} ${r.found > 1 ? "ont trouvé" : "a trouvé"}</div>
      </div>
    </div>
    <div class="results">${(r.results || []).map((x) => `<div class="res">
        <img src="${esc(x.avatar)}" alt="" referrerpolicy="no-referrer">
        <span>${esc(x.name)} <span class="${x.correct ? "ok" : "ko"}">${x.correct ? "✓" : "✗"} ${esc(x.answer || "—")}</span></span>
        <span class="num">${x.seconds != null ? `${x.seconds.toFixed(1)} s` : ""}</span>
        <span class="num">${x.points > 0 ? "+" : ""}${x.points}</span></div>`).join("")}</div>
    ${S.settings.auto_next && r.idx + 1 < r.total ? `<div class="next-bar"><i id="next-bar"></i></div>` : ""}`;
}

function animateNext() {
  $$(".diff i[data-w]").forEach((i) => i.style.setProperty("--w", `${i.dataset.w}%`));
  const bar = $("#next-bar");
  if (!bar) return;
  bar.style.transition = "none";
  bar.style.width = "0";
  requestAnimationFrame(() => requestAnimationFrame(() => {
    bar.style.transition = `width ${S.settings.reveal}s linear`;
    bar.style.width = "100%";
  }));
}

function endHTML() {
  const r = result && result.ranking ? result : null;
  if (!r) return lobbyHTML();
  const top = r.ranking.slice(0, 3);
  const order = [top[1], top[0], top[2]];
  const cls = ["p2", "p1", "p3"], medal = ["🥈", "🥇", "🥉"];
  return `<div class="card code-box">
      <h2>Fin de la partie !</h2>
      <div class="podium">${order.map((p, i) => p ? `<div class="step ${cls[i]}">
          <img src="${esc(p.avatar)}" alt="" referrerpolicy="no-referrer"><div>${esc(p.name)}</div>
          <div class="block"><div class="medal">${medal[i]}</div><b>${p.score}</b><span class="muted small">${p.correct}/${p.rounds} trouvées</span></div>
        </div>` : "<div></div>").join("")}</div>
    </div>
    <div class="card">
      <table class="table"><thead><tr><th>#</th><th>Joueur</th><th class="n">Points</th><th class="n">Trouvées</th><th class="n">Série max</th></tr></thead>
      <tbody>${r.ranking.map((p) => `<tr><td><span class="rank-badge rank-${p.rank}">${p.rank}</span></td>
        <td><div class="who"><img src="${esc(p.avatar)}" alt="" referrerpolicy="no-referrer">${esc(p.name)}</div></td>
        <td class="n">${p.score}</td><td class="n">${p.correct}/${p.rounds}</td><td class="n">${p.best_streak}</td></tr>`).join("")}</tbody></table>
    </div>
    <div class="card">
      <h3 class="section-title">Les musiques de la partie</h3>
      <table class="table"><tbody>${(r.rounds || []).map((x, i) => `<tr><td class="n">${i + 1}</td><td>${esc(x.anime)}</td>
        <td><span class="tag ${esc(x.kind)}">${esc(x.kind)}</span> ${esc(x.song)}</td><td class="n">${x.found} ✓</td></tr>`).join("")}</tbody></table>
    </div>
    <div class="share">
      ${isHost() ? `<button class="btn accent big" data-act="lobby">Rejouer (même salon)</button>` : `<span class="muted">L'hôte peut relancer une partie.</span>`}
      ${r.game_id ? `<a class="btn big ghost" href="/partie/${r.game_id}">Récap de la partie</a>` : ""}
      <a class="btn big ghost" href="/classement">Classement</a>
    </div>`;
}

$("#stage").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-act], [data-copy]");
  if (!b) return;
  if (b.dataset.copy) {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast("Copié ✓"); } catch { toast(b.dataset.copy); }
    return;
  }
  const act = b.dataset.act;
  if (act === "start") { setupAnalyser(); send({ t: "start" }); }
  else if (act === "stop" && confirm("Arrêter la partie en cours ?")) send({ t: "stop" });
  else if (act === "next") send({ t: "next" });
  else if (act === "lobby") send({ t: "lobby" });
});

/* ------------------------------------------------------------------ réglages */
const SECTIONS = [
  { title: "Musiques", fields: [
    { k: "kinds", type: "multi", label: "Types", opts: [["OP", "Openings"], ["ED", "Endings"], ["IN", "Inserts"], ["OST", "OST"]] },
    { k: "pool", type: "seg", label: "Animes vus par…", opts: [["common", "Tout le monde"], ["any", "Au moins un"], ["host", "L'hôte"], ["all", "Tous"]] },
    { k: "statuses", type: "multi", label: "Statuts qui comptent", opts: [["finished", "Fini"], ["watching", "En cours"], ["to_watch", "À regarder"]] },
    { k: "diff", type: "diff", label: "Difficulté" },
    { k: "one_per_anime", type: "bool", label: "Une seule musique par anime" },
  ] },
  { title: "Manches", fields: [
    { k: "rounds", type: "range", label: "Nombre de manches", min: 3, max: 60 },
    { k: "seconds", type: "range", label: "Temps pour répondre", min: 5, max: 60, unit: " s" },
    { k: "clip_start", type: "seg", label: "Passage de la musique", opts: [["start", "Début"], ["middle", "Milieu"], ["random", "Au hasard"]] },
    { k: "audio", type: "seg", label: "Version", opts: [["auto", "Auto"], ["tv", "TV (anime)"], ["full", "Complète"]] },
  ] },
  { title: "Réponses", fields: [
    { k: "answer", type: "seg", label: "Comment répondre", opts: [["choices", "Choix"], ["search", "Recherche"]] },
    { k: "choices", type: "range", label: "Nombre de choix", min: 2, max: 8, when: (s) => s.answer === "choices" },
    { k: "distractors", type: "seg", label: "Mauvaises réponses", opts: [["similar", "Même genre"], ["lists", "Nos animes"], ["random", "Au hasard"]], when: (s) => s.answer === "choices" },
    { k: "images", type: "bool", label: "Images des animes" },
    { k: "hint_kind", type: "bool", label: "Indice : type (OP, ED…)" },
    { k: "hint_year", type: "bool", label: "Indice : année" },
  ] },
  { title: "Points", fields: [
    { k: "speed_bonus", type: "bool", label: "Bonus rapidité (jusqu'à +100)" },
    { k: "difficulty_bonus", type: "bool", label: "Bonus difficulté (jusqu'à +50 %)" },
    { k: "streak_bonus", type: "bool", label: "Bonus série (+10 par réponse d'affilée)" },
    { k: "wrong_penalty", type: "range", label: "Pénalité si faux", min: 0, max: 200, step: 10, unit: " pts" },
  ] },
  { title: "Rythme", fields: [
    { k: "reveal", type: "range", label: "Affichage de la réponse", min: 3, max: 30, unit: " s" },
    { k: "early_end", type: "bool", label: "Manche finie dès que tout le monde a répondu" },
    { k: "auto_next", type: "bool", label: "Enchaîner les manches tout seul" },
  ] },
];
const DIFF_PRESETS = [["Facile", 0, 40], ["Moyen", 30, 70], ["Difficile", 60, 100], ["Tout", 0, 100]];

function fieldHTML(f) {
  if (f.type === "multi" || f.type === "seg") {
    const cls = f.type === "multi" ? "chips" : "seg";
    const item = f.type === "multi" ? "chip" : "";
    return `<div class="field" data-field="${f.k}"><div class="lbl">${f.label}</div>
      <div class="${cls}">${f.opts.map(([v, l]) => `<button type="button" class="${item}" data-k="${f.k}" data-v="${v}">${l}</button>`).join("")}</div></div>`;
  }
  if (f.type === "range") {
    return `<div class="field" data-field="${f.k}"><label>${f.label} <b data-out="${f.k}"></b></label>
      <input type="range" data-k="${f.k}" min="${f.min}" max="${f.max}" step="${f.step || 1}"></div>`;
  }
  if (f.type === "bool") {
    return `<label class="toggle" data-field="${f.k}">${f.label}<input type="checkbox" data-k="${f.k}"></label>`;
  }
  if (f.type === "diff") {
    return `<div class="field" data-field="diff"><div class="lbl">${f.label} <b data-out="diff"></b></div>
      <div class="chips">${DIFF_PRESETS.map(([l, a, b]) => `<button type="button" class="chip" data-diff="${a}-${b}">${l}</button>`).join("")}</div>
      <div class="dual"><input type="range" data-k="diff_min" min="0" max="100"><input type="range" data-k="diff_max" min="0" max="100"></div></div>`;
  }
  return "";
}

let settingsBuilt = false;
function buildSettings() {
  const box = $("#settings");
  box.innerHTML = `<h3>Presets</h3>
    <div class="preset-bar" id="preset-bar"></div>
    <div class="mine-row"><select id="mine" aria-label="Mes presets"><option value="">Mes presets…</option></select>
      <button type="button" class="btn small" id="save-preset" title="Enregistrer ces réglages">Enregistrer</button>
      <button type="button" class="btn small ghost" id="del-preset" title="Supprimer ce preset">✕</button></div>
    <form id="sform">${SECTIONS.map((s) => `<fieldset><h3>${s.title}</h3>${s.fields.map(fieldHTML).join("")}</fieldset>`).join("")}</form>`;
  settingsBuilt = true;
  const form = $("#sform");
  form.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-k], button[data-diff]");
    if (!b || !isHost()) return;
    const s = { ...S.settings };
    if (b.dataset.diff) {
      const [a, z] = b.dataset.diff.split("-").map(Number);
      s.diff_min = a; s.diff_max = z;
    } else {
      const f = SECTIONS.flatMap((x) => x.fields).find((x) => x.k === b.dataset.k);
      if (f.type === "multi") {
        const cur = new Set(s[f.k]);
        cur.has(b.dataset.v) ? cur.delete(b.dataset.v) : cur.add(b.dataset.v);
        if (!cur.size) { toast("Garde au moins un choix", true); return; }
        s[f.k] = f.opts.map(([v]) => v).filter((v) => cur.has(v));
      } else s[f.k] = b.dataset.v;
    }
    pushSettings(s);
  });
  form.addEventListener("input", (e) => {
    const k = e.target.dataset.k;
    if (k && e.target.type === "range") updateOut(k, +e.target.value);
  });
  form.addEventListener("change", (e) => {
    const k = e.target.dataset.k;
    if (!k || !isHost()) return;
    const s = { ...S.settings };
    s[k] = e.target.type === "checkbox" ? e.target.checked : +e.target.value;
    pushSettings(s);
  });
  $("#preset-bar").addEventListener("click", (e) => {
    const b = e.target.closest("[data-preset]");
    if (b && isHost()) send({ t: "settings", preset: b.dataset.preset });
  });
  $("#mine").addEventListener("change", (e) => {
    const p = presets.mine.find((x) => String(x.id) === e.target.value);
    if (p && isHost()) send({ t: "settings", settings: p.settings, preset: "perso" });
  });
  $("#save-preset").addEventListener("click", async () => {
    const name = prompt("Nom du preset :", "Mon blind test");
    if (!name) return;
    try { presets = await api("/api/presets", { name, settings: S.settings }); fillPresets(); toast("Preset enregistré ✓"); }
    catch (err) { toast(err.message, true); }
  });
  $("#del-preset").addEventListener("click", async () => {
    const id = $("#mine").value;
    if (!id) { toast("Choisis d'abord un de tes presets", true); return; }
    if (!confirm("Supprimer ce preset ?")) return;
    try { presets = await api(`/api/presets/${id}/delete`, {}); fillPresets(); } catch (err) { toast(err.message, true); }
  });
  fillPresets();
}

function updateOut(k, v) {
  const f = SECTIONS.flatMap((x) => x.fields).find((x) => x.k === k);
  if (k === "diff_min" || k === "diff_max") {
    const a = +$('[data-k="diff_min"]').value, b = +$('[data-k="diff_max"]').value;
    $('[data-out="diff"]').textContent = `${Math.min(a, b)} – ${Math.max(a, b)}`;
  } else if (f) {
    const out = $(`[data-out="${k}"]`);
    if (out) out.textContent = `${v}${f.unit || ""}`;
  }
}

let pushTimer = null;
function pushSettings(s) {
  S.settings = s;
  fillSettings();
  clearTimeout(pushTimer);
  pushTimer = setTimeout(() => send({ t: "settings", settings: s, preset: "perso" }), 120);
}

function fillPresets() {
  const bar = $("#preset-bar");
  if (!bar) return;
  bar.innerHTML = Object.entries(presets.builtin).map(([k, p]) =>
    `<button type="button" class="chip${S && S.preset === k ? " on" : ""}" data-preset="${esc(k)}" title="${esc(p.desc)}">${esc(p.icon)} ${esc(p.name)}</button>`).join("");
  const sel = $("#mine");
  const cur = sel.value;
  sel.innerHTML = `<option value="">Mes presets…</option>` + presets.mine.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  sel.value = cur;
}

function fillSettings() {
  const s = S.settings, host = isHost();
  $$("#sform fieldset").forEach((fs) => { fs.disabled = !host; });
  for (const f of SECTIONS.flatMap((x) => x.fields)) {
    const wrap = $(`[data-field="${f.k}"]`);
    if (wrap) wrap.hidden = f.when ? !f.when(s) : false;
    if (f.type === "multi" || f.type === "seg") {
      $$(`button[data-k="${f.k}"]`).forEach((b) => b.classList.toggle(f.type === "multi" ? "on" : "on",
        f.type === "multi" ? s[f.k].includes(b.dataset.v) : s[f.k] === b.dataset.v));
    } else if (f.type === "range") {
      const inp = $(`input[data-k="${f.k}"]`);
      if (document.activeElement !== inp) inp.value = s[f.k];
      updateOut(f.k, s[f.k]);
    } else if (f.type === "bool") {
      $(`input[data-k="${f.k}"]`).checked = !!s[f.k];
    } else if (f.type === "diff") {
      $('[data-k="diff_min"]').value = s.diff_min;
      $('[data-k="diff_max"]').value = s.diff_max;
      updateOut("diff_min");
      $$("[data-diff]").forEach((b) => b.classList.toggle("on", b.dataset.diff === `${s.diff_min}-${s.diff_max}`));
    }
  }
  $$("#preset-bar [data-preset]").forEach((b) => b.classList.toggle("on", S.preset === b.dataset.preset));
  $("#save-preset").disabled = false;
  $(".mine-row select").disabled = !host;
}

function renderSettings() {
  if (!settingsBuilt) buildSettings();
  fillSettings();
}

api("/api/presets").then((j) => { presets = j; if (settingsBuilt) fillPresets(); }).catch(() => {});
connect();
