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

bot_active = True
daily_trade_count = 0
last_trade_day = time.strftime("%Y-%m-%d")
last_update_id = None
paper_position = None

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

@app.on_event("startup")
def startup_event():
    threading.Thread(target=telegram_polling, daemon=True).start()
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
        "paper_position": paper_position
    }

@app.post("/webhook")
async def webhook(req: Request):
    global daily_trade_count, last_trade_day, paper_position

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

        if BOT_MODE != "live":
            entry_price = float(data.get("price", 0))
            if entry_price > 0:
                paper_position = calculate_levels(entry_price)
                paper_position["symbol"] = symbol
                paper_position["amount_try"] = amount_try

        telegram(f"✅ ALIŞ\n{symbol}\n{amount_try} TRY\n{BOT_MODE}\n{order}\nPozisyon: {paper_position}")
        return {"ok": True, "order": order, "paper_position": paper_position}

    if side == "SELL":
        if not has_btc():
            telegram("⚠️ Satılacak BTC yok.")
            return {"blocked": "no btc"}

        order = place_market_order(symbol, "SELL", amount_try)
        daily_trade_count += 1

        closed_position = paper_position
        if BOT_MODE != "live":
            paper_position = None

        telegram(f"🔴 SATIŞ\n{symbol}\nTüm BTC bakiyesi\n{BOT_MODE}\n{order}\nKapanan pozisyon: {closed_position}")
        return {"ok": True, "order": order, "closed_position": closed_position}
