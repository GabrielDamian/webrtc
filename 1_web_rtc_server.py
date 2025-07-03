import asyncio
import logging
import time
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCIceServer, RTCConfiguration
import aiohttp_cors
from dotenv import load_dotenv
import os

load_dotenv()

pcs = set()
audio_queue_1 = asyncio.Queue()
audio_queue_2 = asyncio.Queue()

SAMPLE_RATE = 16000

# Setup root logger to WARNING to suppress less important logs
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("webrtc")

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
        if current_time - self.last_switch_time >= 5.0:
            self.use_queue_1 = not self.use_queue_1
            self.last_switch_time = current_time
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
            q1_size = audio_queue_1.qsize()
            q2_size = audio_queue_2.qsize()
            print(f"Queue sizes - Queue 1: {q1_size} items, Queue 2: {q2_size} items")
            await asyncio.sleep(1)  # Update every second
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
