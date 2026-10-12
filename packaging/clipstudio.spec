# PyInstaller: сборка ClipStudio (папка с ClipStudio.exe). Запуск из корня репозитория:
#   pyinstaller packaging/clipstudio.spec --noconfirm
# ffmpeg кладётся заранее в packaging/ffmpeg (это делает GitHub Actions).
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = [
    (os.path.join(ROOT, "web"), "web"),
    (os.path.join(ROOT, "prompts"), "prompts"),
    (os.path.join(ROOT, "fonts"), "fonts"),
    (os.path.join(ROOT, "docs"), "docs"),
    (os.path.join(ROOT, "config.example.yaml"), "."),
    (os.path.join(ROOT, "sources.example.yaml"), "."),
    (os.path.join(ROOT, ".env.example"), "."),
]
ffmpeg_dir = os.path.join(SPECPATH, "ffmpeg")
if os.path.isdir(ffmpeg_dir):
    datas.append((ffmpeg_dir, "ffmpeg"))

binaries = []
hiddenimports = []
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli"))
hiddenimports += collect_submodules("yt_dlp")
hiddenimports += collect_submodules("clipstudio")
for pkg in ("faster_whisper", "ctranslate2"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
datas += collect_data_files("yt_dlp")

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "cv2", "scipy", "pandas", "torch", "tensorflow", "notebook", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ClipStudio",
    console=True,
    icon=os.path.join(SPECPATH, "icon.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="ClipStudio")
