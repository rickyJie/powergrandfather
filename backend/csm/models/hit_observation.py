"""HitObservation ORM model — recorded "you've hit your limit" event with 5h window snapshot."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base


class HitObservation(Base):
    __tablename__ = "hit_observation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    reset_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    msg_count_5h: Mapped[int] = mapped_column(Integer, default=0)
    cc_tokens_5h: Mapped[int] = mapped_column(BigInteger, default=0)
    cr_tokens_5h: Mapped[int] = mapped_column(BigInteger, default=0)
    input_tokens_5h: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens_5h: Mapped[int] = mapped_column(BigInteger, default=0)
    raw_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
