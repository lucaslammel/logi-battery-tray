"""
Logi Battery Tray
-----------------
Shows one Windows system-tray icon per Logitech wireless peripheral (keyboard,
mouse, headset), with the device drawing and its battery percentage on top of it.

* Keyboards, mice and headsets that talk HID++ 2.0 through their USB dongle are
  read directly with hidapi (no G HUB needed).
* Optional fallback for other headsets: put headsetcontrol.exe
  (https://github.com/Sapd/HeadsetControl) next to this script or in the PATH.

Run:          python logi_battery_tray.py
Diagnostics:  python logi_battery_tray.py --diagnose
Requires:     pip install -r requirements.txt
"""

import ctypes
import functools
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import hid
import pystray
from PIL import Image, ImageDraw, ImageFont

# ----------------------------------------------------------------- configuração
POLL_SECONDS = 5           # intervalo entre leituras
LOW_BATTERY = 20           # % para disparar o aviso
# Tempo (s) sem resposta antes de remover o ícone. Mouses dormem sozinhos depois de
# alguns segundos parados, então ganham uma tolerância bem maior.
GRACE_SECONDS = {"mouse": 600, "keyboard": 20, "headset": 20, "default": 20}
LOGITECH = 0x046D
HIDPP_PAGES = (0xFF00, 0xFF43)
SW_ID = 0x08
APP_NAME = "LogiBatteryTray"

# Tipos de dispositivo da feature 0x0005 (DeviceName/Type)
TYPE_NAMES = {0: "keyboard", 1: "remote", 2: "keyboard", 3: "mouse", 4: "mouse",
              5: "mouse", 6: "remote", 8: "headset"}

# Ajuste provisório do headset (feature 0x1F20), calibrado com 1 ponto:
# 3812 mV lidos <-> 64% reais no PRO X Wireless. Refinar com mais medições.
HEADSET_ADC_OFFSET_MV = 72

# Curva voltagem (mV) -> % para dispositivos que só têm a feature 0x1001
VOLTAGE_TABLE = [(4186, 100), (4067, 90), (3989, 80), (3922, 70), (3859, 60),
                 (3811, 50), (3778, 40), (3751, 30), (3717, 20), (3671, 10),
                 (3646, 5), (3579, 2), (3500, 0)]


# ------------------------------------------------------ language, config, single instance
STRINGS = {
    "en": {
        "refresh": "Refresh now", "autostart": "Start with Windows", "quit": "Quit",
        "language": "Language", "lang_auto": "System default",
        "charging": "charging", "no_response": "no response",
        "low_title": "Low battery", "low_body": "{name} is at {pct}% battery.",
    },
    "pt": {
        "refresh": "Atualizar agora", "autostart": "Iniciar com o Windows", "quit": "Sair",
        "language": "Idioma", "lang_auto": "Padrão do sistema",
        "charging": "carregando", "no_response": "sem resposta",
        "low_title": "Bateria baixa", "low_body": "{name} está com {pct}% de bateria.",
    },
    "es": {
        "refresh": "Actualizar ahora", "autostart": "Iniciar con Windows", "quit": "Salir",
        "language": "Idioma", "lang_auto": "Predeterminado del sistema",
        "charging": "cargando", "no_response": "sin respuesta",
        "low_title": "Batería baja", "low_body": "{name} tiene {pct}% de batería.",
    },
    "ja": {
        "refresh": "今すぐ更新", "autostart": "Windows 起動時に開始", "quit": "終了",
        "language": "言語", "lang_auto": "システムの既定",
        "charging": "充電中", "no_response": "応答なし",
        "low_title": "バッテリー残量低下", "low_body": "{name} のバッテリー残量は {pct}% です。",
    },
    "ru": {
        "refresh": "Обновить сейчас", "autostart": "Запускать вместе с Windows", "quit": "Выход",
        "language": "Язык", "lang_auto": "Как в системе",
        "charging": "заряжается", "no_response": "нет ответа",
        "low_title": "Низкий заряд батареи", "low_body": "Заряд {name}: {pct}%.",
    },
}
LANGUAGE_NAMES = {"en": "English", "pt": "Português", "es": "Español",
                  "ja": "日本語", "ru": "Русский"}
CONFIG_PATH = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                           APP_NAME, "config.json")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except OSError:
        pass


_config = load_config()


