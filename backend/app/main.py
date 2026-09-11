"""
AETHERIS Enterprise Esports Engine - v3.0 (Advanced)
Requirements: pip install fastapi uvicorn google-genai intasend-python pydantic
"""

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from typing import Dict, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, status, BackgroundTasks, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from google import genai
from google.genai import types
from intasend import APIService

# ---------------------------------------------------------------------
# LOGGING & CONFIGURATION
# ---------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s"
)
logger = logging.getLogger("AETHERIS-CORE")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# Initialize the new Google GenAI client
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

if not gemini_client:
    logger.warning("GEMINI_API_KEY missing! Running vision referee in local fallback simulation mode.")

app = FastAPI(
    title="AETHERIS Enterprise Esports Engine",
    version="3.0.0",
    docs_url="/docs"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------
# INTEGRATED SERVICES (IntaSend & Web3)
# ---------------------------------------------------------------------

class IntaSendRailService:
    def __init__(self):
        self.public_key = os.getenv("INTASEND_PUBLIC_KEY", "")
        self.secret_key = os.getenv("INTASEND_SECRET_KEY", "")
        self.test_mode = os.getenv("INTASEND_TEST_MODE", "True").lower() == "true"
        
        if self.public_key and self.secret_key:
            self.service = APIService(
                public_key=self.public_key,
                secret_key=self.secret_key,
                test=self.test_mode
            )
        else:
            self.service = None

    async def initiate_stk_push(self, phone_number: str, amount: float, match_id: str, email: str = "gamer@aetheris.co.ke"):
        """Triggers an M-Pesa STK Push via IntaSend's hosted masking service."""
        if not self.service:
            logger.info(f"[SIMULATED] IntaSend STK Push to {phone_number} for KES {amount}")
            return {"invoice_id": f"SIM-INV-{uuid.uuid4().hex[:8]}", "state": "PENDING"}

        try:
            # Run blocking IntaSend API call in a background thread to prevent freezing the event loop
            response = await asyncio.to_thread(
                self.service.collect.mpesa_stk_push,
                phone_number=phone_number,
                amount=amount,
                email=email,
                narrative=f"Aetheris Stake {match_id[:8]}"
            )
            return response
        except Exception as e:
            logger.error(f"IntaSend Collection Error: {str(e)}")
            raise e

    async def execute_payout(self, phone_number: str, amount: float, match_id: str):
        """Sends net winnings automatically to the victor's M-Pesa wallet."""
        if not self.service:
            logger.info(f"[SIMULATED] IntaSend Payout of KES {amount} to {phone_number}")
            return {"transaction_id": f"SIM-TX-{uuid.uuid4().hex[:8]}", "status": "COMPLETE"}

        try:
            response = await asyncio.to_thread(
                self.service.payout.mobile_checkout,
                provider="MPESA",
                phone_number=phone_number,
                amount=amount,
                narrative=f"Aetheris Winnings {match_id[:8]}"
            )
            return response
        except Exception as e:
            logger.error(f"IntaSend Payout Error: {str(e)}")
            raise e

class Web3EscrowService:
    """Handles crypto stakes for non-MPesa users (USDC/USDT on Polygon)."""
    async def release_escrow(self, match_id: str, winner_address: str, amount: float):
        logger.info(f"Executing Web3 Smart Contract release of {amount} to {winner_address}")
        await asyncio.sleep(1) # Simulating block confirmation time
        return f"0x{uuid.uuid4().hex}{uuid.uuid4().hex}"

intasend_service = IntaSendRailService()
web3_service = Web3EscrowService()

# ---------------------------------------------------------------------
# PYDANTIC SCHEMAS & DATA MODELS
# ---------------------------------------------------------------------

class RefereeVerdict(BaseModel):
    victory_detected: bool = Field(description="True if a definitive victory, checkmate, or game-over summary is visible.")
    winner_identifier: Optional[str] = Field(default=None, description="Extracted winning player tag, username, or side.")
    anomaly_detected: bool = Field(description="True if modded menus, floating overlays, or tampered UI elements are present.")
    confidence_score: float = Field(default=0.0, description="Model confidence score between 0.0 and 1.0.")
    status_message: str = Field(description="Concise description of the observed gameplay frame.")

class MatchStatus:
    LOBBY = "LOBBY_WAITING_FOR_STAKES"
    ACTIVE = "MATCH_IN_PROGRESS"
    ADJUDICATING = "AI_VERIFYING_VICTORY"
    SETTLED = "PAYOUT_COMPLETED"
    FLAGGED = "SUSPECTED_MOD_ANOMALY"

class Player(BaseModel):
    player_id: str
    identifier: str
    rail: str  # "INTASEND" or "WEB3"
    stake_amount: float
    staked_status: bool = False

class Match(BaseModel):
    match_id: str
    game_title: str
    game_mode: str
    max_players: int = 10
    players: Dict[str, Player] = {}
    total_pot: float = 0.0
    status: str = MatchStatus.LOBBY
    winner_id: Optional[str] = None
    created_at: float = Field(default_factory=time.time)

matches_db: Dict[str, Match] = {}
db_lock = asyncio.Lock()

# ---------------------------------------------------------------------
# WEBSOCKET ROOM PUBSUB MANAGER
# ---------------------------------------------------------------------

class RoomManager:
    def __init__(self):
        self.rooms: Dict[str, Set[WebSocket]] = {}
        self.processing_locks: Dict[str, bool] = {}

    async def connect(self, match_id: str, websocket: WebSocket):
        await websocket.accept()
        if match_id not in self.rooms:
            self.rooms[match_id] = set()
            self.processing_locks[match_id] = False
        self.rooms[match_id].add(websocket)

    def disconnect(self, match_id: str, websocket: WebSocket):
        if match_id in self.rooms:
            self.rooms[match_id].discard(websocket)
            if not self.rooms[match_id]:
                del self.rooms[match_id]
                del self.processing_locks[match_id]

    async def broadcast(self, match_id: str, message: dict):
        if match_id in self.rooms:
            disconnected = set()
            for ws in self.rooms[match_id]:
                try:
                    await ws.send_json(message)
                except Exception:
                    disconnected.add(ws)
            for ws in disconnected:
                self.disconnect(match_id, ws)

room_manager = RoomManager()

# ---------------------------------------------------------------------
# GEMINI VISION ADJUDICATION PIPELINE
# ---------------------------------------------------------------------

async def adjudicate_frame_advanced(frame_base64: str, game_title: str) -> RefereeVerdict:
    """Utilizes Google GenAI async client for real-time frame evaluation."""
    if not gemini_client:
        await asyncio.sleep(0.02)
        return RefereeVerdict(
            victory_detected=False, winner_identifier=None, 
            anomaly_detected=False, confidence_score=1.0, status_message="Fallback: Clean"
        )

    prompt = f"""
    You are the official AETHERIS AI Referee for '{game_title}'.
    Analyze this gameplay screen capture and strictly evaluate:
    1. Victory or match conclusion state.
    2. Winning player username/tag extraction.
    3. Detection of unauthorized modded APK overlays, god-mode tools, or UI anomalies.
    """

    try:
        image_bytes = base64.b64decode(frame_base64)
        
        # Using the official async aio implementation of the new SDK
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                prompt
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RefereeVerdict,
                temperature=0.1
            )
        )
        # Parse the strictly formatted JSON text into our Pydantic schema
        return RefereeVerdict.model_validate_json(response.text)
        
    except Exception as e:
        logger.error(f"Vision Adjudication Error: {str(e)}")
        return RefereeVerdict(
            victory_detected=False, winner_identifier=None, 
            anomaly_detected=False, confidence_score=0.0, status_message="Processing Error"
        )

