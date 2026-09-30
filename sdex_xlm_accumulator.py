import json
import os
from stellar_sdk import Asset, Keypair, Network, Server, TransactionBuilder
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

TOTAL_CAPITAL_USDC = 10.0  # Total USDC working capital
NUM_TIERS = 10  # 10 tranches ($1.00 USDC per trade)
PROFIT_MARGIN = 1.010  # +1.0% profit target
DIP_THRESHOLD = 0.995  # -0.5% buy trigger below mid-price
REPOSITION_DRIFT = 0.0025  # Reposition if mid-price drifts >0.25%
MIN_SELL_USDC = 0.50  # Minimum $0.50 fill before staging sell offer


def merge_into_position(target, pos_to_add):
    """Merges pos_to_add into target using a cost-weighted target sell price."""
    total_cost = target["cost_usdc"] + pos_to_add["cost_usdc"]
    if total_cost <= 0:
        return

    # Cost-weighted average target price
    weighted_target = (
        (target["cost_usdc"] * target["target_sell_price"])
        + (pos_to_add["cost_usdc"] * pos_to_add["target_sell_price"])
    ) / total_cost

    target["cost_usdc"] = round(total_cost, 6)
    target["target_sell_price"] = round(weighted_target, 6)
    target["xlm_to_sell"] = round(
        target["xlm_to_sell"] + pos_to_add["xlm_to_sell"], 7
    )
    target["pending_xlm_gain"] = round(
        target.get("pending_xlm_gain", 0.0)
        + pos_to_add.get("pending_xlm_gain", 0.0),
        7,
    )

    # Recalculate average effective buy price
    total_xlm = target["xlm_to_sell"] + target["pending_xlm_gain"]
    if total_xlm > 0:
        target["buy_price"] = round(target["cost_usdc"] / total_xlm, 6)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)

        positions = state.get("open_positions", [])
        cleaned = []
        for pos in positions:
            unmapped = [p for p in cleaned if not p.get("sell_offer_id")]
            if (
                pos.get("cost_usdc", 0) < 0.10
                and not pos.get("sell_offer_id")
                and unmapped
            ):
                merge_into_position(unmapped[-1], pos)
            else:
                cleaned.append(pos)

        state["open_positions"] = cleaned
        return state

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
    return (
        asset_type != "native" and code == "USDC" and issuer == USDC_ISSUER
    )


