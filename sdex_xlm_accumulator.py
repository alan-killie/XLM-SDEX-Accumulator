import os
import json
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

from stellar_sdk import (
    Server,
    Keypair,
    TransactionBuilder,
    Network,
    Asset,
    ManageBuyOffer,
    ManageSellOffer,
)
from stellar_sdk.exceptions import HorizonRequestException

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------
STATE_FILE = "grid_state.json"
STELLAR_NETWORK = Network.PUBLIC
HORIZON_URL = "https://horizon.stellar.org"

# Trading Pair Configuration
XLM_ASSET = Asset.native()
USDC_ASSET = Asset(
    "USDC", "GA5ZSEJYB37JRC5AVCI5M4GE3A33DD5TO336283M"  # Official USDC Issuer
)

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
    """Directly sync pending_offers with resting open offers on Horizon to fix discrepancies."""
    try:
        active_offers_response = server.offers().for_account(public_key).call()
        records = active_offers_response.get("_embedded", {}).get("records", [])

        state["pending_offers"] = [
            {
                "id": str(offer["id"]),
                "price": str(offer["price"]),
                "amount": str(offer["amount"]),
                "buying_asset": offer["buying_asset_type"] if offer["buying_asset_type"] == "native" else offer.get("buying_asset_code"),
                "selling_asset": offer["selling_asset_type"] if offer["selling_asset_type"] == "native" else offer.get("selling_asset_code"),
                "created_at": offer["last_modified_time"],
            }
            for offer in records
        ]
    except Exception as e:
        print(f"Warning: Failed to sync open offers from Horizon: {e}")


def reconcile_executed_trades(server: Server, public_key: str, state: dict) -> None:
    """Query recent DEX trade history to record completed buy/sell fills."""
    try:
        trades_response = server.trades().for_account(public_key).limit(20).call()
        trade_records = trades_response.get("_embedded", {}).get("records", [])

        # Process trades chronologically (oldest first)
        for trade in reversed(trade_records):
            # Check if trade matches our account acting as buyer or seller
            bought_xlm = (
                trade.get("bought_asset_type") == "native"
                and trade.get("sold_asset_code") == USDC_ASSET.code
            )
            sold_xlm = (
                trade.get("sold_asset_type") == "native"
                and trade.get("bought_asset_code") == USDC_ASSET.code
            )

            if bought_xlm:
                xlm_amount = Decimal(trade["bought_amount"])
                usdc_amount = Decimal(trade["sold_amount"])
                buy_price = usdc_amount / xlm_amount

                # Verify if this position is already recorded in state
                position_exists = any(
                    abs(Decimal(p["buy_price"]) - buy_price) < Decimal("0.0001")
                    and abs(Decimal(p["amount"]) - xlm_amount) < Decimal("0.0001")
                    for p in state["open_positions"]
                )
                if not position_exists:
                    state["open_positions"].append(
                        {
                            "amount": str(xlm_amount),
                            "buy_price": str(buy_price.quantize(Decimal("0.000001"))),
                            "target_sell_price": str((buy_price * (Decimal("1.0") + PROFIT_TAKE_PCT)).quantize(Decimal("0.000001"))),
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    print(f"Trade Reconciled: Bought {xlm_amount:.4f} XLM @ ${buy_price:.4f}")

            elif sold_xlm:
                xlm_amount = Decimal(trade["sold_amount"])
                sell_price = Decimal(trade["bought_amount"]) / xlm_amount

                # Find corresponding open position to close out
                for i, pos in enumerate(state["open_positions"]):
                    target_price = Decimal(pos["target_sell_price"])
                    if abs(sell_price - target_price) < Decimal("0.005") or sell_price >= target_price:
                        # Realize profit gain
                        entry_price = Decimal(pos["buy_price"])
                        profit_xlm = xlm_amount - (xlm_amount * entry_price / sell_price)
                        state["total_xlm_accumulated"] += float(profit_xlm)

                        print(f"Target Hit! Sold {xlm_amount:.4f} XLM @ ${sell_price:.4f} (+2%)")
                        state["open_positions"].pop(i)
                        break

    except Exception as e:
        print(f"Warning: Failed to reconcile recent trades: {e}")


def cleanup_stale_offers(server: Server, keypair: Keypair, state: dict) -> None:
    """Cancel pending offers older than MAX_OFFER_AGE_HOURS."""
    now = datetime.now(timezone.utc)
    account = server.load_account(keypair.public_key)

    for offer in list(state["pending_offers"]):
        offer_time = datetime.fromisoformat(offer["created_at"].replace("Z", "+00:00"))
        age_hours = (now - offer_time).total_seconds() / 3600.0

        if age_hours >= MAX_OFFER_AGE_HOURS:
            print(f"Canceling stale offer #{offer['id']} (Age: {age_hours:.2f}h)...")
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
    """Evaluate grid entry/exit criteria and place new limit orders as necessary."""
    xlm_bal, usdc_bal = get_account_balances(server, keypair.public_key)
    mid_price = fetch_mid_market_price(server)

    # Determine dynamic trade chunk size
    calculated_chunk = usdc_bal / Decimal(str(NUM_TIERS))
    trade_size_usdc = max(calculated_chunk, MIN_TRADE_USDC)

    # 1. Evaluate Dip-Buy Condition
    if usdc_bal >= trade_size_usdc and len(state["pending_offers"]) == 0:
        if state["open_positions"]:
            # Baseline is lowest entry in state
            lowest_entry = min(Decimal(p["buy_price"]) for p in state["open_positions"])
            target_buy_price = lowest_entry * (Decimal("1.0") - DIP_BUY_PCT)
            should_buy = mid_price <= target_buy_price
        else:
            # Baseline is current market price anchor (-1%)
            target_buy_price = mid_price * (Decimal("1.0") - DIP_BUY_PCT)
            should_buy = True  # Always anchor first entry if no open positions exist

        if should_buy:
            buy_price = target_buy_price.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
            xlm_to_buy = (trade_size_usdc / buy_price).quantize(Decimal("0.0000001"), rounding=ROUND_DOWN)

            print(f"Dip Detected! Placing Buy Offer: {xlm_to_buy} XLM @ ${buy_price}")

            account = server.load_account(keypair.public_key)
            tx = (
                TransactionBuilder(account, STELLAR_NETWORK, base_fee=100)
                .append_manage_buy_offer_op(
                    asset_sold=USDC_ASSET,
                    asset_bought=XLM_ASSET,
                    buy_amount=str(xlm_to_buy),
                    price=str(buy_price),
                    offer_id=0,
                )
                .set_timeout(30)
                .build()
            )
            tx.sign(keypair)
            response = server.submit_transaction(tx)
            print("Transaction submitted successfully.")


def main():
    secret_key = os.getenv("STELLAR_SECRET_KEY")
    if not secret_key:
        raise ValueError("STELLAR_SECRET_KEY environment variable not configured.")

    keypair = Keypair.from_secret(secret_key)
    server = Server(HORIZON_URL)

    state = load_grid_state()

    # Step 1: Sync active open offers directly from Horizon
    sync_active_offers_from_horizon(server, keypair.public_key, state)

    # Step 2: Reconcile any newly executed trade fills
    reconcile_executed_trades(server, keypair.public_key, state)

    # Step 3: Clean up expired/stale orders
    cleanup_stale_offers(server, keypair, state)

    # Step 4: Run trading strategy checks
    execute_grid_logic(server, keypair, state)

    # Step 5: Sync state once more and save
    sync_active_offers_from_horizon(server, keypair.public_key, state)
    save_grid_state(state)


if __name__ == "__main__":
    main()
