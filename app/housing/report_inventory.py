"""Shared additions to detailed instant packets and single-message daily reports."""
from app.housing.inventory import inventory_text
from app.housing.lottery import queue_sort_key


def additions(card, snapshot):
    daily = snapshot.kind in {"digest", "evening"}
    full = (card.is_open and card.stage == "applying") if daily else bool(
        card.batch_id and any(r.id == card.batch_id and (r.is_new or r.is_update) for r in card.references))
    blocks = []
    if full:
        blocks.append(inventory_text(card.inventory, card.profile.housing_type))
    if card.lottery_breakdown and (card.in_transit or getattr(card, "changed", False) or not daily):
        from app.housing.competition import competition_text
        blocks.append(competition_text(card.lottery_breakdown))
        blocks.extend(getattr(card, "lottery_rules", []))
        blocks.extend(getattr(card, "lottery_progress", []))
    return "\n\n".join(blocks)
