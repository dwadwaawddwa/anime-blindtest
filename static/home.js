/* Accueil : créer un salon (avec un preset) ou en rejoindre un par son code. */
"use strict";
const $ = (s) => document.querySelector(s);
const CSRF = $('meta[name="csrf"]').content;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg, err = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", err);
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 3500);
}

async function create(preset) {
  try {
    const r = await fetch("/api/rooms", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF": CSRF },
      body: JSON.stringify({ preset }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || "Erreur");
    location.href = `/room/${j.code}`;
  } catch (e) { toast(e.message, true); }
}

$("#create").addEventListener("click", () => create("classique"));

$("#join-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = $("#join-code").value.trim().toUpperCase();
  if (code.length !== 5) { toast("Le code fait 5 caractères", true); return; }
  const r = await fetch(`/api/rooms/${code}`);
  const j = await r.json();
  if (j.exists) location.href = `/room/${code}`;
  else toast("Aucune partie avec ce code", true);
});
$("#join-code").addEventListener("input", (e) => { e.target.value = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ""); });

fetch("/api/presets").then((r) => r.json()).then((j) => {
  const box = $("#presets");
  const builtin = Object.entries(j.builtin).map(([k, p]) =>
    `<button class="preset" data-preset="${esc(k)}"><b>${esc(p.icon)} ${esc(p.name)}</b><span>${esc(p.desc)}</span></button>`);
  box.innerHTML = builtin.join("");
  box.addEventListener("click", (e) => {
    const b = e.target.closest("[data-preset]");
    if (b) create(b.dataset.preset);
  });
});
