import ctypes
import difflib
import json
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

import mss
import pyautogui
import pytesseract
from PIL import Image, ImageChops, ImageFilter, ImageOps

try:
    import keyboard  # global hotkeys
except Exception:
    keyboard = None

# Make Tk / mss / pyautogui all use real screen pixels on high-DPI Windows displays
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

_WIN_TESS = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.name == "nt" and os.path.exists(_WIN_TESS):
    pytesseract.pytesseract.tesseract_cmd = _WIN_TESS

pyautogui.FAILSAFE = True  # slam the mouse into a screen corner to abort a click
pyautogui.PAUSE = 0.05

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULTS = {
    "keywords": [],
    "region": None,             # (left, top, width, height) in screen pixels
    "leave_phrase": "Leave Match",
    "matchmake_phrase": "MATCHMAKE",
    "scan_interval": 1.0,       # seconds between keyword scans
    "misses_required": 3,       # consecutive no-match scans before leaving
    "min_text_chars": 8,        # fewer chars than this = loading/blank screen, not a miss
    "fuzzy_threshold": 0.85,    # keyword matching, 1.0 = exact only
    "button_fuzzy": 0.80,       # "Leave Match" / "MATCHMAKE" matching
    "text_brightness": 150,     # pixels at/above this (0-255) count as text, the rest is dropped
    "min_conf": 45,             # drop OCR words below this confidence (0-100)
    "region_scale": 2.0,        # upscale for the keyword region
    "screen_scale": 1.0,        # upscale for full-screen scans (higher = slower)
    "config_version": 3,
    "confirm_delay": 0.2,       # seconds to wait before looking for the 2nd "Leave Match"
    "confirm_timeout": 6.0,     # keep re-scanning the center for up to this long for the 2nd one
    "center_scale": 2.0,        # upscale for the center scan (the box is small, so this is cheap)
    "dark_text_threshold": 110, # center-scan fallback: dark text on a light button
    "matchmake_delay": 20.0,    # seconds to wait after leaving before MATCHMAKE
    "streamsnipe_delay": 5.0,   # seconds the Streamsnipe button waits before MATCHMAKE
    "resume_delay": 30.0,       # pause lobby watch this long after a full leave+matchmake
    "cooldown": 5.0,            # pause after a leave attempt that failed
    "center_fraction": 0.7,     # size of the "center of the screen" box (fraction of screen)
    "find_attempts": 5,         # how many times to re-scan for a button before giving up
    "monitor": 1,               # mss monitor index (1 = primary)
    "alpha": 0.92,
}

# (label, config key, type, min, max)
FIELDS = [
    ("Scan every (s)", "scan_interval", float, 0.2, 60),
    ("Misses to leave", "misses_required", int, 1, 50),
    ("Min text chars", "min_text_chars", int, 0, 500),
    ("Keyword fuzz 0.5-1", "fuzzy_threshold", float, 0.5, 1.0),
    ("Text brightness", "text_brightness", int, 30, 250),
    ("2nd click wait (s)", "confirm_delay", float, 0.05, 30),
    ("Center box 0.2-1", "center_fraction", float, 0.2, 1.0),
    ("Center search max (s)", "confirm_timeout", float, 1, 60),
    ("Matchmake after (s)", "matchmake_delay", float, 0, 600),
    ("Resume watch (s)", "resume_delay", float, 0, 600),
]


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("config_version", 0) < DEFAULTS["config_version"]:
            # older saved values for the center/confirm step would override the new defaults
            for k in ("confirm_delay", "center_fraction"):
                saved.pop(k, None)
        cfg.update(saved)
        cfg["config_version"] = DEFAULTS["config_version"]
    except FileNotFoundError:
        pass
    except Exception as e:
        print("Could not read config.json:", e)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# OCR helpers
PAD = 10


