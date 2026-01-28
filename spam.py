import discord
import asyncio
import threading
import time
import os
import uuid
import logging
from flask import Flask, request, render_template_string, jsonify
from dotenv import load_dotenv

# --- CẤU HÌNH ---
load_dotenv()
TOKENS = os.getenv("TOKENS", "").split(",")

if not TOKENS or TOKENS == ['']:
    print("❌ LỖI: Chưa nhập Tokens trong file .env")
    TOKENS = []

# Tắt log rác
logging.getLogger('discord').setLevel(logging.WARNING)

app = Flask(__name__)

# --- DỮ LIỆU ---
bots_instances = {}    
scanned_data = []      
spam_groups = {}       
channel_cache = {}     

# --- CORE LOGIC: SPAM (GIỮ NGUYÊN) ---
def send_message_from_sync(bot_index, channel_id, content):
    bot_data = bots_instances.get(bot_index)
    if not bot_data: return
    bot = bot_data['client']
    loop = bot_data['loop']
    async def _send():
        try:
            channel = bot.get_channel(int(channel_id))
            if channel: await channel.send(content)
        except: pass
    if loop.is_running():
        asyncio.run_coroutine_threadsafe(_send(), loop)

def resolve_spam_channel(bot_indices, guild_id):
    guild_id = str(guild_id)
    if guild_id in channel_cache: return channel_cache[guild_id]
    target_channel_id = None
    for b_idx in bot_indices:
        bot_data = bots_instances.get(b_idx)
        if not bot_data: continue
        bot = bot_data['client']
        guild = bot.get_guild(int(guild_id))
        if not guild: continue
        
        candidates = [c for c in guild.text_channels if 'spam' in c.name.lower()]
        if candidates:
            exact = next((c for c in candidates if c.name == 'spam'), candidates[0])
            target_channel_id = exact.id

        if target_channel_id:
            channel_cache[guild_id] = target_channel_id
            return target_channel_id
    return None

def run_spam_group_logic(group_id):
    print(f"🚀 [Group {group_id}] Đang chạy...", flush=True)
    server_pair_index = 0
    DELAY_BETWEEN_PAIRS = 2.0
    DELAY_WITHIN_PAIR = 1.5
    MAX_THREADS = 4

    while True:
        group = spam_groups.get(group_id)
        if not group or not group.get('active'):
            print(f"🛑 [Group {group_id}] Đã dừng.", flush=True)
            break

        target_servers = group.get('servers', [])
        target_bots = group.get('bots', [])
        message = group.get('message', "")

        if not target_servers or not target_bots or not message:
            time.sleep(2); continue

        if server_pair_index * 2 >= len(target_servers):
            server_pair_index = 0
        
        start_index = server_pair_index * 2
        current_pair_ids = target_servers[start_index : start_index + 2]
        if not current_pair_ids:
            server_pair_index = 0; continue

        valid_targets = []
        for s_id in current_pair_ids:
            c_id = resolve_spam_channel(target_bots, s_id)
            if c_id: valid_targets.append((s_id, c_id))

        if not valid_targets:
            server_pair_index += 1; continue

        bot_chunks = [target_bots[i:i + MAX_THREADS] for i in range(0, len(target_bots), MAX_THREADS)]
        threads = []
        for bot_chunk in bot_chunks:
            def thread_task(bots=bot_chunk, targets=valid_targets):
                if len(targets) > 0:
                    svr1_id, ch1_id = targets[0]
                    for b_idx in bots:
                        send_message_from_sync(b_idx, ch1_id, message)
                        time.sleep(0.1)
                if len(targets) > 1:
                    time.sleep(DELAY_WITHIN_PAIR)
                    svr2_id, ch2_id = targets[1]
                    for b_idx in bots:
                        send_message_from_sync(b_idx, ch2_id, message)
                        time.sleep(0.1)
            t = threading.Thread(target=thread_task)
            threads.append(t); t.start()
        
        for t in threads: t.join()
        time.sleep(DELAY_BETWEEN_PAIRS)
        server_pair_index += 1

