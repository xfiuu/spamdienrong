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

# Lọc bỏ token rỗng
TOKENS = [t.strip() for t in TOKENS if t.strip()]

if not TOKENS:
    print("❌ LỖI: Chưa nhập Tokens trong file .env")

# Tắt bớt log rác của thư viện, chỉ hiện lỗi quan trọng
logging.getLogger('discord').setLevel(logging.ERROR)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)

# --- DỮ LIỆU TOÀN CỤC ---
bots_instances = {}    
scanned_data = {"folders": [], "servers": {}} 
spam_groups = {}       
channel_cache = {}     

# ==========================================
# PHẦN CORE LOGIC ĐÃ FIX LỖI (QUAN TRỌNG)
# ==========================================

def send_message_from_sync(bot_index, channel_id, content):
    """Hàm gửi tin nhắn an toàn, có báo lỗi chi tiết"""
    bot_data = bots_instances.get(bot_index)
    if not bot_data: return
    bot = bot_data['client']
    loop = bot_data['loop']

    async def _send():
        try:
            # 1. Thử lấy kênh từ Cache
            channel = bot.get_channel(int(channel_id))
            
            # 2. Nếu Cache không có, thử Fetch từ API (tốn thời gian hơn chút nhưng chắc chắn)
            if not channel:
                try:
                    channel = await bot.fetch_channel(int(channel_id))
                except discord.NotFound:
                    print(f"❌ [Bot {bot_index+1}] Kênh {channel_id} không tồn tại.")
                    return
                except discord.Forbidden:
                    print(f"🚫 [Bot {bot_index+1}] Không có quyền xem kênh {channel_id}.")
                    return
                except Exception as e:
                    # Lỗi mạng hoặc lỗi lạ
                    return 

            # 3. Gửi tin nhắn
            await channel.send(content)
            # Uncomment dòng dưới nếu muốn thấy log gửi thành công (sẽ spam console)
            # print(f"✅ [Bot {bot_index+1}] Sent to {channel.name}")

        except discord.Forbidden:
            print(f"🚫 [Bot {bot_index+1}] Bị chặn chat tại kênh {channel_id}")
        except discord.HTTPException as e:
            if e.status == 429:
                print(f"⏳ [Bot {bot_index+1}] Rate Limit! Đang chờ...")
            else:
                print(f"❌ [Bot {bot_index+1}] Lỗi HTTP: {e}")
        except Exception as e:
            print(f"❌ [Bot {bot_index+1}] Lỗi không xác định: {e}")

    if loop.is_running():
        asyncio.run_coroutine_threadsafe(_send(), loop)

def resolve_spam_channel(bot_indices, guild_id):
    """Tìm kênh để spam (Ưu tiên cache -> Tìm 'spam' -> Tìm 'chat')"""
    guild_id = str(guild_id)
    if guild_id in channel_cache: return channel_cache[guild_id]
    
    target_channel_id = None
    
    for b_idx in bot_indices:
        bot_data = bots_instances.get(b_idx)
        if not bot_data: continue
        bot = bot_data['client']
        
        guild = bot.get_guild(int(guild_id))
        if not guild: continue
        
        text_channels = guild.text_channels
        
        # Ưu tiên 1: Kênh có tên chứa 'spam' hoặc 'chat'
        candidates = [c for c in text_channels if 'spam' in c.name.lower() or 'chat' in c.name.lower()]
        
        if candidates:
            # Lấy cái đầu tiên tìm thấy
            exact = candidates[0] 
            target_channel_id = exact.id
        elif text_channels:
            # Ưu tiên 2: Lấy kênh chat bất kỳ đầu tiên (nếu không tìm thấy spam/chat)
            # Cảnh báo: Có thể spam nhầm vào kênh rule/welcome
            target_channel_id = text_channels[0].id

        if target_channel_id:
            channel_cache[guild_id] = target_channel_id
            print(f"🔎 [Server {guild.name}] Chọn kênh: {target_channel_id}")
            return target_channel_id
            
    return None

