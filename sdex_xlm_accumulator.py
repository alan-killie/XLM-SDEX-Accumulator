import os
import time
import json
from datetime import datetime
from stellar_sdk import Server, Keypair, TransactionBuilder, Network, Asset

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------
SECRET_KEY = os.getenv("STELLAR_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("STELLAR_SECRET_KEY environment variable is missing.")

kp = Keypair.from_secret(SECRET_KEY)
public_key = kp.public_key

server = Server("https://horizon.stellar.org")
XLM = Asset.native()
USDC = Asset("USDC", "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN")

STATE_FILE = "grid_state.json"
NUM_TIERS = 10
MIN_TRADE_USDC = 1.0
PROFIT_MARGIN = 1.020      # +2.0% profit target
DIP_THRESHOLD = 0.990      # -1.0% dip trigger
MAX_OFFER_AGE_HOURS = 1.0
DRIFT_THRESHOLD = 0.015    # 1.5% price drift to expire stale buys


def load_state():
    """Load grid state schema with ID tracking and price metadata enabled."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            try:
                state = json.load(f)
                if "processed_trade_ids" not in state:
                    state["processed_trade_ids"] = []
                return state
            except json.JSONDecodeError:
                pass
    return {
        "open_positions": [],
        "pending_offers": [],
        "processed_trade_ids": [],
        "total_xlm_accumulated": 0.0,
        "last_market_price": None,
    }



def save_state(state):
    """Persist grid state JSON to disk."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_mid_price():
    """Fetch live mid-market XLM/USDC price."""
    try:
        orderbook = server.orderbook(selling=XLM, buying=USDC).call()
        bids, asks = orderbook.get("bids", []), orderbook.get("asks", [])
        if not bids or not asks:
            return None
        return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
    except Exception as e:
        print(f"Error fetching mid price: {e}")
        return None


def reconcile_executed_trades(state):
    """
    Fetches recent trade history and ingests newly filled trades strictly by unique trade ID,
    creating discrete position tranches with individual target profit prices.
    """
    try:
        trades_page = server.trades().for_account(public_key).order(desc=True).limit(20).call()
        records = trades_page.get("_embedded", {}).get("records", [])

        updated = False
        for trade in reversed(records):  # Process chronologically (oldest to newest)
            trade_id = str(trade["id"])

            # Skip if trade ID was already processed
            if trade_id in state["processed_trade_ids"]:
                continue

            state["processed_trade_ids"].append(trade_id)
            updated = True

            # Evaluate direction relative to account
            is_base = trade.get("base_account") == public_key
            base_is_seller = trade.get("base_is_seller", True)
            base_type = trade.get("base_asset_type")
            counter_code = trade.get("counter_asset_code")

            if base_type == "native" and counter_code == "USDC":
                bought_xlm = not base_is_seller if is_base else base_is_seller

                if bought_xlm:
                    xlm_bought = float(trade["base_amount"])
                    usdc_spent = float(trade["counter_amount"])
                    fill_price = usdc_spent / xlm_bought if xlm_bought > 0 else 0.0
                    target_sell_price = round(fill_price * PROFIT_MARGIN, 6)

                    print(
                        f"Tranche Ingested [Trade ID {trade_id}]: "
                        f"Bought {xlm_bought:.4f} XLM @ ${fill_price:.4f} "
                        f"| Target Sell: ${target_sell_price:.4f}"
                    )

                    state["open_positions"].append({
                        "trade_id": trade_id,
                        "buy_price": fill_price,
                        "target_sell_price": target_sell_price,
                        "xlm_amount": xlm_bought,
                        "cost_usdc": usdc_spent,
                        "timestamp": trade.get("ledger_close_time"),
                    })

        # Cap stored IDs list to keep JSON compact
        state["processed_trade_ids"] = state["processed_trade_ids"][-100:]

        if updated:
            save_state(state)
    except Exception as e:
        print(f"Trade reconciliation skipped: {e}")


def sync_and_clean_offers(builder, current_price, state):
    """
    Syncs resting offers into state['pending_offers'] with explicit USD/XLM prices
    and stages cancellation for stale/drifted buy orders.
    """
    cancellation_count = 0
    state["pending_offers"] = []
    try:
        offers_page = server.offers().for_account(public_key).limit(50).call()
        open_offers = offers_page.get("_embedded", {}).get("records", [])
        current_time = time.time()

        for offer in open_offers:
            offer_id = int(offer["id"])
            raw_price = float(offer["price"])
            time_str = offer["last_modified_time"].replace("Z", "+00:00")
            created_at = datetime.fromisoformat(time_str).timestamp()
            age_hours = (current_time - created_at) / 3600.0

            selling_type = offer.get("selling_asset_type")
            is_selling_xlm = (selling_type == "native")

            # Convert to standard $/XLM price
            # Selling XLM -> Horizon price is already USDC/XLM
            # Buying XLM (selling USDC) -> Horizon price is XLM/USDC, so invert it
            usd_per_xlm = raw_price if is_selling_xlm else (1.0 / raw_price if raw_price > 0 else 0.0)

            # Update live open offers in state with human-readable price
            state["pending_offers"].append({
                "id": offer_id,
                "price_usd_per_xlm": round(usd_per_xlm, 6),
                "selling_xlm": is_selling_xlm,
                "created_at": offer["last_modified_time"],
            })

            drift = (current_price - usd_per_xlm) / usd_per_xlm if usd_per_xlm > 0 else 0

            # Only cancel stale or drifted BUY orders
            if not is_selling_xlm and (age_hours >= MAX_OFFER_AGE_HOURS or drift >= DRIFT_THRESHOLD):
                print(
                    f"Cancelling stale Buy Offer ID {offer_id} "
                    f"(${usd_per_xlm:.4f}/XLM, Age: {age_hours:.1f}h, Drift: {drift*100:.1f}%)"
                )
                builder.append_manage_buy_offer_op(
                    selling=USDC, buying=XLM, amount="0", price=offer["price"], offer_id=offer_id
                )
                cancellation_count += 1
    except Exception as e:
        print(f"Notice: Offer check skipped ({e})")

    return cancellation_count



def run_accumulator_bot():
    price = get_mid_price()
    if not price:
        print("Market data unavailable. Aborting cycle.")
        return

    state = load_state()
    state["last_market_price"] = round(price, 6)
    
    # ... rest of run_accumulator_bot logic ...


    # 1. Reconcile On-Chain Fills by Unique Trade ID
    reconcile_executed_trades(state)

    account = server.load_account(public_key)
    account_details = server.accounts().account_id(public_key).call()

    builder = TransactionBuilder(
        source_account=account,
        network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
        base_fee=100,
    )

    action_taken = False

    # 2. Sync Active Open Offers & Clean Stale Buys
    if sync_and_clean_offers(builder, price, state) > 0:
        action_taken = True

    # 3. Fetch Balances
    usdc_balance = 0.0
    xlm_balance = 0.0
    for b in account_details.get("balances", []):
        if b.get("asset_code") == "USDC":
            usdc_balance = float(b["balance"])
        elif b.get("asset_type") == "native":
            xlm_balance = max(0.0, float(b["balance"]) - 2.0)  # Reserve 2 XLM for fees/min balance

    # 4. Evaluate Profit Targets for Open Tranches
    remaining_positions = []
    accumulated_profit_delta = 0.0

    for pos in state.get("open_positions", []):
        target_sell_price = pos.get("target_sell_price", pos["buy_price"] * PROFIT_MARGIN)

        if price >= target_sell_price and xlm_balance >= pos["xlm_amount"]:
            xlm_to_sell = pos["xlm_amount"]
            print(
                f"Tranche Profit Target Hit! Selling {xlm_to_sell:.4f} XLM @ ${price:.4f} "
                f"(Target was ${target_sell_price:.4f})"
            )

            builder.append_manage_sell_offer_op(
                selling=XLM,
                buying=USDC,
                amount=f"{xlm_to_sell:.7f}",
                price=f"{price * 0.999:.6f}",  # 0.1% buffer for immediate taker execution
                offer_id=0,
            )
            accumulated_profit_delta += (xlm_to_sell * price) - pos["cost_usdc"]
            xlm_balance -= xlm_to_sell
            action_taken = True
        else:
            remaining_positions.append(pos)

    state["open_positions"] = remaining_positions

    # 5. Evaluate Dip-Buy Condition
    calculated_chunk = usdc_balance / NUM_TIERS
    trade_size_usdc = max(calculated_chunk, MIN_TRADE_USDC)

    lowest_entry = (
        min(pos["buy_price"] for pos in state["open_positions"])
        if state["open_positions"]
        else None
    )
    is_price_lower = (lowest_entry is None) or (price <= lowest_entry * DIP_THRESHOLD)
    has_active_buy = any(not o.get("selling_xlm", False) for o in state.get("pending_offers", []))

    if usdc_balance >= trade_size_usdc and is_price_lower and not has_active_buy:
        target_buy_price = price * DIP_THRESHOLD
        xlm_to_buy = trade_size_usdc / target_buy_price

        print(f"Dip Target Met: Placing Buy Offer {xlm_to_buy:.4f} XLM @ ${target_buy_price:.4f}")

        builder.append_manage_buy_offer_op(
            selling=USDC,
            buying=XLM,
            amount=f"{xlm_to_buy:.7f}",
            price=f"{target_buy_price:.6f}",
            offer_id=0,
        )
        action_taken = True

        # 6. Submit Multi-Operation Transaction & Persist State
    if action_taken:
        try:
            # Increase timeout from 30 to 180 seconds to avoid tx_too_late errors
            tx = builder.set_timeout(180).build()
            tx.sign(kp)
            res = server.submit_transaction(tx)

            state["total_xlm_accumulated"] += accumulated_profit_delta
            save_state(state)
            print("Transaction envelope submitted successfully.")
        except Exception as e:
            print(f"Transaction submission failed: {e}")

    else:
        save_state(state)
        print("No actions required this cycle.")


if __name__ == "__main__":
    run_accumulator_bot()
