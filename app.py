# -*- coding: utf-8 -*-
import os, json, time, uuid, threading
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify, Response

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
SECRET = os.environ.get("WEBHOOK_SECRET", "")
CHART_KEY = os.environ.get("CHART_IMG_KEY", "")
CHART_SYMBOL = os.environ.get("CHART_SYMBOL", "OANDA:XAUUSD")
CHART_INTERVAL = os.environ.get("CHART_INTERVAL", "5m")
TG = f"https://api.telegram.org/bot{TOKEN}"
JOURNAL = "journal.jsonl"
ARMFILE = "armed.flag"

# ── readiness gate ────────────────────────────────────────────────
GATE_TRADES = 20
GATE_CLEAN_WINDOW = 10
GATE_WINRATE = 50.0

# ── execution config ──────────────────────────────────────────────
EXECUTION_DRIVER = os.environ.get("EXECUTION_DRIVER", "none").lower()
RISK_PCT = float(os.environ.get("RISK_PCT", "0.3"))
ACCOUNT_BALANCE = float(os.environ.get("ACCOUNT_BALANCE", "50000"))
EXEC_SYMBOL = os.environ.get("EXEC_SYMBOL", "XAUUSD")
PC_URL = os.environ.get("PINECONNECTOR_URL", "https://webhook.pineconnector.com")
PC_LICENSE = os.environ.get("PC_LICENSE_ID", "")
MA_TOKEN = os.environ.get("METAAPI_TOKEN", "")
MA_ACCOUNT = os.environ.get("METAAPI_ACCOUNT_ID", "")
MA_REGION = os.environ.get("METAAPI_REGION", "london")
GOLD_DOLLARS_PER_POINT_PER_LOT = 100.0   # 1 lot = 100oz, $1 move = $100

app = Flask(__name__)
state_lock = threading.Lock()
pending = {}
opened = {}

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def jwrite(rec):
    rec["logged_at"] = now_iso()
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")

