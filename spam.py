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
    exit()

# Tắt log rác
logging.getLogger('discord').setLevel(logging.WARNING)

app = Flask(__name__)

# --- DỮ LIỆU ---
bots_instances = {}   
scanned_servers = {}  
spam_groups = {}      
channel_cache = {}    

# --- CORE LOGIC ---
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
        for ch in guild.text_channels:
            if ch.name == "spam":
                target_channel_id = ch.id; break
        if not target_channel_id:
            for ch in guild.text_channels:
                if "spam" in ch.name.lower():
                    target_channel_id = ch.id; break
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

# --- FIX: CẬP NHẬT QUÉT SERVER ---
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
        
        # FIX: Chờ bot load xong hẳn mới quét
        if index == 0:
            print("📡 [MASTER] Bot 1 đang đợi server load...", flush=True)
            await asyncio.sleep(5) # Đợi 5s để discord cache server
            count = 0
            for guild in bot.guilds:
                scanned_servers[str(guild.id)] = {
                    'name': guild.name,
                    'icon': str(guild.icon_url)
                }
                count += 1
            print(f"📡 [MASTER] Đã tìm thấy {count} Servers (Từ Bot 1).", flush=True)

    try:
        loop.run_until_complete(bot.start(token.strip()))
    except Exception as e:
        print(f"❌ Bot {index+1} lỗi: {e}")

