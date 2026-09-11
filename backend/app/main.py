import asyncio
import base64
import json
import logging
import os
import time
import uuid
from typing import Dict, Optional, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, status, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from app.services.mpesa_rail import MPesaRailService
from app.services.web3_rail import Web3EscrowService

# ---------------------------------------------------------------------
# LOGGING & CONFIGURATION
# ---------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s]: %(message)s"
)
logger = logging.getLogger("AETHERIS-CORE")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

if not gemini_client:
    logger.warning("GEMINI_API_KEY missing! Running vision referee in local fallback simulation mode.")

app = FastAPI(
    title="AETHERIS Enterprise Esports Engine",
    version="2.6.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

mpesa_service = MPesaRailService()
web3_service = Web3EscrowService()

# ---------------------------------------------------------------------
# PYDANTIC SCHEMAS & DATA MODELS
# ---------------------------------------------------------------------

class RefereeVerdict(BaseModel):
    """Structured Pydantic schema enforced on Gemini 2.5 Flash response."""
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
    identifier: str  # Phone number (+254...) or Wallet Address (0x...)
    rail: str        # "MPESA" or "USDT"
    stake_amount: float
    staked_status: bool = False

class Match(BaseModel):
    match_id: str
    game_title: str
    game_mode: str
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
    """Manages multi-client WebSocket broadcasts per match room."""
    def __init__(self):
        self.rooms: Dict[str, Set[WebSocket]] = {}
        self.processing_locks: Dict[str, bool] = {}

    async def connect(self, match_id: str, websocket: WebSocket):
        await websocket.accept()
        if match_id not in self.rooms:
            self.rooms[match_id] = set()
            self.processing_locks[match_id] = False
        self.rooms[match_id].add(websocket)
        logger.info(f"Client connected to room {match_id}. Listeners: {len(self.rooms[match_id])}")

    def disconnect(self, match_id: str, websocket: WebSocket):
        if match_id in self.rooms:
            self.rooms[match_id].discard(websocket)
            if not self.rooms[match_id]:
                del self.rooms[match_id]
                del self.processing_locks[match_id]
        logger.info(f"Client disconnected from room {match_id}.")

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
    """Sends gameplay frame to Gemini 2.5 Flash using structured response schemas."""
    if not gemini_client:
        await asyncio.sleep(0.02)
        return RefereeVerdict(
            victory_detected=False,
            winner_identifier=None,
            anomaly_detected=False,
            confidence_score=1.0,
            status_message="Fallback Mode: Frame clean."
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
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
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
        return RefereeVerdict.model_validate_json(response.text)
    except Exception as e:
        logger.error(f"Gemini Vision Adjudication Error: {str(e)}")
        return RefereeVerdict(
            victory_detected=False,
            winner_identifier=None,
            anomaly_detected=False,
            confidence_score=0.0,
            status_message="Frame Processing Error"
        )

# ---------------------------------------------------------------------
# ESCROW DISPATCHER & PAYOUT ENGINE
# ---------------------------------------------------------------------

async def execute_escrow_settlement(match_id: str, winner_id: str, payout_amount: float, rail: str, recipient: str):
    """Executes disbursement via M-Pesa B2C or Web3 Escrow Smart Contract."""
    logger.info(f"Initiating settlement for {match_id}: {payout_amount} via {rail} to {recipient}")
    tx_hash = ""

    try:
        if rail.upper() == "MPESA":
            result = await mpesa_service.execute_b2c_payout(
                phone_number=recipient,
                amount=payout_amount,
                match_id=match_id,
                result_url=f"https://api.aetheris.gg/callbacks/mpesa/b2c/result",
                timeout_url=f"https://api.aetheris.gg/callbacks/mpesa/b2c/timeout"
            )
            tx_hash = result.get("ConversationID", f"MPESA_TX_{uuid.uuid4().hex[:8]}")
        elif rail.upper() == "USDT":
            tx_hash = await web3_service.release_escrow(
                match_id=match_id,
                winner_address=recipient,
                amount=payout_amount
            )
        else:
            tx_hash = f"SIMULATED_TX_{uuid.uuid4().hex[:12].upper()}"

        async with db_lock:
            if match_id in matches_db:
                matches_db[match_id].status = MatchStatus.SETTLED
                matches_db[match_id].winner_id = winner_id

        await room_manager.broadcast(match_id, {
            "type": "VICTORY_PAYOUT_EXECUTED",
            "match_id": match_id,
            "winner": winner_id,
            "amount_disbursed": payout_amount,
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
    stake_per_player: float = Field(gt=0)

class StakeDepositReq(BaseModel):
    match_id: str
    player_id: str
    rail: str = Field(pattern="^(MPESA|USDT)$")
    identifier: str
    amount: float = Field(gt=0)
    callback_url: Optional[str] = None

@app.get("/healthz")
async def health_check():
    return {"status": "HEALTHY", "engine": "AETHERIS v2.6.0", "gemini_connected": gemini_client is not None}

@app.post("/api/match/create", status_code=status.HTTP_201_CREATED)
async def create_match(req: CreateMatchReq):
    match_id = f"MATCH-{uuid.uuid4().hex[:8].upper()}"
    new_match = Match(
        match_id=match_id,
        game_title=req.game_title,
        game_mode=req.game_mode,
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

    stk_response = None
    if req.rail.upper() == "MPESA":
        cb_url = req.callback_url or "https://api.aetheris.gg/callbacks/mpesa/stk"
        stk_response = await mpesa_service.initiate_stk_push(
            phone_number=req.identifier,
            amount=req.amount,
            match_id=req.match_id,
            callback_url=cb_url
        )

    async with db_lock:
        player = Player(
            player_id=req.player_id,
            identifier=req.identifier,
            rail=req.rail,
            stake_amount=req.amount,
            staked_status=True
        )
        match.players[req.player_id] = player
        match.total_pot += req.amount
        if len(match.players) >= 2:
            match.status = MatchStatus.ACTIVE

    await room_manager.broadcast(req.match_id, {
        "type": "STAKE_UPDATED",
        "total_pot": match.total_pot,
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
    try:
        while True:
            raw_payload = await websocket.receive_text()
            frame_sequence += 1

            if room_manager.processing_locks.get(match_id, False):
                continue

            if frame_sequence % 4 == 0:
                room_manager.processing_locks[match_id] = True
                frame_bytes = raw_payload.split(",")[1] if "," in raw_payload else raw_payload
                match_info = matches_db[match_id]

                verdict: RefereeVerdict = await adjudicate_frame_advanced(frame_bytes, match_info.game_title)
                room_manager.processing_locks[match_id] = False

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

                if verdict.victory_detected and match_info.status not in [MatchStatus.SETTLED, MatchStatus.ADJUDICATING]:
                    async with db_lock:
                        matches_db[match_id].status = MatchStatus.ADJUDICATING

                    winner_id = verdict.winner_identifier or "PLAYER_01"
                    payout_amount = match_info.total_pot * 0.95
                    winner_profile = match_info.players.get(winner_id)
                    payout_rail = winner_profile.rail if winner_profile else "MPESA"
                    recipient_id = winner_profile.identifier if winner_profile else "+254700000000"

                    asyncio.create_task(
                        execute_escrow_settlement(match_id, winner_id, payout_amount, payout_rail, recipient_id)
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
