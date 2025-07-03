import socket
import pickle
import json
from datetime import datetime

# Server configuration
HOST = 'localhost'
PORT = 5001  # Changed from 5000 to 5001

class TranscriptionReceiver:
    def __init__(self, host=HOST, port=PORT):
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.bind((host, port))
        self.server_socket.listen(1)
        print(f"Transcription receiver listening on {host}:{port}")

    def generate_response(self, transcriptions):
        # Combine all transcriptions into one text
        print("Magic here:",transcriptions)
        # EXPERMENTAL: Combine all transcriptions into one text
        # combined_text = " ".join(item['text'] for item in transcriptions)
        combined_text = transcriptions[-1]['text']
        return f"Generated Response to: {combined_text}"

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

                # Unpickle the data and generate response
                if data:
                    transcriptions = pickle.loads(data)
                    print("\n=== Received Transcriptions ===")
                    for item in transcriptions:
                        print(f"[{item['timestamp']}] {item['text']}")
                    
                    # Generate response based on received transcriptions
                    response = self.generate_response(transcriptions)
                    print(f"\nGenerated response: {response}")
                    
                    # Send back the response
                    response_data = pickle.dumps(response)
                    size = len(response_data)
                    client_socket.send(size.to_bytes(8, 'big'))
                    client_socket.sendall(response_data)
                    
            except Exception as e:
                print(f"Error handling data: {e}")
            finally:
                client_socket.close()

if __name__ == "__main__":
    receiver = TranscriptionReceiver()
    receiver.start()