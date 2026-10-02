import datetime from datetime import timezone
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
PROFIT_MARGIN = 1.001  # +0.1% profit target
DIP_THRESHOLD = 0.998  # -0.2% buy trigger below mid-price
REPOSITION_DRIFT = 0.0020  # Reposition if mid-price drifts >0.20%
MIN_SELL_USDC = 0.50  # Minimum $0.50 fill before staging sell offer
SELL_AMOUNT_TOL = 0.01          # XLM tolerance when matching offer <-> positions
SELL_PRICE_TOL = 0.0005         # relative price tolerance when mapping offers
MAX_NEW_SELLS_PER_CYCLE = 2     # new consolidated offers staged per cycle
XLM_FLOOR = 0.55                # XLM kept back from staging
SUBENTRY_RESERVE = 0.50         # reserve consumed/released per offer


def merge_into_position(target, pos_to_add):
    """Merges pos_to_add into target using a cost-weighted target sell price."""
    total_cost = target["cost_usdc"] + pos_to_add["cost_usdc"]
    if total_cost <= 0:
        return

    # Total combined XLM held across both positions prior to recalculation
    total_xlm = (
        target.get("xlm_to_sell", 0.0)
        + target.get("pending_xlm_gain", 0.0)
        + pos_to_add.get("xlm_to_sell", 0.0)
        + pos_to_add.get("pending_xlm_gain", 0.0)
    )

    # Cost-weighted average target sell price
    weighted_target = (
        (target["cost_usdc"] * target["target_sell_price"])
        + (pos_to_add["cost_usdc"] * pos_to_add["target_sell_price"])
    ) / total_cost

    target["cost_usdc"] = round(total_cost, 6)
    target["target_sell_price"] = round(weighted_target, 6)

    # Recalculate xlm_to_sell to recover ONLY total_cost (0% USDC profit)
    target["xlm_to_sell"] = round(total_cost / target["target_sell_price"], 7)

    # All remaining XLM is skimmed strictly as accumulated XLM gain
    target["pending_xlm_gain"] = round(
        max(0.0, total_xlm - target["xlm_to_sell"]), 7
    )

    # Recalculate average effective buy price
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
        state.setdefault("yield_history", [])
        return state

    return {
        "open_positions": [],
        "last_trade_cursor": None,
        "total_xlm_accumulated": 0.0,
        "yield_history": [],
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
            if target > 0 and abs(target - trade_price) / target < 0.005:
                return idx, "price"

    return 0, "fifo_fallback"


def fetch_active_sell_offers(srv, pub_key):
    """Returns every open XLM->USDC sell offer for the account (paginated)."""
    active, cursor = [], None
    while True:
        call_builder = srv.offers().for_account(pub_key).limit(50).order(desc=False)
        if cursor:
            call_builder.cursor(cursor)

        records = call_builder.call().get("_embedded", {}).get("records", [])
        if not records:
            break

        for offer in records:
            selling_is_xlm = offer.get("selling", {}).get("asset_type") == "native"
            buying = offer.get("buying", {})
            buying_is_usdc = (
                buying.get("asset_code") == "USDC"
                and buying.get("asset_issuer") == USDC_ISSUER
            )
            if selling_is_xlm and buying_is_usdc:
                active.append(
                    {
                        "offer_id": str(offer["id"]),
                        "price": float(offer["price"]),
                        "amount": float(offer.get("amount", 0.0)),
                        "used": False,
                    }
                )

        cursor = str(records[-1].get("paging_token", records[-1]["id"]))
        if len(records) < 50:
            break
    return active


def group_unmapped_positions(positions):
    """Groups positions without a sell_offer_id by target price, keeping
    state order inside each group (staging and mapping both rely on it)."""
    groups = {}
    for pos in positions:
        if not pos.get("sell_offer_id"):
            groups.setdefault(round(pos["target_sell_price"], 6), []).append(pos)
    return groups


def sync_and_stage_sell_offers(builder, state, pub_key, srv, liquid_xlm):
    if not state.get("open_positions"):
        return

    try:
        active_sells = fetch_active_sell_offers(srv, pub_key)
    except Exception as e:
        print(f"Notice: Failed to fetch open sell offers ({e})")
        return

    positions = state["open_positions"]
    offers_by_id = {o["offer_id"]: o for o in active_sells}

    mapped_groups = {}
    for pos in positions:
        if pos.get("sell_offer_id"):
            mapped_groups.setdefault(str(pos["sell_offer_id"]), []).append(pos)

    for offer_id, group in mapped_groups.items():
        offer = offers_by_id.get(offer_id)
        group_xlm = sum(p.get("xlm_to_sell", 0.0) for p in group)
        if offer and abs(group_xlm - offer["amount"]) < SELL_AMOUNT_TOL:
            offer["used"] = True
        else:
            for pos in group:
                pos["sell_offer_id"] = None

    for offer in active_sells:
        if offer["used"] or offer["price"] <= 0:
            continue

        for key, group in group_unmapped_positions(positions).items():
            if abs(key - offer["price"]) / offer["price"] >= SELL_PRICE_TOL:
                continue

            running = 0.0
            for k, pos in enumerate(group, start=1):
                running += pos.get("xlm_to_sell", 0.0)
                if abs(running - offer["amount"]) < SELL_AMOUNT_TOL:
                    for p in group[:k]:
                        p["sell_offer_id"] = offer["offer_id"]
                    offer["used"] = True
                    print(
                        f"Mapped Sell Offer ID {offer['offer_id']} to"
                        f" {k} position(s) @ ${key:.6f}"
                    )
                    break
            if offer["used"]:
                break

    available_xlm_to_sell = liquid_xlm
    for offer in active_sells:
        if offer["used"]:
            continue
        builder.append_manage_sell_offer_op(
            selling=XLM,
            buying=USDC,
            amount="0",
            price=f"{offer['price']:.6f}",
            offer_id=int(offer["offer_id"]),
        )
        available_xlm_to_sell += offer["amount"] + SUBENTRY_RESERVE
        print(
            f"CANCELLED ORPHANED SELL OFFER ID {offer['offer_id']}"
            f" ({offer['amount']:.4f} XLM released)"
        )

    staged_count = 0
    for target_price, group in sorted(group_unmapped_positions(positions).items()):
        if (
            staged_count >= MAX_NEW_SELLS_PER_CYCLE
            or available_xlm_to_sell <= XLM_FLOOR
        ):
            break

        budget = max(0.0, available_xlm_to_sell - XLM_FLOOR)
        batch, batch_xlm, batch_cost = [], 0.0, 0.0
        for pos in group:
            pos_xlm = pos.get("xlm_to_sell", 0.0)
            if pos_xlm <= 0.0001:
                continue
            if batch_xlm + pos_xlm > budget:
                break
            batch.append(pos)
            batch_xlm += pos_xlm
            batch_cost += pos.get("cost_usdc", 0.0)

        if not batch:
            continue

        if batch_cost < MIN_SELL_USDC and len(positions) > len(batch):
            continue
        if batch_xlm * target_price < MIN_SELL_USDC:
            continue

        batch_xlm = round(batch_xlm, 7)
        builder.append_manage_sell_offer_op(
            selling=XLM,
            buying=USDC,
            amount=f"{batch_xlm:.7f}",
            price=f"{target_price:.6f}",
            offer_id=0,
        )
        available_xlm_to_sell -= batch_xlm + SUBENTRY_RESERVE
        staged_count += 1
        print(
            f"CONSOLIDATED SELL STAGED: {batch_xlm:.4f} XLM @"
            f" ${target_price:.6f} for {len(batch)} position(s)"
        )


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

                    tranche_size_usdc = TOTAL_CAPITAL_USDC / NUM_TIERS
                    if usdc_paid < (tranche_size_usdc * 0.95) and unmapped_positions:
                        pos_to_add = {
                            "cost_usdc": usdc_paid,
                            "target_sell_price": target_sell_price,
                            "xlm_to_sell": xlm_to_sell,
                            "pending_xlm_gain": pending_xlm_gain,
                        }
                        merge_into_position(unmapped_positions[-1], pos_to_add)
                        print(
                            f"PARTIAL FILL MERGED: Added ${usdc_paid:.4f} fill to active"
                            f" position {unmapped_positions[-1].get('trade_id', 'unmapped')}"
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

                    if realized_gain > 0:
                        state["total_xlm_accumulated"] = round(
                            state.get("total_xlm_accumulated", 0.0) + realized_gain,
                            7,
                        )

                        if "yield_history" not in state:
                            state["yield_history"] = []

                        timestamp = trade.get(
                            "ledger_close_time",
                            datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        )

                        state["yield_history"].append(
                            {
                                "timestamp": timestamp,
                                "amount_xlm": round(realized_gain, 7),
                                "trade_id": pos.get("trade_id", "consolidated"),
                                "sell_price": pos.get("target_sell_price", 0.0),
                            }
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
                        f" {portion:.4f} XLM to trade {pos.get('trade_id', 'group')}. "
                        f"Realized +{realized_gain:.7f} XLM gain."
                    )

                    if pos["xlm_to_sell"] <= 0.0001:
                        state["open_positions"].pop(idx)
                        print(
                            f"POSITION FULLY CLOSED: Removed {pos.get('trade_id', 'group')}"
                            " from grid state."
                        )

    except Exception as e:
        print(f"Notice: Trade reconciliation check failed ({e})")


def get_recent_high_low_close(srv, resolution_ms=300000, limit=3):
    """
    Fetches recent 5-min candles (limit=3 -> 15 min window) from Horizon trade aggregations.
    Returns (max_high, min_low, latest_close) or (None, None, None) on failure.
    """
    try:
        aggregations = (
            srv.trade_aggregations(
                base=XLM,
                counter=USDC,
                resolution=resolution_ms,
            )
            .limit(limit)
            .order(desc=True)
            .call()
            .get("_embedded", {})
            .get("records", [])
        )

        if aggregations:
            highs = [float(c["high"]) for c in aggregations]
            lows = [float(c["low"]) for c in aggregations]
            latest_close = float(aggregations[0]["close"])
            return max(highs), min(lows), latest_close
    except Exception as e:
        print(f"Warning: Could not fetch trade aggregations ({e}). Falling back to spot price.")

    return None, None, None


def manage_trailing_buy_offer(builder, current_price, state, liquid_usdc):
    high, low, close = get_recent_high_low_close(server)

    if high and low and close:
        baseline_price = (high + low + close) / 3.0
    else:
        baseline_price = current_price

    target_buy_price = round(baseline_price * DIP_THRESHOLD, 6)
    tranche_size_usdc = TOTAL_CAPITAL_USDC / NUM_TIERS

    usable_usdc = max(0.0, liquid_usdc - 0.02)
    real_deployed_usdc = TOTAL_CAPITAL_USDC - usable_usdc

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
                    f" ${target_buy_price:.4f} (Baseline: ${baseline_price:.4f})"
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
        if real_deployed_usdc < TOTAL_CAPITAL_USDC and usable_usdc >= tranche_size_usdc:
            xlm_to_buy = (tranche_size_usdc - 0.01) / target_buy_price
            print(
                f"Trailing Buy: Placing new bid for {xlm_to_buy:.4f} XLM @"
                f" ${target_buy_price:.4f} (Baseline: ${baseline_price:.4f})"
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
                f"Skipping buy placement: ${usable_usdc:.2f} USDC usable "
                f"(Deployed: ${real_deployed_usdc:.2f} / ${TOTAL_CAPITAL_USDC:.2f})."
            )

    return False, False


def log_portfolio_valuation(price, liquid_usdc, native_balance, state):
    open_positions = state.get("open_positions", [])

    grid_usdc_cost = sum(p.get("cost_usdc", 0.0) for p in open_positions)
    realized_xlm = state.get("total_xlm_accumulated", 0.0)
    pending_xlm = sum(p.get("pending_xlm_gain", 0.0) for p in open_positions)
    pending_usdc_val = pending_xlm * price

    xlm_market_value = native_balance * price
    total_net_worth_usdc = liquid_usdc + xlm_market_value

    yield_history = state.get("yield_history", [])
    last_yield = yield_history[-1] if yield_history else None

    print("=" * 55)
    print(f"PORTFOLIO VALUATION (@ XLM/USDC ${price:.6f})")
    print(f" Liquid USDC:            ${liquid_usdc:.2f}")
    print(
        f" Grid USDC Cost Basis:   ${grid_usdc_cost:.2f} ({len(open_positions)}"
        " open positions)"
    )
    print(
        f" XLM Balance:            {native_balance:.4f} XLM"
        f" (${xlm_market_value:.2f})"
    )
    print(f" Realized XLM Yield:     +{realized_xlm:.7f} XLM")
    if last_yield:
        print(
            f"   └─ Last Accumulated:  +{last_yield['amount_xlm']:.7f} XLM at"
            f" {last_yield['timestamp']}"
        )
    print(
        f" Pending XLM Yield:      +{pending_xlm:.7f} XLM"
        f" (${pending_usdc_val:.4f})"
    )
    print("-" * 55)
    print(f" TRUE NET WORTH:         ${total_net_worth_usdc:.2f} USDC")
    print("=" * 55)

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
    if created_new_subentry:
        liquid_xlm = max(0.0, liquid_xlm - 0.50)

    sync_and_stage_sell_offers(builder, state, public_key, server, liquid_xlm)

    # ---------------------------------------------------------
    # PORTFOLIO VALUATION LOG
    # ---------------------------------------------------------
def log_portfolio_valuation(price, liquid_usdc, native_balance, state):
    """Calculates true account net worth and pending grid yield."""
    open_positions = state.get("open_positions", [])

    grid_usdc_cost = sum(p.get("cost_usdc", 0.0) for p in open_positions)
    realized_xlm = state.get("total_xlm_accumulated", 0.0)
    
    # Calculate yield locked in open positions if all targets fill
    pending_xlm = sum(p.get("pending_xlm_gain", 0.0) for p in open_positions)
    pending_usdc_val = pending_xlm * price

    xlm_market_value = native_balance * price
    total_net_worth_usdc = liquid_usdc + xlm_market_value

    print("=" * 55)
    print(f"PORTFOLIO VALUATION (@ XLM/USDC ${price:.6f})")
    print(f" Liquid USDC:            ${liquid_usdc:.2f}")
    print(f" Grid USDC Cost Basis:   ${grid_usdc_cost:.2f} ({len(open_positions)} open positions)")
    print(f" XLM Balance:            {native_balance:.4f} XLM (${xlm_market_value:.2f})")
    print(f" Realized XLM Yield:     +{realized_xlm:.7f} XLM")
    print(f" Pending XLM Yield:      +{pending_xlm:.7f} XLM (${pending_usdc_val:.4f})")
    print("-" * 55)
    print(f" TRUE NET WORTH:         ${total_net_worth_usdc:.2f} USDC")
    print("=" * 55)



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
    if created_new_subentry:
        liquid_xlm = max(0.0, liquid_xlm - 0.50)

    sync_and_stage_sell_offers(builder, state, public_key, server, liquid_xlm)

    # Log true portfolio valuation before submission
    log_portfolio_valuation(price, liquid_usdc, native_balance, state)

    if len(builder.operations) > 0:
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

