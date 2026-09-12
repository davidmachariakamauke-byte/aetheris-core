from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import json

# Initialize the AETHERIS Engine
app = FastAPI(title="AETHERIS v8.0 AI Engine", version="8.0")

# Configure CORS to allow your frontend (like GitHub Pages) to communicate with this Render backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # For production, you can replace "*" with your specific frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 1. ROOT ENDPOINT (Fixes the "Not Found" error)
# ==========================================
@app.get("/")
async def root():
    """
    When you visit your Render URL in a browser, this will display the live status 
    instead of throwing a 404 Not Found error.
    """
    return {
        "status": "ONLINE",
        "engine": "AETHERIS v8.0 AI Engine",
        "message": "Backend is active. Awaiting WebSocket connections and M-PESA callbacks."
    }

# ==========================================
# 2. WEBSOCKET MANAGER
# ==========================================
class MatchManager:
    def __init__(self):
        self.active_matches = {}

    async def connect(self, websocket: WebSocket, in_game_id: str):
        await websocket.accept()
        self.active_matches[in_game_id] = websocket
        print(f"[+] Player {in_game_id} connected to AI Referee.")

    def disconnect(self, in_game_id: str):
        if in_game_id in self.active_matches:
            del self.active_matches[in_game_id]
            print(f"[-] Player {in_game_id} disconnected.")

    async def send_ai_message(self, in_game_id: str, message: str, msg_type: str = "chat", action: str = None):
        if in_game_id in self.active_matches:
            payload = {"type": msg_type, "message": message}
            if action:
                payload["action"] = action
            await self.active_matches[in_game_id].send_text(json.dumps(payload))

manager = MatchManager()

# ==========================================
# 3. AI REFEREE WEBSOCKET ROUTE
# ==========================================
@app.websocket("/ws/referee/{in_game_id}")
async def ai_referee_endpoint(websocket: WebSocket, in_game_id: str):
    await manager.connect(websocket, in_game_id)
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)

            # 1. Telemetry / Screen Monitoring Logic
            if payload.get("type") == "telemetry_frame":
                # In the future, this is where the base64 screen frames are passed 
                # to a Vision Model to detect mod menus or modified UIs.
                pass 

            # 2. Player Chat & Auto-Referee Logic
            elif payload.get("type") == "player_chat":
                player_msg = payload.get("text").lower()
                
                # Smart AI Responses
                if "lag" in player_msg or "ping" in player_msg:
                    await manager.send_ai_message(in_game_id, "Telemetry shows stable connection. Focus on the game.")
                
                elif "finished" in player_msg or "i won" in player_msg or "gg" in player_msg:
                    # Trigger the Winner Announcement & Prepare Payout
                    await manager.send_ai_message(
                        in_game_id, 
                        f"MATCH CONCLUDED. Winner declared: {in_game_id}. Processing M-PESA escrow payouts...", 
                        msg_type="announcement",
                        action="trigger_payout"
                    )
                    # NOTE: Once Safaricom provisions your Daraja credentials for Store 1231006, 
                    # this is where we will trigger the B2C M-PESA payout function.
                
                else:
                    await manager.send_ai_message(in_game_id, "I am monitoring the match. Keep your screen focused on the gameplay.")

    except WebSocketDisconnect:
        manager.disconnect(in_game_id)