def jread():
    rows = []
    try:
        with open(JOURNAL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return rows

def is_armed():
    return os.path.exists(ARMFILE)

def set_armed(v):
    if v:
        open(ARMFILE, "w").write(now_iso())
    else:
        try:
            os.remove(ARMFILE)
        except FileNotFoundError:
            pass

def tg(method, payload=None, files=None):
    try:
        if files:
            return requests.post(f"{TG}/{method}", data=payload, files=files, timeout=30)
        return requests.post(f"{TG}/{method}", json=payload, timeout=30)
    except Exception as e:
        print("telegram error:", e)
        return None

def tg_send(text, keyboard=None):
    p = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if keyboard:
        p["reply_markup"] = keyboard
    r = tg("sendMessage", p)
    if r is not None and r.ok:
        return r.json().get("result", {}).get("message_id")
    return None

def fetch_chart_png():
    if not CHART_KEY:
        return None
    try:
        r = requests.get("https://api.chart-img.com/v1/tradingview/advanced-chart",
                         params={"symbol": CHART_SYMBOL, "interval": CHART_INTERVAL,
                                 "key": CHART_KEY, "width": 800, "height": 500,
                                 "theme": "dark"}, timeout=30)
        if r.status_code == 200:
            return r.content
    except Exception as e:
        print("chart-img error:", e)
    return None

def ticket(s):
    side = str(s.get("side", "?")).upper()
    icon = "🟢" if side == "LONG" else "🔴"
    return (f"{icon} <b>TAKE IT - {side} {s.get('symbol','')}</b>\n"
            f"Session: {s.get('session','?')}\n"
            f"Entry: <code>{s.get('entry','?')}</code>  SL: <code>{s.get('sl','?')}</code>  "
            f"TP: <code>{s.get('tp','?')}</code>\nR:R: <code>{s.get('rr','?')}</code>\n"
            f"<i>All non-negotiables met on confirmed close.</i>")

def send_signal(s):
    sid = uuid.uuid4().hex[:10]
    s["sid"] = sid
    s["signal_time"] = now_iso()
    kb = {"inline_keyboard": [[
        {"text": "✅ TAKE", "callback_data": f"take:{sid}"},
        {"text": "❌ SKIP", "callback_data": f"skip:{sid}"}]]}
    text = ticket(s)
    png = fetch_chart_png()
    mid = None
    if png:
        r = tg("sendPhoto", payload={"chat_id": CHAT_ID, "caption": text,
                                     "parse_mode": "HTML",
                                     "reply_markup": json.dumps(kb)},
               files={"photo": ("chart.png", png, "image/png")})
        if r is not None and r.ok:
            mid = r.json().get("result", {}).get("message_id")
    else:
        mid = tg_send(text, kb)
    s["message_id"] = mid
    with state_lock:
        pending[sid] = s
    jwrite({"type": "signal", **s})

def find_signal(sid):
    for r in reversed(jread()):
        if r.get("type") == "signal" and r.get("sid") == sid:
            return r
    return None

# ── stats / gate engine ───────────────────────────────────────────
def build_stats():
    rows = jread()
    sig, dec, outc, proc = {}, {}, {}, {}
    for r in rows:
        sid = r.get("sid") or (r.get("signal") or {}).get("sid")
        if r.get("type") == "signal":
            sig[r["sid"]] = r
        elif r.get("type") == "decision" and sid:
            dec[sid] = r
        elif r.get("type") == "outcome" and sid:
            outc[sid] = r
        elif r.get("type") == "process" and sid:
            proc[sid] = r
    trades = []
    for sid, d in dec.items():
        if d.get("decision") != "take":
            continue
        s = sig.get(sid, {})
        o = outc.get(sid)
        p = proc.get(sid)
        try:
            rr = float(s.get("rr", 1.0))
        except Exception:
            rr = 1.0
        res = o.get("result") if o else None
        realized = (rr if res == "win" else (-1.0 if res == "loss" else 0.0)) if res else None
        trades.append({"sid": sid, "time": s.get("signal_time", ""),
                       "side": s.get("side", "?"), "session": s.get("session", "?"),
                       "rr": rr, "result": res, "realized": realized,
                       "clean": (p.get("clean") if p else None),
                       "latency": d.get("latency_sec", "")})
    trades.sort(key=lambda t: t["time"])
    completed = [t for t in trades if t["result"]]
    wins = [t for t in completed if t["result"] == "win"]
    losses = [t for t in completed if t["result"] == "loss"]
    wr = round(100 * len(wins) / len(completed), 1) if completed else 0.0
    totR = round(sum(t["realized"] or 0 for t in completed), 2)
    lastN = completed[-GATE_CLEAN_WINDOW:]
    breaches_last = sum(1 for t in lastN if t["clean"] is False)
    ungraded = sum(1 for t in completed if t["clean"] is None)
    skips = sum(1 for d in dec.values() if d.get("decision") == "skip")
    streak = 0
    for t in reversed(completed):
        if t["result"] == "be":
            continue
        if streak == 0:
            streak = 1 if t["result"] == "win" else -1
        elif (streak > 0) == (t["result"] == "win"):
            streak += 1 if streak > 0 else -1
        else:
            break
    gate_ok = (len(completed) >= GATE_TRADES and breaches_last == 0
               and wr > GATE_WINRATE and ungraded == 0)
    return {"trades": trades, "completed": len(completed), "wins": len(wins),
            "losses": len(losses), "be": len(completed)-len(wins)-len(losses),
            "wr": wr, "totR": totR, "skips": skips, "signals": len(sig),
            "breaches_last": breaches_last, "ungraded": ungraded,
            "streak": streak, "gate_ok": gate_ok}

def exec_status():
    st = build_stats()
    if EXECUTION_DRIVER == "none":
        return "OFF (no driver)", False, st
    if not st["gate_ok"]:
        return f"LOCKED (gate {st['completed']}/{GATE_TRADES})", False, st
    if not is_armed():
        return "READY (gate met - /arm to go live)", False, st
    return f"LIVE via {EXECUTION_DRIVER}", True, st

def stats_text():
    st = build_stats()
    estr, _, _ = exec_status()
    gate = "✅ GATE MET" if st["gate_ok"] else (
        f"{st['completed']}/{GATE_TRADES} trades | "
        f"{st['breaches_last']} breach(es) in last {GATE_CLEAN_WINDOW} | "
        f"WR {st['wr']}% vs {GATE_WINRATE}%")
    stk = f"{abs(st['streak'])}{'W' if st['streak']>0 else 'L'}" if st["streak"] else "-"
    return (f"📊 <b>Journal</b>\n"
            f"Record: {st['wins']}W-{st['losses']}L-{st['be']}BE (WR {st['wr']}%)\n"
            f"Net: {st['totR']:+.2f}R | Streak: {stk}\n"
            f"Signals {st['signals']} | Skips {st['skips']} | Ungraded {st['ungraded']}\n"
            f"Gate: {gate}\nExecution: {estr}")

# ── execution engine (Phase 3, triple-locked) ─────────────────────
def calc_lots(entry, sl):
    dist = abs(float(entry) - float(sl))
    if dist <= 0:
        return 0.0
    risk_dollars = ACCOUNT_BALANCE * (RISK_PCT / 100.0)
    lots = risk_dollars / (dist * GOLD_DOLLARS_PER_POINT_PER_LOT)
    return max(0.01, round(lots, 2))

def exec_pineconnector(side, lots, sl, tp):
    cmd = "buy" if side == "long" else "sell"
    msg = f"{PC_LICENSE},{cmd},{EXEC_SYMBOL},risk={lots},sl={sl},tp={tp}"
    r = requests.post(PC_URL, data=msg, timeout=30)
    return {"driver": "pineconnector", "sent": msg,
            "status": r.status_code, "resp": r.text[:300]}

def exec_metaapi(side, lots, sl, tp):
    url = (f"https://mt-client-api-v1.{MA_REGION}.agiliumtrade.ai"
           f"/users/current/accounts/{MA_ACCOUNT}/trade")
    body = {"actionType": "ORDER_TYPE_BUY" if side == "long" else "ORDER_TYPE_SELL",
            "symbol": EXEC_SYMBOL, "volume": lots,
            "stopLoss": float(sl), "takeProfit": float(tp)}
    r = requests.post(url, json=body,
                      headers={"auth-token": MA_TOKEN,
                               "Content-Type": "application/json"}, timeout=30)
    return {"driver": "metaapi", "sent": body,
            "status": r.status_code, "resp": r.text[:300]}

def execute_trade(s):
    estr, live, _ = exec_status()
    entry, sl, tp = s.get("entry"), s.get("sl"), s.get("tp")
    side = str(s.get("side", "")).lower()
    if not live:
        jwrite({"type": "execution", "sid": s.get("sid"), "executed": False,
                "reason": estr})
        return f"📓 Journal-only ({estr})"
    lots = calc_lots(entry, sl)
    if lots <= 0:
        jwrite({"type": "execution", "sid": s.get("sid"), "executed": False,
                "reason": "bad lot calc"})
        return "⚠️ Execution skipped - bad SL distance"
    try:
        if EXECUTION_DRIVER == "pineconnector":
            res = exec_pineconnector(side, lots, sl, tp)
        elif EXECUTION_DRIVER == "metaapi":
            res = exec_metaapi(side, lots, sl, tp)
        else:
            res = {"error": "unknown driver"}
        ok = res.get("status") in (200, 201, 204)
        jwrite({"type": "execution", "sid": s.get("sid"), "executed": ok,
                "lots": lots, "result": res})
        return (f"⚡ ORDER SENT | {lots} lots | {res.get('driver')}"
                if ok else f"❌ Execution failed: {res}")
    except Exception as e:
        jwrite({"type": "execution", "sid": s.get("sid"), "executed": False,
                "reason": str(e)})
        return f"❌ Execution error: {e}"

# ── routes ────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    if SECRET and request.args.get("secret") != SECRET:
        return jsonify({"ok": False}), 403
    raw = request.get_data(as_text=True)
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"raw": raw}
    send_signal(payload)
    return jsonify({"ok": True})

