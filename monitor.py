#!/usr/bin/env python3
"""
Ultra-lightweight server monitoring panel (Python 3 only, zero dependencies).

- A tiny background thread reads /proc every 10 seconds and appends one line per
  minute to a history file (about 3 MB for 30 days). Data keeps being collected
  even when the web page is closed.
- The port and the secret token are generated randomly on the first run and saved
  to monitor_config.json, so the same URL comes back after every restart.
- Open the page any time: "Live" tab + history for 1 hour up to 30 days.

Usage:    python3 monitor.py
Optional: python3 monitor.py --port 8080 --token abc123 --days 30 --interval 10
"""
import argparse, json, os, random, secrets, socket, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
SKIP_PREFIX = ("lo", "veth", "docker", "br-", "virbr", "tun", "tap")
RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}
DATA_PATH = os.path.join(BASE, "monitor_history.csv")


def read(path):
    with open(path) as f:
        return f.read()


def stats():
    p = [int(x) for x in read("/proc/stat").split("\n", 1)[0].split()[1:9]]
    cpu = [sum(p), p[3] + p[4]]  # total, idle(+iowait)

    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            m[k] = int(v.split()[0]) * 1024

    ifaces, rx_t, tx_t = {}, 0, 0
    for line in read("/proc/net/dev").splitlines()[2:]:
        name, data = line.split(":", 1)
        name = name.strip()
        if name.startswith(SKIP_PREFIX):
            continue
        f = data.split()
        rx, tx = int(f[0]), int(f[8])
        ifaces[name] = [rx, tx]
        rx_t += rx
        tx_t += tx

    st = os.statvfs("/")
    disk = [st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bavail) * st.f_frsize]

    return {
        "t": time.time(),
        "cpu": cpu,
        "mem": m,
        "net": {"rx": rx_t, "tx": tx_t, "ifaces": ifaces},
        "load": read("/proc/loadavg").split()[:3],
        "uptime": float(read("/proc/uptime").split()[0]),
        "cores": os.cpu_count(),
        "disk": disk,
    }


# ----------------------------------------------------------------- history --
def prune(path, days):
    cut = time.time() - days * 86400
    try:
        with open(path) as f, open(path + ".tmp", "w") as o:
            for line in f:
                try:
                    if int(line.split(",", 1)[0]) >= cut:
                        o.write(line)
                except ValueError:
                    pass
        os.replace(path + ".tmp", path)
    except FileNotFoundError:
        pass


