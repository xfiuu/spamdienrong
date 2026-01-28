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

logging.getLogger('discord').setLevel(logging.WARNING)
app = Flask(__name__)

# --- DỮ LIỆU ---
bots_instances = {}   
scanned_data = {"folders": [], "servers": {}} 
spam_groups = {}      
channel_cache = {}    

# --- 1. HỆ THỐNG LƯU TRỮ ---
def save_settings():
    data = {'spam_groups': spam_groups}
    try:
        with open('spam_settings.json', 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except: pass

    if JSONBIN_API_KEY and JSONBIN_BIN_ID:
        def _upload():
            try:
                requests.put(
                    f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}",
                    json=data,
                    headers={'Content-Type': 'application/json', 'X-Master-Key': JSONBIN_API_KEY}
                )
            except: pass
        threading.Thread(target=_upload).start()

def load_settings():
    global spam_groups
    # 1. Cloud
    if JSONBIN_API_KEY and JSONBIN_BIN_ID:
        try:
            resp = requests.get(
                f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest",
                headers={'X-Master-Key': JSONBIN_API_KEY}
            )
            if resp.status_code == 200:
                spam_groups.update(resp.json().get('record', {}).get('spam_groups', {}))
                print("☁️ Đã tải config từ Cloud.", flush=True)
                return
        except: pass
    
    # 2. Local
    if os.path.exists('spam_settings.json'):
        try:
            with open('spam_settings.json', 'r', encoding='utf-8') as f:
                spam_groups.update(json.load(f).get('spam_groups', {}))
                print("📂 Đã tải config từ Local.", flush=True)
        except: pass

# --- 2. LOGIC SPAM (FIXED CACHE) ---
def send_message_from_sync(bot_index, channel_id, content):
    bot_data = bots_instances.get(bot_index)
    if not bot_data: return
    bot = bot_data['client']
    loop = bot_data['loop']
    
    async def _send():
        try:
            # FIX: Dùng fetch_channel nếu get_channel trả về None (Do chưa cache)
            channel = bot.get_channel(int(channel_id))
            if not channel:
                try: channel = await bot.fetch_channel(int(channel_id))
                except: pass
            
            if channel:
                await channel.send(content)
                # print(f"✅ Bot {bot_index+1} sent to {channel.id}", flush=True)
        except: pass

    if loop.is_running():
        asyncio.run_coroutine_threadsafe(_send(), loop)

def resolve_spam_channel(bot_indices, guild_id):
    guild_id = str(guild_id)
    if guild_id in channel_cache: return channel_cache[guild_id]
    
    for b_idx in bot_indices:
        bot_data = bots_instances.get(b_idx)
        if not bot_data: continue
        
        bot = bot_data['client']
        guild = bot.get_guild(int(guild_id))
        if not guild: continue
        
        target = None
        # Ưu tiên tìm tên 'spam'
        target = discord.utils.get(guild.text_channels, name="spam")
        
        # Nếu không có, tìm tên chứa 'spam'
        if not target:
            target = next((c for c in guild.text_channels if 'spam' in c.name.lower()), None)
            
        # Nếu không có, tìm 'chat' hoặc 'general'
        if not target:
             target = next((c for c in guild.text_channels if c.name in ['chat', 'general', 'chat-tong-hop']), None)

        if target:
            channel_cache[guild_id] = target.id
            return target.id
    return None

