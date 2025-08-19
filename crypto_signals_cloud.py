import os
import time
import json
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

BOT_TOKEN = os.getenv("8270293945:AAECDOAzsAPzONEUzTv0_jn-mwSN3OP6pE4")
CHAT_ID = os.getenv("6796060739")

SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
INTERVAL = os.getenv("INTERVAL", "1m")  # 1m, 3m, 5m, 15m, 1h...
CHECK_SECONDS = int(os.getenv("CHECK_SECONDS", "30"))

EMA_FAST = int(os.getenv("EMA_FAST", "21"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "50"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_LONG_MIN = float(os.getenv("RSI_LONG_MIN", "52"))   # >= triggers long
RSI_SHORT_MAX = float(os.getenv("RSI_SHORT_MAX", "48")) # <= triggers short

ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.0"))
ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "1.5"))

MAX_SIGNALS_PER_DAY = int(os.getenv("MAX_SIGNALS_PER_DAY", "2"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "20"))

if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in env. Set them in your cloud dashboard.")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"

def now_ist():
    return datetime.now(ZoneInfo("Asia/Kolkata"))

def fmt_time_ist():
    return now_ist().strftime("%Y-%m-%d %H:%M IST")

def send_telegram(text: str):
    try:
        r = requests.post(TELEGRAM_API, json={"chat_id": CHAT_ID, "text": text}, timeout=20)
        if r.status_code != 200:
            print("Telegram error:", r.text)
            return False
        return True
    except Exception as e:
        print("Telegram exception:", e)
        return False

def get_klines(symbol: str, interval: str, limit: int = 200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get("https://fapi.binance.com/fapi/v1/klines", params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    kl = [{
        "open_time": int(it[0]),
        "open": float(it[1]),
        "high": float(it[2]),
        "low": float(it[3]),
        "close": float(it[4]),
        "volume": float(it[5]),
        "close_time": int(it[6])
    } for it in data]
    return kl

def ema(values, period):
    k = 2 / (period + 1)
    ema_vals = []
    ema_current = None
    for v in values:
        if ema_current is None:
            ema_current = v
        else:
            ema_current = (v - ema_current) * k + ema_current
        ema_vals.append(ema_current)
    return ema_vals

def rsi(prices, period=14):
    if len(prices) < period + 1:
        return [None] * len(prices)
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i-1]
        gains.append(delta if delta > 0 else 0.0)
        losses.append(-delta if delta < 0 else 0.0)

    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    rs = avg_gain / avg_loss if avg_loss != 0 else 1e9
    rsi_list = [None] * (period) + [100 - (100 / (1 + rs))]

    for i in range(period+1, len(prices)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 1e9
        rsi_list.append(100 - (100 / (1 + rs)))
    while len(rsi_list) < len(prices):
        rsi_list.insert(0, None)
    return rsi_list

def atr(ohlc, period=14):
    if len(ohlc) < period + 1:
        return [None] * len(ohlc)
    trs = [None]
    for i in range(1, len(ohlc)):
        high = ohlc[i]["high"]
        low = ohlc[i]["low"]
        prev_close = ohlc[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    atr_vals = [None]*len(ohlc)
    if len(trs) > period:
        first_atr = sum([t for t in trs[1:period+1] if t is not None]) / period
        atr_vals[period] = first_atr
        for i in range(period+1, len(trs)):
            prev = atr_vals[i-1] if atr_vals[i-1] is not None else first_atr
            atr_vals[i] = (prev * (period - 1) + trs[i]) / period
    return atr_vals

def crossed_above(a_prev, a_now, b_prev, b_now):
    return a_prev is not None and b_prev is not None and a_prev <= b_prev and a_now > b_now

def crossed_below(a_prev, a_now, b_prev, b_now):
    return a_prev is not None and b_prev is not None and a_prev >= b_prev and a_now < b_now

def load_state(path="state.json"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_signal_ts": 0, "signals_today": 0, "date": str(date.today())}

def save_state(state, path="state.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)

def reset_daily_counter_if_needed(state):
    today = str(date.today())
    if state.get("date") != today:
        state["date"] = today
        state["signals_today"] = 0
        save_state(state)

def allowed_to_signal(state):
    reset_daily_counter_if_needed(state)
    if state.get("signals_today", 0) >= MAX_SIGNALS_PER_DAY:
        return False
    minutes_since_last = (time.time() - state.get("last_signal_ts", 0)) / 60.0
    return minutes_since_last >= COOLDOWN_MINUTES

def register_signal(state):
    state["last_signal_ts"] = time.time()
    state["signals_today"] = state.get("signals_today", 0) + 1
    save_state(state)

def format_message(symbol, action, entry, sl, tp):
    pair = f"{symbol[:-4]}/USDT" if symbol.upper().endswith("USDT") else symbol
    return (
        "⚡ Scalp Signal ⚡\\n"
        f"⏰ Time: {fmt_time_ist()}\\n"
        f"Pair: {pair}\\n"
        f"Action: {action}\\n"
        f"Entry: {entry:.2f}\\n"
        f"Stop Loss: {sl:.2f}\\n"
        f"Take Profit: {tp:.2f}\\n"
        "Leverage: 10x–20x\\n"
    )

def scan_once(state):
    if not allowed_to_signal(state):
        return

    for symbol in SYMBOLS:
        try:
            kl = get_klines(symbol, INTERVAL, limit=200)
            closes = [c["close"] for c in kl]
            ema_fast = ema(closes, EMA_FAST)
            ema_slow = ema(closes, EMA_SLOW)
            rsi_vals = rsi(closes, RSI_PERIOD)
            atr_vals = atr(kl, ATR_PERIOD)

            if len(closes) < 3:
                continue

            prev_fast, prev_slow = ema_fast[-2], ema_slow[-2]
            now_fast, now_slow = ema_fast[-1], ema_slow[-1]
            last_rsi = rsi_vals[-1]
            last_atr = atr_vals[-1]
            last_close = closes[-1]

            if last_rsi is None or last_atr is None:
                continue

            if crossed_above(prev_fast, now_fast, prev_slow, now_slow) and last_rsi >= RSI_LONG_MIN:
                entry = last_close
                sl = entry - ATR_SL_MULT * last_atr
                tp = entry + ATR_TP_MULT * last_atr
                msg = format_message(symbol, "LONG", entry, sl, tp)
                if send_telegram(msg):
                    print(fmt_time_ist(), "Sent LONG", symbol, "entry", entry)
                    register_signal(state)
                    return

            if crossed_below(prev_fast, now_fast, prev_slow, now_slow) and last_rsi <= RSI_SHORT_MAX:
                entry = last_close
                sl = entry + ATR_SL_MULT * last_atr
                tp = entry - ATR_TP_MULT * last_atr
                msg = format_message(symbol, "SHORT", entry, sl, tp)
                if send_telegram(msg):
                    print(fmt_time_ist(), "Sent SHORT", symbol, "entry", entry)
                    register_signal(state)
                    return

        except Exception as e:
            print("Scan error for", symbol, ":", repr(e))

def main():
    state = load_state()
    send_telegram("🤖 Signal bot started. Monitoring: " + ", ".join(SYMBOLS) + f" on {INTERVAL}")
    print("Bot running with symbols:", SYMBOLS, "interval:", INTERVAL)
    while True:
        scan_once(state)
        time.sleep(CHECK_SECONDS)

if __name__ == "__main__":
    main()