# ---------------------------------------------------------------------
# ESCROW DISPATCHER & PAYOUT ENGINE (10% LOGIC)
# ---------------------------------------------------------------------

async def execute_escrow_settlement(match_id: str, winner_id: str, total_pot: float, rail: str, recipient: str):
    """Calculates the 10% platform fee and executes the 90% payout."""
    
    platform_cut = total_pot * 0.10
    winner_payout = total_pot * 0.90
    
    logger.info(f"[{match_id}] Settlement Started. Pot: KES {total_pot} | Winner: KES {winner_payout} | Platform Cut: KES {platform_cut}")
    tx_hash = ""

    try:
        if rail.upper() == "INTASEND":
            result = await intasend_service.execute_payout(
                phone_number=recipient,
                amount=winner_payout,
                match_id=match_id
            )
            tx_hash = result.get("transaction_id", f"INTASEND_{uuid.uuid4().hex[:8]}")
            
        elif rail.upper() == "WEB3":
            tx_hash = await web3_service.release_escrow(match_id, recipient, winner_payout)

        async with db_lock:
            if match_id in matches_db:
                matches_db[match_id].status = MatchStatus.SETTLED
                matches_db[match_id].winner_id = winner_id

        await room_manager.broadcast(match_id, {
            "type": "VICTORY_PAYOUT_EXECUTED",
            "match_id": match_id,
            "winner": winner_id,
            "gross_pot": total_pot,
            "net_amount_disbursed": winner_payout,
            "platform_fee_retained": platform_cut,
            "rail": rail,
            "transaction_id": tx_hash
        })
    except Exception as e:
        logger.error(f"Settlement failed for match {match_id}: {str(e)}")

# ---------------------------------------------------------------------
# REST ENDPOINTS
# ---------------------------------------------------------------------

class CreateMatchReq(BaseModel):
    game_title: str
    game_mode: str
    max_players: int = Field(default=10, le=10)
    stake_per_player: float = Field(gt=0)

class StakeDepositReq(BaseModel):
    match_id: str
    player_id: str
    rail: str = Field(pattern="^(INTASEND|WEB3)$")
    identifier: str
    amount: float = Field(gt=0)

@app.get("/", response_class=HTMLResponse)
async def root():
    """HTML Root route required by IntaSend domain validator."""
    return """
    <html>
        <head><title>Aetheris Esports</title></head>
        <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
            <h1>Aetheris Esports Platform</h1>
            <p>Live API & Tournament Gateway Operational.</p>
        </body>
    </html>
    """