def run_spam_group_logic(group_id):
    print(f"🚀 [Group {group_id}] STARTED", flush=True)
    DELAY_BETWEEN_CHUNKS = 1.5
    MAX_THREADS = 4

    while True:
        group = spam_groups.get(group_id)
        if not group or not group.get('active'):
            print(f"🛑 [Group {group_id}] STOPPED", flush=True)
            break

        target_servers = group.get('servers', [])
        target_bots = group.get('bots', [])
        message = group.get('message', "")

        if not target_servers or not target_bots or not message:
            time.sleep(2); continue

        # Chia server thành các nhóm nhỏ (Batch)
        server_chunks = [target_servers[i:i + 2] for i in range(0, len(target_servers), 2)]
        
        for chunk in server_chunks:
            if not group.get('active'): break
            
            # Tìm kênh cho batch này
            valid_destinations = []
            for s_id in chunk:
                c_id = resolve_spam_channel(target_bots, s_id)
                if c_id: valid_destinations.append(c_id)
            
            if not valid_destinations: continue

            # Chia bot chạy song song
            bot_threads_list = []
            bot_subgroups = [target_bots[i:i + MAX_THREADS] for i in range(0, len(target_bots), MAX_THREADS)]
            
            for b_grp in bot_subgroups:
                def _spam_task(bots=b_grp, channels=valid_destinations):
                    for ch_id in channels:
                        for b_idx in bots:
                            send_message_from_sync(b_idx, ch_id, message)
                            time.sleep(0.05)
                
                t = threading.Thread(target=_spam_task)
                bot_threads_list.append(t)
                t.start()
            
            for t in bot_threads_list: t.join()
            time.sleep(DELAY_BETWEEN_CHUNKS)

# --- 3. QUÉT FOLDER (BOT 1) ---
async def scan_discord_structure(bot):
    print("📡 [Scanner] Đang đồng bộ dữ liệu Folder...", flush=True)
    temp_servers = {}
    for g in bot.guilds:
        icon = str(g.icon.url) if g.icon else "https://cdn.discordapp.com/embed/avatars/0.png"
        temp_servers[str(g.id)] = {'id': str(g.id), 'name': g.name, 'icon': icon}

    try:
        settings = await bot.http.request(discord.http.Route('GET', '/users/@me/settings'))
        folders = settings.get('guild_folders', [])
        final_structure = []
        scanned_ids = []

        for f in folders:
            fid = str(f.get('id') or 'unknown')
            if fid == 'None': continue 
            fname = f.get('name') or f"Folder {fid[:4]}"
            gids = [str(x) for x in f.get('guild_ids', [])]
            
            sv_list = [temp_servers[gid] for gid in gids if gid in temp_servers]
            scanned_ids.extend([s['id'] for s in sv_list])
            
            if sv_list:
                final_structure.append({'id': fid, 'name': fname, 'servers': sv_list})

        uncategorized = [s for k,s in temp_servers.items() if k not in scanned_ids]
        if uncategorized:
            final_structure.append({'id': 'root', 'name': 'Server Lẻ', 'servers': uncategorized})

        scanned_data['folders'] = final_structure
        scanned_data['servers'] = temp_servers
        print(f"✨ [Scanner] Xong: {len(final_structure)} thư mục.", flush=True)
    except Exception as e:
        print(f"⚠️ [Scanner] Lỗi: {e}")
        scanned_data['folders'] = [{'id': 'all', 'name': 'All Servers', 'servers': list(temp_servers.values())}]
        scanned_data['servers'] = temp_servers

def start_bot_node(token, index):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = discord.Client(self_bot=True)

    @bot.event
    async def on_ready():
        print(f"✅ Bot {index+1}: {bot.user.name} Ready!", flush=True)
        bots_instances[index] = {'client': bot, 'loop': loop, 'name': bot.user.name}
        if index == 0:
            await asyncio.sleep(2)
            await scan_discord_structure(bot)

    try: loop.run_until_complete(bot.start(token.strip()))
    except: pass

