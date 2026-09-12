from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import json
import asyncio

app = FastAPI(title="AETHERIS v8.0 AI Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class MatchManager:
    def __init__(self):
        self.active_matches = {}

    async def connect(self, websocket: WebSocket, in_game_id: str):
        await websocket.accept()
        self.active_matches[in_game_id] = websocket

    def disconnect(self, in_game_id: str):
        if in_game_id in self.active_matches:
            del self.active_matches[in_game_id]

    async def send_ai_message(self, in_game_id: str, message: str, msg_type: str = "chat", action: str = None):
        if in_game_id in self.active_matches:
            payload = {"type": msg_type, "message": message}
            if action:
                payload["action"] = action
            await self.active_matches[in_game_id].send_text(json.dumps(payload))

manager = MatchManager()

@app.websocket("/ws/referee/{in_game_id}")
async def ai_referee_endpoint(websocket: WebSocket, in_game_id: str):
    await manager.connect(websocket, in_game_id)
    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)

            # 1. Telemetry / Screen Monitoring Logic
            if payload.get("type") == "telemetry_frame":
                # FUTURE: Pass payload["data"] (base64 image) to a Vision Model (e.g., Gemini Flash/Pro)
                # to detect third-party mod menus, crosshairs, or modified UI.
                
                # For now, simulate the AI silently analyzing
                pass 

            # 2. Player Chat Logic
            elif payload.get("type") == "player_chat":
                player_msg = payload.get("text").lower()
                
                # Simulated Smart AI Responses
                if "lag" in player_msg:
                    await manager.send_ai_message(in_game_id, "Telemetry shows stable ping. Focus on the game.")
                elif "finished" in player_msg or "i won" in player_msg:
                    # Trigger the Winner Announcement Logic
                    await manager.send_ai_message(
                        in_game_id, 
                        f"MATCH CONCLUDED. Winner declared: {in_game_id}. Processing rewards.", 
                        msg_type="announcement",
                        action="trigger_payout"
                    )
                else:
                    await manager.send_ai_message(in_game_id, "I am monitoring the match. Please keep the screen focused on the game.")

    except WebSocketDisconnect:
        manager.disconnect(in_game_id)
        print(f"Player {in_game_id} disconnected.")
