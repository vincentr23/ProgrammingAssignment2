def disable(target_id):
    global server_info, lock
    try:
        target_id = int(target_id)
    except ValueError:
        print("disable ERROR")
        return
    
    with lock:
        if target_id not in server_info.neighbors:
            print("disable ERROR")
            return
        
        server_info.direct_costs[str(target_id)] = float('inf')
        server_info.rt[str(target_id)] = float('inf')

        server_info.last_heard[str(target_id)] = 0    
    print("disable SUCCESS")
