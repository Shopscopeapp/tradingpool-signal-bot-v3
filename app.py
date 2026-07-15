# -*- coding: utf-8 -*-
import os, json, time, uuid, threading
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify, Response

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
SECRET = os.environ.get("WEBHOOK_SECRET", "")
CHART_KEY = os.environ.get("CHART_IMG_KEY", "").strip()
CHART_SYMBOL = os.environ.get("CHART_SYMBOL", "OANDA:XAUUSD")
CHART_INTERVAL = os.environ.get("CHART_INTERVAL", "5m")
CHART_LAYOUT_ID = os.environ.get("CHART_LAYOUT_ID", "").strip()
TV_SESSION_ID = os.environ.get("TV_SESSION_ID", "").strip()
TV_SESSION_ID_SIGN = os.environ.get("TV_SESSION_ID_SIGN", "").strip()
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

def fetch_layout_chart():
    """Snapshot a saved TradingView layout (includes custom Pine indicators)."""
    url = f"https://api.chart-img.com/v2/tradingview/layout-chart/{CHART_LAYOUT_ID}"
    headers = {"x-api-key": CHART_KEY, "content-type": "application/json"}
    if TV_SESSION_ID and TV_SESSION_ID_SIGN:
        headers["tradingview-session-id"] = TV_SESSION_ID
        headers["tradingview-session-id-sign"] = TV_SESSION_ID_SIGN
    body = {"symbol": CHART_SYMBOL, "interval": CHART_INTERVAL,
            "width": 800, "height": 500, "format": "png"}
    try:
        r = requests.post(url, json=body, headers=headers, timeout=45)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and r.content[:4] == b"\x89PNG":
            print(f"layout-chart: ok ({len(r.content)} bytes)", flush=True)
            return r.content
        print("layout-chart error:", r.status_code, ct, r.text[:300], flush=True)
    except Exception as e:
        print("layout-chart error:", e, flush=True)
    return None

