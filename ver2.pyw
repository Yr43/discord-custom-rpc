import tkinter as tk
from tkinter import ttk, messagebox
import time
import threading
import sys

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception as e:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "Нужно установить библиотеки",
        "Выполни в терминале:\n\n"
        "pip install pypresence pystray pillow\n\n" + str(e)
    )
    sys.exit()


APP_ID = "1566876330153349160"


class DiscordRPCApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Discord Custom Status")
        self.root.geometry("420x360")
        self.root.resizable(False, False)

        self.rpc = None
        self.connecting = False
        self.exiting = False
        self.auto_reconnect = True
        self.start_time = None
        self.lock = threading.Lock()
        self.library_error_shown = False

        self.current_details = ""
        self.current_state = ""
        self.current_image = ""

        ttk.Label(root, text="Details:").pack(pady=(15, 0), padx=15, anchor="w")
        self.details = ttk.Entry(root, width=45)
        self.details.insert(0, "я в картошке, а не ")
        self.details.pack(padx=15)

        ttk.Label(root, text="State:").pack(pady=(10, 0), padx=15, anchor="w")
        self.state = ttk.Entry(root, width=45)
        self.state.insert(0, "я картошка")
        self.state.pack(padx=15)

        ttk.Label(root, text="Image URL:").pack(pady=(10, 0), padx=15, anchor="w")
        self.image = ttk.Entry(root, width=45)
        self.image.pack(padx=15)

        self.status = ttk.Label(root, text="Не подключено", foreground="red")
        self.status.pack(pady=(10, 0))

        buttons = ttk.Frame(root)
        buttons.pack(pady=15)

        ttk.Button(
            buttons,
            text="Подключить",
            command=self.start_connect
        ).grid(row=0, column=0, padx=5)

        ttk.Button(
            buttons,
            text="Обновить",
            command=self.refresh_status
        ).grid(row=0, column=1, padx=5)

        ttk.Button(
            buttons,
            text="Отключить",
            command=self.disconnect
        ).grid(row=0, column=2, padx=5)

        # При закрытии окна сворачиваем в трей
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)

        self.create_tray()

        # Запускаем свернутым
        self.root.withdraw()

        # Автоподключение после запуска
        self.root.after(1000, self.start_connect)

        # Фоновая проверка каждые 20 секунд
        self.root.after(20000, self.heartbeat)

    def create_tray(self):
        image = Image.new("RGB", (64, 64), (88, 101, 242))
        draw = ImageDraw.Draw(image)
        draw.ellipse((12, 12, 52, 52), fill=(255, 255, 255))

        menu = pystray.Menu(
            pystray.MenuItem("Показать", self.show_window, default=True),
            pystray.MenuItem("Подключить", self.start_connect),
            pystray.MenuItem("Обновить", self.refresh_status),
            pystray.MenuItem("Отключить", self.disconnect),
            pystray.MenuItem("Выход", self.exit_app)
        )

        self.icon = pystray.Icon(
            "discord_rpc",
            image,
            "Discord Custom Status",
            menu
        )

        threading.Thread(target=self.icon.run, daemon=True).start()

    def set_status(self, text, color="black"):
        try:
            self.status.config(text=text, foreground=color)
        except Exception:
            pass

    def show_window(self, icon=None, item=None):
        self.root.after(0, self._show_window)

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self):
        self.root.withdraw()

    def start_connect(self, icon=None, item=None):
        if self.exiting or self.connecting or self.rpc:
            return

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
                self.root.after(
                    0,
                    lambda err=str(e): messagebox.showerror(
                        "Ошибка библиотеки",
                        "Не установлен pypresence.\n\n"
                        "Выполни в терминале:\n"
                        "pip install pypresence\n\n" + err
                    )
                )

            self.root.after(0, lambda: self.set_status("Ошибка библиотеки", "red"))
            return

        try:
            rpc = Presence(APP_ID)
            rpc.connect()

            if not self.auto_reconnect:
                try:
                    rpc.close()
                except Exception:
                    pass
                return

            with self.lock:
                self.rpc = rpc
                if self.start_time is None:
                    self.start_time = int(time.time())

            self.root.after(0, lambda: self.set_status("Подключено", "green"))
            self._read_values()
            self._update_rpc()

        except Exception:
            self.root.after(
                0,
                lambda: self.set_status("Discord не запущен или недоступен", "red")
            )

            if not self.exiting and self.auto_reconnect:
                self.root.after(5000, self.start_connect)

        finally:
            self.connecting = False

    def refresh_status(self, icon=None, item=None):
        self._read_values()

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

        self.root.after(20000, self.heartbeat)

    def _read_values(self):
        try:
            self.current_details = self.details.get().strip()
            self.current_state = self.state.get().strip()
            self.current_image = self.image.get().strip()
        except Exception:
            pass

    def _update_rpc(self):
        with self.lock:
            rpc = self.rpc
            start_time = self.start_time

        if not rpc:
            return

        try:
            rpc.update(
                details=self.current_details,
                state=self.current_state,
                large_image=self.current_image if self.current_image else None,
                start=start_time if start_time else int(time.time())
            )

            self.root.after(0, lambda: self.set_status("Статус обновлён", "green"))

        except Exception:
            with self.lock:
                rpc = self.rpc
                self.rpc = None
                self.start_time = None

            if rpc:
                try:
                    rpc.close()
                except Exception:
                    pass

            self.root.after(
                0,
                lambda: self.set_status("Отключено, переподключение...", "red")
            )

            if not self.exiting and self.auto_reconnect:
                self.root.after(3000, self.start_connect)

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

        self.root.after(0, lambda: self.set_status("Отключено", "red"))

    def exit_app(self, icon=None, item=None):
        self.exiting = False
        self.auto_reconnect = False
        self.exiting = True

        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass

        threading.Thread(target=self._shutdown_worker, daemon=True).start()
        self.root.after(400, self.root.destroy)

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
    root = tk.Tk()
    app = DiscordRPCApp(root)
    root.mainloop()
