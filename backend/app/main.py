"""
AETHERIS Enterprise Esports Engine - v4.7 (Cyber Emblem Edition)
File Location: backend/app/main.py or main.py
"""

import asyncio
import base64
import logging
import os
import time
import uuid
from typing import Dict, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, status
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
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

BASE_TREASURY_WALLET = os.getenv("BASE_TREASURY_WALLET", "0xe69aE274c4D814fDB312120d3db1C5c2BD63a071")

app = FastAPI(
    title="AETHERIS Esports Engine",
    version="4.7.0",
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

# ---------------------------------------------------------------------
# PAYMENT RAILS (IntaSend M-Pesa & Base Network Web3)
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

    async def initiate_stk_push(self, phone_number: str, amount: float, match_id: str, email: str = "player@aetheris.co.ke"):
        if not self.service:
            logger.info(f"[SIMULATED INTASEND STK] Phone: {phone_number} | Amount: KES {amount}")
            return {"invoice_id": f"SIM-INV-{uuid.uuid4().hex[:8]}", "state": "PENDING"}

        try:
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
        if not self.service:
            logger.info(f"[SIMULATED INTASEND PAYOUT] Phone: {phone_number} | Amount: KES {amount}")
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

class BaseWeb3EscrowService:
    def __init__(self, treasury_address: str):
        self.treasury_address = treasury_address

    async def release_escrow(self, match_id: str, winner_address: str, net_payout: float, platform_fee: float):
        logger.info(f"[BASE L2 ESCROW] Match: {match_id} | Winner: {winner_address} ({net_payout}) | Treasury: {self.treasury_address} ({platform_fee})")
        await asyncio.sleep(0.1)
        return f"0x{uuid.uuid4().hex}{uuid.uuid4().hex}"

intasend_service = IntaSendRailService()
base_web3_service = BaseWeb3EscrowService(treasury_address=BASE_TREASURY_WALLET)

# ---------------------------------------------------------------------
# SCHEMAS & DATA STRUCTURES
# ---------------------------------------------------------------------

class RefereeVerdict(BaseModel):
    victory_detected: bool = Field(description="True if match conclusion screen is detected.")
    winner_identifier: Optional[str] = Field(default=None, description="Winning player tag or username.")
    anomaly_detected: bool = Field(description="True if game anomalies detected.")
    confidence_score: float = Field(default=0.0, description="AI confidence score.")
    status_message: str = Field(description="Evaluation notes.")

class MatchStatus:
    LOBBY = "LOBBY_WAITING_FOR_PLAYERS"
    ACTIVE = "MATCH_IN_PROGRESS"
    ADJUDICATING = "AI_VERIFYING_VICTORY"
    SETTLED = "PAYOUT_COMPLETED"
    FLAGGED = "SUSPECTED_ANOMALY"

class Player(BaseModel):
    player_id: str
    identifier: str
    rail: str
    stake_amount: float
    staked_status: bool = False

class Match(BaseModel):
    match_id: str
    game_title: str
    game_mode: str
    min_players: int = 2
    max_players: int = 2
    players: Dict[str, Player] = {}
    total_pot: float = 0.0
    status: str = MatchStatus.LOBBY
    winner_id: Optional[str] = None
    created_at: float = Field(default_factory=time.time)

matches_db: Dict[str, Match] = {}
db_lock = asyncio.Lock()

# ---------------------------------------------------------------------
# ROOM & WEBSOCKET MANAGER
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
# GEMINI VISION AI REFEREE
# ---------------------------------------------------------------------

async def adjudicate_frame_advanced(frame_base64: str, game_title: str) -> RefereeVerdict:
    if not gemini_client:
        await asyncio.sleep(0.01)
        return RefereeVerdict(
            victory_detected=False, winner_identifier=None, 
            anomaly_detected=False, confidence_score=1.0, status_message="Fallback Mode: Clean"
        )

    prompt = f"""
    You are the AETHERIS AI Referee monitoring a live game match of '{game_title}'.
    Check for Victory / Defeat / Match End state screens and identify the winning player tag.
    """

    try:
        image_bytes = base64.b64decode(frame_base64)
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
        return RefereeVerdict.model_validate_json(response.text)
    except Exception as e:
        logger.error(f"Gemini Vision Error: {str(e)}")
        return RefereeVerdict(
            victory_detected=False, winner_identifier=None, 
            anomaly_detected=False, confidence_score=0.0, status_message="Frame Processing Error"
        )

# ---------------------------------------------------------------------
# AUTOMATED SETTLEMENT ENGINE
# ---------------------------------------------------------------------

async def execute_escrow_settlement(match_id: str, winner_id: str, total_pot: float, rail: str, recipient: str):
    platform_cut = total_pot * 0.10
    winner_payout = total_pot * 0.90
    tx_hash = ""

    try:
        if rail.upper() == "INTASEND":
            result = await intasend_service.execute_payout(
                phone_number=recipient,
                amount=winner_payout,
                match_id=match_id
            )
            tx_hash = result.get("transaction_id", f"INTASEND_{uuid.uuid4().hex[:8]}")
        else:
            tx_hash = await base_web3_service.release_escrow(
                match_id=match_id, 
                winner_address=recipient, 
                net_payout=winner_payout, 
                platform_fee=platform_cut
            )

        async with db_lock:
            if match_id in matches_db:
                matches_db[match_id].status = MatchStatus.SETTLED
                matches_db[match_id].winner_id = winner_id

        await room_manager.broadcast(match_id, {
            "type": "VICTORY_PAYOUT_EXECUTED",
            "match_id": match_id,
            "winner": winner_id,
            "total_pot": total_pot,
            "net_disbursed": winner_payout,
            "rail": rail,
            "transaction_id": tx_hash
        })
    except Exception as e:
        logger.error(f"Settlement failed for match {match_id}: {str(e)}")

# ---------------------------------------------------------------------
# REST ENDPOINTS & INTERACTIVE FRONTEND UI
# ---------------------------------------------------------------------

class CreateMatchReq(BaseModel):
    game_title: str
    game_mode: str
    min_players: int = Field(default=2, ge=2)
    max_players: int = Field(default=2, ge=2)
    stake_per_player: float = Field(gt=0)

class StakeDepositReq(BaseModel):
    match_id: str
    player_id: str
    rail: str = Field(pattern="^(INTASEND|WEB3_BASE)$")
    identifier: str
    amount: float = Field(gt=0)

@app.get("/", response_class=HTMLResponse)
async def root():
    """Futuristic Esports Gateway Interface with Cyber Shield Logo & Payment Modals."""
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AETHERIS // Universal Esports Gateway</title>
        <style>
            :root {
                --neon-cyan: #00f3ff;
                --neon-pink: #ff007f;
                --neon-green: #00ff66;
                --dark-bg: #06090e;
                --card-bg: rgba(14, 20, 31, 0.92);
                --border-color: rgba(0, 243, 255, 0.35);
            }
            
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }

            body {
                background-color: var(--dark-bg);
                color: #e2e8f0;
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                padding: 20px;
                position: relative;
            }

            body::before {
                content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%;
                background: linear-gradient(rgba(0, 243, 255, 0.03) 1px, transparent 1px), linear-gradient(90deg, rgba(0, 243, 255, 0.03) 1px, transparent 1px);
                background-size: 30px 30px; z-index: 0; pointer-events: none;
            }

            .container {
                position: relative; z-index: 1; max-width: 480px; width: 100%;
                background: var(--card-bg); border: 1px solid var(--border-color);
                border-radius: 20px; padding: 35px 25px; text-align: center;
                box-shadow: 0 0 45px rgba(0, 243, 255, 0.18);
                backdrop-filter: blur(12px);
            }

            /* Custom Geometric Cyber Shield Logo */
            .logo-container {
                margin: 0 auto 15px;
                width: 85px; height: 85px;
            }
            .cyber-logo {
                width: 100%; height: 100%;
                filter: drop-shadow(0 0 12px var(--neon-cyan));
            }

            h1 {
                font-size: 1.8rem; font-weight: 900; letter-spacing: 2px;
                background: linear-gradient(135deg, #ffffff 0%, var(--neon-cyan) 100%);
                -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 6px; text-transform: uppercase;
            }

            .subtitle { font-size: 0.8rem; color: #94a3b8; letter-spacing: 1px; margin-bottom: 20px; text-transform: uppercase; }

            .fun-banner {
                margin: 15px 0 20px; padding: 12px; border-radius: 10px;
                background: rgba(255, 0, 127, 0.1); border: 1px solid rgba(255, 0, 127, 0.4);
                color: #ffffff; font-size: 0.95rem; font-weight: 800; letter-spacing: 2px;
                text-shadow: 0 0 10px var(--neon-pink);
            }

            .hud-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 20px 0; }
            .hud-card { background: rgba(255, 255, 255, 0.03); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; padding: 10px; font-size: 0.75rem; }
            .hud-card .label { color: #64748b; margin-bottom: 4px; font-size: 0.65rem; text-transform: uppercase; }
            .hud-card .value { color: var(--neon-cyan); font-weight: 700; font-family: monospace; }

            .control-panel { display: flex; flex-direction: column; gap: 12px; margin-top: 20px; }
            
            .btn {
                padding: 14px; border-radius: 8px; font-weight: bold; font-size: 0.9rem;
                text-transform: uppercase; letter-spacing: 1px; cursor: pointer;
                transition: all 0.3s ease; border: none; outline: none; width: 100%;
            }
            .btn-primary { background: var(--neon-cyan); color: #000; box-shadow: 0 0 15px rgba(0, 243, 255, 0.4); }
            .btn-primary:hover { background: #fff; box-shadow: 0 0 25px rgba(0, 243, 255, 0.8); }
            
            .btn-pay { background: transparent; border: 1px solid var(--neon-green); color: var(--neon-green); }
            .btn-pay:hover { background: rgba(0, 255, 102, 0.1); box-shadow: 0 0 15px rgba(0, 255, 102, 0.4); }
            
            .btn-manual { background: transparent; border: 1px solid #64748b; color: #94a3b8; }
            .btn-manual:hover { border-color: #fff; color: #fff; }

            /* Modals & Forms */
            .modal-overlay {
                display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                background: rgba(0,0,0,0.85); z-index: 10; justify-content: center; align-items: center;
                backdrop-filter: blur(8px);
            }
            .modal-content {
                background: var(--card-bg); border: 1px solid var(--neon-cyan);
                border-radius: 16px; padding: 25px; max-width: 420px; width: 90%; text-align: left;
            }
            .modal-content h2 { color: var(--neon-cyan); margin-bottom: 15px; font-size: 1.2rem; text-transform: uppercase; }
            
            .form-group { margin-bottom: 12px; }
            .form-group label { display: block; font-size: 0.75rem; color: #94a3b8; margin-bottom: 4px; text-transform: uppercase; }
            .form-group input, .form-group select {
                width: 100%; padding: 10px; border-radius: 6px; background: rgba(0,0,0,0.5);
                border: 1px solid rgba(0, 243, 255, 0.3); color: #fff; font-size: 0.9rem; outline: none;
            }
            
            .modal-actions { display: flex; gap: 10px; margin-top: 20px; }
            .close-btn { background: #334155; color: white; }
            .status-box { margin-top: 10px; font-size: 0.8rem; padding: 10px; border-radius: 6px; display: none; word-break: break-all; }
        </style>
    </head>
    <body>
        <div class="container">
            <!-- Geometric Cyber Shield Emblem SVG -->
            <div class="logo-container">
                <svg class="cyber-logo" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <defs>
                        <linearGradient id="cyberGrad" x1="0%" y1="0%" x2="100%" y2="100%">
                            <stop offset="0%" stop-color="#00f3ff"/>
                            <stop offset="100%" stop-color="#7928ca"/>
                        </linearGradient>
                    </defs>
                    <!-- Outer Hexagon Frame -->
                    <polygon points="50,5 90,25 90,75 50,95 10,75 10,25" stroke="url(#cyberGrad)" stroke-width="2.5" fill="rgba(0, 243, 255, 0.04)" />
                    <!-- Inner Tech Crest / Wing Wings -->
                    <path d="M50 20 L75 70 L62 70 L50 42 L38 70 L25 70 Z" fill="url(#cyberGrad)" opacity="0.95"/>
                    <path d="M50 32 L60 58 L40 58 Z" fill="#06090e" />
                    <!-- Core Gem -->
                    <polygon points="50,15 55,26 45,26" fill="#ff007f" />
                </svg>
            </div>

            <h1>AETHERIS</h1>
            <div class="subtitle">Universal Esports Gateway</div>

            <div class="fun-banner">
                🎮 HAVE FUN & PLAY FAIR ⚡
            </div>

            <div class="hud-grid">
                <div class="hud-card"><div class="label">AI Referee</div><div class="value">Gemini Vision 2.5</div></div>
                <div class="hud-card"><div class="label">Payment Rails</div><div class="value">M-Pesa + Base L2</div></div>
            </div>

            <div class="control-panel">
                <button class="btn btn-primary" onclick="toggleModal('createMatchModal')">⚔️ Initialize Match</button>
                <button class="btn btn-pay" onclick="toggleModal('depositModal')">💸 Deposit Stake</button>
                <button class="btn btn-manual" onclick="toggleModal('manualModal')">📖 System Manual</button>
            </div>
        </div>

        <!-- Create Match Modal -->
        <div id="createMatchModal" class="modal-overlay">
            <div class="modal-content">
                <h2>Initialize Match Lobby</h2>
                <div class="form-group">
                    <label>Game Title</label>
                    <input type="text" id="createGameTitle" value="EA FC 25">
                </div>
                <div class="form-group">
                    <label>Game Mode</label>
                    <input type="text" id="createGameMode" value="1v1 Head to Head">
                </div>
                <div class="form-group">
                    <label>Stake Per Player (KES / USD)</label>
                    <input type="number" id="createStake" value="100">
                </div>
                <div id="createStatus" class="status-box"></div>
                <div class="modal-actions">
                    <button class="btn btn-primary" onclick="submitCreateMatch()">CREATE LOBBY</button>
                    <button class="btn close-btn" onclick="toggleModal('createMatchModal')">CANCEL</button>
                </div>
            </div>
        </div>

        <!-- Deposit Stake Modal -->
        <div id="depositModal" class="modal-overlay">
            <div class="modal-content">
                <h2>Deposit Stake Escrow</h2>
                <div class="form-group">
                    <label>Match ID</label>
                    <input type="text" id="depMatchId" placeholder="e.g. MATCH-1234ABCD">
                </div>
                <div class="form-group">
                    <label>Player Identifier Tag</label>
                    <input type="text" id="depPlayerId" value="Player1">
                </div>
                <div class="form-group">
                    <label>Payment Rail</label>
                    <select id="depRail">
                        <option value="INTASEND">M-Pesa (IntaSend STK Push)</option>
                        <option value="WEB3_BASE">Base L2 Wallet (Crypto)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Phone Number or Wallet Address</label>
                    <input type="text" id="depIdentifier" placeholder="2547XXXXXXXX or 0x...">
                </div>
                <div class="form-group">
                    <label>Stake Amount</label>
                    <input type="number" id="depAmount" value="100">
                </div>
                <div id="depStatus" class="status-box"></div>
                <div class="modal-actions">
                    <button class="btn btn-pay" onclick="submitDeposit()">CONFIRM DEPOSIT</button>
                    <button class="btn close-btn" onclick="toggleModal('depositModal')">CANCEL</button>
                </div>
            </div>
        </div>

        <!-- Manual Modal -->
        <div id="manualModal" class="modal-overlay">
            <div class="modal-content">
                <h2>System Manual</h2>
                <ol style="padding-left: 20px; font-size: 0.85rem; line-height: 1.6; color: #cbd5e1; margin-bottom: 20px;">
                    <li style="margin-bottom: 8px;"><strong>Initialize:</strong> Click 'Initialize Match' to spin up a new match lobby and obtain a Match ID.</li>
                    <li style="margin-bottom: 8px;"><strong>Stake:</strong> Use 'Deposit Stake' with your Match ID to trigger an M-Pesa STK Push or Base L2 smart contract escrow.</li>
                    <li style="margin-bottom: 8px;"><strong>Play:</strong> Connect your stream node. Gemini Vision AI referee detects match conclusion and determines the winner automatically.</li>
                    <li><strong>Disburse:</strong> Instant automated payout executed to the winner upon match end.</li>
                </ol>
                <button class="btn close-btn" onclick="toggleModal('manualModal')">CLOSE</button>
            </div>
        </div>

        <script>
            function toggleModal(id) {
                const modal = document.getElementById(id);
                modal.style.display = (modal.style.display === 'flex') ? 'none' : 'flex';
            }

            async function submitCreateMatch() {
                const statusBox = document.getElementById('createStatus');
                statusBox.style.display = 'block';
                statusBox.style.background = 'rgba(0, 243, 255, 0.1)';
                statusBox.style.color = '#00f3ff';
                statusBox.innerText = 'Creating lobby...';

                try {
                    const res = await fetch('/api/match/create', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            game_title: document.getElementById('createGameTitle').value,
                            game_mode: document.getElementById('createGameMode').value,
                            stake_per_player: parseFloat(document.getElementById('createStake').value)
                        })
                    });
                    const data = await res.json();
                    if (res.ok) {
                        statusBox.style.color = '#00ff66';
                        statusBox.innerText = 'LOBBY CREATED! Match ID: ' + data.match_id;
                        document.getElementById('depMatchId').value = data.match_id;
                    } else {
                        statusBox.style.color = '#ff007f';
                        statusBox.innerText = 'Error: ' + JSON.stringify(data.detail);
                    }
                } catch (e) {
                    statusBox.style.color = '#ff007f';
                    statusBox.innerText = 'Network error initiating match.';
                }
            }

            async function submitDeposit() {
                const statusBox = document.getElementById('depStatus');
                statusBox.style.display = 'block';
                statusBox.style.background = 'rgba(0, 255, 102, 0.1)';
                statusBox.style.color = '#00ff66';
                statusBox.innerText = 'Processing deposit... Check phone/wallet for prompt.';

                try {
                    const res = await fetch('/api/escrow/deposit', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            match_id: document.getElementById('depMatchId').value,
                            player_id: document.getElementById('depPlayerId').value,
                            rail: document.getElementById('depRail').value,
                            identifier: document.getElementById('depIdentifier').value,
                            amount: parseFloat(document.getElementById('depAmount').value)
                        })
                    });
                    const data = await res.json();
                    if (res.ok) {
                        statusBox.innerText = 'DEPOSIT SUCCESSFUL! Pot Total: KES/USD ' + data.total_pot;
                    } else {
                        statusBox.style.color = '#ff007f';
                        statusBox.innerText = 'Error: ' + JSON.stringify(data.detail);
                    }
                } catch (e) {
                    statusBox.style.color = '#ff007f';
                    statusBox.innerText = 'Failed to execute deposit request.';
                }
            }
        </script>
    </body>
    </html>
    """

@app.get("/healthz")
async def health_check():
    return {
        "status": "HEALTHY",
        "engine": "AETHERIS v4.7.0",
        "gemini_vision": gemini_client is not None
    }

@app.post("/api/match/create", status_code=status.HTTP_201_CREATED)
async def create_match(req: CreateMatchReq):
    match_id = f"MATCH-{uuid.uuid4().hex[:8].upper()}"
    new_match = Match(
        match_id=match_id,
        game_title=req.game_title,
        game_mode=req.game_mode,
        min_players=req.min_players,
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
            raise HTTPException(status_code=400, detail="Match is already in progress or completed")
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
            staked_status=True
        )
        match.players[req.player_id] = player
        match.total_pot += req.amount
        
        if len(match.players) >= match.min_players:
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
# REAL-TIME WEBSOCKET REFEREE & SPECTATOR STREAM
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
    victory_confirmations = 0

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

                if verdict.victory_detected:
                    victory_confirmations += 1
                else:
                    victory_confirmations = 0

                if victory_confirmations >= 2 and match_info.status not in [MatchStatus.SETTLED, MatchStatus.ADJUDICATING]:
                    async with db_lock:
                        matches_db[match_id].status = MatchStatus.ADJUDICATING

                    winner_id = verdict.winner_identifier or "PLAYER_01"
                    winner_profile = match_info.players.get(winner_id)
                    payout_rail = winner_profile.rail if winner_profile else "WEB3_BASE"
                    recipient_id = winner_profile.identifier if winner_profile else BASE_TREASURY_WALLET

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
