import json
import time
import threading
import sys
import io
import math
import tkinter as tk

try:
    import websocket
    import requests
    from PIL import Image, ImageTk, ImageDraw
except ImportError:
    print("pip install websocket-client requests Pillow")
    sys.exit(1)

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222
REFRESH_MS = 1500

GEOCODE_KEYS = [
    "pk.010bb988be9b2a316e7093ae8e316e6d",
    "pk.6ce0e2bf3b2b84e353d2420b38de8ed2",
    "pk.78f4624afaebfd926a65e31358a4507d",
]


class CDPConnector:
    def __init__(self, host=CDP_HOST, port=CDP_PORT):
        self.host = host
        self.port = port
        self.ws = None
        self._id = 0
        self._resp = {}
        self._lock = threading.Lock()
        self._running = False

    def find_tab(self):
        try:
            r = requests.get(f"http://{self.host}:{self.port}/json", timeout=5)
            tabs = r.json()
        except Exception:
            return None

        for t in tabs:
            if t.get("type") == "iframe" and "geoguessr.com" in t.get("url", "").lower():
                return t.get("webSocketDebuggerUrl")

        for t in tabs:
            url = t.get("url", "").lower()
            title = t.get("title", "").lower()
            if t.get("type") == "page" and ("geoguessr" in url or "geoguessr" in title):
                return t.get("webSocketDebuggerUrl")

        for t in tabs:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                return t["webSocketDebuggerUrl"]

        return None

    def connect(self):
        ws_url = self.find_tab()
        if not ws_url:
            return False
        try:
            self.ws = websocket.WebSocket()
            self.ws.settimeout(5)
            self.ws.connect(ws_url)
            self._running = True
            threading.Thread(target=self._listen, daemon=True).start()
            return True
        except Exception:
            return False

    def _listen(self):
        while self._running:
            try:
                raw = self.ws.recv()
                if raw:
                    msg = json.loads(raw)
                    mid = msg.get("id")
                    if mid is not None:
                        with self._lock:
                            self._resp[mid] = msg
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                if self._running:
                    time.sleep(0.3)

    def evaluate(self, expr):
        self._id += 1
        mid = self._id
        try:
            self.ws.send(json.dumps({
                "id": mid,
                "method": "Runtime.evaluate",
                "params": {"expression": expr, "returnByValue": True, "awaitPromise": False}
            }))
        except Exception:
            return None

        for _ in range(60):
            with self._lock:
                if mid in self._resp:
                    resp = self._resp.pop(mid)
                    r = resp.get("result", {}).get("result", {})
                    if r.get("type") == "object" and "value" in r:
                        return r["value"]
                    elif r.get("type") in ("number", "string", "boolean"):
                        return r.get("value")
                    return None
            time.sleep(0.1)
        return None

    def disconnect(self):
        self._running = False
        if self.ws:
            try: self.ws.close()
            except: pass


class CoordinateExtractor:
    INJECT = r"""
    (function() {
        if (window.__geo_hook) return 'ok';
        window.__geo_coords = null;
        window.__geo_hook = true;
        const _open = XMLHttpRequest.prototype.open;
        const _send = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(m, u) {
            this.__url = u; this.__method = m;
            return _open.apply(this, arguments);
        };
        XMLHttpRequest.prototype.send = function() {
            var x = this;
            if (x.__method && x.__method.toUpperCase() === 'POST' && x.__url && (
                x.__url.includes('GetMetadata') || x.__url.includes('SingleImageSearch')
            )) {
                x.addEventListener('load', function() {
                    try {
                        var m = x.responseText.match(/\[null,null,(-?\d+\.\d+),(-?\d+\.\d+)\]/);
                        if (m) window.__geo_coords = {lat:parseFloat(m[1]),lng:parseFloat(m[2]),t:Date.now()};
                    } catch(e) {}
                });
            }
            return _send.apply(this, arguments);
        };
        return 'ok';
    })()
    """

    READ = "(function(){return window.__geo_coords||null})()"

    FALLBACK = r"""
    (function() {
        try {
            var e = performance.getEntriesByType('resource');
            for (var i = e.length-1; i >= 0; i--) {
                var u = e[i].name;
                var m = u.match(/cbll=([\-\d.]+),([\-\d.]+)/);
                if (m) return {lat:parseFloat(m[1]),lng:parseFloat(m[2])};
                m = u.match(/!3d([\-\d.]+)!4d([\-\d.]+)/);
                if (m) return {lat:parseFloat(m[1]),lng:parseFloat(m[2])};
            }
        } catch(e) {}
        return null;
    })()
    """

    def __init__(self, cdp):
        self.cdp = cdp
        self.last = None
        self._ready = False

    def inject(self):
        r = self.cdp.evaluate(self.INJECT)
        self._ready = r == "ok"
        return self._ready

    def extract(self):
        if not self._ready:
            self.inject()

        r = self.cdp.evaluate(self.READ)
        if r and "lat" in r:
            self.last = (r["lat"], r["lng"])
            return self.last

        r = self.cdp.evaluate(self.FALLBACK)
        if r and "lat" in r:
            self.last = (r["lat"], r["lng"])
            return self.last

        return self.last


