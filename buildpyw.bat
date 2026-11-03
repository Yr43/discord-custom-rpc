@echo off
python -m pip install customtkinter pypresence pystray pillow pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name DiscordRPC --collect-all customtkinter --hidden-import pystray._win32 app.pyw
pause