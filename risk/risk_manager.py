import os

STOP_LOSS_PERCENT = float(os.getenv("STOP_LOSS_PERCENT", 2))
TAKE_PROFIT_PERCENT = float(os.getenv("TAKE_PROFIT_PERCENT", 4))
TRAILING_STOP_PERCENT = float(os.getenv("TRAILING_STOP_PERCENT", 1.5))

def calculate_levels(entry_price):
    stop_loss = entry_price * (1 - STOP_LOSS_PERCENT / 100)
    take_profit = entry_price * (1 + TAKE_PROFIT_PERCENT / 100)
    trailing_gap = entry_price * (TRAILING_STOP_PERCENT / 100)

    return {
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "trailing_gap": trailing_gap,
        "highest_price": entry_price,
        "trailing_stop": entry_price - trailing_gap,
    }

def update_trailing(position, current_price):
    if not position:
        return position

    if current_price > position["highest_price"]:
        position["highest_price"] = current_price
        position["trailing_stop"] = current_price - position["trailing_gap"]

    return position

def should_close(position, current_price):
    if not position:
        return False, None

    if current_price <= position["stop_loss"]:
        return True, "STOP_LOSS"

    if current_price >= position["take_profit"]:
        return True, "TAKE_PROFIT"

    if current_price <= position["trailing_stop"]:
        return True, "TRAILING_STOP"

    return False, None