class Geocoder:
    _cache = {}
    _ki = 0

    @classmethod
    def lookup(cls, lat, lng):
        key = (round(lat, 3), round(lng, 3))
        if key in cls._cache:
            return cls._cache[key]
        try:
            r = requests.get("https://us1.locationiq.com/v1/reverse", params={
                "key": GEOCODE_KEYS[cls._ki % len(GEOCODE_KEYS)],
                "lat": lat, "lon": lng, "format": "json", "accept-language": "en",
            }, timeout=5)
            if r.status_code == 200:
                a = r.json().get("address", {})
                res = {
                    "country": a.get("country", ""),
                    "code": a.get("country_code", ""),
                    "state": a.get("state", "") or a.get("county", ""),
                    "city": a.get("city","") or a.get("town","") or a.get("village","") or a.get("suburb",""),
                }
                cls._cache[key] = res
                return res
            cls._ki += 1
        except Exception:
            cls._ki += 1
        return None


class Overlay:
    BG = "#0a0a0a"
    BG2 = "#111111"
    BG3 = "#1a1a1a"
    ACCENT = "#8b5cf6"
    TEXT = "#e0e0e0"
    DIM = "#666666"
    BORDER = "#222222"
    MAP_W, MAP_H = 280, 140

    def __init__(self, ext):
        self.ext = ext
        self.root = None
        self.running = False
        self._last = None
        self._photo = None
        self._zoom = 7
        self._lat = None
        self._lng = None

    def build(self):
        self.root = tk.Tk()
        self.root.title("GeoGuessr Tool")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.93)
        self.root.configure(bg=self.BG)
        self.root.geometry("310x390+20+20")
        self._drag = {"x": 0, "y": 0}

        hdr = tk.Frame(self.root, bg=self.BG2, height=34)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        ttl = tk.Label(hdr, text="⚡ GeoGuessr Cheat", bg=self.BG2, fg=self.ACCENT, font=("Consolas", 10, "bold"))
        ttl.pack(side="left", padx=10)
        self.dot = tk.Label(hdr, text="●", bg=self.BG2, fg="#22c55e", font=("Consolas", 9))
        self.dot.pack(side="right", padx=8)
        for w in (hdr, ttl):
            w.bind("<ButtonPress-1>", self._sd)
            w.bind("<B1-Motion>", self._od)

        body = tk.Frame(self.root, bg=self.BG, padx=10, pady=6)
        body.pack(fill="both", expand=True)
        ro = dict(bg=self.BG3, padx=8, pady=4, bd=0, highlightthickness=1,
                  highlightbackground=self.BORDER, highlightcolor=self.ACCENT)

        f1 = tk.Frame(body, **ro); f1.pack(fill="x", pady=2)
        tk.Label(f1, text="🌐", bg=self.BG3, font=("Segoe UI Emoji", 11)).pack(side="left", padx=(0,6))
        self.lb_country = tk.Label(f1, text="—", bg=self.BG3, fg=self.TEXT, font=("Consolas", 10), anchor="w")
        self.lb_country.pack(side="left", fill="x", expand=True)

        f2 = tk.Frame(body, **ro); f2.pack(fill="x", pady=2)
        tk.Label(f2, text="🗺️", bg=self.BG3, font=("Segoe UI Emoji", 11)).pack(side="left", padx=(0,6))
        self.lb_state = tk.Label(f2, text="—", bg=self.BG3, fg=self.TEXT, font=("Consolas", 10), anchor="w")
        self.lb_state.pack(side="left", fill="x", expand=True)

        f3 = tk.Frame(body, **ro); f3.pack(fill="x", pady=2)
        tk.Label(f3, text="🏙️", bg=self.BG3, font=("Segoe UI Emoji", 11)).pack(side="left", padx=(0,6))
        self.lb_city = tk.Label(f3, text="—", bg=self.BG3, fg=self.TEXT, font=("Consolas", 10), anchor="w")
        self.lb_city.pack(side="left", fill="x", expand=True)

        f4 = tk.Frame(body, **ro); f4.pack(fill="x", pady=2)
        tk.Label(f4, text="📍", bg=self.BG3, font=("Segoe UI Emoji", 11)).pack(side="left", padx=(0,6))
        self.lb_coords = tk.Label(f4, text="—, —", bg=self.BG3, fg=self.DIM, font=("Consolas", 9), anchor="w")
        self.lb_coords.pack(side="left", fill="x", expand=True)

        mf = tk.Frame(body, bg=self.BORDER, bd=1, relief="solid")
        mf.pack(fill="x", pady=(8,2))
        self.lb_map = tk.Label(mf, text="waiting...", bg=self.BG3, fg=self.DIM,
                               font=("Consolas", 8), width=self.MAP_W, height=self.MAP_H)
        self.lb_map.pack()

        zf = tk.Frame(body, bg=self.BG)
        zf.pack(fill="x", pady=(3,0))
        bs = dict(bg=self.BG3, fg=self.TEXT, font=("Consolas", 11, "bold"), bd=0,
                  highlightthickness=1, highlightbackground=self.BORDER,
                  activebackground=self.ACCENT, activeforeground="#fff", cursor="hand2", padx=12, pady=1)
        tk.Button(zf, text="−", command=self._zout, **bs).pack(side="left")
        tk.Button(zf, text="+", command=self._zin, **bs).pack(side="right")

        ft = tk.Frame(self.root, bg=self.BG2, height=22)
        ft.pack(fill="x", side="bottom")
        ft.pack_propagate(False)
        tk.Label(ft, text="drag to move", bg=self.BG2, fg="#333", font=("Consolas", 7)).pack(pady=2)

        self.running = True

    def _sd(self, e): self._drag["x"], self._drag["y"] = e.x, e.y
    def _od(self, e):
        self.root.geometry(f"+{self.root.winfo_x()+e.x-self._drag['x']}+{self.root.winfo_y()+e.y-self._drag['y']}")

    def _zin(self):
        if self._zoom < 17:
            self._zoom += 1
            if self._lat: self._load_map(self._lat, self._lng)

    def _zout(self):
        if self._zoom > 2:
            self._zoom -= 1
            if self._lat: self._load_map(self._lat, self._lng)

    def _load_map(self, lat, lng):
        self._lat, self._lng = lat, lng
        z = self._zoom
        def go():
            try:
                n = 2 ** z
                xe = (lng + 180) / 360 * n
                ye = (1 - math.log(math.tan(math.radians(lat)) + 1/math.cos(math.radians(lat))) / math.pi) / 2 * n
                tx, ty = int(xe), int(ye)
                px, py = int((xe-tx)*256), int((ye-ty)*256)

                canvas = Image.new("RGB", (768, 768), "#ddd")
                h = {"User-Agent": "GeoTool/1.0"}
                for dy in range(-1, 2):
                    for dx in range(-1, 2):
                        ttx = (tx+dx) % n
                        tty = ty+dy
                        if tty < 0 or tty >= n: continue
                        try:
                            r = requests.get(f"https://tile.openstreetmap.org/{z}/{ttx}/{tty}.png", timeout=4, headers=h)
                            if r.status_code == 200:
                                canvas.paste(Image.open(io.BytesIO(r.content)), ((dx+1)*256, (dy+1)*256))
                        except: pass

                cx, cy = 256+px, 256+py
                hw, hh = self.MAP_W//2, self.MAP_H//2
                crop = canvas.crop((cx-hw, cy-hh, cx+hw, cy+hh))

                d = ImageDraw.Draw(crop)
                d.polygon([(hw-7,hh-6),(hw+7,hh-6),(hw,hh+8)], fill="#dc2626", outline="#7f1d1d")
                d.ellipse([hw-7,hh-16,hw+7,hh-2], fill="#dc2626", outline="#7f1d1d", width=1)
                d.ellipse([hw-3,hh-12,hw+3,hh-6], fill="white")

                if self.running and self.root:
                    self.root.after(0, lambda: self._set_map(crop))
            except: pass
        threading.Thread(target=go, daemon=True).start()

    def _set_map(self, img):
        try:
            self._photo = ImageTk.PhotoImage(img)
            self.lb_map.config(image=self._photo, text="", width=self.MAP_W, height=self.MAP_H)
        except: pass

    def _flag(self, code):
        try: return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code.upper())
        except: return ""

    def _update(self, lat, lng, loc=None):
        self.lb_coords.config(text=f"{lat:.6f}, {lng:.6f}")
        if loc:
            c = loc.get("country", "")
            cc = loc.get("code", "").upper()
            self.lb_country.config(text=f"{self._flag(cc)} {c}" if c else "—")
            self.lb_state.config(text=loc.get("state", "") or "—")
            self.lb_city.config(text=loc.get("city", "") or "—")
        self.dot.config(fg="#22c55e")
        self._load_map(lat, lng)

    def _loop(self):
        if not self.running: return
        coords = self.ext.extract()
        if coords:
            lat, lng = coords
            if (lat, lng) != self._last:
                self._last = (lat, lng)
                self.lb_coords.config(text=f"{lat:.6f}, {lng:.6f}")
                self.dot.config(fg="#22c55e")
                def gc():
                    loc = Geocoder.lookup(lat, lng)
                    if loc and self.running:
                        self.root.after(0, lambda: self._update(lat, lng, loc))
                threading.Thread(target=gc, daemon=True).start()
        else:
            self.dot.config(fg="#f97316")
        self.root.after(REFRESH_MS, self._loop)

    def stop(self):
        self.running = False
        if self.root: self.root.destroy()

    def run(self):
        self.build()
        self._loop()
        self.root.mainloop()


def main():
    cdp = CDPConnector()
    while not cdp.connect():
        time.sleep(3)

    ext = CoordinateExtractor(cdp)
    ext.inject()

    overlay = Overlay(ext)
    try:
        overlay.run()
    except KeyboardInterrupt:
        pass
    finally:
        cdp.disconnect()


if __name__ == "__main__":
    main()
