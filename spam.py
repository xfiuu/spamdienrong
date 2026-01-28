import discord
import asyncio
import threading
import time
import os
import uuid
import logging
import requests
import json
from flask import Flask, request, render_template_string, jsonify
from dotenv import load_dotenv

# --- CẤU HÌNH ---
load_dotenv()
TOKENS = os.getenv("TOKENS", "").split(",")
JSONBIN_API_KEY = os.getenv("JSONBIN_API_KEY", "")
JSONBIN_BIN_ID = os.getenv("JSONBIN_BIN_ID", "")

if not TOKENS or TOKENS == ['']:
    print("❌ LỖI: Chưa nhập Tokens trong file .env")
    TOKENS = []

# Tắt log rác
logging.getLogger('discord').setLevel(logging.WARNING)

app = Flask(__name__)

# --- DỮ LIỆU TOÀN CỤC ---
bots_instances = {}   
scanned_data = {"folders": [], "servers": {}} 
spam_groups = {}      
channel_cache = {}    

# --- 1. HỆ THỐNG LƯU TRỮ (JSONBIN) ---
def save_settings():
    """Lưu cấu hình các nhóm SPAM lên JSONBin hoặc File"""
    data_to_save = {'spam_groups': spam_groups}
    
    # 1. Lưu Local (Backup)
    try:
        with open('spam_settings.json', 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=2)
    except: pass

    # 2. Lưu JSONBin (Nếu có config)
    if JSONBIN_API_KEY and JSONBIN_BIN_ID:
        def _upload():
            headers = {'Content-Type': 'application/json', 'X-Master-Key': JSONBIN_API_KEY}
            url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
            try:
                requests.put(url, json=data_to_save, headers=headers)
                print("☁️ Đã lưu cấu hình lên Cloud (JSONBin).", flush=True)
            except Exception as e:
                print(f"⚠️ Lỗi lưu Cloud: {e}", flush=True)
        threading.Thread(target=_upload).start()