def run_spam_group_logic(group_id):
    """Luồng xử lý spam đa luồng"""
    print(f"🚀 [Group {group_id}] Bắt đầu chạy...", flush=True)
    server_pair_index = 0
    DELAY_BETWEEN_PAIRS = 2.0  # Nghỉ giữa các cặp server
    DELAY_WITHIN_PAIR = 1.0    # Nghỉ giữa server 1 và server 2 trong cặp
    MAX_THREADS = 5            # Số bot gửi đồng thời

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

        # Logic xoay vòng server
        if server_pair_index * 2 >= len(target_servers):
            server_pair_index = 0
            time.sleep(1) # Nghỉ nhẹ khi hết vòng
        
        start_index = server_pair_index * 2
        current_pair_ids = target_servers[start_index : start_index + 2]
        
        if not current_pair_ids:
            server_pair_index = 0; continue

        # Lấy ID kênh chat cho các server trong cặp
        valid_targets = []
        for s_id in current_pair_ids:
            c_id = resolve_spam_channel(target_bots, s_id)
            if c_id: valid_targets.append((s_id, c_id))

        if not valid_targets:
            server_pair_index += 1; continue

        # Chia nhỏ Bot ra để gửi (Multi-threading sending)
        bot_chunks = [target_bots[i:i + MAX_THREADS] for i in range(0, len(target_bots), MAX_THREADS)]
        threads = []
        
        for bot_chunk in bot_chunks:
            def thread_task(bots=bot_chunk, targets=valid_targets):
                # Gửi Server 1
                if len(targets) > 0:
                    svr1_id, ch1_id = targets[0]
                    for b_idx in bots:
                        send_message_from_sync(b_idx, ch1_id, message)
                        time.sleep(0.05) # Delay cực nhỏ để tránh crash socket
                
                # Gửi Server 2 (nếu có)
                if len(targets) > 1:
                    time.sleep(DELAY_WITHIN_PAIR)
                    svr2_id, ch2_id = targets[1]
                    for b_idx in bots:
                        send_message_from_sync(b_idx, ch2_id, message)
                        time.sleep(0.05)
                        
            t = threading.Thread(target=thread_task)
            threads.append(t); t.start()
        
        for t in threads: t.join()
        
        time.sleep(DELAY_BETWEEN_PAIRS)
        server_pair_index += 1

# ==========================================
# KHỞI TẠO BOT & QUÉT FOLDER
# ==========================================

def start_bot_node(token, index):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Quan trọng: self_bot=True để dùng Token user
    bot = discord.Client(self_bot=True)

    @bot.event
    async def on_ready():
        print(f"✅ Bot {index+1}: {bot.user.name} Connected!", flush=True)
        bots_instances[index] = {
            'client': bot, 'loop': loop, 'name': bot.user.name, 'id': bot.user.id
        }
        
        # CHỈ BOT 1 QUÉT FOLDER (Để tránh spam API lấy folder)
        if index == 0:
            print(f"📡 [Bot 1] Đang đọc cấu trúc Thư mục (Folder) từ Discord...", flush=True)
            await asyncio.sleep(5) 
            
            # 1. Lấy danh sách Guild cơ bản
            temp_servers = {}
            for guild in bot.guilds:
                icon_link = str(guild.icon.url) if guild.icon else "https://cdn.discordapp.com/embed/avatars/0.png"
                temp_servers[str(guild.id)] = {'id': str(guild.id), 'name': guild.name, 'icon': icon_link}

            # 2. Gọi API lấy User Settings để xem Folder
            try:
                user_settings = await bot.http.request(discord.http.Route('GET', '/users/@me/settings'))
                guild_folders = user_settings.get('guild_folders', [])
                
                folders_structure = []
                scanned_ids = []

                for folder in guild_folders:
                    folder_id = str(folder.get('id', 'unknown'))
                    folder_name = folder.get('name')
                    guild_ids = [str(gid) for gid in folder.get('guild_ids', [])]
                    
                    if not folder_name: folder_name = f"Folder {folder_id[:4]}"
                    
                    folder_servers = []
                    for gid in guild_ids:
                        if gid in temp_servers:
                            folder_servers.append(temp_servers[gid])
                            scanned_ids.append(gid)
                    
                    if folder_servers:
                        folders_structure.append({'id': folder_id, 'name': folder_name, 'servers': folder_servers})

                # Server chưa xếp folder
                uncategorized = [s for gid, s in temp_servers.items() if gid not in scanned_ids]
                if uncategorized:
                    folders_structure.append({'id': 'uncategorized', 'name': 'Server Lẻ', 'servers': uncategorized})

                scanned_data['folders'] = folders_structure
                scanned_data['servers'] = temp_servers
                
                print(f"✨ [Bot 1] Đã quét xong: {len(folders_structure)} Folder, {len(temp_servers)} Server.", flush=True)

            except Exception as e:
                print(f"⚠️ [Bot 1] Lỗi đọc Folder: {e}. Dùng danh sách thường.", flush=True)
                scanned_data['folders'] = [{'id': 'all', 'name': 'Tất cả Server', 'servers': list(temp_servers.values())}]
                scanned_data['servers'] = temp_servers

    try:
        loop.run_until_complete(bot.start(token))
    except Exception as e:
        print(f"❌ Bot {index+1} lỗi login: {e}")

