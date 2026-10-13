"""Synthetic review fixture: deliberately incorrect, never a production dependency."""


def low_stock(records, threshold):
    totals = {}
    for record in records:
        totals[record['item']] = record['quantity']
    return sorted(item for item, quantity in totals.items() if quantity < threshold)
