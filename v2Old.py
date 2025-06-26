import asyncio
import json
import logging
from collections import deque
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCIceServer, RTCConfiguration
import aiohttp_cors
import numpy as np
import io
from av import AudioFrame

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger("webrtc_transcriber")

pcs = set()

async def _mock_aws_transcribe_stream_response(audio_bytes, sample_rate):
    """
    Simulează un răspuns de la AWS Transcribe.
    În realitate, aici ar fi logica de streaming AWS Transcribe.
    """
    if audio_bytes: 
        logger.info(f"Mock Transcribe: Received {len(audio_bytes)} bytes of audio ({round(len(audio_bytes)/(sample_rate * 2), 2)}s) at {sample_rate} Hz.")
    else:
        logger.debug("Mock Transcribe: Received empty audio bytes.")
    
    await asyncio.sleep(0.05)
    
    if len(audio_bytes) > (sample_rate * 2 * 0.5): # Peste 0.5 secunde de audio (stereo e 4 bytes per sample, mono e 2)
        return "Am auzit ceva semnificativ."
    elif len(audio_bytes) > (sample_rate * 2 * 0.1): # Peste 0.1 secunde de audio
        return "Aud..."
    else:
        return "Silențiu."


class AudioTranscriberTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, track):
        super().__init__()
        self.track = track
        self.audio_buffer = deque()
        self.sample_rate = None
        self.processing_task = None
        self.BUFFER_DURATION_MS = 200 # Procesăm audio la fiecare 200 ms
        self.total_samples_buffered = 0
        logger.info(f"AudioTranscriberTrack initialized for track ID: {track.id}")


    async def recv(self):
        """
        Primește un cadru audio de la WebRTC.
        """
        try:
            frame = await self.track.recv()
        except Exception as e:
            logger.error(f"Error receiving audio frame: {e}")
            return # Nu putem continua fără cadru

        if self.sample_rate is None:
            self.sample_rate = frame.sample_rate
            logger.info(f"Detected audio sample rate: {self.sample_rate} Hz from first frame.")

        try:
            audio_samples_ndarray = frame.to_ndarray()
        except Exception as e:
            logger.error(f"Failed to convert AudioFrame to ndarray: {e}. Frame format: {getattr(frame, 'format', 'N/A')}. Frame layout: {getattr(frame, 'layout', 'N/A')}")
            return frame # Sări peste acest cadru dacă nu poate fi convertit

        if audio_samples_ndarray.dtype == np.int16:
            audio_samples_int16 = audio_samples_ndarray
            logger.debug(f"Frame already int16. Shape: {audio_samples_int16.shape}")
        else:
            if audio_samples_ndarray.max() > 1.0 or audio_samples_ndarray.min() < -1.0:
                audio_samples_int16 = (audio_samples_ndarray * 32767).astype(np.int16)
                logger.debug(f"Scaled float audio to int16. Original max/min: {audio_samples_ndarray.max()}/{audio_samples_ndarray.min()}. New shape: {audio_samples_int16.shape}")
            else:
                audio_samples_int16 = (audio_samples_ndarray * 32767).astype(np.int16)
                logger.debug(f"Scaled float audio to int16 (already in range). Shape: {audio_samples_int16.shape}")


        if audio_samples_int16.ndim > 1 and audio_samples_int16.shape[1] > 1:
            original_shape = audio_samples_int16.shape
            audio_samples_int16 = audio_samples_int16.mean(axis=1).astype(np.int16)
            logger.debug(f"Converted stereo {original_shape} to mono {audio_samples_int16.shape}.")
        elif audio_samples_int16.ndim == 1:
            logger.debug(f"Audio is already mono. Shape: {audio_samples_int16.shape}")
        else:
            logger.warning(f"Unexpected audio samples dimension: {audio_samples_int16.ndim}. Expected 1 or 2.")


        self.total_samples_buffered += audio_samples_int16.shape[0]

        self.audio_buffer.append(audio_samples_int16.tobytes())
        logger.debug(f"Buffered {audio_samples_int16.shape[0]} samples. Total buffered: {self.total_samples_buffered} samples.")

        samples_per_buffer = int(self.sample_rate * (self.BUFFER_DURATION_MS / 1000.0))

        if self.total_samples_buffered >= samples_per_buffer:
            if self.processing_task is None or self.processing_task.done():
                logger.info(f"Accumulated {self.total_samples_buffered} samples ({round(self.total_samples_buffered / self.sample_rate * 1000)}ms). Triggering audio processing.")
                self.processing_task = asyncio.create_task(self._process_audio_buffer())
            else:
                logger.debug("Buffer full, but processing task is still running. Waiting for next cycle.")
        
        return frame

    async def _process_audio_buffer(self):
        """
        Colectează datele din buffer și le trimite (simulat) către AWS Transcribe.
        """
        combined_audio_bytes = b"".join(list(self.audio_buffer))
        
        num_samples_processed = len(combined_audio_bytes) // 2 # 2 bytes per int16 sample (mono)
        self.audio_buffer.clear()
        self.total_samples_buffered -= num_samples_processed # Scădem doar ce am procesat
        if self.total_samples_buffered < 0: # Asigură-te că nu devine negativ din cauza erorilor de aproximare
            self.total_samples_buffered = 0

        if not combined_audio_bytes:
            logger.debug("Buffer was empty after extraction, no data to send for transcription.")
            return

        logger.info(f"Sending {len(combined_audio_bytes)} bytes ({round(len(combined_audio_bytes) / (self.sample_rate * 2), 2)}s) for transcription (processed {num_samples_processed} samples).")
        try:
            transcription = await _mock_aws_transcribe_stream_response(
                combined_audio_bytes,
                self.sample_rate
            )
            if transcription:
                logger.info(f"Transcription result: \"{transcription}\"")

        except Exception as e:
            logger.error(f"Error during transcription process: {e}")
        finally:
            logger.debug(f"Audio processing task finished. Remaining samples in buffer: {self.total_samples_buffered}.")


    async def stop(self):
        logger.info("Stopping AudioTranscriberTrack...")
        if self.processing_task:
            self.processing_task.cancel()
            try:
                await self.processing_task
                logger.debug("Processing task cancelled successfully.")
            except asyncio.CancelledError:
                logger.debug("Processing task was already cancelled.")
            except Exception as e:
                logger.error(f"Error while stopping processing task: {e}")
        await super().stop()


