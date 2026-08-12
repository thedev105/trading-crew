# polytrading

`polytrading` is point-in-time, market-neutral trading research software.

## Public-data-only boundary

This package is read-only research software. It is limited to public market data
and does not connect to accounts or execute trading activity.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest tests/test_package.py -q
.venv/bin/ruff check .
```
