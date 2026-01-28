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
                final_list.append({'folder_name': f"{folder_name}", 'servers': folder_servers})

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
    <link href="https://fonts.googleapis.com/css2?family=Oxanium:wght@300..800&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --primary: #00ff41;
            --primary-glow: rgba(0, 255, 65, 0.3);
            --bg: #06080a;
            --card-bg: #0c1116;
            --border: #1f2933;
            --text: #f0f0f0;
            --muted: #8899a6;
        }
        body { 
            background: var(--bg);
            color: var(--text); 
            font-family: 'IBM Plex Sans', sans-serif; 
            margin: 0; 
            padding: 20px;
            background-image: radial-gradient(circle at 50% 0%, #0d1a10 0%, var(--bg) 70%);
            min-height: 100vh;
        }
        .header { 
            text-align: left;
            border-bottom: 1px solid var(--border); 
            padding-bottom: 15px; 
            margin-bottom: 30px;
            display: flex;
            align-items: center;
            gap: 15px;
        }
        .header h1 { 
            color: var(--primary);
            margin: 0; 
            text-transform: uppercase; 
            font-size: 1.4rem; 
            font-family: 'Oxanium', sans-serif;
            letter-spacing: 1px;
            text-shadow: 0 0 10px var(--primary-glow);
        }
        
        .main-container { display: flex; gap: 24px; align-items: flex-start; max-width: 1600px; margin: 0 auto; }
        .sidebar { 
            width: 320px;
            background: var(--card-bg); 
            padding: 20px; 
            border-radius: 12px; 
            border: 1px solid var(--border); 
            flex-shrink: 0;
            box-shadow: 0 10px 30px rgba(0,0,0,0.5);
        }
        .sidebar h3 {
            margin-top: 0;
            font-family: 'Oxanium', sans-serif;
            font-size: 1rem;
            color: var(--primary);
            display: flex;
            align-items: center;
            gap: 8px;
        }
        
        .btn { 
            width: 100%;
            padding: 12px; 
            border: none; 
            font-weight: bold; 
            cursor: pointer; 
            border-radius: 8px; 
            margin-top: 10px; 
            font-family: inherit;
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        .btn-create { background: var(--primary); color: #000; box-shadow: 0 4px 15px var(--primary-glow); }
        .btn-create:hover { transform: translateY(-2px); box-shadow: 0 6px 20px var(--primary-glow); filter: brightness(1.1); }
        
        input[type="text"] { 
            width: 100%;
            padding: 12px; 
            background: #000; 
            border: 1px solid var(--border); 
            color: #fff; 
            margin-bottom: 12px; 
            box-sizing: border-box; 
            border-radius: 8px;
            transition: border-color 0.2s;
        }
        input[type="text"]:focus {
            border-color: var(--primary);
            outline: none;
        }
        
        .groups-area { flex: 1; display: flex; flex-direction: column; gap: 24px; }
        .panel-card { 
            background: var(--card-bg);
            border: 1px solid var(--border); 
            border-radius: 12px; 
            padding: 20px; 
            position: relative;
            transition: all 0.3s;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }
        .panel-card.active { 
            border-color: var(--primary);
            box-shadow: 0 0 20px rgba(0, 255, 65, 0.05), 0 10px 40px rgba(0,0,0,0.4);
        }
        .panel-card.active::before {
            content: '';
            position: absolute;
            top: 0; left: 0; right: 0; height: 3px;
            background: var(--primary);
            border-radius: 12px 12px 0 0;
        }
        
        .panel-header { 
            display: flex;
            justify-content: space-between; 
            align-items: center; 
            border-bottom: 1px solid var(--border); 
            padding-bottom: 15px; 
            margin-bottom: 20px;
        }
        .panel-title { 
            font-size: 1.15rem;
            font-weight: 600; 
            color: #fff;
            display: flex;
            align-items: center;
            gap: 10px;
            font-family: 'Oxanium', sans-serif;
        }
        .badge { 
            padding: 4px 10px;
            font-size: 0.75rem; 
            border-radius: 6px; 
            font-weight: 700; 
            text-transform: uppercase; 
            letter-spacing: 0.5px;
        }
        
        .config-grid { display: grid; grid-template-columns: 1fr 1.5fr; gap: 20px; margin-bottom: 20px; }
        
        .list-box { 
            height: 350px;
            overflow-y: auto; 
            background: #040608; 
            border: 1px solid var(--border); 
            padding: 8px; 
            border-radius: 10px;
        }
        .check-item { 
            display: flex;
            align-items: center; 
            padding: 8px 10px; 
            cursor: pointer; 
            border-radius: 6px;
            margin-bottom: 2px;
            font-size: 0.85rem;
            transition: background 0.2s;
        }
        .check-item:hover { background: #111b14; color: var(--primary); }
        .check-item input { 
            margin-right: 12px;
            accent-color: var(--primary);
            width: 14px;
            height: 14px;
        }

        /* --- FOLDER STYLES UPDATE --- */
        .folder-header { 
            background: #151d25;
            color: #ccc; 
            padding: 8px 12px; 
            font-weight: 600; 
            font-size: 0.85rem; 
            display: flex; 
            justify-content: space-between; 
            align-items: center;
            border-radius: 6px;
            margin: 8px 0 4px 0;
            cursor: pointer;
            user-select: none;
            transition: all 0.2s;
            border: 1px solid transparent;
        }
        .folder-header:hover {
            background: #1e2a35;
            border-color: #2d3b48;
            color: #fff;
        }
        .folder-header.active {
            border-left: 3px solid var(--primary);
            background: #1a232b;
        }
        .folder-left {
            display: flex;
            align-items: center;
            gap: 10px;
            flex: 1;
        }
        .folder-arrow {
            transition: transform 0.2s ease;
            font-size: 0.7rem;
            color: var(--muted);
        }
        .folder-header.active .folder-arrow {
            transform: rotate(90deg);
            color: var(--primary);
        }

        .btn-select-all {
            background: #252f38;
            color: var(--text); 
            border: 1px solid var(--border); 
            padding: 2px 8px; 
            font-size: 0.7rem; 
            cursor: pointer; 
            border-radius: 4px;
            transition: all 0.2s;
            margin-left: auto;
        }
        .btn-select-all:hover { background: var(--primary); color: #000; border-color: var(--primary); }
        
        .folder-content {
            display: none;
            padding-left: 6px;
            margin-bottom: 8px;
            border-left: 1px solid #1f2933;
            margin-left: 12px;
            animation: slideDown 0.2s ease-out;
        }
        .folder-content.active {
            display: block;
        }

        @keyframes slideDown {
            from { opacity: 0; transform: translateY(-5px); }
            to { opacity: 1; transform: translateY(0); }
        }

        textarea { 
            width: 100%;
            background: #040608; 
            border: 1px solid var(--border); 
            color: var(--primary); 
            padding: 15px; 
            font-family: inherit; 
            resize: vertical; 
            margin-bottom: 15px; 
            box-sizing: border-box; 
            min-height: 100px;
            border-radius: 10px;
            line-height: 1.5;
        }
        textarea:focus { border-color: var(--primary); outline: none; }
        
        .action-bar { display: flex; gap: 12px; justify-content: flex-end; border-top: 1px solid var(--border); padding-top: 20px; }
        .btn-save { background: #1f2933; color: #fff; width: auto; min-width: 100px; }
        .btn-save:hover { background: #2d3d4d; }
        .btn-start { background: var(--primary); color: #000; width: auto; min-width: 120px; }
        .btn-stop { background: #ff4d4d; color: #fff; width: auto; min-width: 120px; }
        .btn-del { background: transparent; color: #ff4d4d; width: auto; padding: 8px; border: 1px solid transparent; border-radius: 6px; }
        .btn-del:hover { background: rgba(255, 77, 77, 0.1); border-color: rgba(255, 77, 77, 0.2); }

        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 10px; }
        ::-webkit-scrollbar-thumb:hover { background: #333; }
        
        .status-info {
            padding: 15px;
            background: rgba(0, 255, 65, 0.03);
            border-radius: 10px;
            border: 1px solid rgba(0, 255, 65, 0.1);
            margin-top: 20px;
        }
    </style>
</head>
<body>
    <div class="header">
        <i class="fas fa-terminal" style="color: var(--primary); font-size: 1.2rem;"></i>
        <h1>SPAM MANAGER V7 CONTROL PANEL</h1>
    </div>
    
    <div class="main-container">
        <div class="sidebar">
            <h3><i class="fas fa-plus-circle"></i> THÊM CHIẾN DỊCH</h3>
            <input type="text" id="groupName" placeholder="Tên chiến dịch mới...">
            <button class="btn btn-create" onclick="createGroup()">KHỞI TẠO NHÓM</button>
            
            <div class="status-info">
                <div style="font-size: 0.9rem; color: var(--muted); margin-bottom: 12px; border-bottom: 1px solid rgba(255,255,255,0.05); padding-bottom: 8px;">
                    HỆ THỐNG
                </div>
                <div style="margin-bottom: 8px;"><i class="fas fa-robot"></i> Bots Active: <span style="color:var(--primary); font-weight:bold;">{{ bot_count }}</span></div>
                <div><i class="fas fa-satellite-dish"></i> API Scan: <span style="color: var(--primary); font-size: 0.85rem;">ONLINE</span></div>
            </div>
            
            <button class="btn" style="background: transparent; border: 1px solid var(--border); color: var(--muted); font-size: 0.8rem; margin-top: 20px;" onclick="location.reload()">
                <i class="fas fa-sync-alt"></i> LÀM MỚI DỮ LIỆU
            </button>
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
                    <i class="fas fa-user-circle" style="margin-right:8px; opacity:0.5"></i>
                    <span>Bot ${b.index + 1}: ${b.name}</span>
                </label>`;
            });

            let serverListHTML = '';
            if (folderData.length === 0) {
                serverListHTML = '<div style="padding:40px; color:var(--muted); text-align:center;"><i class="fas fa-spinner fa-spin" style="font-size:2rem; margin-bottom:15px;"></i><br>Đang đồng bộ hóa dữ liệu từ Discord API...</div>';
            } else {
                folderData.forEach((folder, fIndex) => {
                    const folderIdRaw = `f-${id}-${fIndex}`;
                    // Sửa lại giao diện Header folder: Thêm onclick toggleAccordion
                    serverListHTML += `
                        <div class="folder-header" onclick="toggleAccordion('${folderIdRaw}', this)">
                            <div class="folder-left">
                                <i class="fas fa-chevron-right folder-arrow"></i>
                                <span><i class="fas fa-folder" style="color:var(--primary); margin-right:6px;"></i>${folder.folder_name}</span>
                                <span style="opacity:0.5; font-size:0.75rem">(${folder.servers.length})</span>
                            </div>
                            <button class="btn-select-all" onclick="event.stopPropagation(); toggleFolder(this, '${folderIdRaw}')">Chọn hết</button>
                        </div>
                        <div id="${folderIdRaw}-container" class="folder-content">
                    `;
                    folder.servers.forEach(s => {
                        const checked = grp.servers.includes(s.id) ? 'checked' : '';
                        serverListHTML += `
                        <label class="check-item">
                            <input type="checkbox" value="${s.id}" ${checked} class="${folderIdRaw}-chk"> 
                            <img src="${s.icon}" style="width:18px; height:18px; border-radius:50%; margin-right:8px; border: 1px solid var(--border)">
                            <span style="white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${s.name}</span>
                        </label>`;
                    });
                    serverListHTML += `</div>`;
                });
            }

            return `
                <div class="panel-card" id="panel-${id}">
                    <div class="panel-header">
                        <div class="panel-title">
                            <i class="fas fa-project-diagram" style="color:var(--primary)"></i> 
                            <span>${grp.name}</span> 
                            <span id="badge-${id}" class="badge">IDLE</span>
                        </div>
                        <button class="btn btn-del" onclick="deleteGroup('${id}')" title="Xóa nhóm"><i class="fas fa-trash-alt"></i></button>
                    </div>
                    
                    <div class="config-grid">
                        <div>
                            <div style="margin-bottom:12px; font-weight:600; color:var(--primary); font-size:0.85rem; text-transform:uppercase; letter-spacing:1px;"><i class="fas fa-robot"></i> ĐIỀU KHIỂN BOTS</div>
                            <div class="list-box" id="bots-${id}">${botChecks}</div>
                        </div>
                        <div>
                            <div style="margin-bottom:12px; font-weight:600; color:var(--primary); font-size:0.85rem; text-transform:uppercase; letter-spacing:1px;"><i class="fas fa-server"></i> MỤC TIÊU SERVERS</div>
                            <div class="list-box" id="servers-${id}">${serverListHTML}</div>
                        </div>
                    </div>
                    
                    <div>
                        <div style="margin-bottom:10px; font-weight:600; font-size:0.85rem; text-transform:uppercase; color:var(--muted); letter-spacing:1px;">NỘI DUNG CHIẾN DỊCH</div>
                        <textarea id="msg-${id}" placeholder="Nhập tin nhắn spam tại đây...">${grp.message || ''}</textarea>
                    </div>
                    
                    <div class="action-bar">
                        <button class="btn btn-save" onclick="saveGroup('${id}')"><i class="fas fa-cloud-upload-alt"></i> LƯU CẤU HÌNH</button>
                        <span id="btn-area-${id}"></span>
                    </div>
                </div>
            `;
        }

        // --- NEW FUNCTION: TOGGLE ACCORDION ---
        function toggleAccordion(id, headerEl) {
            const content = document.getElementById(id + '-container');
            // Toggle class active
            content.classList.toggle('active');
            headerEl.classList.toggle('active');
        }

        function toggleFolder(btn, classPrefix) {
            const container = document.getElementById(classPrefix + '-container');
            const checkboxes = container.querySelectorAll('input[type="checkbox"]');
            
            // Nếu folder đang đóng, tự động mở ra khi bấm chọn hết để user thấy
            if (!container.classList.contains('active')) {
                container.classList.add('active');
                const header = container.previousElementSibling;
                if(header) header.classList.add('active');
            }

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
                    // Chỉ update trạng thái, KHÔNG render lại HTML để giữ nguyên trạng thái đóng mở folder
                    if (grp.active) panel.classList.add('active');
                    else panel.classList.remove('active');
                    
                    const badge = document.getElementById(`badge-${id}`);
                    badge.innerText = grp.active ? 'RUNNING' : 'IDLE';
                    badge.style.background = grp.active ? 'var(--primary)' : '#1f2933';
                    badge.style.color = grp.active ? '#000' : '#8899a6';
                    badge.style.boxShadow = grp.active ? '0 0 15px var(--primary-glow)' : 'none';
                    
                    const btnArea = document.getElementById(`btn-area-${id}`);
                    if (grp.active) {
                        btnArea.innerHTML = `<button class="btn btn-stop" onclick="toggleGroup('${id}')"><i class="fas fa-stop-circle"></i> STOP SYSTEM</button>`;
                    } else {
                        btnArea.innerHTML = `<button class="btn btn-start" onclick="toggleGroup('${id}')"><i class="fas fa-play-circle"></i> START SYSTEM</button>`;
                    }
                }
            });
        }

        function createGroup() {
            const name = document.getElementById('groupName').value;
            if(!name) return alert("Vui lòng nhập tên chiến dịch!");
            fetch('/api/create', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name}) })
            .then(() => { document.getElementById('groupName').value = ''; renderGroups(); });
        }

        function saveGroup(id) {
            const msg = document.getElementById(`msg-${id}`).value;
            const bots = Array.from(document.querySelectorAll(`#bots-${id} input:checked`)).map(c => parseInt(c.value));
            const servers = Array.from(document.querySelectorAll(`#servers-${id} input:checked`)).map(c => c.value);
            fetch('/api/update', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id, message: msg, bots, servers}) })
            .then(r => r.json()).then(d => {
                const btn = document.querySelector(`#panel-${id} .btn-save`);
                const oldText = btn.innerHTML;
                btn.innerHTML = '<i class="fas fa-check"></i> ĐÃ LƯU!';
                btn.style.background = 'var(--primary)';
                btn.style.color = '#000';
                setTimeout(() => {
                    btn.innerHTML = oldText;
                    btn.style.background = '';
                    btn.style.color = '';
                }, 2000);
            });
        }

        function toggleGroup(id) {
            fetch('/api/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}) })
            .then(() => setTimeout(renderGroups, 200));
        }

        function deleteGroup(id) {
            if(confirm('Bạn có chắc muốn xóa vĩnh viễn chiến dịch này?')) {
                fetch('/api/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}) })
                .then(() => renderGroups());
            }
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
        spam_groups[gid]['active'] = False;
        del spam_groups[gid]
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    print("🔥 SYSTEM V7 STARTING... (API Mode Scan)", flush=True)
    for i, t in enumerate(TOKENS):
        if t.strip(): threading.Thread(target=start_bot_node, args=(t, i), daemon=True).start();
        time.sleep(1)
    
    port = int(os.environ.get("PORT", 10000))
    print(f"🌍 WEB PANEL: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port)
