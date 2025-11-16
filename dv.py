import socket
import sys
import threading
from threading import Timer
import json
import time

# initialization
# Check for command line argument for server ID
if len(sys.argv) < 2:
    print("Usage: python dv.py <server-id>")
    sys.exit(1)

try:
    INITIAL_SERVER_ID = int(sys.argv[1])
except ValueError:
    print("Error: Server ID must be an integer.")
    sys.exit(1)



lock = threading.Lock()
INF = 65535 # integer representation of infinity for json serialization

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
        # Stores the immediate neighbor ID used to reach a destination: {dest_id: next_hop_id}
        self.next_hop = {str(id): str(id)}
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

    server_info.neighbors.remove(int(_id))
    server_info.direct_costs[_id] = float('inf')
    server_info.rt[_id] = float('inf')

def disable(target_id):
    global server_info, lock
    try:
        target_id_int = int(target_id)
        target_id_str = str(target_id)
    except ValueError:
        print("disable ERROR: Server ID must be an integer.")
        return

    with lock:
        # must be a direct neighbor
        if target_id_int not in server_info.neighbors:
            print("disable ERROR: Server is not a direct neighbor.")
            return

        # set direct link + routing-table row to infinity (keep the row)
        server_info.direct_costs[target_id_str] = float('inf')
        server_info.rt[target_id_str] = float('inf')

        # mark as unheard so watchdog treats it as down
        server_info.last_heard[target_id_str] = 0
    
   
    send_all_rt()

    
    send_link_update(target_id_int, 'inf')

    print("disable SUCCESS")

# global server info 
server_info = Server(INITIAL_SERVER_ID)

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

    neighbor_id_str = str(_id)
    
    with lock:
        cost_to_neighbor = server_info.direct_costs.get(neighbor_id_str, float('inf'))

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
            current_next_hop = server_info.next_hop.get(dest_id_str, None)

            # Bellman-Ford logic
            # Case 1: New path is cheaper
            if new_cost < current_cost:
                server_info.rt[dest_id_str] = new_cost
                # Update the next hop to be the current neighbor (_id)
                server_info.next_hop[dest_id_str] = str(_id) 
                # print(f"updated route to {dest_id_str} improved via {_id}: {current_cost} → {new_cost}")
            
            # Case 2: The current next hop is via this neighbor, but their cost to dest changed.
            elif current_next_hop == neighbor_id_str:
                if new_cost != current_cost:
                    # The cost via our current next hop changed. We must adopt the new cost.
                    server_info.rt[dest_id_str] = new_cost
                    # print(f"updated route to {dest_id_str} cost changed via {_id}: {current_cost} → {new_cost}")
            
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
        # Message format: recvrt|my_id|rt_json
        message = f'recvrt|{server_info.id}|{rt_json}'
        sock.sendto(message.encode('utf-8'), (ip, port))
    
    except Exception as e:
        # print(f'error occured while sending to {_id}: {e}')
        pass # suppress error if neighbor is down

# broadcasts routing table
def send_all_rt():
    global server_info
    
    # send to neighbors only
    for nb in server_info.neighbors:
        send_rt(nb)

# Sends a command to a neighbor to symmetrically update its local link cost.
def send_link_update(_id, new_cost_str):
    global server_info

    ip, port = server_info.server_by_id(_id)
    if not ip:
        return

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Message format: update_link|my_id|new_cost_to_me
        message = f'update_link|{server_info.id}|{new_cost_str}'
        sock.sendto(message.encode('utf-8'), (ip, port))
    
    except Exception as e:
        # print(f'error occured while sending link update to {_id}: {e}')
        pass # suppress error if neighbor is down


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

            # means we received a link cost update command from a neighbor
            elif message[0] == 'update_link':
                neighbor_id = int(message[1])
                new_cost_str = message[2].lower()

                # Convert cost string back to float('inf') or int
                if new_cost_str == 'inf':
                    new_cost = float('inf')
                else:
                    new_cost = int(new_cost_str)

                neighbor_id_str = str(neighbor_id)
                
                with lock:
                    # Update our local link cost TO the neighbor that sent the instruction (bidirectional change)
                    updated_locally = server_info.update_link_cost(neighbor_id_str, new_cost)
                
                if updated_locally:
                    # If our cost changed, we need to broadcast our new table
                    send_all_rt()
                    print(f"received link update from {neighbor_id}. Link cost to {neighbor_id} is now {new_cost_str}.")
            
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
            for nid in list(server_info.neighbors):
                nid_str = str(nid)
                last = server_info.last_heard.get(nid_str, 0)
                
                # if we hit threshold
                if now - last > threshold:
                    # check if the link is not already marked as inf
                    if server_info.rt.get(nid_str) != float('inf'): # Use .get for safety
                        trip_neighbors.append(nid)

            # here we will actually poison the route
            for nid in trip_neighbors:
                nid_str = str(nid)
                print(f'we have not heard from {nid_str} in {threshold} seconds. Link cost set to infinity.')
                
                # poison the route to this neighbor
                server_info.direct_costs[nid_str] = float('inf')
                server_info.rt[nid_str] = float('inf')
                
                # initiate an update broadcast since the table has changed
                send_all_rt() 

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
            server_info.next_hop.clear() # Clear next_hop tracking
            
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
                    server_info.next_hop[str(s_id)] = str(s_id) # Next hop to self is self
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
                server_info.next_hop[str(neighbor_id)] = str(neighbor_id) # Next hop to neighbor is neighbor

            # initialize last_heard for all neighbors
            for nid in server_info.neighbors:
                server_info.last_heard[str(nid)] = time.time()
        except Exception as e:
            print(f'major error reading file or parsing entry. check line formats. details: {e}')
            return
    
    print(f"topology successfully loaded for server id: {server_info.id}. initial routing table established.")


