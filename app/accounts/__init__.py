"""Accounts: sign-in, secrets, scheduling and the sync engine.

Layer: L7

Order 1k. The only package, with enrichment, that may open a non-loopback
connection (`tests/unit/test_accounts_network_guard.py` pins the hosts).
Nothing here imports Qt: the window drives it through `app/ui/`.
"""
