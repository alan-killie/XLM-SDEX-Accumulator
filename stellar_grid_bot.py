"""
Stellar XLM accumulation grid bot.

Strategy
--------
1. Buy ~TRADE_SIZE_USDC of XLM with a resting bid DIP_FACTOR below market
   whenever price is DIP_FACTOR below the lowest open position (or there is
   no open position).
2. When a buy fills, immediately place a resting sell at buy_price * PROFIT_FACTOR
   for just enough XLM to recover the original USDC cost.
3. When that sell fills, the leftover XLM is pure profit: it is never sold again
   and is added to total_xlm_accumulated. The recovered USDC funds the next buy.

Run it on a schedule (cron / systemd timer). Set DRY_RUN=1 to print the
operations without submitting anything.
"""
import os
import json
import time
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, getcontext

from stellar_sdk import Server, Keypair, TransactionBuilder, Network, Asset, Account

getcontext().prec = 28

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
SECRET_KEY = os.getenv("STELLAR_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("STELLAR_SECRET_KEY environment variable is missing.")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

kp = Keypair.from_secret(SECRET_KEY)
public_key = kp.public_key

server = Server("https://horizon.stellar.org")
USDC_ISSUER = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"
XLM = Asset.native()
USDC = Asset("USDC", USDC_ISSUER)

STATE_FILE = "grid_state.json"
TRADE_SIZE_USDC = Decimal("1.0")     # fixed chunk per buy
DIP_FACTOR = Decimal("0.99")         # bid 1% below market / 1% below lowest open position
PROFIT_FACTOR = Decimal("1.02")      # sell target 2% above the actual fill price
MAX_OFFER_AGE_HOURS = 1.0            # cancel stale buy bids
DRIFT_THRESHOLD = Decimal("0.015")   # cancel bids the market has run 1.5% away from
OFFER_RESERVE = Decimal("0.5")       # each open offer adds a 0.5 XLM subentry reserve

Q7 = Decimal("0.0000001")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def D(x):
    return Decimal(str(x))


def ceil7(x):
    return x.quantize(Q7, rounding=ROUND_CEILING)


def floor7(x):
    return x.quantize(Q7, rounding=ROUND_DOWN)


def fmt(x):
    return format(x.quantize(Q7), "f")


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def load_state():
    state = {
        "positions": [],
        "trade_cursor": None,
        "total_xlm_accumulated": "0",
        "total_fees_xlm": "0",
        "closed": [],
    }
    if not os.path.exists(STATE_FILE):
        return state
    try:
        with open(STATE_FILE) as f:
            loaded = json.load(f)
    except json.JSONDecodeError:
        return state

    if "positions" in loaded:
        state.update(loaded)
        return state

    # Migrate the old format. NOTE: the old total_xlm_accumulated was actually a
    # USDC figure, so it is deliberately not carried over. Old open_positions may
    # contain duplicates from the old reconciliation bug: check them by hand.
    for p in loaded.get("open_positions", []):
        state["positions"].append({
            "buy_price": str(p["buy_price"]),
            "xlm_amount": str(p["xlm_amount"]),
            "cost_usdc": str(p["cost_usdc"]),
            "sell_submitted": False,
        })
    return state


def ensure_targets(p):
    """Compute the sell target and the XLM amount that recovers the USDC cost."""
    if "target_price" in p and "sell_xlm" in p:
        return
    target = ceil7(D(p["buy_price"]) * PROFIT_FACTOR)
    sell_xlm = ceil7(D(p["cost_usdc"]) / target)   # rounded up so USDC back >= cost
    p["target_price"] = str(target)
    p["sell_xlm"] = str(sell_xlm)


# --------------------------------------------------------------------------
# Market / account data
# --------------------------------------------------------------------------
def get_mid_price():
    try:
        ob = server.orderbook(selling=XLM, buying=USDC).call()
        bids, asks = ob.get("bids", []), ob.get("asks", [])
        if not bids or not asks:
            return None
        return (D(bids[0]["price"]) + D(asks[0]["price"])) / 2
    except Exception as e:
        print(f"Error fetching mid price: {e}")
        return None


def available_balances(details):
    """Spendable XLM and USDC after reserves and amounts locked in open offers."""
    xlm = usdc = Decimal(0)
    for b in details.get("balances", []):
        if b.get("asset_type") == "native":
            reserve = (
                2
                + int(details.get("subentry_count", 0))
                + int(details.get("num_sponsoring", 0))
                - int(details.get("num_sponsored", 0))
            ) * Decimal("0.5")
            xlm = D(b["balance"]) - reserve - D(b.get("selling_liabilities", "0"))
        elif b.get("asset_code") == "USDC" and b.get("asset_issuer") == USDC_ISSUER:
            usdc = D(b["balance"]) - D(b.get("selling_liabilities", "0"))
    return max(xlm, Decimal(0)), max(usdc, Decimal(0))


# --------------------------------------------------------------------------
# Reconcile buy fills from chain (cursor based: no duplicates, no re-adds)
# --------------------------------------------------------------------------
def parse_buy_fill(t):
    """Return (our_offer_id, xlm_bought, usdc_spent) if this trade bought XLM for us."""
    base_native = t.get("base_asset_type") == "native"
    counter_native = t.get("counter_asset_type") == "native"
    if base_native == counter_native:
        return None
    other = "counter" if base_native else "base"
    if t.get(f"{other}_asset_code") != "USDC" or t.get(f"{other}_asset_issuer") != USDC_ISSUER:
        return None

    we_are_base = t.get("base_account") == public_key
    we_are_counter = t.get("counter_account") == public_key
    if we_are_base == we_are_counter:
        return None

    base_is_seller = t["base_is_seller"]
    sold_base = base_is_seller if we_are_base else not base_is_seller
    bought_xlm = (sold_base and counter_native) or (not sold_base and base_native)
    if not bought_xlm:
        return None

    xlm = D(t["base_amount"] if base_native else t["counter_amount"])
    usdc = D(t["counter_amount"] if base_native else t["base_amount"])
    offer_id = t.get("base_offer_id") if we_are_base else t.get("counter_offer_id")
    return str(offer_id), xlm, usdc


def reconcile_buys(state):
    cursor = state.get("trade_cursor")

    if cursor is None:
        # First run: skip existing history, only track fills from here on.
        latest = (
            server.trades().for_account(public_key).order(desc=True).limit(1).call()
            .get("_embedded", {}).get("records", [])
        )
        state["trade_cursor"] = latest[0]["paging_token"] if latest else "0"
        save_state(state)
        return

    fills = {}  # our offer id -> [xlm, usdc]
    while True:
        recs = (
            server.trades().for_account(public_key).order(desc=False)
            .cursor(cursor).limit(200).call()
            .get("_embedded", {}).get("records", [])
        )
        if not recs:
            break
        for t in recs:
            cursor = t["paging_token"]
            fill = parse_buy_fill(t)
            if fill:
                oid, xlm, usdc = fill
                agg = fills.setdefault(oid, [Decimal(0), Decimal(0)])
                agg[0] += xlm
                agg[1] += usdc
        if len(recs) < 200:
            break

    for oid, (xlm, usdc) in fills.items():
        price = usdc / xlm
        print(f"Buy filled: {xlm:.7f} XLM @ ${price:.5f} (cost ${usdc:.5f})")
        state["positions"].append({
            "offer_id": oid,
            "buy_price": str(price),
            "xlm_amount": str(xlm),
            "cost_usdc": str(usdc),
            "sell_submitted": False,
        })

    state["trade_cursor"] = cursor
    save_state(state)


# --------------------------------------------------------------------------
# Offers
# --------------------------------------------------------------------------
def fetch_offers():
    """Return (sell_offers, buy_offers), normalised. Raises on failure."""
    recs = (
        server.offers().for_account(public_key).limit(200).call()
        .get("_embedded", {}).get("records", [])
    )
    sells, buys = [], []
    now = time.time()
    for o in recs:
        selling_xlm = o.get("selling", {}).get("asset_type") == "native"
        price = D(o["price"])          # price of the SELLING asset in BUYING asset
        item = {
            "id": int(o["id"]),
            "amount": D(o["amount"]),  # amount of the SELLING asset
            "age_hours": (
                now - datetime.fromisoformat(o["last_modified_time"].replace("Z", "+00:00")).timestamp()
            ) / 3600.0,
        }
        if selling_xlm:
            item["price"] = price                      # USDC per XLM
            sells.append(item)
        else:
            item["price"] = (1 / price) if price > 0 else Decimal(0)   # invert to USDC per XLM
            buys.append(item)
    return sells, buys


def close_filled_positions(state, sell_offers):
    """A position is closed once its resting sell has left the book."""
    claimed = set()
    remaining = []
    for p in state["positions"]:
        if not p.get("sell_submitted"):
            remaining.append(p)
            continue
        target = D(p["target_price"])
        sell_xlm = D(p["sell_xlm"])
        match = next(
            (o for o in sell_offers
             if o["id"] not in claimed
             and abs(o["price"] - target) <= Decimal("0.000001")
             and o["amount"] <= sell_xlm + Q7),
            None,
        )
        if match:
            claimed.add(match["id"])
            remaining.append(p)
        else:
            kept = D(p["xlm_amount"]) - sell_xlm
            state["total_xlm_accumulated"] = str(D(state["total_xlm_accumulated"]) + kept)
            state["closed"].append({
                "buy_price": p["buy_price"],
                "target_price": p["target_price"],
                "xlm_kept": str(kept),
                "closed_at": datetime.utcnow().isoformat(),
            })
            print(f"Cycle complete: kept {kept:.7f} XLM profit")
    state["positions"] = remaining
    state["closed"] = state["closed"][-50:]
    save_state(state)


# --------------------------------------------------------------------------
# Main cycle
# --------------------------------------------------------------------------
def run_accumulator_bot():
    price = get_mid_price()
    if not price:
        print("Market data unavailable. Aborting cycle.")
        return

    state = load_state()

    try:
        details = server.accounts().account_id(public_key).call()
        reconcile_buys(state)
        sell_offers, buy_offers = fetch_offers()
    except Exception as e:
        print(f"Chain data unavailable, aborting cycle without changes: {e}")
        return

    close_filled_positions(state, sell_offers)

    # Sequence comes from the same account call, so there is only one lookup.
    account = Account(public_key, int(details["sequence"]))
    builder = TransactionBuilder(
        source_account=account,
        network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
        base_fee=100,
    )
    xlm_avail, usdc_avail = available_balances(details)
    op_count = 0

    # 1. Cancel stale / drifted buy bids (never touches resting sells)
    cancelled_ids = set()
    for o in buy_offers:
        drift = (price - o["price"]) / o["price"] if o["price"] > 0 else Decimal(0)
        if o["age_hours"] >= MAX_OFFER_AGE_HOURS or drift >= DRIFT_THRESHOLD:
            print(f"Cancelling buy bid {o['id']} (age {o['age_hours']:.1f}h, drift {drift * 100:.1f}%)")
            builder.append_manage_buy_offer_op(
                selling=USDC, buying=XLM, amount="0", price=fmt(o["price"]), offer_id=o["id"]
            )
            cancelled_ids.add(o["id"])
            op_count += 1

    # 2. Place a resting sell for every filled buy that does not have one yet
    new_sells = []
    for p in state["positions"]:
        if p.get("sell_submitted"):
            continue
        ensure_targets(p)
        sell_xlm = D(p["sell_xlm"])
        if sell_xlm >= D(p["xlm_amount"]):
            print(f"Skipping position, sell amount not below holding: {p}")
            continue
        if xlm_avail - OFFER_RESERVE >= sell_xlm:
            print(f"Placing sell: {sell_xlm:.7f} XLM @ ${D(p['target_price']):.6f} "
                  f"(keeps {D(p['xlm_amount']) - sell_xlm:.7f} XLM)")
            builder.append_manage_sell_offer_op(
                selling=XLM, buying=USDC, amount=fmt(sell_xlm),
                price=fmt(D(p["target_price"])), offer_id=0,
            )
            xlm_avail -= sell_xlm + OFFER_RESERVE
            new_sells.append(p)
            op_count += 1
        else:
            print("Not enough spendable XLM to place sell yet; will retry next cycle.")

    # 3. Dip buy (skipped in a cycle that cancelled a bid, so freed USDC is not double counted)
    active_buys = [o for o in buy_offers if o["id"] not in cancelled_ids]
    lowest_open = min((D(p["buy_price"]) for p in state["positions"]), default=None)
    dip_ok = lowest_open is None or price <= lowest_open * DIP_FACTOR

    if (not cancelled_ids and not active_buys and dip_ok
            and usdc_avail >= TRADE_SIZE_USDC and xlm_avail >= OFFER_RESERVE):
        bid = floor7(price * DIP_FACTOR)
        xlm_to_buy = floor7(TRADE_SIZE_USDC / bid)
        print(f"Dip: placing buy {xlm_to_buy:.7f} XLM @ ${bid:.6f}")
        builder.append_manage_buy_offer_op(
            selling=USDC, buying=XLM, amount=fmt(xlm_to_buy), price=fmt(bid), offer_id=0
        )
        op_count += 1

    # 4. Submit
    if op_count == 0:
        print("No actions required this cycle.")
        return

    if DRY_RUN:
        print(f"DRY_RUN: {op_count} operation(s) built, nothing submitted.")
        return

    try:
        tx = builder.set_timeout(30).build()
        tx.sign(kp)
        res = server.submit_transaction(tx)
    except Exception as e:
        print(f"Transaction submission failed (state unchanged): {e}")
        return

    for p in new_sells:
        p["sell_submitted"] = True
    fee_xlm = D(res.get("fee_charged", 0)) / Decimal(10_000_000)
    state["total_fees_xlm"] = str(D(state["total_fees_xlm"]) + fee_xlm)
    save_state(state)
    print("Transaction submitted successfully.")

    net = D(state["total_xlm_accumulated"]) - D(state["total_fees_xlm"])
    print(f"Accumulated: {D(state['total_xlm_accumulated']):.7f} XLM "
          f"(fees {D(state['total_fees_xlm']):.7f}, net {net:.7f}); "
          f"open positions: {len(state['positions'])}")


if __name__ == "__main__":
    run_accumulator_bot()
