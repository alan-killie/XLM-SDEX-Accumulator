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
MAX_OFFER_AGE_HOURS = 24.0
DRIFT_THRESHOLD = 0.03  # 3% price drift to expire stale buys

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

            # Calculate upward price drift away from active buy offer
            drift = (current_price - offer_price) / offer_price if offer_price > 0 else 0

            if age_hours >= MAX_OFFER_AGE_HOURS or drift >= DRIFT_THRESHOLD:
                print(f"Cancelling stale/drifted Offer ID {offer_id} (Age: {age_hours:.1f}h, Drift: {drift*100:.1f}%)")
                
                selling_asset = parse_asset(offer["selling"])
                buying_asset = parse_asset(offer["buying"])

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
    account = server.load_account(public_key)
    account_details = server.accounts().account_id(public_key).call()

    builder = TransactionBuilder(
        source_account=account,
        network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
        base_fee=100
    )

    action_taken = False

    # 1. Clear Stale or Drifted Orders
    if sync_and_clean_offers(builder, price) > 0:
        action_taken = True

    # 2. Get Available Balances
    usdc_balance = 0.0
    xlm_balance = 0.0
    for b in account_details.get("balances", []):
        if b.get("asset_code") == "USDC":
            usdc_balance = float(b["balance"])
        elif b.get("asset_type") == "native":
            # Reserve 2 XLM minimum buffer for account base + subentries
            xlm_balance = max(0.0, float(b["balance"]) - 2.0)

    pending_positions = []
    accumulated_profit_delta = 0.0

    # 3. Process Sell Targets
    for pos in state.get("open_positions", []):
        target_sell_price = pos["buy_price"] * PROFIT_MARGIN

        if price >= target_sell_price and xlm_balance >= pos["xlm_amount"]:
            xlm_to_sell = pos["xlm_amount"]
            print(f"Target Hit! Selling {xlm_to_sell:.7f} XLM @ ${price:.4f}")

            builder.append_manage_sell_offer_op(
                selling=XLM,
                buying=USDC,
                amount=f"{xlm_to_sell:.7f}",
                price=f"{price:.6f}",
                offer_id=0
            )
            accumulated_profit_delta += (xlm_to_sell * price) - pos["cost_usdc"]
            action_taken = True
        else:
            pending_positions.append(pos)

    # 4. Process Dip Buys
    calculated_chunk = usdc_balance / NUM_TIERS
    trade_size_usdc = max(calculated_chunk, MIN_TRADE_USDC)

    last_buy_price = pending_positions[-1]["buy_price"] if pending_positions else None
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
        # NOTE: Position is NOT added to grid_state.json here.
        # It will only be appended after DEX trade execution is confirmed on a subsequent run.

    # 5. Submit Transaction
    if action_taken:
        try:
            tx = builder.set_timeout(30).build()
            tx.sign(kp)
            res = server.submit_transaction(tx)
            
            state["open_positions"] = pending_positions
            state["total_xlm_accumulated"] += accumulated_profit_delta
            save_state(state)
            print("Transaction submitted successfully.")
        except Exception as e:
            print(f"Transaction submission failed: {e}")
    else:
        print("No actions required this cycle.")

if __name__ == "__main__":
    run_accumulator_bot()
