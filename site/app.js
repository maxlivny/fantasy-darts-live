let payload = null;
let archiveItems = [];
let opened = new Set();
let currentMode = "live";
let currentArchiveId = null;

const standingsBody = document.querySelector("#standings");
const searchInput = document.querySelector("#search");
const tournamentSection = document.querySelector("#tournamentSection");
const archiveSection = document.querySelector("#archiveSection");
const currentButton = document.querySelector("#currentButton");
const archiveButton = document.querySelector("#archiveButton");
const refreshButton = document.querySelector("#refreshButton");

function formatCost(value) {
  return Number(value || 0).toLocaleString("ru-RU", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
}

function formatUpdated(value, archived = false) {
  if (!value) return archived ? "Дата завершения неизвестна" : "Время обновления неизвестно";
  return (archived ? "Итоговые данные: " : "Обновлено: ") + new Date(value).toLocaleString("ru-RU");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function renderStandings() {
  const query = searchInput.value.trim().toLocaleLowerCase("ru");
  standingsBody.innerHTML = "";
  const rows = (payload?.standings || []).filter(item =>
    String(item.name || "").toLocaleLowerCase("ru").includes(query)
  );

  document.querySelector("#empty").classList.toggle("hidden", rows.length !== 0);

  rows.forEach(item => {
    const tr = document.createElement("tr");
    tr.className = "main-row" + (item.rating ? " rating" : "");
    tr.innerHTML = `
      <td>${escapeHtml(item.place)}</td>
      <td><span class="name">${escapeHtml(item.name)}</span></td>
      <td class="points">${escapeHtml(item.points)}</td>
      <td class="alive">${escapeHtml(item.alive)}</td>
      <td class="cost">${formatCost(item.cost)}</td>
    `;
    tr.addEventListener("click", () => {
      opened.has(item.name) ? opened.delete(item.name) : opened.add(item.name);
      renderStandings();
    });
    standingsBody.appendChild(tr);

    if (opened.has(item.name)) {
      const details = document.querySelector("#participantTemplate").content.cloneNode(true);
      const roster = details.querySelector(".roster");
      (item.players || []).forEach(player => {
        const card = document.createElement("div");
        card.className = "player-card" + (player.alive ? "" : " out");
        card.innerHTML = `
          <div class="player-name">${escapeHtml(player.name)}</div>
          <div class="status">${escapeHtml(player.status)}</div>
          <div class="player-points">${escapeHtml(player.points)}</div>
        `;
        roster.appendChild(card);
      });
      standingsBody.appendChild(details);
    }
  });
}

function renderMatches() {
  const container = document.querySelector("#matches");
  container.innerHTML = "";
  (payload?.matches || []).forEach(match => {
    const el = document.createElement("div");
    el.className = "match";
    el.innerHTML = `
      <div class="winner">${escapeHtml(match.winner)}</div>
      <div class="score">${escapeHtml(match.score)}</div>
      <div class="loser">${escapeHtml(match.loser)}</div>
    `;
    container.appendChild(el);
  });
  if (!(payload?.matches || []).length) {
    container.innerHTML = `<div class="empty">Завершённые матчи пока не найдены.</div>`;
  }
}

function applyPayload(data, archived = false) {
  payload = data;
  opened.clear();
  searchInput.value = "";
  document.title = payload.site_title || payload.tournament || "Fantasy Darts";
  document.querySelector("#title").textContent = payload.site_title || payload.tournament || "Fantasy Darts";
  document.querySelector("#updated").textContent = formatUpdated(payload.updated_at, archived);
  document.querySelector("#matchCount").textContent = `Завершённых матчей: ${payload.matches_count || 0}`;
  document.querySelector("#modeLabel").textContent = archived ? "ARCHIVE RESULTS" : "LIVE RESULTS";
  document.querySelector("#archiveNotice").classList.toggle("hidden", !archived);
  renderStandings();
  renderMatches();
}

async function fetchJson(path) {
  const separator = path.includes("?") ? "&" : "?";
  const response = await fetch(path + separator + "t=" + Date.now(), { cache: "no-store" });
  if (!response.ok) throw new Error("Не удалось загрузить данные");
  return response.json();
}

function setNavigation(mode) {
  currentMode = mode;
  const isArchiveList = mode === "archive-list";
  tournamentSection.classList.toggle("hidden", isArchiveList);
  archiveSection.classList.toggle("hidden", !isArchiveList);
  currentButton.classList.toggle("active", mode === "live");
  archiveButton.classList.toggle("active", mode !== "live");
  refreshButton.classList.toggle("hidden", mode !== "live");
}

async function loadCurrent() {
  currentArchiveId = null;
  setNavigation("live");
  const data = await fetchJson("data.json");
  applyPayload(data, false);
  history.replaceState(null, "", location.pathname);
}

function formatArchiveDate(value) {
  if (!value) return "Дата архивирования неизвестна";
  return new Date(value).toLocaleString("ru-RU");
}

function renderArchiveList() {
  const list = document.querySelector("#archiveList");
  const empty = document.querySelector("#archiveEmpty");
  list.innerHTML = "";
  empty.classList.toggle("hidden", archiveItems.length !== 0);

  archiveItems.forEach(item => {
    const card = document.createElement("article");
    card.className = "archive-card";
    card.innerHTML = `
      <div class="archive-card-main">
        <h3>${escapeHtml(item.title || item.id)}</h3>
        <div class="archive-stats">
          <span>Победитель: <strong>${escapeHtml(item.winner || "—")}</strong></span>
          <span>Участников: <strong>${escapeHtml(item.participants || 0)}</strong></span>
          <span>Матчей: <strong>${escapeHtml(item.matches || 0)}</strong></span>
        </div>
        <div class="archive-date">Сохранён: ${escapeHtml(formatArchiveDate(item.archived_at))}</div>
      </div>
      <button class="open-archive">Открыть результаты</button>
    `;
    card.querySelector(".open-archive").addEventListener("click", () => openArchive(item));
    list.appendChild(card);
  });
}

async function showArchiveList() {
  setNavigation("archive-list");
  document.querySelector("#title").textContent = "Архив турниров";
  document.querySelector("#modeLabel").textContent = "FANTASY DARTS";
  document.querySelector("#updated").textContent = "Завершённые турниры";
  document.querySelector("#matchCount").textContent = "";
  archiveItems = await fetchJson("archive/index.json");
  if (!Array.isArray(archiveItems)) archiveItems = [];
  renderArchiveList();
  history.replaceState(null, "", location.pathname + "?view=archive");
}

async function openArchive(item) {
  currentArchiveId = item.id;
  setNavigation("archive-detail");
  const dataPath = item.data || `archive/${encodeURIComponent(item.id)}/data.json`;
  const data = await fetchJson(dataPath);
  applyPayload(data, true);
  history.replaceState(null, "", location.pathname + "?archive=" + encodeURIComponent(item.id));
}

async function openArchiveById(id) {
  archiveItems = await fetchJson("archive/index.json");
  if (!Array.isArray(archiveItems)) archiveItems = [];
  const item = archiveItems.find(entry => entry.id === id);
  if (!item) throw new Error("Турнир не найден в архиве");
  await openArchive(item);
}

refreshButton.addEventListener("click", () => loadCurrent().catch(showError));
currentButton.addEventListener("click", () => loadCurrent().catch(showError));
archiveButton.addEventListener("click", () => showArchiveList().catch(showError));
document.querySelector("#backToArchiveButton").addEventListener("click", () => showArchiveList().catch(showError));
searchInput.addEventListener("input", renderStandings);

document.querySelectorAll(".tab").forEach(button => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    button.classList.add("active");
    const view = button.dataset.view;
    document.querySelector("#standingsView").classList.toggle("hidden", view !== "standings");
    document.querySelector("#matchesView").classList.toggle("hidden", view !== "matches");
  });
});

function showError(error) {
  document.querySelector("#updated").textContent = error.message || "Ошибка загрузки";
}

async function initialise() {
  const params = new URLSearchParams(location.search);
  if (params.has("archive")) {
    await openArchiveById(params.get("archive"));
  } else if (params.get("view") === "archive") {
    await showArchiveList();
  } else {
    await loadCurrent();
  }
}

initialise().catch(showError);

setInterval(() => {
  if (currentMode === "live") loadCurrent().catch(() => {});
}, 60000);
