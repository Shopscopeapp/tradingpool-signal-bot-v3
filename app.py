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

# â”€â”€ readiness gate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
GATE_TRADES = 20
GATE_CLEAN_WINDOW = 10
GATE_WINRATE = 50.0

# â”€â”€ execution config â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(rec) + "\n")

def jread():
    rows = []
    try:
        with open(JOURNAL) as f:
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
    icon = "ðŸŸ¢" if side == "LONG" else "ðŸ”´"
    return (f"{icon} <b>TAKE IT â€” {side} {s.get('symbol','')}</b>\n"
            f"Session: {s.get('session','?')}\n"
            f"Entry: <code>{s.get('entry','?')}</code>  SL: <code>{s.get('sl','?')}</code>  "
            f"TP: <code>{s.get('tp','?')}</code>\nR:R: <code>{s.get('rr','?')}</code>\n"
            f"<i>All non-negotiables met on confirmed close.</i>")

def send_signal(s):
    sid = uuid.uuid4().hex[:10]
    s["sid"] = sid
    s["signal_time"] = now_iso()
    kb = {"inline_keyboard": [[
        {"text": "âœ… TAKE", "callback_data": f"take:{sid}"},
        {"text": "âŒ SKIP", "callback_data": f"skip:{sid}"}]]}
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

# â”€â”€ stats / gate engine â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
        return "READY (gate met â€” /arm to go live)", False, st
    return f"LIVE via {EXECUTION_DRIVER}", True, st

def stats_text():
    st = build_stats()
    estr, _, _ = exec_status()
    gate = "âœ… GATE MET" if st["gate_ok"] else (
        f"{st['completed']}/{GATE_TRADES} trades Â· "
        f"{st['breaches_last']} breach(es) in last {GATE_CLEAN_WINDOW} Â· "
        f"WR {st['wr']}% vs {GATE_WINRATE}%")
    stk = f"{abs(st['streak'])}{'W' if st['streak']>0 else 'L'}" if st["streak"] else "-"
    return (f"ðŸ“Š <b>Journal</b>\n"
            f"Record: {st['wins']}W-{st['losses']}L-{st['be']}BE (WR {st['wr']}%)\n"
            f"Net: {st['totR']:+.2f}R Â· Streak: {stk}\n"
            f"Signals {st['signals']} Â· Skips {st['skips']} Â· Ungraded {st['ungraded']}\n"
            f"Gate: {gate}\nExecution: {estr}")

# â”€â”€ execution engine (Phase 3, triple-locked) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
        return f"ðŸ““ Journal-only ({estr})"
    lots = calc_lots(entry, sl)
    if lots <= 0:
        jwrite({"type": "execution", "sid": s.get("sid"), "executed": False,
                "reason": "bad lot calc"})
        return "âš ï¸ Execution skipped â€” bad SL distance"
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
        return (f"âš¡ ORDER SENT Â· {lots} lots Â· {res.get('driver')}"
                if ok else f"âŒ Execution failed: {res}")
    except Exception as e:
        jwrite({"type": "execution", "sid": s.get("sid"), "executed": False,
                "reason": str(e)})
        return f"âŒ Execution error: {e}"

# â”€â”€ routes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
        data = open(JOURNAL).read()
    except FileNotFoundError:
        data = ""
    return Response(data, mimetype="application/jsonl",
                    headers={"Content-Disposition": "attachment; filename=journal.jsonl"})

@app.route("/")
def health():
    return "tradingpool bot up â€” dashboard at /dashboard"

