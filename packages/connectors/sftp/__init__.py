"""SFTP drop connector - not implemented yet.

Planned for Phase 2 (spec section 77). Deliberately absent rather than stubbed:
a connector that silently returns nothing would let a reconciliation look
complete while a whole side was missing, which spec section 89 forbids.

When implemented it needs: polls a remote directory and hands files to FileConnector.

Everything a connector must provide is defined in ``packages.connectors.base``.
See ``docs/connector-sdk.md``.
"""

__all__: list[str] = []

IMPLEMENTED = False
NOTES = "polls a remote directory and hands files to FileConnector"
