// Ra'no · карта цен на квартиры в Ташкенте: сетка медиан $/м² по срезу объявлений о продаже.
// Данные кладёт Python после каждого среза (/svc/pricemap), страница — /map, данные — /map.json.

export const MAP_HTML = `<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Карта цен · Ra'no</title>
<meta name="description" content="Сколько стоит м² в разных частях районов Ташкента — по объявлениям о продаже">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{--bg:#f6f7f9;--panel:#ffffffee;--ink:#1d2330;--muted:#5d6675;--line:#dfe3ea;--accent:#2f6fde;--chip:#eef1f6}
@media (prefers-color-scheme:dark){:root{--bg:#0f141b;--panel:#171d26ee;--ink:#e8edf3;--muted:#9aa5b4;--line:#2a3340;--accent:#6aa7ff;--chip:#222a35}}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);
font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans",sans-serif}
#map{position:absolute;inset:0}
.panel{position:absolute;z-index:500;left:12px;right:12px;top:12px;max-width:420px;background:var(--panel);
backdrop-filter:blur(8px);border:1px solid var(--line);border-radius:14px;padding:12px 14px;box-shadow:0 4px 18px #0002}
h1{font-size:16px;margin:0 0 2px}.sub{color:var(--muted);font-size:12.5px;margin:0 0 10px}
.seg{display:flex;gap:6px;margin-bottom:10px}.seg button{flex:1;border:1px solid var(--line);background:var(--chip);color:var(--ink);
border-radius:9px;padding:7px 4px;font:inherit;font-size:13px;cursor:pointer}.seg button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.legend{display:flex;align-items:stretch;gap:2px}.legend div{flex:1;height:8px;border-radius:2px}
.lab{display:flex;justify-content:space-between;color:var(--muted);font-size:11.5px;margin-top:3px}
.foot{position:absolute;z-index:500;left:12px;bottom:18px;max-width:420px;right:64px;color:var(--muted);font-size:11.5px;
background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:6px 10px}
.foot a{color:var(--accent)}
.leaflet-tile-pane{filter:saturate(.35) contrast(.95)}
@media (prefers-color-scheme:dark){.leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.85) saturate(.3)}}
.leaflet-popup-content{font:13px/1.45 inherit;margin:10px 12px}.pp b{font-size:15px}
.pp .m{color:#5d6675}.empty{position:absolute;inset:0;display:grid;place-items:center;z-index:400;color:var(--muted)}
</style></head><body>
<div id="map"></div>
<div class="panel">
  <h1>Сколько стоит м² в Ташкенте</h1>
  <p class="sub" id="sub">Загружаю…</p>
  <div class="seg" role="tablist">
    <button data-k="all" class="on">Все</button><button data-k="old">Вторичка</button><button data-k="new">Новостройки</button>
  </div>
  <div class="legend" id="lg"></div><div class="lab" id="lb"></div>
</div>
<div class="foot">Цены <b>предложения</b> в объявлениях, не сделок. Клетка ~700 м, от 3 объявлений.
Нажмите на клетку — сравнение с районом. <a href="https://t.me/rano_smart_bot">Ra'no</a></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const COLORS=["#2a9d8f","#8ab17d","#e9c46a","#f4a261","#e76f51","#b5233d"];
const dark=matchMedia("(prefers-color-scheme: dark)").matches;
const q=new URLSearchParams(location.search), p=(q.get("p")||"").split(",").map(Number);
const map=L.map("map",{zoomControl:false,attributionControl:true}).setView([41.3,69.27],12);
L.control.zoom({position:"bottomright"}).addTo(map);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  {maxZoom:19,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'}).addTo(map);
if(p.length===2&&p.every(isFinite)){map.setView(p,15);L.circleMarker(p,{radius:8,color:"#fff",weight:3,fillColor:"#2f6fde",fillOpacity:1}).addTo(map).bindTooltip("Эта квартира",{permanent:true,direction:"top",offset:[0,-8]});}
const fmt=v=>"$"+Math.round(v).toLocaleString("ru-RU").replace(/,/g," ");
let D=null,layer=L.layerGroup().addTo(map),seg="all",cuts=[];
function quant(vals){vals=[...vals].sort((a,b)=>a-b);return [1,2,3,4,5].map(i=>vals[Math.floor(vals.length*i/6)]);}
function color(v){let i=0;while(i<cuts.length&&v>cuts[i])i++;return COLORS[i];}
function draw(){
  layer.clearLayers();
  const vals=D.cells.map(c=>c[seg]).filter(Boolean); if(!vals.length)return;
  cuts=quant(vals);
  document.getElementById("lg").innerHTML=COLORS.map(c=>'<div style="background:'+c+'"></div>').join("");
  document.getElementById("lb").innerHTML="<span>"+fmt(Math.min(...vals))+"</span><span>"+fmt(cuts[2])+"</span><span>"+fmt(Math.max(...vals))+"/м²</span>";
  for(const c of D.cells){const v=c[seg]; if(!v)continue;
    const dm=(D.districts[c.d]||{}).m2, diff=dm?Math.round((v/dm-1)*100):null;
    const r=L.rectangle([[c.b[0],c.b[1]],[c.b[2],c.b[3]]],{weight:0,fillColor:color(v),fillOpacity:dark?.55:.5}).addTo(layer);
    r.bindPopup('<div class="pp"><b>'+fmt(v)+'/м²</b> · '+(seg==="all"?c.n:seg==="new"?c.nn:c.no)+' объявл.<br>'+
      (c.old?'Вторичка '+fmt(c.old)+' ('+c.no+')<br>':'')+(c.new?'Новостройки '+fmt(c.new)+' ('+c.nn+')<br>':'')+
      (c.d?'<span class="m">'+c.d+(diff!==null?': '+(diff>0?'+':'')+diff+'% к медиане района ('+fmt(dm)+')':'')+'</span>':'')+'</div>');}
  for(const m of D.metro||[])L.circleMarker([m[1],m[2]],{radius:3,color:dark?"#ddd":"#333",weight:1,fillOpacity:1}).addTo(layer).bindTooltip("м. "+m[0]);
}
document.querySelectorAll(".seg button").forEach(b=>b.onclick=()=>{document.querySelectorAll(".seg button").forEach(x=>x.classList.toggle("on",x===b));seg=b.dataset.k;draw();});
fetch("map.json").then(r=>r.json()).then(d=>{
  if(!d||!d.cells){document.getElementById("sub").textContent="Карта появится после ближайшего среза рынка.";return;}
  D=d; const at=(d.at||"").slice(0,10).split("-").reverse().join(".");
  document.getElementById("sub").textContent="Медиана $/м² по "+d.cells.reduce((s,c)=>s+c.n,0)+" объявлениям о продаже"+(at?", срез "+at:"")+(d.city_m2?" · по городу "+fmt(d.city_m2):"");
  draw();
}).catch(()=>document.getElementById("sub").textContent="Не получилось загрузить данные — обновите страницу.");
</script></body></html>`;
