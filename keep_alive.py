from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "I am alive!"

def run():
    # Render assigns a port automatically via environment variable, or uses 8080 default
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()