@app.route("/export")
def export():
    if SECRET and request.args.get("secret") != SECRET:
        return "forbidden", 403
    try:
        data = open(JOURNAL, encoding="utf-8").read()
    except FileNotFoundError:
        data = ""
    return Response(data, mimetype="application/jsonl",
                    headers={"Content-Disposition": "attachment; filename=journal.jsonl"})

@app.route("/")
def health():
    return "tradingpool bot up - dashboard at /dashboard"

@app.route("/dashboard")
def dashboard():
    st = build_stats()
    estr, live, _ = exec_status()
    pct = min(100, int(100 * st["completed"] / GATE_TRADES))
    gcol = "#3ddc84" if st["gate_ok"] else "#ffb24d"
    gtxt = "GATE MET - eligible to arm execution" if st["gate_ok"] else \
        f"{st['completed']}/{GATE_TRADES} completed | {st['breaches_last']} breach(es) in last {GATE_CLEAN_WINDOW} | need WR>{GATE_WINRATE}%"
    ecol = "#3ddc84" if live else "#ffb24d"
    estatus = "LIVE" if live else ("ARMED" if is_armed() and st["gate_ok"] else "LOCKED")
    stk = f"{abs(st['streak'])}{'W' if st['streak']>0 else 'L'}" if st["streak"] else "-"
    wr_col = "#3ddc84" if st["wr"] > GATE_WINRATE else "#ffb24d"
    r_col = "#3ddc84" if st["totR"] >= 0 else "#ef5350"
    u_col = "#ef5350" if st["ungraded"] else "#3ddc84"
    rows = ""
    for i, t in enumerate(reversed(st["trades"][-30:])):
        res = t["result"] or "open"
        rc = {"win": "#3ddc84", "loss": "#ef5350", "be": "#ffb24d"}.get(res, "#5a6862")
        rb = {"win": "badge-win", "loss": "badge-loss", "be": "badge-be"}.get(res, "badge-open")
        side_cls = "side-long" if str(t["side"]).lower() == "long" else "side-short"
        cl = "-" if t["clean"] is None else ("CLEAN" if t["clean"] else "BREACH")
        cc = "badge-muted" if t["clean"] is None else ("badge-clean" if t["clean"] else "badge-breach")
        rl = "" if t["realized"] is None else f"{t['realized']:+.2f}R"
        delay = f"animation-delay:{i * 0.04}s"
        rows += (f"<tr class='trade-row' style='{delay}'>"
                 f"<td class='mono dim'>{t['time'][:16].replace('T',' ')}</td>"
                 f"<td><span class='side-pill {side_cls}'>{t['side'].upper()}</span></td>"
                 f"<td><span class='sess'>{t['session']}</span></td>"
                 f"<td><span class='badge {rb}'>{res.upper()}</span></td>"
                 f"<td class='mono r-val' style='color:{rc}'>{rl or '-'}</td>"
                 f"<td><span class='badge {cc}'>{cl}</span></td></tr>")
    if not rows:
        rows = "<tr><td colspan='6' class='empty'>No trades yet. Waiting for first signal.</td></tr>"
    gate_ring = int(283 * pct / 100)
    html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradingpool Journal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Outfit:wght@300;500;700&display=swap" rel="stylesheet">
