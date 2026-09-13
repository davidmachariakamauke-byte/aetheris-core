from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
from typing import List

# Initialize the V8 Command Engine
app = FastAPI(title="Aetheris V8 Core API")

# Allow the frontend to talk to this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Update this to your GitHub Pages URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Hardware SMS Bridge / Admin routing line
SYSTEM_ADMIN_NUMBER = "0748615143"

# --- WEBSOCKET MANAGER (The AI Referee Comms) ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            await connection.send_json(message)

# Global instance of the connection manager
manager = ConnectionManager()

# --- PYDANTIC SCHEMAS (Strict Type Checking) ---
class EscrowRequest(BaseModel):
    in_game_id: str
    stake_amount: float
    mpesa_number: str

# --- REST API: STK PUSH TRIGGER ---
@app.post("/api/escrow/stk-push")
async def trigger_stk_push(req: EscrowRequest, background_tasks: BackgroundTasks):
    """
    Receives the HUD data, saves it to DB, and triggers Safaricom Daraja.
    """
    # TODO: Insert SQLAlchemy DB saving logic here

    # 1. Here is where you will call your mpesa_rail.py STK push function
    # mpesa_response = await initiate_stk_push(req.mpesa_number, req.stake_amount)
    
    # 2. Simulate Daraja Callback for development (Removes the need to wait on M-Pesa while coding)
    background_tasks.add_task(simulate_daraja_callback, req)
    
    return {"status": "success", "message": f"STK Push authorized to {req.mpesa_number}"}

async def simulate_daraja_callback(req: EscrowRequest):
    """Simulates the delay of a user entering their M-Pesa PIN, then alerts the HUD via WebSockets"""
    await asyncio.sleep(4)  
    await manager.broadcast({
        "event": "PAYMENT_SECURED",
        "in_game_id": req.in_game_id,
        "message": f"FUNDS SECURED. Match for {req.in_game_id} is live. Vision algorithms active."
    })

# --- WEBSOCKET API: LIVE TELEMETRY & CHAT ---
@app.websocket("/ws/referee")
async def websocket_referee(websocket: WebSocket):
    """
    The persistent connection between the AI and the frontend UI.
    """
    await manager.connect(websocket)
    try:
        while True:
            # Listen for messages from the user's chat input
            data = await websocket.receive_text()
            
            # Simulated AI NLP Processing Time
            await asyncio.sleep(0.5)
            
            if "win" in data.lower():
                await websocket.send_json({
                    "event": "AI_ALERT", 
                    "message": "I am monitoring the match stream. Results will be announced upon conclusion.",
                    "type": "alert"
                })
            else:
                await websocket.send_json({
                    "event": "AI_RESPONSE", 
                    "message": "Acknowledged. Maintain clean gameplay. My vision algorithms are actively scanning memory buffers.",
                    "type": "normal"
                })
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
