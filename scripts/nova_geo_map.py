#!/usr/bin/env python3
"""nova_geo_map.py — render a self-contained 'what's around me' radar of scanner/fire activity.

Polar radar centered on home: distance rings + compass, incidents plotted by distance & bearing,
colored by service. No map tiles (uses the distance/bearing we already have — CSP-safe). Writes a
standalone HTML file; publish it as an Artifact. Regenerable, so it can be refreshed on a schedule.
"""
import html
import json
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

MEM_DSN = "host=localhost dbname=nova_memories user=kochj"
OUT = Path(sys.argv[sys.argv.index("-o") + 1]) if "-o" in sys.argv else \
    Path("/private/tmp/claude-501/-Users-kochj/3070c430-736e-4d28-bd8b-cf2a2b7be3ae/scratchpad/nova_radar.html")
HOURS = 24
MAX_MI = 12   # clamp far (often garbled) geocodes to the outer ring


def fetch():
    con = psycopg2.connect(MEM_DSN); con.autocommit = True
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT source, text, created_at, (metadata->'geo'->>'nearest_mi')::float mi, "
        "metadata->'geo'->>'nearest_dir' dir FROM memories WHERE source IN ('scanner','fire') "
        "AND metadata->'geo'->>'nearest_mi' IS NOT NULL AND metadata->'geo'->>'nearest_dir' IS NOT NULL "
        "AND created_at > now() - interval '%d hours' "
        "ORDER BY (metadata->'geo'->>'nearest_mi')::float" % HOURS)
    import re
    rows = []
    for r in cur.fetchall():
        txt = re.sub(r"^\[.*?\]\s*", "", (r["text"] or "")).strip()[:140]
        rows.append({"svc": "fire" if r["source"] == "fire" else "police",
                     "mi": round(r["mi"], 1), "dir": r["dir"],
                     "t": r["created_at"].strftime("%a %H:%M"), "txt": txt})
    con.close()
    return rows


