import asyncio
import websockets
import wave
import io
import time
from datetime import datetime
import os
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent
from amazon_transcribe.utils import apply_realtime_delay

# Audio configuration
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
CHANNEL_NUMS = 1
REGION = "eu-west-1"

class MyEventHandler(TranscriptResultStreamHandler):
    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        results = transcript_event.transcript.results
        for result in results:
            for alt in result.alternatives:
                print(alt.transcript)

class AudioStreamHandler:
    def __init__(self):
        self.client = None
        self.stream = None
        self.handler = None
        self.processing_task = None

    async def initialize_transcribe(self):
        self.client = TranscribeStreamingClient(region=REGION)
        self.stream = await self.client.start_stream_transcription(
            language_code="en-US",
            media_sample_rate_hz=SAMPLE_RATE,
            media_encoding="pcm",
        )
        self.handler = MyEventHandler(self.stream.output_stream)
        self.processing_task = asyncio.create_task(self.handler.handle_events())

    async def process_audio_data(self, data):
        if not self.stream:
            await self.initialize_transcribe()
        
        # Send audio data to AWS Transcribe
        await self.stream.input_stream.send_audio_event(audio_chunk=data)

    async def close(self):
        if self.stream:
            await self.stream.input_stream.end_stream()
        if self.processing_task:
            await self.processing_task

async def handle_websocket(websocket):
    print("Client connected")
    stream_handler = AudioStreamHandler()
    
    try:
        async for message in websocket:
            if isinstance(message, bytes):
                await stream_handler.process_audio_data(message)
    except websockets.exceptions.ConnectionClosed:
        print("Client disconnected")
    finally:
        await stream_handler.close()

async def main():
    async with websockets.serve(handle_websocket, "localhost", 8765):
        print("WebSocket server started on ws://localhost:8765")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())