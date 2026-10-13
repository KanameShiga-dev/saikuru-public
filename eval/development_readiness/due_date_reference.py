"""Evaluator-only reference for synthetic-due-date-v1. Never copied into the AI project."""
from datetime import timedelta


def due_date(start, days, closed_weekdays):
    closed = set(closed_weekdays)
    if days < 0:
        raise ValueError('days must not be negative')
    if set(range(7)) <= closed:
        raise ValueError('at least one weekday must be open')
    result = start + timedelta(days=days)
    while result.weekday() in closed:
        result += timedelta(days=1)
    return result
