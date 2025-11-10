import socket
import sys
import threading

lock = threading.Lock()

# a server class to handle all things server related
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

# global server
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

    while(server_info.up):
        # receive messages
        data, client_address = server_socket.recvfrom(1024)  # buffer size 1024 bytes
        print(f"Received {data.decode()} from {client_address}")
        server_socket.sendto(b"Success", client_address)

# handles ingesting file
def handle_file(lines):
    global server_info
    
    #read the network size and our immediate neighbor count
    try:
        num_servers = int(lines[0].strip())
        num_neighbors = int(lines[1].strip()) 
    except (IndexError, ValueError):
        print('Error reading file headers (num-servers/num-neighbors). File format might be off.')
        return
    
    server_list_end_index = 2 + num_servers
    
    with lock:
        try:
            # clear out any old data before loading new topology
            server_info.servers.clear()
            server_info.rt.clear()
            server_info.neighbors.clear()
            
            #  read *all* server entries to build the network map and initial routing table
            for i in range(num_servers):
                line = lines[2 + i].strip().split()
                s_id = int(line[0])
                s_ip = line[1]
                s_port = int(line[2])
                
                # list of servers in network
                server_info.servers.append((s_id, s_ip, s_port))
                
                # initialize the routing table - Bellman-Ford setup
                if s_id == server_info.id:
                    # set self IP/ port
                    server_info.ip = s_ip
                    server_info.port = s_port
                    server_info.rt[str(s_id)] = 0
                else:
                    server_info.rt[str(s_id)] = float('inf')

            # read NEIGHBOR/EDGE entries to set direct link costs.
            for i in range(num_neighbors):
                line = lines[server_list_end_index + i].strip().split()
                
                # format is <server-ID1> <server-ID2> <cost>
                s1_id = int(line[0])
                s2_id = int(line[1])
                cost = int(line[2])
                
                
                if s1_id == server_info.id:
                    neighbor_id = s2_id
                elif s2_id == server_info.id:
                    neighbor_id = s1_id
                else:
                    # safety check
                    continue 

                # record the neighbor ID and its initial link cost
                server_info.neighbors.append((neighbor_id, cost))
                
                # update the routing table
                server_info.rt[str(neighbor_id)] = cost

        except Exception as e:
            print(f'Major error reading file or parsing entry. Check line formats. Details: {e}')
            return
    
    print(f"Topology successfully loaded for Server ID: {server_info.id}. Initial routing table established.")


def handle_command(command):
    global server_info
    
    # check for the correct startup command structure
    if command[0] == 'server':
        # Expected format: server -t <file> -i <interval>
        if len(command) != 5 or command[1] != '-t' or command[3] != '-i':
            print('Command should be "server -t <topology-file-name> -i <routing-update-interval>"')
            return

        file_name = command[2]
        interval_str = command[4]
        
        try:
            interval = int(interval_str)
        except ValueError:
            print('Interval (-i) must be a number!')
            return
        
        try:
            with open(file_name, 'r', encoding='utf-8') as file:
                lines = file.readlines()
                handle_file(lines)

                # Only start the server thread if IP/Port were correctly set
                if server_info.ip != 0 and server_info.port != 0:
                    # create a thread to handle the server
                    server_info.server_thread = threading.Thread(target=server,
                                args=(server_info.ip, server_info.port), daemon=True)
                    server_info.server_thread.start() 
                    # start the thread

                    print(server_info)
                    print('Server setup successful!')
                else:
                    print('Error: Could not find server ID, IP, or Port in the topology file.')
                    
        except FileNotFoundError:
            print(f'Error: The file {file_name} was not found.')
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