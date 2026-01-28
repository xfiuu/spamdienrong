from flask import Flask
from threading import Thread
import os

app = Flask('')

@app.route('/')
def home():
    return "Bot đang hoạt động!"

def run():
    port = int(os.environ.get("PORT", 8080))  # Lấy PORT từ Render hoặc dùng mặc định 8080
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()
