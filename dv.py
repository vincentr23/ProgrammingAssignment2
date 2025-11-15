import socket
import sys
import threading
from threading import Timer
import json
import time

lock = threading.Lock()
INF = 65535 # integer representation of infinity for json serialization

class UnknownCommand(Exception):
    '''
    User entered a bad command
    Attributes:
        message -- explanation of error
    '''
    def __init__(self, message="UNKNOWN COMMAND"):
        self.message = message
        super().__init__(self.message)
    def __str__(self):
        return self.message
# a server class to handle all things server related
class Server:
    def __init__(self, id, ip = 0, port = 0):
        self.ip = ip
        self.port = port
        self.id = id
        self.interval = 0
        self.servers = [] # all servers in network (id, ip, port)
        self.neighbors = {} # only neighbor (id, cost)
        self.up = False
        # routing table {destination_id (str): cost (int/inf)}
        self.rt = {str(id): 0}
        self.route_to = {}
        # direct link costs {neighbor_id (str): cost (int/inf)} for fast lookup
        self.direct_costs = {}
        self.server_thread = None
        self.server_socket = None
        self.last_heard = {}
        self.watchdog_thread = None
        self.packets_received_count = 0

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
    '''
    def get_neighbor(self, _id):
        return next((n for n in self.neighbors if str(n[0])==str(_id)), None)
    '''
    def crash(self):
        global lock
        """
        simulates a server crash by stopping the server thread.
        (Original logic, relies on neighbor watchdog to detect failure)
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

    def update_link_cost(self, neighbor_id_str, new_cost):
        """
        updates the direct cost to a neighbor and triggers a route update.
        returns true if an update occurred, false otherwise.
        """
        # check if the neighbor id is a known neighbor
        if neighbor_id_str not in self.direct_costs:
            return False # not a neighbor, cannot update

        # check if the cost is actually changing
        current_cost = self.direct_costs.get(neighbor_id_str, float('inf'))
        if current_cost == new_cost:
            return False # cost is the same, no update needed

        # apply the new cost
        self.direct_costs[neighbor_id_str] = new_cost
        
        # the direct entry in the routing table must also reflect the new cost
        self.rt[neighbor_id_str] = new_cost
        
        # reset the last heard time if the link is now active (cost < INF)
        if new_cost != float('inf'):
            self.last_heard[neighbor_id_str] = time.time()

        return True # success, an update occurred

# since we can't send float('inf') over udp
def encode_rt(rt: dict) -> str:
    # replace float('inf') with the integer INF
    safe_rt = {k: (INF if v == float('inf') else int(v)) for k, v in rt.items()}
    return json.dumps(safe_rt)

def decode_rt(rt_json: str) -> dict:
    data = json.loads(rt_json)
    return {str(k): (float('inf') if int(v) == INF else int(v)) for k, v in data.items()}
    
# when we receive a shutdown message
def shutdown(_id):
    global server_info

    server_info.neighbors.pop(str(_id))
    server_info.rt[str(_id)] = float('inf')

# disable a link with given node
def disable(target_id):
    global server_info, lock
    try:
        target_id = int(target_id)
    except ValueError:
        print("disable ERROR")
        return

    with lock:
        try:
            # must be a direct neighbor
            target = server_info.neighbors[str(target_id)]
        except:
            print('Must be a neighbor in order to disable.')
            return

        # set direct link + routing-table row to infinity (keep the row)
        server_info.neighbors.pop(str(target_id))
        server_info.rt[str(target_id)] = float('inf')

        # mark as unheard so watchdog treats it as down
        server_info.last_heard[str(target_id)] = 0
        signal_neighbor_change(str(target_id), -1)

    print("disable SUCCESS")

# when we make a change to a neighbor, we want to let everyone else know
# so they can update their routing table
def signal_neighbors_change():
    # use a new socket for sending to avoid interrupting the main server_socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    message = 'refactor'
    for nid in server_info.neighbors.keys():
        ip,port = server_info.server_by_id(nid)
        sock.sendto(message.encode('utf-8'), (ip, port))
    time.sleep(1)
    refactor()

def signal_neighbor_change(_id, cost):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    message = f'refactor|{_id}|{cost}'
    ip,port = server_info.server_by_id(_id)
    sock.sendto(message.encode('utf-8'), (ip, port))
    time.sleep(1)
    refactor()

# use this to just reset our routing table
def refactor():
    global server_info,lock

    server_info.rt.clear()
    server_info.route_to.clear()


    # for each of our neighbors, we will set that as default
    for s in server_info.servers:
        server_info.rt[str(s[0])] = server_info.neighbors.get(str(s[0]), float('inf'))
        server_info.rt[str(server_info.id)] = 0
                
    time.sleep(1)
    send_all_rt()

# at each interval, it will ask all neighbors for their routing table
def interval_check():
    global server_info

    time.sleep(1)
    while server_info.up:
        # timer not needed, just sleep and call function in a loop
        send_all_rt()
        time.sleep(server_info.interval)
        
# gets routing tables and calculates routes
def get_tables(_id, table):
    global server_info, lock

    nid = str(_id)
    
    with lock:
        cost_to_neighbor = server_info.neighbors[nid]

        # iterate through all destinations in the neighbor's table
        for dest_id_str, neighbor_cost_to_dest in table.items():
            # skip invalid or self-loop entries
            if dest_id_str == str(server_info.id):
                continue

            # check if neighbor's cost to dest is infinity before adding
            if neighbor_cost_to_dest == float('inf'):
                new_cost = float('inf')
            else:
                # compute cost if we go via this neighbor (bellman-ford)
                new_cost = cost_to_neighbor + neighbor_cost_to_dest

            # current cost in our table
            current_cost = server_info.rt.get(dest_id_str, float('inf'))

            if new_cost < current_cost:
                server_info.rt[dest_id_str] = new_cost
                server_info.route_to[dest_id_str] = _id
                print(f"updated route to {dest_id_str} improved via {_id}: {current_cost} → {new_cost}")
            
# send our routing table to specified id
# sends as json
def send_rt(_id):
    global server_info

    ip, port = server_info.server_by_id(_id)
    if not ip:
        return # skip if server not found

    try:
        # use a new socket for sending to avoid interrupting the main server_socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rt_json = encode_rt(server_info.rt)
        message = f'recvrt|{server_info.id}|{rt_json}'
        sock.sendto(message.encode('utf-8'), (ip, port))
    
    except Exception as e:
        print(f'error occured while sending to {_id}: {e}')

# broadcasts routing table
def send_all_rt():
    global server_info
    
    # send to neighbors only
    for nb in server_info.neighbors.keys():
        send_rt(nb)

# listens for incoming messages
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
            
            message = data.decode('utf-8').split('|')

            # means we received a routing table
            if message[0] == 'recvrt':
                _id = int(message[1])
                rt_data = decode_rt(message[2])
                
                # update last heard *before* table calculation
                with lock:
                    server_info.last_heard[str(_id)] = time.time()
                    server_info.packets_received_count += 1
                
                get_tables(_id, rt_data)
                # output required on successful receipt of a route update
                print(f"received a message from server {_id}") 
            
            elif message[0] == 'refactor':
                if message[1] and message[2]:
                    try:
                        cost = int(message[2])
                        if cost < 0:
                            server_info.neighbors.pop(message[1])
                        else:
                            server_info.neighbors[message[1]] = cost
                    except:
                        print('Error occurred updating link')
                refactor()
            else:
                # for simple message receipt confirmation
                server_socket.sendto(b"success", client_address)

        except socket.timeout:
            # this is expected when waiting for 'up' flag to change
            continue
        except OSError as e:
            # e.g. socket closed, connection refused, etc.
            if server_info.up:
                # ignore icmp “port unreachable” noise on windows (error 10054)
                if getattr(e, "winerror", None) == 10054:
                    continue
                # socket may be closed by crash command, check 'up' status
                if 'bad file descriptor' in str(e).lower():
                    break
                print(f"server running os error: {e}")
            break
        except Exception as e:
            # handle other exceptions during run
            if server_info.up:
                print(f"server running general error: {e}")
            break
            
    # cleanup:
    # server_socket is already closed by crash or os error break
    print(f"server {server_info.id} socket closed and thread stopped.")

# handles if we've talked to a node recently
def watchdog_loop():
    global server_info, lock

    time.sleep(1)
    while server_info.up:
        now = time.time()
        # threshold is 3 intervals (required by dv protocol failure detection)
        threshold = 3 * server_info.interval
        # holds any nodes we haven't heard from
        trip_neighbors = []

        with lock:
            # checks to see if we've heard from neighbors
            for _id in server_info.neighbors.keys():
                nid = str(_id)
                last = server_info.last_heard.get(nid, 0)
                
                # if we hit threshold
                if now - last > threshold:
                    # check if the link is not already marked as inf
                    if server_info.rt[nid] != float('inf'):
                        trip_neighbors.append(nid)

            # here we will actually poison the route
            for nid in trip_neighbors:
                print(f'we have not heard from {nid} in 3 intervals. link cost set to infinity.')
                
                # poison the route to this neighbor
                # server_info.direct_costs[nid] = float('inf')
                server_info.rt[nid] = float('inf')
                server_info.neighbors.pop(nid, None)
                
                # initiate an update broadcast since the table has changed
                signal_neighbors_change()

        time.sleep(server_info.interval)

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
                server_info.neighbors[neighbor_id] = cost
                
                # update the direct_costs dictionary
                # server_info.direct_costs[str(neighbor_id)] = cost
                
                # update the routing table
                server_info.rt[str(neighbor_id)] = cost

            # initialize last_heard for all neighbors
            for nid in server_info.neighbors.keys():
                server_info.last_heard[str(nid)] = time.time()
        except Exception as e:
            print(f'major error reading file or parsing entry. check line formats. details: {e}')
            return
    
    print(f"topology successfully loaded for server id: {server_info.id}. initial routing table established.")

# self explanatory, handles commands from console
def handle_command(command):
    global server_info, lock
    
    # check for the correct startup command structure
    try:
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
                    else:
                        print('error: could not find server id, ip, or port in the topology file.')
                        
            except FileNotFoundError:
                print(f'error: the file {file_name} was not found.')
            except Exception as e:
                print(f"an error occurred: {e}")
                
        # handle link cost update
        elif command[0] == 'update':
            # expected format: update <server-ID1> <server-ID2> <Link Cost>
            if len(command) != 4:
                print('update error: usage is "update <server-ID1> <server-ID2> <Link Cost>"')
                return

            if not server_info.up:
                print('update error: server is not running.')
                return

            try:
                s1_id = int(command[1])
                s2_id = int(command[2])
                cost_str = command[3].lower()
                
                
                if cost_str == 'inf':
                    new_cost = float('inf')
                else:
                    new_cost = int(cost_str)
                    if new_cost < 0 or new_cost > INF:
                        print('update error: link cost must be a non-negative integer or "inf"')
                        return

            except ValueError:
                print('update error: server ids must be integers and link cost must be "inf" or an integer.')
                return

            # check if this server is involved in the link change
            local_id = server_info.id
            neighbor_id = 0
            if local_id == s1_id:
                neighbor_id = s2_id
            elif local_id == s2_id:
                neighbor_id = s1_id
            else:
                print('update error: this server is not part of the link being updated.')
                return

            neighbor_id_str = str(neighbor_id)

            # update neighbor or add to neighbor
            server_info.neighbors[neighbor_id_str] = new_cost
            signal_neighbor_change(neighbor_id, new_cost)

        # crashes server        
        elif command[0] == 'crash':
            if len(command) != 1:
                print('crash error: usage is just "crash"')
                return
            
            if not server_info.up:
                print('crash error: server is already crashed or not running')
                return
                
            server_info.crash()
            
            print('')
            print(f"server {server_info.id} has initiated crash sequence.")
        
        elif command[0] == 'step':
            if len(command) != 1:
                print('step error: usage is just "step"')
                return
            
            if not server_info.up:
                print('step error: server is not running.')
                return
                
            send_all_rt()
            print('step success: routing table sent to all neighbors.')
            
        
        elif command[0] == 'packets':
            if len(command) != 1:
                print('packets error: usage is just "packets"')
                return
                
            current_count = server_info.packets_received_count
            print(f"received {current_count} distance vector packets since last display.")
            
            # Reset the counter
            server_info.packets_received_count = 0
            
        # disable 
        elif command[0] == 'disable':
            if len(command) != 2:
                print('disable error: usage is "disable <server-ID>"')
                return
            disable(command[1])
            
        elif command[0] == 'display':
            routes = server_info.rt
            print(server_info.rt)
            print('--------Routing Table--------')
            for i in range(len(routes)):
                r, sr = i + 1, str(i+1)
                hop = server_info.route_to.get(sr, sr)
                print(f'  Dest: {r}  Hop: {hop}  Cost: {routes.get(sr)}')
            print('-----------------------------')
        
        else:
            raise UnknownCommand()
        
        print(f'{" ".join(command)} SUCCESS')
    except Exception as e:
        print(f'{" ".join(command)} ERROR: {e}')

def main():
    global server_info,lock
    while True:
        message = input('>')
        if len(message) == 0:
            continue
        command = message.split()
        if command[0] == 'exit':
            
            if server_info.up:
                # server is running stop the thread
                with lock:
                    server_info.up = False
                
            return
        handle_command(command)

# global server info
server_info = Server(1)

main()