import os
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from stellar_sdk import (
    Server,
    Keypair,
    TransactionBuilder,
    Network,
    Asset,
    StrKey,
)

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------
STATE_FILE = "grid_state.json"
STELLAR_NETWORK = Network.PUBLIC_NETWORK_PASSPHRASE
HORIZON_URL = "https://horizon.stellar.org"

# Official Circle USDC Mainnet Issuer (56 characters)
USDC_ISSUER = "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"

# Startup Key Validation Check
if not StrKey.is_valid_ed25519_public_key(USDC_ISSUER):
    raise ValueError(f"Invalid Stellar public key format for USDC issuer: {USDC_ISSUER}")

# Trading Pair Configuration
XLM_ASSET = Asset.native()
USDC_ASSET = Asset("USDC", USDC_ISSUER)

# Strategy Parameters
NUM_TIERS = 10
MIN_TRADE_USDC = Decimal("1.00")
DIP_BUY_PCT = Decimal("0.01")       # -1.0% Dip Trigger
PROFIT_TAKE_PCT = Decimal("0.02")   # +2.0% Profit Target
MAX_OFFER_AGE_HOURS = 1.0


def load_grid_state() -> dict:
    """Load or initialize local grid state schema."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                pass
    return {
        "open_positions": [],
        "pending_offers": [],
        "total_xlm_accumulated": 0.0,
    }


def save_grid_state(state: dict) -> None:
    """Save grid state JSON to disk."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_account_balances(server: Server, public_key: str) -> tuple[Decimal, Decimal]:
    """Fetch current XLM and USDC liquid balances from Horizon."""
    account = server.accounts().account_id(public_key).call()
    xlm_balance = Decimal("0.0")
    usdc_balance = Decimal("0.0")

    for balance in account["balances"]:
        if balance["asset_type"] == "native":
            xlm_balance = Decimal(balance["balance"])
        elif (
            balance.get("asset_code") == USDC_ASSET.code
            and balance.get("asset_issuer") == USDC_ASSET.issuer
        ):
            usdc_balance = Decimal(balance["balance"])

    return xlm_balance, usdc_balance


def fetch_mid_market_price(server: Server) -> Decimal:
    """Fetch live mid-market price for XLM/USDC from the order book."""
    orderbook = (
        server.orderbook(selling=XLM_ASSET, buying=USDC_ASSET)
        .limit(1)
        .call()
    )
    bids = orderbook.get("bids", [])
    asks = orderbook.get("asks", [])

    if not bids or not asks:
        raise ValueError("Insufficient order book depth on XLM/USDC pair.")

    best_bid = Decimal(bids[0]["price"])
    best_ask = Decimal(asks[0]["price"])
    return (best_bid + best_ask) / Decimal("2")


def sync_active_offers_from_horizon(server: Server, public_key: str, state: dict) -> None:
    """Directly sync pending_offers with resting open offers on Horizon."""
    try:
        active_offers_response = server.offers().for_account(public_key).limit(50).call()
        records = active_offers_response.get("_embedded", {}).get("records", [])

        state["pending_offers"] = [
            {
                "id": str(offer["id"]),
                "price": str(offer["price"]),
                "amount": str(offer["amount"]),
                "buying_asset": (
                    offer["buying_asset_type"]
                    if offer["buying_asset_type"] == "native"
                    else offer.get("buying_asset_code")
                ),
                "selling_asset": (
                    offer["selling_asset_type"]
                    if offer["selling_asset_type"] == "native"
                    else offer.get("selling_asset_code")
                ),
                "created_at": offer["last_modified_time"],
            }
            for offer in records
        ]
    except Exception as e:
        print(f"Warning: Failed to sync open offers from Horizon: {e}")


