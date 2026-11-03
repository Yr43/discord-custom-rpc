import tkinter as tk
from tkinter import messagebox, filedialog
import sys
import os
import io
import shutil
import json
import time
import queue
import socket
import logging
import threading
import webbrowser
import urllib.request
from logging.handlers import RotatingFileHandler

try:
    import customtkinter as ctk
except Exception as e:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "Ошибка",
        "Выполни в терминале:\npip install customtkinter pypresence pystray pillow\n\n" + str(e)
    )
    sys.exit()

try:
    import pystray
    from PIL import Image, ImageDraw, ImageOps
except Exception as e:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "Ошибка",
        "Выполни в терминале:\npip install pystray pillow\n\n" + str(e)
    )
    sys.exit()


APP_TITLE = "Discord Custom Status"
VERSION = "4.1"

DEFAULT_APP_ID = "1566876330153349160"

PRIMARY = "#5865F2"
SUCCESS = "#23a55a"
DANGER = "#f23f43"
WARNING = "#f0b232"
MUTED = "#9aa0a6"
TEXT = ("gray10", "gray90")
CARD_BG = ("#f2f3f5", "#1e1f22")

INSTANCE_PORT = 47653          # защита от второго запуска
MIN_ROTATION = 15              # Discord не любит обновления чаще ~15 сек
USER_AGENT = "DiscordCustomStatus/" + VERSION

THEME_LABELS = {
    "dark": "Тёмная",
    "light": "Светлая",
    "system": "Системная",
    "glass": "Стекло (прозрачность + блюр)"
}
THEME_KEYS = {v: k for k, v in THEME_LABELS.items()}


def apply_window_effect(window, theme):
    """Применяет прозрачность и системный acrylic blur в Windows 10/11."""
    glass = theme == "glass"

    try:
        window.attributes("-alpha", 0.93 if glass else 1.0)
    except Exception:
        pass

    if sys.platform != "win32":
        return

    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        hwnd = user32.GetAncestor(ctypes.c_void_p(window.winfo_id()), 2)
        if not hwnd:
            hwnd = ctypes.c_void_p(window.winfo_id())

        class ACCENT_POLICY(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_int),
                ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_int),
            ]

        class WINDOW_COMPOSITION_ATTRIB_DATA(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_int),
                ("Data", ctypes.POINTER(ACCENT_POLICY)),
                ("SizeOfData", ctypes.c_size_t),
            ]

        # ACCENT_ENABLE_ACRYLICBLURBEHIND = 4; ACCENT_DISABLED = 0.
        accent = ACCENT_POLICY(
            4 if glass else 0,
            2,
            0xCC20232A if glass else 0,
            0,
        )
        data = WINDOW_COMPOSITION_ATTRIB_DATA(
            19, ctypes.pointer(accent), ctypes.sizeof(accent)
        )
        setter = user32.SetWindowCompositionAttribute
        setter.argtypes = [ctypes.c_void_p, ctypes.POINTER(WINDOW_COMPOSITION_ATTRIB_DATA)]
        setter.restype = ctypes.c_int
        if not setter(hwnd, ctypes.byref(data)):
            raise OSError("SetWindowCompositionAttribute вернул ошибку")
    except Exception as exc:
        # Fallback для Windows 11, где доступен системный backdrop Acrylic.
        try:
            import ctypes
            hwnd = ctypes.c_void_p(window.winfo_id())
            backdrop = ctypes.c_int(3 if glass else 1)  # 3 = Acrylic, 1 = None
            dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
            dwm.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
            dwm.restype = ctypes.c_long
            dwm(hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
        except Exception:
            if glass:
                logger.warning("Не удалось включить системный blur: %s", exc)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "DiscordCustomStatus"


# ---------------------------------------------------------------- пути

def get_documents_folder():
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)

            if buf.value:
                return buf.value
        except Exception:
            pass

    return os.path.join(os.path.expanduser("~"), "Documents")


def get_data_dir():
    try:
        path = os.path.join(get_documents_folder(), "rpc")
        os.makedirs(path, exist_ok=True)
        return path
    except Exception:
        path = os.path.join(os.path.expanduser("~"), ".discord_rpc")
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
        return path


DATA_DIR = get_data_dir()
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
LOG_FILE = os.path.join(DATA_DIR, "log.txt")


# ---------------------------------------------------------------- логи

logger = logging.getLogger("rpc")
logger.setLevel(logging.INFO)

try:
    _handler = RotatingFileHandler(
        LOG_FILE, maxBytes=512 * 1024, backupCount=1, encoding="utf-8"
    )
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(_handler)
except Exception:
    logger.addHandler(logging.NullHandler())


# ---------------------------------------------------------------- конфиг

def normalize_profile(raw, default_app_id=DEFAULT_APP_ID):
    """Нормализует профиль; старые image/buttons игнорируются."""
    raw = raw if isinstance(raw, dict) else {}

    app_id = str(raw.get("app_id") or "").strip()
    if not app_id.isdigit():
        app_id = str(default_app_id)

    return {
        "app_id": app_id,
        "details": str(raw.get("details", "")),
        "state": str(raw.get("state", "")),
    }


