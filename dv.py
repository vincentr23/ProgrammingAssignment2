import socket
import sys
import threading
from threading import Timer
import json
import time

lock = threading.Lock()
INF = 65535

# a server class to handle all things server related
class Server:
    def __init__(self, id, ip = 0, port = 0):
        self.ip = ip
        self.port = port
        self.id = id
        self.interval = 0
        self.servers = [] # all servers in network (id, ip, port)
        self.neighbors = [] # only neighbor ids
        self.up = False
        # routing table {destination_id (str): cost (int/inf)}
        self.rt = {str(id): 0}
        # direct link costs {neighbor_id (str): cost (int/inf)} for fast lookup
        self.direct_costs = {}
        self.server_thread = None
        self.server_socket = None
        self.last_heard = {}
        self.watchdog_thread = None

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
        global lock
        """
        simulates a server crash by stopping the server thread.
        """
        with lock:
            # signal the server loop to stop
            self.up = False
            try: self.server_socket.close()  # this interrupts recvfrom()
            except: pass
        # note: the server thread may take a moment to exit after the loop breaks.
    
    def server_by_id(self, _id):
        for sid, ip, port in self.servers:
            if sid == _id:
                return ip, port
        
        return None, None
    
# Since we can't send float('inf') over UDP
def encode_rt(rt: dict) -> str:
    # Replace float('inf') with the integer INF
    safe_rt = {k: (INF if v == float('inf') else int(v)) for k, v in rt.items()}
    return json.dumps(safe_rt)

def decode_rt(rt_json: str) -> dict:
    data = json.loads(rt_json)
    return {str(k): (float('inf') if int(v) == INF else int(v)) for k, v in data.items()}
    

# global server info
server_info = Server(1)

# at each interval, it will ask all neighbors for their routing table
def interval_check():
    global server_info

    time.sleep(1)
    while server_info.up:
        t = Timer(server_info.interval, send_all_rt())
        time.sleep(server_info.interval)
       
# gets routing tables and calculates routes
def get_tables(_id, table):
    global server_info

    neighbor_id_str = str(_id)
    cost_to_neighbor = server_info.direct_costs.get(neighbor_id_str, float('inf'))

    # iterate through all destinations in the neighbor's table
    for dest_id_str, neighbor_cost_to_dest in table.items():
        # skip invalid or self-loop entries
        if dest_id_str == str(server_info.id):
            continue

        # compute cost if we go via this neighbor
        new_cost = cost_to_neighbor + neighbor_cost_to_dest

        # current cost in our table
        current_cost = server_info.rt.get(dest_id_str, float('inf'))

        if new_cost < current_cost:
            server_info.rt[dest_id_str] = new_cost
            print(f"Updated route to {dest_id_str} improved via {_id}: {current_cost} → {new_cost}")

# send our routing table to specified id
# sends as json
def send_rt(_id):
    global server_info

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rt_json = encode_rt(server_info.rt)
        message = f'recvrt|{server_info.id}|{rt_json}'
        ip, port = server_info.server_by_id(_id)
        sock.sendto(message.encode('utf-8'), (ip, port))
    
    except Exception as e:
        (f'Error occured: {e}')

# broadcasts routing table
def send_all_rt():
    global server_info

    for nb in server_info.neighbors:
        send_rt(nb)

