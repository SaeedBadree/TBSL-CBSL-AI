// static/app.js

let spec = null;
let sending = false;

function addMsg(role, text) {
  const chatDiv = document.getElementById("chat");
  const bubble = document.createElement("div");
  bubble.className = "bubble " + role;
  bubble.textContent = text;
  chatDiv.appendChild(bubble);
  chatDiv.scrollTop = chatDiv.scrollHeight;
}

function setSending(isSending) {
  sending = isSending;
  const btn = document.getElementById("send");
  if (!btn) return;
  btn.disabled = isSending;
  btn.textContent = isSending ? "Sending…" : "Send";
}

async function onSend() {
  if (sending) return;

  const msgIn = document.getElementById("msg");
  if (!msgIn) return;

  const text = (msgIn.value || "").trim();
  if (!text) return;

  addMsg("user", text);
  msgIn.value = "";
  setSending(true);

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache"
      },
      cache: "no-store",
      body: JSON.stringify({ message: text, spec })
    });

    // Try JSON first; fall back to text for debugging
    let data = null;
    const ct = resp.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      data = await resp.json();
    } else {
      const t = await resp.text();
      console.error("Non-JSON response:", t);
      addMsg("assistant", "Sorry, something went wrong (non-JSON response).");
      setSending(false);
      return;
    }

    if (!data || data.ok === false) {
      addMsg("assistant", "Error: " + (data && data.error ? data.error : "unknown"));
      setSending(false);
      return;
    }

    spec = data.spec || spec;
    addMsg("assistant", data.assistant || "OK.");

    if (data.estimate) {
      const results = document.getElementById("results");
      const bomTbody = document.querySelector("#bom tbody");
      const totalDiv = document.getElementById("total");
      const notesDiv = document.getElementById("notes");

      results.style.display = "block";
      bomTbody.innerHTML = "";

      (data.estimate.lines || []).forEach((l) => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${l.name}</td>
          <td>${Number(l.qty).toFixed(2)}</td>
          <td>${l.unit}</td>
          <td>${Number(l.unit_price || 0).toFixed(2)}</td>
          <td>${Number(l.total || 0).toFixed(2)}</td>
        `;
        bomTbody.appendChild(tr);
      });

      totalDiv.textContent = "Total: " + Number(data.estimate.total || 0).toFixed(2);
      notesDiv.textContent = data.ai_notes ? "Notes: " + data.ai_notes : "";
    }
  } catch (e) {
    console.error(e);
    addMsg("assistant", "Sorry, something went wrong.");
  } finally {
    setSending(false);
  }
}

// Bind only after the DOM is ready so #send/#msg exist.
document.addEventListener("DOMContentLoaded", () => {
  addMsg(
    "assistant",
    "Tell me what you want to build (e.g., driveway 12x20 ft, 4 inch thick). I’ll propose the materials and price only what you stock."
  );

  const sendBtn = document.getElementById("send");
  const msgIn = document.getElementById("msg");

  if (sendBtn) sendBtn.addEventListener("click", onSend);
  if (msgIn) {
    msgIn.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        onSend();
      }
    });
  }
});
