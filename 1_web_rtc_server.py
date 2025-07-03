import asyncio
import logging
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCIceServer, RTCConfiguration
import aiohttp_cors
from dotenv import load_dotenv
import os

load_dotenv()

pcs = set()
audio_queue = asyncio.Queue()

SAMPLE_RATE = 16000

# Setup root logger to WARNING to suppress less important logs
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("webrtc")

class AudioRelayTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, track):
        super().__init__()
        self.track = track

    async def recv(self):
        frame = await self.track.recv()
        audio_bytes = b"".join(bytes(plane) for plane in frame.planes)
        await audio_queue.put(audio_bytes)
        print(f"[AudioRelayTrack] Added frame to queue. Queue size now: {audio_queue.qsize()}")
        return frame

async def audio_worker():
    try:
        print("Audio worker started")
        while True:
            # Wait for data in the queue
            chunk = await audio_queue.get()
            current_queue_size = audio_queue.qsize()
            
            if chunk is None:
                print("Audio queue received stop signal")
                break
                
            print(f"Processing audio chunk, size: {len(chunk)} bytes. Remaining items in queue: {current_queue_size}")
            
        print("Audio worker finished")

    except Exception as e:
        print(f"Error in audio_worker: {e}")

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
    print("[app] Starting audio worker task")
    app['audio_task'] = asyncio.create_task(audio_worker())

async def on_shutdown(app):
    print("[app] Shutting down PeerConnections")
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)

    print("[app] Stopping audio worker")
    await audio_queue.put(None)  # signal for stopping worker
    if 'audio_task' in app:
        await app['audio_task']

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
