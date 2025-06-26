import asyncio
import logging
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCIceServer, RTCConfiguration
import aiohttp_cors

from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

pcs = set()
audio_queue = asyncio.Queue()

SAMPLE_RATE = 16000
REGION = "us-west-2"

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("webrtc-aws")

class MyEventHandler(TranscriptResultStreamHandler):
    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        for result in transcript_event.transcript.results:
            for alt in result.alternatives:
                logger.info(f"AWS Transcribe: {alt.transcript}")

class AudioRelayTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, track):
        super().__init__()
        self.track = track

    async def recv(self):
        frame = await self.track.recv()
        audio_bytes = b"".join(bytes(plane) for plane in frame.planes)

        logger.debug(f"Received audio frame timestamp={frame.time}, bytes={len(audio_bytes)}")
        await audio_queue.put(audio_bytes)
        return frame

async def aws_transcribe_worker():
    logger.info("AWS Transcribe worker started")
    client = TranscribeStreamingClient(region=REGION)

    stream = await client.start_stream_transcription(
        language_code="en-US",
        media_sample_rate_hz=SAMPLE_RATE,
        media_encoding="pcm",
    )

    async def send_audio():
        logger.info("Started sending audio to AWS Transcribe")
        while True:
            chunk = await audio_queue.get()
            if chunk is None:
                logger.info("Audio queue received stop signal")
                break
            await stream.input_stream.send_audio_event(audio_chunk=chunk)
        await stream.input_stream.end_stream()
        logger.info("Finished sending audio to AWS Transcribe")

    handler = MyEventHandler(stream.output_stream)
    await asyncio.gather(send_audio(), handler.handle_events())
    logger.info("AWS Transcribe worker finished")

async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    ice_servers = [RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
    config = RTCConfiguration(iceServers=ice_servers)

    pc = RTCPeerConnection(configuration=config)
    pcs.add(pc)
    logger.info("Created new PeerConnection")

    @pc.on("iceconnectionstatechange")
    def on_ice_state_change():
        logger.info(f"ICE connection state: {pc.iceConnectionState}")

    @pc.on("track")
    def on_track(track):
        logger.info(f"Track received: kind={track.kind}")
        if track.kind == "audio":
            relay = AudioRelayTrack(track)
            pc.addTrack(relay)
            logger.info("AudioRelayTrack added to PeerConnection")

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    logger.info("Sending SDP answer to client")
    return web.json_response({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    })

async def on_startup(app):
    logger.info("Starting AWS transcribe worker task")
    app['aws_task'] = asyncio.create_task(aws_transcribe_worker())

async def on_shutdown(app):
    logger.info("Shutting down PeerConnections")
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)

    logger.info("Stopping AWS transcribe worker")
    await audio_queue.put(None)  # semnal pentru oprirea worker-ului
    if 'aws_task' in app:
        await app['aws_task']

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
    logger.info("Starting server on http://0.0.0.0:8080")
    web.run_app(app, port=8080)