@functools.lru_cache(maxsize=1)
def system_language():
    """Windows display language ('pt-BR' -> 'pt'); English if there is no translation."""
    code = ""
    try:
        buf = ctypes.create_unicode_buffer(85)
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, 85):
            code = buf.value
    except (AttributeError, OSError):
        pass
    if not code:
        try:
            code = locale.getlocale()[0] or ""
        except (ValueError, TypeError):
            code = ""
    code = code.replace("_", "-").split("-")[0].lower()
    return code if code in STRINGS else "en"


def current_lang():
    setting = _config.get("language", "auto")
    return setting if setting in STRINGS else system_language()


def tr(key, **kwargs):
    text = STRINGS[current_lang()][key]
    return text.format(**kwargs) if kwargs else text


_mutex_handle = None   # kept alive for the whole process lifetime


def acquire_single_instance():
    """True if this is the only running instance (named Windows mutex)."""
    global _mutex_handle
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        _mutex_handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}_SingleInstance")
        return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        return True


# ----------------------------------------------------------------------- HID++
def _drain(dev):
    for _ in range(20):
        if not dev.read(20, 1):
            break


def hidpp_call(dev, idx, feat_idx, func, params=b"", timeout=0.3):
    msg = bytearray(20)
    msg[0], msg[1], msg[2], msg[3] = 0x11, idx, feat_idx, (func << 4) | SW_ID
    msg[4:4 + len(params)] = params
    _drain(dev)
    try:
        if dev.write(bytes(msg)) < 0:
            return None
    except (OSError, ValueError):
        return None
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = dev.read(20, 100)
        except OSError:
            return None
        if not r or len(r) < 5:
            continue
        if r[1] == idx and r[2] in (0xFF, 0x8F):      # erro HID++ 2.0 / 1.0
            return None
        if r[1] == idx and r[2] == feat_idx and r[3] == msg[3]:
            return bytes(r)
    return None


def feature_index(dev, idx, feature_id):
    r = hidpp_call(dev, idx, 0x00, 0, feature_id.to_bytes(2, "big"))
    return r[4] if r and r[4] != 0 else None


def read_name_and_type(dev, idx):
    f = feature_index(dev, idx, 0x0005)
    if f is None:
        return None, None
    r = hidpp_call(dev, idx, f, 0)
    if not r:
        return None, None
    total, name = r[4], b""
    while len(name) < total:
        r = hidpp_call(dev, idx, f, 1, bytes([len(name)]))
        if not r:
            break
        name += r[4:20]
    r = hidpp_call(dev, idx, f, 2)
    kind = TYPE_NAMES.get(r[4], "mouse") if r else "mouse"
    return name[:total].decode(errors="ignore").strip("\x00") or None, kind


def voltage_to_percent(mv):
    if mv >= VOLTAGE_TABLE[0][0]:
        return 100
    for (v1, p1), (v2, p2) in zip(VOLTAGE_TABLE, VOLTAGE_TABLE[1:]):
        if mv >= v2:
            return round(p2 + (p1 - p2) * (mv - v2) / (v1 - v2))
    return 0


def read_battery(dev, idx, cached_feature=None):
    """Retorna (feature_cache, (pct, carregando)) ou (None, None)."""
    candidates = [cached_feature] if cached_feature else [0x1004, 0x1000, 0x1001, 0x1F20]
    for fid in candidates:
        f = feature_index(dev, idx, fid)
        if f is None:
            continue
        if fid == 0x1004:                       # Unified Battery
            r = hidpp_call(dev, idx, f, 1)
            if r:
                return fid, (r[4], r[6] in (1, 2))
        elif fid == 0x1000:                     # Battery Level Status
            r = hidpp_call(dev, idx, f, 0)
            if r:
                return fid, (r[4], r[6] in (1, 2, 4))
        elif fid in (0x1001, 0x1F20):           # Voltagem / ADC (headsets)
            r = hidpp_call(dev, idx, f, 0)
            if r:
                mv = (r[4] << 8) | r[5]
                if not 3000 <= mv <= 4500:      # leitura sem sentido: aparelho desligado
                    continue
                if fid == 0x1F20:
                    mv += HEADSET_ADC_OFFSET_MV
                # No 0x1F20 o bit 0x01 NAO indica carregamento (visto no PRO X Wireless)
                charging = bool(r[6] & 0x80) if fid == 0x1001 else False
                return fid, (voltage_to_percent(mv), charging)
    return None, None


