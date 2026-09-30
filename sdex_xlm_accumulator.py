import os
import json
from stellar_sdk import Server, Keypair, TransactionBuilder, Network, Asset
from stellar_sdk.exceptions import BadRequestError

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
PROFIT_MARGIN = 1.010       # +1.0% profit target
DIP_THRESHOLD = 0.995       # -0.5% buy trigger below mid-price
REPOSITION_DRIFT = 0.0025   # Reposition if mid-price drifts >0.25%
MIN_SELL_USDC = 0.50        # Minimum $0.50 fill before staging sell offer


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


def find_matching_position(open_positions, trade):
    if not open_positions:
        return None, None

    base_is_xlm = (trade.get("base_asset_type") == "native")
    if base_is_xlm:
        xlm_amt = float(trade.get("base_amount", 0))
        usdc_amt = float(trade.get("counter_amount", 0))
    else:
        xlm_amt = float(trade.get("counter_amount", 0))
        usdc_amt = float(trade.get("base_amount", 0))

    trade_price = usdc_amt / xlm_amt if xlm_amt > 0 else 0.0
    base_offer = str(trade.get("base_offer_id", ""))
    counter_offer = str(trade.get("counter_offer_id", ""))

    for idx, pos in enumerate(open_positions):
        sell_id = str(pos.get("sell_offer_id", ""))
        if sell_id and (sell_id == base_offer or sell_id == counter_offer):
            return idx, "offer_id"

    if trade_price > 0:
        for idx, pos in enumerate(open_positions):
            target = pos.get("target_sell_price", 0.0)
            if target > 0 and abs(target - trade_price) / target < 0.001:
                return idx, "price"

    return None, None


def sync_and_stage_sell_offers(builder, state, pub_key, srv, liquid_xlm):
    if not state.get("open_positions"):
        return

    try:
        open_offers = (
            srv.offers()
            .for_account(pub_key)
            .limit(50)
            .call()
            .get("_embedded", {})
            .get("records", [])
        )

        active_sells = []
        for offer in open_offers:
            selling_is_xlm = offer.get("selling", {}).get("asset_type") == "native"
            buying_is_usdc = (
                offer.get("buying", {}).get("asset_code") == "USDC"
                and offer.get("buying", {}).get("asset_issuer") == USDC_ISSUER
            )
            if selling_is_xlm and buying_is_usdc:
                active_sells.append(
                    {
                        "offer_id": str(offer["id"]),
                        "price": float(offer["price"]),
                        "used": False,
                    }
                )

        remaining_positions = []
        for pos in state["open_positions"]:
            pos_offer_id = str(pos.get("sell_offer_id")) if pos.get("sell_offer_id") else None
            if pos_offer_id:
                match = next((o for o in active_sells if o["offer_id"] == pos_offer_id), None)
                if match:
                    match["used"] = True
                    remaining_positions.append(pos)
                else:
                    gain = pos.get("pending_xlm_gain", 0.0)
                    state["total_xlm_accumulated"] = round(
                        state.get("total_xlm_accumulated", 0.0) + gain, 7
                    )
                    print(
                        f"RECONCILED FILLED OFFER: Offer {pos_offer_id} (Trade {pos['trade_id']}) "
                        f"filled on-chain. Realized +{gain:.7f} XLM gain."
                    )
            else:
                remaining_positions.append(pos)

        state["open_positions"] = remaining_positions

        for offer in active_sells:
            if offer["used"]:
                continue
            matching_positions = [
                p for p in state["open_positions"]
                if not p.get("sell_offer_id") and abs(p.get("target_sell_price", 0.0) - offer["price"]) / offer["price"] < 0.001
            ]
            if matching_positions:
                for p in matching_positions:
                    p["sell_offer_id"] = offer["offer_id"]
                    print(f"Mapped Sell Offer ID {offer['offer_id']} to target ${p['target_sell_price']:.6f}")
                offer["used"] = True

        unmapped_groups = {}
        for pos in state["open_positions"]:
            if not pos.get("sell_offer_id"):
                target_key = round(pos["target_sell_price"], 6)
                unmapped_groups.setdefault(target_key, []).append(pos)

        staged_count = 0
        available_xlm_to_sell = liquid_xlm

        for target_price, positions in unmapped_groups.items():
            if staged_count >= 2 or available_xlm_to_sell <= 0.0001:
                break

            total_cost = sum(p.get("cost_usdc", 0.0) for p in positions)
            requested_xlm = round(sum(p["xlm_to_sell"] for p in positions), 7)

            if requested_xlm <= 0.0001:
                continue

            if total_cost < MIN_SELL_USDC and len(state["open_positions"]) > len(positions):
                continue

            total_xlm_to_sell = min(requested_xlm, available_xlm_to_sell)

            builder.append_manage_sell_offer_op(
                selling=XLM,
                buying=USDC,
                amount=f"{total_xlm_to_sell:.7f}",
                price=f"{target_price:.6f}",
                offer_id=0,
            )
            available_xlm_to_sell -= total_xlm_to_sell
            staged_count += 1
            print(
                f"CONSOLIDATED SELL STAGED: {total_xlm_to_sell:.4f} XLM @ ${target_price:.6f} "
                f"for {len(positions)} position(s)"
            )

    except Exception as e:
        print(f"Notice: Failed to sync/stage sell offer IDs ({e})")


