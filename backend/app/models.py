from backend.app.database import Base
from sqlalchemy import Column, Float, Integer, String


class MatchEscrow(Base):
  __tablename__ = "match_escrows"

  id = Column(Integer, primary_key=True, index=True)
  match_id = Column(String, index=True)
  player_phone = Column(String, index=True)
  amount = Column(Float, default=10.0)
  checkout_request_id = Column(String, unique=True, index=True)
  status = Column(String, default="pending")  # pending, funded, completed
  mpesa_receipt = Column(String, nullable=True)
