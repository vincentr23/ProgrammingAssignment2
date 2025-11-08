import socket
import sys
import threading

lock = threading.Lock()

class Server:
    def __init__(self, id, ip = 0, port = 0):
        self.ip = ip
        self.port = port
        self.id = id
        self.servers = []
        self.neighbors = []
        self.up = False
        # routing table
        self.rt = {str(id): 0}
        self.server_thread = ''

    def __str__(self):
        return f'ID: {self.id} | IP: {self.ip} | Port: {self.port} | Routing Table: {self.rt}'

server_info = Server(1)


def server(ip, port):
    global server_info, lock
    try:
        # create UDP socket
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # attach to specified port and ip
        server_address = (ip, port)
        server_socket.bind(server_address)

        print(f'Server is listening on {ip}:{port}')
    except:
        print('Error while spooling up server')
        return
    with lock:
        server_info.up = True

    while(True):
        # receive messages
        data, client_address = server_socket.recvfrom(1024)  # buffer size 1024 bytes
        print(f"Received {data.decode()} from {client_address}")
        server_socket.sendto(b"Success", client_address)

# handles ingesting file
def handle_file(lines):
    global server_info
    # reads first two lines
    try:
        num_servers = int(lines[0])
        num_edges = int(lines[1])
    except:
        print('File is not setup correctly')
        return
    # reads server/neighbor lines
    try:
        with lock:
            for i in range(num_servers):
                if (i+1) != server_info.id:
                    server_info.rt[str(i + 1)] = float('inf')
                next_server = lines[2+i].split()
                server_info.servers.append((int(next_server[0]), next_server[1], int(next_server[2])))
                if int(next_server[0]) == server_info.id:
                    server_info.ip = next_server[1]
                    server_info.port = int(next_server[2])
            for i in range(num_edges):
                next_nb = lines[2 + num_servers + i].split()
                if int(next_nb[0]) == server_info.id:
                    server_info.neighbors.append((int(next_nb[1]), int(next_nb[2])))
    except Exception as e:
        print(f'Error reading file: {e}')
        return
    with lock:
        for i in server_info.neighbors:
            server_info.rt[str(i[0])] = i[1]


def handle_command(command):
    if command[0] == 'server':
        if len(command) != 3:
            print('Command should be "server -t <topology-file-name> -i <routing-update-interval>"')
            return
        try:
            interval = int(command[2])
        except ValueError:
            print('Interval (-i) must be a number!')
            return
        try:
            with open(command[1], 'r', encoding='utf-8') as file:
                lines = file.readlines()
                handle_file(lines)
                # create a thread to handle the server
                server_info.server_thread = threading.Thread(target=server,
                        args=(server_info.ip, server_info.port), daemon=True)
                server_info.server_thread.start() # start the thread
                print(server_info)
                print('Server setup successful!')
        except FileNotFoundError:
            print(f'Error: The file {command[1]} was not found.')
        except Exception as e:
            print(f"An error occurred: {e}")

def main():
    while True:
        message = input('>')
        if len(message) == 0:
            continue
        command = message.split()
        handle_command(command)


main()