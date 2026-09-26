import os
import time
import json
from stellar_sdk import Server, Keypair, TransactionBuilder, Network, Asset

# --- CONFIGURATION ---
SECRET_KEY = os.getenv("STELLAR_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("STELLAR_SECRET_KEY environment variable is missing.")

kp = Keypair.from_secret(SECRET_KEY)
public_key = kp.public_key

server = Server("https://horizon.stellar.org")
XLM = Asset.native()
USDC = Asset("USDC", "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN") # Mainnet Circle USDC

STATE_FILE = "grid_state.json"
NUM_TIERS = 10             # Divide available capital into 10 equal parts
MIN_TRADE_USDC = 1.0       # Floor trade size ($1.00 minimum for SDEX)
PROFIT_MARGIN = 1.020      # +2.0% profit target per tier
MAX_OFFER_AGE_HOURS = 24.0 # Auto-cancel limit orders older than 24h

# --- STATE MANAGEMENT ---
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"open_positions": [], "total_xlm_accumulated": 0.0}

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

# --- AUTO-CANCEL STALE SDEX OFFERS ---
def cancel_stale_offers(builder):
    cancellation_count = 0
    try:
        offers_page = server.offers().for_account(public_key).call()
        open_offers = offers_page.get("_embedded", {}).get("records", [])
        current_time = time.time()

        for offer in open_offers:
            offer_id = int(offer["id"])
            created_at = time.mktime(time.strptime(offer["last_modified_time"], "%Y-%m-%dT%H:%M:%SZ"))
            age_hours = (current_time - created_at) / 3600.0

            if age_hours >= MAX_OFFER_AGE_HOURS:
                print(f"Cleaning up stale Offer ID {offer_id} ({age_hours:.1f} hrs old)")
                builder.append_manage_sell_offer_op(
                    selling=parse_asset(offer["selling"]),
                    buying=parse_asset(offer["buying"]),
                    amount="0",  # Amount=0 cancels open offer on-chain
                    price=offer["price"],
                    offer_id=offer_id
                )
                cancellation_count += 1
    except Exception as e:
        print(f"Notice: Offer check skipped ({e})")
        
    return cancellation_count

# --- MAIN ENGINE ---
def run_accumulator_bot():
    price = get_mid_price()
    if not price:
        print("Market data unavailable. Aborting cycle.")
        return

    state = load_state()
    
    # Load sequence for transaction building
    account = server.load_account(public_key)
    
    # Fetch account details for balances
    account_details = server.accounts().account_id(public_key).call()

    builder = TransactionBuilder(
        source_account=account,
        network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
        base_fee=100
    )

    action_taken = False

    # 1. Clean up stale orders
    if cancel_stale_offers(builder) > 0:
        action_taken = True

    # 2. Check available USDC balance
    usdc_balance = 0.0
    for b in account_details.get("balances", []):
        if b.get("asset_code") == "USDC":
            usdc_balance = float(b["balance"])

    # 3. Process Target Hits (+2% -> Reclaim principal USDC, retain XLM profit)
    remaining_positions = []
    for pos in state["open_positions"]:
        target_sell_price = pos["buy_price"] * PROFIT_MARGIN

        if price >= target_sell_price:
            initial_usdc_cost = pos["cost_usdc"]
            xlm_to_sell = round(initial_usdc_cost / price, 2)
            xlm_retained = round(pos["xlm_amount"] - xlm_to_sell, 2)

            print(f"Target Hit! Selling {xlm_to_sell} XLM @ ${price:.4f} to reclaim ${initial_usdc_cost:.2f} USDC.")
            print(f"Retained XLM Profit: {xlm_retained} XLM")

            builder.append_manage_sell_offer_op(
                selling=XLM,
                buying=USDC,
                amount=str(xlm_to_sell),
                price=str(round(price, 4)),
                offer_id=0
            )
            
            state["total_xlm_accumulated"] += max(0, xlm_retained)
            action_taken = True
        else:
            remaining_positions.append(pos)

    state["open_positions"] = remaining_positions

    # 4. Calculate Dynamic Trade Size based on available USDC
    calculated_chunk = round(usdc_balance / NUM_TIERS, 2)
    trade_size_usdc = max(calculated_chunk, MIN_TRADE_USDC)

    # 5. Open New Buy Tier if balance permits
    if usdc_balance >= trade_size_usdc:
        xlm_to_buy = round(trade_size_usdc / price, 2)
        print(f"Opening Buy Tier: Purchasing {xlm_to_buy} XLM @ ${price:.4f} (${trade_size_usdc:.2f} USDC)")

        builder.append_manage_buy_offer_op(
            selling=USDC,
            buying=XLM,
            buy_amount=str(xlm_to_buy),
            price=str(round(1 / price, 6)),
            offer_id=0
        )

        state["open_positions"].append({
            "buy_price": price,
            "xlm_amount": xlm_to_buy,
            "cost_usdc": trade_size_usdc
        })
        action_taken = True

    # 6. Single Batch Submit
    if action_taken:
        tx = builder.set_timeout(30).build()
        tx.sign(kp)
        res = server.submit_transaction(tx)
        save_state(state)
        print("Transaction submitted successfully.")
    else:
        print("No actions required this cycle.")

if __name__ == "__main__":
    run_accumulator_bot()
