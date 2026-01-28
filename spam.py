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
    <title>DISCORD FOLDER SPAMMER V7</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        body { background: #0f0f0f; color: #f0f0f0; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 0; padding: 20px; }
        .header { text-align: center; border-bottom: 2px solid #00ff41; padding-bottom: 10px; margin-bottom: 20px; }
        .header h1 { color: #00ff41; margin: 0; text-transform: uppercase; font-size: 1.5rem; }
        
        .main-container { display: flex; gap: 20px; align-items: flex-start; }
        .sidebar { width: 300px; background: #1a1a1a; padding: 15px; border-radius: 8px; border: 1px solid #333; flex-shrink: 0; }
        
        .btn { width: 100%; padding: 10px; border: none; font-weight: bold; cursor: pointer; border-radius: 4px; margin-top: 8px; font-family: inherit; transition: 0.2s; }
        .btn-create { background: #00ff41; color: #000; }
        .btn-create:hover { background: #00cc33; }
        
        input[type="text"] { width: 100%; padding: 10px; background: #000; border: 1px solid #444; color: #fff; margin-bottom: 10px; box-sizing: border-box; border-radius: 4px; }
        
        .groups-area { flex: 1; display: flex; flex-direction: column; gap: 20px; }
        .panel-card { background: #1a1a1a; border: 1px solid #333; border-radius: 8px; padding: 15px; position: relative; }
        .panel-card.active { border-color: #00ff41; box-shadow: 0 0 10px rgba(0, 255, 65, 0.1); }
        
        .panel-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 15px; }
        .panel-title { font-size: 1.1em; font-weight: bold; color: #fff; }
        .badge { padding: 3px 8px; font-size: 0.7em; border-radius: 4px; margin-left: 10px; font-weight: bold; text-transform: uppercase; }
        
        .config-grid { display: grid; grid-template-columns: 200px 1fr; gap: 15px; margin-bottom: 15px; }
        
        .list-box { height: 300px; overflow-y: auto; background: #050505; border: 1px solid #333; padding: 5px; border-radius: 4px; }
        .check-item { display: flex; align-items: center; padding: 6px; cursor: pointer; border-bottom: 1px solid #222; font-size: 0.9em; }
        .check-item:hover { background: #222; color: #00ff41; }
        .check-item input { margin-right: 8px; }

        /* FOLDER STYLING */
        .folder-header { 
            background: #2a2a2a; color: #ddd; padding: 8px; 
            font-weight: bold; font-size: 0.85em; 
            display: flex; justify-content: space-between; align-items: center;
            position: sticky; top: 0; z-index: 10; border-bottom: 1px solid #444;
        }
        .folder-header button {
            background: #444; color: #fff; border: none; padding: 2px 8px; 
            font-size: 0.8em; cursor: pointer; border-radius: 3px;
        }
        .folder-header button:hover { background: #00ff41; color: #000; }
        
        textarea { width: 100%; background: #050505; border: 1px solid #333; color: #00ff41; padding: 10px; font-family: inherit; resize: vertical; margin-bottom: 10px; box-sizing: border-box; min-height: 60px; border-radius: 4px;}
        
        .action-bar { display: flex; gap: 10px; justify-content: flex-end; border-top: 1px solid #333; padding-top: 15px; }
        .btn-save { background: #333; color: #fff; width: auto; }
        .btn-start { background: #00ff41; color: #000; width: auto; }
        .btn-stop { background: #ff3333; color: #fff; width: auto; }
        .btn-del { background: #ff3333; color: #fff; width: auto; padding: 5px 10px; font-size: 0.8em; }

        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: #000; }
        ::-webkit-scrollbar-thumb { background: #333; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #555; }
    </style>
</head>
<body>
    <div class="header"><h1><i class="fas fa-folder-tree"></i> SPAM MANAGER V7 (API SCAN)</h1></div>
    
    <div class="main-container">
        <div class="sidebar">
            <h3><i class="fas fa-plus-circle"></i> Tạo Nhóm Spam</h3>
            <input type="text" id="groupName" placeholder="Tên chiến dịch...">
            <button class="btn btn-create" onclick="createGroup()">THÊM MỚI</button>
            
            <div style="margin-top: 20px; font-size: 0.85em; color: #888;">
                <div><i class="fas fa-robot"></i> Bots Online: <span style="color:#fff; font-weight:bold;">{{ bot_count }}</span></div>
                <div style="margin-top:5px;"><i class="fas fa-sync"></i> Bot 1 Scan Status: <br>
                    <span style="color: #00ff41;">Xong (Dùng API Settings)</span>
                </div>
            </div>
            <button class="btn" style="background: #333; color: #aaa; font-size: 0.8em;" onclick="location.reload()">Refresh Page</button>
        </div>

        <div id="groupsList" class="groups-area"></div>
    </div>

    <script>
        const bots = {{ bots_json|safe }};
        const folderData = {{ scanned_data|safe }};

        function createPanelHTML(id, grp) {
            let botChecks = '';
            bots.forEach(b => {
                const checked = grp.bots.includes(b.index) ? 'checked' : '';
                botChecks += `
                <label class="check-item">
                    <input type="checkbox" value="${b.index}" ${checked}> 
                    <span>Bot ${b.index + 1}: ${b.name}</span>
                </label>`;
            });

            let serverListHTML = '';
            if (folderData.length === 0) {
                serverListHTML = '<div style="padding:20px; color:#888; text-align:center;">Đang lấy dữ liệu từ API Settings...<br>Đợi 5s và F5 lại.</div>';
            } else {
                folderData.forEach((folder, fIndex) => {
                    const folderIdRaw = `f-${id}-${fIndex}`;
                    serverListHTML += `
                        <div class="folder-header">
                            <span>${folder.folder_name} (${folder.servers.length})</span>
                            <button onclick="toggleFolder(this, '${folderIdRaw}')">Chọn hết</button>
                        </div>
                        <div id="${folderIdRaw}-container">
                    `;
                    folder.servers.forEach(s => {
                        const checked = grp.servers.includes(s.id) ? 'checked' : '';
                        serverListHTML += `
                        <label class="check-item">
                            <input type="checkbox" value="${s.id}" ${checked} class="${folderIdRaw}-chk"> 
                            <span>${s.name}</span>
                        </label>`;
                    });
                    serverListHTML += `</div>`;
                });
            }

            return `
                <div class="panel-card" id="panel-${id}">
                    <div class="panel-header">
                        <div class="panel-title">
                            <i class="fas fa-tasks"></i> ${grp.name} 
                            <span id="badge-${id}" class="badge">IDLE</span>
                        </div>
                        <button class="btn btn-del" onclick="deleteGroup('${id}')"><i class="fas fa-trash"></i></button>
                    </div>
                    
                    <div class="config-grid">
                        <div>
                            <div style="margin-bottom:8px; font-weight:bold; color:#00ff41"><i class="fas fa-user-astronaut"></i> CHỌN BOTS</div>
                            <div class="list-box" id="bots-${id}">${botChecks}</div>
                        </div>
                        <div>
                            <div style="margin-bottom:8px; font-weight:bold; color:#00ff41"><i class="fas fa-server"></i> CHỌN SERVERS</div>
                            <div class="list-box" id="servers-${id}">${serverListHTML}</div>
                        </div>
                    </div>
                    
                    <div>
                        <div style="margin-bottom:5px; font-weight:bold; font-size:0.9em;">NỘI DUNG SPAM</div>
                        <textarea id="msg-${id}" placeholder="Nhập nội dung...">${grp.message || ''}</textarea>
                    </div>
                    
                    <div class="action-bar">
                        <button class="btn btn-save" onclick="saveGroup('${id}')"><i class="fas fa-save"></i> LƯU</button>
                        <span id="btn-area-${id}"></span>
                    </div>
                </div>
            `;
        }

        function toggleFolder(btn, classPrefix) {
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
                Array.from(container.children).forEach(child => {
                    const childId = child.id.replace('panel-', '');
                    if (!currentIds.includes(childId)) child.remove();
                });

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
                    badge.innerText = grp.active ? 'ĐANG CHẠY' : 'ĐÃ DỪNG';
                    badge.style.background = grp.active ? '#00ff41' : '#333';
                    badge.style.color = grp.active ? '#000' : '#fff';
                    const btnArea = document.getElementById(`btn-area-${id}`);
                    if (grp.active) {
                        btnArea.innerHTML = `<button class="btn btn-stop" onclick="toggleGroup('${id}')"><i class="fas fa-stop"></i> STOP</button>`;
                    } else {
                        btnArea.innerHTML = `<button class="btn btn-start" onclick="toggleGroup('${id}')"><i class="fas fa-play"></i> START</button>`;
                    }
                }
            });
        }

        function createGroup() {
            const name = document.getElementById('groupName').value;
            if(!name) return alert("Nhập tên nhóm!");
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
            if(confirm('Xóa nhóm này?')) fetch('/api/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}) }).then(() => renderGroups());
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
