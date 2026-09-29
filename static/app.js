const oddsBanner = document.querySelector("#odds-banner");
const refreshButton = document.querySelector("#refresh");
const statusLine = document.querySelector("#status-line");
const bar = document.querySelector("#bar");
const barFill = document.querySelector("#bar-fill");
const errorBox = document.querySelector("#error");
const latestDate = document.querySelector("#latest-date");
const latestBalls = document.querySelector("#latest-balls");
const latestJackpot = document.querySelector("#latest-jackpot");
const nextDate = document.querySelector("#next-date");
const nextAmount = document.querySelector("#next-amount");
const methodLabel = document.querySelector("#method-label");
const tipBalls = document.querySelector("#tip-balls");
const tipNote = document.querySelector("#tip-note");
const tipMeta = document.querySelector("#tip-meta");
const generateButton = document.querySelector("#generate");
const costAmount = document.querySelector("#cost-amount");
const costStake = document.querySelector("#cost-stake");
const costHint = document.querySelector("#cost-hint");
const mainFreq = document.querySelector("#main-freq");
const euroFreq = document.querySelector("#euro-freq");
const recent = document.querySelector("#recent");

let method = "frequency";
let suggestion = null;
let polling = false;

function formatDate(iso) {
  if (!iso) return "—";
  const [year, month, day] = iso.split("-");
  return `${day}.${month}.${year}`;
}

function formatMoney(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const amount = Number(value);
  const whole = Math.abs(amount - Math.round(amount)) < 0.001;
  return new Intl.NumberFormat("de-DE", {
    style: "currency",
    currency: "EUR",
    maximumFractionDigits: whole ? 0 : 2,
  }).format(amount);
}