@app.post("/api/match/create", status_code=status.HTTP_201_CREATED)
async def create_match(req: CreateMatchReq):
    match_id = f"MATCH-{uuid.uuid4().hex[:8].upper()}"
    new_match = Match(
        match_id=match_id,
        game_title=req.game_title,
        game_mode=req.game_mode,
        max_players=req.max_players,
        status=MatchStatus.LOBBY
    )
    async with db_lock:
        matches_db[match_id] = new_match
    return {"status": "SUCCESS", "match_id": match_id, "data": new_match}

@app.post("/api/escrow/deposit")
async def deposit_stake(req: StakeDepositReq):
    async with db_lock:
        if req.match_id not in matches_db:
            raise HTTPException(status_code=404, detail="Match ID not found")
        match = matches_db[req.match_id]
        
        if match.status != MatchStatus.LOBBY:
            raise HTTPException(status_code=400, detail="Match no longer accepting deposits")
        if len(match.players) >= match.max_players:
            raise HTTPException(status_code=400, detail="Lobby is full")

    stk_response = None
    if req.rail.upper() == "INTASEND":
        stk_response = await intasend_service.initiate_stk_push(
            phone_number=req.identifier,
            amount=req.amount,
            match_id=req.match_id
        )

    async with db_lock:
        player = Player(
            player_id=req.player_id,
            identifier=req.identifier,
            rail=req.rail,
            stake_amount=req.amount,
            staked_status=True  # In production, toggle this True only inside a webhook handler
        )
        match.players[req.player_id] = player
        match.total_pot += req.amount
        
        # Start match instantly if 10/10 lobby is full
        if len(match.players) == match.max_players:
            match.status = MatchStatus.ACTIVE

    await room_manager.broadcast(req.match_id, {
        "type": "STAKE_UPDATED",
        "total_pot": match.total_pot,
        "players_ready": f"{len(match.players)}/{match.max_players}",
        "match_status": match.status
    })

    return {
        "status": "STAKE_INITIATED",
        "match_id": req.match_id,
        "total_pot": match.total_pot,
        "stk_meta": stk_response
    }

# ---------------------------------------------------------------------
# REAL-TIME WEBSOCKET STREAMING
# ---------------------------------------------------------------------

@app.websocket("/ws/spectator/{match_id}")
async def spectator_node_endpoint(websocket: WebSocket, match_id: str):
    await room_manager.connect(match_id, websocket)
    
    async with db_lock:
        if match_id not in matches_db:
            await websocket.send_json({"type": "ERROR", "message": "Invalid Match ID"})
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            room_manager.disconnect(match_id, websocket)
            return

    frame_sequence = 0
    victory_confirmations = 0  # Temporal tracking to prevent false positives

    try:
        while True:
            raw_payload = await websocket.receive_text()
            frame_sequence += 1

            if room_manager.processing_locks.get(match_id, False):
                continue

            # Process every 4th frame to manage memory and API quotas
            if frame_sequence % 4 == 0:
                room_manager.processing_locks[match_id] = True
                frame_bytes = raw_payload.split(",")[1] if "," in raw_payload else raw_payload
                match_info = matches_db[match_id]

                verdict: RefereeVerdict = await adjudicate_frame_advanced(frame_bytes, match_info.game_title)
                room_manager.processing_locks[match_id] = False

                # Handle Mod/Cheat Detection
                if verdict.anomaly_detected:
                    async with db_lock:
                        matches_db[match_id].status = MatchStatus.FLAGGED
                    await room_manager.broadcast(match_id, {
                        "type": "ANOMALY_WARNING",
                        "match_id": match_id,
                        "message": "MODDED_BUILD_DETECTED. Room locked.",
                        "details": verdict.status_message
                    })
                    break

                # Require 2 consecutive victory frames to confirm a win (temporal consistency)
                if verdict.victory_detected:
                    victory_confirmations += 1
                else:
                    victory_confirmations = 0

                # Execute logic once victory is verified
                if victory_confirmations >= 2 and match_info.status not in [MatchStatus.SETTLED, MatchStatus.ADJUDICATING]:
                    async with db_lock:
                        matches_db[match_id].status = MatchStatus.ADJUDICATING

                    winner_id = verdict.winner_identifier or "PLAYER_01"
                    winner_profile = match_info.players.get(winner_id)
                    payout_rail = winner_profile.rail if winner_profile else "INTASEND"
                    recipient_id = winner_profile.identifier if winner_profile else "+254700000000"

                    asyncio.create_task(
                        execute_escrow_settlement(
                            match_id=match_id, 
                            winner_id=winner_id, 
                            total_pot=match_info.total_pot, 
                            rail=payout_rail, 
                            recipient=recipient_id
                        )
                    )
                    break

                await room_manager.broadcast(match_id, {
                    "type": "TELEMETRY",
                    "frame": frame_sequence,
                    "confidence": verdict.confidence_score,
                    "status": verdict.status_message
                })

    except WebSocketDisconnect:
        room_manager.disconnect(match_id, websocket)
    except Exception as e:
        logger.error(f"WebSocket execution error: {str(e)}")
        room_manager.disconnect(match_id, websocket)