def display_routing_table():
    """
    Displays the current routing table, sorted by destination ID,
    in the requested format: destServerID- nextHopServerID- Cost
    """
    global server_info
    
    output_lines = []
    
    
    dest_ids = []
    for d in server_info.rt.keys():
        try:
            dest_ids.append(int(d))
        except ValueError:
            # Skip any non-integer keys
            continue
            
    sorted_dest_ids = sorted(dest_ids)

    
    print("---------ROUTING TABLE-----------") 
    print("destServerID- nextHopServerID- Cost")
    
    
    for dest_id in sorted_dest_ids:
        dest_id_str = str(dest_id)
        
        # Get the current cost and next hop
        cost = server_info.rt.get(dest_id_str, float('inf'))
        # If the next hop is not set, default to '-'
        next_hop_id = server_info.next_hop.get(dest_id_str, '-') 

        # Format the cost: replace float('inf') with "inf"
        display_cost = "inf" if cost == float('inf') else str(int(cost))
        
      
        line = f"{dest_id_str} {next_hop_id} {display_cost}" 
        output_lines.append(line)
        
    print("\n".join(output_lines))
    print("----------END ROUTING TABLE------")
 





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
        
        # Prevent starting the server if it's already running
        if server_info.up:
            print("Server is already running. Please crash or exit before starting a new one.")
            return

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
                    
                    # Print full server info on successful setup
                    print(server_info) 
                    print('server setup successful!')
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

        # check if the other server is a direct neighbor
        if neighbor_id not in server_info.neighbors:
             print(f'update error: server {neighbor_id} is not a direct neighbor. cannot update link cost.')
             return

        # execute the update
        with lock:
            updated = server_info.update_link_cost(neighbor_id_str, new_cost)

        if updated:
            # Propagate the update to the neighbor so they update their cost symmetrically
            send_link_update(neighbor_id, cost_str)
            
            # immediately send updates to neighbors
            send_all_rt()
            # print success message
            print('update SUCCESS')
        else:
            # this happens if cost didn't change, or neighbor was not found 
            print('update SUCCESS (no change applied).')

    # crashes server        
    elif command[0] == 'crash':
        if len(command) != 1:
            print('crash error: usage is just "crash"')
            return
        
        if not server_info.up:
            print('crash error: server is already crashed or not running')
            return
            
        server_info.crash()
        
        # Neighbor must handle this close correctly and set link cost to infinity
        
        print('crash SUCCESS') 
        print(f"server {server_info.id} has initiated crash sequence.")
    
    elif command[0] == 'step':
        if len(command) != 1:
            print('step error: usage is just "step"')
            return
        
        if not server_info.up:
            print('step error: server is not running.')
            return
            
        send_all_rt()
        print('step SUCCESS') 
        print('routing table sent to all neighbors.')
        
    
    elif command[0] == 'packets':
        if len(command) != 1:
            print('packets error: usage is just "packets"')
            return
            
        current_count = server_info.packets_received_count
        print(f"received {current_count} distance vector packets since last invocation of this information.")
        
        # Reset the counter
        server_info.packets_received_count = 0
        
        print('packets SUCCESS') 
        
    # disable 
    elif command[0] == 'disable':
        if len(command) != 2:
            print('disable ERROR: usage is "disable <server-ID>"')
            return
        # The disable function now includes a symmetric link update
        disable(command[1]) # disable function prints "disable SUCCESS"
        
    elif command[0] == 'display':
        display_routing_table() # Display the current routing table
        print('display SUCCESS') 
    
    else:
        print(f'unknown command: {command[0]}')

def main():
    global server_info,lock
    while True:
        try:
            message = input('>')
        except EOFError:
            # Handle Ctrl+D or end of pipe
            break

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


main()