"""Generic bank REST/OFX connector - not implemented yet.

Planned for Phase 2 (spec section 77). Deliberately absent rather than stubbed:
a connector that silently returns nothing would let a reconciliation look
complete while a whole side was missing, which spec section 89 forbids.

When implemented it needs: per-bank endpoint mapping supplied through connection config.

Everything a connector must provide is defined in ``packages.connectors.base``.
See ``docs/connector-sdk.md``.
"""

__all__: list[str] = []

IMPLEMENTED = False
NOTES = "per-bank endpoint mapping supplied through connection config"
