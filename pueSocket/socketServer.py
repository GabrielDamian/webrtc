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
import threading
from collections import deque
from dataclasses import dataclass
from typing import List
import socket
import pickle

# Audio configuration
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
CHANNEL_NUMS = 1
REGION = "eu-west-1"

# Transcription receiver configuration
RECEIVER_HOST = 'localhost'
RECEIVER_PORT = 5000

@dataclass
class TranscriptionItem:
    text: str
    timestamp: datetime

class TranscriptionBuffer:
    def __init__(self):
        self.items = []
        self.lock = threading.Lock()
        self.last_addition_time = None

    def add_item(self, text: str):
        with self.lock:
            self.items.append({
                'text': text,
                'timestamp': datetime.now().strftime('%H:%M:%S.%f')[:-3]
            })
            self.last_addition_time = time.time()

    def get_items(self):
        with self.lock:
            return list(self.items)

    def clear_items(self):
        with self.lock:
            self.items.clear()
            self.last_addition_time = None

    def time_since_last_addition(self):
        with self.lock:
            if self.last_addition_time is None:
                return float('inf')
            return time.time() - self.last_addition_time

def send_to_receiver(transcriptions):
    try:
        # Create a new socket connection
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.connect((RECEIVER_HOST, RECEIVER_PORT))

        # Serialize the transcriptions
        data = pickle.dumps(transcriptions)
        
        # Send the size first
        size = len(data)
        client_socket.send(size.to_bytes(8, 'big'))
        
        # Send the actual data
        client_socket.sendall(data)
        
        print(f"Sent {len(transcriptions)} transcriptions to receiver")
        
    except Exception as e:
        print(f"Error sending to receiver: {e}")
    finally:
        client_socket.close()

# Global transcription buffer
transcription_buffer = TranscriptionBuffer()

class MyEventHandler(TranscriptResultStreamHandler):
    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        results = transcript_event.transcript.results
        for result in results:
            for alt in result.alternatives:
                transcription_buffer.add_item(alt.transcript)

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
        
        await self.stream.input_stream.send_audio_event(audio_chunk=data)

    async def close(self):
        if self.stream:
            await self.stream.input_stream.end_stream()
        if self.processing_task:
            await self.processing_task

def monitor_transcriptions():
    while True:
        items = transcription_buffer.get_items()
        if items and transcription_buffer.time_since_last_addition() >= 2:
            # If there are items and no new items for 3 seconds, send them
            send_to_receiver(items)
            transcription_buffer.clear_items()
        time.sleep(1)

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
    # Start the monitoring thread
    monitor_thread = threading.Thread(target=monitor_transcriptions, daemon=True)
    monitor_thread.start()

    # Start the WebSocket server
    async with websockets.serve(handle_websocket, "localhost", 8765):
        print("WebSocket server started on ws://localhost:8765")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")