class LogitechHidpp:
    """Lê teclados/mouses pareados nos receptores Logitech."""

    def __init__(self):
        self.info = {}   # (path, idx) -> dict(name, kind, feature)

    def read_all(self):
        results = {}
        for item in hid.enumerate(LOGITECH, 0):
            if item["usage_page"] < 0xFF00:
                continue
            dev = hid.device()
            try:
                dev.open_path(item["path"])
            except OSError:
                continue
            try:
                seen = set()   # nomes já vistos nesta interface
                for idx in (*range(1, 7), 0xFF):
                    key = (item["path"], idx)
                    meta = self.info.get(key)
                    if meta is None:
                        name, kind = read_name_and_type(dev, idx)
                        if not name:
                            continue
                        meta = self.info[key] = {"name": name, "kind": kind, "feature": None,
                                                 "dup": name in seen}
                    if meta["dup"]:        # mesmo aparelho visto em outro índice: pula
                        continue
                    seen.add(meta["name"])
                    feat, data = read_battery(dev, idx, meta["feature"])
                    if data is None and meta["feature"]:   # uma tentativa extra antes de desistir
                        feat, data = read_battery(dev, idx, meta["feature"])
                    if data is None:
                        continue
                    meta["feature"] = feat
                    results[f"hidpp:{meta['name']}"] = {
                        "name": meta["name"], "kind": meta["kind"],
                        "pct": max(0, min(100, data[0])), "charging": data[1]}
            finally:
                dev.close()
        return results


# --------------------------------------------------------------------- headset
def find_headsetcontrol():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "headsetcontrol.exe"), shutil.which("headsetcontrol")):
        if cand and os.path.exists(cand):
            return cand
    return None


