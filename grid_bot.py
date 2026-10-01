import json

def merge_into_position(target, addon):
    target["cost_usdc"] = round(
        target.get("cost_usdc", 0.0) + addon.get("cost_usdc", 0.0), 6
    )
    target["xlm_to_sell"] = round(
        target.get("xlm_to_sell", 0.0) + addon.get("xlm_to_sell", 0.0), 7
    )
    target["pending_xlm_gain"] = round(
        target.get("pending_xlm_gain", 0.0) + addon.get("pending_xlm_gain", 0.0), 7
    )

def consolidate_unmapped_positions(state):
    unmapped = [p for p in state["open_positions"] if not p.get("sell_offer_id")]
    if len(unmapped) <= 1:
        return

    grouped = {}
    for pos in unmapped:
        price_key = round(pos["target_sell_price"], 6)
        grouped.setdefault(price_key, []).append(pos)

    new_open_positions = [p for p in state["open_positions"] if p.get("sell_offer_id")]

    for price, group in grouped.items():
        base_pos = group[0]
        for addon in group[1:]:
            merge_into_position(base_pos, addon)
        new_open_positions.append(base_pos)

    state["open_positions"] = new_open_positions

if __name__ == "__main__":
    with open("grid_state.json", "r") as f:
        state = json.load(f)

    consolidate_unmapped_positions(state)

    with open("grid_state.json", "w") as f:
        json.dump(state, f, indent=2)