def find_matching_position(open_positions, trade):
    if not open_positions:
        return None, None

    base_is_xlm = trade.get("base_asset_type") == "native"
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
        active_sells = []
        cursor = None
        while True:
            call_builder = srv.offers().for_account(pub_key).limit(50)
            if cursor:
                call_builder.cursor(cursor)

            res = call_builder.call().get("_embedded", {}).get("records", [])
            if not res:
                break

            for offer in res:
                selling_is_xlm = (
                    offer.get("selling", {}).get("asset_type") == "native"
                )
                buying_is_usdc = (
                    offer.get("buying", {}).get("asset_code") == "USDC"
                    and offer.get("buying", {}).get("asset_issuer")
                    == USDC_ISSUER
                )
                if selling_is_xlm and buying_is_usdc:
                    active_sells.append(
                        {
                            "offer_id": str(offer["id"]),
                            "price": float(offer["price"]),
                            "amount": float(offer.get("amount", 0.0)),
                            "used": False,
                        }
                    )
            cursor = str(res[-1]["id"])

        remaining_positions = []
        for pos in state["open_positions"]:
            pos_offer_id = (
                str(pos.get("sell_offer_id"))
                if pos.get("sell_offer_id")
                else None
            )
            if pos_offer_id:
                match = next(
                    (
                        o
                        for o in active_sells
                        if o["offer_id"] == pos_offer_id
                    ),
                    None,
                )
                if match:
                    if abs(pos["xlm_to_sell"] - match["amount"]) < 0.01:
                        match["used"] = True
                        remaining_positions.append(pos)
                    else:
                        pos["sell_offer_id"] = None
                        remaining_positions.append(pos)
                else:
                    pos["sell_offer_id"] = None
                    remaining_positions.append(pos)
            else:
                remaining_positions.append(pos)

        state["open_positions"] = remaining_positions

        # Map active unassigned offers to matching state positions
        for offer in active_sells:
            if offer["used"]:
                continue
            matching_pos = next(
                (
                    p
                    for p in state["open_positions"]
                    if not p.get("sell_offer_id")
                    and abs(p.get("target_sell_price", 0.0) - offer["price"]) / offer["price"] < 0.0005
                    and abs(p.get("xlm_to_sell", 0.0) - offer["amount"]) < 0.01
                ),
                None,
            )
            if matching_pos:
                matching_pos["sell_offer_id"] = offer["offer_id"]
                offer["used"] = True
                print(
                    f"Mapped Sell Offer ID {offer['offer_id']} to target"
                    f" ${matching_pos['target_sell_price']:.6f}"
                )

        # Cancel orphaned on-chain offers to release liquid XLM liabilities
        for offer in active_sells:
            if not offer["used"]:
                builder.append_manage_sell_offer_op(
                    selling=XLM,
                    buying=USDC,
                    amount="0",
                    price=f"{offer['price']:.6f}",
                    offer_id=int(offer["offer_id"]),
                )
                print(
                    f"CANCELLED ORPHANED SELL OFFER ID {offer['offer_id']} to release liquid XLM"
                )

        unmapped_groups = {}
        for pos in state["open_positions"]:
            if not pos.get("sell_offer_id"):
                target_key = round(pos["target_sell_price"], 6)
                unmapped_groups.setdefault(target_key, []).append(pos)

        staged_count = 0
        available_xlm_to_sell = liquid_xlm

        for target_price, positions in unmapped_groups.items():
            if staged_count >= 2 or available_xlm_to_sell <= 0.55:
                break

            total_cost = sum(p.get("cost_usdc", 0.0) for p in positions)
            requested_xlm = round(
                sum(p["xlm_to_sell"] for p in positions), 7
            )

            if requested_xlm <= 0.0001:
                continue

            if total_cost < MIN_SELL_USDC and len(
                state["open_positions"]
            ) > len(positions):
                continue

            max_sellable = max(0.0, available_xlm_to_sell - 0.55)
            total_xlm_to_sell = min(requested_xlm, max_sellable)

            if (total_xlm_to_sell * target_price) < MIN_SELL_USDC:
                continue

            builder.append_manage_sell_offer_op(
                selling=XLM,
                buying=USDC,
                amount=f"{total_xlm_to_sell:.7f}",
                price=f"{target_price:.6f}",
                offer_id=0,
            )
            available_xlm_to_sell -= total_xlm_to_sell + 0.50
            staged_count += 1
            print(
                f"CONSOLIDATED SELL STAGED: {total_xlm_to_sell:.4f} XLM @"
                f" ${target_price:.6f} for {len(positions)} position(s)"
            )

    except Exception as e:
        print(f"Notice: Failed to sync/stage sell offer IDs ({e})")


