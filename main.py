import socket

from pyexpat.errors import messages

HOST = "...."
PORT = 10000

def start_client():
    client_sock  = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client_sock.connect((HOST, PORT))
        print(f"Connected to server in :{HOST}, {PORT}")
        response = client_sock.recv(1024).decode('utf-8')
        while True:
            message = input("send message to server or exit")
            if message.lower == "exit" :
                break
            client_sock.sendall(message.encode('utf-8'))

            response = client_sock.recv(1024).decode('utf-8')
            print(f"server {response}")
    except ConnectionRefusedError:
        print("Connection Failed")
    finally:
        client_sock.close()

if __name__ == "__main__":
    start_client()