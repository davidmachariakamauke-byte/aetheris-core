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

Base.metadata.create_all(bind=engine)
load_dotenv()

app = FastAPI(title="Aetheris Esports Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

mpesa_service = MpesaRail()
web3_service = Web3Rail()

RENDER_URL = os.getenv(
    "RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com"
)


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
  if os.path.exists("index.html"):
    with open("index.html", "r") as f:
      return f.read()
  return "<h1>Aetheris Backend Service Online</h1>"


@app.post("/api/v1/deposit")
async def initiate_deposit(
    phone_number: str,
    match_id: str,
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
    raise HTTPException(
        status_code=400,
        detail=response.get(
            "errorMessage", "STK Push failure from Daraja Gateway"
        ),
    )

  new_escrow = MatchEscrow(
      match_id=match_id,
      player_phone=phone_number,
      amount=float(amount),
      checkout_request_id=checkout_request_id,
      status="pending",
  )
  db.add(new_escrow)
  db.commit()

  return {"status": "success", "checkout_request_id": checkout_request_id}


@app.post("/api/v1/payments/stk-callback")
async def handle_stk_callback(request: Request, db: Session = Depends(get_db)):
  body = await request.json()
  stk_callback = body.get("Body", {}).get("stkCallback", {})

  checkout_request_id = stk_callback.get("CheckoutRequestID")
  result_code = stk_callback.get("ResultCode")

  if result_code == 0:
    callback_metadata = stk_callback.get("CallbackMetadata", {}).get("Item", [])
    metadata = {item["Name"]: item.get("Value") for item in callback_metadata}
    mpesa_receipt = metadata.get("MpesaReceiptNumber")

    escrow = (
        db.query(MatchEscrow)
        .filter(MatchEscrow.checkout_request_id == checkout_request_id)
        .first()
    )
    if escrow:
      escrow.status = "funded"
      escrow.mpesa_receipt = mpesa_receipt
      db.commit()

  return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.post("/api/v1/release-escrow")
async def release_escrow(
    match_id: str, winner_phone: str, db: Session = Depends(get_db)
):
  # 20 total pool - 10% fee (2) = 18 KES winner distribution
  payout_amount = 18
  web3_service.log_match_result(match_id, winner_phone)

  return {
      "status": "success",
      "net_payout": payout_amount,
      "recipient": winner_phone,
  }
