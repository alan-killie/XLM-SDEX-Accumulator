import os
import time
import json
from stellar_sdk import Server, Keypair, TransactionBuilder, Network, Asset

# ---------------------------------------------------------
# Configuration & Strategy Parameters
# ---------------------------------------------------------
SECRET_KEY = os.getenv("STELLAR_SECRET_KEY")
if not SECRET_KEY:
    raise ValueError("STELLAR_SECRET_KEY environment variable is missing.")

kp = Keypair.from_secret(SECRET_KEY)
public_key = kp.public_key

server = Server("https://horizon.stellar.org")
XLM = Asset.native()
USDC_ISSUER = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"
USDC = Asset("USDC", USDC_ISSUER)

STATE_FILE = "grid_state.json"

TOTAL_CAPITAL_USDC = 9.0   # Total USDC working capital
NUM_TIERS = 10               # 10 tranches ($10 USDC per trade)
PROFIT_MARGIN = 1.020        # +2.0% profit target
DIP_THRESHOLD = 0.990        # -1.0% buy trigger below mid-price
REPOSITION_DRIFT = 0.005     # Reposition buy offer if mid-price drifts >0.5%
MIN_SELL_USDC = 1.00         # Dust threshold: minimum $1.00 fill before posting sell offer


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                pass
    return {
        "open_positions": [],
        "last_trade_cursor": None,
        "total_xlm_accumulated": 0.0,
        "last_market_price": None,
    }


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_mid_price():
    try:
        orderbook = server.orderbook(selling=XLM, buying=USDC).call()
        bids, asks = orderbook.get("bids", []), orderbook.get("asks", [])
        if not bids or not asks:
            return None
        return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
    except Exception as e:
        print(f"Error fetching mid price: {e}")
        return None


def is_circle_usdc(asset_type, code, issuer):
    return asset_type != "native" and code == "USDC" and issuer == USDC_ISSUER


def reconcile_executed_trades(builder, state):
    """
    Scans new fills using Horizon cursor. Processes partial fills and stages 
    passive sell offers (max 2 per batch to prevent op_underfunded).
    """
    try:
        # Initialize cursor on first run to prevent replaying past history
        if not state.get("last_trade_cursor"):
            latest = server.trades().for_account(public_key).order(desc=True).limit(1).call()
            records = latest.get("_embedded", {}).get("records", [])
            if records:
                state["last_trade_cursor"] = records[0]["paging_token"]
                print(f"Initialized trade cursor to latest fill ({state['last_trade_cursor']}).")
            return

        trades_call = (
            server.trades()
            .for_account(public_key)
            .order(desc=False)
            .cursor(state["last_trade_cursor"])
            .limit(50)
        )

        records = trades_call.call().get("_embedded", {}).get("records", [])
        staged_sell_count = 0

        for trade in records:
            state["last_trade_cursor"] = trade["paging_token"]
            
            base_is_xlm = (trade.get("base_asset_type") == "native")
            counter_is_xlm = (trade.get("counter_asset_type") == "native")
            base_is_usdc = is_circle_usdc(
                trade.get("base_asset_type"), trade.get("base_asset_code"), trade.get("base_asset_issuer")
            )
            counter_is_usdc = is_circle_usdc(
                trade.get("counter_asset_type"), trade.get("counter_asset_code"), trade.get("counter_asset_issuer")
            )

            if not ((base_is_xlm and counter_is_usdc) or (base_is_usdc and counter_is_xlm)):
                continue

            is_base = (trade.get("base_account") == public_key)
            is_counter = (trade.get("counter_account") == public_key)
            base_is_seller = trade.get("base_is_seller", False)

            bought_via_base = base_is_xlm and ((is_base and not base_is_seller) or (is_counter and base_is_seller))
            bought_via_counter = counter_is_xlm and ((is_base and base_is_seller) or (is_counter and not base_is_seller))

            if bought_via_base or bought_via_counter:
                if base_is_xlm:
                    xlm_bought = float(trade.get("base_amount", 0))
                    usdc_paid = float(trade.get("counter_amount", 0))
                else:
                    xlm_bought = float(trade.get("counter_amount", 0))
                    usdc_paid = float(trade.get("base_amount", 0))

                if xlm_bought > 0:
                    buy_price = usdc_paid / xlm_bought
                    target_sell_price = round(buy_price * PROFIT_MARGIN, 6)

                    # Calculate principal recovery vs retained XLM profit
                    xlm_to_sell = round(usdc_paid / target_sell_price, 7)
                    xlm_retained = round(xlm_bought - xlm_to_sell, 7)

                    if xlm_retained > 0:
                        state["total_xlm_accumulated"] += xlm_retained

                    # Stage sell offer if meets threshold and under batch limit (max 2 per run)
                    if usdc_paid >= MIN_SELL_USDC:
                        if staged_sell_count < 2:
                            builder.append_manage_sell_offer_op(
                                selling=XLM,
                                buying=USDC,
                                amount=f"{xlm_to_sell:.7f}",
                                price=f"{target_sell_price:.6f}",
                                offer_id=0,
                            )
                            staged_sell_count += 1
                            print(f"SELL OFFER STAGED: {xlm_to_sell:.4f} XLM @ ${target_sell_price:.4f}")
                        else:
                            print(f"BATCH LIMIT REACHED: Deferred sell offer for trade {trade['id']} to next cycle.")
                    else:
                        print(f"DUST FILL LOGGED (${usdc_paid:.2f}): Postponed sell offer placement.")

                    state["open_positions"].append({
                        "trade_id": str(trade["id"]),
                        "buy_price": round(buy_price, 6),
                        "cost_usdc": round(usdc_paid, 6),
                        "xlm_to_sell": xlm_to_sell,
                        "xlm_retained": xlm_retained,
                        "target_sell_price": target_sell_price,
                        "created_at": trade.get("ledger_close_time")
                    })

    except Exception as e:
        print(f"Notice: Trade reconciliation check failed ({e})")