# --- CƠ CHẾ QUÉT SERVER: DÙNG API SETTINGS ---
async def background_server_scanner(bot, index):
    if index != 0: return

    print(f"📡 [Bot 1] Đang gọi API User Settings để lấy Folder...", flush=True)
    await bot.wait_until_ready()
    
    global scanned_data
    
    try:
        # 1. Gọi trực tiếp API để lấy User Settings (chứa cấu trúc Folder)
        # Route: GET /users/@me/settings
        route = discord.http.Route('GET', '/users/@me/settings')
        settings_data = await bot.http.request(route)
        
        # Lấy danh sách folder raw từ API
        # Cấu trúc json: "guild_folders": [{"id": 123, "guild_ids": ["id1", "id2"], "name": "Name", ...}]
        raw_folders = settings_data.get('guild_folders', [])
        
        # 2. Chuẩn bị dữ liệu
        all_guilds = {str(g.id): g for g in bot.guilds} # Map ID -> Guild Object
        processed_ids = set()
        
        final_list = []
        
        # 3. Duyệt qua từng Folder từ API
        for folder in raw_folders:
            folder_name = folder.get('name')
            folder_ids = folder.get('guild_ids', [])
            
            # Nếu folder không có tên, Discord thường để null
            if not folder_name:
                folder_name = "Unnamed Folder"

            folder_servers = []
            
            for g_id in folder_ids:
                g_id = str(g_id)
                if g_id in all_guilds:
                    guild = all_guilds[g_id]
                    icon_link = str(guild.icon.url) if guild.icon else "https://cdn.discordapp.com/embed/avatars/0.png"
                    
                    folder_servers.append({
                        'id': g_id,
                        'name': guild.name,
                        'icon': icon_link
                    })
                    processed_ids.add(g_id)
            
            # Chỉ thêm folder nếu có server
            if folder_servers:
                # Nếu folder chưa có tên (thường là folder ẩn hoặc gom nhóm tạm), đặt tên
                final_list.append({'folder_name': f"📁 {folder_name}", 'servers': folder_servers})

        # 4. Xử lý các Server không nằm trong folder nào (Uncategorized)
        uncategorized = []
        for g_id, guild in all_guilds.items():
            if g_id not in processed_ids:
                icon_link = str(guild.icon.url) if guild.icon else "https://cdn.discordapp.com/embed/avatars/0.png"
                uncategorized.append({
                    'id': g_id,
                    'name': guild.name,
                    'icon': icon_link
                })
        
        if uncategorized:
            uncategorized.sort(key=lambda x: x['name'])
            final_list.append({'folder_name': 'Server Lẻ (Ngoài Folder)', 'servers': uncategorized})
            
        scanned_data = final_list
        total_sv = sum(len(x['servers']) for x in final_list)
        print(f"✨ [Bot 1] QUÉT XONG! Tìm thấy {total_sv} servers (API Mode).", flush=True)

    except Exception as e:
        print(f"❌ [Bot 1] Lỗi quét API: {e}")
        # Fallback: Quét thường nếu API lỗi
        flat_list = []
        for guild in bot.guilds:
            icon_link = str(guild.icon.url) if guild.icon else "https://cdn.discordapp.com/embed/avatars/0.png"
            flat_list.append({'id': str(guild.id), 'name': guild.name, 'icon': icon_link})
        scanned_data = [{'folder_name': 'All Servers (Backup)', 'servers': flat_list}]

    return

def start_bot_node(token, index):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = discord.Client(self_bot=True)

    @bot.event
    async def on_ready():
        print(f"✅ Bot {index+1}: {bot.user.name} Connected!", flush=True)
        bots_instances[index] = {
            'client': bot, 'loop': loop, 'name': bot.user.name, 'id': bot.user.id
        }
        bot.loop.create_task(background_server_scanner(bot, index))

    try:
        loop.run_until_complete(bot.start(token.strip()))
    except Exception as e:
        print(f"❌ Bot {index+1} lỗi login: {e}")