<style>
:root{{
  --bg:#060908;--surface:#0c100e;--card:#101614;--border:#1a2420;
  --text:#d8e4dc;--muted:#5c6e64;--gold:#c9a227;--gold-dim:#8a7020;
  --green:#2fd67a;--red:#f05252;--amber:#f0a030;--glow:0 0 40px rgba(201,162,39,.08);
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{
  font-family:'Outfit',system-ui,sans-serif;background:var(--bg);color:var(--text);
  min-height:100vh;overflow-x:hidden;
  background-image:
    radial-gradient(ellipse 80% 50% at 50% -20%,rgba(201,162,39,.12),transparent),
    radial-gradient(ellipse 60% 40% at 100% 100%,rgba(47,214,122,.06),transparent),
    linear-gradient(rgba(26,36,32,.4) 1px,transparent 1px),
    linear-gradient(90deg,rgba(26,36,32,.4) 1px,transparent 1px);
  background-size:auto,auto,48px 48px,48px 48px;
}}
.wrap{{max-width:1100px;margin:0 auto;padding:28px 20px 48px}}
header{{
  display:flex;align-items:flex-end;justify-content:space-between;gap:16px;
  margin-bottom:32px;padding-bottom:24px;border-bottom:1px solid var(--border);
  animation:rise .6s ease both;
}}
.brand{{display:flex;flex-direction:column;gap:4px}}
.brand h1{{
  font-size:clamp(1.6rem,4vw,2.2rem);font-weight:700;letter-spacing:.06em;
  background:linear-gradient(135deg,var(--gold) 0%,#f0d878 50%,var(--gold-dim) 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}}
.brand span{{font-size:11px;color:var(--muted);letter-spacing:.2em;text-transform:uppercase}}
.live-pill{{
  display:inline-flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;
  font-size:11px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;
  border:1px solid var(--border);background:var(--card);
}}
.live-pill.on{{border-color:rgba(47,214,122,.4);color:var(--green);box-shadow:0 0 20px rgba(47,214,122,.15)}}
.live-pill.off{{color:var(--amber)}}
.live-pill .dot{{width:7px;height:7px;border-radius:50%;background:currentColor}}
.live-pill.on .dot{{animation:pulse 2s ease infinite}}
.hero{{
  display:grid;grid-template-columns:1fr 1.4fr;gap:16px;margin-bottom:16px;
  animation:rise .6s .1s ease both;
}}
@media(max-width:720px){{.hero{{grid-template-columns:1fr}}}}
.card{{
  background:linear-gradient(145deg,var(--card),var(--surface));
  border:1px solid var(--border);border-radius:12px;padding:20px 22px;
  box-shadow:var(--glow);position:relative;overflow:hidden;
}}
.card::before{{
  content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(201,162,39,.35),transparent);
}}
.lbl{{font-size:10px;color:var(--muted);letter-spacing:.18em;text-transform:uppercase;margin-bottom:10px}}
.gate-ring-wrap{{display:flex;align-items:center;gap:20px}}
.ring-box{{position:relative;width:88px;height:88px;flex-shrink:0}}
.ring-box svg{{transform:rotate(-90deg)}}
.ring-box .pct{{
  position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  font-family:'JetBrains Mono',monospace;font-size:18px;font-weight:600;color:var(--gold);
}}
.gate-txt{{font-size:14px;line-height:1.5;color:{gcol};font-weight:500}}
.gate-sub{{font-size:12px;color:var(--muted);margin-top:6px}}
.exec-row{{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}}
.exec-status{{
  font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:600;
  padding:6px 12px;border-radius:6px;background:rgba(0,0,0,.3);color:{ecol};
  border:1px solid rgba(255,255,255,.06);
}}
.exec-meta{{font-size:12px;color:var(--muted)}}
.exec-meta b{{color:var(--gold);font-weight:500}}
.stats{{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;
  margin-bottom:16px;animation:rise .6s .2s ease both;
}}
.stat{{
  background:var(--card);border:1px solid var(--border);border-radius:10px;
  padding:16px 18px;transition:border-color .2s,transform .2s;
}}
.stat:hover{{border-color:rgba(201,162,39,.25);transform:translateY(-2px)}}
.stat .k{{font-size:9px;color:var(--muted);letter-spacing:.16em;text-transform:uppercase;margin-bottom:8px}}
.stat .v{{font-family:'JetBrains Mono',monospace;font-size:1.55rem;font-weight:600;line-height:1}}
.stat .sub{{font-size:10px;color:var(--muted);margin-top:4px}}
.table-card{{animation:rise .6s .3s ease both}}
.table-wrap{{overflow-x:auto;margin-top:4px;border-radius:8px;border:1px solid var(--border)}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{
  padding:10px 14px;text-align:left;font-size:9px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--muted);background:rgba(0,0,0,.25);
  border-bottom:1px solid var(--border);
}}
td{{padding:11px 14px;border-bottom:1px solid rgba(26,36,32,.8)}}
tr:last-child td{{border-bottom:none}}
.trade-row{{opacity:0;animation:fadein .5s ease forwards}}
.mono{{font-family:'JetBrains Mono',monospace;font-size:11px}}
.dim{{color:var(--muted)}}
.side-pill{{
  display:inline-block;padding:3px 10px;border-radius:4px;font-size:10px;
  font-weight:600;letter-spacing:.08em;font-family:'JetBrains Mono',monospace;
}}
.side-long{{background:rgba(47,214,122,.12);color:var(--green);border:1px solid rgba(47,214,122,.25)}}
.side-short{{background:rgba(240,82,82,.12);color:var(--red);border:1px solid rgba(240,82,82,.25)}}
.sess{{font-size:11px;color:var(--muted);letter-spacing:.06em}}
.badge{{
  display:inline-block;padding:3px 9px;border-radius:4px;font-size:9px;
  font-weight:700;letter-spacing:.1em;font-family:'JetBrains Mono',monospace;
}}
.badge-win{{background:rgba(47,214,122,.15);color:var(--green)}}
.badge-loss{{background:rgba(240,82,82,.15);color:var(--red)}}
.badge-be{{background:rgba(240,160,48,.15);color:var(--amber)}}
.badge-open{{background:rgba(92,110,100,.15);color:var(--muted)}}
.badge-clean{{background:rgba(47,214,122,.1);color:var(--green)}}
.badge-breach{{background:rgba(240,82,82,.1);color:var(--red)}}
.badge-muted{{background:rgba(92,110,100,.1);color:var(--muted)}}
.r-val{{font-weight:600}}
.empty{{text-align:center;padding:32px!important;color:var(--muted);font-style:italic}}
footer{{
  margin-top:20px;padding:16px 20px;border-radius:10px;
  background:rgba(0,0,0,.25);border:1px solid var(--border);
  font-size:11px;color:var(--muted);line-height:1.7;animation:rise .6s .4s ease both;
}}
footer code{{
  font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--gold-dim);
  background:rgba(201,162,39,.08);padding:2px 6px;border-radius:3px;
}}
@keyframes rise{{from{{opacity:0;transform:translateY(16px)}}to{{opacity:1;transform:none}}}}
@keyframes fadein{{from{{opacity:0;transform:translateX(-8px)}}to{{opacity:1;transform:none}}}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
</style></head><body>
<div class="wrap">
<header>
  <div class="brand">
    <h1>TRADINGPOOL</h1>
    <span>XAUUSD Signal Journal v3</span>
  </div>
  <div class="live-pill {'on' if live else 'off'}">
    <span class="dot"></span>{estatus}
  </div>
</header>

<div class="hero">
  <div class="card">
    <div class="lbl">Funded readiness gate</div>
    <div class="gate-ring-wrap">
      <div class="ring-box">
        <svg width="88" height="88" viewBox="0 0 100 100">
          <circle cx="50" cy="50" r="45" fill="none" stroke="#1a2420" stroke-width="6"/>
          <circle cx="50" cy="50" r="45" fill="none" stroke="{gcol}" stroke-width="6"
            stroke-dasharray="{gate_ring} 283" stroke-linecap="round"
            style="filter:drop-shadow(0 0 6px {gcol})"/>
        </svg>
        <div class="pct">{pct}%</div>
      </div>
      <div>
        <div class="gate-txt">{gtxt}</div>
        <div class="gate-sub">{GATE_TRADES} trades | WR &gt; {GATE_WINRATE}% | 0 breaches in last {GATE_CLEAN_WINDOW}</div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="lbl">Execution engine</div>
    <div class="exec-row">
      <span class="exec-status">{estr}</span>
      <span class="exec-meta">Risk <b>{RISK_PCT}%</b> on <b>${int(ACCOUNT_BALANCE):,}</b> | Driver <b>{EXECUTION_DRIVER}</b></span>
    </div>
  </div>
</div>

<div class="stats">
  <div class="stat"><div class="k">Record</div><div class="v">{st['wins']}W-{st['losses']}L-{st['be']}BE</div><div class="sub">{st['completed']} completed</div></div>
  <div class="stat"><div class="k">Win rate</div><div class="v" style="color:{wr_col}">{st['wr']}%</div><div class="sub">target &gt; {GATE_WINRATE}%</div></div>
  <div class="stat"><div class="k">Net R</div><div class="v" style="color:{r_col}">{st['totR']:+.2f}</div><div class="sub">cumulative</div></div>
  <div class="stat"><div class="k">Streak</div><div class="v">{stk}</div><div class="sub">current run</div></div>
  <div class="stat"><div class="k">Signals</div><div class="v">{st['signals']}</div><div class="sub">{st['skips']} skipped</div></div>
  <div class="stat"><div class="k">Ungraded</div><div class="v" style="color:{u_col}">{st['ungraded']}</div><div class="sub">need CLEAN/BREACH</div></div>
</div>

<div class="card table-card">
  <div class="lbl">Trade log (last 30)</div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Time UTC</th><th>Side</th><th>Session</th><th>Result</th><th>R</th><th>Process</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</div>

<footer>
  Journal is ephemeral on free hosting - back up via <code>/export?secret=...</code><br>
  Telegram commands: <code>/stats</code> <code>/arm</code> <code>/disarm</code>
</footer>
</div>
</body></html>"""
    return html

# ── telegram callbacks + commands ─────────────────────────────────
def handle_callback(cb):
    data = cb.get("data", "")
    tg("answerCallbackQuery", {"callback_query_id": cb.get("id")})
    if ":" not in data:
        return
    action, sid = data.split(":", 1)
    if action in ("take", "skip"):
        with state_lock:
            s = pending.pop(sid, None)
        if s is None:
            s = find_signal(sid)
        if s is None:
            return
        latency = ""
        try:
            t0 = datetime.fromisoformat(s["signal_time"])
            latency = round((datetime.now(timezone.utc) - t0).total_seconds(), 1)
        except Exception:
            pass
        jwrite({"type": "decision", "sid": sid, "decision": action,
                "latency_sec": latency, "signal": s})
        if action == "skip":
            tg_send(f"❌ Skipped | logged ({latency}s)\n\n{stats_text()}")
            return
        exec_msg = execute_trade(s)
        with state_lock:
            opened[sid] = s
        kb = {"inline_keyboard": [[
            {"text": "🎯 WIN", "callback_data": f"win:{sid}"},
            {"text": "🛑 LOSS", "callback_data": f"loss:{sid}"},
            {"text": "➖ BE", "callback_data": f"be:{sid}"}]]}
        tg_send(f"✅ Taken | logged ({latency}s)\n{exec_msg}\n"
                f"When the trade closes, record the outcome:", kb)
    elif action in ("win", "loss", "be"):
        jwrite({"type": "outcome", "sid": sid, "result": action})
        kb = {"inline_keyboard": [[
            {"text": "✅ CLEAN", "callback_data": f"clean:{sid}"},
            {"text": "⚠️ BREACH", "callback_data": f"breach:{sid}"}]]}
        tg_send(f"Outcome logged: <b>{action.upper()}</b>\n"
                f"Process check - every non-negotiable followed "
                f"(entry on close, SL placement, 3SL, BE-only management)?", kb)
    elif action in ("clean", "breach"):
        jwrite({"type": "process", "sid": sid, "clean": action == "clean"})
        with state_lock:
            opened.pop(sid, None)
        note = "" if action == "clean" else \
            "\n⚠️ Breach logged - the clean window resets. Name it in your notes."
        tg_send(f"Process: <b>{action.upper()}</b>{note}\n\n{stats_text()}")

def handle_message(msg):
    txt = (msg.get("text") or "").strip().lower()
    if txt in ("/stats", "/journal", "stats"):
        tg_send(stats_text())
    elif txt == "/arm":
        st = build_stats()
        if EXECUTION_DRIVER == "none":
            tg_send("Cannot arm: EXECUTION_DRIVER is 'none'. Set it in env first.")
        elif not st["gate_ok"]:
            tg_send(f"🔒 Cannot arm - gate not met.\n"
                    f"{st['completed']}/{GATE_TRADES} trades | "
                    f"{st['breaches_last']} breach(es) in last {GATE_CLEAN_WINDOW} | "
                    f"WR {st['wr']}% (need >{GATE_WINRATE}%) | "
                    f"ungraded {st['ungraded']}.\nThe gate is the system. Keep going.")
        else:
            set_armed(True)
            tg_send(f"⚡ ARMED - live execution via {EXECUTION_DRIVER}, "
                    f"{RISK_PCT}% risk. /disarm to stop.")
    elif txt == "/disarm":
        set_armed(False)
        tg_send("🔒 Disarmed - back to journal-only.")

def poll_loop():
    offset = 0
    while True:
        try:
            r = requests.get(f"{TG}/getUpdates",
                             params={"offset": offset, "timeout": 30,
                                     "allowed_updates": json.dumps(["message", "callback_query"])},
                             timeout=40)
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    handle_callback(upd["callback_query"])
                elif "message" in upd:
                    handle_message(upd["message"])
        except Exception as e:
            print("poll error:", e)
            time.sleep(5)

_started = False
def start_poll():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=poll_loop, daemon=True).start()

start_poll()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
