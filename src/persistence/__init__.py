"""Dormant Super-7 PostgreSQL persistence foundation.

Importing this package declares metadata only; it does not connect to a database.
"""

from persistence.base import Base

__all__ = ["Base"]
