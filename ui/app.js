/* BA-Account Swarm — Mission Control dashboard client. */

(function () { "use strict";

  /* ---- DOM helpers ---------------------------------------------------------- */
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function appendLog(text, extraClass) {
    const div = document.createElement("div");
    div.className = "msg" + (extraClass ? " " + extraClass : "");
    div.innerHTML = `<small class="stamp">${new Date().toISOString()}</small> ${esc(text)}`;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function esc(s) {
    const d = document.createElement("textarea");
    d.textContent = s;
    return d.innerHTML;
  }

  /* ---- WebSocket chat ------------------------------------------------------ */
  let ws = null;

  function connectWS() {
    if (ws) { try { ws.close(); } catch (_) {} }

    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/chat`);

    ws.onopen = () => appendLog("[connected]", "ok");
    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        switch (msg.type) {
          case "status":  appendLog("⚡ " + msg.text, "dim"); break;
          case "result":
            appendLog(`<b>${esc(msg.result).substring(0, 300)}</b>`, "highlight");
            showSession(`swarm/audit/${msg.session_id}`);
            startHITLRefresh();
            break;
          case "event":
            appendLog(`${esc(msg.agent || "")}: ${esc(msg.action || "")} — ${esc(msg.details || msg.payload || "")}`, "dim");
            break;
          case "error":   appendLog("⚠️  " + esc(msg.text), "err"); break;
        }
      } catch (_) { appendLog(evt.data, "dim"); }
    };
    ws.onclose = () => setTimeout(connectWS, 3000);
  }

  function sendQuery(query) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      appendLog("[offline] — connect to start", "err");
      return;
    }
    ws.send(JSON.stringify({ type: "query", content: query, caller: caller.value || "web" }));
  }

  /* ---- HITL polling -------------------------------------------------------- */
  let hitlTimer = null;

  async function fetchHITL() {
    try {
      const resp = await fetch(`/hitl/pending?caller=${encodeURIComponent(caller.value || "web")}`);
      const data = await resp.json();
      const pending = (data.pending || []).filter(p => p.question);

      const listEl = hitlList;
      listEl.innerHTML = "";

      if (!pending.length) {
        listEl.textContent = "No pending actions.";
        return;
      }

      pending.forEach((p) => {
        const row = document.createElement("div");
        row.className = "hitl-row";
        row.innerHTML = `
          <span class="hitl-tool"><b>${esc(p.tool_name)}</b></span>
          <span class="hitl-q">${esc(p.question.substring(0, 100))}</span>
          <div class="hitl-actions">
            <button data-id="${esc(p.id)}" data-action="approve">✓</button>
            <button data-id="${esc(p.id)}" data-action="deny">✗</button>
          </div>`;
        listEl.appendChild(row);
      });

      $$(".hitl-actions button", listEl).forEach(btn => {
        btn.addEventListener("click", async () => {
          const id = btn.dataset.id;
          const action = btn.dataset.action;
          await fetch("/hitl/action", {
            method: "POST",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({ id, action, answerer: caller.value || "web" }),
          });
          startHITLRefresh();  // refresh after action
        });
      });

    } catch (err) { /* ignore offline / CORS during dev */ }
  }

  function startHITLRefresh() {
    if (hitlTimer) clearInterval(hitlTimer);
    fetchHITL();
    hitlTimer = setInterval(fetchHITL, 4000);
  }

  /* ---- Audit log ----------------------------------------------------------- */

  async function showPanel(url) {
    const panel = $("#panel");
    if (!panel) return;
    try {
      const resp = await fetch(url);
      const data = await resp.json();
      panel.innerHTML = `<pre>${esc(JSON.stringify(data, null, 2))}</pre>`;
    } catch (err) {
      panel.innerHTML = "<em>[failed to load]</em>";
    }
  }

  function showSession(path) {
    const btn = $("#audit-btn");
    if (btn) btn.addEventListener("click", () => showPanel(`/audit/${path}`), { once: true });
  }

  /* ---- Context ------------------------------------------------------------- */

  async function saveContext() {
    const keyEl = ctxKey;
    const valEl = ctxVal;
    await fetch("/context/save", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ caller: caller.value || "web", key: keyEl.value, value: valEl.value }),
    });
    appendLog(`Context saved: ${esc(keyEl.value)} = ${esc(valEl.value).substring(0, 60)}`, "ok");
  }

  async function loadContext() {
    const keyEl = ctxKey;
    try {
      const resp = await fetch(`/context/get?caller=${encodeURIComponent(caller.value||"web")}&key=${encodeURIComponent(keyEl.value)}`);
      const data = await resp.json();
      ctxVal.value = data.value !== undefined ? JSON.stringify(data.value, null, 2) : "";
    } catch (err) { /* offline */ }
  }

  /* ---- DOM wiring ---------------------------------------------------------- */

  const log     = $("#log");
  const submit  = $("#btnSubmit");
  const caller  = $("#callerId");
  const hitlList = $("#hitlList");
  const ctxKey = $("#ctxKey");
  const ctxVal = $("#ctxVal");
  let panelShown = false;

  submit.addEventListener("click", () => { sendQuery(inputBox.value); inputBox.value = ""; });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target === inputBox) { submit.click(); }
  });
  ctxKey.addEventListener("change", loadContext);

  /* Init */
  connectWS();
  startHITLRefresh();

})();