DEFAULT_CONFIG = {
    "app_id": DEFAULT_APP_ID,
    "details": "я в картошке, а не ",
    "state": "я картошка",
    "auto_connect": True,
    "fallback_name": "Custom Status",
    "theme": "dark",
    "notifications": True,
    "rotation_enabled": False,
    "rotation_interval": 60,
    "profiles": {},
    "active_profile": "Основной"
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))

    for path in (CONFIG_FILE, CONFIG_FILE + ".bak"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
                if path != CONFIG_FILE:
                    logger.warning("Основной конфиг повреждён, загружена копия .bak")
                break
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.warning("Не удалось прочитать %s: %s", path, e)

    cfg["app_id"] = str(cfg.get("app_id", DEFAULT_APP_ID)).strip()
    if not cfg["app_id"].isdigit():
        cfg["app_id"] = DEFAULT_APP_ID

    cfg["auto_connect"] = bool(cfg.get("auto_connect", True))
    cfg["notifications"] = bool(cfg.get("notifications", True))
    cfg["rotation_enabled"] = bool(cfg.get("rotation_enabled", False))
    cfg["fallback_name"] = str(cfg.get("fallback_name", "Custom Status")).strip() or "Custom Status"

    if cfg.get("theme") not in THEME_LABELS:
        cfg["theme"] = "dark"

    try:
        cfg["rotation_interval"] = max(MIN_ROTATION, int(cfg.get("rotation_interval", 60)))
    except Exception:
        cfg["rotation_interval"] = 60

    # профили; конфиг от v3 превращается в профиль «Основной»
    profiles = {}
    raw_profiles = cfg.get("profiles")

    if isinstance(raw_profiles, dict):
        for name, p in raw_profiles.items():
            name = str(name).strip()
            if name:
                profiles[name] = normalize_profile(p, cfg["app_id"])

    if not profiles:
        profiles["Основной"] = normalize_profile({
            "details": cfg.get("details", ""),
            "state": cfg.get("state", ""),
        }, cfg["app_id"])

    cfg["profiles"] = profiles
    # Удаляем оставшиеся ключи от старой версии приложения.
    cfg.pop("image", None)
    cfg.pop("buttons", None)

    if cfg.get("active_profile") not in profiles:
        cfg["active_profile"] = next(iter(profiles))

    return cfg


def save_config(cfg):
    """Атомарная запись: сначала во временный файл, потом замена.
    Предыдущая версия остаётся в config.json.bak."""
    tmp = CONFIG_FILE + ".tmp"

    try:
        os.makedirs(DATA_DIR, exist_ok=True)

        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
            f.flush()
            os.fsync(f.fileno())

        if os.path.exists(CONFIG_FILE):
            try:
                shutil.copy2(CONFIG_FILE, CONFIG_FILE + ".bak")
            except Exception:
                pass

        os.replace(tmp, CONFIG_FILE)
        return True, None
    except Exception as e:
        return False, str(e)


def make_startup_backup(keep=10):
    """При каждом запуске кладёт копию конфига в Документы\\rpc\\backups."""
    try:
        if not os.path.exists(CONFIG_FILE):
            return

        bdir = os.path.join(DATA_DIR, "backups")
        os.makedirs(bdir, exist_ok=True)

        shutil.copy2(
            CONFIG_FILE,
            os.path.join(bdir, "config_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
        )

        files = sorted(
            f for f in os.listdir(bdir)
            if f.startswith("config_") and f.endswith(".json")
        )

        for f in files[:-keep]:
            os.remove(os.path.join(bdir, f))
    except Exception as e:
        logger.warning("Не удалось сделать бэкап: %s", e)


# ---------------------------------------------------------------- утилиты

def clean_text(s):
    s = (s or "").strip()[:128]

    if not s:
        return None

    if len(s) == 1:
        s += "\u200b"   # Discord требует минимум 2 символа

    return s


def http_get(url, timeout=8, limit=5 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(limit + 1)

    if len(data) > limit:
        raise ValueError("Файл слишком большой")

    return data


def fetch_app_info(app_id):
    """Имя и иконка приложения из публичного API Discord (токен не нужен)."""
    raw = http_get("https://discord.com/api/v10/applications/%s/rpc" % app_id)
    data = json.loads(raw.decode("utf-8"))

    name = str(data.get("name") or "").strip() or None
    icon = data.get("icon")
    icon_bytes = None

    if icon:
        icon_bytes = http_get(
            "https://cdn.discordapp.com/app-icons/%s/%s.png?size=128" % (app_id, icon)
        )

    return name, icon_bytes


def make_preview_image(data, size=80, radius=14):
    img = Image.open(io.BytesIO(data)).convert("RGBA")
    img = ImageOps.fit(img, (size * 2, size * 2), Image.LANCZOS)

    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, img.size[0] - 1, img.size[1] - 1), radius=radius * 2, fill=255
    )

    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def make_placeholder(size=80, radius=14):
    s = size * 2
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius * 2, fill=(88, 101, 242, 255))
    draw.ellipse((s * 0.3, s * 0.3, s * 0.7, s * 0.7), fill=(255, 255, 255, 255))
    return img


def open_path(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            os.system('open "%s"' % path)
        else:
            os.system('xdg-open "%s"' % path)
    except Exception as e:
        logger.warning("Не удалось открыть %s: %s", path, e)


# ---------------------------------------------------------------- автозагрузка (Windows)

def autostart_supported():
    return sys.platform == "win32"


def autostart_command():
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable

    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")

    if os.path.exists(pyw):
        exe = pyw

    return '"%s" "%s"' % (exe, os.path.abspath(sys.argv[0]))


def is_autostart():
    if not autostart_supported():
        return False

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except Exception:
        return False


def set_autostart(enabled):
    if not autostart_supported():
        return False, "Автозагрузка поддерживается только в Windows"

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except FileNotFoundError:
                    pass

        return True, None
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------- защита от второго запуска

def acquire_instance_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    try:
        s.bind(("127.0.0.1", INSTANCE_PORT))
        return s
    except OSError:
        try:
            s.close()
        except Exception:
            pass

        try:
            c = socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=2)
            c.sendall(b"show")
            c.close()
        except Exception:
            pass

        return None


# ---------------------------------------------------------------- приложение

