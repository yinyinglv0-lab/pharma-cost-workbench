"""Shared Decimal display rules; percentages are expressed in percentage points.

Presentation never replaces exact arithmetic. Small nonzero percentages keep four
significant digits so a small gap cannot round to zero or exceed a 1% error budget.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext


def decimal_value(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _quantize(value, places):
    with localcontext() as context:
        context.prec = max(60, len(value.as_tuple().digits) + abs(places) + 4, value.adjusted() + places + 4)
        number = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return number.copy_abs() if number.is_zero() else number


def format_number(value, places=2, *, signed=False, grouping=True):
    """Format finite input with ROUND_HALF_UP; unavailable input is explicit."""
    number = decimal_value(value)
    if number is None:
        return '不可计算'
    if not isinstance(places, int) or not 0 <= places <= 60:
        raise ValueError('places must be an integer between 0 and 60')
    number = _quantize(number, places)
    return format(number, ('+' if signed else '') + (',' if grouping else '') + f'.{places}f')


def format_percent(value, *, signed=False):
    """Return a percentage number WITHOUT %; zero is 0.00, abs<1 uses 4 sig figs."""
    number = decimal_value(value)
    if number is None:
        return '不可计算'
    places = max(2, 3 - number.copy_abs().adjusted()) if number and abs(number) < 1 else 2
    rounded = _quantize(number, places)
    if number and abs(number) < 1 and rounded.adjusted() != number.copy_abs().adjusted():
        places = max(0, 3-rounded.copy_abs().adjusted())
        rounded = _quantize(rounded, places)
    return format(rounded, ('+' if signed else '') + f'.{places}f')


def percentage_fields(value):
    """Return JSON-safe exact numeric, Decimal text and adaptive display fields."""
    number = decimal_value(value)
    return {'value': float(number) if number is not None else None,
            'exact': str(number) if number is not None else None,
            'display': format_percent(number) if number is not None else None}
