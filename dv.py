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
        self.servers = [] # all servers in network (id, ip, port)
        self.neighbors = [] # only neighbor ids
        self.up = False
        # routing table {destination_id (str): cost (int/inf)}
        self.rt = {str(id): 0}
        # direct link costs {neighbor_id (str): cost (int/inf)} for fast lookup
        self.direct_costs = {}
        self.server_thread = ''

    def __str__(self):
        return f'id: {self.id} | ip: {self.ip} | port: {self.port} | routing table: {self.rt}'

    def get_direct_link_cost(self, neighbor_id):
        """
        retrieves the current direct link cost to a neighbor.
        returns float('inf') if it's not a known neighbor.
        """
        neighbor_id_str = str(neighbor_id)
        if neighbor_id_str in self.direct_costs:
            return self.direct_costs[neighbor_id_str]
        
        return float('inf')

    def crash(self):
        """
        simulates a server crash by setting all direct link costs to infinity
        and stopping the server thread.
        """
        # set all direct link costs to infinity
        for neighbor_id in list(self.direct_costs.keys()):
            self.direct_costs[neighbor_id] = float('inf')
            # the routing table entry to the neighbor must also reflect the link cost change
            self.rt[neighbor_id] = float('inf') 
        
        # signal the server loop to stop
        self.up = False
        # note: the server thread may take a moment to exit after the loop breaks.


# global server info
server_info = Server(1)


def server(ip, port):
    global server_info, lock
    try:
        # create udp socket
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # attach to specified port and ip
        server_address = (ip, port)
        server_socket.bind(server_address)

        print(f'server is listening on {ip}:{port}')
    except:
        print('error while spooling up server')
        return
    
    with lock:
        server_info.up = True

    while(server_info.up):
        try:
            # receive messages
            server_socket.settimeout(0.5) # small timeout to check the 'up' flag
            data, client_address = server_socket.recvfrom(1024) # buffer size 1024 bytes
            print(f"received {data.decode()} from {client_address}")
            server_socket.sendto(b"success", client_address)
        except socket.timeout:
            # this is expected when waiting for 'up' flag to change
            continue
        except Exception as e:
            # handle other exceptions during run (e.g. if the socket is closed)
            if server_info.up:
                print(f"server running error: {e}")
            break
            
    # cleanup:
    server_socket.close()
    print(f"server {server_info.id} socket closed and thread stopped.")

# handles ingesting file
def handle_file(lines):
    global server_info
    
    # read the network size and our immediate neighbor count
    try:
        num_servers = int(lines[0].strip())
        num_neighbors = int(lines[1].strip())
    except (IndexError, ValueError):
        print('error reading file headers (num-servers/num-neighbors). file format might be off.')
        return
    
    server_list_end_index = 2 + num_servers
    
    with lock:
        try:
            # clear out any old data before loading new topology
            server_info.servers.clear()
            server_info.rt.clear()
            server_info.neighbors.clear()
            server_info.direct_costs.clear()
            
            # read *all* server entries to build the network map and initial routing table
            for i in range(num_servers):
                line = lines[2 + i].strip().split()
                s_id = int(line[0])
                s_ip = line[1]
                s_port = int(line[2])
                
                # list of servers in network
                server_info.servers.append((s_id, s_ip, s_port))
                
                # initialize the routing table - bellman-ford setup
                if s_id == server_info.id:
                    # set self ip/ port
                    server_info.ip = s_ip
                    server_info.port = s_port
                    server_info.rt[str(s_id)] = 0
                else:
                    # initial cost to all non-neighbors is infinity
                    server_info.rt[str(s_id)] = float('inf')

            # read neighbor/edge entries to set direct link costs.
            for i in range(num_neighbors):
                line = lines[server_list_end_index + i].strip().split()
                
                # format is <server-id1> <server-id2> <cost>
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

                # record the neighbor id
                server_info.neighbors.append(neighbor_id)
                
                # update the direct_costs dictionary
                server_info.direct_costs[str(neighbor_id)] = cost
                
                # update the routing table
                server_info.rt[str(neighbor_id)] = cost

        except Exception as e:
            print(f'major error reading file or parsing entry. check line formats. details: {e}')
            return
    
    print(f"topology successfully loaded for server id: {server_info.id}. initial routing table established.")


def handle_command(command):
    global server_info
    
    # check for the correct startup command structure
    if command[0] == 'server':
        # expected format: server -t <file> -i <interval>
        if len(command) != 5 or command[1] != '-t' or command[3] != '-i':
            print('command should be "server -t <topology-file-name> -i <routing-update-interval>"')
            return

        file_name = command[2]
        interval_str = command[4]
        
        try:
            # interval = int(interval_str) # storing this for future use
            pass
        except ValueError:
            print('interval (-i) must be a number!')
            return
        
        try:
            with open(file_name, 'r', encoding='utf-8') as file:
                lines = file.readlines()
                handle_file(lines)

                # only start the server thread if ip/port were correctly set
                if server_info.ip != 0 and server_info.port != 0:
                    # create a thread to handle the server
                    server_info.server_thread = threading.Thread(target=server,
                                 args=(server_info.ip, server_info.port), daemon=True)
                    server_info.server_thread.start()
                    
                    print(server_info)
                    print('server setup successful!')
                else:
                    print('error: could not find server id, ip, or port in the topology file.')
                    
        except FileNotFoundError:
            print(f'error: the file {file_name} was not found.')
        except Exception as e:
            print(f"an error occurred: {e}")
            
    elif command[0] == 'crash':
        if len(command) != 1:
            print('crash error: usage is just "crash"')
            return
        
        if not server_info.up:
            print('crash error: server is already crashed or not running')
            return
            
        with lock:
            server_info.crash()
        
        print('crash success')
        print(f"server {server_info.id} has initiated crash sequence.")
    
    else:
        print(f'unknown command: {command[0]}')

def main():
    while True:
        message = input('>')
        if len(message) == 0:
            continue
        command = message.split()
        handle_command(command)


main()