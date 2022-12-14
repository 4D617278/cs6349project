#!/usr/bin/env python3
import argparse
import socket
import sys
from threading import Thread
import readline

from nacl.encoding import HexEncoder
from nacl.public import Box, PrivateKey, PublicKey
from nacl.signing import SigningKey, VerifyKey

from config import HOST, MAX_PORT, MIN_PORT
from utility import mac_send, port, recv_dec, sign_send


def clear_current_line():
    # Might be specific to UNIX?
    sys.stdout.write("\x1b[2K")


class Client:
    def __init__(self, user):
        self.user = user
        # key used to decrypt messages
        self.private_key = PrivateKey(
            open(f"./key_pairs/{user}", encoding="utf-8").read(), HexEncoder
        )
        # key used to sign messages
        self.signing_key = SigningKey(
            open(f"./key_pairs/{user}_dsa", encoding="utf-8").read(), HexEncoder
        )
        # key used to encrypt messages for the server
        self.server_public_key = PublicKey(
            open("./key_pairs/server.pub", encoding="utf-8").read(), HexEncoder
        )
        # key used to verify messages from the server
        self.verify_key = VerifyKey(
            open("./key_pairs/server_dsa.pub", encoding="utf-8").read(), HexEncoder
        )
        self.clients = {}
        self.peer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.peer.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.peer_name = ""
        self.peer_key = None
        self.server = None
        self.running_shell = True
        self.connecting = False
        self.program_running = True
        self.sym_key = b""
        self.keySock = None

    def start(self, host, port):
        args = (host, port)
        t = Thread(target=self.login, args=args)
        t.start()
        t.join()

        Thread(target=self.shell).start()

    def shell(self):
        self.running_shell = True

        print("""
Commands: 
    g: get list of clients
    p: connect to a client
    q: quit
        """)

        while self.running_shell:
            cmd = input("> ")
            if not self.running_shell:
                sys.stdout.flush()
                break

            match cmd:
                case "g":
                    # Get list of clients
                    self.get_clients()
                # case "l":
                #    self.login(host, port)
                case "p":
                    # Connect to a peer
                    self.peer_connect()
                case "q":
                    # Quit everything
                    self.running_shell = False
                    self.program_running = False
                    self.die()
                case _:
                    print("Unknown command")
                    print("Commands: g, p, q")

        return

    def peer_connect(self):
        """Connect to another user"""
        user = input("What user would you like to connect to? ")

        if user not in self.clients:
            print(f"No user {user}")
            return

        self.connecting = True
        self.peer_name = user

        ip = self.clients[user][0]
        key, port = self.get_key(user)

        if not key or not port:
            print(f"{user} is busy")
            return

        self.peer.close()
        self.peer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.peer_key = key

        try:
            self.peer.connect((ip, int(port)))
        except ConnectionRefusedError:
            print(f"{user} is busy")
            return

        print(f"You are connected to user {user}")
        self.chat(self.peer, self.peer_name, self.peer_key)
        self.connecting = False

    def chat(self, peer, user, key):
        """
        Start send and receive channels between a user and a peer, using key for encryption
        """
        try:
            peer.getpeername()
        except OSError:
            print("No peer is set")
            return

        args = (peer, user, key)
        Thread(target=self.recv_msgs, args=args).start()
        self.send_msgs(peer, user, key)

    def recv_msgs(self, sock, user, key):
        """
        Receive messages continuously until empty packet received
        """
        while True:
            try:
                msg = recv_dec(sock, key)
            except OSError:
                break

            if not msg:
                break

            current_input = readline.get_line_buffer()
            clear_current_line()
            print(f"{user}: {msg.decode()}\n$ {current_input}", end="")

        sock.close()
        clear_current_line()
        print(f"\nDisconnected from {user}")
        return

    def send_msgs(self, sock, user, key):
        while True:
            msg = input("$ ")

            try:
                mac_send(sock, bytes(msg, "utf-8"), key)
            except (BrokenPipeError, OSError):
                break

            if not msg:
                break

        sock.close()
        return

    def login(self, host, port):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.server.connect((host, port))
        except ConnectionRefusedError:
            print("Failed to connect to server")
            return

        port = (self.server.getsockname()[1] + 1) % MAX_PORT
        Thread(target=self.get_keys, args=(port,)).start()

        self.server.send(bytes(self.user, "utf-8"))

        # challenge
        box = Box(self.private_key, self.server_public_key)
        decrypted_nonce = recv_dec(self.server, self.verify_key, box)

        if not decrypted_nonce:
            print("Error: Username must be alphanumeric")
            return

        # response
        sign_send(self.server, decrypted_nonce, self.signing_key)

        # server sym key
        self.sym_key = recv_dec(self.server, self.verify_key, box)

        self.get_clients()

    def get_keys(self, port):
        """Wait for an incoming connection from a peer"""
        self.keySock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.keySock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.keySock.settimeout(1)
        self.keySock.bind((HOST, port))
        self.keySock.listen()

        while self.program_running:
            try:
                conn, _ = self.keySock.accept()
            except socket.timeout:
                continue

            if self.connecting:
                mac_send(conn, b'n', self.sym_key)
                continue

            msg = recv_dec(conn, self.sym_key)

            if not msg:
                continue

            print("\nIncoming message. Press enter to receive.")
            self.running_shell = False

            user_bytes, key = msg.split(b":", 1)
            user = user_bytes.decode()

            if user in self.clients:
                self.clients[user][2] = key

            ans = input(f"Chat with {user}? ")

            if ans != "y":
                mac_send(conn, b"n", self.sym_key)
                Thread(target=self.shell).start()
                continue

            self.peer.close()
            self.peer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.peer.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.peer_key = key
            self.peer_name = user

            for port in range(MIN_PORT, MAX_PORT + 1):
                try:
                    self.peer.bind((HOST, port))
                    break
                except OSError:
                    continue

            self.peer.listen()
            msg = bytes(str(port), "utf-8")
            mac_send(conn, msg, self.sym_key)
            self.peer, addr = self.peer.accept()
            print(f"You are connected to user {user}")
            self.chat(self.peer, self.peer_name, self.peer_key)
            Thread(target=self.shell).start()

        return

    def get_clients(self):
        try:
            mac_send(self.server, b"?", self.sym_key)
        except BrokenPipeError:
            print("Not logged in")
            return

        client_list = recv_dec(self.server, self.sym_key)

        if not client_list:
            print("Not logged in")
            return

        clients = client_list.decode().split("\n")

        # reset
        self.clients = {}

        for client in clients:
            name, ip, port = client.split(":")

            if name in self.clients:
                self.clients[name][0] = ip
                self.clients[name][1] = port
            else:
                self.clients[name] = [ip, port, None]

        printable_clients = [k for k in self.clients if k != self.user]

        print(f"List of available clients: {', '.join(printable_clients)}")

    def get_key(self, user):
        mac_send(self.server, bytes(user, "utf-8"), self.sym_key)
        msg = recv_dec(self.server, self.sym_key)

        if not msg:
            return None, None

        port_bytes, key = msg.split(b":", 1)
        port = port_bytes.decode()

        if not key or not port:
            return key, port

        self.clients[user][2] = key
        return key, port

    def die(self):
        try:
            self.server.shutdown(1)
            self.server.close()
        except:
            pass


def main():
    parser = argparse.ArgumentParser("Client application")
    parser.add_argument("--host", default="localhost", help="Location of server")
    parser.add_argument(
        "--port",
        type=port,
        default=8000,
        help="Port that server is running on",
    )
    parser.add_argument("user", help="Name of the user logging in")
    args = parser.parse_args()

    c = Client(args.user)
    c.start(args.host, args.port)


if __name__ == "__main__":
    main()
