#!/usr/bin/env python3
import argparse
import socket
import threading
from collections import defaultdict

from nacl.encoding import HexEncoder
from nacl.public import Box, PrivateKey, PublicKey
from nacl.signing import SigningKey, VerifyKey
from nacl.utils import random

from config import HOST, MAX_PORT, MIN_PORT, MAX_USERNAME_LEN, SESSION_KEY_SIZE
from utility import mac_send, port, recv_dec, recv_verify


class Server:
    def __init__(self, host, port):
        # key used to decrypt messages
        self.private_key = PrivateKey(
            open("./key_pairs/server", encoding="utf-8").read(), HexEncoder
        )
        # key used to sign messages
        self.signing_key = SigningKey(
            open("./key_pairs/server_dsa", encoding="utf-8").read(), HexEncoder
        )
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.s.bind((host, port))
        self.clients = defaultdict(str)

    def start(self):
        print("Waiting for connection")
        self.s.listen()

        while True:
            conn, addr = self.s.accept()
            args = (conn, addr)
            threading.Thread(target=self.connect_client, args=args).start()
    
    def connect_client(self, conn, addr):
        client_user_bytes = conn.recv(MAX_USERNAME_LEN)
        client_user = client_user_bytes.decode()

        if not client_user.isalnum():
            conn.close()
            return

        print(f"Received connection request from client {client_user}")

        # key used to encrypt messages for the client
        client_public_key = PublicKey(
            open(f"./key_pairs/{client_user}.pub", encoding="utf-8").read(), HexEncoder
        )
        # key used to verify messages from the client
        verify_key = VerifyKey(
            open(f"./key_pairs/{client_user}_dsa.pub", encoding="utf-8").read(), HexEncoder
        )

        box = Box(self.private_key, client_public_key)

        # 24 bytes
        nonce = random(Box.NONCE_SIZE)

        # challenge
        mac_send(conn, nonce, self.signing_key, box)

        # response
        decrypted_nonce = recv_verify(conn, verify_key)

        if nonce == decrypted_nonce:
            print(f"Client {client_user} authenticated successfully")
        else:
            print(f"Failed login from client {client_user}")
            conn.close()

        sym_key = random(SESSION_KEY_SIZE)
        mac_send(conn, sym_key, self.signing_key, box)

        # ip:port:key
        self.clients[client_user] = [addr[0], addr[1], sym_key]

        while True:
            cmd = recv_dec(conn, sym_key)

            match cmd:
                case b'?':
                    # client list
                    msg = bytes("\n".join(self.get_clients()), "utf-8")
                    mac_send(conn, msg, sym_key)

                case b'':
                    conn.close()

                    if client_user in self.clients:
                        print(f'User disconnected: {client_user}')
                        del self.clients[client_user]

                    break

                case _:
                    # start session
                    user = cmd.decode()

                    if user not in self.clients:
                        mac_send(conn, b':', sym_key)
                        continue

                    session_key = random(SESSION_KEY_SIZE)
                    #print(f'Key: {session_key}')

                    # connect to peer's current port + 1
                    ip, port, peer_key = self.clients[user]
                    keySock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    keySock.connect((ip, (port + 1) % MAX_PORT))

                    # send session key to peer

                    msg = bytes(client_user, "utf-8") + b":" + session_key
                    mac_send(keySock, msg, peer_key)

                    # get listen port from peer
                    msg = recv_dec(keySock, peer_key)
                    keySock.close() 

                    if not msg:
                        port = ""
                    try:
                        msg = msg.decode()
                        port = int(msg)
                        if port < MIN_PORT or port > MAX_PORT:
                            port = ""
                    except ValueError:
                        port = ""

                    # send port and session_key to client
                    msg = bytes(str(port), "utf-8") + b":" + session_key
                    mac_send(conn, msg, sym_key)

    def get_clients(self):
        return [f"{name}:{val[0]}:{val[1]}" for name, val in self.clients.items()]

    def die(self):
        print("dying")
        self.s.shutdown(1)
        self.s.close()


def main():
    parser = argparse.ArgumentParser("Client application")
    parser.add_argument(
        "--port", type=port, default=8000, help="Port to run server on"
    )
    args = parser.parse_args()

    s = Server(HOST, args.port)
    s.start()


if __name__ == "__main__":
    main()
