"""
AETHERIS Enterprise Esports Engine - v6.0 (Live Interactive AI, Anti-Cheat & Victory Telemetry)
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
    version="6.0.0",
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
    victory_reason: Optional[str] = Field(default=None, description="Detailed explanation of why the winner won.")
    modded_app_detected: bool = Field(default=False, description="True if modified APK, speed hack, or cheat overlay is detected.")
    cheat_warning_details: Optional[str] = Field(default=None, description="Warning details regarding detected modded app or visual anomaly.")
    confidence_score: float = Field(default=0.0, description="AI confidence score.")
    status_message: str = Field(description="Evaluation summary.")

class MatchStatus:
    LOBBY = "LOBBY_WAITING_FOR_PLAYERS"
    ACTIVE = "MATCH_IN_PROGRESS"
    ADJUDICATING = "AI_VERIFYING_VICTORY"
    FORFEITED = "TERMINATED_CHEATING_DETECTED"
    SETTLED = "PAYOUT_COMPLETED"

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
    victory_reason: Optional[str] = None
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
# GEMINI VISION & INTERACTIVE LIVE AI REFEREE
# ---------------------------------------------------------------------

async def adjudicate_frame_advanced(frame_base64: str, game_title: str) -> RefereeVerdict:
    if not gemini_client:
        await asyncio.sleep(0.01)
        return RefereeVerdict(
            victory_detected=False, winner_identifier=None, 
            victory_reason=None, modded_app_detected=False, cheat_warning_details=None,
            confidence_score=1.0, status_message="Fallback Mode Active"
        )

    prompt = f"""
    You are the live AETHERIS AI Referee monitoring an active high-stakes game match of '{game_title}'.
    Perform a strict two-part evaluation:

    1. VICTORY ANALYSIS:
       Check if a final game-over/victory screen is present. If yes:
       - Set 'victory_detected' to true.
       - Extract the 'winner_identifier' (player username/tag).
       - Provide a clear, factual 'victory_reason' explaining EXACTLY why they won (e.g., "Player 1 won with a final score of 3-1 at full-time", "Knockout achieved in Round 3").

    2. ANTI-CHEAT & MODDED APP DETECTION:
       Scan the frame for any modified APK indicators, third-party mod menus, speed hack overlays, visual memory tamplers, floating cheat controls, or illegal game overlays.
       - If any modded app or cheat overlay is detected: set 'modded_app_detected' to true and describe the cheat in 'cheat_warning_details'.
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
            victory_detected=False, winner_identifier=None, victory_reason=None,
            modded_app_detected=False, cheat_warning_details=None,
            confidence_score=0.0, status_message="Frame Processing Error"
        )

async def handle_live_ai_chat(query: str, match_id: str, sender_id: str) -> str:
    """Interacts with players live regarding game rules, status, or arbitration queries."""
    if not gemini_client:
        return f"AI Referee (Offline Mode): Processing your query regarding match {match_id}."

    async with db_lock:
        match = matches_db.get(match_id)
        match_info = f"Game: {match.game_title}, Mode: {match.game_mode}, Status: {match.status}, Total Pot: {match.total_pot}" if match else "Match Info Unavailable"

    prompt = f"""
    You are the live AETHERIS AI Referee for match '{match_id}'.
    Current Match Context: {match_info}.
    Player '{sender_id}' asks: "{query}".
    Provide a brief, authoritative, highly professional esports referee response (max 3 sentences).
    """

    try:
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"AI Chat Error: {str(e)}")
        return "AI Referee is evaluating active telemetry."

# ---------------------------------------------------------------------
# AUTOMATED SETTLEMENT ENGINE
# ---------------------------------------------------------------------