def text_mask(img, thr, scale=1.0, dark_text=False):
    """Keep ONLY text pixels. Returns black text on a white page.

    Normal mode (light text on dark): brightness is the max of R/G/B so coloured light text
    (orange, red, etc.) still counts. Everything below `thr` - the dark background - becomes
    plain white and is never read.
    dark_text=True is the reverse (dark text on a light button): pixels at/below `thr` are text.
    """
    r, g, b = img.convert("RGB").split()
    v = ImageChops.lighter(ImageChops.lighter(r, g), b)
    if dark_text:
        # Only dark pixels that sit INSIDE a light area (a light button) count as text; the dark
        # game background around it is not text. Closing fills the letters' dark strokes so the
        # whole button counts as one light area.
        light = v.point(lambda p: 255 if p >= 150 else 0)
        light = light.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.MinFilter(9))
        dark = v.point(lambda p: 255 if p <= thr else 0)
        mask = ImageChops.invert(ImageChops.multiply(light, dark))  # black text on white
        if scale != 1.0:
            mask = mask.resize((max(1, int(mask.width * scale)), max(1, int(mask.height * scale))),
                               Image.LANCZOS)
        return mask.filter(ImageFilter.MedianFilter(3))
    if scale != 1.0:
        v = v.resize((max(1, int(v.width * scale)), max(1, int(v.height * scale))), Image.LANCZOS)
    mask = v.point(lambda p: 0 if p >= thr else 255)
    return mask.filter(ImageFilter.MedianFilter(3))  # wipe single-pixel specks


def ocr_words(img, thr, scale, min_conf, dark_text=False):
    """OCR the text mask. Returns confident words with boxes in the image's own pixel coords."""
    mask = ImageOps.expand(text_mask(img, thr, scale, dark_text), border=PAD, fill=255)
    data = pytesseract.image_to_data(mask, config="--psm 11", output_type=pytesseract.Output.DICT)
    words = []
    for i, t in enumerate(data["text"]):
        t = t.strip()
        if not t or not re.search(r"[A-Za-z0-9]", t):
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if conf < min_conf:
            continue
        x = (data["left"][i] - PAD) / scale
        y = (data["top"][i] - PAD) / scale
        w = data["width"][i] / scale
        h = data["height"][i] / scale
        words.append({"text": t, "conf": conf, "box": (x, y, w, h)})
    return words


def normalize(s):
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower())


def find_matches(text, keywords, threshold):
    """Keywords found in text (exact substring, or fuzzy word-window match)."""
    words = normalize(text).split()
    joined = " ".join(words)
    found = []
    for kw in keywords:
        kw_n = " ".join(normalize(kw).split())
        if not kw_n:
            continue
        if kw_n in joined:
            found.append(kw)
            continue
        if threshold >= 1.0:
            continue
        n = len(kw_n.split())
        for i in range(max(1, len(words) - n + 1)):
            chunk = " ".join(words[i:i + n])
            if difflib.SequenceMatcher(None, kw_n, chunk).ratio() >= threshold:
                found.append(kw)
                break
    return found


def _adjacent(a, b):
    """True if word b sits right after word a on the same row (so they form one phrase)."""
    ax, ay, aw, ah = a["box"]
    bx, by, bw, bh = b["box"]
    h = max(ah, bh, 1)
    same_row = abs((ay + ah / 2) - (by + bh / 2)) < 0.7 * h
    gap = bx - (ax + aw)
    return same_row and -0.5 * h <= gap <= 2.5 * h


def find_phrase(words, phrase, threshold):
    """Find a (possibly multi-word) phrase among OCR words. Returns centre point or None."""
    target = re.sub(r"[^a-z0-9]", "", phrase.lower())
    ntok = max(1, len(phrase.split()))
    if not target:
        return None
    best = None
    for i in range(len(words)):
        for size in sorted({1, ntok}):  # 1 also catches OCR gluing "LeaveMatch" together
            win = words[i:i + size]
            if len(win) < size:
                continue
            if size > 1 and not all(_adjacent(win[j], win[j + 1]) for j in range(size - 1)):
                continue
            cand = re.sub(r"[^a-z0-9]", "", "".join(w["text"] for w in win).lower())
            score = difflib.SequenceMatcher(None, target, cand).ratio()
            if score >= threshold and (best is None or score > best[0]):
                best = (score, win)
    if not best:
        return None
    score, win = best
    x0 = min(w["box"][0] for w in win)
    y0 = min(w["box"][1] for w in win)
    x1 = max(w["box"][0] + w["box"][2] for w in win)
    y1 = max(w["box"][1] + w["box"][3] for w in win)
    return {"cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2, "score": score,
            "text": " ".join(w["text"] for w in win)}


