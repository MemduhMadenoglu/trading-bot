import os, time, hmac, hashlib, requests, threading
from urllib.parse import urlencode
from fastapi import FastAPI, Request, HTTPException
from risk.risk_manager import calculate_levels, update_trailing, should_close

app = FastAPI()

BOT_MODE = os.getenv("BOT_MODE", "paper")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = str(os.getenv("TELEGRAM_CHAT_ID"))
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

BINANCE_BASE = "https://www.binance.tr"

MAX_TRADE_TRY = 100
MAX_TRADES_PER_DAY = 3
MIN_BTC_POSITION = 0.00005

AUTO_PAPER_TRADE = os.getenv("AUTO_PAPER_TRADE", "false").lower() == "true"
PAPER_TRADE_AMOUNT_TRY = float(os.getenv("PAPER_TRADE_AMOUNT_TRY", 100))
PAPER_START_BALANCE_TRY = float(os.getenv("PAPER_START_BALANCE_TRY", 10000))
RSI_BUY_LEVEL = float(os.getenv("RSI_BUY_LEVEL", 30))
RSI_SELL_LEVEL = float(os.getenv("RSI_SELL_LEVEL", 70))

bot_active = True
daily_trade_count = 0
last_trade_day = time.strftime("%Y-%m-%d")
last_update_id = None
paper_position = None
paper_balance_try = PAPER_START_BALANCE_TRY
paper_realized_pnl = 0.0
paper_total_trades = 0
paper_winning_trades = 0
paper_losing_trades = 0
price_history = []

def telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10)