# --- GIAO DIỆN WEB ---
HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SPAM MANAGER V7 - NEON UI</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --primary: #00ff41;
            --bg-dark: #0f0f0f;
            --bg-panel: #161616;
            --border: #333;
            --text-gray: #888;
        }
        body { background: var(--bg-dark); color: #f0f0f0; font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; margin: 0; padding: 20px; font-size: 14px; }
        
        /* SCROLLBAR */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: #111; }
        ::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--primary); }

        .header { text-align: center; border-bottom: 1px solid var(--primary); padding-bottom: 15px; margin-bottom: 25px; box-shadow: 0 4px 15px -10px var(--primary); }
        .header h1 { color: var(--primary); margin: 0; letter-spacing: 2px; font-size: 1.8rem; text-shadow: 0 0 10px rgba(0, 255, 65, 0.3); }

        .main-container { display: flex; gap: 20px; align-items: flex-start; }
        
        /* SIDEBAR */
        .sidebar { width: 280px; background: var(--bg-panel); padding: 20px; border-radius: 12px; border: 1px solid var(--border); flex-shrink: 0; height: fit-content; }
        .sidebar h3 { margin-top: 0; color: #fff; display: flex; align-items: center; gap: 10px; font-size: 1.1em;}
        
        input[type="text"], textarea { 
            width: 100%; padding: 12px; background: #0a0a0a; border: 1px solid #444; color: #fff; 
            margin-bottom: 10px; box-sizing: border-box; border-radius: 6px; outline: none; transition: 0.3s; font-family: inherit;
        }
        input[type="text"]:focus, textarea:focus { border-color: var(--primary); box-shadow: 0 0 8px rgba(0,255,65, 0.1); }
        
        .btn { width: 100%; padding: 12px; border: none; font-weight: 600; cursor: pointer; border-radius: 6px; margin-top: 8px; font-family: inherit; transition: all 0.2s; text-transform: uppercase; font-size: 0.85rem; display: flex; align-items: center; justify-content: center; gap: 8px;}
        .btn-create { background: var(--primary); color: #000; box-shadow: 0 0 10px rgba(0,255,65,0.2); }
        .btn-create:hover { background: #00cc33; transform: translateY(-1px); }
        .btn-refresh { background: #222; color: #aaa; margin-top: 20px; }
        .btn-refresh:hover { background: #333; color: #fff; }

        /* PANELS */
        .groups-area { flex: 1; display: flex; flex-direction: column; gap: 20px; }
        .panel-card { background: var(--bg-panel); border: 1px solid var(--border); border-radius: 12px; padding: 20px; position: relative; transition: border-color 0.3s; }
        .panel-card.active { border-color: var(--primary); box-shadow: 0 0 20px rgba(0, 255, 65, 0.05); }
        .panel-card.active::before { content: ''; position: absolute; top: 0; left: 0; width: 4px; height: 100%; background: var(--primary); border-top-left-radius: 12px; border-bottom-left-radius: 12px; }

        .panel-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #2a2a2a; padding-bottom: 15px; margin-bottom: 20px; }
        .panel-title { font-size: 1.2em; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 10px; }
        .badge { padding: 4px 10px; font-size: 0.65em; border-radius: 20px; font-weight: 800; letter-spacing: 0.5px; }

        .config-grid { display: grid; grid-template-columns: 220px 1fr; gap: 20px; margin-bottom: 20px; }
        .col-title { margin-bottom: 10px; font-weight: 700; color: var(--primary); font-size: 0.85em; text-transform: uppercase; letter-spacing: 1px; display: flex; align-items: center; gap: 8px; }

        .list-box { height: 350px; overflow-y: auto; background: #0a0a0a; border: 1px solid #333; border-radius: 8px; padding: 5px; }

        /* CHECKBOX STYLING */
        input[type="checkbox"] { accent-color: var(--primary); width: 16px; height: 16px; cursor: pointer; }
        .check-item { display: flex; align-items: center; padding: 8px 12px; cursor: pointer; border-radius: 4px; transition: background 0.2s; color: #ddd; font-size: 0.95em; }
        .check-item:hover { background: #222; color: #fff; }
        .check-item span { margin-left: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

        /* FOLDER UI (NEW) */
        .folder-group { margin-bottom: 5px; border: 1px solid #222; border-radius: 6px; background: #111; overflow: hidden; }
        .folder-group[open] { border-color: #333; }
        
        .folder-summary {
            list-style: none; padding: 10px 12px; background: #1a1a1a; cursor: pointer;
            display: flex; justify-content: space-between; align-items: center;
            font-weight: 600; font-size: 0.9em; color: #eee; transition: background 0.2s;
        }
        .folder-summary:hover { background: #252525; }
        .folder-summary::-webkit-details-marker { display: none; } /* Hide default arrow */
        
        .folder-info { display: flex; align-items: center; gap: 8px; }
        .folder-icon { color: #888; transition: transform 0.2s; }
        .folder-group[open] .folder-summary .folder-icon { transform: rotate(90deg); color: var(--primary); }
        
        .btn-select-all {
            background: #333; border: 1px solid #444; color: #ccc; font-size: 0.75em;
            padding: 2px 8px; border-radius: 4px; cursor: pointer; transition: 0.2s;
        }
        .btn-select-all:hover { background: var(--primary); color: #000; border-color: var(--primary); }

        .folder-content { padding: 5px 0 5px 15px; border-top: 1px solid #222; background: #0e0e0e; }
        .folder-content .check-item { font-size: 0.9em; padding: 6px 10px; }

        /* ACTION BAR */
        .action-bar { display: flex; gap: 12px; justify-content: flex-end; border-top: 1px solid #2a2a2a; padding-top: 20px; }
        .btn-sm { width: auto; padding: 8px 16px; margin: 0; }
        .btn-save { background: #333; color: #fff; }
        .btn-save:hover { background: #444; }
        .btn-stop { background: #ff3333; color: #fff; }
        .btn-stop:hover { background: #cc0000; }
        .btn-del { background: #222; color: #ff3333; width: 30px; height: 30px; padding: 0; border-radius: 50%; display: flex; align-items: center; justify-content: center; }
        .btn-del:hover { background: #ff3333; color: #fff; }

        /* STATS */
        .stat-box { margin-top: 25px; padding: 15px; background: #111; border-radius: 8px; border: 1px dashed #333; }
        .stat-row { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 0.9em; color: #888; }
        .stat-val { color: #fff; font-weight: bold; }
        .stat-status { color: var(--primary); font-weight: bold; }

    </style>
</head>
<body>
    <div class="header">
        <h1><i class="fas fa-network-wired"></i> SPAM MANAGER V7</h1>
    </div>
    
    <div class="main-container">
        <div class="sidebar">
            <h3><i class="fas fa-rocket"></i> New Campaign</h3>
            <input type="text" id="groupName" placeholder="Nhập tên nhóm...">
            <button class="btn btn-create" onclick="createGroup()"><i class="fas fa-plus"></i> TẠO NHÓM</button>
            
            <div class="stat-box">
                <div class="stat-row"><span><i class="fas fa-robot"></i> Bots Online</span> <span class="stat-val">{{ bot_count }}</span></div>
                <div class="stat-row"><span><i class="fas fa-folder-tree"></i> Mode</span> <span class="stat-status">API SCAN</span></div>
                <div style="margin-top:10px; font-size:0.8em; color:#555; text-align:center;">* Bot 1 tự động đồng bộ folder</div>
            </div>
            
            <button class="btn btn-refresh" onclick="location.reload()"><i class="fas fa-sync"></i> Refresh UI</button>
        </div>

        <div id="groupsList" class="groups-area"></div>
    </div>

    <script>
        const bots = {{ bots_json|safe }};
        const folderData = {{ scanned_data|safe }};

        function createPanelHTML(id, grp) {
            // 1. Render Bots
            let botChecks = '';
            bots.forEach(b => {
                const checked = grp.bots.includes(b.index) ? 'checked' : '';
                botChecks += `
                <label class="check-item">
                    <input type="checkbox" value="${b.index}" ${checked}> 
                    <span>Bot ${b.index + 1}: ${b.name}</span>
                </label>`;
            });

            // 2. Render Servers (Folder Accordion Style)
            let serverListHTML = '';
            if (folderData.length === 0) {
                serverListHTML = '<div style="padding:40px 20px; color:#666; text-align:center; font-style:italic;"><i class="fas fa-spinner fa-spin"></i><br>Đang đồng bộ dữ liệu...<br>Vui lòng đợi 5s rồi F5</div>';
            } else {
                folderData.forEach((folder, fIndex) => {
                    const folderIdRaw = `f-${id}-${fIndex}`;
                    const count = folder.servers.length;
                    
                    // Header folder (summary)
                    serverListHTML += `
                        <details class="folder-group" open>
                            <summary class="folder-summary">
                                <div class="folder-info">
                                    <i class="fas fa-chevron-right folder-icon"></i>
                                    <span>${folder.folder_name} <span style="color:#666; font-size:0.85em;">(${count})</span></span>
                                </div>
                                <button class="btn-select-all" onclick="toggleFolder(this, '${folderIdRaw}')">Chọn hết</button>
                            </summary>
                            
                            <div id="${folderIdRaw}-container" class="folder-content">
                    `;
                    
                    // List Servers
                    folder.servers.forEach(s => {
                        const checked = grp.servers.includes(s.id) ? 'checked' : '';
                        // Icon server (nếu lỗi ảnh thì hiện icon mặc định)
                        const iconHtml = `<i class="fas fa-server" style="color:#444; margin-right:5px; font-size:0.8em;"></i>`;
                        
                        serverListHTML += `
                            <label class="check-item">
                                <input type="checkbox" value="${s.id}" ${checked} class="${folderIdRaw}-chk"> 
                                <span>${iconHtml} ${s.name}</span>
                            </label>`;
                    });
                    
                    serverListHTML += `</div></details>`;
                });
            }

            return `
                <div class="panel-card" id="panel-${id}">
                    <div class="panel-header">
                        <div class="panel-title">
                            <i class="fas fa-layer-group" style="color:var(--primary)"></i> ${grp.name} 
                            <span id="badge-${id}" class="badge">IDLE</span>
                        </div>
                        <button class="btn btn-del" onclick="deleteGroup('${id}')" title="Xóa nhóm"><i class="fas fa-trash-alt"></i></button>
                    </div>
                    
                    <div class="config-grid">
                        <div>
                            <div class="col-title"><i class="fas fa-user-astronaut"></i> Chọn Bots</div>
                            <div class="list-box" id="bots-${id}">${botChecks}</div>
                        </div>
                        
                        <div>
                            <div class="col-title"><i class="fas fa-server"></i> Chọn Servers</div>
                            <div class="list-box" id="servers-${id}">${serverListHTML}</div>
                        </div>
                    </div>
                    
                    <div style="margin-top:15px;">
                        <div class="col-title"><i class="fas fa-comment-dots"></i> Nội dung Spam</div>
                        <textarea id="msg-${id}" placeholder="Nhập nội dung tin nhắn spam tại đây..." rows="3">${grp.message || ''}</textarea>
                    </div>
                    
                    <div class="action-bar">
                        <button class="btn btn-sm btn-save" onclick="saveGroup('${id}')"><i class="fas fa-save"></i> LƯU CẤU HÌNH</button>
                        <span id="btn-area-${id}"></span>
                    </div>
                </div>
            `;
        }

        // --- GIỮ NGUYÊN LOGIC JS ---
        function toggleFolder(btn, classPrefix) {
            // Ngăn sự kiện click lan ra details/summary làm đóng mở folder
            event.preventDefault(); 
            
            const container = document.getElementById(classPrefix + '-container');
            const checkboxes = container.querySelectorAll('input[type="checkbox"]');
            let allChecked = true;
            checkboxes.forEach(cb => { if(!cb.checked) allChecked = false; });
            checkboxes.forEach(cb => cb.checked = !allChecked);
            btn.innerText = !allChecked ? "Bỏ chọn" : "Chọn hết";
        }

        function renderGroups() {
            fetch('/api/groups').then(r => r.json()).then(data => {
                const container = document.getElementById('groupsList');
                const currentIds = Object.keys(data);
                
                // Remove deleted
                Array.from(container.children).forEach(child => {
                    const childId = child.id.replace('panel-', '');
                    if (!currentIds.includes(childId)) child.remove();
                });

                // Add/Update
                for (const [id, grp] of Object.entries(data)) {
                    let panel = document.getElementById(`panel-${id}`);
                    if (!panel) {
                        const div = document.createElement('div');
                        div.innerHTML = createPanelHTML(id, grp);
                        container.appendChild(div.firstElementChild);
                        panel = document.getElementById(`panel-${id}`);
                    }

                    if (grp.active) panel.classList.add('active');
                    else panel.classList.remove('active');

                    const badge = document.getElementById(`badge-${id}`);
                    badge.innerText = grp.active ? 'RUNNING' : 'STOPPED';
                    badge.style.background = grp.active ? 'var(--primary)' : '#333';
                    badge.style.color = grp.active ? '#000' : '#fff';

                    const btnArea = document.getElementById(`btn-area-${id}`);
                    if (grp.active) {
                        btnArea.innerHTML = `<button class="btn btn-sm btn-stop" onclick="toggleGroup('${id}')"><i class="fas fa-stop-circle"></i> DỪNG LẠI</button>`;
                    } else {
                        btnArea.innerHTML = `<button class="btn btn-sm btn-create" onclick="toggleGroup('${id}')"><i class="fas fa-play"></i> BẮT ĐẦU</button>`;
                    }
                }
            });
        }

        function createGroup() {
            const name = document.getElementById('groupName').value;
            if(!name) return alert("Vui lòng nhập tên nhóm!");
            fetch('/api/create', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name}) })
            .then(() => { document.getElementById('groupName').value = ''; renderGroups(); });
        }

        function saveGroup(id) {
            const msg = document.getElementById(`msg-${id}`).value;
            const bots = Array.from(document.querySelectorAll(`#bots-${id} input:checked`)).map(c => parseInt(c.value));
            const servers = Array.from(document.querySelectorAll(`#servers-${id} input:checked`)).map(c => c.value);
            fetch('/api/update', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id, message: msg, bots, servers}) })
            .then(r => r.json()).then(d => alert(d.msg));
        }

        function toggleGroup(id) {
            fetch('/api/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}) })
            .then(() => setTimeout(renderGroups, 200));
        }

        function deleteGroup(id) {
            if(confirm('Bạn có chắc muốn xóa nhóm này?')) fetch('/api/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}) }).then(() => renderGroups());
        }

        renderGroups();
        setInterval(renderGroups, 3000); 
    </script>
</body>
</html>
"""

# --- API ---
@app.route('/')
def index():
    bots_list = [{'index': k, 'name': v['name']} for k, v in bots_instances.items()]
    return render_template_string(HTML, bots_json=bots_list, scanned_data=scanned_data, bot_count=len(bots_instances))

@app.route('/api/groups')
def get_groups(): return jsonify(spam_groups)

@app.route('/api/create', methods=['POST'])
def create_grp():
    gid = str(uuid.uuid4())[:6]
    spam_groups[gid] = {'name': request.json.get('name'), 'active': False, 'bots': [], 'servers': [], 'message': ''}
    return jsonify({'status': 'ok'})

@app.route('/api/update', methods=['POST'])
def update_grp():
    d = request.json
    if d['id'] in spam_groups:
        spam_groups[d['id']].update({'bots': d['bots'], 'servers': d['servers'], 'message': d['message']})
    return jsonify({'status': 'ok', 'msg': '✅ Đã lưu cấu hình!'})

@app.route('/api/toggle', methods=['POST'])
def toggle_grp():
    gid = request.json['id']
    if gid in spam_groups:
        curr = spam_groups[gid]['active']
        spam_groups[gid]['active'] = not curr
        if not curr: threading.Thread(target=run_spam_group_logic, args=(gid,), daemon=True).start()
    return jsonify({'status': 'ok'})

@app.route('/api/delete', methods=['POST'])
def del_grp():
    gid = request.json['id']
    if gid in spam_groups:
        spam_groups[gid]['active'] = False; del spam_groups[gid]
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    print("🔥 SYSTEM V7 STARTING... (API Mode Scan)", flush=True)
    for i, t in enumerate(TOKENS):
        if t.strip(): threading.Thread(target=start_bot_node, args=(t, i), daemon=True).start(); time.sleep(1)
    
    port = int(os.environ.get("PORT", 10000))
    print(f"🌍 WEB PANEL: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port)