def server(ip, port):
    global server_info, lock
    try:
        # create udp socket
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server_info.server_socket = server_socket

        # attach to specified port and ip
        server_address = (ip, port)
        server_socket.bind(server_address)

        print(f'server is listening on {ip}:{port}')
    except Exception as e:
        print(f'error while spooling up server: {e}')
        return
    
    with lock:
        server_info.up = True

    while(server_info.up):
        try:
            # receive messages
            server_socket.settimeout(0.5) # small timeout to check the 'up' flag
            data, client_address = server_socket.recvfrom(1024) # buffer size 1024 bytes
            #print(f"received {data.decode()} from {client_address}")
            server_socket.sendto(b"success", client_address)
            message = data.decode('utf-8').split('|')

            # means we received a routing table
            if message[0] == 'recvrt':
                _id = message[1]
                rt_data = decode_rt(message[2])
                get_tables(_id, rt_data)
                with lock:
                    server_info.last_heard[str(_id)] = time.time()

        except socket.timeout:
            # this is expected when waiting for 'up' flag to change
            continue
        except OSError as e:
            if getattr(e, "winerror", None) == 10054:
                # Ignore ICMP “port unreachable” noise on Windows
                continue
        except Exception as e:
            # handle other exceptions during run (e.g. if the socket is closed)
            if server_info.up:
                print(f"server running error: {e}")
            break
            
    # cleanup:
    server_socket.close()
    print(f"server {server_info.id} socket closed and thread stopped.")

# handles if we've talked to a node recently
def watchdog_loop():
    global server_info, lock

    time.sleep(1)
    while server_info.up:
        now = time.time()
        threshold = 3 * server_info.interval
        # holds any nodes we haven't heard from
        trip_neighbors = []

        # checks to see if we've heard from neighbors
        for nid in list(server_info.neighbors):
            last = server_info.last_heard.get(str(nid), 0)
            # if we hit threshold, we wanna mark it for removal
            # by adding to our array from earlier
            if now - last > threshold:
                with lock:
                    if server_info.rt[str(nid)] != float('inf'):
                        trip_neighbors.append(nid)

        # here we will actually remove
        for nid in trip_neighbors:
            print(f'We have not heard from {nid} in 3 intervals')
            server_info.neighbors.remove(nid)
            server_info.rt[str(nid)] = float('inf')



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

            for nid in server_info.neighbors:
                server_info.last_heard[str(nid)] = time.time()
        except Exception as e:
            print(f'major error reading file or parsing entry. check line formats. details: {e}')
            return
    
    print(f"topology successfully loaded for server id: {server_info.id}. initial routing table established.")

# send a message telling all other nodes we are shutting down
def shutdown():
    global server_info

    sock, _id = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), server_info.id
    msg = f'shutdown|{_id}'
    for i in server_info.neighbors:
        ip, port = server_info.server_by_id(i)
        sock.sendto(msg.encode('utf-8'), (ip,port))

# when we receive a shutdown message
def shutdown(_id):
    global server_info

    server_info.neighbors.remove(int(_id))
    server_info.direct_costs[_id] = float('inf')
    server_info.rt[_id] = float('inf')

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
            with lock:
                server_info.interval = int(interval_str) # storing this for future use
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
                    # another thread to watch our threads
                    server_info.watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True)
                    server_info.watchdog_thread.start()
                    # a thread to periodically send out our routing table
                    interval_thread = threading.Thread(target=interval_check, daemon=True)
                    interval_thread.start()
                    
                    print(server_info)
                    print('server setup successful!')
                else:
                    print('error: could not find server id, ip, or port in the topology file.')
                    
        except FileNotFoundError:
            print(f'error: the file {file_name} was not found.')
        except Exception as e:
            print(f"an error occurred: {e}")
    # crashes server        
    elif command[0] == 'crash':
        if len(command) != 1:
            print('crash error: usage is just "crash"')
            return
        
        if not server_info.up:
            print('crash error: server is already crashed or not running')
            return
            
        server_info.crash()
        
        print('crash success')
        print(f"server {server_info.id} has initiated crash sequence.")
    
    elif command[0] == 'display':
        print(server_info)
    
    else:
        print(f'unknown command: {command[0]}')

def main():
    global server_info,lock
    while True:
        message = input('>')
        if len(message) == 0:
            continue
        command = message.split()
        if command[0] == 'exit':
            if server_info:
                with lock:
                    server_info.up = False
            return
        handle_command(command)


main()