PAGE = """<title>Scanner Radar — Burbank</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(120% 120% at 50% 0%,#0d1416 0%,#080c0d 70%);
  color:#cfe3de;font:14px/1.5 ui-monospace,"SF Mono",Menlo,Consolas,monospace;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:26px 20px 60px}
h1{font-size:15px;letter-spacing:.22em;text-transform:uppercase;color:#7fa8a2;font-weight:600;margin:0 0 2px}
.sub{color:#4d6b66;font-size:12px;letter-spacing:.05em;margin-bottom:20px}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:22px}
.stat{background:#0e1719;border:1px solid #163230;border-radius:8px;padding:10px 14px;min-width:104px}
.stat .n{font-size:22px;font-variant-numeric:tabular-nums;color:#e6f4ef}
.stat .l{font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:#5a7b76;margin-top:2px}
.stat.fire .n{color:#ff9f43}.stat.police .n{color:#4aa8ff}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:26px;align-items:start}
@media(max-width:820px){.grid{grid-template-columns:1fr}}
.radar{width:100%;aspect-ratio:1;max-width:560px;margin:0 auto;display:block}
.legend{display:flex;gap:16px;justify-content:center;margin-top:10px;font-size:12px;color:#7fa8a2}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;vertical-align:middle}
.log{border:1px solid #163230;border-radius:10px;background:#0b1416;overflow:hidden}
.log h2{margin:0;padding:11px 14px;font-size:11px;letter-spacing:.16em;text-transform:uppercase;
  color:#7fa8a2;border-bottom:1px solid #163230;font-weight:600}
.rows{max-height:520px;overflow-y:auto}
.row{padding:9px 14px;border-bottom:1px solid #101e1f;display:grid;grid-template-columns:auto 1fr;gap:3px 10px}
.row:last-child{border-bottom:0}
.row .meta{grid-column:1;white-space:nowrap;font-variant-numeric:tabular-nums;font-size:11.5px}
.row .txt{grid-column:2;color:#9fbcb6;font-size:12px;overflow:hidden;text-overflow:ellipsis}
.row.fire .tag{color:#ff9f43}.row.police .tag{color:#4aa8ff}
.row .d{color:#5a7b76}
.foot{margin-top:22px;color:#3f5a56;font-size:11px;letter-spacing:.05em;text-align:center}
tspan,text{font:11px ui-monospace,Menlo,monospace}
</style>
<div class="wrap">
  <h1>Scanner Radar</h1>
  <div class="sub" id="sub">508 S Glenwood Pl · last 24h · distance &amp; bearing from home</div>
  <div class="stats" id="stats"></div>
  <div class="grid">
    <div><svg class="radar" id="radar" viewBox="0 0 600 600" role="img" aria-label="Radar of nearby scanner activity"></svg>
      <div class="legend">
        <span><span class="dot" style="background:#4aa8ff"></span>Police</span>
        <span><span class="dot" style="background:#ff9f43"></span>Fire</span>
        <span><span class="dot" style="background:#39ff14"></span>Home</span>
      </div></div>
    <div class="log"><h2>Incident Log · nearest first</h2><div class="rows" id="rows"></div></div>
  </div>
  <div class="foot" id="foot"></div>
</div>
<script>
const DATA = __DATA__;
const SVGNS="http://www.w3.org/2000/svg", R=600, C=R/2, MAXR=250, MAXMI=12;
const RINGS=[1,2,3,5,10], COL={police:"#4aa8ff",fire:"#ff9f43"};
const DIRDEG={N:0,NE:45,E:90,SE:135,S:180,SW:225,W:270,NW:315};
const rmi = mi => MAXR*Math.min(mi,MAXMI)/MAXMI;   // linear radius, clamp far
function el(n,a){const e=document.createElementNS(SVGNS,n);for(const k in a)e.setAttribute(k,a[k]);return e;}
const svg=document.getElementById("radar");
// rings + labels
for(const mi of RINGS){const r=rmi(mi);
  svg.appendChild(el("circle",{cx:C,cy:C,r,fill:"none",stroke:"#163230","stroke-width":1}));
  const lab=el("text",{x:C+4,y:C-r+13,fill:"#3f5a56"});lab.textContent=mi+" mi";svg.appendChild(lab);}
// crosshair + compass
for(const[dx,dy,lx,ly,txt] of [[0,-1,C,26,"N"],[1,0,R-14,C+4,"E"],[0,1,C,R-16,"S"],[-1,0,14,C+4,"W"]]){
  svg.appendChild(el("line",{x1:C,y1:C,x2:C+dx*MAXR,y2:C+dy*MAXR,stroke:"#122827","stroke-width":1}));
  const t=el("text",{x:lx,y:ly,fill:"#5a7b76","text-anchor":"middle"});t.textContent=txt;svg.appendChild(t);}
// sweep (respects reduced motion)
if(!matchMedia("(prefers-reduced-motion: reduce)").matches){
  const g=el("g",{}),grad=el("radialGradient",{id:"sw"});
  grad.innerHTML='<stop offset="0%" stop-color="#39ff14" stop-opacity="0.16"/><stop offset="100%" stop-color="#39ff14" stop-opacity="0"/>';
  const defs=el("defs",{});defs.appendChild(grad);svg.appendChild(defs);
  const wedge=el("path",{d:`M${C} ${C} L${C} ${C-MAXR} A${MAXR} ${MAXR} 0 0 1 ${C+MAXR*Math.sin(0.5)} ${C-MAXR*Math.cos(0.5)} Z`,fill:"url(#sw)"});
  g.appendChild(wedge);svg.appendChild(g);
  const an=el("animateTransform",{attributeName:"transform",type:"rotate",from:`0 ${C} ${C}`,to:`360 ${C} ${C}`,dur:"7s",repeatCount:"indefinite"});g.appendChild(an);}
// incidents
for(const d of DATA){const deg=(DIRDEG[d.dir]||0)*Math.PI/180, r=rmi(d.mi);
  const x=C+r*Math.sin(deg), y=C-r*Math.cos(deg);
  const c=el("circle",{cx:x,cy:y,r:d.mi<2.5?6:4.5,fill:COL[d.svc],opacity:0.9,stroke:"#080c0d","stroke-width":1});
  const tip=el("title",{});tip.textContent=`${d.t} · ${d.svc} · ~${d.mi} mi ${d.dir}\n${d.txt}`;c.appendChild(tip);
  svg.appendChild(c);}
// home
svg.appendChild(el("circle",{cx:C,cy:C,r:5,fill:"#39ff14"}));
svg.appendChild(el("circle",{cx:C,cy:C,r:9,fill:"none",stroke:"#39ff14","stroke-width":1,opacity:0.5}));
// stats
const nf=DATA.filter(d=>d.svc==="fire").length, np=DATA.length-nf;
const near=DATA.length?DATA[0]:null, close=DATA.filter(d=>d.mi<=2.5).length;
document.getElementById("stats").innerHTML=
  `<div class="stat"><div class="n">${DATA.length}</div><div class="l">Located 24h</div></div>`+
  `<div class="stat police"><div class="n">${np}</div><div class="l">Police</div></div>`+
  `<div class="stat fire"><div class="n">${nf}</div><div class="l">Fire</div></div>`+
  `<div class="stat"><div class="n">${near?near.mi:"—"}<span style="font-size:12px"> mi</span></div><div class="l">Nearest ${near?near.dir:""}</div></div>`+
  `<div class="stat"><div class="n">${close}</div><div class="l">Within 2.5 mi</div></div>`;
// log
document.getElementById("rows").innerHTML=DATA.map(d=>
  `<div class="row ${d.svc}"><div class="meta"><span class="tag">${d.svc==="fire"?"FIRE":"LAPD"}</span> <span class="d">${d.t} · ~${d.mi}mi ${d.dir}</span></div><div class="txt">${d.txt.replace(/[<>&]/g,"")}</div></div>`).join("");
document.getElementById("foot").textContent=`${DATA.length} geocodable transmissions · rings at 1/2/3/5/10 mi · far outliers clamped to edge (garbled addresses)`;
</script>"""


def main():
    rows = fetch()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(PAGE.replace("__DATA__", json.dumps(rows)))
    print(f"[geo-map] wrote {OUT} ({len(rows)} incidents)", flush=True)


if __name__ == "__main__":
    main()
