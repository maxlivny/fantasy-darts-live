let payload = null;
let opened = new Set();

const standingsBody = document.querySelector("#standings");
const searchInput = document.querySelector("#search");

function formatCost(value) {
  return Number(value || 0).toLocaleString("ru-RU", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
}

function formatUpdated(value) {
  if (!value) return "Время обновления неизвестно";
  return "Обновлено: " + new Date(value).toLocaleString("ru-RU");
}

function renderStandings() {
  const query = searchInput.value.trim().toLocaleLowerCase("ru");
  standingsBody.innerHTML = "";
  const rows = (payload?.standings || []).filter(item =>
    item.name.toLocaleLowerCase("ru").includes(query)
  );

  document.querySelector("#empty").classList.toggle("hidden", rows.length !== 0);

  rows.forEach(item => {
    const tr = document.createElement("tr");
    tr.className = "main-row" + (item.rating ? " rating" : "");
    tr.innerHTML = `
      <td>${item.place}</td>
      <td><span class="name">${item.name}</span></td>
      <td class="points">${item.points}</td>
      <td class="alive">${item.alive}</td>
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
      item.players.forEach(player => {
        const card = document.createElement("div");
        card.className = "player-card" + (player.alive ? "" : " out");
        card.innerHTML = `
          <div class="player-name">${player.name}</div>
          <div class="status">${player.status}</div>
          <div class="player-points">${player.points}</div>
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
      <div class="winner">${match.winner}</div>
      <div class="score">${match.score}</div>
      <div class="loser">${match.loser}</div>
    `;
    container.appendChild(el);
  });
  if (!(payload?.matches || []).length) {
    container.innerHTML = `<div class="empty">Завершённые матчи пока не найдены.</div>`;
  }
}

async function loadData() {
  const response = await fetch("data.json?t=" + Date.now(), { cache: "no-store" });
  if (!response.ok) throw new Error("Не удалось загрузить таблицу");
  payload = await response.json();
  document.title = payload.site_title || payload.tournament;
  document.querySelector("#title").textContent = payload.site_title || payload.tournament;
  document.querySelector("#updated").textContent = formatUpdated(payload.updated_at);
  document.querySelector("#matchCount").textContent = `Завершённых матчей: ${payload.matches_count || 0}`;
  renderStandings();
  renderMatches();
}

document.querySelector("#refreshButton").addEventListener("click", () => loadData());
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

loadData().catch(error => {
  document.querySelector("#updated").textContent = error.message;
});

setInterval(() => loadData().catch(() => {}), 60000);