def read_headset():
    exe = find_headsetcontrol()
    if not exe:
        return {}
    try:
        out = subprocess.run([exe, "-b"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    m = re.search(r"(\d{1,3})\s*%", out)
    if not m:
        return {}
    name = re.search(r"Found (.+?)!", out)
    name = name.group(1).strip() if name else "Headset Logitech"
    return {"headset:" + name: {"name": name, "kind": "headset",
                                "pct": min(100, int(m.group(1))),
                                "charging": "CHARGING" in out.upper() and "UNAVAILABLE" not in out.upper()}}


# ------------------------------------------------------------------------ ícone
S = 4  # supersampling: desenha 4x maior e reduz, para bordas suaves
_fonts = {}


def _font(size):
    key = int(size)
    if key not in _fonts:
        font = None
        for name in ("segoeuib.ttf", "arialbd.ttf", "arial.ttf"):
            try:
                font = ImageFont.truetype(name, key)
                break
            except OSError:
                pass
        _fonts[key] = font or ImageFont.load_default()
    return _fonts[key]


def system_uses_light_theme():
    """True se a barra de tarefas do Windows está no tema claro."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "SystemUsesLightTheme")[0] == 1
    except (ImportError, OSError):
        return False


# --- primitivas (coordenadas numa grade 64x64) -------------------------------
def _line(d, pts, color, w):
    pts = [(x * S, y * S) for x, y in pts]
    d.line(pts, fill=color, width=int(w * S), joint="curve")
    r = w * S / 2
    for x, y in (pts[0], pts[-1]):          # pontas arredondadas
        d.ellipse((x - r, y - r, x + r, y + r), fill=color)


def _dot(d, x, y, r, color):
    d.ellipse(((x - r) * S, (y - r) * S, (x + r) * S, (y + r) * S), fill=color)


def _rrect(d, box, radius, color, width=None):
    b = [v * S for v in box]
    if width is None:
        d.rounded_rectangle(b, radius * S, fill=color)
    else:
        d.rounded_rectangle(b, radius * S, outline=color, width=int(width * S))


# --- layout: percentage OVERLAID on the device drawing (64x64 grid) ---------------
def make_dpi_aware():
    """Without this Windows reports 16 px for tray icons on every screen and scales
    our bitmap up (blurry). Must run before any window is created."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def tray_icon_size():
    """Size Windows really uses for tray icons (16 at 100% scale, 20/24/32 above)."""
    try:
        n = ctypes.windll.user32.GetSystemMetrics(49)       # SM_CXSMICON
        return n if n >= 16 else 16
    except (AttributeError, OSError):
        return 32


def draw_glyph(d, kind, fg):
    """Fluent-style outline icons that fill the whole icon."""
    if kind == "keyboard":
        _rrect(d, (1, 8, 63, 56), 9, fg, 4)
        for x in range(11, 54, 8):                       # top row of keys
            _dot(d, x, 17, 2, fg)
        for x in range(15, 50, 8):                       # middle row of keys
            _dot(d, x, 24, 2, fg)
        _line(d, [(19, 49.5), (45, 49.5)], fg, 3.5)      # space bar
    elif kind == "headset":
        d.arc((9 * S, 2 * S, 55 * S, 56 * S), 180, 360, fill=fg, width=int(4.5 * S))
        _rrect(d, (2, 26, 15, 56), 6, fg)                # ear cups
        _rrect(d, (49, 26, 62, 56), 6, fg)
    else:  # mouse
        _rrect(d, (13, 2, 51, 62), 19, fg, 4)
        _line(d, [(32, 9), (32, 17)], fg, 4)             # wheel


def make_icon(kind, pct, charging, light=False, size=None):
    size = size or tray_icon_size()
    fg = (32, 32, 32, 255) if light else (255, 255, 255, 255)
    halo = (255, 255, 255, 255) if light else (32, 32, 32, 255)
    img = Image.new("RGBA", (64 * S, 64 * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # the drawing is semi-transparent so the percentage dominates
    draw_glyph(d, kind, fg[:3] + (150,))

    # percentage centred over the drawing, with a dark outline to stay readable
    text = "?" if pct is None else str(pct)
    font_size = {1: 56, 2: 46}.get(len(text), 34)
    sw = int(2.5 * S)
    font = _font(font_size * S)
    box = d.textbbox((0, 0), text, font=font, stroke_width=sw)
    while box[2] - box[0] > 62 * S and font_size > 12:   # never wider than the icon
        font_size -= 2
        font = _font(font_size * S)
        box = d.textbbox((0, 0), text, font=font, stroke_width=sw)
    x = 32 * S - (box[0] + box[2]) / 2
    y = 32 * S - (box[1] + box[3]) / 2
    low = pct is not None and pct <= LOW_BATTERY
    num_color = (255, 64, 64, 255) if low else (255, 255, 255, 255)   # white; red if <= 20%
    d.text((x, y), text, font=font, stroke_width=sw, stroke_fill=(20, 20, 20, 255), fill=num_color)

    if charging:  # yellow bolt in the top-right corner
        bolt = [(57 * S, 0), (49 * S, 12 * S), (54 * S, 12 * S),
                (51 * S, 23 * S), (63 * S, 8 * S), (57 * S, 8 * S)]
        d.line(bolt + [bolt[0]], fill=halo, width=3 * S, joint="curve")
        d.polygon(bolt, fill=(250, 200, 20, 255))
    return img.resize((size, size), Image.LANCZOS)



# ------------------------------------------------------------ iniciar com Windows
def _run_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else py}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except (ImportError, OSError):
        return False


def set_autostart(enable):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion\Run",
                        0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _run_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# -------------------------------------------------------------------- aplicativo
class App:
    def __init__(self):
        self.hidpp = LogitechHidpp()
        self.icons = {}                 # key -> dict(icon, kind, name, warned, last, last_seen, stale)
        self.refresh = threading.Event()
        self.quitting = False

    # menu shared by all icons
    def build_menu(self, title):
        lang_items = [pystray.MenuItem(tr("lang_auto"), self._lang_action("auto"),
                                       checked=self._lang_checked("auto"), radio=True)]
        for code, label in LANGUAGE_NAMES.items():
            lang_items.append(pystray.MenuItem(label, self._lang_action(code),
                                               checked=self._lang_checked(code), radio=True))
        return pystray.Menu(
            pystray.MenuItem(title, None, enabled=False),
            pystray.MenuItem(tr("refresh"), lambda i, it: self.refresh.set()),
            pystray.MenuItem(tr("language"), pystray.Menu(*lang_items)),
            pystray.MenuItem(tr("autostart"), self.toggle_autostart,
                             checked=lambda it: autostart_enabled()),
            pystray.MenuItem(tr("quit"), self.quit))

    def _lang_action(self, code):
        def action(icon, item):
            self.set_language(code)
        return action

    def _lang_checked(self, code):
        def checked(item):
            current = _config.get("language", "auto")
            if current not in STRINGS:
                current = "auto"
            return current == code
        return checked

    def set_language(self, code):
        if code == "auto":
            _config.pop("language", None)
        else:
            _config["language"] = code
        save_config(_config)
        for entry in list(self.icons.values()):      # re-translate tooltips and menus now
            if entry.get("last"):
                pct, charging = entry["last"][:2]
                title = self.tooltip(entry["name"], pct, charging, entry.get("stale", False))
                entry["icon"].title = title
                entry["icon"].menu = self.build_menu(title)

    def toggle_autostart(self, icon, item):
        try:
            set_autostart(not autostart_enabled())
        except OSError:
            pass

    def quit(self, *_):
        self.quitting = True
        self.refresh.set()

    def tooltip(self, name, pct, charging, stale=False):
        s = f"{name}: {pct}%"
        if stale:
            return f"{s} ({tr('no_response')})"
        return f"{s} ({tr('charging')})" if charging else s

    def apply(self, found):
        light = system_uses_light_theme()
        size = tray_icon_size()
        # atualiza / cria
        for key, dev in found.items():
            pct, charging = dev["pct"], dev["charging"]
            title = self.tooltip(dev["name"], pct, charging)
            entry = self.icons.get(key)
            if entry is None:
                icon = pystray.Icon(f"logibat_{abs(hash(key))}",
                                    make_icon(dev["kind"], pct, charging, light, size), title,
                                    self.build_menu(title))
                icon.run_detached()
                entry = self.icons[key] = {"icon": icon, "kind": dev["kind"],
                                           "warned": False, "last": None, "name": dev["name"]}
            was_stale = entry.get("stale", False)
            entry["last_seen"] = time.time()
            entry["stale"] = False
            entry["name"] = dev["name"]
            if entry["last"] != (pct, charging, light, size) or was_stale:
                entry["icon"].icon = make_icon(dev["kind"], pct, charging, light, size)
                entry["icon"].title = title
                entry["icon"].menu = self.build_menu(title)
                entry["last"] = (pct, charging, light, size)
            # aviso de bateria baixa (uma vez por descarga)
            if pct <= LOW_BATTERY and not charging and not entry["warned"]:
                entry["warned"] = True
                try:
                    entry["icon"].notify(tr("low_body", name=dev["name"], pct=pct), tr("low_title"))
                except Exception:
                    pass
            elif pct > LOW_BATTERY + 5 or charging:
                entry["warned"] = False

        # remove dispositivos que sumiram
        now = time.time()
        for key in list(self.icons):
            if key in found:
                continue
            entry = self.icons[key]
            grace = GRACE_SECONDS.get(entry["kind"], GRACE_SECONDS["default"])
            if now - entry["last_seen"] >= grace:
                entry["icon"].stop()
                del self.icons[key]
            elif not entry.get("stale") and entry["last"]:
                # sem resposta (dormindo?): mantém o último valor e avisa no tooltip
                entry["stale"] = True
                entry["icon"].title = self.tooltip(entry["name"], entry["last"][0], False, stale=True)

    def poll(self):
        found = {}
        try:
            found.update(self.hidpp.read_all())
        except Exception as e:
            print("HID++ error:", e)
        try:
            found.update(read_headset())
        except Exception as e:
            print("Headset error:", e)
        return found

    def run(self):
        while not self.quitting:
            self.apply(self.poll())
            self.refresh.wait(POLL_SECONDS)
            self.refresh.clear()
        for entry in self.icons.values():
            entry["icon"].stop()


def diagnose():
    """python logi_battery_tray.py --diagnose  -> shows what each HID interface answers."""
    items = hid.enumerate(LOGITECH, 0)
    print("Logitech HID interfaces:")
    for it in items:
        print(f"  {it['product_string']!r} pid={it['product_id']:04X} "
              f"usage_page={it['usage_page']:04X} usage={it['usage']:04X} "
              f"interface={it['interface_number']}")
    print("\nHID++ probe (only devices that answer are listed):")
    for it in items:
        if it["usage_page"] < 0xFF00:
            continue
        dev = hid.device()
        try:
            dev.open_path(it["path"])
        except OSError as e:
            print(f"  pid={it['product_id']:04X} page={it['usage_page']:04X}: could not open ({e})")
            continue
        try:
            for idx in (*range(1, 7), 0xFF):
                name, kind = read_name_and_type(dev, idx)
                feat, data = read_battery(dev, idx)
                if name or data:
                    raw = None
                    if feat:
                        fi = feature_index(dev, idx, feat)
                        r = hidpp_call(dev, idx, fi, 1 if feat == 0x1004 else 0)
                        raw = r[:8].hex(" ") if r else None
                    print(f"  pid={it['product_id']:04X} page={it['usage_page']:04X} "
                          f"idx={idx}: name={name!r} type={kind} "
                          f"feature={hex(feat) if feat else None} battery={data} raw={raw}")
        finally:
            dev.close()
    print("\nHeadsetControl:", find_headsetcontrol() or "not found")
    print("End of diagnostics.")


if __name__ == "__main__":
    if "--diagnose" in sys.argv or "--diagnostico" in sys.argv:
        diagnose()
    elif acquire_single_instance():
        make_dpi_aware()
        App().run()