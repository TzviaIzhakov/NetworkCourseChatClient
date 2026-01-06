import socket
import threading
import sys
from typing import Optional, List


HOST = "192.168.50.57"
PORT = 9000


class ChatClient:
    """
    Simple TCP chat client.

    Format:
    - HELLO <name>\n                 -> handshake
    - OK ... / ERR ...               -> server replies
    - TO <target>\n                  -> set active chat target (client-side)
    - TO <target> <message>\n        -> send message to target
    - END <target>\n                 -> end chat (both sides via server)
    - FROM <sender> <message>\n      -> incoming message
    - SYS END <name>\n               -> server notification: chat ended
    - ERR User '<name>' not found... -> server error about availability
    """

    def __init__(self, host: str, port: int) -> None:
        """
           Initialize the client object and its shared state.

           Args:
               host (str): Server IP address to connect to.
               port (int): Server TCP port to connect to.
        """

        self.host = host
        self.port = port

        # TCP socket used to communicate with the server (single connection per client)
        self.sock: socket.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        # Shared state between the main thread (user input / sending)
        # and the receiver thread (listening for server messages).
        self._lock = threading.Lock()

        # Connection flag: True only after successfully connecting + completing HELLO handshake
        self._connected: bool = False

        # The active chat target (chat-mode).
        # If None, the client is not currently chatting with anyone.
        # Example:
        # If Client1 types: TO Client2
        # then _current_target becomes: "Client2"
        self._current_target: Optional[str] = None

        # History stack of previous targets.
        # Used to "go back" to the previous chat when END is called (or when a user disconnects).
        self._target_stack: List[str] = []

        # Client's name (sent to the server during HELLO handshake)
        self.name: str = ""

    # ----------------------------
    # Connection / Handshake
    # ----------------------------
    def connect(self) -> bool:
        """
           Connect to the server (TCP connection).

           Returns:
               bool: True if the TCP connection was established successfully,
                     False otherwise (e.g., server is down / wrong IP / network error).
        """
        try:
            # Create the TCP connection to (host, port).
            # If it fails -> OSError (e.g., timeout, unreachable host, refused).
            self.sock.connect((self.host, self.port))
        except OSError as e:
            # Print a clear error and report failure to the caller.
            print(f"[System] Connection failed: {e}")
            return False

        # Mark client as connected (shared state used by multiple threads).
        # We lock because the receiver thread / main thread may read this flag too.
        with self._lock:
            self._connected = True
        return True

    def handshake(self, name: str) -> str:
        """
            Perform the initial HELLO handshake with the server.

            Protocol:
                Client sends:  HELLO <name>\n
                Server replies:
                    - OK Welcome <name>                 -> success
                    - ERR Name already taken / invalid  -> choose another name
                    - ERR Server full (...)             -> server at capacity (exit)

            Args:
                name (str): The requested client name.

            Returns:
                "OK"    -> handshake succeeded (self.name is set)
                "RETRY" -> name is empty / invalid / already taken (ask user for another name)
                "EXIT"  -> fatal error (server full / connection problem) => stop program
        """

        # Remove whitespace from the user's input
        name = name.strip()

        # Validate: empty names are not allowed
        if not name:
            print("[System] Name cannot be empty.")
            return "RETRY"

        # Send HELLO command to the server.
        # If sending fails, it usually means the connection is broken.
        if not self._safe_send(f"HELLO {name}\n"):
            return "EXIT"

        # Receive the server reply (OK / ERR / etc.)
        try:
            reply = self.sock.recv(1024).decode("utf-8", errors="ignore").strip()
        except OSError as e:
            # Socket error while receiving
            # disconnect
            print(f"[System] Failed to receive server reply: {e}")
            self._set_disconnected()
            return "EXIT"

        # Print server feedback for the user
        print(reply)

        # If the server is full, there is nothing the client can do right now.
        # The server typically closes the connection after sending this error.
        if "Server full" in reply:
            print("[System] Server is full. Try again later.")
            self.close()
            return "EXIT"

        # Any ERR means the name is taken or invalid -> user should pick a different name
        if reply.startswith("ERR"):
            return "RETRY"

        # OK means registration succeeded -> store the final name
        if reply.startswith("OK"):
            self.name = name
            return "OK"

        # Any unexpected reply -> ask for a new name (safe fallback)
        return "RETRY"

    def start_receiver(self) -> None:
        """
           Start a background thread that continuously receives messages from the server.

           - The main thread is blocked on `input()` waiting for the user to type.
           - Without a receiver thread, incoming messages would not be printed until after
             the user presses Enter.

           The thread runs as a daemon so it won't prevent the program from exiting
           when the main thread finishes.
        """

        t = threading.Thread(target=self._recv_loop, daemon=True)
        t.start()

    # ----------------------------
    # Core send/recv helpers
    # ----------------------------
    def _set_disconnected(self) -> None:
        """
           Mark the client as disconnected and reset chat-related state.

           this function called when we detect that the server connection is broken (recv/send error).
           We clear current chat information because it is no longer valid once disconnected.
        """
        with self._lock:
            self._connected = False
            self._current_target = None
            self._target_stack.clear()

    def _safe_send(self, text: str) -> bool:
        """
           Send data to the server safely.

           Args:
               text (str): The text to send (should include '\n' at the end for line-based protocol).

           Returns:
               bool: True if send succeeded, False if the connection is lost.

           Notes:
               If sending fails (broken pipe / reset / socket error), we update the state to
               disconnected and return False so the caller can stop the program gracefully.
        """
        try:
            self.sock.sendall(text.encode("utf-8"))
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            self._set_disconnected()
            print("[System] Failed to send (connection lost).")
            return False

    def _recv_loop(self) -> None:
        """
        Background receiver loop (runs in a daemon thread).

        This loop continuously reads data from the server socket, reconstructs full lines
        (newline-terminated), and prints server messages to the terminal.

        It also handles special protocol messages:
        - FROM <sender> <message>  : incoming private message notification
        - SYS END <sender>         : the other side ended the current chat
        - ERR User '<name>' ...    : target user is unavailable (not found/disconnected)

        If the socket is closed or a network error occurs, the client is marked as disconnected
        and the receiver thread exits.
        """
        buffer = ""  # TCP stream buffer (recv may return partial/multiple lines)

        while True:
            try:
                # Read raw bytes from the server (blocking call)
                data = self.sock.recv(1024)
            except OSError:
                # Socket error => treat as unexpected disconnect
                self._set_disconnected()
                print("[System] Disconnected from server (socket error).")
                break

            if not data:
                # Server closed the connection (recv returned 0 bytes)
                self._set_disconnected()
                print("[System] Server closed the connection.")
                break

            # Append received bytes to buffer and decode as UTF-8
            buffer += data.decode("utf-8", errors="ignore")

            # Process complete lines (newline-delimited protocol)
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue

                # Incoming message does NOT auto-switch the active chat that means that if Client1 wants
                # to talk with Client2 and Client2 wants to replay to Client1 he needs to type TO Client1
                # The user must explicitly choose who to talk to using: TO <sender>
                if line.startswith("FROM "):
                    parts = line.split(" ", 2)  # ["FROM", "Client1", "hello ..."]
                    sender = parts[1].strip() if len(parts) >= 2 else ""
                    if sender:
                        print(f"[System] New message from {sender}. Use: TO {sender} to reply.")

                # System notification: SYS END <sender>
                # Meaning: the other client (sender) ended the chat on their side and the server notifies us.
                if line.startswith("SYS END "):
                    sender = line[len("SYS END "):].strip()
                    # We lock because _current_target and _target_stack are shared between threads
                    # (main thread may read/write them while this receiver thread updates them).
                    with self._lock:
                        # If we are currently chatting with the sender who ended the chat,
                        # we must close this chat-mode locally as well.
                        if self._current_target == sender:

                            # If we have a previous chat in the stack, return to it automatically.
                            # Example: Client2 was chatting with Client1 -> switched to Client3 -> END with Client3
                            # Then go back to Client1.
                            if self._target_stack:
                                back_to = self._target_stack.pop()
                                self._current_target = back_to
                                print(f"[System] {sender} ended the chat. Back to chat with {back_to}.")

                            # Otherwise, there is no previous chat to return to, so we exit chaא.
                            else:
                                self._current_target = None
                                print(f"[System] {sender} ended the chat. Chat closed.")
                        # If we were NOT chatting with that sender, this is just an informational message.
                        # (e.g., we ended a different chat, or sender ended a chat that isn't currently active for us)
                        else:
                            print(f"[System] {sender} ended the chat.")
                    continue

                # If the server reports that a user is unavailable and we are currently chatting with them,
                # we close the current chat-mode locally (because sending to them will no longer work).

                # Examples of server messages this handles:
                #   ERR User 'Client3' not found
                #   ERR User 'Client3' disconnected
                if line.startswith("ERR User '") and ("' not found" in line or "' disconnected" in line):
                    try:
                        # Extract the username from the error message between quotes: '...'
                        user = line.split("'", 2)[1]
                    except Exception:
                        user = None

                    if user:
                        # Lock because _current_target and _target_stack are shared state between threads.
                        with self._lock:
                            # Only close chat if the unavailable user is our current active target.
                            if self._current_target == user:
                                # If we have a previous target stored, return to it automatically.
                                if self._target_stack:
                                    back_to = self._target_stack.pop()
                                    self._current_target = back_to
                                    print(f"[System] Chat closed: {user} is unavailable. Back to chat with {back_to}.")
                                # Otherwise, there is no previous chat to return to -> exit chat-mode.
                                else:
                                    self._current_target = None
                                    print(f"[System] Chat closed: {user} is unavailable.")

                # Print the raw server line too (ERR / FROM / SENT / etc.)
                print(line)

    # ----------------------------
    # Chat state operations
    # ----------------------------
    def is_connected(self) -> bool:
        """
           Check if the client is currently connected to the server.

           Returns:
               bool: True if connected, False otherwise.
        """
        with self._lock:
            return self._connected

    def get_current_target(self) -> Optional[str]:
        """
            Get the current active chat target.

            Returns: The target username if a chat is active, otherwise None.
        """
        with self._lock:
            return self._current_target

    def open_chat(self, target: str) -> None:
        """
            Enter chat-mode with a specific target.

            Behavior:
            - Sets `_current_target` to `target`.
            - If there was a previous target and it's different, it is pushed into `_target_stack`
              so we can "go back" to it later (e.g., after END).

            Args:
                target (str): The username we want to chat with.
        """
        target = target.strip()
        if not target:
            return
        # Update shared state safely (main thread + receiver thread)
        with self._lock:
            prev = self._current_target # remember who we were chatting with before
            # If we are switching to a different target, save the previous target in a stack.
            # This allows the client to return to the previous chat automatically after END.
            if prev and prev != target:
                self._target_stack.append(prev)
            # Set the new active chat target
            self._current_target = target
        print(f"Now chatting with {target}. Type END to stop.")

    def end_current_chat(self) -> bool:
        """
        End the currently active chat session.

        What it does:
        1) Reads the current active target (_current_target).
        2) Sends "END <target>" to the server so the server notifies the other user too
           (both sides exit chat-mode).
        3) Locally closes chat-mode and, if possible, returns to the previous chat target
           using the stack (_target_stack).

        Returns:
            bool: True  -> END processed successfully (or there was no active chat to end)
                  False -> failed to send END because the connection is lost
        """

        # Read the active target safely (shared state between threads)
        with self._lock:
            target = self._current_target

        # If there is no active chat, nothing to end
        if not target:
            print("No active chat to end.")
            return True

        # Inform the server to end the chat with this target on BOTH sides
        # (server will send SYS END <me> to the other client).
        if not self._safe_send(f"END {target}\n"):
            return False

        # Locally exit chat-mode:
        # If we have a previous target saved, return to it automatically.
        with self._lock:
            if self._target_stack:
                back_to = self._target_stack.pop()
                self._current_target = back_to
                print(f"[System] Back to chat with {back_to}.")
            else:
                self._current_target = None
                print("Chat closed.")

        return True

    def send_to_current(self, message: str) -> bool:
        """
        Send a text message to the currently selected chat target (chat-mode).

        Behavior:
        - Reads the current active target (_current_target).
        - If no target is selected, prints a helpful message and does not send anything.
        - Otherwise, sends the command in the required protocol format:
            TO <target> <message>

        Args:
            message (str): The message content to send (can include spaces).

        Returns:
            bool: True  -> message was sent successfully OR no target was selected (no-op)
                  False -> sending failed because the connection is lost
        """

        # Read shared state safely (receiver thread may change _current_target)
        with self._lock:
            target = self._current_target

        # If the user is not currently chatting with anyone, we cannot send a plain message.
        if not target:
            print("No active target. Use: TO <target> (or TO <target> <message>).")
            return True

        # Send to server using the protocol: TO <target> <message>\n
        return self._safe_send(f"TO {target} {message}\n")

    def send_one_off(self, raw_to_command: str) -> bool:
        """
        Send a one-off private message without changing the active chat target.

        This method expects the caller to provide a full protocol line in the format:
            TO <target> <message>

        Example:
            send_one_off("TO Client3 hello")

        Returns:
            bool: True if sent successfully, False if the connection is lost.
        """
        # Ensure newline terminator for the server's line-based parsing
        if not raw_to_command.endswith("\n"):
            raw_to_command += "\n"

        return self._safe_send(raw_to_command)

    def close(self) -> None:
        """
        Close the client socket and reset connection/chat state.

        This is used when the user exits or when we detect a fatal connection issue.
        We wrap close() with try/except because closing an already-broken socket may raise OSError.
        """
        try:
            self.sock.close()
        except OSError:
            pass

        # Mark the client as disconnected and clear chat-related state
        self._set_disconnected()

    # ----------------------------
    # CLI
    # ----------------------------
    def run_cli(self) -> None:
        """
        Run the interactive command-line interface (CLI).

        This method:
        - Prints available commands and usage examples.
        - Repeatedly reads user input from the terminal.
        - Converts user input into protocol commands sent to the server:
            * TO <target>            -> set active chat target (chat-mode)
            * TO <target> <message>  -> send one-off message (does not change active target)
            * END                    -> end current chat on both sides
            * exit / quit            -> disconnect and exit
        - If the connection is lost, the loop stops and the client closes gracefully.
        """

        # Print help/usage for the user
        print("\nCommands:")
        print("TO <target>              -> open chat with target (set default)")
        print("TO <target> <message>    -> send one message to target (one-off)")
        print("END                      -> end current chat on BOTH sides (no blocking)")
        print("exit / quit              -> disconnect\n")
        print("Note: If someone messages you (FROM X ...), the chat auto-opens with X.\n")

        while True:
            # If connection dropped (detected by receiver thread / send failures), exit the CLI.
            if not self.is_connected():
                print("[System] Connection lost. Exiting...")
                break

            # Build the prompt: show active target if we are in chat-mode
            current_target = self.get_current_target()
            prompt = "> " if not current_target else f"[to {current_target}]> "

            # Read user command / message from terminal
            user_input = input(prompt).strip()
            if not user_input:
                continue

            # Exit command
            low = user_input.lower()
            if low in ("exit", "quit"):
                break

            # END: end current chat on both sides via the server
            if user_input.upper() == "END":
                if not self.end_current_chat():
                    break
                continue

            # TO: either open chat-mode or send a one-off message
            if user_input.upper().startswith("TO "):
                parts = user_input.split(maxsplit=2)

                # "TO <target>" -> open chat-mode (set active target)
                if len(parts) == 2:
                    target = parts[1].strip()
                    if not target:
                        print("Usage: TO <target> [message]")
                        continue
                    self.open_chat(target)
                    continue

                # "TO <target> <message>" -> one-off send (does not change active target)
                else:
                    if not self.send_one_off(user_input):
                        break
                    continue

            # Any other text is treated as a normal chat message for the current active target
            if not self.send_to_current(user_input):
                break

        # On exit, close socket and cleanup state
        self.close()
        print(f"[{self.name or 'Client'}] Disconnected.")




