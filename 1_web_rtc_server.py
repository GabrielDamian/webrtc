import asyncio
import logging
import time
import wave
from datetime import datetime
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCIceServer, RTCConfiguration
import aiohttp_cors
from dotenv import load_dotenv
import os

load_dotenv()

pcs = set()
audio_queue_1 = asyncio.Queue()
audio_queue_2 = asyncio.Queue()

# Shared state between threads
active_queue_num = asyncio.Event()  # False = Queue 1, True = Queue 2
active_queue_num.clear()  # Start with Queue 1

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit audio

# Setup root logger to WARNING to suppress less important logs
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("webrtc")

def save_audio_chunk(audio_data, queue_num):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"queue_{queue_num}_chunk_{timestamp}.wav"
    
    with wave.open(filename, 'wb') as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(audio_data)
    
    print(f"Saved audio chunk to {filename}")

class AudioRelayTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, track):
        super().__init__()
        self.track = track
        self.last_switch_time = time.time()
        self.use_queue_1 = True

    async def recv(self):
        frame = await self.track.recv()
        audio_bytes = b"".join(bytes(plane) for plane in frame.planes)
        
        current_time = time.time()
        if current_time - self.last_switch_time >= 2.0:
            self.use_queue_1 = not self.use_queue_1
            self.last_switch_time = current_time
            # Update the shared state
            if self.use_queue_1:
                active_queue_num.clear()  # Queue 1
            else:
                active_queue_num.set()    # Queue 2
            print(f"Switching to queue {'1' if self.use_queue_1 else '2'}")

        if self.use_queue_1:
            await audio_queue_1.put(audio_bytes)
        else:
            await audio_queue_2.put(audio_bytes)

        return frame

async def queue_monitor():
    try:
        print("Queue monitor started")
        while True:
            # Check which queue is active
            is_queue_2 = active_queue_num.is_set()
            inactive_queue = audio_queue_1 if is_queue_2 else audio_queue_2
            inactive_queue_num = 1 if is_queue_2 else 2
            
            # Collect all chunks from inactive queue
            all_chunks = []
            while not inactive_queue.empty():
                chunk = await inactive_queue.get()
                all_chunks.append(chunk)
            
            # If we collected any chunks, save them to a WAV file
            if all_chunks:
                combined_audio = b"".join(all_chunks)
                save_audio_chunk(combined_audio, inactive_queue_num)
            
            await asyncio.sleep(0.1)  # Small delay to prevent CPU overload
    except Exception as e:
        print(f"Error in queue monitor: {e}")

async def offer(request):
    print("[offer] Received new offer request")
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    ice_servers = [RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
    config = RTCConfiguration(iceServers=ice_servers)

    pc = RTCPeerConnection(configuration=config)
    pcs.add(pc)
    print("[offer] Created new PeerConnection")

    @pc.on("iceconnectionstatechange")
    def on_ice_state_change():
        print(f"[PeerConnection] ICE connection state changed: {pc.iceConnectionState}")

    @pc.on("track")
    def on_track(track):
        print(f"[PeerConnection] Track received: kind={track.kind}")
        if track.kind == "audio":
            relay = AudioRelayTrack(track)
            pc.addTrack(relay)
            print("[PeerConnection] AudioRelayTrack added to PeerConnection")

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    print("[offer] Sending SDP answer to client")
    return web.json_response({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    })

async def on_startup(app):
    print("[app] Starting queue monitor task")
    app['monitor_task'] = asyncio.create_task(queue_monitor())

async def on_shutdown(app):
    print("[app] Shutting down PeerConnections")
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)

    print("[app] Stopping queue monitor")
    if 'monitor_task' in app:
        app['monitor_task'].cancel()
        try:
            await app['monitor_task']
        except asyncio.CancelledError:
            pass

app = web.Application()

cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(
        allow_credentials=True,
        expose_headers="*",
        allow_headers="*",
    )
})

route = app.router.add_post("/offer", offer)
cors.add(route)

app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    print("[main] Starting server on http://0.0.0.0:8080")
    web.run_app(app, port=8080)
