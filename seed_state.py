import os
import json
from sdex_xlm_accumulator import server, public_key, save_state

def initialize_clean_state():
    """Resets grid_state.json to a clean 0-position baseline and seeds recent trade IDs."""
    print(f"Initializing clean state for public key: {public_key}")

    clean_state = {
        "open_positions": [],
        "pending_offers": [],
        "processed_trade_ids": [],
        "total_xlm_accumulated": 0.0
    }

    try:
        # Query last 20 trades to seed processed_trade_ids
        trades = server.trades().for_account(public_key).limit(20).call()
        records = trades.get("_embedded", {}).get("records", [])

        for trade in records:
            trade_id = str(trade["id"])
            if trade_id not in clean_state["processed_trade_ids"]:
                clean_state["processed_trade_ids"].append(trade_id)

        print(f"Successfully seeded {len(clean_state['processed_trade_ids'])} recent trade IDs.")
    except Exception as e:
        print(f"Warning: Could not fetch trade history from Horizon: {e}")

    save_state(clean_state)
    print("grid_state.json successfully re-initialized!")

if __name__ == "__main__":
    initialize_clean_state()
