import asyncio
import logging
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCIceServer, RTCConfiguration
import aiohttp_cors
from dotenv import load_dotenv
import os

from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

load_dotenv()

pcs = set()
audio_queue = asyncio.Queue()

SAMPLE_RATE = 16000
REGION = "us-west-2"

# Setup root logger to WARNING to suppress less important logs
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("webrtc-aws")

# Create dedicated AWS logger for transcribe-related info at INFO level
logger_aws = logging.getLogger("aws-transcribe")
logger_aws.setLevel(logging.INFO)

# Add simple console handler for aws logger to ensure output
if not logger_aws.hasHandlers():
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger_aws.addHandler(ch)
    logger_aws.propagate = False

class MyEventHandler(TranscriptResultStreamHandler):
    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        logger_aws.info("handle_transcript_event called")
        logger_aws.info("handle_transcript_event called")
        for result in transcript_event.transcript.results:
            for alt in result.alternatives:
                logger_aws.info(f"AWS Transcribe: {alt.transcript}")

class AudioRelayTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, track):
        super().__init__()
        self.track = track

    async def recv(self):
        frame = await self.track.recv()
        audio_bytes = b"".join(bytes(plane) for plane in frame.planes)
        print(f"[AudioRelayTrack] Received audio frame, size: {len(audio_bytes)} bytes")
        await audio_queue.put(audio_bytes)
        return frame

async def aws_transcribe_worker():
    try:
        logger_aws.info("AWS Transcribe worker started")
        client = TranscribeStreamingClient(region=REGION)

        stream = await client.start_stream_transcription(
            language_code="en-US",
            media_sample_rate_hz=SAMPLE_RATE,
            media_encoding="pcm",
        )

        async def send_audio():
            logger_aws.info("Started sending audio to AWS Transcribe")
            while True:
                logger_aws.info(f"Audio queue size: {audio_queue.qsize()}")
                chunk = await audio_queue.get()
                if chunk is None:
                    logger_aws.info("Audio queue received stop signal")
                    break
                logger_aws.info(f"Sending audio chunk to AWS Transcribe, size: {len(chunk)} bytes, queue size: {audio_queue.qsize()}")
                try:
                    await stream.input_stream.send_audio_event(audio_chunk=chunk)
                except Exception as e:
                    logger_aws.error(f"Error sending audio chunk to AWS Transcribe: {e}", exc_info=True)
                logger_aws.info(f"Sending audio chunk to AWS Transcribe, size: {len(chunk)} bytes")
                await stream.input_stream.send_audio_event(audio_chunk=chunk)
            await stream.input_stream.end_stream()
            logger_aws.info("Finished sending audio to AWS Transcribe")

        handler = MyEventHandler(stream.output_stream)
        await asyncio.gather(send_audio(), handler.handle_events())
        logger_aws.info("AWS Transcribe worker finished")

    except Exception as e:
        logger_aws.error(f"Error in aws_transcribe_worker: {e}", exc_info=True)

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
    print("[app] Starting AWS transcribe worker task")
    app['aws_task'] = asyncio.create_task(aws_transcribe_worker())

async def on_shutdown(app):
    print("[app] Shutting down PeerConnections")
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)

    print("[app] Stopping AWS transcribe worker")
    await audio_queue.put(None)  # signal for stopping worker
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
    print("[main] Starting server on http://0.0.0.0:8080")
    web.run_app(app, port=8080)
