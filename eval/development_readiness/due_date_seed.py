"""Synthetic library due-date helper. Fictional data only; never a production dependency."""
from datetime import timedelta


def due_date(start, days, closed_weekdays):
    """Return the due date `days` after `start` (a datetime.date).

    closed_weekdays uses date.weekday() numbers (Monday=0 ... Sunday=6).
    """
    closed = set(closed_weekdays)
    if days < 0:
        raise ValueError('days must not be negative')
    if set(range(7)) <= closed:
        raise ValueError('at least one weekday must be open')
    return start + timedelta(days=days)
