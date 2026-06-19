// Cam AI — visor móvil: video con esqueleto + panel de eventos (2 juntas + género) + PTZ
const $ = (s) => document.querySelector(s);

// ---- video MJPEG (cámara 1) ----
const cam = $("#cam");
function loadCam(){ cam.src = "/stream/1?t=" + Date.now(); }
cam.addEventListener("error", () => setTimeout(loadCam, 1500));
loadCam();

// ---- estado en vivo + eventos ----
const peopleEl = $("#people"), togetherEl = $("#together"), statusEl = $("#status");
const eventsEl = $("#events"), evCountEl = $("#evCount");
let lastIds = "";

function genderTag(list){
  if(!list || !list.length) return "";
  return list.map(g => {
    const cls = g.toLowerCase().startsWith("h") ? "m" : "f";
    return `<span class="${cls}">${g}</span>`;
  }).join(" + ");
}

function renderEvents(events){
  const ids = events.map(e => e.id).join(",");
  if(ids === lastIds) return;
  lastIds = ids;
  evCountEl.textContent = events.length;
  if(!events.length){
    eventsEl.innerHTML = '<p class="empty">Aún sin detecciones de “2 personas juntas”.</p>';
    return;
  }
  eventsEl.innerHTML = events.map(e => `
    <div class="ev" data-img="${e.img}" data-info="${e.time} · ${e.people} pers. ${e.genders.join(', ')}">
      <img src="${e.img}" loading="lazy" />
      <div class="meta">
        <span class="t">${e.time}</span>
        <span class="g">${genderTag(e.genders) || (e.people + ' pers.')}</span>
      </div>
    </div>`).join("");
}

async function poll(){
  try{
    const r = await fetch("/api/events"); const d = await r.json();
    statusEl.className = "dot ok";
    const live = d.live || {};
    peopleEl.textContent = "👤 " + (live.people ?? 0);
    togetherEl.classList.toggle("off", !live.together);
    renderEvents(d.events || []);
  }catch(e){ statusEl.className = "dot bad"; }
}
poll(); setInterval(poll, 1000);

// ---- visor de captura ----
const viewer = $("#viewer"), viewerImg = $("#viewerImg"), viewerInfo = $("#viewerInfo");
eventsEl.addEventListener("click", (e) => {
  const card = e.target.closest(".ev"); if(!card) return;
  viewerImg.src = card.dataset.img;
  viewerInfo.textContent = card.dataset.info;
  viewer.showModal();
});
$("#viewerClose").addEventListener("click", () => viewer.close());
viewer.addEventListener("click", (e) => { if(e.target === viewer) viewer.close(); });

// ---- PTZ (mantener pulsado) ----
const ptzMsg = $("#ptzMsg");
async function ptz(action){
  try{
    const r = await fetch("/api/ptz/" + action, {method:"POST"});
    const d = await r.json();
    if(!d.ok && d.error) ptzMsg.textContent = d.error;
  }catch(e){ ptzMsg.textContent = "PTZ sin conexión"; }
}
document.querySelectorAll(".ptz button[data-act]").forEach(btn => {
  const act = btn.dataset.act;
  if(act === "stop"){ btn.addEventListener("click", () => ptz("stop")); return; }
  const start = (e) => { e.preventDefault(); ptz(act); };
  const end = (e) => { e.preventDefault(); ptz("stop"); };
  btn.addEventListener("touchstart", start, {passive:false});
  btn.addEventListener("touchend", end);
  btn.addEventListener("mousedown", start);
  btn.addEventListener("mouseup", end);
  btn.addEventListener("mouseleave", end);
});
