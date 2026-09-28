import os
import time
import json
from datetime import datetime
from stellar_sdk import Server, Keypair, TransactionBuilder, Network, Asset

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
PROFIT_MARGIN = 1.020
DIP_THRESHOLD = 0.990
MAX_OFFER_AGE_HOURS = 1.0
DRIFT_THRESHOLD = 0.015  # 1.5% price drift to expire stale buys

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"open_positions": [], "pending_offers": [], "total_xlm_accumulated": 0.0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def parse_asset(asset_dict):
    if asset_dict["asset_type"] == "native":
        return Asset.native()
    return Asset(asset_dict["asset_code"], asset_dict["asset_issuer"])

def get_mid_price():
    orderbook = server.orderbook(selling=XLM, buying=USDC).call()
    bids, asks = orderbook.get("bids", []), orderbook.get("asks", [])
    if not bids or not asks:
        return None
    return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2

def reconcile_executed_trades(state):
    """
    Fetches recent executed trades from Horizon and records newly filled 
    buy orders into state['open_positions'].
    """
    try:
        trades_page = server.trades().for_account(public_key).order(desc=True).limit(10).call()
        records = trades_page.get("_embedded", {}).get("records", [])
        
        updated = False
        for trade in reversed(records):  # Process oldest to newest
            bought_asset = trade.get("bought_asset_type")
            sold_asset = trade.get("sold_asset_code")
            
            if bought_asset == "native" and sold_asset == "USDC":
                xlm_bought = float(trade["bought_amount"])
                usdc_spent = float(trade["sold_amount"])
                fill_price = usdc_spent / xlm_bought if xlm_bought > 0 else 0.0

                already_logged = any(
                    abs(pos.get("buy_price", 0.0) - fill_price) < 0.0001 and abs(pos.get("xlm_amount", 0.0) - xlm_bought) < 0.001
                    for pos in state.get("open_positions", [])
                )

                if not already_logged:
                    print(f"Detected On-Chain Fill: {xlm_bought:.7f} XLM @ ${fill_price:.4f}")
                    state["open_positions"].append({
                        "buy_price": fill_price,
                        "xlm_amount": xlm_bought,
                        "cost_usdc": usdc_spent
                    })
                    updated = True
        
        if updated:
            save_state(state)
    except Exception as e:
        print(f"Trade reconciliation skipped: {e}")

def sync_and_clean_offers(builder, current_price):
    cancellation_count = 0
    try:
        offers_page = server.offers().for_account(public_key).call()
        open_offers = offers_page.get("_embedded", {}).get("records", [])
        current_time = time.time()

        for offer in open_offers:
            offer_id = int(offer["id"])
            offer_price = float(offer["price"])
            time_str = offer["last_modified_time"].replace("Z", "+00:00")
            created_at = datetime.fromisoformat(time_str).timestamp()
            age_hours = (current_time - created_at) / 3600.0

            drift = (current_price - offer_price) / offer_price if offer_price > 0 else 0

            if age_hours >= MAX_OFFER_AGE_HOURS or drift >= DRIFT_THRESHOLD:
                print(f"Cancelling stale/drifted Offer ID {offer_id} (Age: {age_hours:.1f}h, Drift: {drift*100:.1f}%)")
                
                selling_asset = parse_asset(offer["selling"])
                if selling_asset == XLM:
                    builder.append_manage_sell_offer_op(
                        selling=XLM, buying=USDC, amount="0", price=offer["price"], offer_id=offer_id
                    )
                else:
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

    # 1. Reconcile On-Chain Fills First
    reconcile_executed_trades(state)

    account = server.load_account(public_key)
    account_details = server.accounts().account_id(public_key).call()

    builder = TransactionBuilder(
        source_account=account,
        network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
        base_fee=100
    )

    action_taken = False

    # 2. Clear Stale or Drifted Orders
    if sync_and_clean_offers(builder, price) > 0:
        action_taken = True

    # 3. Get Available Balances
    usdc_balance = 0.0
    xlm_balance = 0.0
    for b in account_details.get("balances", []):
        if b.get("asset_code") == "USDC":
            usdc_balance = float(b["balance"])
        elif b.get("asset_type") == "native":
            xlm_balance = max(0.0, float(b["balance"]) - 2.0)

    # 4. Process Sell Targets (Mutate active state directly)
    remaining_positions = []
    accumulated_profit_delta = 0.0

    for pos in state.get("open_positions", []):
        target_sell_price = pos["buy_price"] * PROFIT_MARGIN

        if price >= target_sell_price and xlm_balance >= pos["xlm_amount"]:
            xlm_to_sell = pos["xlm_amount"]
            print(f"Target Hit! Selling {xlm_to_sell:.7f} XLM @ ${price:.4f}")

            # Sell slightly below mid-price to ensure instant taker execution
            builder.append_manage_sell_offer_op(
                selling=XLM,
                buying=USDC,
                amount=f"{xlm_to_sell:.7f}",
                price=f"{price * 0.999:.6f}",
                offer_id=0
            )
            accumulated_profit_delta += (xlm_to_sell * price) - pos["cost_usdc"]
            xlm_balance -= xlm_to_sell
            action_taken = True
        else:
            remaining_positions.append(pos)

    # Update state positions immediately so we don't drop newly reconciled items
    state["open_positions"] = remaining_positions

    # 5. Process Dip Buys
    calculated_chunk = usdc_balance / NUM_TIERS
    trade_size_usdc = max(calculated_chunk, MIN_TRADE_USDC)

    last_buy_price = state["open_positions"][-1]["buy_price"] if state["open_positions"] else None
    is_price_lower = (last_buy_price is None) or (price <= last_buy_price * DIP_THRESHOLD)

    if usdc_balance >= trade_size_usdc and is_price_lower:
        target_buy_price = price * DIP_THRESHOLD
        xlm_to_buy = trade_size_usdc / target_buy_price
        
        print(f"Dip Detected! Placing Buy Offer: {xlm_to_buy:.7f} XLM @ ${target_buy_price:.4f}")

        builder.append_manage_buy_offer_op(
            selling=USDC,
            buying=XLM,
            amount=f"{xlm_to_buy:.7f}",
            price=f"{target_buy_price:.6f}",
            offer_id=0
        )
        action_taken = True

    # 6. Submit Transaction
    if action_taken:
        try:
            tx = builder.set_timeout(30).build()
            tx.sign(kp)
            res = server.submit_transaction(tx)
            
            state["total_xlm_accumulated"] += accumulated_profit_delta
            save_state(state)
            print("Transaction submitted successfully.")
        except Exception as e:
            print(f"Transaction submission failed: {e}")
    else:
        print("No actions required this cycle.")

if __name__ == "__main__":
    run_accumulator_bot()