def sampler(path, interval, days):
    """Sample every `interval` seconds; write the per-minute average to the history file."""
    prune(path, days)
    last_prune = time.time()
    prev, acc = None, []
    minute = int(time.time() // 60)
    while True:
        try:
            s = stats()
            if prev:
                dt = s["t"] - prev["t"]
                dT = s["cpu"][0] - prev["cpu"][0]
                dI = s["cpu"][1] - prev["cpu"][1]
                cpu = (1 - dI / dT) * 100 if dT > 0 else 0.0
                ram = (1 - s["mem"]["MemAvailable"] / s["mem"]["MemTotal"]) * 100
                rx = tx = 0
                for k, v in s["net"]["ifaces"].items():  # only count interfaces that existed in the previous sample
                    if k in prev["net"]["ifaces"]:
                        o = prev["net"]["ifaces"][k]
                        rx += max(0, v[0] - o[0])
                        tx += max(0, v[1] - o[1])
                acc.append((cpu, ram, rx, tx, dt))
            prev = s
            cur = int(s["t"] // 60)
            if cur != minute:
                if acc:
                    dur = sum(a[4] for a in acc) or 1
                    cpu = sum(a[0] * a[4] for a in acc) / dur
                    ram = sum(a[1] * a[4] for a in acc) / dur
                    line = "%d,%.1f,%.1f,%.1f,%d,%d,%.1f\n" % (
                        minute * 60, cpu, max(a[0] for a in acc), ram,
                        sum(a[2] for a in acc), sum(a[3] for a in acc), dur)
                    with open(path, "a") as f:
                        f.write(line)
                acc, minute = [], cur
                if time.time() - last_prune > 21600:
                    prune(path, days)
                    last_prune = time.time()
        except Exception:
            pass
        time.sleep(interval)


def history(rng):
    secs = RANGES.get(rng, 3600)
    now = time.time()
    since = now - secs
    bucket = max(60, int(secs / 300) // 60 * 60)
    B = {}
    try:
        with open(DATA_PATH) as f:
            for line in f:
                try:
                    ts, cpu, cmax, ram, rx, tx, dur = line.split(",")
                    ts = int(ts)
                    if ts < since:
                        continue
                    b = B.setdefault(ts // bucket, [0, 0.0, 0.0, 0.0, 0, 0, 0.0])
                    b[0] += 1
                    b[1] += float(cpu)
                    b[2] = max(b[2], float(cmax))
                    b[3] += float(ram)
                    b[4] += int(rx)
                    b[5] += int(tx)
                    b[6] += float(dur)
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    out = {"from": since, "to": now, "b": bucket, "t": [], "cpu": [], "cpumax": [],
           "ram": [], "rx": [], "tx": [], "rxsum": 0, "txsum": 0}
    for k in sorted(B):
        n, cpu, cmax, ram, rx, tx, dur = B[k]
        dur = dur or 1
        out["t"].append(k * bucket + bucket // 2)
        out["cpu"].append(round(cpu / n, 1))
        out["cpumax"].append(round(cmax, 1))
        out["ram"].append(round(ram / n, 1))
        out["rx"].append(round(rx / dur))
        out["tx"].append(round(tx / dur))
        out["rxsum"] += rx
        out["txsum"] += tx
    return out


# -------------------------------------------------------------------- page --
HTML = r"""<!doctype html><html lang="en" dir="ltr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Server Monitor</title><style>
*{box-sizing:border-box}body{margin:0;background:#0f1115;color:#e6e6e6;font:14px system-ui,Tahoma,sans-serif;padding:14px}
h1{font-size:16px;margin:0 0 10px;color:#9aa4b2;font-weight:600}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px}
.tabs button{background:#171a21;color:#9aa4b2;border:1px solid #242933;border-radius:8px;padding:6px 12px;cursor:pointer;font:inherit}
.tabs button.on{background:#25406a;color:#fff;border-color:#25406a}
.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}
.g2{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:10px;margin-top:10px}
.c{background:#171a21;border:1px solid #242933;border-radius:10px;padding:12px}
.c b{display:block;color:#9aa4b2;font-weight:500;font-size:12px}
.v{font-size:24px;margin:4px 0}
.s{color:#7d8794;font-size:12px}
canvas{width:100%;height:46px;display:block;margin-top:6px}
.big canvas{height:170px}
table{width:100%;border-collapse:collapse;margin-top:6px;direction:ltr;font-size:12px}
td,th{padding:3px 6px;text-align:right;color:#b8c0cc;font-weight:400}th{color:#7d8794}
#tip{position:absolute;display:none;background:#000d;border:1px solid #333;padding:4px 8px;border-radius:6px;font-size:12px;direction:ltr;pointer-events:none;z-index:9}
</style></head><body>
<h1>Server Monitor — <span id="up"></span> · Load <span id="ld"></span> · <span id="co"></span> core</h1>
<div class="tabs" id="tabs">
<button data-r="live" class="on">Live</button><button data-r="1h">1 hour</button><button data-r="6h">6 hours</button>
<button data-r="24h">24 hours</button><button data-r="7d">7 days</button><button data-r="30d">30 days</button></div>

<div id="live">
<div class="g">
<div class="c"><b>CPU</b><div class="v" id="cpu">-</div><canvas id="cc"></canvas></div>
<div class="c"><b>RAM</b><div class="v" id="ram">-</div><div class="s" id="ramd"></div><canvas id="cr"></canvas></div>
<div class="c"><b>Download speed ↓</b><div class="v" id="rx">-</div><div class="s" id="rxt"></div><canvas id="cd"></canvas></div>
<div class="c"><b>Upload speed ↑</b><div class="v" id="tx">-</div><div class="s" id="txt"></div><canvas id="cu"></canvas></div>
<div class="c"><b>Total traffic (since boot)</b><div class="v" id="tot">-</div><div class="s" id="totd"></div></div>
<div class="c"><b>Disk (/) and swap</b><div class="v" id="dsk">-</div><div class="s" id="swp"></div></div>
</div>
<div class="c" style="margin-top:10px"><b>Network interfaces</b><table id="tb"></table></div>
</div>

<div id="hist" style="display:none">
<div class="g" id="hs"></div>
<div class="g2">
<div class="c big"><b>CPU (%)</b><canvas id="h1"></canvas></div>
<div class="c big"><b>RAM (%)</b><canvas id="h2"></canvas></div>
<div class="c big"><b>Download ↓</b><canvas id="h3"></canvas></div>
<div class="c big"><b>Upload ↑</b><canvas id="h4"></canvas></div>
</div></div>
<div id="tip"></div>
<script>
const N=60,$=id=>document.getElementById(id),H={cpu:[],ram:[],rx:[],tx:[]};
const root=location.pathname.replace(/\/?$/,'/'),base=root+'api';let prev=null,cur='live';
function B(n){const u=['B','KB','MB','GB','TB'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i?2:0)+' '+u[i]}
function T(s){s|=0;const d=s/86400|0,h=s%86400/3600|0,m=s%3600/60|0;return (d?d+'d ':'')+h+'h '+m+'m'}
const p2=n=>String(n).padStart(2,'0');
function tfmt(t,span){const d=new Date(t*1000),hm=p2(d.getHours())+':'+p2(d.getMinutes());return span<=90000?hm:p2(d.getMonth()+1)+'/'+p2(d.getDate())+' '+hm}
function spark(id,a,mx,col){const c=$(id),r=devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;c.width=w*r;c.height=h*r;
const x=c.getContext('2d');x.scale(r,r);mx=mx||Math.max(1,...a);x.strokeStyle=col;x.lineWidth=1.5;x.beginPath();
a.forEach((v,i)=>{const px=i*(w/(N-1)),py=h-2-(v/mx)*(h-4);i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()}
function push(k,v){H[k].push(v);if(H[k].length>N)H[k].shift()}

function chart(id,d,S,mx,fmt){
 const c=$(id),r=devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;c.width=w*r;c.height=h*r;
 const x=c.getContext('2d');x.scale(r,r);
 const L=62,Bm=18,Tp=6,R=10,pw=w-L-R,ph=h-Bm-Tp,f=d.from,to=d.to,span=to-f;
 const m=mx||Math.max(1,...S.flatMap(s=>s.y))*1.1;
 const X=t=>L+(t-f)/span*pw,Y=v=>Tp+ph-(v/m)*ph;
 x.font='11px system-ui';x.fillStyle='#7d8794';x.strokeStyle='#242933';x.lineWidth=1;x.textAlign='right';
 for(let i=0;i<=4;i++){const y=Y(m*i/4);x.beginPath();x.moveTo(L,y);x.lineTo(w-R,y);x.stroke();x.fillText(fmt(m*i/4),L-4,y+4)}
 for(let i=0;i<=4;i++){x.textAlign=i==0?'left':i==4?'right':'center';x.fillText(tfmt(f+span*i/4,span),L+pw*i/4,h-3)}
 S.forEach(s=>{x.strokeStyle=s.c;x.lineWidth=s.w||1.5;x.beginPath();let pen=false;
  d.t.forEach((t,i)=>{if(pen&&t-d.t[i-1]>d.b*2.5)pen=false;pen?x.lineTo(X(t),Y(s.y[i])):x.moveTo(X(t),Y(s.y[i]));pen=true});x.stroke()});
 c._s={d,S,fmt,L,R,w};
 if(!c._h){c._h=1;
  c.addEventListener('mousemove',e=>{const s=c._s,rc=c.getBoundingClientRect(),px=e.clientX-rc.left,
   t=s.d.from+(px-s.L)/(s.w-s.L-s.R)*(s.d.to-s.d.from);let bi=-1,bd=1e18;
   s.d.t.forEach((tt,i)=>{const q=Math.abs(tt-t);if(q<bd){bd=q;bi=i}});
   if(bi<0||bd>s.d.b*1.5){$('tip').style.display='none';return}
   $('tip').innerHTML=new Date(s.d.t[bi]*1000).toLocaleString()+'<br>'+s.S.map(q=>q.n+': '+s.fmt(q.y[bi])).join('<br>');
   $('tip').style.display='block';$('tip').style.left=(e.pageX+12)+'px';$('tip').style.top=(e.pageY+12)+'px'});
  c.addEventListener('mouseleave',()=>$('tip').style.display='none')}
}
const pct=v=>v.toFixed(v<10?1:0)+'%',rate=v=>B(v)+'/s';
async function loadHist(){
 if(cur=='live')return;
 try{const d=await (await fetch(base+'/history?r='+cur,{cache:'no-store'})).json();
  if(!d.t.length){$('hs').innerHTML='<div class="c">No data recorded for this range yet (one point is saved per minute).</div>';
   ['h1','h2','h3','h4'].forEach(i=>{const c=$(i);c.getContext('2d').clearRect(0,0,c.width,c.height)});return}
  const avg=a=>a.reduce((x,y)=>x+y,0)/a.length,mx=a=>Math.max(...a);
  const card=(t,v,s)=>`<div class="c"><b>${t}</b><div class="v">${v}</div><div class="s">${s}</div></div>`;
  $('hs').innerHTML=card('CPU','avg '+avg(d.cpu).toFixed(1)+'%','max '+mx(d.cpumax).toFixed(1)+'%')
   +card('RAM','avg '+avg(d.ram).toFixed(1)+'%','max '+mx(d.ram).toFixed(1)+'%')
   +card('Total download ↓',B(d.rxsum),'Peak speed: '+rate(mx(d.rx)))
   +card('Total upload ↑',B(d.txsum),'Peak speed: '+rate(mx(d.tx)));
  chart('h1',d,[{n:'max',y:d.cpumax,c:'#2b5b94',w:1},{n:'avg',y:d.cpu,c:'#4ea1ff'}],100,pct);
  chart('h2',d,[{n:'RAM',y:d.ram,c:'#c792ea'}],100,pct);
  chart('h3',d,[{n:'↓',y:d.rx,c:'#3ddc97'}],0,rate);
  chart('h4',d,[{n:'↑',y:d.tx,c:'#ffb454'}],0,rate);
 }catch(e){}
}
$('tabs').onclick=e=>{const r=e.target.dataset.r;if(!r)return;cur=r;prev=null;
 [...$('tabs').children].forEach(b=>b.classList.toggle('on',b.dataset.r==r));
 $('live').style.display=r=='live'?'':'none';$('hist').style.display=r=='live'?'none':'';loadHist()};
async function tick(){
 if(cur!='live')return;
 try{const d=await (await fetch(base,{cache:'no-store'})).json();
 const m=d.mem,used=m.MemTotal-m.MemAvailable;
 $('up').textContent=T(d.uptime);$('ld').textContent=d.load.join(' ');$('co').textContent=d.cores;
 $('ram').textContent=(used/m.MemTotal*100).toFixed(1)+'%';$('ramd').textContent=B(used)+' / '+B(m.MemTotal);
 $('swp').textContent='Swap: '+B(m.SwapTotal-m.SwapFree)+' / '+B(m.SwapTotal);
 $('dsk').textContent=(d.disk[1]/d.disk[0]*100).toFixed(1)+'%';
 $('tot').textContent=B(d.net.rx+d.net.tx);$('totd').textContent='↓ '+B(d.net.rx)+'   ↑ '+B(d.net.tx);
 $('rxt').textContent='Total: '+B(d.net.rx);$('txt').textContent='Total: '+B(d.net.tx);
 $('tb').innerHTML='<tr><th>iface</th><th>↓ RX</th><th>↑ TX</th></tr>'+Object.entries(d.net.ifaces).map(([k,v])=>`<tr><td>${k}</td><td>${B(v[0])}</td><td>${B(v[1])}</td></tr>`).join('');
 push('ram',used/m.MemTotal*100);
 if(prev){const dt=d.t-prev.t,dT=d.cpu[0]-prev.cpu[0],dI=d.cpu[1]-prev.cpu[1];
  const cpu=dT>0?(1-dI/dT)*100:0,rx=(d.net.rx-prev.net.rx)/dt,tx=(d.net.tx-prev.net.tx)/dt;
  $('cpu').textContent=cpu.toFixed(1)+'%';$('rx').textContent=B(Math.max(rx,0))+'/s';$('tx').textContent=B(Math.max(tx,0))+'/s';
  push('cpu',cpu);push('rx',Math.max(rx,0));push('tx',Math.max(tx,0));
  spark('cc',H.cpu,100,'#4ea1ff');spark('cd',H.rx,0,'#3ddc97');spark('cu',H.tx,0,'#ffb454');}
 spark('cr',H.ram,100,'#c792ea');prev=d;}catch(e){}
}
tick();setInterval(tick,2000);setInterval(loadHist,60000);
</script></body></html>"""


def make_handler(token):
    root = "/" + token

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlsplit(self.path)
            p = u.path
            if p == root:
                self.send_response(301)
                self.send_header("Location", root + "/")
                self.end_headers()
            elif p == root + "/":
                self.send(200, HTML.encode(), "text/html; charset=utf-8")
            elif p == root + "/api":
                self.send(200, json.dumps(stats()).encode(), "application/json")
            elif p == root + "/api/history":
                r = parse_qs(u.query).get("r", ["1h"])[0]
                self.send(200, json.dumps(history(r)).encode(), "application/json")
            else:
                self.send(404, b"not found", "text/plain")

    return H


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no packet is sent; this only finds the outgoing IP
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "<SERVER_IP>"


def main():
    global DATA_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--token", default="")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--interval", type=int, default=10, help="sampling interval in seconds")
    ap.add_argument("--days", type=int, default=30, help="how many days of history to keep")
    ap.add_argument("--data", default=DATA_PATH, help="path of the history file")
    a = ap.parse_args()
    DATA_PATH = a.data

    cfg_path = os.path.join(BASE, "monitor_config.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    token = a.token or cfg.get("token") or secrets.token_urlsafe(9)
    handler = make_handler(token)
    srv = None
    if a.port:
        srv = ThreadingHTTPServer((a.host, a.port), handler)
    else:
        if cfg.get("port"):
            try:
                srv = ThreadingHTTPServer((a.host, int(cfg["port"])), handler)
            except OSError:
                srv = None
        for _ in range(50):
            if srv:
                break
            try:
                srv = ThreadingHTTPServer((a.host, random.randint(20000, 60000)), handler)
            except OSError:
                continue
    if not srv:
        raise SystemExit("no free port found")
    srv.daemon_threads = True
    port = srv.server_address[1]

    try:
        with open(cfg_path, "w") as f:
            json.dump({"port": port, "token": token}, f)
        os.chmod(cfg_path, 0o600)
    except OSError:
        pass

    threading.Thread(target=sampler, args=(DATA_PATH, max(2, a.interval), a.days), daemon=True).start()
    print(f"Panel running:  http://{local_ip()}:{port}/{token}/", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
