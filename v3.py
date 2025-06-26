import asyncio
import json
import logging
from collections import deque
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCIceServer, RTCConfiguration
import aiohttp_cors
import numpy as np
import io

import boto3
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from dotenv import load_dotenv
import os
load_dotenv()

AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "eu-west-1") 

# logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger("webrtc_transcriber")

pcs = set()

# --- Funcție de streaming AWS Transcribe Reală ---
async def _send_to_aws_transcribe_real(audio_stream_iterator, sample_rate, language_code="ro-RO"):
    """
    Trimite un flux audio către AWS Transcribe în timp real și procesează răspunsurile.
    """
    client = boto3.client("transcribe", region_name="eu-west-1") # Folosește regiunea ta!

    # Această clasă este o interfață simplificată pentru a trimite și a primi evenimente
    # Pe măsură ce boto3 se îmbunătățește, s-ar putea să existe o cale directă.
    # Pentru moment, o vom construi manual.

    # EventStream API necesită o clasă pentru a gestiona evenimentele de transcriere.
    # Aici o implementăm ca un generator asincron.
    class TranscriptResultStreamHandler:
        def __init__(self, output_stream):
            self.output_stream = output_stream
            self.final_transcription = ""
            self.loop = asyncio.get_event_loop()
            self.segment_start_time = None

        async def handle_events(self):
            try:
                async for event in self.output_stream:
                    if "TranscriptEvent" in event:
                        transcript_event = event["TranscriptEvent"]
                        for result in transcript_event["Results"]:
                            if not result["IsPartial"]:
                                # Rezultat final
                                full_transcript = " ".join([alt["Transcript"] for alt in result["Alternatives"]])
                                self.final_transcription += full_transcript + " "
                                logger.info(f"FINAL Transcribe: {full_transcript}")
                                # Resetăm pentru următorul segment
                                self.segment_start_time = None
                            else:
                                # Rezultat parțial
                                partial_transcript = " ".join([alt["Transcript"] for alt in result["Alternatives"]])
                                # Poți decide să printezi sau să ignori rezultatele parțiale
                                # pentru a evita spam-ul în consolă.
                                logger.debug(f"Partial Transcribe: {partial_transcript}")
            except Exception as e:
                logger.error(f"Error handling transcribe stream events: {e}")

    # Coadă asincronă pentru a pune bucățile audio care urmează să fie trimise
    request_queue = asyncio.Queue()

    # Generator asincron care preia bucăți audio din coadă
    async def audio_event_generator():
        while True:
            audio_chunk = await request_queue.get()
            if audio_chunk is None: # Semnal de sfârșit de stream
                break
            yield {'AudioEvent': {'AudioChunk': audio_chunk}}
            logger.debug(f"Sent audio chunk to Transcribe: {len(audio_chunk)} bytes.")

    try:
        # Inițierea stream-ului de transcriere
        response = await client.start_stream_transcription(
            LanguageCode=language_code,
            MediaEncoding="pcm",
            SampleRateHertz=sample_rate,
            AudioStream=audio_event_generator()
        )
        output_stream = response['EventStream']
        
        handler = TranscriptResultStreamHandler(output_stream)
        
        # Rulează în paralel trimiterea audio și primirea evenimentelor
        await asyncio.gather(
            _put_audio_chunks_in_queue(audio_stream_iterator, request_queue),
            handler.handle_events()
        )
        
        logger.info(f"Transcribe streaming session ended. Full transcription: {handler.final_transcription}")
        return handler.final_transcription

    except ClientError as e:
        logger.error(f"AWS Transcribe Client Error: {e}")
        return None
    except Exception as e:
        logger.error(f"Error during AWS Transcribe streaming: {e}")
        return None

