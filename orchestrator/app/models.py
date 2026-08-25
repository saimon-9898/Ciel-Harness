"""Database models.

Phase 1 deliberately defines no models. Future phases will add entities such
as agents, tasks, sessions, and events. Importing this module from
``db.init_db()`` registers any defined models on ``Base.metadata`` so their
tables are created automatically.
"""

from .db import Base

__all__ = ["Base"]