class DiscordRPCApp(ctk.CTk):
    def __init__(self, cfg, lock_sock=None):
        super().__init__()

        self.cfg = cfg
        self.profiles = cfg["profiles"]
        self.active_profile = cfg["active_profile"]
        self.app_id = self.profiles[self.active_profile]["app_id"]
        self.rpc_app_id = None

        self.rpc = None
        self.connecting = False
        self.exiting = False
        self.auto_reconnect = cfg["auto_connect"]
        self.start_time = None
        self.lock = threading.Lock()
        self.library_error_shown = False
        self.settings_win = None
        self.icon = None

        self.was_connected = False
        self.fail_logged = False
        self.last_payload = None

        self.current_details = ""
        self.current_state = ""

        # превью
        self.preview_start = int(time.time())
        self.app_infos = {}
        self.info_inflight = set()
        self._pv_img = None
        self._last_sig = None
        self._info_job = None
        self._save_job = None
        self._placeholder = make_placeholder()

        # ротация
        self.rotation_job = None

        # очередь для безопасных вызовов UI из потоков
        self.ui_queue = queue.Queue()

        self.title(APP_TITLE)
        self.geometry("580x740")
        self.resizable(False, False)
        self.grid_columnconfigure(0, weight=1)

        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.hide_window)
        self.bind_hotkeys()

        if not os.path.exists(CONFIG_FILE):
            self.persist()

        self.create_tray()
        self.withdraw()

        if lock_sock is not None:
            self.start_instance_server(lock_sock)

        self.set_preview_image(self._placeholder)
        self.check_fields()
        self.ensure_app_info()
        self.after(200, self.apply_theme_effects)

        logger.info("Запуск v%s, Application ID %s, профиль «%s»",
                    VERSION, self.app_id, self.active_profile)

        self.after(80, self._pump)
        self.after(1000, self.tick_timer)
        self.after(20000, self.heartbeat)
        self.schedule_rotation()

        if self.auto_reconnect:
            self.after(1000, self.start_connect)

    # ------------------------------------------------ потокобезопасный UI

    def ui(self, fn):
        self.ui_queue.put(fn)

    def _pump(self):
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    logger.exception("Ошибка в UI-вызове")
        except queue.Empty:
            pass

        self.after(80, self._pump)

    def report_callback_exception(self, exc, val, tb):
        logger.error("Ошибка Tk", exc_info=(exc, val, tb))

    # ------------------------------------------------ интерфейс

    def apply_theme_effects(self):
        apply_window_effect(self, self.cfg.get("theme", "dark"))

    def build_ui(self):
        # --- шапка + статус
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 4))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header, text=APP_TITLE, font=ctk.CTkFont(size=24, weight="bold")
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            header,
            text="Rich Presence · профили · ротация · превью",
            text_color=MUTED,
            font=ctk.CTkFont(size=12)
        ).grid(row=1, column=0, sticky="w")

        self.status_label = ctk.CTkLabel(
            header,
            text="Не подключено",
            text_color=DANGER,
            font=ctk.CTkFont(size=14, weight="bold")
        )
        self.status_label.grid(row=0, column=1, rowspan=2, sticky="e")

        # --- профили (вкладки)
        prof = ctk.CTkFrame(self, corner_radius=12)
        prof.grid(row=1, column=0, sticky="ew", padx=24, pady=(8, 6))
        prof.grid_columnconfigure(0, weight=1)

        self.tab_bar = ctk.CTkScrollableFrame(
            prof, orientation="horizontal", height=38, fg_color="transparent"
        )
        self.tab_bar.grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=(6, 4))

        self.more_btn = ctk.CTkButton(
            prof, text="⋮", width=34, height=32,
            fg_color="transparent", border_width=1, text_color=TEXT,
            command=self.open_profile_menu_button
        )
        self.more_btn.grid(row=0, column=1, padx=(0, 10), pady=(6, 4), sticky="n")

        self.tab_widgets = []
        self.tab_buttons = {}
        self.rebuild_tabs()

        # --- превью
        card = ctk.CTkFrame(self, corner_radius=12, fg_color=CARD_BG)
        card.grid(row=2, column=0, sticky="ew", padx=24, pady=6)
        card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            card, text="ИГРАЕТ В", text_color=MUTED,
            font=ctk.CTkFont(size=11, weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(12, 6))

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))

        self.pv_image = ctk.CTkLabel(body, text="", width=80, height=80)
        self.pv_image.grid(row=0, column=0, padx=(0, 12), sticky="n")

        info = ctk.CTkFrame(body, fg_color="transparent")
        info.grid(row=0, column=1, sticky="nw")

        self.pv_name = ctk.CTkLabel(
            info, text="", anchor="w", justify="left", wraplength=380,
            font=ctk.CTkFont(size=14, weight="bold")
        )
        self.pv_name.grid(row=0, column=0, sticky="w")

        self.pv_details = ctk.CTkLabel(
            info, text="", anchor="w", justify="left", wraplength=380,
            font=ctk.CTkFont(size=13)
        )
        self.pv_details.grid(row=1, column=0, sticky="w")

        self.pv_state = ctk.CTkLabel(
            info, text="", anchor="w", justify="left", wraplength=380,
            font=ctk.CTkFont(size=13)
        )
        self.pv_state.grid(row=2, column=0, sticky="w")

        self.pv_time = ctk.CTkLabel(
            info, text="00:00 прошло", anchor="w", text_color=SUCCESS,
            font=ctk.CTkFont(size=13)
        )
        self.pv_time.grid(row=3, column=0, sticky="w")

        # --- поля
        form = ctk.CTkFrame(self, corner_radius=12)
        form.grid(row=3, column=0, sticky="ew", padx=24, pady=6)
        form.grid_columnconfigure(1, weight=1)

        prof_data = self.profiles[self.active_profile]

        self.app_id_entry = self.add_field(form, 0, "Application ID", prof_data["app_id"])
        self._entry_border = self.app_id_entry.cget("border_color")
        self.details_entry = self.add_field(form, 1, "Строка 1", prof_data["details"])
        self.state_entry = self.add_field(form, 2, "Строка 2", prof_data["state"])

        help_text = (
            "Как получить Application ID: открой Discord Developer Portal → "
            "New Application → создай приложение → General Information → "
            "Application ID → Copy. Затем вставь ID в первое поле. "
            "Нажми сюда, чтобы открыть портал."
        )
        self.application_id_help = ctk.CTkLabel(
            form, text=help_text, text_color=PRIMARY,
            font=ctk.CTkFont(size=11), wraplength=470,
            justify="left", anchor="w", cursor="hand2"
        )
        self.application_id_help.grid(
            row=3, column=0, columnspan=2, padx=18, pady=(2, 10), sticky="w"
        )
        self.application_id_help.bind(
            "<Button-1>",
            lambda _event: webbrowser.open("https://discord.com/developers/applications")
        )

        for e in [self.app_id_entry, self.details_entry, self.state_entry]:
            e.bind("<KeyRelease>", lambda ev: self.check_fields())

        # --- кнопки
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=4, column=0, sticky="ew", padx=24, pady=(6, 4))
        buttons.grid_columnconfigure((0, 1, 2), weight=1)

        self.connect_btn = ctk.CTkButton(
            buttons, text="Подключить", height=40, corner_radius=10,
            fg_color=SUCCESS, hover_color="#1c8f4d",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.start_connect
        )
        self.connect_btn.grid(row=0, column=0, padx=(0, 8), sticky="ew")

        self.update_btn = ctk.CTkButton(
            buttons, text="Обновить", height=40, corner_radius=10,
            fg_color=PRIMARY, hover_color="#4752c4",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.refresh_status
        )
        self.update_btn.grid(row=0, column=1, padx=8, sticky="ew")

        self.disconnect_btn = ctk.CTkButton(
            buttons, text="Отключить", height=40, corner_radius=10,
            fg_color=DANGER, hover_color="#c73236",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.disconnect
        )
        self.disconnect_btn.grid(row=0, column=2, padx=(8, 0), sticky="ew")

        self.settings_btn = ctk.CTkButton(
            buttons, text="Настройки", height=40, corner_radius=10,
            fg_color="transparent", border_width=1, text_color=TEXT,
            font=ctk.CTkFont(size=14),
            command=self.open_settings
        )
        self.settings_btn.grid(row=1, column=0, columnspan=3, pady=(8, 0), sticky="ew")

        self.footer_label = ctk.CTkLabel(
            self, text="Автосохранение включено · " + DATA_DIR, text_color=MUTED,
            font=ctk.CTkFont(size=11), wraplength=530, justify="left"
        )
        self.footer_label.grid(row=5, column=0, sticky="w", padx=24, pady=(4, 12))

    def add_field(self, parent, row, label_text, default_text=""):
        ctk.CTkLabel(
            parent, text=label_text, width=130, anchor="w", text_color=MUTED
        ).grid(row=row, column=0, padx=(18, 10), pady=5, sticky="w")

        entry = ctk.CTkEntry(parent, height=34, corner_radius=8)

        if default_text:
            entry.insert(0, default_text)

        entry.grid(row=row, column=1, padx=(0, 18), pady=5, sticky="ew")
        return entry

    @staticmethod
    def set_entry(entry, text):
        entry.delete(0, "end")
        if text:
            entry.insert(0, text)

    # ------------------------------------------------ чтение полей

    def read_fields(self):
        return {
            "app_id": self.app_id_entry.get().strip(),
            "details": self.details_entry.get().strip(),
            "state": self.state_entry.get().strip(),
        }

    def fields_signature(self):
        f = self.read_fields()
        return f["app_id"], f["details"], f["state"]

    def load_profile_to_fields(self, name):
        p = self.profiles[name]

        self.set_entry(self.app_id_entry, p["app_id"])
        self.set_entry(self.details_entry, p["details"])
        self.set_entry(self.state_entry, p["state"])

    # ------------------------------------------------ превью

    def preview_app_id(self):
        v = self.app_id_entry.get().strip()
        return v if v.isdigit() else self.app_id

    def display_name(self):
        info = self.app_infos.get(self.preview_app_id())
        name = info["name"] if info else None
        return name or self.cfg.get("fallback_name") or "Custom Status"

    def mark_app_id_validity(self):
        ok = self.app_id_entry.get().strip().isdigit()

        try:
            self.app_id_entry.configure(border_color=self._entry_border if ok else DANGER)
        except Exception:
            pass

    @staticmethod
    def set_line(label, text):
        if text:
            label.configure(text=text)
            label.grid()
        else:
            label.grid_remove()

    def check_fields(self):
        sig = self.fields_signature()

        if sig == self._last_sig:
            return

        old = self._last_sig
        self._last_sig = sig

        self.mark_app_id_validity()
        self.update_preview_text()

        if old is None or old[0] != sig[0]:
            self.show_app_icon()
            self.schedule_info_fetch()

        if old is not None:
            self.schedule_autosave()

    def update_preview_text(self):
        f = self.read_fields()

        self.pv_name.configure(text=self.display_name())
        self.set_line(self.pv_details, f["details"])
        self.set_line(self.pv_state, f["state"])

    def set_preview_image(self, pil):
        img = ctk.CTkImage(light_image=pil, dark_image=pil, size=(80, 80))
        self.pv_image.configure(image=img)
        self._pv_img = img

    def show_app_icon(self):
        info = self.app_infos.get(self.preview_app_id())
        pil = info["pil"] if info else None
        self.set_preview_image(pil or self._placeholder)

    def schedule_info_fetch(self):
        if self._info_job is not None:
            try:
                self.after_cancel(self._info_job)
            except Exception:
                pass

        self._info_job = self.after(700, self.ensure_app_info)

    def ensure_app_info(self):
        """Имя и иконка приложения по ID; результат кэшируется для каждого ID."""
        self._info_job = None
        aid = self.preview_app_id()

        if aid in self.app_infos or aid in self.info_inflight:
            return

        self.info_inflight.add(aid)

        def worker():
            try:
                name, icon_bytes = fetch_app_info(aid)
                pil = make_preview_image(icon_bytes) if icon_bytes else None
                logger.info("Приложение %s: %s", aid, name)
                self.ui(lambda: self._on_app_info(aid, name, pil, True))
            except Exception as e:
                logger.warning("Не удалось получить данные приложения %s: %s", aid, e)
                self.ui(lambda: self._on_app_info(aid, None, None, False))

        threading.Thread(target=worker, daemon=True).start()

    def _on_app_info(self, aid, name, pil, ok):
        self.info_inflight.discard(aid)

        if ok:
            self.app_infos[aid] = {"name": name, "pil": pil}

        if aid == self.preview_app_id():
            self.pv_name.configure(text=self.display_name())
            self.show_app_icon()

    def format_elapsed(self):
        start = self.start_time or self.preview_start
        sec = max(0, int(time.time()) - int(start))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)

        if h:
            return "%02d:%02d:%02d прошло" % (h, m, s)

        return "%02d:%02d прошло" % (m, s)

    def tick_timer(self):
        if self.exiting:
            return

        try:
            if self.state() != "withdrawn":
                self.pv_time.configure(text=self.format_elapsed())
                self.check_fields()
        except Exception:
            pass

        self.after(1000, self.tick_timer)

    # ------------------------------------------------ профили

    def commit_fields(self):
        """Правки в полях всегда принадлежат активному профилю."""
        if self.active_profile in self.profiles:
            prev_id = self.profiles[self.active_profile]["app_id"]
            self.profiles[self.active_profile] = normalize_profile(self.read_fields(), prev_id)

    def build_cfg_to_save(self):
        cur = self.profiles.get(self.active_profile) or normalize_profile({})
        cfg = dict(self.cfg)

        cfg.pop("image", None)
        cfg.pop("buttons", None)
        cfg.update({
            "app_id": cur["app_id"],
            "details": cur["details"],
            "state": cur["state"],
            "profiles": self.profiles,
            "active_profile": self.active_profile
        })

        return cfg

    def persist(self):
        self.commit_fields()
        self.cfg["profiles"] = self.profiles
        self.cfg["active_profile"] = self.active_profile

        ok, err = save_config(self.build_cfg_to_save())

        if ok:
            self.mark_saved()
        else:
            logger.error("Не удалось сохранить конфиг: %s", err)

        return ok, err

    def mark_saved(self):
        try:
            self.footer_label.configure(
                text="✓ Сохранено %s · %s" % (time.strftime("%H:%M:%S"), DATA_DIR)
            )
        except Exception:
            pass

    def schedule_autosave(self):
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except Exception:
                pass

        self._save_job = self.after(800, self.autosave)

    def autosave(self):
        self._save_job = None

        if not self.exiting:
            self.persist()

    def on_profile_selected(self, name):
        self.select_profile(name)

    def apply_entry_app_id(self):
        """Берёт ID из поля, если он корректный. True — ID изменился."""
        v = self.app_id_entry.get().strip()

        if v.isdigit() and v != self.app_id:
            self.app_id = v
            return True

        return False

    def reconnect_for_new_id(self):
        """Presence привязан к Application ID, поэтому при смене ID
        закрываем старое соединение и подключаемся заново."""
        logger.info("Смена Application ID → %s, переподключение", self.app_id)

        with self.lock:
            rpc = self.rpc
            self.rpc = None
            self.start_time = None

        self.last_payload = None
        self.set_status("Смена приложения...", "orange")

        def worker():
            if rpc:
                try:
                    rpc.clear()
                except Exception:
                    pass
                try:
                    rpc.close()
                except Exception:
                    pass

            self.ui(self._after_old_closed)

        threading.Thread(target=worker, daemon=True).start()

    def _after_old_closed(self):
        if self.exiting:
            return

        self.auto_reconnect = True
        self.start_connect()

    def select_profile(self, name, from_rotation=False):
        if name not in self.profiles:
            return

        if name != self.active_profile:
            self.commit_fields()

        self.active_profile = name
        self.load_profile_to_fields(name)
        self.update_tab_styles()

        id_changed = self.apply_entry_app_id()

        self._last_sig = None
        self.check_fields()
        self.ensure_app_info()

        self.persist()
        self.update_tray_menu()

        logger.info("Профиль «%s»%s", name, " (ротация)" if from_rotation else "")

        if self.rpc:
            if id_changed:
                self.reconnect_for_new_id()
            else:
                self._read_values()
                threading.Thread(target=self._update_rpc, daemon=True).start()

    # ---- вкладки

    def set_profile_values(self):
        self.rebuild_tabs()

    @staticmethod
    def tab_text(name):
        return name if len(name) <= 16 else name[:15] + "…"

    def rebuild_tabs(self):
        for w in self.tab_widgets:
            try:
                w.destroy()
            except Exception:
                pass

        self.tab_widgets = []
        self.tab_buttons = {}

        for i, name in enumerate(self.profiles.keys()):
            text = self.tab_text(name)

            btn = ctk.CTkButton(
                self.tab_bar, text=text,
                width=min(max(len(text) * 8 + 28, 64), 150),
                height=32, corner_radius=8,
                command=lambda n=name: self.on_tab_click(n)
            )
            btn.grid(row=0, column=i, padx=3)

            btn.bind("<Button-3>", lambda e, n=name: self.on_tab_right_click(n, e))
            btn.bind("<Button-2>", lambda e, n=name: self.on_tab_right_click(n, e))
            btn.bind("<Double-Button-1>", lambda e, n=name: self.on_tab_double_click(n))

            self.tab_buttons[name] = btn
            self.tab_widgets.append(btn)

        plus = ctk.CTkButton(
            self.tab_bar, text="+", width=36, height=32, corner_radius=8,
            fg_color="transparent", border_width=1, text_color=TEXT,
            command=self.new_profile
        )
        plus.grid(row=0, column=len(self.profiles), padx=3)
        self.tab_widgets.append(plus)

        self.update_tab_styles()

    def update_tab_styles(self):
        for name, btn in self.tab_buttons.items():
            try:
                if name == self.active_profile:
                    btn.configure(
                        fg_color=PRIMARY, hover_color="#4752c4", text_color="white"
                    )
                else:
                    btn.configure(
                        fg_color=("#dfe1e5", "#2b2d31"),
                        hover_color=("#d0d3d8", "#383a40"),
                        text_color=TEXT
                    )
            except Exception:
                pass

        self.after(50, self.scroll_to_active)

    def scroll_to_active(self):
        try:
            btn = self.tab_buttons.get(self.active_profile)

            if btn is None:
                return

            self.update_idletasks()
            canvas = self.tab_bar._parent_canvas
            bbox = canvas.bbox("all")
            total = max(1, bbox[2] - bbox[0])
            left = canvas.canvasx(0)
            right = left + canvas.winfo_width()
            x = btn.winfo_x()

            if x < left or x + btn.winfo_width() > right:
                canvas.xview_moveto(max(0.0, (x - 20) / total))
        except Exception:
            pass

    def on_tab_click(self, name):
        if name != self.active_profile:
            self.select_profile(name)

    def on_tab_double_click(self, name):
        self.on_tab_click(name)
        self.rename_profile()

    def on_tab_right_click(self, name, event):
        self.on_tab_click(name)
        self.popup_profile_menu(event.x_root, event.y_root)

    def open_profile_menu_button(self):
        self.popup_profile_menu(
            self.more_btn.winfo_rootx(),
            self.more_btn.winfo_rooty() + self.more_btn.winfo_height()
        )

    def popup_profile_menu(self, x, y):
        names = list(self.profiles.keys())

        try:
            i = names.index(self.active_profile)
        except ValueError:
            return

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Переименовать", command=self.rename_profile)
        menu.add_command(label="Дублировать", command=self.copy_profile)
        menu.add_separator()
        menu.add_command(
            label="Сдвинуть влево", command=lambda: self.move_profile(-1),
            state="normal" if i > 0 else "disabled"
        )
        menu.add_command(
            label="Сдвинуть вправо", command=lambda: self.move_profile(1),
            state="normal" if i < len(names) - 1 else "disabled"
        )
        menu.add_separator()
        menu.add_command(label="Удалить", command=self.delete_profile)

        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def move_profile(self, delta):
        names = list(self.profiles.keys())

        try:
            i = names.index(self.active_profile)
        except ValueError:
            return

        j = i + delta

        if not 0 <= j < len(names):
            return

        self.commit_fields()
        names[i], names[j] = names[j], names[i]
        self.profiles = {n: self.profiles[n] for n in names}

        self.persist()
        self.rebuild_tabs()
        self.update_tray_menu()
        logger.info("Профиль «%s» сдвинут (порядок влияет на ротацию)", self.active_profile)

    def switch_relative(self, delta):
        names = list(self.profiles.keys())

        if len(names) < 2:
            return

        try:
            i = names.index(self.active_profile)
        except ValueError:
            i = 0

        self.select_profile(names[(i + delta) % len(names)])

    def select_index(self, idx):
        names = list(self.profiles.keys())

        if 0 <= idx < len(names):
            self.on_tab_click(names[idx])

    def bind_hotkeys(self):
        def bind(seq, fn):
            try:
                self.bind(seq, lambda e: (fn(), "break")[1])
            except Exception:
                pass

        bind("<Control-Tab>", lambda: self.switch_relative(1))
        bind("<Control-Shift-Tab>", lambda: self.switch_relative(-1))
        bind("<Control-ISO_Left_Tab>", lambda: self.switch_relative(-1))

        for n in range(1, 10):
            bind("<Control-Key-%d>" % n, lambda k=n: self.select_index(k - 1))

    def ask_name(self, title, text):
        dialog = ctk.CTkInputDialog(text=text, title=title)
        return (dialog.get_input() or "").strip()

    def unique_name(self, base):
        name = base
        i = 2

        while name in self.profiles:
            name = "%s %d" % (base, i)
            i += 1

        return name

    def new_profile(self):
        """Пустой профиль; Application ID берётся от текущего."""
        name = self.ask_name("Новый профиль", "Название профиля:")

        if not name:
            return

        if name in self.profiles:
            messagebox.showinfo("Профили", "Профиль «%s» уже существует." % name)
            return

        self.commit_fields()
        cur_id = self.profiles[self.active_profile]["app_id"]
        self.profiles[name] = normalize_profile({}, cur_id)

        self.set_profile_values()
        logger.info("Создан профиль «%s»", name)
        self.select_profile(name)

    def copy_profile(self):
        self.commit_fields()
        src = self.profiles[self.active_profile]
        name = self.unique_name(self.active_profile + " (копия)")

        self.profiles[name] = normalize_profile(src, src["app_id"])

        self.set_profile_values()
        logger.info("Профиль «%s» скопирован в «%s»", self.active_profile, name)
        self.select_profile(name)

    def rename_profile(self):
        old = self.active_profile
        name = self.ask_name("Переименовать", "Новое название для «%s»:" % old)

        if not name or name == old:
            return

        if name in self.profiles:
            messagebox.showinfo("Профили", "Профиль «%s» уже существует." % name)
            return

        self.commit_fields()
        self.profiles = {(name if k == old else k): v for k, v in self.profiles.items()}
        self.active_profile = name

        self.set_profile_values()
        self.persist()
        self.update_tray_menu()
        logger.info("Профиль «%s» переименован в «%s»", old, name)

    def delete_profile(self):
        if len(self.profiles) <= 1:
            messagebox.showinfo("Профили", "Нельзя удалить последний профиль.")
            return

        name = self.active_profile

        if not messagebox.askyesno("Удалить профиль", "Удалить профиль «%s»?" % name):
            return

        del self.profiles[name]
        nxt = next(iter(self.profiles))

        self.active_profile = ""
        self.set_profile_values()
        logger.info("Удалён профиль «%s»", name)
        self.select_profile(nxt)

    def export_profiles(self):
        self.persist()

        path = filedialog.asksaveasfilename(
            parent=self.settings_win or self,
            title="Экспорт профилей",
            defaultextension=".json",
            initialfile="rpc_profiles.json",
            filetypes=[("JSON", "*.json")]
        )

        if not path:
            return

        data = {
            "format": "discord-rpc-profiles",
            "version": 1,
            "profiles": self.profiles
        }

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            messagebox.showerror("Экспорт", "Не удалось сохранить файл:\n" + str(e))
            return

        logger.info("Экспортировано профилей: %d → %s", len(self.profiles), path)
        messagebox.showinfo("Экспорт", "Профилей сохранено: %d" % len(self.profiles))

    def import_profiles(self):
        path = filedialog.askopenfilename(
            parent=self.settings_win or self,
            title="Импорт профилей",
            filetypes=[("JSON", "*.json"), ("Все файлы", "*.*")]
        )

        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            messagebox.showerror("Импорт", "Не удалось прочитать файл:\n" + str(e))
            return

        raw = data.get("profiles") if isinstance(data, dict) else None

        if not isinstance(raw, dict) or not raw:
            messagebox.showerror("Импорт", "В файле нет профилей.")
            return

        self.commit_fields()
        added = 0

        for name, p in raw.items():
            name = str(name).strip()

            if not name:
                continue

            final = name if name not in self.profiles else self.unique_name(name + " (импорт)")
            self.profiles[final] = normalize_profile(p, DEFAULT_APP_ID)
            added += 1

        self.set_profile_values()
        self.persist()
        self.update_tray_menu()
        self.schedule_rotation()

        logger.info("Импортировано профилей: %d из %s", added, path)
        messagebox.showinfo("Импорт", "Добавлено профилей: %d" % added)

    # ------------------------------------------------ ротация

    def schedule_rotation(self):
        if self.rotation_job is not None:
            try:
                self.after_cancel(self.rotation_job)
            except Exception:
                pass
            self.rotation_job = None

        if self.exiting:
            return

        if self.cfg.get("rotation_enabled") and len(self.profiles) > 1:
            delay = max(MIN_ROTATION, int(self.cfg.get("rotation_interval", 60))) * 1000
            self.rotation_job = self.after(delay, self.rotation_tick)

    def rotation_tick(self):
        self.rotation_job = None

        if self.exiting:
            return

        if self.cfg.get("rotation_enabled") and len(self.profiles) > 1 and self.rpc:
            names = list(self.profiles.keys())

            try:
                idx = names.index(self.active_profile)
            except ValueError:
                idx = -1

            self.select_profile(names[(idx + 1) % len(names)], from_rotation=True)

        self.schedule_rotation()

    def toggle_rotation(self, icon=None, item=None):
        self.ui(self._toggle_rotation)

    def _toggle_rotation(self):
        self.cfg["rotation_enabled"] = not self.cfg.get("rotation_enabled")
        self.persist()
        self.schedule_rotation()

        state = "включена" if self.cfg["rotation_enabled"] else "выключена"
        logger.info("Ротация %s", state)
        self.notify("Ротация статусов " + state)

        if self.cfg["rotation_enabled"] and len(self.profiles) < 2:
            self.set_status("Для ротации нужно минимум 2 профиля", "orange")

    # ------------------------------------------------ трей

    def create_tray(self):
        image = Image.new("RGB", (64, 64), (88, 101, 242))
        draw = ImageDraw.Draw(image)
        draw.ellipse((12, 12, 52, 52), fill=(255, 255, 255))

        menu = pystray.Menu(
            pystray.MenuItem("Показать", self.show_window, default=True),
            pystray.MenuItem("Профили", pystray.Menu(self.tray_profile_items)),
            pystray.MenuItem(
                "Ротация статусов",
                self.toggle_rotation,
                checked=lambda item: bool(self.cfg.get("rotation_enabled"))
            ),
            pystray.MenuItem("Настройки", self.open_settings),
            pystray.MenuItem("Подключить", self.tray_connect),
            pystray.MenuItem("Обновить", self.tray_refresh),
            pystray.MenuItem("Отключить", self.tray_disconnect),
            pystray.MenuItem("Выход", self.exit_app)
        )

        self.icon = pystray.Icon("discord_rpc", image, APP_TITLE, menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def tray_profile_items(self):
        items = []

        for name in list(self.profiles.keys()):
            items.append(pystray.MenuItem(
                name,
                self._make_profile_action(name),
                checked=self._make_profile_checked(name),
                radio=True
            ))

        return items

    def _make_profile_action(self, name):
        def action(icon, item):
            self.ui(lambda: self.select_profile(name))
        return action

    def _make_profile_checked(self, name):
        def checked(item):
            return name == self.active_profile
        return checked

    def update_tray_menu(self):
        try:
            if self.icon:
                self.icon.update_menu()
        except Exception:
            pass

    def notify(self, text):
        if not self.cfg.get("notifications"):
            return

        try:
            if self.icon:
                self.icon.notify(text, APP_TITLE)
        except Exception as e:
            logger.warning("Уведомление не показано: %s", e)

    def tray_connect(self, icon=None, item=None):
        self.ui(self.start_connect)

    def tray_refresh(self, icon=None, item=None):
        self.ui(self.refresh_status)

    def tray_disconnect(self, icon=None, item=None):
        self.ui(self.disconnect)

    # ------------------------------------------------ второй запуск

    def start_instance_server(self, sock):
        def loop():
            try:
                sock.listen(5)
            except Exception:
                return

            while True:
                try:
                    conn, _ = sock.accept()
                except OSError:
                    break

                try:
                    data = conn.recv(16)
                    if data.startswith(b"show"):
                        logger.info("Повторный запуск: показываю окно")
                        self.ui(self._show_window)
                except Exception:
                    pass
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

        threading.Thread(target=loop, daemon=True).start()

    # ------------------------------------------------ статус и окна

    def set_status(self, text, color="gray"):
        colors = {
            "red": DANGER,
            "green": SUCCESS,
            "orange": WARNING,
            "gray": MUTED,
            "black": TEXT,
            "white": TEXT
        }

        try:
            self.status_label.configure(
                text=text,
                text_color=colors.get(color, color)
            )
        except Exception:
            pass

    def show_window(self, icon=None, item=None):
        self.ui(self._show_window)

    def _show_window(self):
        self.deiconify()
        self.apply_theme_effects()
        self.lift()
        self.focus_force()
        self.check_fields()

    def hide_window(self):
        try:
            self.persist()
        except Exception:
            pass

        self.withdraw()

    def open_settings(self, icon=None, item=None):
        self.ui(self._open_settings)

    def _open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.deiconify()
            self.settings_win.lift()
            self.settings_win.focus_force()
            return

        win = ctk.CTkToplevel(self)
        self.settings_win = win
        win.title("Настройки")
        win.geometry("560x650")
        win.resizable(False, False)
        win.grid_columnconfigure(0, weight=1)
        win.protocol("WM_DELETE_WINDOW", self.close_settings)
        win.after(150, lambda: apply_window_effect(win, self.cfg.get("theme", "dark")))

        ctk.CTkLabel(
            win, text="Настройки", font=ctk.CTkFont(size=24, weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=24, pady=(20, 4))

        ctk.CTkLabel(
            win,
            text="Application ID и две строки статуса задаются в профиле. "
                 "Для ID открой Discord Developer Portal → New Application → "
                 "General Information → Application ID → Copy.",
            text_color=MUTED, font=ctk.CTkFont(size=12),
            wraplength=500, justify="left"
        ).grid(row=1, column=0, sticky="w", padx=24, pady=(0, 6))

        form = ctk.CTkFrame(win, corner_radius=12)
        form.grid(row=2, column=0, sticky="ew", padx=24, pady=8)
        form.grid_columnconfigure(1, weight=1)

        self.s_fallback = self.add_field(
            form, 0, "Имя приложения", self.cfg.get("fallback_name", "")
        )

        ctk.CTkLabel(
            form, text="Тема", width=130, anchor="w", text_color=MUTED
        ).grid(row=1, column=0, padx=(18, 10), pady=5, sticky="w")

        self.s_theme = ctk.CTkOptionMenu(
            form, values=list(THEME_LABELS.values()), height=32
        )
        self.s_theme.set(THEME_LABELS.get(self.cfg.get("theme", "dark"), "Тёмная"))
        self.s_theme.grid(row=1, column=1, padx=(0, 18), pady=5, sticky="ew")

        self.s_interval = self.add_field(
            form, 2, "Ротация, сек", str(self.cfg.get("rotation_interval", 60))
        )

        self.s_rotation_var = tk.BooleanVar(value=bool(self.cfg.get("rotation_enabled")))
        self.s_notify_var = tk.BooleanVar(value=bool(self.cfg.get("notifications")))
        self.s_auto_var = tk.BooleanVar(value=bool(self.cfg.get("auto_connect")))
        self.s_autostart_var = tk.BooleanVar(value=is_autostart())

        switches = [
            ("Ротация статусов по таймеру", self.s_rotation_var),
            ("Уведомления в трей", self.s_notify_var),
            ("Автоподключение и переподключение", self.s_auto_var),
            ("Запускать вместе с Windows", self.s_autostart_var)
        ]

        for i, (text, var) in enumerate(switches):
            sw = ctk.CTkSwitch(form, text=text, variable=var)
            sw.grid(row=3 + i, column=0, columnspan=2, padx=18, pady=(4, 10), sticky="w")

            if var is self.s_autostart_var and not autostart_supported():
                sw.configure(state="disabled", text=text + " (только Windows)")

        tools = ctk.CTkFrame(win, fg_color="transparent")
        tools.grid(row=3, column=0, sticky="ew", padx=24, pady=(4, 0))
        tools.grid_columnconfigure((0, 1), weight=1)

        def tool_button(text, command, row, col):
            ctk.CTkButton(
                tools, text=text, height=34, corner_radius=10,
                fg_color="transparent", border_width=1, text_color=TEXT,
                command=command
            ).grid(
                row=row, column=col, sticky="ew", pady=(0, 8),
                padx=(0, 8) if col == 0 else (8, 0)
            )

        tool_button("Экспорт профилей", self.export_profiles, 0, 0)
        tool_button("Импорт профилей", self.import_profiles, 0, 1)
        tool_button("Открыть лог", lambda: open_path(LOG_FILE), 1, 0)
        tool_button("Открыть папку данных", lambda: open_path(DATA_DIR), 1, 1)

        buttons = ctk.CTkFrame(win, fg_color="transparent")
        buttons.grid(row=4, column=0, sticky="ew", padx=24, pady=(6, 20))
        buttons.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkButton(
            buttons, text="Сохранить", height=40, corner_radius=10,
            fg_color=PRIMARY, hover_color="#4752c4",
            command=self.save_settings
        ).grid(row=0, column=0, padx=(0, 8), sticky="ew")

        ctk.CTkButton(
            buttons, text="Закрыть", height=40, corner_radius=10,
            fg_color="transparent", border_width=1, text_color=TEXT,
            command=self.close_settings
        ).grid(row=0, column=1, padx=(8, 0), sticky="ew")

        win.lift()
        win.focus_force()

    def close_settings(self):
        if self.settings_win is not None:
            self.settings_win.destroy()
            self.settings_win = None

    def save_settings(self):
        try:
            interval = int(self.s_interval.get().strip())
        except Exception:
            messagebox.showerror("Ошибка", "Интервал ротации должен быть числом (секунды).")
            return

        interval = max(MIN_ROTATION, min(interval, 86400))

        self.cfg["fallback_name"] = self.s_fallback.get().strip() or "Custom Status"
        self.cfg["theme"] = THEME_KEYS.get(self.s_theme.get(), "dark")
        self.cfg["rotation_interval"] = interval
        self.cfg["rotation_enabled"] = bool(self.s_rotation_var.get())
        self.cfg["notifications"] = bool(self.s_notify_var.get())
        self.cfg["auto_connect"] = bool(self.s_auto_var.get())
        self.auto_reconnect = self.cfg["auto_connect"]

        want_autostart = bool(self.s_autostart_var.get())

        if autostart_supported() and want_autostart != is_autostart():
            ok, err = set_autostart(want_autostart)

            if ok:
                logger.info("Автозагрузка: %s", "включена" if want_autostart else "выключена")
            else:
                logger.error("Автозагрузка: %s", err)
                messagebox.showerror("Автозагрузка", "Не удалось изменить автозагрузку:\n" + str(err))

        appearance = "dark" if self.cfg["theme"] == "glass" else self.cfg["theme"]
        ctk.set_appearance_mode(appearance)
        self.apply_theme_effects()
        if self.settings_win is not None and self.settings_win.winfo_exists():
            apply_window_effect(self.settings_win, self.cfg["theme"])

        ok, err = self.persist()

        if not ok:
            messagebox.showerror("Ошибка сохранения", err)
            return

        self.schedule_rotation()

        self._last_sig = None
        self.check_fields()

        msg = "Настройки сохранены."

        if self.cfg["rotation_enabled"] and len(self.profiles) < 2:
            msg += "\n\nДля ротации нужно минимум 2 профиля."

        logger.info("Настройки сохранены")
        messagebox.showinfo("Сохранено", msg)

    # ------------------------------------------------ подключение

    def start_connect(self, icon=None, item=None):
        if self.exiting or self.connecting or self.rpc:
            return

        self.apply_entry_app_id()

        self.auto_reconnect = True
        self.connecting = True
        self.set_status("Подключение...", "orange")

        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        try:
            from pypresence import Presence
        except Exception as e:
            self.connecting = False

            if not self.library_error_shown:
                self.library_error_shown = True
                err = str(e)

                self.ui(lambda: messagebox.showerror(
                    "Ошибка библиотеки",
                    "Не установлен pypresence.\n\npip install pypresence\n\n" + err
                ))

            logger.error("pypresence не установлен: %s", e)
            self.ui(lambda: self.set_status("Ошибка библиотеки", "red"))
            return

        try:
            connect_id = self.app_id
            rpc = Presence(connect_id)
            rpc.connect()

            if not self.auto_reconnect:
                try:
                    rpc.close()
                except Exception:
                    pass
                return

            with self.lock:
                self.rpc = rpc
                self.rpc_app_id = connect_id
                if self.start_time is None:
                    self.start_time = int(time.time())

            self.ui(self._on_connected)

        except Exception as e:
            if not self.fail_logged:
                self.fail_logged = True
                logger.warning("Не удалось подключиться к Discord: %s", e)

            self.ui(lambda: self.set_status("Discord не запущен или недоступен", "red"))

            if not self.exiting and self.auto_reconnect:
                self.ui(lambda: self.after(5000, self.start_connect))

        finally:
            self.connecting = False

    def _on_connected(self):
        if self.rpc_app_id != self.app_id:
            # пока шло подключение, выбрали профиль с другим ID
            self.reconnect_for_new_id()
            return

        self.set_status("Подключено", "green")
        self.fail_logged = False

        if not self.was_connected:
            self.was_connected = True
            logger.info("Подключено к Discord")
            self.notify("Подключено к Discord")

        self._read_values()
        threading.Thread(target=self._update_rpc, daemon=True).start()

    def refresh_status(self, icon=None, item=None):
        if not self.app_id_entry.get().strip().isdigit():
            messagebox.showerror("Ошибка", "Application ID должен состоять только из цифр.")
            return

        self.persist()
        self._read_values()

        id_changed = self.apply_entry_app_id()

        if self.rpc and id_changed:
            self.reconnect_for_new_id()
            return

        if not self.rpc:
            self.auto_reconnect = True
            self.start_connect()
            return

        threading.Thread(target=self._update_rpc, daemon=True).start()

    def heartbeat(self):
        if self.exiting:
            return

        if self.rpc:
            self._read_values()
            threading.Thread(target=self._update_rpc, daemon=True).start()
        else:
            if self.auto_reconnect:
                self.start_connect()

        self.after(20000, self.heartbeat)

    def _read_values(self):
        try:
            f = self.read_fields()
            self.current_details = f["details"]
            self.current_state = f["state"]
        except Exception:
            pass

    def _update_rpc(self):
        with self.lock:
            rpc = self.rpc
            start_time = self.start_time

        if not rpc:
            return

        kwargs = {
            "details": clean_text(self.current_details),
            "state": clean_text(self.current_state),
            "start": start_time if start_time else int(time.time())
        }

        try:
            rpc.update(**kwargs)

            payload = (kwargs["details"], kwargs["state"])

            if payload != self.last_payload:
                self.last_payload = payload
                logger.info("Статус обновлён: %s | %s",
                            kwargs["details"], kwargs["state"])

            self.ui(lambda: self.set_status("Статус обновлён", "green"))

        except Exception as e:
            try:
                import pypresence.exceptions as pe
                rejected = tuple(
                    getattr(pe, n) for n in
                    ("ServerError", "DiscordError", "InvalidArgument", "ArgumentError")
                    if hasattr(pe, n)
                )
            except Exception:
                rejected = ()

            # Discord отклонил данные статуса — переподключение не требуется.
            if rejected and isinstance(e, rejected):
                logger.error("Discord отклонил статус: %s", e)
                self.ui(lambda: self.set_status("Discord отклонил данные статуса", "red"))
                return

            logger.warning("Соединение потеряно: %s", e)

            with self.lock:
                rpc = self.rpc
                self.rpc = None
                self.start_time = None

            if rpc:
                try:
                    rpc.close()
                except Exception:
                    pass

            if self.was_connected:
                self.was_connected = False
                self.notify("Соединение с Discord потеряно")

            self.ui(lambda: self.set_status("Отключено, переподключение...", "red"))

            if not self.exiting and self.auto_reconnect:
                self.ui(lambda: self.after(3000, self.start_connect))

    def disconnect(self, icon=None, item=None):
        self.auto_reconnect = False
        threading.Thread(target=self._disconnect_worker, daemon=True).start()

    def _disconnect_worker(self):
        with self.lock:
            rpc = self.rpc
            self.rpc = None
            self.start_time = None

        if rpc:
            try:
                rpc.clear()
                rpc.close()
            except Exception:
                pass

        self.last_payload = None
        logger.info("Отключено пользователем")

        if self.was_connected:
            self.was_connected = False
            self.notify("Отключено от Discord")

        self.ui(lambda: self.set_status("Отключено", "red"))

    def exit_app(self, icon=None, item=None):
        self.ui(self._exit_app)

    def _exit_app(self):
        if self.exiting:
            return

        self.exiting = True
        self.auto_reconnect = False

        try:
            self.persist()
        except Exception:
            pass

        logger.info("Выход")

        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass

        threading.Thread(target=self._shutdown_worker, daemon=True).start()
        self.after(400, self.destroy)

    def _shutdown_worker(self):
        with self.lock:
            rpc = self.rpc
            self.rpc = None

        if rpc:
            try:
                rpc.clear()
                rpc.close()
            except Exception:
                pass


if __name__ == "__main__":
    lock_sock = acquire_instance_lock()

    if lock_sock is None:
        logger.info("Уже запущено — второй экземпляр завершён")
        sys.exit(0)

    make_startup_backup()
    config = load_config()

    initial_appearance = "dark" if config["theme"] == "glass" else config["theme"]
    ctk.set_appearance_mode(initial_appearance)
    ctk.set_default_color_theme("dark-blue")

    app = DiscordRPCApp(config, lock_sock)
    app.mainloop()
