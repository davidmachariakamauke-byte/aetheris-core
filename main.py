import os
import asyncio
import base64
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types

app = FastAPI(title="AETHERIS | Autonomous AI Escrow")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

@app.get("/", response_class=HTMLResponse)
def read_root():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AETHERIS | Next-Gen AI Escrow</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <meta name="theme-color" content="#0f172a">
    </head>
    <body class="bg-slate-900 text-white font-sans antialiased flex flex-col items-center justify-center min-h-screen p-4">

        <div class="max-w-md w-full p-6 bg-slate-800 rounded-2xl shadow-2xl border border-slate-700">
            <h1 class="text-4xl font-black text-transparent bg-clip-text bg-gradient-to-r from-cyan-400 to-blue-600 mb-2 tracking-tighter">
                AETHERIS
            </h1>
            <p class="text-slate-400 text-sm mb-8">Autonomous AI-Adjudicated Esport Escrow</p>

            <div class="space-y-4">
                <button id="depositBtn" class="w-full bg-slate-700 hover:bg-slate-600 text-white font-bold py-3 px-4 rounded-xl transition duration-200">
                    Fund Escrow (M-Pesa / USDT)
                </button>
                
                <button id="startMatchBtn" class="w-full bg-gradient-to-r from-cyan-500 to-blue-600 hover:from-cyan-400 hover:to-blue-500 text-white font-bold py-3 px-4 rounded-xl shadow-lg shadow-cyan-500/30 transition duration-200">
                    Start Match & Share Screen
                </button>
            </div>

            <div id="statusBox" class="mt-6 p-4 bg-slate-900 rounded-lg text-xs font-mono text-cyan-400 hidden border border-slate-800">
                <!-- AI Referee real-time logs -->
            </div>
            
            <video id="screenVideo" autoplay muted class="hidden"></video>
            <canvas id="frameCanvas" class="hidden"></canvas>
        </div>

        <script>
            const startBtn = document.getElementById('startMatchBtn');
            const video = document.getElementById('screenVideo');
            const canvas = document.getElementById('frameCanvas');
            const statusBox = document.getElementById('statusBox');
            const ctx = canvas.getContext('2d');

            startBtn.addEventListener('click', async () => {
                try {
                    const stream = await navigator.mediaDevices.getDisplayMedia({ video: true });
                    video.srcObject = stream;
                    
                    statusBox.classList.remove('hidden');
                    statusBox.innerText = ">> Screen capture active. Connecting to AETHERIS AI Referee...";

                    const wsProto = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
                    const wsUrl = wsProto + window.location.host + '/ws/live-referee/match_001';
                    const ws = new WebSocket(wsUrl);

                    ws.onopen = () => {
                        statusBox.innerText += "\\n>> AI connection established. Referee is tracking match.";
                        
                        setInterval(() => {
                            if (video.videoWidth) {
                                canvas.width = video.videoWidth;
                                canvas.height = video.videoHeight;
                                ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
                                const frameData = canvas.toDataURL('image/jpeg', 0.5); 
                                ws.send(frameData);
                            }
                        }, 3000);
                    };

                    ws.onmessage = (event) => {
                        statusBox.innerText = ">> " + event.data;
                    };

                    ws.onerror = (error) => {
                        statusBox.innerText += "\\n>> WebSocket error encountered.";
                        console.error(error);
                    };

                } catch (err) {
                    alert("Screen sharing permission is required to operate the AETHERIS escrow referee.");
                    console.error(err);
                }
            });
        </script>
    </body>
    </html>
    """

@app.websocket("/ws/live-referee/{match_id}")
async def live_referee_stream(websocket: WebSocket, match_id: str):
    await websocket.accept()
    print(f"Match {match_id} Stream Connected.")
    
    try:
        while True:
            frame_data = await websocket.receive_text()
            b64_string = frame_data.split(',')[1] if ',' in frame_data else frame_data
            
            prompt = """
            You are the AETHERIS Autonomous AI Referee. Analyze this live game frame. 
            1. Is the match actively being played?
            2. Has a 'Victory' or 'Game Over' screen appeared?
            Respond strictly in valid JSON format: {"status": "PLAYING" | "VICTORY" | "FORFEIT", "winner": "string or null"}
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
                print(f"Match {match_id} Concluded. Triggering Auto-Escrow Payout...")
                break
                
            await asyncio.sleep(3) 
            
    except WebSocketDisconnect:
        print(f"Player disconnected from Match {match_id}")
