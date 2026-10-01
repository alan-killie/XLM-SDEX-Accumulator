def consolidate_unmapped_positions(state):
    unmapped = [p for p in state["open_positions"] if not p.get("sell_offer_id")]
    if len(unmapped) <= 1:
        return

    # Group unmapped positions by target_sell_price (rounded to 6 decimals)
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