def fetch_chart_png():
    if not CHART_KEY:
        print("chart-img: CHART_IMG_KEY not set", flush=True)
        return None
    if CHART_LAYOUT_ID:
        png = fetch_layout_chart()
        if png:
            return png
        print("layout-chart failed, falling back to plain chart", flush=True)
    params = {"symbol": CHART_SYMBOL, "interval": CHART_INTERVAL,
              "width": 800, "height": 500, "theme": "dark", "format": "png"}
    attempts = [
        {"params": params, "headers": {"Authorization": f"Bearer {CHART_KEY}"}},
        {"params": {**params, "key": CHART_KEY}, "headers": {}},
    ]
    url = "https://api.chart-img.com/v1/tradingview/advanced-chart"
    for i, kw in enumerate(attempts, 1):
        try:
            r = requests.get(url, timeout=20, **kw)
            ct = r.headers.get("content-type", "")
            if r.status_code == 200 and r.content[:4] == b"\x89PNG":
                print(f"chart-img: ok via attempt {i} ({len(r.content)} bytes)", flush=True)
                return r.content
            print(f"chart-img attempt {i}:", r.status_code, ct, r.text[:200], flush=True)
        except Exception as e:
            print(f"chart-img attempt {i} error:", e, flush=True)
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
            print("telegram photo error:", getattr(r, "status_code", None),
                  r.text[:200] if r is not None else "no response")
    if mid is None:
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
    threading.Thread(target=send_signal, args=(payload,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/test-chart")
def test_chart():
    if SECRET and request.args.get("secret") != SECRET:
        return "forbidden", 403
    png = fetch_chart_png()
    if png:
        return Response(png, mimetype="image/png")
    return "chart fetch failed - check render logs", 500

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
    gate_ok = st["gate_ok"]
    gtxt = "GATE MET - ELIGIBLE TO ARM" if gate_ok else f"INCOMPLETE - {st['completed']}/{GATE_TRADES} TRADES"
    estatus = "LIVE" if live else ("ARMED" if is_armed() and gate_ok else "LOCKED")
    stk = f"{abs(st['streak'])}{'W' if st['streak']>0 else 'L'}" if st["streak"] else "N/A"
    wr_ok = st["wr"] > GATE_WINRATE
    utc_now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    bar_fill = int(pct / 10)
    bar = "#" * bar_fill + "-" * (10 - bar_fill)
    rows = ""
    for i, t in enumerate(reversed(st["trades"][-30:]), 1):
        res = t["result"] or "OPEN"
        res_cls = {"WIN": "c-pos", "LOSS": "c-neg", "BE": "c-warn", "OPEN": "c-dim"}.get(res.upper(), "c-dim")
        side = str(t["side"]).upper()
        side_cls = "c-pos" if side == "LONG" else "c-neg"
        cl = "--" if t["clean"] is None else ("CLN" if t["clean"] else "BRC")
        cl_cls = "c-dim" if t["clean"] is None else ("c-pos" if t["clean"] else "c-neg")
        rl = "--" if t["realized"] is None else f"{t['realized']:+.2f}"
        lat = t.get("latency", "") or "--"
        rows += (f"<tr>"
                 f"<td class='c-dim'>{i:02d}</td>"
                 f"<td>{t['time'][:16].replace('T',' ')}</td>"
                 f"<td class='c-amber'>XAU</td>"
                 f"<td class='{side_cls}'>{side}</td>"
                 f"<td>{t['session']}</td>"
                 f"<td class='{res_cls}'>{res.upper()}</td>"
                 f"<td class='{res_cls}'>{rl}</td>"
                 f"<td class='{cl_cls}'>{cl}</td>"
                 f"<td class='c-dim'>{lat}</td></tr>")
    if not rows:
        rows = '<tr><td colspan="9" class="c-dim empty-row">NO TRADES - AWAITING SIGNAL</td></tr>'
    html = f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TRP | JOURNAL</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{
  --bg:#000;--pnl:#0d0d0d;--pnl2:#141414;--line:#2a2a2a;--line2:#3d3d3d;
  --amber:#ff9900;--amber2:#ffcc00;--txt:#c8c8c8;--white:#f0f0f0;
  --pos:#00cc66;--neg:#ff4444;--warn:#ffaa00;--dim:#666;--cyan:#33bbff;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%}}
body{{
  font-family:'IBM Plex Mono',Consolas,'Courier New',monospace;
  font-size:11px;line-height:1.35;background:var(--bg);color:var(--txt);
  display:flex;flex-direction:column;min-height:100vh;
}}
.topbar{{
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;
  background:var(--amber);color:#000;padding:3px 10px;font-weight:600;font-size:10px;
  letter-spacing:.04em;border-bottom:2px solid var(--amber2);
}}
.topbar .logo{{font-size:12px;font-weight:700;letter-spacing:.12em}}
.topbar .rt{{display:flex;gap:14px;flex-wrap:wrap}}
.fnbar{{
  display:flex;background:#1a1a1a;border-bottom:1px solid var(--line2);
  font-size:10px;overflow-x:auto;
}}
.fnbar span{{
  padding:4px 12px;border-right:1px solid var(--line);color:var(--dim);white-space:nowrap;
}}
.fnbar span.on{{background:var(--pnl2);color:var(--amber);border-bottom:2px solid var(--amber)}}
.main{{flex:1;padding:6px;display:grid;grid-template-rows:auto 1fr auto;gap:6px;min-height:0}}
.panels{{display:grid;grid-template-columns:1fr 1fr;gap:6px}}
@media(max-width:800px){{.panels{{grid-template-columns:1fr}}}}
.pnl{{background:var(--pnl);border:1px solid var(--line2);display:flex;flex-direction:column;min-height:0}}
.pnl-h{{
  background:var(--pnl2);color:var(--amber);font-size:10px;font-weight:600;
  padding:3px 8px;border-bottom:1px solid var(--line);letter-spacing:.1em;
  display:flex;justify-content:space-between;
}}
.pnl-b{{padding:8px;flex:1}}
.row{{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px dotted #222}}
.row:last-child{{border-bottom:none}}
.k{{color:var(--dim)}}
.v{{color:var(--white);font-weight:500}}
.c-amber{{color:var(--amber)}}
.c-pos{{color:var(--pos)}}
.c-neg{{color:var(--neg)}}
.c-warn{{color:var(--warn)}}
.c-dim{{color:var(--dim)}}
.c-cyan{{color:var(--cyan)}}
.bar{{color:var(--amber);letter-spacing:2px;font-weight:600}}
.bar-dim{{color:#444}}
.gate-ok{{color:var(--pos);font-weight:700}}
.gate-no{{color:var(--warn);font-weight:700}}
.exec-live{{color:var(--pos);animation:blink 1.2s step-end infinite}}
.exec-off{{color:var(--warn)}}
.blotter{{display:flex;flex-direction:column;min-height:0;flex:1}}
.blotter .pnl-b{{padding:0;overflow:auto;flex:1}}
table{{width:100%;border-collapse:collapse;font-size:10px}}
th{{
  position:sticky;top:0;background:#1a1a1a;color:var(--amber);font-weight:600;
  padding:4px 8px;text-align:left;border-bottom:1px solid var(--line2);
  letter-spacing:.06em;font-size:9px;
}}
td{{padding:3px 8px;border-bottom:1px solid #181818;white-space:nowrap}}
tr:nth-child(even) td{{background:#0a0a0a}}
tr:hover td{{background:#1a1a1a}}
.empty-row{{text-align:center;padding:20px!important;letter-spacing:.08em}}
.statusbar{{
  display:flex;flex-wrap:wrap;gap:12px;padding:3px 10px;background:#1a1a1a;
  border-top:1px solid var(--line2);font-size:9px;color:var(--dim);
}}
.statusbar b{{color:var(--amber);font-weight:500}}
.statusbar .live{{color:var(--pos)}}
@keyframes blink{{50%{{opacity:.3}}}}
</style></head><body>
<div class="topbar">
  <span class="logo">TRADINGPOOL TERMINAL</span>
  <span class="rt">
    <span>XAUUSD</span><span>JOURNAL v3</span>
    <span>UTC {utc_now}</span>
    <span>EXEC: {estatus}</span>
  </span>
</div>
<div class="fnbar">
  <span class="on">F1 JOURNAL</span><span>F2 GATE</span><span>F3 EXEC</span>
  <span>F4 BLOTTER</span><span>F5 EXPORT</span>
</div>
<div class="main">
  <div class="panels">
    <div class="pnl">
      <div class="pnl-h"><span>GATE MONITOR</span><span class="{'gate-ok' if gate_ok else 'gate-no'}">{gtxt}</span></div>
      <div class="pnl-b">
        <div class="row"><span class="k">READINESS</span><span class="bar">[{bar}]</span><span class="c-amber"> {pct}%</span></div>
        <div class="row"><span class="k">COMPLETED</span><span class="v">{st['completed']}/{GATE_TRADES}</span></div>
        <div class="row"><span class="k">WIN RATE</span><span class="{'c-pos' if wr_ok else 'c-warn'}">{st['wr']}%</span><span class="k"> (REQ &gt;{GATE_WINRATE}%)</span></div>
        <div class="row"><span class="k">NET R</span><span class="{'c-pos' if st['totR']>=0 else 'c-neg'}">{st['totR']:+.2f}</span></div>
        <div class="row"><span class="k">RECORD</span><span class="v">{st['wins']}W {st['losses']}L {st['be']}BE</span></div>
        <div class="row"><span class="k">STREAK</span><span class="v">{stk}</span></div>
        <div class="row"><span class="k">BREACHES(L{GATE_CLEAN_WINDOW})</span><span class="{'c-neg' if st['breaches_last'] else 'c-pos'}">{st['breaches_last']}</span></div>
        <div class="row"><span class="k">UNGRADED</span><span class="{'c-neg' if st['ungraded'] else 'c-pos'}">{st['ungraded']}</span></div>
        <div class="row"><span class="k">SIGNALS/SKIP</span><span class="v">{st['signals']}/{st['skips']}</span></div>
      </div>
    </div>
    <div class="pnl">
      <div class="pnl-h"><span>EXECUTION DESK</span><span class="{'exec-live' if live else 'exec-off'}">{estr}</span></div>
      <div class="pnl-b">
        <div class="row"><span class="k">DRIVER</span><span class="c-cyan">{EXECUTION_DRIVER.upper()}</span></div>
        <div class="row"><span class="k">STATUS</span><span class="{'exec-live' if live else 'c-warn'}">{estatus}</span></div>
        <div class="row"><span class="k">ARMED</span><span class="{'c-pos' if is_armed() else 'c-dim'}">{'YES' if is_armed() else 'NO'}</span></div>
        <div class="row"><span class="k">RISK PCT</span><span class="v">{RISK_PCT}%</span></div>
        <div class="row"><span class="k">ACCOUNT</span><span class="v">${int(ACCOUNT_BALANCE):,}</span></div>
        <div class="row"><span class="k">SYMBOL</span><span class="c-amber">{EXEC_SYMBOL}</span></div>
        <div class="row"><span class="k">GATE LOCK</span><span class="{'c-pos' if gate_ok else 'c-neg'}">{'OPEN' if gate_ok else 'SEALED'}</span></div>
        <div class="row"><span class="k">TRIPLE LOCK</span><span class="c-dim">DRIVER + ARM + GATE</span></div>
      </div>
    </div>
  </div>
  <div class="pnl blotter">
    <div class="pnl-h"><span>TRADE BLOTTER</span><span class="c-dim">LAST {min(30, len(st['trades']))} POSITIONS</span></div>
    <div class="pnl-b">
      <table>
        <thead><tr>
          <th>#</th><th>TIME_UTC</th><th>SYM</th><th>SIDE</th><th>SESS</th>
          <th>OUT</th><th>R_MULT</th><th>PROC</th><th>LAT_S</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  </div>
  <div class="statusbar">
    <span><b>MSG</b> JOURNAL EPHEMERAL ON FREE HOST - BACKUP /export?secret=...</span>
    <span><b>TG</b> /stats /arm /disarm</span>
    <span class="{'live' if live else ''}"><b>SYS</b> {'EXECUTION LIVE' if live else 'JOURNAL-ONLY MODE'}</span>
  </div>
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