def reconcile_executed_trades(server: Server, public_key: str, state: dict) -> None:
    """Query recent DEX trade history using valid Horizon base/counter fields."""
    try:
        trades_response = server.trades().for_account(public_key).limit(20).call()
        trade_records = trades_response.get("_embedded", {}).get("records", [])

        for trade in reversed(trade_records):
            is_base = trade.get("base_account") == public_key
            base_is_seller = trade.get("base_is_seller", True)

            base_type = trade.get("base_asset_type")
            counter_code = trade.get("counter_asset_code")

            # Validate XLM/USDC pair (Base = XLM native, Counter = USDC)
            is_xlm_usdc_pair = base_type == "native" and counter_code == USDC_ASSET.code
            if not is_xlm_usdc_pair:
                continue

            # Determine trade direction relative to public_key
            if is_base:
                bought_xlm = not base_is_seller
                sold_xlm = base_is_seller
            else:
                bought_xlm = base_is_seller
                sold_xlm = not base_is_seller

            xlm_amount = Decimal(trade["base_amount"])
            usdc_amount = Decimal(trade["counter_amount"])
            trade_price = usdc_amount / xlm_amount

            if bought_xlm:
                position_exists = any(
                    abs(Decimal(p["buy_price"]) - trade_price) < Decimal("0.0001")
                    and abs(Decimal(p["amount"]) - xlm_amount) < Decimal("0.0001")
                    for p in state["open_positions"]
                )
                if not position_exists:
                    target_sell = (trade_price * (Decimal("1.0") + PROFIT_TAKE_PCT)).quantize(
                        Decimal("0.000001")
                    )
                    state["open_positions"].append(
                        {
                            "amount": str(xlm_amount),
                            "buy_price": str(trade_price.quantize(Decimal("0.000001"))),
                            "target_sell_price": str(target_sell),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    print(f"Trade Reconciled: Bought {xlm_amount:.4f} XLM @ ${trade_price:.4f}")

            elif sold_xlm:
                candidates = []
                for i, pos in enumerate(state["open_positions"]):
                    target_price = Decimal(pos["target_sell_price"])
                    if trade_price >= target_price - Decimal("0.0005"):
                        candidates.append((i, abs(trade_price - target_price)))

                if candidates:
                    candidates.sort(key=lambda x: x[1])
                    match_idx = candidates[0][0]
                    matched_pos = state["open_positions"].pop(match_idx)

                    entry_price = Decimal(matched_pos["buy_price"])
                    profit_xlm = xlm_amount - (xlm_amount * entry_price / trade_price)
                    state["total_xlm_accumulated"] += float(profit_xlm)

                    print(f"Target Hit! Sold {xlm_amount:.4f} XLM @ ${trade_price:.4f} (+2%)")

    except Exception as e:
        print(f"Warning: Failed to reconcile recent trades: {e}")


def cleanup_stale_offers(server: Server, keypair: Keypair, state: dict) -> None:
    """Cancel pending offers older than MAX_OFFER_AGE_HOURS."""
    now = datetime.now(timezone.utc)

    for offer in list(state["pending_offers"]):
        offer_time = datetime.fromisoformat(offer["created_at"].replace("Z", "+00:00"))
        age_hours = (now - offer_time).total_seconds() / 3600.0

        if age_hours >= MAX_OFFER_AGE_HOURS:
            print(f"Canceling stale offer #{offer['id']} (Age: {age_hours:.2f}h)...")
            account = server.load_account(keypair.public_key)
            tx = (
                TransactionBuilder(account, STELLAR_NETWORK, base_fee=100)
                .append_manage_buy_offer_op(
                    asset_sold=USDC_ASSET,
                    asset_bought=XLM_ASSET,
                    buy_amount="0",
                    price=offer["price"],
                    offer_id=int(offer["id"]),
                )
                .set_timeout(30)
                .build()
            )
            tx.sign(keypair)
            server.submit_transaction(tx)


def execute_grid_logic(server: Server, keypair: Keypair, state: dict) -> None:
    """Evaluate grid entry/exit criteria and submit all required operations in a single transaction."""
    xlm_bal, usdc_bal = get_account_balances(server, keypair.public_key)
    mid_price = fetch_mid_market_price(server)

    calculated_chunk = usdc_bal / Decimal(str(NUM_TIERS))
    trade_size_usdc = max(calculated_chunk, MIN_TRADE_USDC)

    account = server.load_account(keypair.public_key)
    builder = TransactionBuilder(account, STELLAR_NETWORK, base_fee=100)
    has_operations = False

    # 1. Stage Take-Profit Sell Orders for Unhedged Positions
    for pos in state["open_positions"]:
        target_price = str(pos["target_sell_price"])
        pos_amount = str(pos["amount"])

        has_active_sell = any(
            offer.get("selling_asset") in ["native", "XLM"]
            and abs(Decimal(offer["price"]) - Decimal(target_price)) < Decimal("0.0001")
            for offer in state["pending_offers"]
        )

        if not has_active_sell:
            print(f"Staging Take-Profit Sell: {pos_amount} XLM @ ${target_price}")
            builder.append_manage_sell_offer_op(
                asset_selling=XLM_ASSET,
                asset_buying=USDC_ASSET,
                amount=pos_amount,
                price=target_price,
                offer_id=0,
            )
            has_operations = True

    # 2. Stage Dip-Buy Order
    if usdc_bal >= trade_size_usdc and len(state["pending_offers"]) == 0:
        if state["open_positions"]:
            lowest_entry = min(Decimal(p["buy_price"]) for p in state["open_positions"])
            target_buy_price = lowest_entry * (Decimal("1.0") - DIP_BUY_PCT)
            should_buy = mid_price <= target_buy_price
        else:
            target_buy_price = mid_price * (Decimal("1.0") - DIP_BUY_PCT)
            should_buy = True

        if should_buy:
            buy_price = target_buy_price.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            xlm_to_buy = (trade_size_usdc / buy_price).quantize(
                Decimal("0.0000001"), rounding=ROUND_DOWN
            )

            print(f"Staging Buy Offer: {xlm_to_buy} XLM @ ${buy_price}")
            builder.append_manage_buy_offer_op(
                asset_sold=USDC_ASSET,
                asset_bought=XLM_ASSET,
                buy_amount=str(xlm_to_buy),
                price=str(buy_price),
                offer_id=0,
            )
            has_operations = True

    # 3. Submit single atomic transaction envelope if any operations were staged
    if has_operations:
        tx = builder.set_timeout(30).build()
        tx.sign(keypair)
        server.submit_transaction(tx)
        print("Grid transaction envelope submitted successfully.")


def main():
    secret_key = os.getenv("STELLAR_SECRET_KEY")
    if not secret_key:
        raise ValueError("STELLAR_SECRET_KEY environment variable not configured.")

    keypair = Keypair.from_secret(secret_key)
    server = Server(HORIZON_URL)

    state = load_grid_state()

    # Step 1: Query resting offers directly from Horizon
    sync_active_offers_from_horizon(server, keypair.public_key, state)

    # Step 2: Reconcile recent trade fills with open positions
    reconcile_executed_trades(server, keypair.public_key, state)

    # Step 3: Clean up stale pending offers (>1 hour old)
    cleanup_stale_offers(server, keypair, state)

    # Step 4: Execute buy/sell grid logic
    execute_grid_logic(server, keypair, state)

    # Step 5: Perform final offer sync and write state back to JSON
    sync_active_offers_from_horizon(server, keypair.public_key, state)
    save_grid_state(state)


if __name__ == "__main__":
    main()