# --- GIAO DIỆN (FIX CHECKBOX PERSISTENCE) ---
HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SPAM TOOL V9 - PERFECTED</title>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        body { background: #0f0f0f; color: #ccc; font-family: 'Segoe UI', monospace; margin: 0; padding: 20px; font-size: 14px;}
        .container { display: flex; gap: 20px; }
        .sidebar { width: 300px; background: #1a1a1a; padding: 15px; border-radius: 8px; border: 1px solid #333; }
        .main { flex: 1; display: flex; flex-direction: column; gap: 15px; }
        
        .btn { width: 100%; padding: 10px; border: none; font-weight: bold; cursor: pointer; border-radius: 4px; margin-top: 5px; color: #000; }
        .btn-green { background: #00ff41; }
        .btn-refresh { background: #333; color: #fff; border: 1px solid #555; margin-top: 20px; }
        .btn-refresh:hover { background: #555; }
        
        input[type="text"] { width: 100%; padding: 8px; background: #000; border: 1px solid #444; color: #fff; margin-bottom: 10px; box-sizing: border-box; }
        
        .panel { background: #1a1a1a; border: 1px solid #333; border-radius: 8px; padding: 15px; }
        .panel.active { border-color: #00ff41; box-shadow: 0 0 8px rgba(0, 255, 65, 0.1); }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 10px; }
        .badge { padding: 2px 6px; font-size: 0.8em; border-radius: 4px; margin-left: 10px; }
        
        .grid { display: grid; grid-template-columns: 1fr 2fr; gap: 15px; margin-bottom: 10px; }
        .box { height: 300px; overflow-y: auto; background: #050505; border: 1px solid #333; padding: 5px; }
        
        /* FOLDER STYLING */
        .folder { margin-bottom: 5px; border: 1px solid #222; border-radius: 4px; overflow: hidden; }
        .folder-head { background: #222; padding: 8px; cursor: pointer; display: flex; align-items: center; font-weight: bold; color: #aaa; }
        .folder-head:hover { background: #2a2a2a; color: #fff; }
        .folder-head input { margin-right: 10px; transform: scale(1.2); cursor: pointer; }
        .folder-body { display: none; background: #111; }
        .folder-body.open { display: block; }
        
        .item { display: flex; align-items: center; padding: 5px 10px; border-bottom: 1px solid #222; color: #888; }
        .item:hover { background: #151515; color: #fff; }
        .item input { margin-right: 10px; }
        
        textarea { width: 100%; background: #000; border: 1px solid #333; color: #00ff41; padding: 10px; resize: vertical; min-height: 60px; box-sizing: border-box;}
        .actions { display: flex; gap: 10px; justify-content: flex-end; border-top: 1px solid #333; padding-top: 10px; }
        .btn-sm { width: auto; padding: 6px 15px; color: #fff; background: #333; }
        .btn-start { background: #00ff41; color: #000; }
        .btn-stop { background: #ff3333; color: #fff; }
    </style>
</head>
<body>
    <div style="text-align:center; margin-bottom:20px; border-bottom:2px solid #00ff41; padding-bottom:10px;">
        <h1 style="margin:0; color:#00ff41;">SPAM TOOL V9 (FIXED)</h1>
    </div>
    
    <div class="container">
        <div class="sidebar">
            <h3>Tạo Panel</h3>
            <input type="text" id="gName" placeholder="Tên nhóm...">
            <button class="btn btn-green" onclick="create()">+ TẠO</button>
            
            <button class="btn btn-refresh" onclick="refresh()" id="rfBtn"><i class="fas fa-sync"></i> QUÉT LẠI SERVER</button>
            <div id="rfStatus" style="text-align:center; font-size:0.8em; color:#666; margin-top:5px;"></div>
            
            <div style="margin-top:20px; font-size:0.9em; color:#666;">
                Bots: <b style="color:#fff">{{ bot_count }}</b> | Servers: <b style="color:#fff">{{ server_count }}</b>
            </div>
        </div>
        <div id="list" class="main"></div>
    </div>

    <script>
        const bots = {{ bots_json|safe }};
        const folders = {{ folders_json|safe }};

        function render(data) {
            const list = document.getElementById('list');
            const ids = Object.keys(data);
            Array.from(list.children).forEach(c => { if(!ids.includes(c.id.substring(6))) c.remove(); });

            for (const [id, grp] of Object.entries(data)) {
                let el = document.getElementById(`panel-${id}`);
                if (!el) {
                    el = document.createElement('div');
                    el.id = `panel-${id}`;
                    el.className = 'panel';
                    el.innerHTML = buildHTML(id, grp);
                    list.appendChild(el);
                } else {
                    // Update dynamic parts if needed, but avoiding full re-render to keep open folders open
                    updateStatus(id, grp);
                }
            }
        }

        function buildHTML(id, grp) {
            // Bots HTML
            let bHtml = bots.map(b => 
                `<label class="item"><input type="checkbox" value="${b.index}" ${grp.bots.includes(b.index)?'checked':''}> Bot ${b.index+1}: ${b.name}</label>`
            ).join('');

            // Folders HTML with CHECK LOGIC FIX
            let fHtml = '';
            if (folders.length === 0) fHtml = '<div style="padding:20px; text-align:center;">Chưa có dữ liệu server. Hãy bấm Quét Lại.</div>';
            else {
                folders.forEach(f => {
                    // --- LOGIC FIX: Kiểm tra xem folder có nên được tích hay không ---
                    let totalSv = f.servers.length;
                    let checkedSv = 0;
                    let sHtml = '';
                    
                    f.servers.forEach(s => {
                        const isChecked = grp.servers.includes(s.id);
                        if(isChecked) checkedSv++;
                        sHtml += `<label class="item"><input type="checkbox" class="sc-${id}-${f.id}" value="${s.id}" ${isChecked?'checked':''}> ${s.name}</label>`;
                    });

                    // Nếu tất cả server trong folder đều được chọn -> Tích vào folder cha
                    const folderChecked = (totalSv > 0 && totalSv === checkedSv) ? 'checked' : '';
                    
                    fHtml += `
                    <div class="folder">
                        <div class="folder-head" onclick="toggleBody(this)">
                            <input type="checkbox" ${folderChecked} onclick="checkAll('${id}', '${f.id}', this); event.stopPropagation();">
                            <i class="fas fa-folder" style="margin-right:8px; color:#ffd700"></i> ${f.name} (${totalSv})
                        </div>
                        <div class="folder-body">${sHtml}</div>
                    </div>`;
                });
            }

            return `
                <div class="header">
                    <div style="font-weight:bold; font-size:1.1em; color:#fff">${grp.name} <span id="st-${id}" class="badge"></span></div>
                    <button class="btn-sm" style="background:#ff3333" onclick="del('${id}')"><i class="fas fa-trash"></i></button>
                </div>
                <div class="grid">
                    <div><div style="color:#00ff41; margin-bottom:5px; font-weight:bold">BOTS</div><div class="box" id="b-${id}">${bHtml}</div></div>
                    <div><div style="color:#00ff41; margin-bottom:5px; font-weight:bold">FOLDERS</div><div class="box" id="s-${id}">${fHtml}</div></div>
                </div>
                <textarea id="m-${id}" placeholder="Nội dung spam...">${grp.message || ''}</textarea>
                <div class="actions">
                    <button class="btn-sm" onclick="save('${id}')">LƯU CONFIG</button>
                    <span id="act-${id}"></span>
                </div>
            `;
        }

        function updateStatus(id, grp) {
            const p = document.getElementById(`panel-${id}`);
            if(grp.active) p.classList.add('active'); else p.classList.remove('active');
            
            const badge = document.getElementById(`st-${id}`);
            badge.innerText = grp.active ? 'RUNNING' : 'IDLE';
            badge.style.background = grp.active ? '#00ff41' : '#333';
            badge.style.color = grp.active ? '#000' : '#fff';

            const act = document.getElementById(`act-${id}`);
            act.innerHTML = grp.active 
                ? `<button class="btn-sm btn-stop" onclick="toggle('${id}')">DỪNG</button>` 
                : `<button class="btn-sm btn-start" onclick="toggle('${id}')">BẮT ĐẦU</button>`;
        }

        function toggleBody(el) { el.nextElementSibling.classList.toggle('open'); }
        function checkAll(pid, fid, cb) {
            document.querySelectorAll(`.sc-${pid}-${fid}`).forEach(c => c.checked = cb.checked);
        }

        // API CALLS
        function api(ep, body) { return fetch(`/api/${ep}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}).then(r=>r.json()); }
        function loop() { fetch('/api/groups').then(r=>r.json()).then(render); }
        
        function create() { const n=document.getElementById('gName').value; if(n) api('create', {name:n}).then(()=>{document.getElementById('gName').value=''; loop()}); }
        function save(id) {
            const msg = document.getElementById(`m-${id}`).value;
            const bots = Array.from(document.getElementById(`b-${id}`).querySelectorAll('input:checked')).map(c=>parseInt(c.value));
            // Gom tất cả server đã check từ tất cả folder
            const svrs = [];
            document.getElementById(`s-${id}`).querySelectorAll('input[type="checkbox"]:not([onclick])').forEach(c => {
                if(c.checked) svrs.push(c.value);
            });
            api('update', {id, message:msg, bots, servers:svrs}).then(d=>alert(d.msg));
        }
        function toggle(id) { api('toggle', {id}).then(()=>setTimeout(loop, 200)); }
        function del(id) { if(confirm('Xóa?')) api('delete', {id}).then(loop); }
        function refresh() {
            document.getElementById('rfBtn').disabled = true;
            document.getElementById('rfStatus').innerText = "Đang gửi lệnh quét...";
            fetch('/api/refresh').then(r=>r.json()).then(d=>{
                if(d.ok) { document.getElementById('rfStatus').innerText = "Đang tải lại trang..."; setTimeout(()=>location.reload(), 4000); }
                else { alert(d.msg); document.getElementById('rfBtn').disabled = false; }
            });
        }

        loop(); setInterval(loop, 3000);
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    bl = [{'index': k, 'name': v['name']} for k, v in bots_instances.items()]
    return render_template_string(HTML, bots_json=bl, folders_json=scanned_data['folders'], bot_count=len(bots_instances), server_count=len(scanned_data['servers']))

@app.route('/api/groups')
def g_groups(): return jsonify(spam_groups)
@app.route('/api/create', methods=['POST'])
def g_create(): gid=str(uuid.uuid4())[:6]; spam_groups[gid]={'name':request.json['name'],'active':False,'bots':[],'servers':[],'message':''}; save_settings(); return jsonify({})
@app.route('/api/update', methods=['POST'])
def g_update(): d=request.json; spam_groups[d['id']].update({'bots':d['bots'],'servers':d['servers'],'message':d['message']}); save_settings(); return jsonify({'msg':'Đã lưu!'})
@app.route('/api/toggle', methods=['POST'])
def g_toggle(): 
    gid=request.json['id']; cur=spam_groups[gid]['active']; spam_groups[gid]['active']=not cur; 
    if not cur: threading.Thread(target=run_spam_group_logic, args=(gid,), daemon=True).start()
    return jsonify({})
@app.route('/api/delete', methods=['POST'])
def g_del(): gid=request.json['id']; spam_groups[gid]['active']=False; del spam_groups[gid]; save_settings(); return jsonify({})
@app.route('/api/refresh')
def g_refresh():
    b1 = bots_instances.get(0)
    if b1: asyncio.run_coroutine_threadsafe(scan_discord_structure(b1['client']), b1['loop']); return jsonify({'ok':True})
    return jsonify({'ok':False, 'msg':'Bot 1 chưa online'})

if __name__ == '__main__':
    print("🔥 V9 STARTED - FIXED UI & SPAM", flush=True)
    load_settings()
    for i, t in enumerate(TOKENS):
        if t.strip(): threading.Thread(target=start_bot_node, args=(t, i), daemon=True).start(); time.sleep(1)
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))
