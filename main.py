import os
import asyncio
import base64
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types

app = FastAPI(title="AETHERIS AI Escrow")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

@app.get("/")
def read_root():
    return {"system": "AETHERIS Core Online", "status": "operational"}

@app.websocket("/ws/live-referee/{match_id}")
async def live_referee_stream(websocket: WebSocket, match_id: str):
    await websocket.accept()
    print(f"Match {match_id} Stream Connected.")
    
    try:
        while True:
            frame_data = await websocket.receive_text()
            # Clean the base64 string
            b64_string = frame_data.split(',')[1] if ',' in frame_data else frame_data
            
            prompt = """
            You are the AETHERIS AI Referee. Analyze this live game frame. 
            1. Is the match actively being played?
            2. Has a 'Victory' or 'Game Over' screen appeared?
            Respond strictly in JSON format: {"status": "PLAYING" | "VICTORY" | "FORFEIT", "winner": "string or null"}
            """
            
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[
                    types.Part.from_bytes(data=base64.b64decode(b64_string), mime_type="image/jpeg"),
                    prompt
                ],
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json"
                )
            )
            
            ai_decision = response.text
            await websocket.send_text(f"AI_SYNC: {ai_decision}")
            
            if "VICTORY" in ai_decision:
                print("Match Concluded. Initiating Escrow Payout...")
                break
                
            await asyncio.sleep(3) 
            
    except WebSocketDisconnect:
        print(f"Player disconnected from Match {match_id}")