# --- FIX: GIAO DIỆN KHÔNG BỊ LOAD LẠI ---
HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SPAM TOOL PRO</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        body { background: #121212; color: #e0e0e0; font-family: 'Segoe UI', monospace; margin: 0; padding: 20px; }
        .header { text-align: center; border-bottom: 2px solid #00ff41; padding-bottom: 10px; margin-bottom: 20px; }
        .header h1 { color: #00ff41; margin: 0; }
        .main-container { display: flex; gap: 20px; align-items: flex-start; }
        .sidebar { width: 300px; background: #1e1e1e; padding: 15px; border-radius: 8px; border: 1px solid #333; }
        .btn { width: 100%; padding: 10px; border: none; font-weight: bold; cursor: pointer; border-radius: 4px; margin-top: 5px; }
        .btn-create { background: #00ff41; color: #000; }
        input[type="text"] { width: 92%; padding: 8px; background: #000; border: 1px solid #444; color: #fff; margin-bottom: 10px; }
        .groups-area { flex: 1; display: flex; flex-direction: column; gap: 20px; }
        .panel-card { background: #1e1e1e; border: 1px solid #333; border-radius: 8px; padding: 15px; position: relative; }
        .panel-card.active { border-color: #00ff41; box-shadow: 0 0 10px rgba(0, 255, 65, 0.1); }
        .panel-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 10px; }
        .badge { background: #333; padding: 2px 6px; font-size: 0.7em; border-radius: 4px; margin-left: 5px; }
        .config-row { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 15px; }
        .check-list { height: 250px; overflow-y: auto; background: #000; border: 1px solid #333; padding: 5px; }
        .check-item { display: block; padding: 4px; cursor: pointer; font-size: 0.9em; border-bottom: 1px solid #222; }
        .check-item:hover { background: #222; color: #00ff41; }
        textarea { width: 100%; background: #000; border: 1px solid #333; color: #00ff41; padding: 10px; font-family: inherit; resize: vertical; margin-bottom: 10px; box-sizing: border-box;}
        .action-bar { display: flex; gap: 10px; justify-content: flex-end; }
        .btn-save { background: #333; color: #fff; width: auto; }
        .btn-start { background: #00ff41; color: #000; width: auto; }
        .btn-stop { background: #ff3333; color: #fff; width: auto; }
        .btn-del { background: #ff3333; color: #fff; width: auto; padding: 5px 10px; font-size: 0.8em; }
    </style>
</head>
<body>
    <div class="header"><h1>Multi-Panel Spam Tool (Fixed)</h1></div>
    <div class="main-container">
        <div class="sidebar">
            <h3><i class="fas fa-plus-circle"></i> Create Panel</h3>
            <input type="text" id="groupName" placeholder="Tên nhóm...">
            <button class="btn btn-create" onclick="createGroup()">Tạo Nhóm Mới</button>
            <hr style="border-color: #333; margin: 20px 0;">
            <div style="font-size: 0.85em; color: #888;">
                <p>Bot Online: <b style="color: #fff">{{ bot_count }}</b></p>
                <p>Servers (Bot 1): <b style="color: #fff">{{ server_count }}</b></p>
            </div>
        </div>
        <div id="groupsList" class="groups-area"></div>
    </div>

    <script>
        const bots = {{ bots_json|safe }};
        const servers = {{ servers_json|safe }};

        // Hàm này chỉ tạo HTML nếu Panel chưa tồn tại
        function createPanelHTML(id, grp) {
            let botChecks = '';
            bots.forEach(b => {
                const checked = grp.bots.includes(b.index) ? 'checked' : '';
                botChecks += `<label class="check-item"><input type="checkbox" value="${b.index}" ${checked}> ${b.name}</label>`;
            });

            let serverChecks = '';
            servers.forEach(s => {
                const checked = grp.servers.includes(s.id) ? 'checked' : '';
                serverChecks += `<label class="check-item"><input type="checkbox" value="${s.id}" ${checked}> ${s.name}</label>`;
            });

            return `
                <div class="panel-card" id="panel-${id}">
                    <div class="panel-header">
                        <div class="panel-title"><span id="title-${id}">${grp.name}</span> <span id="badge-${id}" class="badge">IDLE</span></div>
                        <button class="btn btn-del" onclick="deleteGroup('${id}')"><i class="fas fa-trash"></i></button>
                    </div>
                    <div class="config-row">
                        <div>
                            <div style="margin-bottom:5px; font-weight:bold; color:#00ff41">🤖 Chọn Bot</div>
                            <div class="check-list" id="bots-${id}">${botChecks}</div>
                        </div>
                        <div>
                            <div style="margin-bottom:5px; font-weight:bold; color:#00ff41">📂 Chọn Server</div>
                            <div class="check-list" id="servers-${id}">${serverChecks}</div>
                        </div>
                    </div>
                    <div>
                        <div style="margin-bottom:5px; font-weight:bold;">💬 Nội dung Spam</div>
                        <textarea id="msg-${id}" rows="2">${grp.message || ''}</textarea>
                    </div>
                    <div class="action-bar">
                        <button class="btn btn-save" onclick="saveGroup('${id}')"><i class="fas fa-save"></i> Lưu Config</button>
                        <span id="btn-area-${id}"></span>
                    </div>
                </div>
            `;
        }

        function renderGroups() {
            fetch('/api/groups').then(r => r.json()).then(data => {
                const container = document.getElementById('groupsList');
                
                // Xóa các panel không còn tồn tại trên server
                const currentIds = Object.keys(data);
                Array.from(container.children).forEach(child => {
                    const childId = child.id.replace('panel-', '');
                    if (!currentIds.includes(childId)) child.remove();
                });

                for (const [id, grp] of Object.entries(data)) {
                    let panel = document.getElementById(`panel-${id}`);
                    
                    // Nếu Panel chưa có -> Tạo mới
                    if (!panel) {
                        const div = document.createElement('div');
                        div.innerHTML = createPanelHTML(id, grp);
                        container.appendChild(div.firstElementChild);
                        panel = document.getElementById(`panel-${id}`);
                    }

                    // --- CHỈ CẬP NHẬT TRẠNG THÁI (KHÔNG CẬP NHẬT CHECKBOX) ---
                    // Cập nhật class Active/Idle
                    if (grp.active) panel.classList.add('active');
                    else panel.classList.remove('active');

                    // Cập nhật Badge
                    const badge = document.getElementById(`badge-${id}`);
                    badge.innerText = grp.active ? 'RUNNING' : 'IDLE';
                    badge.style.background = grp.active ? '#00ff41' : '#333';
                    badge.style.color = grp.active ? '#000' : '#fff';

                    // Cập nhật nút Bấm (Start/Stop)
                    const btnArea = document.getElementById(`btn-area-${id}`);
                    if (grp.active) {
                        btnArea.innerHTML = `<button class="btn btn-stop" onclick="toggleGroup('${id}')"><i class="fas fa-stop"></i> STOP</button>`;
                    } else {
                        btnArea.innerHTML = `<button class="btn btn-start" onclick="toggleGroup('${id}')"><i class="fas fa-play"></i> START</button>`;
                    }
                    
                    // Lưu ý: Chúng ta KHÔNG chạm vào ô input/textarea để tránh mất dữ liệu khi user đang nhập
                }
            });
        }

        function createGroup() {
            const name = document.getElementById('groupName').value;
            if(!name) return;
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
        setInterval(renderGroups, 2000); // Check trạng thái mỗi 2s
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    bots_list = [{'index': k, 'name': v['name']} for k, v in bots_instances.items()]
    servers_list = [{'id': k, 'name': v['name']} for k, v in scanned_servers.items()]
    return render_template_string(HTML, bots_json=bots_list, servers_json=servers_list, bot_count=len(bots_instances), server_count=len(scanned_servers))

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
    return jsonify({'status': 'ok', 'msg': 'Đã Lưu Cấu Hình!'})

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
    print("🔥 SYSTEM STARTING...", flush=True)
    for i, t in enumerate(TOKENS):
        if t.strip(): threading.Thread(target=start_bot_node, args=(t, i), daemon=True).start(); time.sleep(1)
    port = int(os.environ.get("PORT", 10000))
    print(f"🌍 WEB PANEL: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port)
