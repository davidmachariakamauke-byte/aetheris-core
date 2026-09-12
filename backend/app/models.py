from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime

Base = declarative_base()

class UserProfile(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    phone_number = Column(String, unique=True, index=True) # Safaricom M-PESA line
    
    # Newly added for AI multi-player tracking
    in_game_id = Column(String, unique=True, index=True) 
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class MatchHistory(Base):
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True, index=True)
    in_game_id = Column(String, index=True) # Ties back to UserProfile
    match_status = Column(String, default="ongoing") # ongoing, completed, disqualified
    ai_fraud_flag = Column(Boolean, default=False) # Flags if the AI detected modified apps
    winner_declared = Column(Boolean, default=False)
