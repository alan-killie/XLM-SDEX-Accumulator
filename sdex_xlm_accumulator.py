import os
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

TOTAL_CAPITAL_USDC = 10.0   # Total USDC working capital
NUM_TIERS = 10              # 10 tranches ($1.00 USDC per trade)
PROFIT_MARGIN = 1.020       # +2.0% profit target
DIP_THRESHOLD = 0.995       # -0.5% buy trigger below mid-price
REPOSITION_DRIFT = 0.005    # Reposition buy offer if mid-price drifts >0.5%
MIN_SELL_USDC = 0.50        # Minimum $0.50 fill before posting sell offer


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
    Scans new fills using Horizon cursor.
    - Buy fills: Stages passive sell offers and tracks open positions with pending gains.
    - Sell fills: Realizes XLM gain into total_xlm_accumulated once USDC principal is recovered.
    """
    try:
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

            sold_via_base = base_is_xlm and ((is_base and base_is_seller) or (is_counter and not base_is_seller))
            sold_via_counter = counter_is_xlm and ((is_base and not base_is_seller) or (is_counter and *is_base if false else not base_is_seller))

            # --------------------------------------------------
            # 1. HANDLE BUY FILL (Stage Sell Offer + Track Pending Position)
            # --------------------------------------------------
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

                    xlm_to_sell = round(usdc_paid / target_sell_price, 7)
                    pending_xlm_gain = round(xlm_bought - xlm_to_sell, 7)

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

                    state["open_positions"].append({
                        "trade_id": str(trade["id"]),
                        "buy_price": round(buy_price, 6),
                        "cost_usdc": round(usdc_paid, 6),
                        "xlm_to_sell": xlm_to_sell,
                        "pending_xlm_gain": pending_xlm_gain,
                        "target_sell_price": target_sell_price,
                        "created_at": trade.get("ledger_close_time")
                    })

            # --------------------------------------------------
            # 2. HANDLE SELL FILL (Realize Profit + Close Position)
            # --------------------------------------------------
            elif sold_via_base or sold_via_counter:
                if state["open_positions"]:
                    closed_pos = state["open_positions"].pop(0)
                    realized_gain = closed_pos.get("pending_xlm_gain", 0.0)
                    
                    state["total_xlm_accumulated"] = round(
                        state["total_xlm_accumulated"] + realized_gain, 7
                    )
                    print(
                        f"POSITION CLOSED: Recovered ${closed_pos['cost_usdc']:.2f} USDC. "
                        f"Realized +{realized_gain:.7f} XLM gain."
                    )

    except Exception as e:
        print(f"Notice: Trade reconciliation check failed ({e})")


def manage_trailing_buy_offer(builder, current_price, state, liquid_usdc):
    """
    Cancels lingering buy orders and places a fresh trailing bid
    in a single atomic transaction.
    """
    target_buy_price = round(current_price * DIP_THRESHOLD, 6)
    tranche_size_usdc = TOTAL_CAPITAL_USDC / NUM_TIERS

    open_offers = server.offers().for_account(public_key).limit(50).call().get("_embedded", {}).get("records", [])
    active_buy_offer = None

    for offer in open_offers:
        selling_asset = offer.get("selling", {})
        buying_asset = offer.get("buying", {})

        selling_is_usdc = (
            selling_asset.get("asset_code") == "USDC" 
            and selling_asset.get("asset_issuer") == USDC_ISSUER
        )
        buying_is_xlm = (buying_asset.get("asset_type") == "native")

        if selling_is_usdc and buying_is_xlm:
            active_buy_offer = offer
            break

    if active_buy_offer:
        offer_id = int(active_buy_offer["id"])
        xlm_per_usdc = float(active_buy_offer["price"])
        existing_buy_price = 1.0 / xlm_per_usdc if xlm_per_usdc > 0 else 0.0

        drift = abs(current_price - (existing_buy_price / DIP_THRESHOLD)) / current_price

        if drift >= REPOSITION_DRIFT:
            xlm_remaining = float(active_buy_offer["amount"])
            usdc_locked_in_offer = xlm_remaining * existing_buy_price
            effective_usdc = liquid_usdc + usdc_locked_in_offer

            if effective_usdc >= tranche_size_usdc:
                print(f"Trailing Buy: Clearing Buy Offer ID {offer_id} and resetting bid to ${target_buy_price:.4f}")
                
                builder.append_manage_buy_offer_op(
                    selling=USDC, buying=XLM, amount="0", price=active_buy_offer["price"], offer_id=offer_id
                )
                
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
                print(f"Insufficient USDC (${effective_usdc:.2f}) to reposition buy offer.")
    else:
        if liquid_usdc >= tranche_size_usdc and len(state["open_positions"]) < NUM_TIERS:
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
        else:
            print(f"Skipping buy placement: ${liquid_usdc:.2f} liquid USDC available.")

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

    # 1. Reconcile fills (stage sell orders / realize gains)
    reconcile_executed_trades(builder, state)
    if len(builder.operations) > 0:
        action_taken = True

    # 2. Calculate liquid USDC balance
    liquid_usdc = 0.0
    for b in account_details.get("balances", []):
        if b.get("asset_code") == "USDC" and b.get("asset_issuer") == USDC_ISSUER:
            total = float(b["balance"])
            liabilities = float(b.get("selling_liabilities", 0.0))
            liquid_usdc = max(0.0, total - liabilities)

    # 3. Manage trailing buy offer
    if manage_trailing_buy_offer(builder, price, state, liquid_usdc):
        action_taken = True

    # 4. Single atomic submit
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