async def offer(request):
    """
    Gestionează oferta SDP de la clientul WebRTC.
    """
    logger.info("Received /offer request.")
    params = await request.json()
    offer_sdp = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    ice_servers = [RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
    config = RTCConfiguration(iceServers=ice_servers)

    pc = RTCPeerConnection(configuration=config)
    pcs.add(pc)

    logger.info(f"New PeerConnection created. Total active PCs: {len(pcs)}")

    @pc.on("iceconnectionstatechange")
    async def on_ice_state_change():
        logger.info(f"PC ID {id(pc)} - ICE connection state: {pc.iceConnectionState}")
        if pc.iceConnectionState == "failed":
            logger.warning(f"PC ID {id(pc)} - ICE connection failed. Closing PeerConnection.")
            await pc.close()
            pcs.discard(pc)
            logger.info(f"PC ID {id(pc)} closed. Total active PCs: {len(pcs)}")

    @pc.on("track")
    def on_track(track):
        logger.info(f"PC ID {id(pc)} - Track received: kind={track.kind}, id={track.id}")
        if track.kind == "audio":
            audio_transcriber = AudioTranscriberTrack(track)
            pc.addTrack(audio_transcriber)
            logger.info(f"PC ID {id(pc)} - Added AudioTranscriberTrack for audio stream.")
            
            @track.on("ended")
            async def on_track_ended():
                logger.info(f"PC ID {id(pc)} - Track {track.kind} ended.")
                await audio_transcriber.stop() # Asigură-te că oprești procesarea la sfârșitul track-ului

    @pc.on("connectionstatechange")
    async def on_connection_state_change():
        logger.info(f"PC ID {id(pc)} - PeerConnection state: {pc.connectionState}")
        if pc.connectionState == "disconnected":
            logger.info(f"PC ID {id(pc)} - PeerConnection disconnected. Closing...")
            await pc.close()
            pcs.discard(pc)
            logger.info(f"PC ID {id(pc)} closed. Total active PCs: {len(pcs)}")
        elif pc.connectionState == "closed":
            logger.info(f"PC ID {id(pc)} - PeerConnection already closed.")
            pcs.discard(pc) # Asigură-te că este eliminat din set
            logger.info(f"PC ID {id(pc)} removed from active set. Total active PCs: {len(pcs)}")


    await pc.setRemoteDescription(offer_sdp)

    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    logger.info(f"PC ID {id(pc)} - ⬅ Sending SDP answer to client.")
    return web.json_response({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type
    })


async def on_shutdown(app):
    """
    Închide toate PeerConnections la oprirea aplicației.
    """
    logger.info("Server shutting down. Closing all active PeerConnections...")
    coros = [pc.close() for pc in list(pcs)] # Copiem setul pentru a evita modificarea în timpul iterației
    await asyncio.gather(*coros)
    pcs.clear()
    logger.info("All PeerConnections closed. Server shutdown complete.")

app = web.Application()

cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(
        allow_credentials=True,
        expose_headers="*",
        allow_headers="*",
        allow_methods=["POST"],
    )
})

route = app.router.add_post("/offer", offer)
cors.add(route)

app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    logger.info("Starting WebRTC Audio Transcriber Server...")
    web.run_app(app, port=8080)