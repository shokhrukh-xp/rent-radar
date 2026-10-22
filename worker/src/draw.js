// Ra'no · «Обвести на карте»: клиент пальцем рисует области, внутри которых искать квартиру.
// Открывается из кнопки «Меню» в чате (Telegram Mini App). Сохранение — POST /draw/save с initData:
// воркер проверяет подпись Telegram, кладёт области в kv и передаёт их Python (поиск — только внутри).

export const DRAW_HTML = `<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Где искать · Ra'no</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#f6f7f9;--panel:#ffffffee;--ink:#1d2330;--muted:#5d6675;--line:#dfe3ea;--accent:#2f6fde;--chip:#eef1f6;--ok:#1f9d55;--warn:#d64545}
@media (prefers-color-scheme:dark){:root{--bg:#0f141b;--panel:#171d26ee;--ink:#e8edf3;--muted:#9aa5b4;--line:#2a3340;--accent:#6aa7ff;--chip:#222a35}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);overscroll-behavior:none;
font:15px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans",sans-serif}
#map{position:absolute;inset:0}
#map.drawing{cursor:crosshair}
.hint{position:absolute;z-index:600;left:12px;right:12px;top:10px;background:var(--panel);backdrop-filter:blur(8px);
border:1px solid var(--line);border-radius:14px;padding:10px 12px;box-shadow:0 4px 18px #0002;font-size:14px}
.hint b{display:block;font-size:15px;margin-bottom:2px}.hint .m{color:var(--muted);font-size:13px}
.bar{position:absolute;z-index:600;left:10px;right:10px;bottom:calc(12px + env(safe-area-inset-bottom));display:flex;gap:8px}
.bar button{flex:1;min-height:48px;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:12px;
font:inherit;font-size:15px;font-weight:600;backdrop-filter:blur(8px);box-shadow:0 2px 10px #0002}
.bar button.main{background:var(--accent);border-color:var(--accent);color:#fff;flex:1.6}
.bar button.on{background:var(--ok);border-color:var(--ok);color:#fff}
.bar button:disabled{opacity:.45}
.leaflet-tile-pane{filter:saturate(.55)}
@media (prefers-color-scheme:dark){.leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.85) saturate(.4)}}
.toast{position:absolute;z-index:700;left:50%;top:50%;transform:translate(-50%,-50%);background:var(--panel);border:1px solid var(--line);
border-radius:14px;padding:14px 18px;font-weight:600;display:none;max-width:80%;text-align:center}
</style></head><body>
<div id="map"></div>
<div class="hint" id="hint"><b>Где искать квартиру</b><span class="m">Нажмите «Обвести» и проведите пальцем по краю нужного места.
Можно несколько областей. Ищу только внутри — и только объявления с точным адресом.</span></div>
<div class="bar">
  <button id="draw" class="main">✏️ Обвести</button>
  <button id="undo" title="Убрать последнюю">↶</button>
  <button id="clear" title="Убрать все">🗑</button>
  <button id="save">✅ Сохранить</button>
</div>
<div class="toast" id="toast"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const tg = window.Telegram && Telegram.WebApp;
try { tg.ready(); tg.expand(); tg.disableVerticalSwipes && tg.disableVerticalSwipes(); } catch (e) {}
const T = {
  ru: { draw: "✏️ Обвести", drawing: "Ведите пальцем…", save: "✅ Сохранить", saved: "Сохранила — ищу внутри областей",
        cleared: "Области убраны — ищу как раньше", err: "Не получилось сохранить — попробуйте ещё раз", n: n => "Областей: " + n,
        title: "Где искать квартиру", sub: "Нажмите «Обвести» и проведите пальцем по краю нужного места. Можно несколько областей. Ищу только внутри — и только объявления с точным адресом.",
        small: "Слишком маленькая область — обведите крупнее" },
  uz: { draw: "✏️ Chizish", drawing: "Barmog'ingiz bilan chizing…", save: "✅ Saqlash", saved: "Saqlandi — faqat shu joylarda qidiraman",
        cleared: "Hududlar olib tashlandi — avvalgidek qidiraman", err: "Saqlanmadi — yana urinib ko'ring", n: n => "Hududlar: " + n,
        title: "Qayerdan qidiramiz", sub: "«Chizish»ni bosing va kerakli joy chegarasi bo'ylab barmog'ingizni yurgizing. Bir nechta hudud mumkin. Faqat ichidan, aniq manzilli e'lonlarni qidiraman.",
        small: "Hudud juda kichik — kattaroq chizing" },
  en: { draw: "✏️ Draw", drawing: "Trace with your finger…", save: "✅ Save", saved: "Saved — searching inside the areas",
        cleared: "Areas removed — searching as before", err: "Couldn't save — please try again", n: n => "Areas: " + n,
        title: "Where to search", sub: "Tap «Draw» and trace the edge of the place with your finger. Several areas are fine. I search only inside — and only listings with an exact address.",
        small: "Area too small — draw a bigger one" },
};
const lang = (new URLSearchParams(location.search).get("lang") || (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.language_code) || "ru").slice(0, 2);
const L_ = T[lang] || T.ru;
document.querySelector("#hint").innerHTML = "<b>" + L_.title + "</b><span class='m'>" + L_.sub + "</span>";
const $ = s => document.querySelector(s);
$("#draw").textContent = L_.draw; $("#save").textContent = L_.save;
const map = L.map("map", { zoomControl: false, attributionControl: true }).setView([41.3111, 69.2797], 12);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(map);
const style = { color: "#2f6fde", weight: 3, fillColor: "#2f6fde", fillOpacity: 0.18 };
let areas = [];                    // [[ [lat, lon], ... ], ...]
const layers = [];
function toast(msg, ms = 1600) { const t = $("#toast"); t.textContent = msg; t.style.display = "block"; setTimeout(() => t.style.display = "none", ms); }
function redraw() {
  layers.splice(0).forEach(l => map.removeLayer(l));
  areas.forEach(a => layers.push(L.polygon(a, style).addTo(map)));
  $("#undo").disabled = $("#clear").disabled = !areas.length;
}
function fit() { if (areas.length) map.fitBounds(L.featureGroup(layers).getBounds(), { padding: [40, 40] }); }
// рисование пальцем: пока режим включён, карта не двигается, а точки касания собираются в линию
let drawing = false, pts = [], line = null, lastPx = null;
function setDrawing(on) {
  drawing = on; $("#map").classList.toggle("drawing", on);
  $("#draw").classList.toggle("on", on); $("#draw").textContent = on ? L_.drawing : L_.draw;
  if (on) { map.dragging.disable(); map.touchZoom.disable(); map.doubleClickZoom.disable(); map.boxZoom.disable(); }
  else { map.dragging.enable(); map.touchZoom.enable(); map.doubleClickZoom.enable(); map.boxZoom.enable(); }
}
const el = map.getContainer();
function pos(e) { const t = e.touches ? e.touches[0] : e; const r = el.getBoundingClientRect(); return L.point(t.clientX - r.left, t.clientY - r.top); }
function start(e) {
  if (!drawing) return; e.preventDefault();
  pts = []; lastPx = null; add(pos(e));
  line = L.polyline([], { color: "#2f6fde", weight: 3 }).addTo(map);
}
function add(p) {
  if (lastPx && lastPx.distanceTo(p) < 6) return;
  lastPx = p; pts.push(map.containerPointToLatLng(p)); if (line) line.setLatLngs(pts);
}
function move(e) { if (!drawing || !line) return; e.preventDefault(); add(pos(e)); }
function end(e) {
  if (!drawing || !line) return;
  map.removeLayer(line); line = null;
  let a = pts.map(p => [+p.lat.toFixed(5), +p.lng.toFixed(5)]);
  if (a.length > 300) { const k = Math.ceil(a.length / 300); a = a.filter((_, i) => i % k === 0); }
  const b = L.latLngBounds(a);
  if (a.length < 4 || map.distance(b.getNorthWest(), b.getSouthEast()) < 200) { toast(L_.small); }
  else { areas.push(a); redraw(); tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred("light"); }
  setDrawing(false);
}
el.addEventListener("touchstart", start, { passive: false }); el.addEventListener("touchmove", move, { passive: false });
el.addEventListener("touchend", end); el.addEventListener("mousedown", start); el.addEventListener("mousemove", e => { if (e.buttons) move(e); });
el.addEventListener("mouseup", end);
$("#draw").onclick = () => setDrawing(!drawing);
$("#undo").onclick = () => { areas.pop(); redraw(); };
$("#clear").onclick = () => { areas = []; redraw(); };
const initData = (tg && tg.initData) || "";
async function api(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ initData, ...body }) });
  return r.json().catch(() => ({}));
}
$("#save").onclick = async () => {
  $("#save").disabled = true;
  const r = await api("/draw/save", { areas });
  $("#save").disabled = false;
  if (!r.ok) return toast(L_.err, 2400);
  toast(areas.length ? L_.saved : L_.cleared);
  setTimeout(() => { try { tg.close(); } catch (e) {} }, 900);
};
api("/draw/load", {}).then(r => { if (r.ok && Array.isArray(r.areas)) { areas = r.areas; redraw(); fit(); } else redraw(); });
</script></body></html>`;
