import asyncio
import json
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCIceServer, RTCConfiguration
import aiohttp_cors

pcs = set()

class AudioPrinterTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, track):
        super().__init__()
        self.track = track

    async def recv(self):
        frame = await self.track.recv()
        print("Frame:", frame)
        print("Received audio frame (timestamp: {})".format(frame.time))
        return frame

async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    ice_servers = [RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
    config = RTCConfiguration(iceServers=ice_servers)

    pc = RTCPeerConnection(configuration=config)
    pcs.add(pc)

    @pc.on("iceconnectionstatechange")
    def on_ice_state_change():
        print(f"ICE connection state: {pc.iceConnectionState}")

    @pc.on("track")
    def on_track(track):
        print(f"Track received: kind={track.kind}")
        if track.kind == "audio":
            audio_printer = AudioPrinterTrack(track)
            pc.addTrack(audio_printer)

    await pc.setRemoteDescription(offer)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    print("⬅Sending SDP answer to client")
    return web.json_response({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    })


async def on_shutdown(app):
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)

app = web.Application()

cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(
        allow_credentials=True,
        expose_headers="*",
        allow_headers="*",
    )
})

# CORS
route = app.router.add_post("/offer", offer)
cors.add(route)

app.on_shutdown.append(on_shutdown)

print("Server started on http://localhost:8080")
web.run_app(app, port=8080)
