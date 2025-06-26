import asyncio
import json
import logging
from collections import deque
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCIceServer, RTCConfiguration
import aiohttp_cors
import numpy as np
import io
from av import AudioFrame # Make sure av is installed (pip install av)
import traceback # Import traceback for detailed error logging

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger("webrtc_transcriber")

pcs = set()

async def _mock_aws_transcribe_stream_response(audio_bytes: bytes, sample_rate: int) -> str:
    """
    Simulates a response from AWS Transcribe.
    In reality, this would be the actual AWS Transcribe streaming logic.
    """
    try:
        duration_seconds = 0.0
        if audio_bytes:
            # Assuming int16 (2 bytes per sample) mono audio for duration calculation
            duration_seconds = len(audio_bytes) / (sample_rate * 2)
            logger.info(f"Mock Transcribe: Processing {len(audio_bytes)} bytes of audio ({round(duration_seconds, 3)}s) at {sample_rate} Hz.")
        else:
            logger.debug("Mock Transcribe: Received empty audio bytes, skipping processing.")

        await asyncio.sleep(0.05) # Simulate network/processing delay

        if duration_seconds > 0.5:
            return "Am auzit ceva semnificativ."
        elif duration_seconds > 0.1:
            return "Aud..."
        else:
            return "Silențiu."
    except Exception as e:
        logger.error(f"Error in _mock_aws_transcribe_stream_response: {e}")
        logger.error(traceback.format_exc())
        return "Eroare la transcriere simulată."


class AudioTranscriberTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, track: MediaStreamTrack):
        super().__init__()
        self.track = track
        self.audio_buffer = deque()
        self.sample_rate = None
        self.processing_task = None
        self.BUFFER_DURATION_MS = 200 # Process audio every 200 ms
        self.total_samples_buffered = 0
        self.active = True # Flag to control the processing loop
        logger.info(f"AudioTranscriberTrack initialized for track ID: {track.id}")

    async def recv(self) -> AudioFrame:
        """
        Receives an audio frame from WebRTC, processes it, buffers it,
        and triggers a transcription task if enough audio is buffered.
        """
        frame = None
        try:
            # Așteptăm un cadru de la track-ul original
            frame = await self.track.recv()
            logger.debug(f"Received raw AudioFrame: {frame.samples} samples, {frame.sample_rate} Hz, layout={frame.layout}, format={frame.format}")
        except Exception as e:
            logger.error(f"Error receiving audio frame from original track: {e}")
            logger.error(traceback.format_exc())
            return frame # Returnează ultimul frame valid sau None dacă nu a putut primi nimic

        if frame is None:
            logger.warning("Received None frame. Skipping processing.")
            return frame

        if self.sample_rate is None:
            self.sample_rate = frame.sample_rate
            logger.info(f"Detected audio sample rate: {self.sample_rate} Hz from first frame.")
        elif self.sample_rate != frame.sample_rate:
            # This can happen if the audio source changes dynamically,
            # or if initial frames report different rates.
            logger.warning(f"Audio sample rate changed from {self.sample_rate} Hz to {frame.sample_rate} Hz. Re-initializing buffer logic.")
            self.sample_rate = frame.sample_rate
            # Clear buffer on sample rate change to avoid mixing incompatible audio
            self.audio_buffer.clear()
            self.total_samples_buffered = 0


        try:
            # Convert to numpy array. PyAV usually decodes to float or int types.
            audio_samples_ndarray = frame.to_ndarray()
            logger.debug(f"Converted AudioFrame to ndarray. Dtype: {audio_samples_ndarray.dtype}, Shape: {audio_samples_ndarray.shape}")
        except Exception as e:
            logger.error(f"Failed to convert AudioFrame to ndarray: {e}. Frame format: {getattr(frame, 'format', 'N/A')}, layout: {getattr(frame, 'layout', 'N/A')}")
            logger.error(traceback.format_exc())
            return frame # Skip this frame if conversion fails

        # --- Audio Preprocessing (ensure int16 mono) ---
        processed_audio_int16 = None

        # Convert to int16 if not already
        if audio_samples_ndarray.dtype != np.int16:
            if np.issubdtype(audio_samples_ndarray.dtype, np.floating):
                # Scale float values to int16 range
                processed_audio_int16 = (audio_samples_ndarray * 32767).astype(np.int16)
                logger.debug(f"Scaled float audio (max/min: {audio_samples_ndarray.max()}/{audio_samples_ndarray.min()}) to int16. New shape: {processed_audio_int16.shape}")
            else:
                # Direct conversion for other non-int16 dtypes
                processed_audio_int16 = audio_samples_ndarray.astype(np.int16)
                logger.debug(f"Converted non-float audio ({audio_samples_ndarray.dtype}) to int16. New shape: {processed_audio_int16.shape}")
        else:
            processed_audio_int16 = audio_samples_ndarray
            logger.debug(f"Audio data is already int16. Shape: {processed_audio_int16.shape}")

        if processed_audio_int16 is None:
            logger.error("Processed audio is None after int16 conversion. Skipping buffering.")
            return frame

        # Flatten the array first to handle channels consistently
        audio_samples_flat = processed_audio_int16.flatten()
        logger.debug(f"Flattened audio array. Shape: {audio_samples_flat.shape}")

        audio_mono = None
        # Determine number of channels from frame layout (this is more reliable than ndarray shape for PyAV)
        # The `TypeError` you saw was likely because `frame.layout.channels` was not an integer,
        # which can happen with malformed frames or unexpected data.
        # Ensure it's an integer before comparison.
        num_channels = getattr(frame.layout, 'channels', 1) # Default to 1 if not found

        if not isinstance(num_channels, int):
             logger.warning(f"Unexpected type for frame.layout.channels: {type(num_channels)}. Defaulting to 1 channel. Value: {num_channels}")
             num_channels = 1 # Force to 1 to prevent TypeErrors

        if num_channels > 1:
            logger.debug(f"Detected {num_channels} channels. Converting to mono.")
            # Reshape to (num_samples, num_channels) and then average across channels
            if audio_samples_flat.shape[0] % num_channels != 0:
                logger.warning(f"Flattened audio length ({audio_samples_flat.shape[0]}) is not a multiple of channel count ({num_channels}). Mono conversion might be imprecise.")
                # Fallback to taking only the first channel if reshape is problematic
                audio_mono = audio_samples_flat[::num_channels]
                logger.debug(f"Fallback mono conversion (first channel only). New shape: {audio_mono.shape}")
            else:
                audio_samples_reshaped = audio_samples_flat.reshape(-1, num_channels)
                audio_mono = audio_samples_reshaped.mean(axis=1).astype(np.int16)
                logger.debug(f"Converted multi-channel ({audio_samples_flat.shape}) to mono ({audio_mono.shape}) by averaging.")
        else:
            audio_mono = audio_samples_flat # Already mono or treated as such
            logger.debug(f"Audio is already mono (1D). No further channel processing needed. Shape: {audio_mono.shape}")

        if audio_mono is None:
            logger.error("Mono audio conversion resulted in None. Skipping buffering.")
            return frame

        num_samples_in_frame = audio_mono.shape[0]
        self.total_samples_buffered += num_samples_in_frame

        self.audio_buffer.append(audio_mono.tobytes())
        logger.debug(f"Buffered {num_samples_in_frame} samples. Total buffered: {self.total_samples_buffered} samples.")

        if self.sample_rate is None or self.sample_rate == 0:
            logger.warning("Sample rate not set or is zero. Cannot calculate samples_per_buffer for processing trigger.")
            # Cannot trigger processing task without a valid sample rate
        else:
            samples_per_buffer = int(self.sample_rate * (self.BUFFER_DURATION_MS / 1000.0))
            if self.total_samples_buffered >= samples_per_buffer:
                if self.processing_task is None or self.processing_task.done():
                    logger.info(f"Accumulated {self.total_samples_buffered} samples ({round(self.total_samples_buffered / self.sample_rate * 1000, 2)}ms). Triggering audio processing.")
                    self.processing_task = asyncio.create_task(self._process_audio_buffer())
                else:
                    logger.debug("Buffer full, but previous processing task is still running. Waiting for next cycle.")
            else:
                logger.debug(f"Buffer has {self.total_samples_buffered} samples, less than target {samples_per_buffer}. Waiting for more audio.")

        return frame # It's good practice to return the frame, even if not strictly needed for this use case

    async def _process_audio_buffer(self):
        """
        Collects data from the buffer and sends it (simulated) to AWS Transcribe.
        """
        try:
            logger.debug("Starting _process_audio_buffer task.")
            combined_audio_bytes = b"".join(list(self.audio_buffer))

            # Calculate actual samples processed from the extracted bytes
            num_samples_processed = len(combined_audio_bytes) // 2 # 2 bytes per int16 sample (mono)

            # Clear the buffer and adjust total_samples_buffered
            self.audio_buffer.clear()
            # Only subtract the samples that were actually extracted and processed
            self.total_samples_buffered -= num_samples_processed
            if self.total_samples_buffered < 0:
                self.total_samples_buffered = 0 # Prevent negative counts

            if not combined_audio_bytes:
                logger.debug("Buffer was empty after extraction, no data to send for transcription.")
                return

            if self.sample_rate is None or self.sample_rate == 0:
                logger.error("Cannot process audio: sample_rate is not set or is zero.")
                return

            logger.info(f"Sending {len(combined_audio_bytes)} bytes ({round(len(combined_audio_bytes) / (self.sample_rate * 2), 2)}s) for transcription (processed {num_samples_processed} samples).")
            transcription = await _mock_aws_transcribe_stream_response(
                combined_audio_bytes,
                self.sample_rate
            )
            if transcription:
                logger.info(f"Transcription result: \"{transcription}\"")
            else:
                logger.debug("Mock transcription returned no text.")

        except Exception as e:
            logger.error(f"Error during _process_audio_buffer task: {e}")
            logger.error(traceback.format_exc())
        finally:
            logger.debug(f"Audio processing task finished. Remaining samples in buffer: {self.total_samples_buffered}.")


    async def stop(self):
        """
        Stops the processing and cancels background tasks.
        """
        logger.info("Stopping AudioTranscriberTrack...")
        self.active = False # Signal to any loops that they should stop
        if self.processing_task and not self.processing_task.done():
            self.processing_task.cancel()
            try:
                await self.processing_task # Wait for task to truly finish/cancel
                logger.debug("Processing task cancelled successfully.")
            except asyncio.CancelledError:
                logger.debug("Processing task was already cancelled during stop.")
            except Exception as e:
                logger.error(f"Error awaiting processing task during stop: {e}")
                logger.error(traceback.format_exc())
        else:
            logger.debug("No active processing task to stop or task already done.")
        await super().stop() # Call parent's stop