def reconcile_executed_trades(builder, state):
    try:
        if not state.get("last_trade_cursor"):
            latest = (
                server.trades()
                .for_account(public_key)
                .order(desc=True)
                .limit(1)
                .call()
            )
            records = latest.get("_embedded", {}).get("records", [])
            if records:
                state["last_trade_cursor"] = records[0]["paging_token"]
                print(
                    "Initialized trade cursor to latest fill"
                    f" ({state['last_trade_cursor']})."
                )
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

            base_is_xlm = trade.get("base_asset_type") == "native"
            counter_is_xlm = trade.get("counter_asset_type") == "native"
            base_is_usdc = is_circle_usdc(
                trade.get("base_asset_type"),
                trade.get("base_asset_code"),
                trade.get("base_asset_issuer"),
            )
            counter_is_usdc = is_circle_usdc(
                trade.get("counter_asset_type"),
                trade.get("counter_asset_code"),
                trade.get("counter_asset_issuer"),
            )

            if not (
                (base_is_xlm and counter_is_usdc)
                or (base_is_usdc and counter_is_xlm)
            ):
                continue

            is_base = trade.get("base_account") == public_key
            is_counter = trade.get("counter_account") == public_key
            base_is_seller = trade.get("base_is_seller", False)

            bought_via_base = base_is_xlm and (
                (is_base and not base_is_seller)
                or (is_counter and base_is_seller)
            )
            bought_via_counter = counter_is_xlm and (
                (is_base and base_is_seller)
                or (is_counter and not base_is_seller)
            )

            sold_via_base = base_is_xlm and (
                (is_base and base_is_seller)
                or (is_counter and not base_is_seller)
            )
            sold_via_counter = counter_is_xlm and (
                (is_base and not base_is_seller)
                or (is_counter and base_is_seller)
            )

            if bought_via_base or bought_via_counter:
                trade_id_str = str(trade["id"])
                if any(
                    p.get("trade_id") == trade_id_str
                    for p in state["open_positions"]
                ):
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

                    unmapped_positions = [
                        p
                        for p in state["open_positions"]
                        if not p.get("sell_offer_id")
                    ]
                    if usdc_paid < 0.10 and unmapped_positions:
                        pos_to_add = {
                            "cost_usdc": usdc_paid,
                            "target_sell_price": target_sell_price,
                            "xlm_to_sell": xlm_to_sell,
                            "pending_xlm_gain": pending_xlm_gain,
                        }
                        merge_into_position(unmapped_positions[-1], pos_to_add)
                        print(
                            f"DUST MERGED: Added ${usdc_paid:.4f} fill to active"
                            f" position {unmapped_positions[-1]['trade_id']}"
                        )
                    else:
                        state["open_positions"].append(
                            {
                                "trade_id": trade_id_str,
                                "buy_price": round(buy_price, 6),
                                "cost_usdc": round(usdc_paid, 6),
                                "xlm_to_sell": xlm_to_sell,
                                "pending_xlm_gain": pending_xlm_gain,
                                "target_sell_price": target_sell_price,
                                "created_at": trade.get("ledger_close_time"),
                                "sell_offer_id": None,
                            }
                        )

            elif sold_via_base or sold_via_counter:
                if base_is_xlm:
                    xlm_sold = float(trade.get("base_amount", 0))
                else:
                    xlm_sold = float(trade.get("counter_amount", 0))

                while xlm_sold > 0.0001 and state["open_positions"]:
                    idx, match_type = find_matching_position(
                        state["open_positions"], trade
                    )
                    if idx is None:
                        break

                    pos = state["open_positions"][idx]
                    needed = pos.get("xlm_to_sell", 0.0)

                    if needed <= 0.0001:
                        state["open_positions"].pop(idx)
                        continue

                    portion = min(xlm_sold, needed)
                    fill_ratio = portion / needed if needed > 0 else 1.0
                    realized_gain = (
                        pos.get("pending_xlm_gain", 0.0) * fill_ratio
                    )

                    state["total_xlm_accumulated"] = round(
                        state.get("total_xlm_accumulated", 0.0) + realized_gain,
                        7,
                    )

                    pos["xlm_to_sell"] = round(
                        max(0.0, pos["xlm_to_sell"] - portion), 7
                    )
                    pos["pending_xlm_gain"] = round(
                        max(
                            0.0, pos.get("pending_xlm_gain", 0.0) - realized_gain
                        ),
                        7,
                    )
                    xlm_sold = round(xlm_sold - portion, 7)

                    print(
                        f"SELL FILL MATCHED ({match_type}): Applied"
                        f" {portion:.4f} XLM to trade {pos['trade_id']}. "
                        f"Realized +{realized_gain:.7f} XLM gain."
                    )

                    if pos["xlm_to_sell"] <= 0.0001:
                        state["open_positions"].pop(idx)
                        print(
                            f"POSITION FULLY CLOSED: Removed {pos['trade_id']}"
                            " from grid state."
                        )

    except Exception as e:
        print(f"Notice: Trade reconciliation check failed ({e})")


