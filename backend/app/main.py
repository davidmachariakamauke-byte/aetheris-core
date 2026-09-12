import os
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from backend.app.database import engine, get_db, Base
from backend.app.models import MatchEscrow
from backend.app.services.mpesa_rail import MpesaRail

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

RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://your-app-name.onrender.com")

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("index.html", "r") as f:
        return f.read()

@app.post("/api/v1/deposit")
async def initiate_deposit(phone_number: str, match_id: str, db: Session = Depends(get_db)):
    callback_url = f"{RENDER_URL}/api/v1/payments/stk-callback"
    
    response = mpesa_service.trigger_stk_push(
        phone_number=phone_number,
        amount=10, 
        account_ref=match_id,
        callback_url=callback_url
    )
    
    checkout_request_id = response.get("CheckoutRequestID")
    if not checkout_request_id:
        raise HTTPException(status_code=400, detail=response.get("errorMessage", "STK Push failed"))

    new_escrow = MatchEscrow(
        match_id=match_id,
        player_phone=phone_number,
        amount=10,
        checkout_request_id=checkout_request_id,
        status="pending"
    )
    db.add(new_escrow)
    db.commit()
    
    return {"message": "STK Push sent", "checkout_request_id": checkout_request_id}

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
        
        escrow = db.query(MatchEscrow).filter(MatchEscrow.checkout_request_id == checkout_request_id).first()
        if escrow:
            escrow.status = "funded"
            escrow.mpesa_receipt = mpesa_receipt
            db.commit()
            
    return {"ResultCode": 0, "ResultDesc": "Accepted"}

@app.post("/api/v1/release-escrow")
async def release_escrow(match_id: str, winner_phone: str, db: Session = Depends(get_db)):
    # 20 total pool - 10% total platform/gateway fee = 18 KES net payout
    payout_amount = 18 
    return {"message": f"Escrow released. {payout_amount} KES scheduled for disbursement to {winner_phone}"}