def sign_params(params):
    query = urlencode(params)
    signature = hmac.new(BINANCE_SECRET_KEY.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + signature

def binance_request(method, path, params=None):
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000

    signed = sign_params(params)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = f"{BINANCE_BASE}{path}?{signed}"

    if method == "GET":
        r = requests.get(url, headers=headers, timeout=15)
    else:
        r = requests.post(url, headers=headers, timeout=15)

    return r.json()

def get_asset_free(asset):
    if BOT_MODE != "live":
        return 0.0

    result = binance_request("GET", "/open/v1/account/spot")
    assets = result.get("data", {}).get("accountAssets", [])

    for item in assets:
        if item.get("asset") == asset:
            return float(item.get("free", 0))

    return 0.0

def has_btc():
    return get_asset_free("BTC") >= MIN_BTC_POSITION

def normalize_symbol(symbol):
    if "_" in symbol:
        return symbol
    if symbol.endswith("TRY"):
        return symbol.replace("TRY", "_TRY")
    return symbol

def get_current_price(symbol="BTC_TRY"):
    raw_symbol = symbol.replace("_", "")

    urls = [
        f"https://api.binance.me/api/v3/ticker/price?symbol={raw_symbol}",
        f"https://api.binance.me/api/v3/ticker/24hr?symbol={raw_symbol}",
    ]

    for url in urls:
        try:
            r = requests.get(url, timeout=10)
            data = r.json()

            if isinstance(data, dict):
                if "price" in data:
                    return float(data["price"])
                if "lastPrice" in data:
                    return float(data["lastPrice"])

        except Exception as e:
            print("Price error:", e)

    return None


def ema(values, period):
    if len(values) < period:
        return None

    k = 2 / (period + 1)
    result = values[0]

    for price in values[1:]:
        result = price * k + result * (1 - k)

    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None

    recent = values[-(period + 1):]
    gains = []
    losses = []

    for i in range(1, len(recent)):
        diff = recent[i] - recent[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def run_auto_paper_strategy(current_price):
    global paper_position, paper_balance_try, daily_trade_count

    if BOT_MODE == "live":
        return

    if not AUTO_PAPER_TRADE:
        return

    if len(price_history) < 30:
        return

    current_rsi = rsi(price_history, 14)
    ema_fast = ema(price_history[-20:], 9)
    ema_slow = ema(price_history[-30:], 21)

    if current_rsi is None or ema_fast is None or ema_slow is None:
        return

    if paper_position is None:
        if current_rsi <= RSI_BUY_LEVEL and ema_fast > ema_slow:
            if paper_balance_try < PAPER_TRADE_AMOUNT_TRY:
                telegram(f"⚠️ AUTO PAPER ALIŞ ENGELLENDİ\nYetersiz sanal bakiye: {round(paper_balance_try, 2)} TRY")
                return

            paper_position = calculate_levels(current_price)
            paper_position["symbol"] = "BTC_TRY"
            paper_position["amount_try"] = PAPER_TRADE_AMOUNT_TRY
            paper_position["qty"] = PAPER_TRADE_AMOUNT_TRY / current_price
            paper_position["strategy"] = "RSI_EMA_AUTO"

            paper_balance_try -= PAPER_TRADE_AMOUNT_TRY
            daily_trade_count += 1

            telegram(
                f"🟢 AUTO PAPER ALIŞ\n"
                f"Sembol: BTC_TRY\n"
                f"Giriş: {current_price}\n"
                f"RSI: {round(current_rsi, 2)}\n"
                f"EMA9: {round(ema_fast, 2)}\n"
                f"EMA21: {round(ema_slow, 2)}\n"
                f"Tutar: {PAPER_TRADE_AMOUNT_TRY} TRY"
            )

    else:
        if current_rsi >= RSI_SELL_LEVEL:
            closed = paper_position
            qty = closed.get("qty", 0)
            entry_value = closed.get("amount_try", 0)
            exit_value = qty * current_price
            pnl = exit_value - entry_value

            paper_balance_try += exit_value
            paper_realized_pnl += pnl
            paper_total_trades += 1

            if pnl > 0:
                paper_winning_trades += 1
            else:
                paper_losing_trades += 1

            paper_position = None

            telegram(
                f"🔴 AUTO PAPER SATIŞ\n"
                f"Sebep: RSI_SELL\n"
                f"Giriş: {closed.get('entry_price')}\n"
                f"Çıkış: {current_price}\n"
                f"PnL: {round(pnl, 2)} TRY\n"
                f"Bakiye: {round(paper_balance_try, 2)} TRY\n"
                f"Toplam PnL: {round(paper_realized_pnl, 2)} TRY\n"
                f"RSI: {round(current_rsi, 2)}"
            )

def place_market_order(symbol, side, amount_try):
    symbol = normalize_symbol(symbol)

    if BOT_MODE != "live":
        return {"paper": True, "symbol": symbol, "side": side, "amount_try": amount_try}

    if side == "BUY":
        params = {
            "symbol": symbol,
            "side": "0",
            "type": "2",
            "quoteOrderQty": str(amount_try)
        }
        return binance_request("POST", "/open/v1/orders", params)

    if side == "SELL":
        btc_qty = get_asset_free("BTC")
        if btc_qty < MIN_BTC_POSITION:
            return {"code": "NO_BTC", "msg": "Satılacak BTC yok", "btc_qty": btc_qty}

        params = {
            "symbol": symbol,
            "side": "1",
            "type": "2",
            "quantity": f"{btc_qty:.8f}"
        }
        return binance_request("POST", "/open/v1/orders", params)

def handle_telegram_command(text):
    global bot_active

    if text == "/stopbot":
        bot_active = False
        telegram("🛑 Bot durduruldu.")

    elif text == "/startbot":
        bot_active = True
        telegram("▶️ Bot aktif.")

    elif text == "/status":
        telegram(
            f"📊 DURUM\n"
            f"Aktif: {bot_active}\n"
            f"Mod: {BOT_MODE}\n"
            f"BTC: {get_asset_free('BTC')}\n"
            f"TRY: {get_asset_free('TRY')}\n"
            f"Günlük işlem: {daily_trade_count}/{MAX_TRADES_PER_DAY}"
        )

def telegram_polling():
    global last_update_id
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"timeout": 10}
            if last_update_id is not None:
                params["offset"] = last_update_id + 1

            r = requests.get(url, params=params, timeout=15)
            for update in r.json().get("result", []):
                last_update_id = update["update_id"]
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id"))
                text = msg.get("text", "")

                if chat_id == TELEGRAM_CHAT_ID:
                    handle_telegram_command(text)

        except Exception as e:
            print("Telegram polling error:", e)

        time.sleep(2)

def position_monitor():
    global paper_position, paper_balance_try, paper_realized_pnl, paper_total_trades, paper_winning_trades, paper_losing_trades, price_history

    while True:
        try:
            current_price = get_current_price("BTC_TRY")

            if current_price:
                price_history.append(current_price)

                if len(price_history) > 200:
                    price_history.pop(0)

                run_auto_paper_strategy(current_price)

                if paper_position:
                    paper_position = update_trailing(paper_position, current_price)
                    close_now, reason = should_close(paper_position, current_price)

                    if close_now:
                        closed = paper_position
                        qty = closed.get("qty", 0)
                        entry_value = closed.get("amount_try", 0)
                        exit_value = qty * current_price
                        pnl = exit_value - entry_value

                        paper_balance_try += exit_value
                        paper_realized_pnl += pnl
                        paper_position = None

                        telegram(
                            f"📉 PAPER POZİSYON KAPANDI\n"
                            f"Sebep: {reason}\n"
                            f"Sembol: {closed.get('symbol')}\n"
                            f"Giriş: {closed.get('entry_price')}\n"
                            f"Çıkış: {current_price}\n"
                            f"PnL: {round(pnl, 2)} TRY\n"
                            f"Bakiye: {round(paper_balance_try, 2)} TRY\n"
                            f"Toplam PnL: {round(paper_realized_pnl, 2)} TRY\n"
                            f"Stop: {closed.get('stop_loss')}\n"
                            f"TP: {closed.get('take_profit')}\n"
                            f"Trailing: {closed.get('trailing_stop')}"
                        )

        except Exception as e:
            print("Position monitor error:", e)

        time.sleep(15)


@app.on_event("startup")
def startup_event():
    threading.Thread(target=telegram_polling, daemon=True).start()
    threading.Thread(target=position_monitor, daemon=True).start()
    telegram("✅ Bot başlatıldı. Bakiye kontrollü sürüm aktif.")

@app.get("/")
def home():
    return {"status": "ok", "mode": BOT_MODE, "bot_active": bot_active}

@app.get("/status")
def status():
    return {
        "mode": BOT_MODE,
        "bot_active": bot_active,
        "btc_balance": get_asset_free("BTC"),
        "try_balance": get_asset_free("TRY"),
        "daily_trade_count": daily_trade_count,
        "max_trades_per_day": MAX_TRADES_PER_DAY,
        "paper_position": paper_position,
        "paper_balance_try": round(paper_balance_try, 2),
        "paper_realized_pnl": round(paper_realized_pnl, 2),
        "paper_total_trades": paper_total_trades,
        "paper_winning_trades": paper_winning_trades,
        "paper_losing_trades": paper_losing_trades,
        "paper_win_rate": round((paper_winning_trades / paper_total_trades) * 100, 2) if paper_total_trades > 0 else 0
    }

@app.post("/webhook")
async def webhook(req: Request):
    global daily_trade_count, last_trade_day, paper_position, paper_balance_try, paper_realized_pnl, paper_total_trades, paper_winning_trades, paper_losing_trades

    if not bot_active:
        telegram("⛔ Bot kapalı, sinyal işlenmedi.")
        return {"blocked": "bot inactive"}

    today = time.strftime("%Y-%m-%d")
    if today != last_trade_day:
        daily_trade_count = 0
        last_trade_day = today

    data = await req.json()

    if data.get("secret") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Bad secret")

    symbol = data.get("symbol")
    side = data.get("side")
    amount_try = float(data.get("amount_try", 0))

    if symbol != "BTC_TRY":
        telegram(f"⚠️ Desteklenmeyen sembol: {symbol}")
        return {"blocked": "unsupported symbol"}

    if side not in ["BUY", "SELL"]:
        raise HTTPException(status_code=400, detail="side BUY veya SELL olmalı")

    if daily_trade_count >= MAX_TRADES_PER_DAY:
        telegram("🛑 Günlük işlem limiti doldu.")
        return {"blocked": "daily limit"}

    if side == "BUY":
        if amount_try <= 0:
            raise HTTPException(status_code=400, detail="amount_try zorunlu")

        if amount_try > MAX_TRADE_TRY:
            amount_try = MAX_TRADE_TRY

        if has_btc():
            telegram("⚠️ Hesapta BTC var, yeni alış engellendi.")
            return {"blocked": "btc exists"}

        order = place_market_order(symbol, "BUY", amount_try)
        daily_trade_count += 1

        telegram(f"✅ ALIŞ\n{symbol}\n{amount_try} TRY\n{BOT_MODE}\n{order}")
        return {"ok": True, "order": order}

    if side == "SELL":
        if not has_btc():
            telegram("⚠️ Satılacak BTC yok.")
            return {"blocked": "no btc"}

        order = place_market_order(symbol, "SELL", amount_try)
        daily_trade_count += 1

        telegram(f"🔴 SATIŞ\n{symbol}\nTüm BTC bakiyesi\n{BOT_MODE}\n{order}")
        return {"ok": True, "order": order}