# UI 
class RegionPicker(tk.Toplevel):
    """Fullscreen translucent window: drag a rectangle to choose the scan region."""

    def __init__(self, master, on_done):
        super().__init__(master)
        self.on_done = on_done
        self.attributes("-fullscreen", True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.3)
        self.configure(bg="black", cursor="crosshair")
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            self.winfo_screenwidth() // 2, 40, fill="white", font=("Segoe UI", 18),
            text="Drag a box around the lobby text to scan  (Esc to cancel)")
        self.start = None
        self.rect = None
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.bind("<Escape>", lambda e: self.destroy())
        self.focus_force()

    def press(self, e):
        self.start = (e.x_root, e.y_root)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="red", width=3)

    def drag(self, e):
        if self.rect and self.start:
            x0 = self.start[0] - self.winfo_rootx()
            y0 = self.start[1] - self.winfo_rooty()
            self.canvas.coords(self.rect, x0, y0, e.x, e.y)

    def release(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = e.x_root, e.y_root
        left, top = min(x0, x1), min(y0, y1)
        w, h = abs(x1 - x0), abs(y1 - y0)
        self.destroy()
        if w > 10 and h > 10:
            self.on_done([left, top, w, h])


class App:
    def __init__(self):
        self.cfg = load_config()
        self.scanning = False       # lobby watch on/off
        self.snipe_active = False   # Streamsnipe running
        self.running = True
        self.misses = 0
        self.capture_excluded = False

        self.root = tk.Tk()
        self.root.title("Lobby Keyword Overlay")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", self.cfg["alpha"])
        self.root.geometry("+30+30")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.build_ui()
        self.refresh_keywords()
        self.exclude_from_capture()

        if keyboard:
            try:
                keyboard.add_hotkey("f8", lambda: self.root.after(0, self.toggle))
                keyboard.add_hotkey("f10", lambda: self.root.after(0, self.quit))
            except Exception as e:
                print("Hotkeys unavailable (try running as administrator):", e)

        threading.Thread(target=self.scan_loop, daemon=True).start()

    # more UI
    def build_ui(self):
        r = self.root
        pad = {"padx": 6, "pady": 3}
        self.status = tk.Label(r, text="STOPPED", fg="white", bg="#aa3333",
                               font=("Segoe UI", 11, "bold"), width=34)
        self.status.pack(fill="x")
        self.detail = tk.Label(r, text="Set a region and add keywords", anchor="w",
                               justify="left", wraplength=300, font=("Segoe UI", 9))
        self.detail.pack(fill="x", **pad)

        frm = tk.Frame(r)
        frm.pack(fill="x", **pad)
        self.entry = tk.Entry(frm)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.add_keyword())
        tk.Button(frm, text="Add", command=self.add_keyword).pack(side="left", padx=(4, 0))

        self.listbox = tk.Listbox(r, height=5, selectmode="extended")
        self.listbox.pack(fill="x", **pad)
        tk.Button(r, text="Remove selected", command=self.remove_keywords).pack(fill="x", **pad)

        row = tk.Frame(r)
        row.pack(fill="x", **pad)
        tk.Button(row, text="Set scan region", command=self.pick_region).pack(side="left", expand=True, fill="x")
        tk.Button(row, text="Test OCR", command=self.test_ocr).pack(side="left", expand=True, fill="x", padx=(4, 0))
        row2 = tk.Frame(r)
        row2.pack(fill="x", **pad)
        tk.Button(row2, text="Preview text mask", command=self.preview_mask).pack(side="left", expand=True, fill="x")
        tk.Button(row2, text="Test buttons (no click)", command=self.test_buttons).pack(side="left", expand=True, fill="x", padx=(4, 0))

        ph = tk.Frame(r)
        ph.pack(fill="x", **pad)
        tk.Label(ph, text="Leave button text", font=("Segoe UI", 8), width=16, anchor="w").grid(row=0, column=0)
        self.leave_var = tk.StringVar(value=self.cfg["leave_phrase"])
        e1 = tk.Entry(ph, textvariable=self.leave_var)
        e1.grid(row=0, column=1, sticky="ew")
        tk.Label(ph, text="Matchmake button text", font=("Segoe UI", 8), width=16, anchor="w").grid(row=1, column=0)
        self.mm_var = tk.StringVar(value=self.cfg["matchmake_phrase"])
        e2 = tk.Entry(ph, textvariable=self.mm_var)
        e2.grid(row=1, column=1, sticky="ew")
        ph.columnconfigure(1, weight=1)
        for e in (e1, e2):
            e.bind("<FocusOut>", lambda ev: self.save_settings())

        grid = tk.Frame(r)
        grid.pack(fill="x", **pad)
        self.vars = {}
        for i, (label, key, _typ, _lo, _hi) in enumerate(FIELDS):
            tk.Label(grid, text=label, font=("Segoe UI", 8), anchor="w").grid(
                row=i // 2, column=(i % 2) * 2, sticky="w")
            v = tk.StringVar(value=str(self.cfg[key]))
            e = tk.Entry(grid, textvariable=v, width=6)
            e.grid(row=i // 2, column=(i % 2) * 2 + 1, padx=(2, 10))
            e.bind("<FocusOut>", lambda ev: self.save_settings())
            self.vars[key] = v

        self.btn = tk.Button(r, text="Start lobby watch (F8)", command=self.toggle, bg="#2e7d32", fg="white",
                             font=("Segoe UI", 10, "bold"))
        self.btn.pack(fill="x", **pad)
        self.snipe_btn = tk.Button(r, text="Start Streamsnipe", command=self.toggle_streamsnipe,
                                   bg="#6a1b9a", fg="white", font=("Segoe UI", 10, "bold"))
        self.snipe_btn.pack(fill="x", **pad)
        tk.Label(r, text="F8 start/stop watch · F10 quit", font=("Segoe UI", 8),
                 fg="#666").pack(pady=(0, 4))

    def set_status(self, text, color):
        self.status.config(text=text, bg=color)

    def ui(self, status, color, detail):
        try:
            self.root.after(0, lambda: (self.set_status(status, color), self.detail.config(text=detail)))
        except Exception:
            pass

    def ui_wait(self, fn):
        """Run fn on the Tk thread and wait for it."""
        if threading.current_thread() is threading.main_thread():
            fn()
            self.root.update()
            return
        done = threading.Event()

        def run():
            try:
                fn()
            finally:
                done.set()
        try:
            self.root.after(0, run)
            done.wait(2.0)
        except Exception:
            pass

    def exclude_from_capture(self):
        """Ask Windows to leave this panel out of screen captures so OCR never reads it."""
        self.capture_excluded = False
        if os.name != "nt":
            return
        try:
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            self.capture_excluded = bool(ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11))
        except Exception:
            self.capture_excluded = False

    def refresh_keywords(self):
        self.listbox.delete(0, "end")
        for k in self.cfg["keywords"]:
            self.listbox.insert("end", k)

    def add_keyword(self):
        k = self.entry.get().strip()
        if k and k.lower() not in [x.lower() for x in self.cfg["keywords"]]:
            self.cfg["keywords"].append(k)
            save_config(self.cfg)
            self.refresh_keywords()
        self.entry.delete(0, "end")

    def remove_keywords(self):
        for i in reversed(self.listbox.curselection()):
            del self.cfg["keywords"][i]
        save_config(self.cfg)
        self.refresh_keywords()

    def save_settings(self):
        for _label, key, typ, lo, hi in FIELDS:
            try:
                self.cfg[key] = min(hi, max(lo, typ(float(self.vars[key].get()))))
            except ValueError:
                pass
            self.vars[key].set(str(self.cfg[key]))
        self.cfg["leave_phrase"] = self.leave_var.get().strip() or DEFAULTS["leave_phrase"]
        self.cfg["matchmake_phrase"] = self.mm_var.get().strip() or DEFAULTS["matchmake_phrase"]
        save_config(self.cfg)

    def pick_region(self):
        self.root.withdraw()
        self.root.after(200, lambda: RegionPicker(self.root, self.region_chosen))
        self.root.after(400, self._wait_picker)

    def _wait_picker(self):
        if any(isinstance(w, RegionPicker) for w in self.root.winfo_children()):
            self.root.after(200, self._wait_picker)
        else:
            self.root.deiconify()

    def region_chosen(self, region):
        self.cfg["region"] = region
        save_config(self.cfg)
        self.detail.config(text=f"Region set: {region}")

    # screen + OCR
    def grab(self, region=None):
        """Capture the screen (or a region). Returns (image, left, top) in screen pixels."""
        hide = not self.capture_excluded
        if hide:  # fallback when Windows can't exclude our panel: make it invisible for the grab
            self.ui_wait(lambda: self.root.attributes("-alpha", 0.0))
            time.sleep(0.12)
        try:
            with mss.mss() as sct:
                if region is None:
                    idx = int(self.cfg["monitor"])
                    if idx < 1 or idx >= len(sct.monitors):
                        idx = 1
                    mon = sct.monitors[idx]
                else:
                    left, top, w, h = region
                    mon = {"left": int(left), "top": int(top), "width": int(w), "height": int(h)}
                shot = sct.grab(mon)
        finally:
            if hide:
                self.ui_wait(lambda: self.root.attributes("-alpha", self.cfg["alpha"]))
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return img, mon["left"], mon["top"]

    def thresholds(self):
        t = int(self.cfg["text_brightness"])
        return [t, max(60, t - 40)]  # 2nd pass catches dimmer text if the 1st finds nothing

    def scan_keywords(self, img):
        """OCR a region (text only) and look for keywords. Returns (matches, text_read)."""
        texts = []
        for thr in self.thresholds():
            words = ocr_words(img, thr, self.cfg["region_scale"], self.cfg["min_conf"])
            text = " ".join(w["text"] for w in words)
            texts.append(text)
            matches = find_matches(text, self.cfg["keywords"], self.cfg["fuzzy_threshold"])
            if matches:
                return matches, text
        return [], max(texts, key=len)

    def locate(self, phrases, region=None, scale=None, dark_fallback=False):
        """Scan the whole screen (or a region) for phrases. Returns {phrase: (x, y, score)}.

        Reads bright text only. With dark_fallback it also tries dark text on a light button
        as a last resort (used for the center confirm button).
        """
        img, ox, oy = self.grab(region)
        scale = scale or self.cfg["screen_scale"]
        passes = [(t, False) for t in self.thresholds()]
        if dark_fallback:
            passes.append((int(self.cfg["dark_text_threshold"]), True))
        found = {}
        for thr, dark in passes:
            words = ocr_words(img, thr, scale, self.cfg["min_conf"], dark)
            for p in phrases:
                if p in found:
                    continue
                hit = find_phrase(words, p, self.cfg["button_fuzzy"])
                if hit:
                    found[p] = (ox + hit["cx"], oy + hit["cy"], hit["score"])
            if len(found) == len(phrases):
                break
        return found

    def find_until(self, phrase, region, active, timeout, scale=None, dark_fallback=False):
        """Keep re-scanning (no clicking) until phrase shows up or timeout. Returns (x, y, score)."""
        deadline = time.time() + timeout
        while True:
            if not active():
                return None
            found = self.locate([phrase], region, scale, dark_fallback)
            if phrase in found:
                return found[phrase]
            if time.time() >= deadline:
                return None
            if not self.wait(0.2, active):
                return None

    def save_debug(self, region, name):
        """Save what was scanned (raw + text mask) so a failed search can be inspected."""
        try:
            folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")
            os.makedirs(folder, exist_ok=True)
            img, _, _ = self.grab(region)
            img.save(os.path.join(folder, f"{name}_raw.png"))
            thr = int(self.cfg["text_brightness"])
            text_mask(img, thr, 1.0).save(os.path.join(folder, f"{name}_mask_light.png"))
            text_mask(img, int(self.cfg["dark_text_threshold"]), 1.0, True).save(
                os.path.join(folder, f"{name}_mask_dark.png"))
            return folder
        except Exception:
            return None

    def center_region(self):
        with mss.mss() as sct:
            idx = int(self.cfg["monitor"])
            if idx < 1 or idx >= len(sct.monitors):
                idx = 1
            m = sct.monitors[idx]
        f = min(1.0, max(0.1, float(self.cfg["center_fraction"])))
        w, h = int(m["width"] * f), int(m["height"] * f)
        return [m["left"] + (m["width"] - w) // 2, m["top"] + (m["height"] - h) // 2, w, h]

    def find_with_retries(self, phrase, region, active):
        """Re-scan up to find_attempts times. Never clicks - just returns the position."""
        for _ in range(max(1, int(self.cfg["find_attempts"]))):
            if not active():
                return None
            found = self.locate([phrase], region)
            if phrase in found:
                return found[phrase]
            if not self.wait(0.75, active):
                return None
        return None

    def click(self, x, y):
        pyautogui.moveTo(int(x), int(y), duration=0.15)
        time.sleep(0.05)
        pyautogui.click()

    def wait(self, seconds, active=None):
        """Sleep that wakes early (returns False) if the thing it belongs to was stopped."""
        active = active or self.watch_active
        end = time.time() + seconds
        while time.time() < end:
            if not active():
                return False
            time.sleep(min(0.1, max(0.0, end - time.time())))
        return active()

    def watch_active(self):
        return self.scanning and self.running

    def snipe_is_active(self):
        return self.snipe_active and self.running

    # test buttons
    def test_ocr(self):
        self.save_settings()
        if not self.cfg["region"]:
            messagebox.showinfo("Test OCR", "Set a scan region first.")
            return
        try:
            img, _, _ = self.grab(self.cfg["region"])
            matches, text = self.scan_keywords(img)
        except Exception as e:
            messagebox.showerror("OCR error", f"{e}\n\nIs Tesseract installed?")
            return
        messagebox.showinfo(
            "Test OCR",
            f"Keyword matches: {matches or 'none'}\n\nText read (background ignored):\n{text.strip() or '(nothing)'}")

    def preview_mask(self):
        self.save_settings()
        if not self.cfg["region"]:
            messagebox.showinfo("Preview", "Set a scan region first.")
            return
        try:
            from PIL import ImageTk
            img, _, _ = self.grab(self.cfg["region"])
            mask = text_mask(img, int(self.cfg["text_brightness"]), 1.0)
            mask.thumbnail((900, 600))
            top = tk.Toplevel(self.root)
            top.title("What OCR sees (black = text, white = ignored)")
            top.attributes("-topmost", True)
            photo = ImageTk.PhotoImage(mask)
            lbl = tk.Label(top, image=photo)
            lbl.image = photo
            lbl.pack()
        except Exception as e:
            messagebox.showerror("Preview error", str(e))

    def test_buttons(self):
        self.save_settings()
        leave, mm = self.cfg["leave_phrase"], self.cfg["matchmake_phrase"]
        self.set_status("TESTING", "#1565c0")
        self.detail.config(text="Scanning the whole screen (no clicks)...")

        def work():
            try:
                found = self.locate([leave, mm], None)
                lines = []
                for p in (leave, mm):
                    if p in found:
                        x, y, s = found[p]
                        lines.append(f'"{p}" (full screen): found at ({int(x)}, {int(y)})  match {s:.2f}')
                    else:
                        lines.append(f'"{p}" (full screen): NOT found')
                center = self.locate([leave], self.center_region(), self.cfg["center_scale"], True)
                if leave in center:
                    x, y, s = center[leave]
                    lines.append(f'"{leave}" (center box): found at ({int(x)}, {int(y)})  match {s:.2f}')
                else:
                    lines.append(f'"{leave}" (center box): NOT found')
                msg = "\n".join(lines) + "\n\nNo clicks were made."
            except Exception as e:
                msg = f"Error: {e}\n\nIs Tesseract installed?"

            def show():
                self.set_status("STOPPED", "#aa3333")
                messagebox.showinfo("Test buttons", msg)
            self.root.after(0, show)

        threading.Thread(target=work, daemon=True).start()

    # lobby watch / scan for streamers
    def toggle(self):
        if not self.scanning:
            if not self.cfg["region"]:
                messagebox.showinfo("Start", "Set a scan region first.")
                return
            if not self.cfg["keywords"]:
                messagebox.showinfo("Start", "Add at least one keyword first.")
                return
            self.save_settings()
            self.misses = 0
            self.scanning = True
            self.btn.config(text="Stop lobby watch (F8)", bg="#aa3333")
            self.set_status("SCANNING", "#1565c0")
        else:
            self.scanning = False
            self.btn.config(text="Start lobby watch (F8)", bg="#2e7d32")
            self.set_status("STOPPED", "#aa3333")

    def stop_watch_ui(self, text="STOPPED"):
        self.scanning = False
        self.btn.config(text="Start lobby watch (F8)", bg="#2e7d32")
        self.set_status(text, "#aa3333")

    def scan_loop(self):
        while self.running:
            if self.scanning:
                try:
                    img, _, _ = self.grab(self.cfg["region"])
                    matches, text = self.scan_keywords(img)
                    enough_text = len(text.strip()) >= self.cfg["min_text_chars"]
                    saw = (text.strip()[:90] + "...") if len(text.strip()) > 90 else text.strip()
                    if matches:
                        self.misses = 0
                        self.ui(f"MATCH: {', '.join(matches)}", "#2e7d32", "Staying in lobby.")
                    elif not enough_text:
                        self.misses = 0
                        self.ui("WAITING (no text)", "#8d6e00", "Not enough text in the region - not leaving.")
                    else:
                        self.misses += 1
                        need = self.cfg["misses_required"]
                        self.ui(f"NO MATCH {self.misses}/{need}", "#e65100", f"Saw: {saw}")
                        if self.misses >= need:
                            ok = self.leave_sequence()
                            self.misses = 0
                            delay = self.cfg["resume_delay"] if ok else self.cfg["cooldown"]
                            if self.scanning:
                                self.ui("RESUMING SOON", "#1565c0", f"Lobby watch resumes in {delay:g}s.")
                                self.wait(delay)
                                if self.scanning:
                                    self.ui("SCANNING", "#1565c0", "Watching the lobby again.")
                except pyautogui.FailSafeException:
                    self.root.after(0, lambda: self.stop_watch_ui("STOPPED (failsafe)"))
                except Exception as e:
                    self.ui("ERROR", "#b71c1c", str(e))
                    time.sleep(2)
            time.sleep(self.cfg["scan_interval"])

    def leave_sequence(self):
        """No keywords matched: Leave Match (full screen) -> Leave Match (center) -> wait -> MATCHMAKE."""
        c = self.cfg
        leave, mm = c["leave_phrase"], c["matchmake_phrase"]
        active = self.watch_active
        red = "#b71c1c"

        self.ui("LEAVING: SCANNING SCREEN", red, f'Looking for "{leave}" on the entire screen...')
        hit = self.find_with_retries(leave, None, active)
        if not hit:
            if active():
                self.ui("LEAVE BUTTON NOT FOUND", red, f'Could not find "{leave}" anywhere. Nothing was clicked.')
            return False
        self.click(hit[0], hit[1])

        if not self.wait(c["confirm_delay"], active):
            return False
        self.ui("LEAVING: CONFIRM", red, f'Looking for "{leave}" in the center of the screen...')
        region = self.center_region()
        hit = self.find_until(leave, region, active, c["confirm_timeout"],
                              scale=c["center_scale"], dark_fallback=True)
        if not hit:
            if active():
                folder = self.save_debug(region, "center_not_found")
                where = f" Saved what it saw to {folder}." if folder else ""
                self.ui("CONFIRM NOT FOUND", red,
                        f'Could not find "{leave}" in the center. Nothing more was clicked.{where}')
            return False
        self.click(hit[0], hit[1])

        end = time.time() + c["matchmake_delay"]
        while True:
            remaining = end - time.time()
            if remaining <= 0:
                break
            self.ui(f"MATCHMAKE IN {int(remaining) + 1}s", "#6a1b9a", "Waiting before queuing again...")
            if not self.wait(min(1.0, remaining), active):
                return False

        self.ui("MATCHMAKE: SCANNING", "#6a1b9a", f'Looking for "{mm}" on the entire screen...')
        hit = self.find_with_retries(mm, None, active)
        if not hit:
            if active():
                self.ui("MATCHMAKE NOT FOUND", red, f'Could not find "{mm}" anywhere. Nothing was clicked.')
            return False
        self.click(hit[0], hit[1])  # exactly one click
        self.ui("MATCHMAKE CLICKED", "#2e7d32", "Queued again.")
        return True

    # Streamsnipe / Queue Start
    def toggle_streamsnipe(self):
        if self.snipe_active:
            self.snipe_active = False  # worker notices and stops
            return
        self.save_settings()
        self.snipe_active = True
        self.snipe_btn.config(text="Cancel Streamsnipe", bg="#aa3333")
        threading.Thread(target=self.streamsnipe, daemon=True).start()

    def finish_snipe_ui(self):
        self.snipe_active = False
        self.snipe_btn.config(text="Start Streamsnipe", bg="#6a1b9a")

    def streamsnipe(self):
        """Wait 5 seconds, scan the whole screen for MATCHMAKE, click it exactly once."""
        c = self.cfg
        mm = c["matchmake_phrase"]
        active = self.snipe_is_active
        purple = "#6a1b9a"
        try:
            end = time.time() + c["streamsnipe_delay"]
            while True:
                remaining = end - time.time()
                if remaining <= 0:
                    break
                self.ui(f"STREAMSNIPE IN {int(remaining) + 1}s", purple, "Get ready...")
                if not self.wait(min(1.0, remaining), active):
                    self.ui("STREAMSNIPE CANCELLED", "#aa3333", "Nothing was clicked.")
                    return
            self.ui("STREAMSNIPE: SCANNING", purple, f'Looking for "{mm}" on the entire screen...')
            hit = self.find_with_retries(mm, None, active)
            if hit:
                self.click(hit[0], hit[1])  # exactly one click
                self.ui("STREAMSNIPE: CLICKED", "#2e7d32", f'Clicked "{mm}" once.')
            elif active():
                self.ui("MATCHMAKE NOT FOUND", "#b71c1c", f'Could not find "{mm}" anywhere. Nothing was clicked.')
            else:
                self.ui("STREAMSNIPE CANCELLED", "#aa3333", "Nothing was clicked.")
        except pyautogui.FailSafeException:
            self.ui("STOPPED (failsafe)", "#aa3333", "Mouse hit a screen corner.")
        except Exception as e:
            self.ui("ERROR", "#b71c1c", str(e))
        finally:
            try:
                self.root.after(0, self.finish_snipe_ui)
            except Exception:
                pass

    # quit program behaviour
    def quit(self):
        self.running = False
        self.scanning = False
        self.snipe_active = False
        self.save_settings()
        self.root.destroy()
        sys.exit(0)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