def reconcile_executed_trades(builder, state):
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
            sold_via_counter = counter_is_xlm and ((is_base and not base_is_seller) or (is_counter and base_is_seller))

            if bought_via_base or bought_via_counter:
                trade_id_str = str(trade["id"])
                if any(p.get("trade_id") == trade_id_str for p in state["open_positions"]):
                    continue

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

                    state["open_positions"].append({
                        "trade_id": trade_id_str,
                        "buy_price": round(buy_price, 6),
                        "cost_usdc": round(usdc_paid, 6),
                        "xlm_to_sell": xlm_to_sell,
                        "pending_xlm_gain": pending_xlm_gain,
                        "target_sell_price": target_sell_price,
                        "created_at": trade.get("ledger_close_time"),
                        "sell_offer_id": None
                    })

            elif sold_via_base or sold_via_counter:
                if base_is_xlm:
                    xlm_sold = float(trade.get("base_amount", 0))
                else:
                    xlm_sold = float(trade.get("counter_amount", 0))

                while xlm_sold > 0.0001 and state["open_positions"]:
                    idx, match_type = find_matching_position(state["open_positions"], trade)
                    if idx is None:
                        break

                    pos = state["open_positions"][idx]
                    needed = pos.get("xlm_to_sell", 0.0)

                    if needed <= 0.0001:
                        state["open_positions"].pop(idx)
                        continue

                    portion = min(xlm_sold, needed)
                    fill_ratio = portion / needed if needed > 0 else 1.0
                    realized_gain = pos.get("pending_xlm_gain", 0.0) * fill_ratio

                    state["total_xlm_accumulated"] = round(
                        state.get("total_xlm_accumulated", 0.0) + realized_gain, 7
                    )

                    pos["xlm_to_sell"] = round(max(0.0, pos["xlm_to_sell"] - portion), 7)
                    pos["pending_xlm_gain"] = round(
                        max(0.0, pos.get("pending_xlm_gain", 0.0) - realized_gain), 7
                    )
                    xlm_sold = round(xlm_sold - portion, 7)

                    print(
                        f"SELL FILL MATCHED ({match_type}): Applied {portion:.4f} XLM to trade {pos['trade_id']}. "
                        f"Realized +{realized_gain:.7f} XLM gain."
                    )

                    if pos["xlm_to_sell"] <= 0.0001:
                        state["open_positions"].pop(idx)
                        print(f"POSITION FULLY CLOSED: Removed {pos['trade_id']} from grid state.")

    except Exception as e:
        print(f"Notice: Trade reconciliation check failed ({e})")


def manage_trailing_buy_offer(builder, current_price, state, liquid_usdc):
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
            usdc_locked_in_offer = float(active_buy_offer["amount"])
            effective_usdc = liquid_usdc + usdc_locked_in_offer

            if effective_usdc >= tranche_size_usdc:
                print(f"Trailing Buy: Clearing Buy Offer ID {offer_id} and resetting bid to ${target_buy_price:.4f}")
                
                builder.append_manage_buy_offer_op(
                    selling=USDC, buying=XLM, amount="0", price=active_buy_offer["price_r"], offer_id=offer_id
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

    reconcile_executed_trades(builder, state)

    liquid_usdc = 0.0
    native_balance = 0.0
    native_liabilities = 0.0

    for b in account_details.get("balances", []):
        if b.get("asset_code") == "USDC" and b.get("asset_issuer") == USDC_ISSUER:
            total = float(b["balance"])
            liabilities = float(b.get("selling_liabilities", 0.0))
            liquid_usdc = max(0.0, total - liabilities)
        elif b.get("asset_type") == "native":
            native_balance = float(b["balance"])
            native_liabilities = float(b.get("selling_liabilities", 0.0))

    subentry_count = account_details.get("subentry_count", 0)
    base_reserve = (2 + subentry_count) * 0.5
    liquid_xlm = max(0.0, native_balance - native_liabilities - base_reserve)

    sync_and_stage_sell_offers(builder, state, public_key, server, liquid_xlm)
    if len(builder.operations) > 0:
        action_taken = True

    if manage_trailing_buy_offer(builder, price, state, liquid_usdc):
        action_taken = True

    if action_taken:
        try:
            tx = builder.set_timeout(180).build()
            tx.sign(kp)
            server.submit_transaction(tx)
            save_state(state)
            print("Transaction submitted and state persisted.")
        except BadRequestError as e:
            result_codes = e.response.json().get("extras", {}).get("result_codes", {})
            print(f"Transaction submission failed with codes: {result_codes}")
        except Exception as e:
            print(f"Transaction submission failed: {e}")
    else:
        save_state(state)
        print("No order adjustments required this cycle.")


if __name__ == "__main__":
    run_accumulator_bot()
