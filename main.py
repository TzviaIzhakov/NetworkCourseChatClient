import sys
from chat_client import ChatClient, HOST, PORT


def main() -> None:
    """
    Client program entry point.

    Flow:
    1) Create a ChatClient instance configured with HOST and PORT.
    2) Connect to the server (TCP).
    3) Ask the user for a name (or take it from command-line arguments).
    4) Perform HELLO handshake in a retry loop until:
       - success ("OK"), or
       - fatal exit ("EXIT") such as server full / connection lost.
    5) Start the receiver thread so incoming messages are printed immediately.
    6) Run the interactive CLI loop until the user quits or the connection drops.
    """

    # Create the client instance (holds socket + state)
    client = ChatClient(HOST, PORT)

    # 1) Connect to server
    if not client.connect():
        # If connection fails, stop the program
        return

    # 2) Choose name:
    #    - If the user provided a name as a command-line argument, use it.
    #    - Otherwise, ask via input().
    name = sys.argv[1].strip() if len(sys.argv) > 1 else input("Enter client name: ").strip()

    # 3) Handshake retry loop
    # The server can reject a name (taken/invalid), so we keep asking until it succeeds.
    while True:
        # Validate locally: prevent sending empty names to server
        if not name:
            name = input("Enter client name: ").strip()
            if not name:
                print("[System] Name cannot be empty. Please enter a name.")
                continue

        # Perform HELLO handshake (client sends HELLO, server replies OK/ERR)
        status = client.handshake(name)

        # Success: proceed to chat
        if status == "OK":
            break

        # Fatal: server full / connection issue -> end program completely
        if status == "EXIT":
            return

        # Otherwise status == "RETRY": ask for another name
        name = input("Invalid / taken name. Choose another name: ").strip()

    # 4) Start background receiver thread (prints incoming messages while we type)
    client.start_receiver()

    # 5) Run the command-line interface loop
    client.run_cli()


if __name__ == "__main__":
    main()