def manage_trailing_buy_offer(builder, current_price, state, usdc_balance):
    """
    Cancels lingering/partially filled buy orders and places a fresh trailing bid 
    in a single atomic transaction.
    """
    target_buy_price = round(current_price * DIP_THRESHOLD, 6)
    tranche_size_usdc = TOTAL_CAPITAL_USDC / NUM_TIERS

    open_offers = server.offers().for_account(public_key).limit(50).call().get("_embedded", {}).get("records", [])
    active_buy_offer = None

    for offer in open_offers:
        if offer.get("selling_asset_type") != "native":  # Selling USDC = Buying XLM
            active_buy_offer = offer
            break

    if active_buy_offer:
        offer_id = int(active_buy_offer["id"])
        raw_price = float(active_buy_offer["price"])
        existing_buy_price = 1.0 / raw_price if raw_price > 0 else 0.0

        drift = abs(current_price - (existing_buy_price / DIP_THRESHOLD)) / current_price

        # If price drifted or order was partially filled, cancel remainder and reset
        if drift >= REPOSITION_DRIFT:
            print(f"Trailing Buy: Clearing Offer ID {offer_id} and resetting bid to ${target_buy_price:.4f}")
            
            # Op 1: Cancel unfilled remainder (unlocks reserved USDC instantly in tx)
            builder.append_manage_buy_offer_op(
                selling=USDC, buying=XLM, amount="0", price=active_buy_offer["price"], offer_id=offer_id
            )
            
            # Op 2: Place fresh full-tranche buy order
            xlm_to_buy = tranche_size_usdc / target_buy_price
            builder.append_manage_buy_offer_op(
                selling=USDC,
                buying=XLM,
                amount=f"{xlm_to_buy:.7f}",
                price=f"{target_buy_price:.6f}",
                offer_id=0,
            )
            return True
    else:
        # Place new trailing bid if no active buy order exists
        if usdc_balance >= tranche_size_usdc and len(state["open_positions"]) < NUM_TIERS:
            xlm_to_buy = tranche_size_usdc / target_buy_price
            print(f"Trailing Buy: Placing new bid for {xlm_to_buy:.4f} XLM @ ${target_buy_price:.4f}")
            builder.append_manage_buy_offer_op(
                selling=USDC,
                buying=XLM,
                amount=f"{xlm_to_buy:.7f}",
                price=f"{target_buy_price:.6f}",
                offer_id=0,
            )
            return True

    return False


def run_accumulator_bot():
    price = get_mid_price()
    if not price:
        print("Market data unavailable. Aborting cycle.")
        return

    state = load_state()
    state["last_market_price"] = round(price, 6)

    account = server.load_account(public_key)
    account_details = server.accounts().account_id(public_key).call()

    builder = TransactionBuilder(
        source_account=account,
        network_passphrase=Network.PUBLIC_NETWORK_PASSPHRASE,
        base_fee=100,
    )

    action_taken = False

    # 1. Reconcile executed fills
    reconcile_executed_trades(builder, state)
    if len(builder.operations) > 0:
        action_taken = True

    # 2. Get available USDC balance
    usdc_balance = 0.0
    for b in account_details.get("balances", []):
        if b.get("asset_code") == "USDC" and b.get("asset_issuer") == USDC_ISSUER:
            usdc_balance = float(b["balance"])

    # 3. Manage trailing buy offer & atomic resets
    if manage_trailing_buy_offer(builder, price, state, usdc_balance):
        action_taken = True

    # 4. Submit multi-operation transaction envelope
    if action_taken:
        try:
            tx = builder.set_timeout(180).build()
            tx.sign(kp)
            server.submit_transaction(tx)
            save_state(state)
            print("Transaction submitted and state persisted.")
        except Exception as e:
            print(f"Transaction submission failed: {e}")
    else:
        save_state(state)
        print("No order adjustments required this cycle.")


if __name__ == "__main__":
    run_accumulator_bot()
