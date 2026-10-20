"""Per-group Force Join configuration."""
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, ForeignKey
from bot.database.models import Base


class ForceJoinSetting(Base):
    __tablename__ = "force_join_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(BigInteger, ForeignKey("groups.id", ondelete="CASCADE"), unique=True, nullable=False)
    required_chat_id = Column(String(128), nullable=False)
    required_link = Column(String(512), nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
