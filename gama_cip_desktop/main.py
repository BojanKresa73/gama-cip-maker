"""Gama United CIP Maker - local desktop program."""
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path


def bundled_ghostscript():
    """Use the Ghostscript shipped inside the installer, if present."""
    if os.environ.get("GAMA_GS"):
        return
    base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    for name in ("gswin64c.exe", "gswin32c.exe", "gs"):
        candidate = base / "ghostscript" / "bin" / name
        if candidate.is_file():
            os.environ["GAMA_GS"] = str(candidate)
            return


class Bridge:
    """Functions the window can call for native dialogs (window.pywebview.api.*)."""

    def __init__(self, app):
        self.app = app
        self.window = None

    def save_export(self, job_id, name, suggested):
        import webview
        source = self.app.jobs.path(job_id) / "results" / Path(name).name
        if not source.is_file():
            return {"error": "Izvoz ne postoji."}
        ext = Path(name).suffix
        kind = "PPF fajl (*.PPF;*.ppf)" if ext.lower() == ".ppf" else "ZIP arhiva (*.zip)"
        chosen = self.window.create_file_dialog(webview.SAVE_DIALOG, save_filename=suggested, file_types=(kind,))
        if not chosen:
            return {"cancelled": True}
        target = chosen if isinstance(chosen, str) else chosen[0]
        if not target.lower().endswith(ext.lower()):
            target += ext
        shutil.copyfile(source, target)
        return {"path": target}

    def show_in_folder(self, path):
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            webbrowser.open(Path(path).parent.as_uri())
        return True

    def open_data_folder(self):
        folder = str(self.app.root)
        if sys.platform == "win32":
            os.startfile(folder)  # noqa: S606 - user's own data folder
        else:
            webbrowser.open(Path(folder).as_uri())
        return True


def main():
    bundled_ghostscript()
    if "--selftest" in sys.argv:
        from gama_cip_desktop.selftest import main as selftest
        target = sys.argv[sys.argv.index("--selftest") + 1] if len(sys.argv) > sys.argv.index("--selftest") + 1 else "selftest.txt"
        sys.exit(selftest(target))
    from gama_cip_desktop.server import start

    app, server, url = start()
    if "--browser" in sys.argv:
        print("Gama CIP Maker:", url)
        webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    try:
        import webview
    except ImportError:
        webbrowser.open(url)
        server.serve_forever()
        return
    bridge = Bridge(app)
    bridge.window = webview.create_window("Gama United · CIP Maker", url, js_api=bridge, width=1360, height=880, min_size=(1024, 680))
    webview.start()
    server.shutdown()


if __name__ == "__main__":
    main()