# Funcție helper pentru a pune bucățile audio în coada pentru Transcribe
async def _put_audio_chunks_in_queue(audio_stream_iterator, request_queue):
    """
    Preia bucăți audio de la iterator și le pune în coada pentru Transcribe.
    """
    try:
        async for chunk in audio_stream_iterator:
            await request_queue.put(chunk)
        await request_queue.put(None) # Semnalăm sfârșitul stream-ului
    except Exception as e:
        logger.error(f"Error putting audio chunks into queue: {e}")
        await request_queue.put(None) # Asigură-te că stream-ul se închide

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
        self.transcribe_session_task = None # Task pentru sesiunea de transcriere reală
        self.transcribe_audio_input_queue = asyncio.Queue() # Coada pentru audio trimis la Transcribe
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
            # Inițializează sesiunea de transcriere reală la primul cadru
            self.transcribe_session_task = asyncio.create_task(
                _send_to_aws_transcribe_real(self._audio_chunk_generator(), self.sample_rate)
            )

        # Extrageți datele audio ca un array NumPy
        try:
            audio_samples_ndarray = frame.to_ndarray()
        except Exception as e:
            logger.error(f"Failed to convert AudioFrame to ndarray: {e}. Frame format: {getattr(frame, 'format', 'N/A')}. Frame layout: {getattr(frame, 'layout', 'N/A')}")
            return frame # Sări peste acest cadru dacă nu poate fi convertit

        # Asigurăm conversia la np.int16 și mono
        if audio_samples_ndarray.dtype == np.int16:
            audio_samples_int16 = audio_samples_ndarray
            logger.debug(f"Frame already int16. Shape: {audio_samples_int16.shape}")
        else:
            # Dacă este float (e.g., float32, float64), scalați și convertiți la int16
            # Asigură-te că este în intervalul [-1.0, 1.0] dacă nu este deja
            if audio_samples_ndarray.max() > 1.0 or audio_samples_ndarray.min() < -1.0:
                # Scalare pentru a aduce în intervalul int16
                audio_samples_int16 = (audio_samples_ndarray * 32767).astype(np.int16)
                logger.debug(f"Scaled float audio to int16. Original max/min: {audio_samples_ndarray.max()}/{audio_samples_ndarray.min()}. New shape: {audio_samples_int16.shape}")
            else:
                # Dacă este deja în [-1.0, 1.0], scalați la [-32767, 32767]
                audio_samples_int16 = (audio_samples_ndarray * 32767).astype(np.int16)
                logger.debug(f"Scaled float audio to int16 (already in range). Shape: {audio_samples_int16.shape}")

        # aiortc frames can be mono or stereo. AWS Transcribe expects mono.
        # If your audio is stereo (e.g., shape (N, 2)), you'll need to convert to mono.
        if audio_samples_int16.ndim > 1 and audio_samples_int16.shape[1] > 1:
            # Convert stereo to mono by averaging channels
            original_shape = audio_samples_int16.shape
            audio_samples_int16 = audio_samples_int16.mean(axis=1).astype(np.int16)
            logger.debug(f"Converted stereo {original_shape} to mono {audio_samples_int16.shape}.")
        elif audio_samples_int16.ndim == 1:
            logger.debug(f"Audio is already mono. Shape: {audio_samples_int16.shape}")
        else:
            logger.warning(f"Unexpected audio samples dimension: {audio_samples_int16.ndim}. Expected 1 or 2.")

        # Adăugăm numărul de eșantioane la totalul tamponat
        self.total_samples_buffered += audio_samples_int16.shape[0]

        # Adăugăm datele audio brute în buffer
        self.audio_buffer.append(audio_samples_int16.tobytes())
        logger.debug(f"Buffered {audio_samples_int16.shape[0]} samples. Total buffered: {self.total_samples_buffered} samples.")

        # Verificăm dacă am acumulat suficient audio pentru a procesa și a trimite la Transcribe
        samples_per_buffer = int(self.sample_rate * (self.BUFFER_DURATION_MS / 1000.0))

        if self.total_samples_buffered >= samples_per_buffer:
            if self.processing_task is None or self.processing_task.done():
                logger.info(f"Accumulated {self.total_samples_buffered} samples ({round(self.total_samples_buffered / self.sample_rate * 1000)}ms). Sending chunk to Transcribe queue.")
                self.processing_task = asyncio.create_task(self._put_chunk_to_transcribe_queue())
            else:
                logger.debug("Buffer full, but sending task is still running. Waiting for next cycle.")
        
        return frame

    async def _put_chunk_to_transcribe_queue(self):
        """
        Colectează datele din buffer și le pune în coada pentru Transcribe.
        """
        # Extragem tot ce este în buffer acum
        combined_audio_bytes = b"".join(list(self.audio_buffer))
        
        # Calculate how many samples were actually removed from buffer
        num_samples_processed = len(combined_audio_bytes) // 2 # 2 bytes per int16 sample (mono)
        self.audio_buffer.clear()
        self.total_samples_buffered -= num_samples_processed # Scădem doar ce am procesat
        if self.total_samples_buffered < 0: # Asigură-te că nu devine negativ din cauza erorilor de aproximare
            self.total_samples_buffered = 0

        if not combined_audio_bytes:
            logger.debug("Buffer was empty after extraction, no data to send to Transcribe queue.")
            return

        logger.info(f"Queueing {len(combined_audio_bytes)} bytes ({round(len(combined_audio_bytes) / (self.sample_rate * 2), 2)}s) for AWS Transcribe.")
        try:
            await self.transcribe_audio_input_queue.put(combined_audio_bytes)
        except Exception as e:
            logger.error(f"Error putting audio chunk into Transcribe queue: {e}")
        finally:
            logger.debug(f"Chunk queued. Remaining samples in buffer: {self.total_samples_buffered}.")

    async def _audio_chunk_generator(self):
        """
        Generator asincron care servește bucățile audio către _send_to_aws_transcribe_real.
        """
        while True:
            chunk = await self.transcribe_audio_input_queue.get()
            if chunk is None:
                logger.info("Transcribe audio input queue received None, stopping generator.")
                break
            yield chunk

    async def stop(self):
        logger.info("Stopping AudioTranscriberTrack and AWS Transcribe session...")
        
        # Semnalează sfârșitul stream-ului către Transcribe
        if self.transcribe_audio_input_queue:
            await self.transcribe_audio_input_queue.put(None)

        if self.transcribe_session_task:
            self.transcribe_session_task.cancel()
            try:
                await self.transcribe_session_task
                logger.debug("Transcribe session task cancelled successfully.")
            except asyncio.CancelledError:
                logger.debug("Transcribe session task was already cancelled.")
            except Exception as e:
                logger.error(f"Error while stopping transcribe session task: {e}")
        
        if self.processing_task:
            self.processing_task.cancel()
            try:
                await self.processing_task
                logger.debug("Local buffering processing task cancelled successfully.")
            except asyncio.CancelledError:
                logger.debug("Local buffering processing task was already cancelled.")
            except Exception as e:
                logger.error(f"Error while stopping local buffering task: {e}")

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

# Configurarea CORS pentru a permite cereri de la orice origine (pentru dezvoltare)
cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(
        allow_credentials=True,
        expose_headers="*",
        allow_headers="*",
        allow_methods=["POST"], # Specificăm metodele permise
    )
})

# Adăugarea rutei și aplicarea CORS
route = app.router.add_post("/offer", offer)
cors.add(route)

app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    logger.info("Starting WebRTC Audio Transcriber Server...")
    web.run_app(app, port=8080)