import os
import sys
import subprocess
import asyncio
from threading import Thread, Lock
from queue import Queue, Empty
import tkinter as tk
from tkinter import filedialog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.websockets import WebSocketState
import time

# Helper function to show progress messages
def show_message(msg):
    print(f"\n{'-' * 50}\n{msg}\n{'-' * 50}")

# Function to enqueue subprocess output
def enqueue_output(out, queue):
    for line in iter(out.readline, b''):
        queue.put(line)
    out.close()

class EngineChess:
    def __init__(self, path_engine):
        print(f"Attempting to start engine: {path_engine}")
        if not os.path.exists(path_engine):
            show_message(f"ERROR: Engine path does not exist:\n{path_engine}")
            raise FileNotFoundError(f"Engine not found at {path_engine}")
        
        try:
            self._engine = subprocess.Popen(
                [path_engine],
                universal_newlines=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as e:
            print(f"FATAL ERROR starting engine: {path_engine}")
            print(f"DETAILS: {e}")
            if hasattr(e, 'winerror') and e.winerror == 193:
                show_message(
                    "The selected file is not a valid Windows application.\\n"
                    "This can happen if:\\n"
                    "  - The file is for a different OS (e.g., Linux).\\n"
                    "  - The file requires a CPU feature you don't have (like AVX2).\\n"
                    "  - The file is corrupt or not a real engine.\\n\\n"
                    "Please restart and try selecting a different engine, like 'opental_x64plain.exe'."
                )
            raise e

        self.queueOutput = Queue()
        self.thread = Thread(target=enqueue_output, args=(self._engine.stdout, self.queueOutput))
        self.thread.daemon = True
        self.thread.start()

        self._command_queue = Queue()  # Queue for incoming commands
        self._queue_lock = Lock()     # Lock for queue operations
        self._has_quit_command_been_sent = False
        self._current_turn = "w"      # Track the current turn

    def put(self, cmd):
        with self._queue_lock:
            self._command_queue.put(cmd)

    def send_next_command(self):
        """Send the next command from the queue to the engine."""
        with self._queue_lock:
            if not self._command_queue.empty():
                cmd = self._command_queue.get()
                if self._engine.stdin and not self._has_quit_command_been_sent:
                    self._engine.stdin.write(f"{cmd}\n")
                    self._engine.stdin.flush()
                    if cmd == "quit":
                        self._has_quit_command_been_sent = True

    def _read_line(self) -> str:
        if not self._engine.stdout:
            raise BrokenPipeError()
        if self._engine.poll() is not None:
            raise Exception("The engine process has crashed")

        try:
            line = self.queueOutput.get_nowait()
        except Empty:
            return ""
        
        return line.strip()

    def read_line(self) -> str:
        self.send_next_command()  # Ensure the next command is sent before reading output
        return self._read_line()

# Initialize Tkinter for file dialog
root = tk.Tk()
root.withdraw()

show_message("Please select engine executable files.")
initial_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'engines & books')
engine_exe_paths = filedialog.askopenfilenames(
    title="Select engine executable files",
    initialdir=initial_dir
)

if not engine_exe_paths:
    print("No engine selected. Exiting.")
    sys.exit()

# Initialize engines
engines = [EngineChess(path) for path in engine_exe_paths]

# FastAPI app
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

active_connections = set()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global active_connections
    if len(active_connections) >= 2:
        await websocket.close(code=1001, reason="Maximum connections reached")
        return

    active_connections.add(websocket)
    await websocket.accept()

    try:
        async def process_client_command(data):
            """Process commands received from the client."""
            if data.startswith("ucinewgame"):
                # Reset engines on new game
                for engine in engines:
                    engine.put("ucinewgame")
            elif data.startswith("position fen"):
                for engine in engines:
                    engine.put(data)
            elif data.startswith("go"):
                # Process turn alternation during "go"
                for engine in engines:
                    engine.put(data)
            else:
                for engine in engines:
                    engine.put(data)

        async def handle_client():
            while websocket.client_state == WebSocketState.CONNECTED:
                try:
                    data = await websocket.receive_text()
                    print(f"Client: {data}")
                    asyncio.create_task(process_client_command(data))
                except Exception as e:
                    print(f"Error receiving data: {e}")
                    break

        asyncio.create_task(handle_client())

        while True:
            responses = [engine.read_line() for engine in engines]
            responses = list(filter(None, responses))  # Remove empty responses

            if responses:
                for res in responses:
                    for conn in active_connections:
                        if conn.client_state == WebSocketState.CONNECTED:
                            await conn.send_text(res)
            else:
                await asyncio.sleep(0.01)  # Reduce delay for high-speed responses

    except WebSocketDisconnect:
        print("WebSocket disconnected.")
    except Exception as e:
        print(f"Error in WebSocket: {e}")
    finally:
        active_connections.discard(websocket)
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close()

@app.get("/")
async def get():
    return HTMLResponse("""<!DOCTYPE html>
<html>
    <head>
        <title>Engine WebSocket</title>
    </head>
    <body>
        <h1>Chess Engine WebSocket</h1>
        <form action="" onsubmit="sendMessage(event)">
            <input type="text" id="messageText" autocomplete="off"/>
            <button>Send</button>
        </form>
        <ul id='messages'></ul>
        <script>
            var ws = new WebSocket("ws://localhost:8000/ws");
            ws.onmessage = function(event) {
                var messages = document.getElementById('messages');
                var message = document.createElement('li');
                var content = document.createTextNode(event.data);
                message.appendChild(content);
                messages.appendChild(message);
            };
            ws.onclose = function(event) {
                console.log("WebSocket closed, attempting to reconnect...");
                setTimeout(function() {
                    ws = new WebSocket("ws://localhost:8000/ws");
                }, 1000);
            };
            function sendMessage(event) {
                var input = document.getElementById("messageText");
                if (ws.readyState === WebSocket.OPEN) {
                    ws.send(input.value);
                }
                input.value = '';
                event.preventDefault();
            }
        </script>
    </body>
</html>
""")

if __name__ == "__main__":
    print("BetterMint Initialized!")
    print("All selected engines are active.")
