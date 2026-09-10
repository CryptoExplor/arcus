"""Zero-dependency monitoring dashboard (stdlib http.server).

Serves:
  GET /            single-page live dashboard (auto-refreshing)
  GET /api/status  the engine status JSON
  GET /api/report  the PnL report JSON
  GET /healthz     liveness

Started automatically when BOT_METRICS_PORT is set. Binds 0.0.0.0 so it works
behind a preview proxy.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

log = logging.getLogger("arcusbot.dashboard")

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Arcus Testnet Bot</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0e14;--panel:#141922;--line:#232b39;--fg:#e6edf3;--dim:#8b98ab;
--good:#3fb950;--bad:#f85149;--warn:#d29922;--acc:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;
gap:16px;align-items:center;flex-wrap:wrap}
h1{font-size:15px;margin:0;letter-spacing:.5px}
.badge{padding:2px 8px;border-radius:10px;border:1px solid var(--line);font-size:12px;color:var(--dim)}
.badge.live{color:var(--good);border-color:var(--good)}
.badge.halt{color:var(--bad);border-color:var(--bad)}
main{padding:18px;display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}
.card h2{font-size:12px;margin:0 0 10px;color:var(--dim);text-transform:uppercase;letter-spacing:1px}
.kv{display:flex;justify-content:space-between;gap:10px;padding:3px 0}
.kv span:last-child{color:var(--fg);font-weight:600}
.big{font-size:26px;font-weight:700;margin:4px 0}
.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}.dim{color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:var(--dim);font-weight:500;padding:4px 6px;border-bottom:1px solid var(--line)}
td{padding:4px 6px;border-bottom:1px solid #1b212c}
.wide{grid-column:1/-1}
footer{padding:10px 20px;color:var(--dim);font-size:12px}
</style></head><body>
<header>
  <h1>ARCUS TESTNET BOT</h1>
  <span class="badge" id="venue">-</span>
  <span class="badge" id="mode">-</span>
  <span class="badge" id="strategy">-</span>
  <span class="badge" id="risk">-</span>
  <span class="badge" id="uptime">-</span>
</header>
<main>
  <div class="card"><h2>Net PnL (after fees)</h2>
    <div class="big" id="net">-</div>
    <div class="kv"><span>gross</span><span id="gross">-</span></div>
    <div class="kv"><span>fees paid</span><span id="fees">-</span></div>
    <div class="kv"><span>rebates</span><span id="rebates">-</span></div>
    <div class="kv"><span>fee coverage</span><span id="cover">-</span></div>
    <div class="kv"><span>net bps of volume</span><span id="bps">-</span></div>
  </div>
  <div class="card"><h2>Volume</h2>
    <div class="big" id="vol">-</div>
    <div class="kv"><span>per hour</span><span id="volh">-</span></div>
    <div class="kv"><span>maker share</span><span id="maker">-</span></div>
    <div class="kv"><span>fills</span><span id="fills">-</span></div>
    <div class="kv"><span>orders / cancels</span><span id="oc">-</span></div>
  </div>
  <div class="card"><h2>Risk</h2>
    <div class="kv"><span>state</span><span id="rstate">-</span></div>
    <div class="kv"><span>equity</span><span id="equity">-</span></div>
    <div class="kv"><span>free collateral</span><span id="freecoll">-</span></div>
    <div class="kv"><span>drawdown</span><span id="dd">-</span></div>
    <div class="kv"><span>orders/min</span><span id="opm">-</span></div>
    <div class="kv"><span>rejects</span><span id="rej">-</span></div>
    <div class="kv"><span>reasons</span><span id="reasons" class="dim">-</span></div>
  </div>
  <div class="card"><h2>Fees &amp; connectivity</h2>
    <div class="kv"><span>tier</span><span id="tier">-</span></div>
    <div class="kv"><span>maker / taker</span><span id="mt">-</span></div>
    <div class="kv"><span>ws connected</span><span id="wsc">-</span></div>
    <div class="kv"><span>ws reconnects</span><span id="wsr">-</span></div>
    <div class="kv"><span>ip weight left</span><span id="ipb">-</span></div>
  </div>
  <div class="card wide"><h2>Markets</h2>
    <table><thead><tr><th>market</th><th>bid</th><th>ask</th><th>spread bps</th>
    <th>req edge</th><th>position</th><th>entry</th><th>quotes</th><th>cycles</th>
    <th>net</th><th>volume</th></tr></thead><tbody id="mk"></tbody></table>
  </div>
</main>
<footer id="ft">loading…</footer>
<script>
const f=(x,d=2)=>x===null||x===undefined?'-':Number(x).toLocaleString(undefined,{maximumFractionDigits:d});
const money=x=>x===null||x===undefined?'-':'$'+f(x,4);
const cls=v=>Number(v)>0?'good':(Number(v)<0?'bad':'dim');
async function tick(){
 try{
  const s=await (await fetch('/api/status',{cache:'no-store'})).json();
  const p=s.pnl,r=s.risk;
  document.getElementById('venue').textContent=s.venue+' / '+s.network;
  const m=document.getElementById('mode');m.textContent=s.mode;m.className='badge'+(s.mode==='live'?' live':'');
  document.getElementById('strategy').textContent=s.strategy;
  const rb=document.getElementById('risk');rb.textContent=r.state;
  rb.className='badge'+(r.state==='HALT'?' halt':(r.state==='OK'?' live':''));
  document.getElementById('uptime').textContent=f(s.uptimeSeconds,0)+'s · '+s.loops+' loops';
  const net=document.getElementById('net');net.textContent=money(p.netPnl);net.className='big '+cls(p.netPnl);
  document.getElementById('gross').textContent=money(p.grossPnl);
  document.getElementById('fees').textContent=money(p.feesPaid);
  document.getElementById('rebates').textContent=money(p.rebatesEarned);
  const cv=document.getElementById('cover');cv.textContent=f(p.feeCoverageRatio,2)+'x';
  cv.className=Number(p.feeCoverageRatio)>=1?'good':'bad';
  const bp=document.getElementById('bps');bp.textContent=f(p.netBpsOfVolume,3)+' bps';bp.className=cls(p.netBpsOfVolume);
  document.getElementById('vol').textContent='$'+f(p.volumeUsd,2);
  document.getElementById('volh').textContent='$'+f(p.volumePerHourUsd,2);
  document.getElementById('maker').textContent=f(p.makerShare,1)+'%';
  document.getElementById('fills').textContent=p.fillCount;
  document.getElementById('oc').textContent=s.ordersSent+' / '+s.cancelsSent;
  document.getElementById('rstate').textContent=r.state;
  document.getElementById('equity').textContent=money(r.equity);
  document.getElementById('freecoll').textContent=money(r.freeCollateral);
  document.getElementById('dd').textContent=money(p.drawdown);
  document.getElementById('opm').textContent=r.ordersLastMinute;
  document.getElementById('rej').textContent=s.rejects+' ('+Object.entries(r.rejections||{}).map(([k,v])=>k+':'+v).join(', ')+')';
  document.getElementById('reasons').textContent=(r.reasons||[]).join('; ')||(r.haltReason||'none');
  document.getElementById('tier').textContent=p.fees.level+' '+p.fees.name+' ('+p.fees.source+')';
  document.getElementById('mt').textContent=p.fees.maker_bps+' / '+p.fees.taker_bps+' bps';
  document.getElementById('wsc').textContent=s.ws?(s.ws.connected?'yes':'no'):'n/a';
  document.getElementById('wsr').textContent=s.ws?s.ws.reconnects:'-';
  document.getElementById('ipb').textContent=f(s.ipBudget.tokens,0)+' / '+f(s.ipBudget.capacity,0);
  const byMkt={};(p.markets||[]).forEach(x=>byMkt[x.market]=x);
  document.getElementById('mk').innerHTML=(s.markets||[]).map(x=>{
    const b=byMkt[x.market]||{};
    return `<tr><td>${x.market}</td><td>${f(x.bestBid,4)}</td><td>${f(x.bestAsk,4)}</td>
    <td>${f(x.spreadBps,2)}</td><td>${f(x.requiredEdgeBps,2)}</td>
    <td class="${cls(x.position)}">${f(x.position,6)}</td><td>${f(x.avgEntry,4)}</td>
    <td>${x.liveQuotes}</td><td>${x.cycles}</td>
    <td class="${cls(b.net)}">${money(b.net)}</td><td>$${f(b.volume,2)}</td></tr>`;}).join('');
  document.getElementById('ft').textContent='updated '+new Date().toLocaleTimeString()+
    ' · session '+p.sessionId+(s.exitReason?(' · STOPPED: '+s.exitReason):'');
 }catch(e){document.getElementById('ft').textContent='status unavailable: '+e;}
}
tick();setInterval(tick,2000);
</script></body></html>"""


def start_dashboard(host: str, port: int, status_fn: Callable[[], dict[str, Any]]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            try:
                if path in {"/", "/index.html"}:
                    self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                elif path == "/api/status":
                    self._send(200, json.dumps(status_fn(), default=str).encode(), "application/json")
                elif path == "/api/report":
                    self._send(200, json.dumps(status_fn().get("pnl", {}), default=str).encode(),
                               "application/json")
                elif path == "/healthz":
                    self._send(200, b'{"ok":true}', "application/json")
                else:
                    self._send(404, b'{"error":"not found"}', "application/json")
            except Exception as exc:  # keep the server alive
                self._send(500, json.dumps({"error": str(exc)}).encode(), "application/json")

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="dashboard", daemon=True)
    thread.start()
    log.info("dashboard listening on http://%s:%d", host, port)
    return server
