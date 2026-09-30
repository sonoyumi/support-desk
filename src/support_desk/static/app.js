// Small helpers for the operators' panel. The page works without JavaScript; this only adds comfort.
"use strict";

document.addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-href]");
  if (row && !e.target.closest("a, button, input, select")) location.href = row.dataset.href;
});

document.addEventListener("submit", (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

const canned = document.getElementById("canned");
if (canned) {
  canned.addEventListener("change", () => {
    const reply = document.getElementById("reply");
    if (canned.value) {
      reply.value = reply.value ? reply.value + "\n" + canned.value : canned.value;
      reply.focus();
    }
    canned.value = "";
  });
}

// New customer messages appear without reloading the page (and without losing a half-typed reply).
const box = document.getElementById("msgs");
if (box) {
  const ticket = box.dataset.ticket;
  let last = Number(box.dataset.last || 0);
  const label = { in: "Клиент", out: "Оператор", note: "🔒 Заметка", rating: "Бот · запрос оценки" };
  async function poll() {
    try {
      const r = await fetch(`/api/tickets/${ticket}/messages?after=${last}`, { credentials: "same-origin" });
      if (!r.ok) return;
      for (const m of (await r.json()).messages) {
        const div = document.createElement("div");
        div.className = "msg " + m.kind;
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = `${m.kind === "in" ? label.in : (m.operator || label[m.kind])} · ${m.time}`;
        const text = document.createElement("div");
        text.className = "text";
        text.textContent = m.text;  // textContent: customer text is never parsed as HTML
        div.append(meta, text);
        box.append(div);
        last = Math.max(last, m.id);
      }
    } catch (_) { /* offline for a moment: try again next time */ }
  }
  setInterval(poll, 5000);
}
