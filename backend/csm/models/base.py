"""SQLAlchemy declarative base."""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Single declarative base for all ORM models."""
    pass