# ==========================================
# GIAO DIỆN WEB (HTML)
# ==========================================
HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SPAM TOOL V7 - FIXED EDITION</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        body { background: #0f0f0f; color: #f0f0f0; font-family: 'Segoe UI', monospace; margin: 0; padding: 20px; font-size: 14px;}
        .header { text-align: center; border-bottom: 2px solid #00ff41; padding-bottom: 10px; margin-bottom: 20px; }
        .header h1 { color: #00ff41; margin: 0; text-transform: uppercase; }
        
        .main-container { display: flex; gap: 20px; align-items: flex-start; }
        .sidebar { width: 300px; background: #1a1a1a; padding: 20px; border-radius: 8px; border: 1px solid #333; }
        
        .btn { width: 100%; padding: 10px; border: none; font-weight: bold; cursor: pointer; border-radius: 4px; margin-top: 5px; color: #000; }
        .btn-create { background: #00ff41; }
        
        input[type="text"] { width: 100%; padding: 8px; background: #000; border: 1px solid #444; color: #fff; margin-bottom: 10px; box-sizing: border-box; }
        
        .groups-area { flex: 1; display: flex; flex-direction: column; gap: 20px; }
        .panel-card { background: #1a1a1a; border: 1px solid #333; border-radius: 8px; padding: 15px; position: relative; }
        .panel-card.active { border-color: #00ff41; box-shadow: 0 0 10px rgba(0, 255, 65, 0.1); }
        
        .panel-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 15px; }
        .badge { padding: 2px 6px; font-size: 0.8em; border-radius: 4px; margin-left: 10px; font-weight: bold; }
        
        .config-grid { display: grid; grid-template-columns: 1fr 2fr; gap: 15px; margin-bottom: 15px; }
        
        .list-box { height: 350px; overflow-y: auto; background: #050505; border: 1px solid #333; padding: 5px; }
        
        .folder-group { margin-bottom: 10px; border: 1px solid #333; border-radius: 4px; overflow: hidden; }
        .folder-header { 
            background: #222; padding: 8px; cursor: pointer; display: flex; align-items: center; font-weight: bold; color: #aaa;
            border-bottom: 1px solid #333;
        }
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
    <div class="header"><h1><i class="fas fa-robot"></i> SPAM TOOL V7 - FINAL FIX</h1></div>
    
    <div class="main-container">
        <div class="sidebar">
            <h3>Tạo Panel Mới</h3>
            <input type="text" id="groupName" placeholder="Tên nhóm...">
            <button class="btn btn-create" onclick="createGroup()">+ TẠO NHÓM</button>
            <div style="margin-top:20px; font-size:0.9em; color:#888;">
                * Dữ liệu thư mục được lấy từ Bot 1.<br>
                * Đợi 10s sau khi chạy tool để bot load xong server.
            </div>
            <button class="btn" style="background:#333; color:#aaa; margin-top:20px;" onclick="location.reload()">Refresh Data</button>
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
                folderHtml = '<div style="padding:20px; text-align:center; color:#666">Đang tải folder...<br>Đợi bot load xong rồi F5 lại web.</div>';
            } else {
                folderData.forEach(folder => {
                    let serverHtml = '';
                    folder.servers.forEach(s => {
                        const checked = grp.servers.includes(s.id) ? 'checked' : '';
                        serverHtml += `
                        <label class="server-item">
                            <input type="checkbox" class="sv-cb-${id}" data-folder="${folder.id}" value="${s.id}" ${checked}> 
                            ${s.name}
                        </label>`;
                    });

                    folderHtml += `
                    <div class="folder-group">
                        <div class="folder-header" onclick="toggleFolderContent(this)">
                            <input type="checkbox" onclick="toggleFolderAll('${id}', '${folder.id}', this); event.stopPropagation();"> 
                            <i class="fas fa-folder" style="margin-right:8px; color:#ffd700"></i> ${folder.name} (${folder.servers.length})
                            <i class="fas fa-chevron-down" style="margin-left:auto; font-size:0.8em"></i>
                        </div>
                        <div class="folder-content">
                            ${serverHtml}
                        </div>
                    </div>`;
                });
            }

            return `
                <div class="panel-card" id="panel-${id}">
                    <div class="panel-header">
                        <div class="panel-title" style="font-weight:bold; font-size:1.1em">${grp.name} <span id="badge-${id}" class="badge">IDLE</span></div>
                        <button class="btn btn-sm" style="background:#ff3333" onclick="deleteGroup('${id}')"><i class="fas fa-trash"></i></button>
                    </div>
                    <div class="config-grid">
                        <div>
                            <div style="font-weight:bold; color:#00ff41; margin-bottom:5px;">1. CHỌN BOT</div>
                            <div class="list-box" id="bots-${id}">${botChecks}</div>
                        </div>
                        <div>
                            <div style="font-weight:bold; color:#00ff41; margin-bottom:5px;">2. CHỌN SERVER</div>
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

        function toggleFolderContent(header) {
            const content = header.nextElementSibling;
            content.classList.toggle('open');
            const icon = header.querySelector('.fa-chevron-down');
            icon.style.transform = content.classList.contains('open') ? 'rotate(180deg)' : 'rotate(0deg)';
        }

        function toggleFolderAll(panelId, folderId, masterCb) {
            const container = document.getElementById(`servers-${panelId}`);
            const childCbs = container.querySelectorAll(`.sv-cb-${panelId}[data-folder="${folderId}"]`);
            childCbs.forEach(cb => cb.checked = masterCb.checked);
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
                    
                    const badge = document.getElementById(`badge-${id}`);
                    if (grp.active) {
                        panel.classList.add('active');
                        badge.innerText = 'RUNNING';
                        badge.style.background = '#00ff41';
                        badge.style.color = '#000';
                    } else {
                        panel.classList.remove('active');
                        badge.innerText = 'STOPPED';
                        badge.style.background = '#333';
                        badge.style.color = '#fff';
                    }

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
        function deleteGroup(id) { if(confirm('Xóa nhóm này?')) fetch('/api/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}) }).then(() => renderGroups()); }

        renderGroups(); setInterval(renderGroups, 2000);
    </script>
</body>
</html>
"""

# ==========================================
# FLASK API
# ==========================================

@app.route('/')
def index():
    bots_list = [{'index': k, 'name': v['name']} for k, v in bots_instances.items()]
    return render_template_string(HTML, bots_json=bots_list, folders_json=scanned_data['folders'])

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
    return jsonify({'status': 'ok', 'msg': '✅ Config Saved!'})

@app.route('/api/toggle', methods=['POST'])
def toggle_grp(): 
    gid = request.json['id']
    if gid in spam_groups:
        curr = spam_groups[gid]['active']
        spam_groups[gid]['active'] = not curr
        if not curr:
            threading.Thread(target=run_spam_group_logic, args=(gid,), daemon=True).start()
    return jsonify({'status': 'ok'})

@app.route('/api/delete', methods=['POST'])
def del_grp(): 
    gid = request.json['id']
    if gid in spam_groups:
        spam_groups[gid]['active'] = False
        del spam_groups[gid]
    return jsonify({'status': 'ok'})

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == '__main__':
    print("🔥 SYSTEM STARTING... (V7 - Fixed Edition)", flush=True)
    
    # Khởi chạy từng bot trong Thread riêng
    for i, t in enumerate(TOKENS):
        threading.Thread(target=start_bot_node, args=(t, i), daemon=True).start()
        time.sleep(1) # Delay nhẹ để tránh login đồng thời
        
    port = int(os.environ.get("PORT", 10000))
    print(f"🌍 WEB PANEL: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port)