function formatPrice(value) {
  return new Intl.NumberFormat("de-DE", {
    style: "currency",
    currency: "EUR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

function renderCost(status) {
  const total = Number(status.fieldPrice);
  const stake = Number(status.fieldStake);
  const fee = Number(status.fieldFee);
  const drawDate = status.nextDrawDate || (status.latest && status.latest.date);
  const when = drawDate ? ` am ${formatDate(drawDate)}` : "";
  if (!Number.isFinite(total)) {
    costStake.textContent = "—";
    costAmount.textContent = "—";
    costHint.textContent = status.fieldPriceError || "Der Preis wird ermittelt …";
    return;
  }
  costStake.textContent = formatPrice(Number.isFinite(stake) ? stake : total);
  costAmount.textContent = formatPrice(total);
  const fields = Number(status.slipFields) || 1;
  if (Number.isFinite(stake) && Number.isFinite(fee)) {
    costHint.textContent = `${fields} Felder × ${formatPrice(stake)} plus ${formatPrice(fee)} Bearbeitungsgebühr in NRW${when}.`;
  } else {
    costHint.textContent = `Zu zahlen in NRW für ${fields} Felder${when}.`;
  }
}

function formatOdds(value) {
  const odds = Number(value) || 139838160;
  return `1 : ${new Intl.NumberFormat("de-DE").format(odds)}`;
}

function ball(number, kind) {
  const node = document.createElement("span");
  node.className = kind === "euro" ? "ball euro" : "ball";
  node.textContent = String(number);
  return node;
}

function ballGroup(numbers, kind, details, mode) {
  const fragment = document.createDocumentFragment();
  numbers.forEach((number, index) => {
    const wrap = document.createElement("div");
    wrap.className = "ball-wrap";
    wrap.append(ball(number, kind));
    const detail = details && details[index];
    if (detail && mode !== "random") {
      const caption = document.createElement("span");
      caption.className = "caption";
      caption.textContent = mode === "overdue" ? `Pause ${detail.gap}` : `${detail.count}-mal`;
      wrap.append(caption);
    }
    fragment.append(wrap);
  });
  return fragment;
}

function renderBalls(container, main, euro) {
  container.replaceChildren();
  if (!main || !main.length) return;
  main.forEach((number) => container.append(ball(number, "main")));
  const plus = document.createElement("span");
  plus.className = "plus";
  plus.textContent = "+";
  container.append(plus);
  euro.forEach((number) => container.append(ball(number, "euro")));
}

function renderTip() {
  tipBalls.replaceChildren();
  if (!suggestion) {
    methodLabel.textContent = "Verfahren: —";
    tipNote.textContent = "Der Tipp erscheint, sobald die Ziehungen geladen sind.";
    tipMeta.textContent = "";
    return;
  }
  const fields = suggestion.fields && suggestion.fields.length
    ? suggestion.fields
    : [{ main: suggestion.main, euro: suggestion.euro }];
  methodLabel.textContent = `Verfahren: ${suggestion.methodLabel}`;
  fields.forEach((field, index) => {
    const row = document.createElement("div");
    row.className = "slip-field";
    const label = document.createElement("div");
    label.textContent = `Feld ${index + 1}`;
    const balls = document.createElement("div");
    balls.className = "balls";
    renderBalls(balls, field.main, field.euro);
    row.append(label, balls);
    tipBalls.append(row);
  });
  tipNote.textContent = suggestion.note;
  const jackpot = formatMoney(suggestion.nextJackpot);
  const when = formatDate(suggestion.nextDrawDate);
  tipMeta.textContent = `Chance ${formatOdds(suggestion.odds)} je Feld. Nächster Jackpot ${jackpot} am ${when}. Grundlage: ${suggestion.basedOnDraws} Ziehungen.`;
}

function heat(count, max) {
  if (!max) return 0.08;
  return 0.12 + (count / max) * 0.88;
}

function renderFrequencies(frequencies) {
  const main = frequencies.main || [];
  const euro = frequencies.euro || [];
  const mainMax = Math.max(1, ...main.map((row) => row.count));
  const euroMax = Math.max(1, ...euro.map((row) => row.count));
  const pickedMain = new Set();
  const pickedEuro = new Set();
  const fields = (suggestion && suggestion.fields) || [];
  if (fields.length) {
    fields.forEach((field) => {
      field.main.forEach((number) => pickedMain.add(number));
      field.euro.forEach((number) => pickedEuro.add(number));
    });
  } else if (suggestion) {
    (suggestion.main || []).forEach((number) => pickedMain.add(number));
    (suggestion.euro || []).forEach((number) => pickedEuro.add(number));
  }

  function fill(target, rows, max, picked, kind) {
    target.replaceChildren();
    rows.forEach((row) => {
      const cell = document.createElement("div");
      cell.className = picked.has(row.number) ? "cell picked" : "cell";
      const tone = kind === "euro" ? "29, 78, 137" : "18, 56, 45";
      cell.style.background = `rgba(${tone}, ${heat(row.count, max)})`;
      if (kind === "euro" && row.count / max > 0.55) cell.style.color = "#f6f1e6";
      const strong = document.createElement("strong");
      strong.textContent = String(row.number);
      const span = document.createElement("span");
      span.textContent = String(row.count);
      if (kind === "euro" && row.count / max > 0.55) span.style.color = "#d5e4dc";
      cell.title = `${row.count}-mal, Pause ${row.gap} Ziehungen${row.lastSeen ? `, zuletzt ${formatDate(row.lastSeen)}` : ""}`;
      cell.append(strong, span);
      target.append(cell);
    });
  }

  fill(mainFreq, main, mainMax, pickedMain, "main");
  fill(euroFreq, euro, euroMax, pickedEuro, "euro");
}

function renderRecent(draws) {
  recent.replaceChildren();
  if (!draws || !draws.length) {
    recent.textContent = "Noch keine Ziehungen geladen.";
    return;
  }
  draws.forEach((draw) => {
    const row = document.createElement("div");
    row.className = "draw-row";
    const date = document.createElement("div");
    date.textContent = formatDate(draw.date);
    const balls = document.createElement("div");
    balls.className = "balls";
    renderBalls(balls, draw.main, draw.euro);
    row.append(date, balls);
    recent.append(row);
  });
}

function renderStatus(status) {
  oddsBanner.textContent = formatOdds(status.odds);
  const progress = status.progress || { done: 0, total: 0 };
  refreshButton.disabled = Boolean(status.refreshing);
  if (status.refreshing) {
    const current = progress.currentDate ? ` (${formatDate(progress.currentDate)})` : "";
    statusLine.textContent = progress.total
      ? `Abruf ${progress.done} von ${progress.total}${current}`
      : "Abruf wird vorbereitet …";
    bar.hidden = false;
    const ratio = progress.total ? Math.min(100, (progress.done / progress.total) * 100) : 4;
    barFill.style.width = `${ratio}%`;
  } else {
    bar.hidden = true;
    const since = formatDate(status.ruleStart);
    statusLine.textContent = status.drawCount
      ? `${status.drawCount} Ziehungen seit dem ${since}. ${status.message || ""}`.trim()
      : "Noch keine Ziehungen gespeichert.";
  }

  if (status.error) {
    errorBox.hidden = false;
    errorBox.textContent = status.error;
  } else {
    errorBox.hidden = true;
    errorBox.textContent = "";
  }

  const latest = status.latest;
  if (latest) {
    latestDate.textContent = formatDate(latest.date);
    renderBalls(latestBalls, latest.main, latest.euro);
    latestJackpot.textContent = `Jackpot dieser Ziehung: ${formatMoney(latest.jackpot)}`;
  } else {
    latestDate.textContent = "—";
    latestBalls.replaceChildren();
    latestJackpot.textContent = "";
  }

  nextDate.textContent = formatDate(status.nextDrawDate);
  nextAmount.textContent = formatMoney(status.nextJackpot);
  renderCost(status);
  renderFrequencies(status.frequencies || { main: [], euro: [] });
  renderRecent(status.recent || []);
}

async function readJson(response) {
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Die Anfrage ist fehlgeschlagen.");
  }
  return payload;
}

async function loadStatus() {
  const response = await fetch("/api/status");
  return readJson(response);
}

async function loadSuggestion() {
  const response = await fetch(`/api/suggestion?method=${encodeURIComponent(method)}`);
  suggestion = await readJson(response);
  renderTip();
}

async function refreshView() {
  const status = await loadStatus();
  renderStatus(status);
  if (status.drawCount && !suggestion) {
    await loadSuggestion();
    renderStatus(status);
  } else if (suggestion) {
    renderFrequencies(status.frequencies || { main: [], euro: [] });
  }
  return status;
}

async function poll() {
  if (polling) return;
  polling = true;
  try {
    let status = await refreshView();
    while (status.refreshing) {
      await new Promise((resolve) => setTimeout(resolve, 700));
      status = await refreshView();
    }
    if (status.drawCount) {
      await loadSuggestion();
      renderStatus(await loadStatus());
    }
  } catch (error) {
    errorBox.hidden = false;
    errorBox.textContent = error.message;
  } finally {
    polling = false;
  }
}

async function startRefresh() {
  refreshButton.disabled = true;
  const response = await fetch("/api/refresh", { method: "POST" });
  await readJson(response);
  await poll();
}

refreshButton.addEventListener("click", () => {
  startRefresh().catch((error) => {
    errorBox.hidden = false;
    errorBox.textContent = error.message;
    refreshButton.disabled = false;
  });
});

document.querySelectorAll(".method").forEach((button) => {
  button.addEventListener("click", () => {
    method = button.dataset.method;
    document.querySelectorAll(".method").forEach((item) => {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-pressed", active ? "true" : "false");
    });
    loadSuggestion()
      .then(() => loadStatus())
      .then((status) => renderStatus(status))
      .catch((error) => {
        errorBox.hidden = false;
        errorBox.textContent = error.message;
      });
  });
});

generateButton.addEventListener("click", () => {
  loadSuggestion()
    .then(() => loadStatus())
    .then((status) => renderStatus(status))
    .catch((error) => {
      errorBox.hidden = false;
      errorBox.textContent = error.message;
    });
});

refreshView()
  .then((status) => {
    if (status.refreshing || status.drawCount === 0) return startRefresh();
    return loadSuggestion().then(() => refreshView());
  })
  .catch((error) => {
    errorBox.hidden = false;
    errorBox.textContent = error.message;
  });