def manage_trailing_buy_offer(builder, current_price, state, liquid_usdc):
    target_buy_price = round(current_price * DIP_THRESHOLD, 6)
    tranche_size_usdc = TOTAL_CAPITAL_USDC / NUM_TIERS

    usable_usdc = max(0.0, liquid_usdc - 0.02)

    open_offers = (
        server.offers()
        .for_account(public_key)
        .limit(50)
        .call()
        .get("_embedded", {})
        .get("records", [])
    )
    active_buy_offer = None

    for offer in open_offers:
        selling_asset = offer.get("selling", {})
        buying_asset = offer.get("buying", {})

        selling_is_usdc = (
            selling_asset.get("asset_code") == "USDC"
            and selling_asset.get("asset_issuer") == USDC_ISSUER
        )
        buying_is_xlm = buying_asset.get("asset_type") == "native"

        if selling_is_usdc and buying_is_xlm:
            active_buy_offer = offer
            break

    if active_buy_offer:
        offer_id = int(active_buy_offer["id"])
        xlm_per_usdc = float(active_buy_offer["price"])
        existing_buy_price = 1.0 / xlm_per_usdc if xlm_per_usdc > 0 else 0.0

        drift = (
            abs(current_price - (existing_buy_price / DIP_THRESHOLD))
            / current_price
        )

        if drift >= REPOSITION_DRIFT:
            usdc_locked_in_offer = float(active_buy_offer["amount"])
            effective_usdc = usable_usdc + usdc_locked_in_offer

            if effective_usdc >= tranche_size_usdc:
                print(
                    f"Trailing Buy: Repositioning Offer ID {offer_id} to"
                    f" ${target_buy_price:.4f}"
                )
                xlm_to_buy = (tranche_size_usdc - 0.01) / target_buy_price

                builder.append_manage_buy_offer_op(
                    selling=USDC,
                    buying=XLM,
                    amount=f"{xlm_to_buy:.7f}",
                    price=f"{target_buy_price:.6f}",
                    offer_id=offer_id,
                )
                return True, False
            else:
                print(
                    f"Insufficient USDC (${effective_usdc:.2f}) to reposition"
                    " buy offer."
                )
    else:
        if (
            usable_usdc >= tranche_size_usdc
            and len(state["open_positions"]) < NUM_TIERS
        ):
            xlm_to_buy = (tranche_size_usdc - 0.01) / target_buy_price
            print(
                f"Trailing Buy: Placing new bid for {xlm_to_buy:.4f} XLM @"
                f" ${target_buy_price:.4f}"
            )
            builder.append_manage_buy_offer_op(
                selling=USDC,
                buying=XLM,
                amount=f"{xlm_to_buy:.7f}",
                price=f"{target_buy_price:.6f}",
                offer_id=0,
            )
            return True, True
        else:
            print(
                "Skipping buy placement:"
                f" ${usable_usdc:.2f} usable USDC available."
            )

    return False, False


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
        if (
            b.get("asset_code") == "USDC"
            and b.get("asset_issuer") == USDC_ISSUER
        ):
            total = float(b["balance"])
            liabilities = float(b.get("selling_liabilities", 0.0))
            liquid_usdc = max(0.0, total - liabilities)
        elif b.get("asset_type") == "native":
            native_balance = float(b["balance"])
            native_liabilities = float(b.get("selling_liabilities", 0.0))

    subentry_count = account_details.get("subentry_count", 0)
    base_reserve = ((2 + subentry_count) * 0.5) + 0.1
    liquid_xlm = max(0.0, native_balance - native_liabilities - base_reserve)

    buy_action, created_new_subentry = manage_trailing_buy_offer(
        builder, price, state, liquid_usdc
    )
    if buy_action:
        action_taken = True
        if created_new_subentry:
            liquid_xlm = max(0.0, liquid_xlm - 0.50)

    sync_and_stage_sell_offers(builder, state, public_key, server, liquid_xlm)
    if len(builder.operations) > (1 if buy_action else 0):
        action_taken = True

    if action_taken:
        try:
            tx = builder.set_timeout(180).build()
            tx.sign(kp)
            server.submit_transaction(tx)
            save_state(state)
            print("Transaction submitted and state persisted.")
        except BadRequestError as e:
            try:
                err_data = json.loads(e.text)
                result_codes = err_data.get("extras", {}).get(
                    "result_codes", {}
                )
                print(
                    f"Transaction submission failed with codes: {result_codes}"
                )
            except Exception:
                print(f"Transaction submission failed: {e}")
        except Exception as e:
            print(f"Transaction submission failed: {e}")
    else:
        save_state(state)
        print("No order adjustments required this cycle.")


if __name__ == "__main__":
    run_accumulator_bot()