def load_settings():
    """Tải cấu hình từ JSONBin hoặc File"""
    global spam_groups
    
    # 1. Ưu tiên tải từ Cloud
    if JSONBIN_API_KEY and JSONBIN_BIN_ID:
        try:
            headers = {'X-Master-Key': JSONBIN_API_KEY}
            url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest"
            resp = requests.get(url, headers=headers)
            if resp.status_code == 200:
                record = resp.json().get('record', {})
                spam_groups.update(record.get('spam_groups', {}))
                print("☁️ Đã tải cấu hình từ Cloud.", flush=True)
                return
        except: pass

    # 2. Nếu lỗi cloud, tải từ Local
    if os.path.exists('spam_settings.json'):
        try:
            with open('spam_settings.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
                spam_groups.update(data.get('spam_groups', {}))
                print("📂 Đã tải cấu hình từ File local.", flush=True)
        except: pass

# --- 2. LOGIC SPAM CỐT LÕI (FIXED) ---
def send_message_from_sync(bot_index, channel_id, content):
    bot_data = bots_instances.get(bot_index)
    if not bot_data: return
    bot = bot_data['client']
    loop = bot_data['loop']
    
    async def _send():
        try:
            channel = bot.get_channel(int(channel_id))
            if channel:
                await channel.send(content)
                # print(f"✅ [Bot {bot_index+1}] Sent to {channel.guild.name}", flush=True)
            else:
                # Nếu không tìm thấy kênh bằng ID, thử fetch lại (do cache lỗi)
                try:
                    ch = await bot.fetch_channel(int(channel_id))
                    await ch.send(content)
                except:
                    pass
        except: pass

    if loop.is_running():
        asyncio.run_coroutine_threadsafe(_send(), loop)

def resolve_spam_channel(bot_indices, guild_id):
    """
    Tìm kênh spam trong server.
    Nâng cấp: Nếu cache lỗi, thử tìm lại bằng bot khác.
    """
    guild_id = str(guild_id)
    
    # Check cache trước
    if guild_id in channel_cache:
        return channel_cache[guild_id]
    
    for b_idx in bot_indices:
        bot_data = bots_instances.get(b_idx)
        if not bot_data: continue
        bot = bot_data['client']
        guild = bot.get_guild(int(guild_id))
        
        if not guild: continue
        
        # Tìm các kênh tiềm năng
        target_channel = None
        
        # Ưu tiên 1: Tên chính xác là 'spam'
        for ch in guild.text_channels:
            if ch.name == "spam":
                target_channel = ch
                break
        
        # Ưu tiên 2: Tên chứa chữ 'spam' (vd: chat-spam)
        if not target_channel:
            for ch in guild.text_channels:
                if "spam" in ch.name.lower():
                    target_channel = ch
                    break
        
        # Ưu tiên 3: Tên là 'chat' hoặc 'general' (Chống cháy)
        if not target_channel:
             for ch in guild.text_channels:
                if ch.name in ["chat", "general", "chat-tong-hop"]:
                    target_channel = ch
                    break

        if target_channel:
            # print(f"🔍 [Scanner] Found channel '{target_channel.name}' in '{guild.name}'", flush=True)
            channel_cache[guild_id] = target_channel.id
            return target_channel.id

    return None

def run_spam_group_logic(group_id):
    print(f"🚀 [Group {group_id}] Bắt đầu chạy...", flush=True)
    
    DELAY_BETWEEN_PAIRS = 2.0
    DELAY_WITHIN_PAIR = 1.0
    MAX_THREADS = 4

    while True:
        group = spam_groups.get(group_id)
        # Nếu nhóm bị xóa hoặc tắt -> Dừng
        if not group or not group.get('active'):
            print(f"🛑 [Group {group_id}] Đã dừng.", flush=True)
            break

        target_servers = group.get('servers', [])
        target_bots = group.get('bots', [])
        message = group.get('message', "")

        # Nếu cấu hình chưa đủ -> Chờ
        if not target_servers or not target_bots or not message:
            time.sleep(2)
            continue
        
        # Duyệt qua từng server để spam
        # (Không dùng pair nữa nếu muốn chắc ăn, dùng từng cái 1 cho ổn định)
        # Nhưng để nhanh, ta vẫn dùng batch nhỏ
        
        chunks = [target_servers[i:i + 2] for i in range(0, len(target_servers), 2)]
        
        for pair in chunks:
            if not group.get('active'): break
            
            valid_tasks = []
            
            # 1. Tìm kênh spam cho cặp server này
            for s_id in pair:
                c_id = resolve_spam_channel(target_bots, s_id)
                if c_id:
                    valid_tasks.append(c_id)
                else:
                    # Nếu không tìm thấy kênh, thử clear cache để lần sau tìm lại
                    if str(s_id) in channel_cache: del channel_cache[str(s_id)]

            if not valid_tasks:
                continue

            # 2. Chia bot ra để bắn
            bot_chunks = [target_bots[i:i + MAX_THREADS] for i in range(0, len(target_bots), MAX_THREADS)]
            
            threads = []
            for b_chunk in bot_chunks:
                def spam_task(bots=b_chunk, channels=valid_tasks):
                    for ch_id in channels:
                        for b_idx in bots:
                            send_message_from_sync(b_idx, ch_id, message)
                            time.sleep(0.05) # Delay cực nhỏ giữa các bot
                        time.sleep(DELAY_WITHIN_PAIR) # Delay giữa các server

                t = threading.Thread(target=spam_task)
                threads.append(t)
                t.start()
            
            for t in threads: t.join()
            time.sleep(DELAY_BETWEEN_PAIRS)

# --- 3. QUÉT FOLDER (TÁCH BIỆT ĐỂ REFRESH ĐƯỢC) ---
async def scan_discord_structure(bot):
    """Hàm này chạy trong Event Loop của Bot 1"""
    print(f"📡 [Scanner] Đang quét lại dữ liệu từ Discord...", flush=True)
    
    temp_servers = {}
    # Lấy list server cơ bản
    for guild in bot.guilds:
        icon = str(guild.icon.url) if guild.icon else "https://cdn.discordapp.com/embed/avatars/0.png"
        temp_servers[str(guild.id)] = {'id': str(guild.id), 'name': guild.name, 'icon': icon}

    # Lấy folder từ User Settings
    try:
        user_settings = await bot.http.request(discord.http.Route('GET', '/users/@me/settings'))
        guild_folders = user_settings.get('guild_folders', [])
        
        folders_structure = []
        scanned_ids = []

        for folder in guild_folders:
            folder_id = str(folder.get('id', 'unknown'))
            # Nếu folder id là None -> Nó là mục "Server Lẻ" của Discord nhưng chưa chắc
            if folder_id == 'None': continue

            folder_name = folder.get('name') or f"Folder {folder_id[:4]}"
            guild_ids = [str(gid) for gid in folder.get('guild_ids', [])]
            
            folder_servers = []
            for gid in guild_ids:
                if gid in temp_servers:
                    folder_servers.append(temp_servers[gid])
                    scanned_ids.append(gid)
            
            if folder_servers:
                folders_structure.append({'id': folder_id, 'name': folder_name, 'servers': folder_servers})

        # Xử lý server lẻ (chưa vào folder nào)
        uncategorized = [s for gid, s in temp_servers.items() if gid not in scanned_ids]
        if uncategorized:
            folders_structure.append({'id': 'root', 'name': 'Server Lẻ (Chưa xếp)', 'servers': uncategorized})

        # Cập nhật Global Data
        scanned_data['folders'] = folders_structure
        scanned_data['servers'] = temp_servers
        print(f"✨ [Scanner] Hoàn tất: {len(folders_structure)} Folder, {len(temp_servers)} Server.", flush=True)
        return True
    except Exception as e:
        print(f"⚠️ [Scanner] Lỗi: {e}", flush=True)
        # Fallback
        scanned_data['folders'] = [{'id': 'all', 'name': 'All Servers', 'servers': list(temp_servers.values())}]
        scanned_data['servers'] = temp_servers
        return False

# --- LOGIC KHỞI ĐỘNG BOT ---
def start_bot_node(token, index):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = discord.Client(self_bot=True)

    @bot.event
    async def on_ready():
        print(f"✅ Bot {index+1}: {bot.user.name} Online!", flush=True)
        bots_instances[index] = {
            'client': bot, 'loop': loop, 'name': bot.user.name, 'id': bot.user.id
        }
        
        # Chỉ Bot 1 tự động quét lúc đầu
        if index == 0:
            await asyncio.sleep(3)
            await scan_discord_structure(bot)

    try:
        loop.run_until_complete(bot.start(token.strip()))
    except Exception as e:
        print(f"❌ Bot {index+1} lỗi: {e}")

# --- GIAO DIỆN WEB (FIX REFRESH BUTTON) ---
HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SPAM PRO V8</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        body { background: #0f0f0f; color: #f0f0f0; font-family: 'Segoe UI', monospace; margin: 0; padding: 20px; font-size: 14px;}
        .header { text-align: center; border-bottom: 2px solid #00ff41; padding-bottom: 10px; margin-bottom: 20px; }
        .header h1 { color: #00ff41; margin: 0; text-transform: uppercase; }
        
        .main-container { display: flex; gap: 20px; align-items: flex-start; }
        .sidebar { width: 300px; background: #1a1a1a; padding: 20px; border-radius: 8px; border: 1px solid #333; }
        
        .btn { width: 100%; padding: 10px; border: none; font-weight: bold; cursor: pointer; border-radius: 4px; margin-top: 5px; color: #000; }
        .btn-create { background: #00ff41; }
        .btn-refresh { background: #333; color: #fff; margin-top: 20px; border: 1px solid #555; }
        .btn-refresh:hover { background: #444; border-color: #fff; }
        
        input[type="text"] { width: 100%; padding: 8px; background: #000; border: 1px solid #444; color: #fff; margin-bottom: 10px; box-sizing: border-box; }
        
        .groups-area { flex: 1; display: flex; flex-direction: column; gap: 20px; }
        .panel-card { background: #1a1a1a; border: 1px solid #333; border-radius: 8px; padding: 15px; position: relative; }
        .panel-card.active { border-color: #00ff41; box-shadow: 0 0 10px rgba(0, 255, 65, 0.1); }
        
        .panel-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 15px; }
        .badge { padding: 2px 6px; font-size: 0.8em; border-radius: 4px; margin-left: 10px; font-weight: bold; }
        
        .config-grid { display: grid; grid-template-columns: 1fr 2fr; gap: 15px; margin-bottom: 15px; }
        
        /* FOLDER UI */
        .list-box { height: 350px; overflow-y: auto; background: #050505; border: 1px solid #333; padding: 5px; }
        .folder-group { margin-bottom: 10px; border: 1px solid #333; border-radius: 4px; overflow: hidden; }
        .folder-header { background: #222; padding: 8px; cursor: pointer; display: flex; align-items: center; font-weight: bold; color: #aaa; border-bottom: 1px solid #333; }
        .folder-header:hover { color: #fff; background: #333; }
        .folder-header input { margin-right: 10px; transform: scale(1.2); }
        .folder-content { padding: 5px; background: #111; display: none; }
        .folder-content.open { display: block; }
        
        .server-item { display: flex; align-items: center; padding: 5px 10px; border-bottom: 1px solid #222; color: #ccc; }
        .server-item:hover { color: #00ff41; background: #1a1a1a; }
        .server-item input { margin-right: 10px; }

        .bot-item { display: flex; align-items: center; padding: 5px; border-bottom: 1px solid #222; }
        
        textarea { width: 100%; background: #050505; border: 1px solid #333; color: #00ff41; padding: 10px; resize: vertical; margin-bottom: 10px; box-sizing: border-box; min-height: 60px;}
        
        .action-bar { display: flex; gap: 10px; justify-content: flex-end; border-top: 1px solid #333; padding-top: 10px; }
        .btn-sm { width: auto; padding: 8px 15px; color: #fff; background: #333; }
        .btn-start { background: #00ff41; color: #000; }
        .btn-stop { background: #ff3333; color: #fff; }
    </style>
</head>
<body>
    <div class="header"><h1><i class="fas fa-satellite-dish"></i> SPAM PRO V8 - FINAL FIXED</h1></div>
    
    <div class="main-container">
        <div class="sidebar">
            <h3>Tạo Panel Mới</h3>
            <input type="text" id="groupName" placeholder="Tên nhóm...">
            <button class="btn btn-create" onclick="createGroup()">+ TẠO NHÓM</button>
            
            <button class="btn btn-refresh" onclick="refreshData()" id="refreshBtn">
                <i class="fas fa-sync"></i> QUÉT LẠI SERVER (BOT 1)
            </button>
            <div id="refresh-status" style="text-align:center; margin-top:5px; font-size:0.8em; color:#888;"></div>

            <div style="margin-top:20px; font-size:0.9em; color:#888;">
                Bot Online: <b style="color:#fff">{{ bot_count }}</b><br>
                Servers: <b style="color:#fff">{{ server_count }}</b><br>
                <br>
                <i class="fas fa-cloud"></i> JSONBin: 
                <b style="color:{{ 'lime' if has_jsonbin else 'red' }}">{{ 'CONNECTED' if has_jsonbin else 'MISSING' }}</b>
            </div>
        </div>
        <div id="groupsList" class="groups-area"></div>
    </div>

    <script>
        const bots = {{ bots_json|safe }};
        const folderData = {{ folders_json|safe }};

        function createPanelHTML(id, grp) {
            let botChecks = bots.map(b => 
                `<label class="bot-item"><input type="checkbox" value="${b.index}" ${grp.bots.includes(b.index)?'checked':''}> Bot ${b.index+1}: ${b.name}</label>`
            ).join('');

            let folderHtml = '';
            if (folderData.length === 0) {
                folderHtml = '<div style="padding:20px; text-align:center; color:#666">Chưa có dữ liệu.<br>Bấm nút "QUÉT LẠI SERVER" bên trái.</div>';
            } else {
                folderData.forEach(folder => {
                    let serverHtml = '';
                    folder.servers.forEach(s => {
                        const checked = grp.servers.includes(s.id) ? 'checked' : '';
                        serverHtml += `<label class="server-item"><input type="checkbox" class="sv-cb-${id}" data-folder="${folder.id}" value="${s.id}" ${checked}> ${s.name}</label>`;
                    });

                    folderHtml += `
                    <div class="folder-group">
                        <div class="folder-header" onclick="toggleFolderContent(this)">
                            <input type="checkbox" onclick="toggleFolderAll('${id}', '${folder.id}', this); event.stopPropagation();"> 
                            <i class="fas fa-folder" style="margin-right:8px; color:#ffd700"></i> ${folder.name} (${folder.servers.length})
                        </div>
                        <div class="folder-content">${serverHtml}</div>
                    </div>`;
                });
            }

            return `
                <div class="panel-card" id="panel-${id}">
                    <div class="panel-header">
                        <div class="panel-title">${grp.name} <span id="badge-${id}" class="badge">IDLE</span></div>
                        <button class="btn btn-sm" style="background:#ff3333" onclick="deleteGroup('${id}')"><i class="fas fa-trash"></i></button>
                    </div>
                    <div class="config-grid">
                        <div>
                            <div style="font-weight:bold; color:#00ff41; margin-bottom:5px;">CHỌN BOT</div>
                            <div class="list-box" id="bots-${id}">${botChecks}</div>
                        </div>
                        <div>
                            <div style="font-weight:bold; color:#00ff41; margin-bottom:5px;">CHỌN SERVER THEO FOLDER</div>
                            <div class="list-box" id="servers-${id}">${folderHtml}</div>
                        </div>
                    </div>
                    <textarea id="msg-${id}" placeholder="Nội dung spam...">${grp.message || ''}</textarea>
                    <div class="action-bar">
                        <button class="btn btn-sm" onclick="saveGroup('${id}')">LƯU CẤU HÌNH</button>
                        <span id="btn-area-${id}"></span>
                    </div>
                </div>
            `;
        }

        function toggleFolderContent(header) { header.nextElementSibling.classList.toggle('open'); }
        function toggleFolderAll(panelId, folderId, masterCb) {
            document.querySelectorAll(`.sv-cb-${panelId}[data-folder="${folderId}"]`).forEach(cb => cb.checked = masterCb.checked);
        }

        function refreshData() {
            const btn = document.getElementById('refreshBtn');
            const status = document.getElementById('refresh-status');
            btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ĐANG QUÉT...';
            status.innerText = "Đang gửi lệnh quét cho Bot 1...";
            
            fetch('/api/refresh_data').then(r => r.json()).then(d => {
                if(d.status === 'ok') {
                    status.innerText = "Đã gửi lệnh. Đang đợi dữ liệu...";
                    setTimeout(() => location.reload(), 4000); // F5 sau 4s
                } else {
                    alert("Lỗi: " + d.msg);
                    btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync"></i> QUÉT LẠI SERVER';
                }
            });
        }

        function renderGroups() {
            fetch('/api/groups').then(r => r.json()).then(data => {
                const container = document.getElementById('groupsList');
                const currentIds = Object.keys(data);
                Array.from(container.children).forEach(child => { if (!currentIds.includes(child.id.replace('panel-', ''))) child.remove(); });
                for (const [id, grp] of Object.entries(data)) {
                    let panel = document.getElementById(`panel-${id}`);
                    if (!panel) {
                        const div = document.createElement('div');
                        div.innerHTML = createPanelHTML(id, grp);
                        container.appendChild(div.firstElementChild);
                        panel = document.getElementById(`panel-${id}`);
                    }
                    if (grp.active) panel.classList.add('active'); else panel.classList.remove('active');
                    const badge = document.getElementById(`badge-${id}`);
                    badge.innerText = grp.active ? 'RUNNING' : 'STOPPED';
                    badge.style.background = grp.active ? '#00ff41' : '#333';
                    const btnArea = document.getElementById(`btn-area-${id}`);
                    btnArea.innerHTML = grp.active 
                        ? `<button class="btn btn-sm btn-stop" onclick="toggleGroup('${id}')">DỪNG LẠI</button>` 
                        : `<button class="btn btn-sm btn-start" onclick="toggleGroup('${id}')">BẮT ĐẦU</button>`;
                }
            });
        }
        
        function createGroup() { const name = document.getElementById('groupName').value; if(name) fetch('/api/create', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name}) }).then(() => { document.getElementById('groupName').value = ''; renderGroups(); }); }
        function saveGroup(id) { 
            const msg = document.getElementById(`msg-${id}`).value; 
            const bots = Array.from(document.querySelectorAll(`#bots-${id} input:checked`)).map(c => parseInt(c.value)); 
            const servers = Array.from(document.querySelectorAll(`#servers-${id} input:checked`)).map(c => c.value); 
            fetch('/api/update', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id, message: msg, bots, servers}) }).then(r => r.json()).then(d => alert(d.msg)); 
        }
        function toggleGroup(id) { fetch('/api/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}) }).then(() => setTimeout(renderGroups, 200)); }
        function deleteGroup(id) { if(confirm('Xóa?')) fetch('/api/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}) }).then(() => renderGroups()); }
        renderGroups(); setInterval(renderGroups, 3000);
    </script>
</body>
</html>
"""

# --- API ---
@app.route('/')
def index():
    bots_list = [{'index': k, 'name': v['name']} for k, v in bots_instances.items()]
    has_jsonbin = bool(JSONBIN_API_KEY and JSONBIN_BIN_ID)
    return render_template_string(HTML, 
        bots_json=bots_list, 
        folders_json=scanned_data['folders'], 
        bot_count=len(bots_instances), 
        server_count=len(scanned_data['servers']),
        has_jsonbin=has_jsonbin
    )

@app.route('/api/refresh_data')
def api_refresh():
    # Tìm Bot 1
    bot_data = bots_instances.get(0) # Index 0 là Bot 1
    if not bot_data:
        return jsonify({'status': 'error', 'msg': 'Bot 1 chưa Online!'})
    
    # Gửi lệnh quét vào Loop của Bot 1
    asyncio.run_coroutine_threadsafe(scan_discord_structure(bot_data['client']), bot_data['loop'])
    return jsonify({'status': 'ok', 'msg': 'Đang quét...'})

@app.route('/api/groups')
def get_groups(): return jsonify(spam_groups)

@app.route('/api/create', methods=['POST'])
def create_grp(): 
    gid = str(uuid.uuid4())[:6]
    spam_groups[gid] = {'name': request.json.get('name'), 'active': False, 'bots': [], 'servers': [], 'message': ''}
    save_settings() # Lưu ngay khi tạo
    return jsonify({'status': 'ok'})

@app.route('/api/update', methods=['POST'])
def update_grp(): 
    d = request.json
    if d['id'] in spam_groups:
        spam_groups[d['id']].update({'bots': d['bots'], 'servers': d['servers'], 'message': d['message']})
        save_settings() # Lưu ngay khi sửa
    return jsonify({'status': 'ok', 'msg': '✅ Config Saved!'})

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
        spam_groups[gid]['active'] = False
        del spam_groups[gid]
        save_settings() # Lưu ngay khi xóa
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    print("🔥 SYSTEM STARTING... (V8 - FINAL FIXED)", flush=True)
    load_settings() # Tải config cũ

    for i, t in enumerate(TOKENS):
        if t.strip(): threading.Thread(target=start_bot_node, args=(t, i), daemon=True).start(); time.sleep(1)
    
    port = int(os.environ.get("PORT", 10000))
    print(f"🌍 WEB PANEL: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port)
