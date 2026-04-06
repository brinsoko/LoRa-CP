from __future__ import annotations


FORMULA_PREFIXES = ("=", "+", "-", "@")


def escape_formula_cell(value):
    if isinstance(value, str):
        if value.startswith(FORMULA_PREFIXES):
            return "'" + value
        return value
    return value


def escape_formula_cells(values):
    if isinstance(values, list):
        return [escape_formula_cells(v) for v in values]
    return escape_formula_cell(values)