# --- WebRTC Signaling and Server Setup ---

async def offer(request):
    """
    Handles the SDP offer from the WebRTC client.
    """
    logger.info("Received /offer request.")
    pc = None # Initialize pc outside try-except for final cleanup
    try:
        params = await request.json()
        offer_sdp = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
        logger.debug(f"Received SDP offer type: {params['type']}")
    except json.JSONDecodeError as e:
        logger.error(f"Invalid JSON in /offer request: {e}")
        logger.error(traceback.format_exc())
        return web.json_response({"error": "Invalid JSON"}, status=400)
    except KeyError as e:
        logger.error(f"Missing key in /offer request: {e}")
        logger.error(traceback.format_exc())
        return web.json_response({"error": f"Missing key: {e}"}, status=400)
    except Exception as e:
        logger.error(f"Unexpected error parsing offer request: {e}")
        logger.error(traceback.format_exc())
        return web.json_response({"error": "Internal server error during request parsing"}, status=500)

    try:
        ice_servers = [RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
        config = RTCConfiguration(iceServers=ice_servers)

        pc = RTCPeerConnection(configuration=config)
        pcs.add(pc)
        logger.info(f"New PeerConnection created (ID: {id(pc)}). Total active PCs: {len(pcs)}")

        # Store AudioTranscriberTrack instance to manage its lifecycle
        pc_audio_transcriber = {}

        @pc.on("iceconnectionstatechange")
        async def on_ice_state_change():
            current_pc_id = id(pc)
            logger.info(f"PC ID {current_pc_id} - ICE connection state: {pc.iceConnectionState}")
            if pc.iceConnectionState == "failed":
                logger.warning(f"PC ID {current_pc_id} - ICE connection failed. Attempting to close PeerConnection.")
                await pc.close() # This will trigger on_connection_state_change
            elif pc.iceConnectionState == "disconnected":
                logger.info(f"PC ID {current_pc_id} - ICE connection disconnected. Preparing to close PeerConnection.")
                await pc.close() # This will trigger on_connection_state_change

        @pc.on("track")
        def on_track(track):
            current_pc_id = id(pc)
            logger.info(f"PC ID {current_pc_id} - New track received: kind={track.kind}, id={track.id}")
            if track.kind == "audio":
                logger.info(f"PC ID {current_pc_id} - Creating AudioTranscriberTrack for audio stream ID: {track.id}")
                audio_transcriber = AudioTranscriberTrack(track)
                pc.addTrack(audio_transcriber) # This makes AudioTranscriberTrack.recv() get called
                pc_audio_transcriber[track.id] = audio_transcriber # Store reference

                @track.on("ended")
                async def on_track_ended():
                    logger.info(f"PC ID {current_pc_id} - Track {track.kind} (ID: {track.id}) ended. Initiating transcriber stop.")
                    if track.id in pc_audio_transcriber:
                        await pc_audio_transcriber[track.id].stop()
                        del pc_audio_transcriber[track.id] # Remove reference
                    else:
                        logger.warning(f"No AudioTranscriberTrack found for track ID {track.id} when track ended.")

        @pc.on("connectionstatechange")
        async def on_connection_state_change():
            current_pc_id = id(pc)
            logger.info(f"PC ID {current_pc_id} - PeerConnection state: {pc.connectionState}")
            if pc.connectionState == "disconnected" or pc.connectionState == "closed":
                logger.info(f"PC ID {current_pc_id} - PeerConnection {pc.connectionState}. Attempting to close...")
                # Stop all associated transcriber tracks
                for track_id, transcriber in list(pc_audio_transcriber.items()):
                    logger.info(f"PC ID {current_pc_id} - Stopping transcriber for track {track_id}.")
                    await transcriber.stop()
                    del pc_audio_transcriber[track_id] # Clean up reference

                await pc.close() # Ensure PC is truly closed
                if pc in pcs:
                    pcs.discard(pc) # Remove from global set
                logger.info(f"PC ID {current_pc_id} removed from active set. Total active PCs: {len(pcs)}")

        await pc.setRemoteDescription(offer_sdp)
        logger.debug(f"PC ID {id(pc)} - Remote description set successfully.")

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        logger.info(f"PC ID {id(pc)} - ⬅ Sending SDP answer to client (Type: {pc.localDescription.type}).")

        return web.json_response({
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type
        })

    except Exception as e:
        logger.error(f"Critical error during PeerConnection setup or SDP exchange: {e}")
        logger.error(traceback.format_exc())
        if pc and pc in pcs:
            try:
                logger.info(f"Attempting to close PC ID {id(pc)} due to setup error.")
                await pc.close()
                pcs.discard(pc)
                logger.info(f"PC ID {id(pc)} closed after error. Total active PCs: {len(pcs)}")
            except Exception as close_e:
                logger.error(f"Error closing PeerConnection after initial setup error: {close_e}")
                logger.error(traceback.format_exc())
        return web.json_response({"error": "Failed to establish WebRTC connection"}, status=500)


async def on_shutdown(app: web.Application):
    """
    Closes all PeerConnections on application shutdown to release resources.
    """
    logger.info("Server shutting down. Closing all active PeerConnections...")
    coros = [pc.close() for pc in list(pcs)] # Copy set to avoid modification during iteration
    if coros:
        await asyncio.gather(*coros, return_exceptions=True) # Run concurrently, log exceptions
        logger.info(f"Attempted to close {len(coros)} PeerConnections.")
    else:
        logger.info("No active PeerConnections to close.")
    pcs.clear() # Clear the global set
    logger.info("All PeerConnections processed during shutdown. Server shutdown complete.")


app = web.Application()

# Configure CORS
cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(
        allow_credentials=True,
        expose_headers="*",
        allow_headers="*",
        allow_methods=["POST"], # Only POST is needed for /offer
    )
})

# Add routes and CORS handlers
route = app.router.add_post("/offer", offer)
cors.add(route)

# Register shutdown hook
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    try:
        logger.info("Starting WebRTC Audio Transcriber Server on http://0.0.0.0:8080")
        web.run_app(app, port=8080)
    except Exception as e:
        logger.critical(f"Failed to start the WebRTC server: {e}")
        logger.critical(traceback.format_exc())