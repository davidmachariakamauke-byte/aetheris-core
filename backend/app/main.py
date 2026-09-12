import os
from backend.app.database import Base, engine, get_db
from backend.app.models import MatchEscrow
from backend.app.services.mpesa_rail import MpesaRail
from backend.app.services.web3_rail import Web3Rail
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel

Base.metadata.create_all(bind=engine)
load_dotenv()

app = FastAPI(title="Aetheris Esports Engine V8")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

mpesa_service = MpesaRail()
web3_service = Web3Rail()

RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com")

class ChatMessage(BaseModel):
    message: str
    match_id: str = "UNKNOWN"

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
  if os.path.exists("index.html"):
    with open("index.html", "r") as f:
      return f.read()
  return "<h1>Aetheris Backend Service Online</h1>"

# Notice we added in_game_id here
@app.post("/api/v1/deposit")
async def initiate_deposit(
    phone_number: str,
    match_id: str,
    in_game_id: str = "Player_Unknown", 
    amount: int = 10,
    db: Session = Depends(get_db),
):
  callback_url = f"{RENDER_URL}/api/v1/payments/stk-callback"

  response = mpesa_service.trigger_stk_push(
      phone_number=phone_number,
      amount=amount,
      account_ref=match_id,
      callback_url=callback_url,
  )

  checkout_request_id = response.get("CheckoutRequestID")
  if not checkout_request_id:
    raise HTTPException(status_code=400, detail="STK Push failure")

  new_escrow = MatchEscrow(
      match_id=match_id,
      player_phone=phone_number,
      in_game_id=in_game_id, # Bound to the database!
      amount=float(amount),
      checkout_request_id=checkout_request_id,
      status="pending",
  )
  db.add(new_escrow)
  db.commit()

  return {"status": "success", "checkout_request_id": checkout_request_id}
