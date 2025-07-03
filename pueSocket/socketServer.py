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
import json

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
        self.websocket = None  # Store WebSocket connection

    def set_websocket(self, websocket):
        self.websocket = websocket

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

async def send_to_receiver(transcriptions, websocket):
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

        # Receive response size
        response_size_data = client_socket.recv(8)
        response_size = int.from_bytes(response_size_data, 'big')

        # Receive response data
        response_data = b""
        while len(response_data) < response_size:
            chunk = client_socket.recv(min(response_size - len(response_data), 4096))
            if not chunk:
                break
            response_data += chunk

        # Unpickle and send response to WebSocket client
        if response_data:
            response = pickle.loads(response_data)
            await websocket.send(json.dumps({
                'type': 'server_response',
                'message': response
            }))
            print(f"Forwarded response to client: {response}")
        
    except Exception as e:
        print(f"Error communicating with receiver: {e}")
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

async def monitor_transcriptions(websocket):
    while True:
        try:
            items = transcription_buffer.get_items()
            if items and transcription_buffer.time_since_last_addition() >= 3:
                # If there are items and no new items for 3 seconds, send them
                await send_to_receiver(items, websocket)
                transcription_buffer.clear_items()
            await asyncio.sleep(1)
        except websockets.exceptions.ConnectionClosed:
            break
        except Exception as e:
            print(f"Error in monitor_transcriptions: {e}")
            break

async def handle_websocket(websocket):
    print("Client connected")
    stream_handler = AudioStreamHandler()
    
    # Start the monitoring task for this connection
    monitor_task = asyncio.create_task(monitor_transcriptions(websocket))
    
    try:
        async for message in websocket:
            if isinstance(message, bytes):
                await stream_handler.process_audio_data(message)
    except websockets.exceptions.ConnectionClosed:
        print("Client disconnected")
    finally:
        monitor_task.cancel()
        await stream_handler.close()

async def main():
    async with websockets.serve(handle_websocket, "localhost", 8765):
        print("WebSocket server started on ws://localhost:8765")
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")