@app.route("/dashboard")
def dashboard():
    st = build_stats()
    estr, live, _ = exec_status()
    pct = min(100, int(100 * st["completed"] / GATE_TRADES))
    gcol = "#3ddc84" if st["gate_ok"] else "#ffb24d"
    gtxt = "GATE MET â€” eligible to arm execution" if st["gate_ok"] else \
        f"{st['completed']}/{GATE_TRADES} completed Â· {st['breaches_last']} breach(es) in last {GATE_CLEAN_WINDOW} Â· need WR>{GATE_WINRATE}%"
    ecol = "#3ddc84" if live else "#ffb24d"
    stk = f"{abs(st['streak'])}{'W' if st['streak']>0 else 'L'}" if st["streak"] else "â€”"
    rows = ""
    for t in reversed(st["trades"][-30:]):
        res = t["result"] or "open"
        rc = {"win": "#3ddc84", "loss": "#ef5350", "be": "#ffb24d"}.get(res, "#5a6862")
        cl = "â€”" if t["clean"] is None else ("clean" if t["clean"] else "BREACH")
        cc = "#5a6862" if t["clean"] is None else ("#3ddc84" if t["clean"] else "#ef5350")
        rl = "" if t["realized"] is None else f"{t['realized']:+.2f}R"
        rows += (f"<tr><td>{t['time'][:16].replace('T',' ')}</td>"
                 f"<td>{t['side'].upper()}</td><td>{t['session']}</td>"
                 f"<td style='color:{rc}'>{res.upper()}</td>"
                 f"<td>{rl}</td><td style='color:{cc}'>{cl}</td></tr>")
    html = f"""<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Tradingpool Journal</title><style>
body{{background:#0a0e0d;color:#c8d3ce;font-family:'SF Mono',ui-monospace,monospace;margin:0;padding:16px}}
.card{{background:#0f1513;border:1px solid #1c2622;border-radius:2px;padding:14px 16px;margin-bottom:12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}}
.k{{font-size:10px;color:#5a6862;letter-spacing:.12em;text-transform:uppercase;margin-bottom:4px}}
.v{{font-size:22px;font-weight:600}}
.bar{{height:8px;background:#1c2622;border-radius:4px;overflow:hidden;margin-top:8px}}
.fill{{height:100%;background:{gcol};width:{pct}%}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
td,th{{padding:6px 8px;border-bottom:1px solid #1c2622;text-align:left}}
th{{color:#5a6862;font-size:10px;letter-spacing:.1em;text-transform:uppercase}}
</style></head><body>
<div class=card><div class=k>Funded readiness gate</div>
<div style="color:{gcol};font-size:14px">{gtxt}</div><div class=bar><div class=fill></div></div></div>
<div class=card><div class=k>Execution</div><div style="color:{ecol};font-size:14px">{estr} Â· risk {RISK_PCT}% on ${int(ACCOUNT_BALANCE)}</div></div>
<div class=grid>
<div class=card><div class=k>Record</div><div class=v>{st['wins']}W-{st['losses']}L-{st['be']}BE</div></div>
<div class=card><div class=k>Win rate</div><div class=v style="color:{'#3ddc84' if st['wr']>GATE_WINRATE else '#ffb24d'}">{st['wr']}%</div></div>
<div class=card><div class=k>Net R</div><div class=v style="color:{'#3ddc84' if st['totR']>=0 else '#ef5350'}">{st['totR']:+.2f}</div></div>
<div class=card><div class=k>Streak</div><div class=v>{stk}</div></div>
<div class=card><div class=k>Signals / Skips</div><div class=v>{st['signals']} / {st['skips']}</div></div>
<div class=card><div class=k>Ungraded</div><div class=v style="color:{'#ef5350' if st['ungraded'] else '#3ddc84'}">{st['ungraded']}</div></div>
</div>
<div class=card><div class=k>Last 30 trades</div>
<table><tr><th>time (utc)</th><th>side</th><th>session</th><th>result</th><th>R</th><th>process</th></tr>{rows}</table></div>
<div class=card style="color:#5a6862;font-size:11px">journal is ephemeral on free hosting â€” download via /export?secret=... Â·
telegram: /stats /arm /disarm</div>
</body></html>"""
    return html

# â”€â”€ telegram callbacks + commands â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
            tg_send(f"âŒ Skipped Â· logged ({latency}s)\n\n{stats_text()}")
            return
        exec_msg = execute_trade(s)
        with state_lock:
            opened[sid] = s
        kb = {"inline_keyboard": [[
            {"text": "ðŸŽ¯ WIN", "callback_data": f"win:{sid}"},
            {"text": "ðŸ›‘ LOSS", "callback_data": f"loss:{sid}"},
            {"text": "âž– BE", "callback_data": f"be:{sid}"}]]}
        tg_send(f"âœ… Taken Â· logged ({latency}s)\n{exec_msg}\n"
                f"When the trade closes, record the outcome:", kb)
    elif action in ("win", "loss", "be"):
        jwrite({"type": "outcome", "sid": sid, "result": action})
        kb = {"inline_keyboard": [[
            {"text": "âœ… CLEAN", "callback_data": f"clean:{sid}"},
            {"text": "âš ï¸ BREACH", "callback_data": f"breach:{sid}"}]]}
        tg_send(f"Outcome logged: <b>{action.upper()}</b>\n"
                f"Process check â€” every non-negotiable followed "
                f"(entry on close, SL placement, 3SL, BE-only management)?", kb)
    elif action in ("clean", "breach"):
        jwrite({"type": "process", "sid": sid, "clean": action == "clean"})
        with state_lock:
            opened.pop(sid, None)
        note = "" if action == "clean" else \
            "\nâš ï¸ Breach logged â€” the clean window resets. Name it in your notes."
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
            tg_send(f"ðŸ”’ Cannot arm â€” gate not met.\n"
                    f"{st['completed']}/{GATE_TRADES} trades Â· "
                    f"{st['breaches_last']} breach(es) in last {GATE_CLEAN_WINDOW} Â· "
                    f"WR {st['wr']}% (need >{GATE_WINRATE}%) Â· "
                    f"ungraded {st['ungraded']}.\nThe gate is the system. Keep going.")
        else:
            set_armed(True)
            tg_send(f"âš¡ ARMED â€” live execution via {EXECUTION_DRIVER}, "
                    f"{RISK_PCT}% risk. /disarm to stop.")
    elif txt == "/disarm":
        set_armed(False)
        tg_send("ðŸ”’ Disarmed â€” back to journal-only.")

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
