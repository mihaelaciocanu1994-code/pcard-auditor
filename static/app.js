"use strict";

const $ = (sel) => document.querySelector(sel);
const last = { ask: null, dash: null };

const MONEY_COLS = new Set(["Amount", "Total spent", "Total", "Sum"]);

function isMoney(col) {
  return MONEY_COLS.has(col) || /amount|total|spent|sum|value/i.test(col);
}

function fmtMoney(v) {
  const n = Number(v);
  if (!isFinite(n)) return v;
  return n.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function fmtDate(v) {
  if (typeof v === "string") {
    const m = v.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/);
    if (m) return `${m[1]}/${m[2]}/${m[3]}`;
  }
  return v;
}

function renderTable(table, columns, rows) {
  const head = columns.map((c) => `<th>${escapeHtml(c)}</th>`).join("");
  const body = rows
    .map((row) => {
      const cells = row
        .map((cell, i) => {
          const col = columns[i];
          if (cell === null || cell === undefined) return '<td class="num">—</td>';
          if (typeof cell === "number" && isMoney(col)) {
            return `<td class="num${cell < 0 ? " negative" : ""}">${fmtMoney(cell)}</td>`;
          }
          if (typeof cell === "number") return `<td class="num">${cell.toLocaleString("en-US")}</td>`;
          if (/date/i.test(col)) return `<td>${escapeHtml(fmtDate(cell))}</td>`;
          const wide = /description|vendor|mcc/i.test(col) ? " class=\"wide\"" : "";
          return `<td${wide}>${escapeHtml(String(cell))}</td>`;
        })
        .join("");
      return `<tr>${cells}</tr>`;
    })
    .join("");
  table.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function setStatus(el, message, isError) {
  if (!message) {
    el.hidden = true;
    el.textContent = "";
    return;
  }
  el.hidden = false;
  el.className = "status" + (isError ? " is-error" : "");
  el.textContent = message;
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({ error: "The server sent back something unreadable." }));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status}).`);
  return data;
}

/* ---------------- tabs ---------------- */

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => {
      const on = t === tab;
      t.classList.toggle("is-active", on);
      t.setAttribute("aria-selected", String(on));
    });
    document.querySelectorAll(".panel").forEach((p) => {
      p.classList.toggle("is-active", p.id === `panel-${tab.dataset.panel}`);
    });
  });
});

/* ---------------- tab 1: ask ---------------- */

const askStatus = $("#ask-status");
const askResults = $("#ask-results");

document.querySelectorAll(".examples .chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    $("#question").value = chip.dataset.q;
    $("#question").focus();
  });
});

$("#ask-run").addEventListener("click", runAsk);
$("#question").addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") runAsk();
});

async function runAsk() {
  const question = $("#question").value.trim();
  if (!question) {
    setStatus(askStatus, "Type a question first.", true);
    return;
  }
  const btn = $("#ask-run");
  btn.disabled = true;
  btn.textContent = "Running…";
  askResults.hidden = true;
  setStatus(askStatus, "Gemini is writing the query, then it runs against the database.");

  try {
    const data = await postJSON("/api/ask", { question });
    last.ask = data;
    $("#ask-sql").textContent = data.sql;
    if (data.summary) {
      $("#ask-summary").textContent = data.summary;
      $("#ask-summary").hidden = false;
    } else {
      $("#ask-summary").hidden = true;
    }
    const n = data.rows.length;
    $("#ask-count").innerHTML = n
      ? `<strong>${n.toLocaleString("en-US")}</strong> row${n === 1 ? "" : "s"}${
          data.truncated ? " (capped at 500 — narrow the question for the full set)" : ""
        } · ${data.elapsed}s`
      : "No rows matched. Try rewording the question or widening the date range.";
    renderTable($("#ask-table"), data.columns, data.rows);
    askResults.hidden = false;
    setStatus(askStatus, null);
  } catch (err) {
    setStatus(askStatus, err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run query";
  }
}

/* ---------------- tab 2: dashboard ---------------- */

const dashStatus = $("#dash-status");
const dashResults = $("#dash-results");

document.querySelectorAll(".chip-flag").forEach((chip) => {
  chip.addEventListener("click", () => {
    $("#kw-description").value = chip.dataset.description;
    $("#kw-vendor").value = chip.dataset.vendor;
    setStatus(
      dashStatus,
      `Keywords for ${chip.textContent.trim()} loaded into both searches. Run whichever one you need.`
    );
  });
});

document.querySelectorAll("[data-search]").forEach((btn) => {
  btn.addEventListener("click", () => runSearch(btn.dataset.search, btn));
});

["kw-description", "kw-vendor"].forEach((id) => {
  $(`#${id}`).addEventListener("keydown", (e) => {
    if (e.key === "Enter") runSearch(id === "kw-description" ? "Description" : "Vendor");
  });
});

async function runSearch(field, btn) {
  const input = field === "Description" ? $("#kw-description") : $("#kw-vendor");
  const keywords = input.value.split(",").map((k) => k.trim()).filter(Boolean);
  if (!keywords.length) {
    setStatus(dashStatus, `Enter a keyword to search the ${field.toLowerCase()} field.`, true);
    input.focus();
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Searching…";
  }
  dashResults.hidden = true;
  setStatus(dashStatus, "Searching…");

  try {
    const data = await postJSON("/api/search", {
      field,
      year: $("#year").value,
      keywords,
    });
    last.dash = data;
    const yr = $("#year").value === "all" ? "all years" : $("#year").value;
    $("#dash-count").innerHTML = data.count
      ? `<span class="flagged">${data.count.toLocaleString("en-US")}</span> transaction${
          data.count === 1 ? "" : "s"
        } in ${yr} matched the ${field.toLowerCase()} search, totalling ${fmtMoney(data.total)}${
          data.truncated ? ". Showing the 500 largest." : "."
        }`
      : `Nothing in ${yr} matched those keywords in the ${field.toLowerCase()} field. Try a shorter or more common word.`;
    renderTable($("#dash-table"), data.columns, data.rows);
    dashResults.hidden = false;
    setStatus(dashStatus, null);
  } catch (err) {
    setStatus(dashStatus, err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Search";
    }
  }
}

/* ---------------- CSV export ---------------- */

document.querySelectorAll("[data-export]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const data = last[btn.dataset.export];
    if (!data) return;
    const res = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ columns: data.columns, rows: data.rows }),
    });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "pcard-audit-results.csv";
    a.click();
    URL.revokeObjectURL(url);
  });
});
