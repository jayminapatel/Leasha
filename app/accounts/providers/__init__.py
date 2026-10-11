"""The contact connectors: Google People, Microsoft Graph and CardDAV.

Layer: L7

Order 1k 4a-4e. Each module implements `app.accounts.provider.ContactProvider`
over a `Transport` it is handed, and returns `app.contacts.card.Card` and
nothing else. No module here opens a socket or imports `app.accounts.http`.
"""