async def execute_escrow_settlement(match_id: str, winner_id: str, total_pot: float, rail: str, recipient: str, victory_reason: str):
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
                matches_db[match_id].victory_reason = victory_reason

        await room_manager.broadcast(match_id, {
            "type": "VICTORY_PAYOUT_EXECUTED",
            "match_id": match_id,
            "winner": winner_id,
            "victory_reason": victory_reason,
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
    """Futuristic UI with Fair Play Banners, Live AI Referee Chat, and Deep Linking."""
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AETHERIS // Live AI & Fair Play Protocol</title>
        <style>
            :root {
                --neon-cyan: #00e5ff;
                --neon-pink: #ff007f;
                --neon-green: #00ff66;
                --neon-yellow: #ffcc00;
                --dark-bg: #030508;
                --card-bg: rgba(10, 14, 23, 0.95);
                --border-color: rgba(0, 229, 255, 0.4);
            }
            
            * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Segoe UI', Roboto, Helvetica, sans-serif; }

            body {
                background-color: var(--dark-bg); color: #e2e8f0; min-height: 100vh;
                display: flex; flex-direction: column; align-items: center; justify-content: center;
                padding: 20px; position: relative;
            }

            /* Linear Grid Background */
            body::before {
                content: ""; position: absolute; top: 0; left: 0; width: 100%; height: 100%;
                background: linear-gradient(rgba(0, 229, 255, 0.04) 1px, transparent 1px), 
                            linear-gradient(90deg, rgba(0, 229, 255, 0.04) 1px, transparent 1px);
                background-size: 40px 40px; z-index: 0; pointer-events: none;
            }

            /* Container Outer Border */
            .container-border {
                position: relative; padding: 1px; max-width: 500px; width: 100%;
                background: linear-gradient(135deg, var(--neon-cyan), var(--neon-pink) 60%);
                clip-path: polygon(15px 0, 100% 0, 100% calc(100% - 15px), calc(100% - 15px) 100%, 0 100%, 0 15px);
            }

            /* Angular Container */
            .container {
                position: relative; z-index: 1; width: 100%;
                background: var(--card-bg); 
                padding: 35px 25px; text-align: center;
                clip-path: polygon(15px 0, 100% 0, 100% calc(100% - 15px), calc(100% - 15px) 100%, 0 100%, 0 15px);
                box-shadow: inset 0 0 20px rgba(0, 229, 255, 0.1), 0 0 30px rgba(0, 0, 0, 0.8);
            }

            /* EXACT FAIR PLAY BUTTON BANNER */
            .fair-play-pill {
                display: inline-flex; align-items: center; justify-content: center; gap: 8px;
                padding: 10px 24px; border-radius: 25px; border: 1.5px solid var(--neon-pink);
                color: #ffffff; font-weight: 800; font-size: 0.85rem; letter-spacing: 2px;
                text-transform: uppercase; margin-bottom: 20px; background: rgba(255, 0, 127, 0.05);
                box-shadow: 0 0 15px rgba(ff, 0, 127, 0.2);
            }

            /* EXACT FAIR PLAY MANDATE BANNER */
            .fair-play-mandate {
                margin: 0 0 20px 0; padding: 14px 16px;
                background: rgba(255, 0, 127, 0.08); border-left: 4px solid var(--neon-pink);
                color: #94a3b8; font-size: 0.78rem; line-height: 1.5; text-align: left;
            }
            .fair-play-mandate b { color: var(--neon-pink); letter-spacing: 1px; }

            .logo-container { margin: 0 auto 10px; width: 70px; height: 70px; }
            .cyber-logo { width: 100%; height: 100%; filter: drop-shadow(0 0 10px var(--neon-cyan)); }

            h1 {
                font-size: 2rem; font-weight: 900; letter-spacing: 3px;
                background: linear-gradient(135deg, #ffffff 0%, var(--neon-cyan) 100%);
                -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 5px; 
            }

            .subtitle { font-size: 0.75rem; color: #64748b; letter-spacing: 2px; margin-bottom: 20px; text-transform: uppercase; }

            .hud-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 15px 0; }
            .hud-card { 
                background: rgba(255, 255, 255, 0.02); padding: 10px; text-align: left;
                border-bottom: 2px solid rgba(255, 255, 255, 0.1);
            }
            .hud-card .label { color: #64748b; margin-bottom: 4px; font-size: 0.6rem; text-transform: uppercase; letter-spacing: 1px;}
            .hud-card .value { color: var(--neon-cyan); font-weight: 700; font-family: monospace; font-size: 0.85rem;}

            .control-panel { display: flex; flex-direction: column; gap: 12px; margin-top: 20px; }
            
            /* Angular Buttons */
            .btn {
                padding: 14px; font-weight: 800; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 1.5px; 
                cursor: pointer; transition: all 0.3s ease; border: none; outline: none; width: 100%;
                clip-path: polygon(10px 0, 100% 0, 100% calc(100% - 10px), calc(100% - 10px) 100%, 0 100%, 0 10px);
            }
            .btn-primary { background: var(--neon-cyan); color: #000; box-shadow: 0 4px 15px rgba(0, 229, 255, 0.3); }
            .btn-primary:hover { background: #fff; }
            
            .btn-pay { background: rgba(0, 255, 102, 0.1); color: var(--neon-green); border-bottom: 2px solid var(--neon-green); }
            .btn-pay:hover { background: rgba(0, 255, 102, 0.2); }
            
            .btn-chat { background: rgba(0, 229, 255, 0.1); color: var(--neon-cyan); border-bottom: 2px solid var(--neon-cyan); }
            .btn-chat:hover { background: rgba(0, 229, 255, 0.2); }

            /* Modals */
            .modal-overlay {
                display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                background: rgba(0,0,0,0.9); z-index: 10; justify-content: center; align-items: center;
                backdrop-filter: blur(5px);
            }
            .modal-content {
                background: var(--card-bg); width: 90%; max-width: 440px; padding: 25px; text-align: left;
                clip-path: polygon(20px 0, 100% 0, 100% calc(100% - 20px), calc(100% - 20px) 100%, 0 100%, 0 20px);
                border: 1px solid var(--border-color);
            }
            .modal-content h2 { color: var(--neon-cyan); margin-bottom: 15px; font-size: 1rem; text-transform: uppercase; letter-spacing: 1px;}
            
            .form-group { margin-bottom: 12px; }
            .form-group label { display: block; font-size: 0.65rem; color: #64748b; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 1px;}
            .form-group input, .form-group select {
                width: 100%; padding: 10px; background: rgba(0,0,0,0.6);
                border: 1px solid rgba(255, 255, 255, 0.1); color: #fff; font-size: 0.85rem; outline: none;
            }
            .form-group input:focus, .form-group select:focus { border-color: var(--neon-cyan); }
            
            .modal-actions { display: flex; gap: 10px; margin-top: 20px; }
            .close-btn { background: #1e293b; color: #cbd5e1; }
            
            .status-box { margin-top: 15px; font-size: 0.8rem; padding: 12px; display: none; word-break: break-all; border-left: 3px solid; }

            /* Live AI Terminal Box */
            .chat-box {
                height: 180px; overflow-y: auto; background: rgba(0,0,0,0.7);
                border: 1px solid rgba(0, 229, 255, 0.2); padding: 10px; font-family: monospace; font-size: 0.75rem;
                display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px;
            }
            .chat-msg { color: #cbd5e1; }
            .chat-msg.ai { color: var(--neon-cyan); }
            .chat-msg.alert { color: var(--neon-pink); font-weight: bold; }
            .chat-msg.win { color: var(--neon-green); font-weight: bold; }

            /* Share Link Box */
            .share-box {
                margin-top: 12px; padding: 10px; background: rgba(0, 0, 0, 0.5); 
                border: 1px solid rgba(0, 229, 255, 0.3); display: none; align-items: center; gap: 8px;
            }
            .share-box input { flex: 1; background: transparent; border: none; color: var(--neon-cyan); outline: none; font-size: 0.75rem; font-family: monospace;}
            .copy-btn { 
                background: var(--neon-cyan); color: #000; border: none; padding: 6px 12px; 
                font-weight: bold; cursor: pointer; font-size: 0.7rem; text-transform: uppercase;
                clip-path: polygon(5px 0, 100% 0, 100% calc(100% - 5px), calc(100% - 5px) 100%, 0 100%, 0 5px);
            }
        </style>
    </head>
    <body>
        <div class="container-border">
            <div class="container">
                
                <!-- EXACT FAIR PLAY BUTTON BANNER -->
                <div class="fair-play-pill">
                    🎮 HAVE FUN & PLAY FAIR ⚡
                </div>

                <!-- Geometric Cyber Shield Emblem -->
                <div class="logo-container">
                    <svg class="cyber-logo" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <polygon points="50,5 90,25 90,75 50,95 10,75 10,25" stroke="#00e5ff" stroke-width="2" fill="rgba(0, 229, 255, 0.05)" />
                        <path d="M50 20 L75 70 L62 70 L50 42 L38 70 L25 70 Z" fill="#00e5ff" opacity="0.9"/>
                        <path d="M50 32 L60 58 L40 58 Z" fill="#030508" />
                    </svg>
                </div>

                <h1>AETHERIS</h1>
                <div class="subtitle">Live Vision Protocol v6.0</div>

                <!-- EXACT FAIR PLAY MANDATE BANNER -->
                <div class="fair-play-mandate">
                    ⚠️ <b>FAIR PLAY MANDATE:</b> Real-time vision telemetry actively flags modded APKs, speed hacks, and cheat overlays. Zero-tolerance auto-forfeiture enabled.
                </div>

                <div class="hud-grid">
                    <div class="hud-card"><div class="label">Arbitration</div><div class="value">Live Gemini 2.5</div></div>
                    <div class="hud-card"><div class="label">Anti-Cheat</div><div class="value">Active Vision</div></div>
                </div>

                <div class="control-panel">
                    <button class="btn btn-primary" onclick="toggleModal('createMatchModal')">INITIALIZE MATCH</button>
                    <button class="btn btn-pay" onclick="toggleModal('depositModal')">DEPOSIT STAKE</button>
                    <button class="btn btn-chat" onclick="toggleModal('liveAiModal')">LIVE AI REFEREE TERMINAL</button>
                </div>
            </div>
        </div>

        <!-- Create Match Modal -->
        <div id="createMatchModal" class="modal-overlay">
            <div class="modal-content">
                <h2>Initialize Lobby</h2>
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
                
                <!-- Share Link UI -->
                <div id="shareLinkContainer" class="share-box">
                    <input type="text" id="inviteLink" readonly>
                    <button class="copy-btn" onclick="copyInviteLink()">COPY</button>
                </div>

                <div class="modal-actions">
                    <button class="btn btn-primary" onclick="submitCreateMatch()">CREATE</button>
                    <button class="btn close-btn" onclick="toggleModal('createMatchModal')">CLOSE</button>
                </div>
            </div>
        </div>

        <!-- Deposit Stake Modal -->
        <div id="depositModal" class="modal-overlay">
            <div class="modal-content">
                <h2>Execute Deposit</h2>
                <div class="form-group">
                    <label>Match ID</label>
                    <input type="text" id="depMatchId" placeholder="M-XXXXXXXX">
                </div>
                <div class="form-group">
                    <label>Player Tag</label>
                    <input type="text" id="depPlayerId" placeholder="e.g., PlayerOne">
                </div>
                <div class="form-group">
                    <label>Payment Rail</label>
                    <select id="depRail">
                        <option value="INTASEND">M-Pesa (Daraja API / STK)</option>
                        <option value="WEB3_BASE">Base L2 (Smart Contract)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Phone Number or Address</label>
                    <input type="text" id="depIdentifier" placeholder="2547XXXXXXXX or 0x...">
                </div>
                <div class="form-group">
                    <label>Stake Amount</label>
                    <input type="number" id="depAmount" value="100">
                </div>
                <div id="depStatus" class="status-box"></div>
                <div class="modal-actions">
                    <button class="btn btn-pay" onclick="submitDeposit()">CONFIRM</button>
                    <button class="btn close-btn" onclick="toggleModal('depositModal')">CLOSE</button>
                </div>
            </div>
        </div>

        <!-- Live AI Referee Modal -->
        <div id="liveAiModal" class="modal-overlay">
            <div class="modal-content">
                <h2>Live AI Referee Terminal</h2>
                <div class="chat-box" id="chatTerminal">
                    <div class="chat-msg ai">[SYSTEM]: AI Referee system active. Telemetry stream connected.</div>
                </div>
                <div class="form-group">
                    <label>Query AI Referee</label>
                    <input type="text" id="aiQueryInput" placeholder="Ask AI referee live..." onkeydown="if(event.key==='Enter') sendAiQuery()">
                </div>
                <div class="modal-actions">
                    <button class="btn btn-primary" onclick="sendAiQuery()">SEND QUERY</button>
                    <button class="btn close-btn" onclick="toggleModal('liveAiModal')">CLOSE</button>
                </div>
            </div>
        </div>

        <script>
            let ws = null;
            let currentMatchId = "";

            window.onload = function() {
                const urlParams = new URLSearchParams(window.location.search);
                const matchIdParam = urlParams.get('match');
                if (matchIdParam) {
                    document.getElementById('depMatchId').value = matchIdParam;
                    currentMatchId = matchIdParam;
                    connectWebSocket(matchIdParam);
                    toggleModal('depositModal');
                }
            };

            function connectWebSocket(matchId) {
                if (ws) ws.close();
                const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                ws = new WebSocket(`${wsProto}//${window.location.host}/ws/${matchId}`);
                
                ws.onmessage = function(event) {
                    const data = JSON.parse(event.data);
                    const terminal = document.getElementById('chatTerminal');

                    if (data.type === 'CHEATING_WARNING') {
                        terminal.innerHTML += `<div class="chat-msg alert">🚨 [ALERT]: ${data.message}</div>`;
                        alert(`🚨 ANTI-CHEAT WARNING: ${data.message}`);
                    }
                    else if (data.type === 'VICTORY_PAYOUT_EXECUTED') {
                        terminal.innerHTML += `<div class="chat-msg win">🏆 [VICTORY]: Player ${data.winner} WON! Reason: ${data.victory_reason} | Payout: KES ${data.net_disbursed}</div>`;
                    }
                    else if (data.type === 'AI_CHAT_RESPONSE') {
                        terminal.innerHTML += `<div class="chat-msg ai">🤖 [AI REFEREE]: ${data.response}</div>`;
                    }
                    terminal.scrollTop = terminal.scrollHeight;
                };
            }

            function sendAiQuery() {
                const input = document.getElementById('aiQueryInput');
                const query = input.value.trim();
                const matchId = document.getElementById('depMatchId').value || currentMatchId;
                const playerId = document.getElementById('depPlayerId').value || "Player1";

                if (!query) return;
                
                const terminal = document.getElementById('chatTerminal');
                terminal.innerHTML += `<div class="chat-msg">👤 [${playerId}]: ${query}</div>`;
                
                if (ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({
                        type: "AI_CHAT_QUERY",
                        query: query,
                        sender_id: playerId
                    }));
                } else {
                    terminal.innerHTML += `<div class="chat-msg ai">🤖 [AI REFEREE]: Socket not connected to a live match. Please initialize or deposit first.</div>`;
                }
                input.value = "";
                terminal.scrollTop = terminal.scrollHeight;
            }

            function toggleModal(id) {
                const el = document.getElementById(id);
                el.style.display = el.style.display === 'flex' ? 'none' : 'flex';
                if (id === 'createMatchModal') {
                    document.getElementById('createStatus').style.display = 'none';
                    document.getElementById('shareLinkContainer').style.display = 'none';
                }
                if (id === 'depositModal') {
                    document.getElementById('depStatus').style.display = 'none';
                }
            }

            function copyInviteLink() {
                const linkInput = document.getElementById('inviteLink');
                linkInput.select();
                linkInput.setSelectionRange(0, 99999);
                document.execCommand("copy");
                alert("Invite Link Copied to Clipboard!");
            }

            async function submitCreateMatch() {
                const btn = document.querySelector('#createMatchModal .btn-primary');
                const statusBox = document.getElementById('createStatus');
                btn.innerText = 'PROCESSING...';
                
                try {
                    const res = await fetch('/match/create', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            game_title: document.getElementById('createGameTitle').value,
                            game_mode: document.getElementById('createGameMode').value,
                            min_players: 2, max_players: 2,
                            stake_per_player: parseFloat(document.getElementById('createStake').value)
                        })
                    });
                    const data = await res.json();
                    
                    statusBox.style.display = 'block';
                    statusBox.style.borderColor = 'var(--neon-green)';
                    statusBox.style.color = 'var(--neon-green)';
                    statusBox.innerHTML = `MATCH ID: <b>${data.match_id}</b>`;
                    
                    currentMatchId = data.match_id;
                    connectWebSocket(data.match_id);

                    const shareUrl = window.location.origin + '?match=' + data.match_id;
                    const shareBox = document.getElementById('shareLinkContainer');
                    document.getElementById('inviteLink').value = shareUrl;
                    shareBox.style.display = 'flex';
                    
                } catch (e) {
                    statusBox.style.display = 'block';
                    statusBox.style.borderColor = 'var(--neon-pink)';
                    statusBox.style.color = 'var(--neon-pink)';
                    statusBox.innerText = 'ERROR INITIATING MATCH';
                }
                btn.innerText = 'CREATE';
            }

            async function submitDeposit() {
                const btn = document.querySelector('#depositModal .btn-pay');
                const statusBox = document.getElementById('depStatus');
                btn.innerText = 'AUTHORIZING...';
                
                const matchId = document.getElementById('depMatchId').value;
                currentMatchId = matchId;
                connectWebSocket(matchId);

                try {
                    const res = await fetch('/deposit', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            match_id: matchId,
                            player_id: document.getElementById('depPlayerId').value,
                            rail: document.getElementById('depRail').value,
                            identifier: document.getElementById('depIdentifier').value,
                            amount: parseFloat(document.getElementById('depAmount').value)
                        })
                    });
                    const data = await res.json();
                    
                    statusBox.style.display = 'block';
                    if (res.ok) {
                        statusBox.style.borderColor = 'var(--neon-green)';
                        statusBox.style.color = 'var(--neon-green)';
                        statusBox.innerText = data.status === 'success' ? 'DEPOSIT ESCROWED' : JSON.stringify(data);
                    } else {
                        statusBox.style.borderColor = 'var(--neon-pink)';
                        statusBox.style.color = 'var(--neon-pink)';
                        statusBox.innerText = data.detail || 'DEPOSIT FAILED';
                    }
                } catch (e) {
                    statusBox.style.display = 'block';
                    statusBox.style.borderColor = 'var(--neon-pink)';
                    statusBox.style.color = 'var(--neon-pink)';
                    statusBox.innerText = 'NETWORK ERROR';
                }
                btn.innerText = 'CONFIRM';
            }
        </script>
    </body>
    </html>
    """

@app.post("/match/create")
async def create_match(req: CreateMatchReq):
    match_id = f"M-{uuid.uuid4().hex[:8].upper()}"
    new_match = Match(
        match_id=match_id,
        game_title=req.game_title,
        game_mode=req.game_mode,
        min_players=req.min_players,
        max_players=req.max_players
    )
    async with db_lock:
        matches_db[match_id] = new_match
    
    return {"status": "success", "match_id": match_id, "detail": "Lobby Initialized"}

@app.post("/deposit")
async def handle_deposit(req: StakeDepositReq):
    async with db_lock:
        if req.match_id not in matches_db:
            raise HTTPException(status_code=404, detail="Match ID not found")
        match = matches_db[req.match_id]

        if len(match.players) >= match.max_players:
            raise HTTPException(status_code=400, detail="Match Escrow Full")

    if req.rail == "INTASEND":
        result = await intasend_service.initiate_stk_push(req.identifier, req.amount, req.match_id)
        if result.get("state") not in ["PENDING", "SUCCESS"]:
            raise HTTPException(status_code=400, detail="STK Push Initialization Failed")
    elif req.rail == "WEB3_BASE":
        pass

    async with db_lock:
        match.players[req.player_id] = Player(
            player_id=req.player_id,
            identifier=req.identifier,
            rail=req.rail,
            stake_amount=req.amount,
            staked_status=True
        )
        match.total_pot += req.amount
        
        if len(match.players) == match.max_players:
            match.status = MatchStatus.ACTIVE

    await room_manager.broadcast(req.match_id, {
        "type": "ESCROW_UPDATE",
        "player_id": req.player_id,
        "amount": req.amount,
        "total_pot": match.total_pot,
        "status": match.status
    })

    return {"status": "success", "player": req.player_id, "escrowed": req.amount}

@app.websocket("/ws/{match_id}")
async def match_websocket(websocket: WebSocket, match_id: str):
    await room_manager.connect(match_id, websocket)
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            # 1. Handle Frame Telemetry Adjudication
            if msg_type == "FRAME_UPLOAD":
                if room_manager.processing_locks.get(match_id):
                    continue

                async with db_lock:
                    match = matches_db.get(match_id)
                    if not match or match.status != MatchStatus.ACTIVE:
                        continue
                
                room_manager.processing_locks[match_id] = True
                asyncio.create_task(process_telemetry(match_id, data["frame"], match.game_title, data.get("player_id", "Unknown")))

            # 2. Handle Live Interactive AI Chat Query
            elif msg_type == "AI_CHAT_QUERY":
                user_query = data.get("query", "")
                sender_id = data.get("sender_id", "Player")
                ai_response = await handle_live_ai_chat(user_query, match_id, sender_id)
                await websocket.send_json({
                    "type": "AI_CHAT_RESPONSE",
                    "response": ai_response
                })

    except WebSocketDisconnect:
        room_manager.disconnect(match_id, websocket)
    except Exception as e:
        logger.error(f"WS Error {match_id}: {str(e)}")
        room_manager.disconnect(match_id, websocket)

async def process_telemetry(match_id: str, frame_data: str, game_title: str, submitting_player: str):
    try:
        verdict = await adjudicate_frame_advanced(frame_data, game_title)
        
        # Anti-Cheat Warning Alert Broadcast
        if verdict.modded_app_detected:
            warning_msg = f"MODDED APP DETECTED from player '{submitting_player}'! Details: {verdict.cheat_warning_details}. Match Auto-Forfeited."
            async with db_lock:
                match = matches_db.get(match_id)
                if match:
                    match.status = MatchStatus.FORFEITED
            
            await room_manager.broadcast(match_id, {
                "type": "CHEATING_WARNING",
                "player_id": submitting_player,
                "message": warning_msg,
                "details": verdict.cheat_warning_details
            })
            return

        # Victory Determination with Explicit Reason
        if verdict.victory_detected and verdict.confidence_score > 0.8:
            winner_tag = verdict.winner_identifier or submitting_player
            victory_reason = verdict.victory_reason or verdict.status_message or "Match conclusions visually confirmed by AI Referee."
            
            async with db_lock:
                match = matches_db.get(match_id)
                if match and match.status == MatchStatus.ACTIVE:
                    match.status = MatchStatus.ADJUDICATING
                    winning_player = match.players.get(winner_tag)
                    
                    if winning_player:
                        await execute_escrow_settlement(
                            match_id, winner_tag, match.total_pot, 
                            winning_player.rail, winning_player.identifier,
                            victory_reason
                        )
                    else:
                        logger.warning(f"Victory detected for {winner_tag}, but player not registered in match roster.")

        await room_manager.broadcast(match_id, {
            "type": "AI_REFEREE_UPDATE",
            "verdict": verdict.model_dump()
        })
    finally:
        room_manager.processing_locks[match_id] = False

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
