import socket
import pickle
import json
from datetime import datetime

class TranscriptionReceiver:
    def __init__(self, host='localhost', port=5000):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.bind((host, port))
        self.server_socket.listen(1)
        print(f"Transcription receiver listening on {host}:{port}")

    def start(self):
        while True:
            client_socket, address = self.server_socket.accept()
            print(f"Connection from {address}")
            
            try:
                # Receive the data size first
                size_data = client_socket.recv(8)
                size = int.from_bytes(size_data, 'big')
                
                # Receive the actual data
                data = b""
                while len(data) < size:
                    chunk = client_socket.recv(min(size - len(data), 4096))
                    if not chunk:
                        break
                    data += chunk

                # Unpickle the data
                if data:
                    transcriptions = pickle.loads(data)
                    print("\n=== Received Transcriptions ===")
                    for item in transcriptions:
                        print(f"[{item['timestamp']}] {item['text']}")
                    
            except Exception as e:
                print(f"Error receiving data: {e}")
            finally:
                client_socket.close()

if __name__ == "__main__":
    receiver = TranscriptionReceiver()
    receiver.start()