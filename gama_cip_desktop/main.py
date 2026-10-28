"""Gama United CIP Maker - local desktop program."""
import faulthandler
import logging
import os
import shutil
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

LOG = logging.getLogger("gama.cip")


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


def setup_logging():
    """Log to %LOCALAPPDATA%\\GamaCIP\\gama-cip.log so problems on a user's PC can be diagnosed."""
    from gama_cip_desktop.store import data_dir

    path = data_dir() / "gama-cip.log"
    try:
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            path.replace(path.with_suffix(".old.log"))
    except OSError:
        pass
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(threadName)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    stream = open(path, "a", encoding="utf-8")  # noqa: SIM115 - kept open for crash dumps
    faulthandler.enable(stream)
    # A windowed .exe has no console: send stray output to the log instead of losing it.
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    sys.excepthook = lambda *exc: LOG.error("Neobrađena greška:\n%s", "".join(traceback.format_exception(*exc)))
    threading.excepthook = lambda a: LOG.error("Greška u niti %s:\n%s", a.thread.name if a.thread else "?",
                                               "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    return path, stream


class Bridge:
    """Functions the window can call for native dialogs (window.pywebview.api.*).

    pywebview walks every public attribute of this object recursively to build the
    JavaScript API. Everything that is not an API method MUST start with an underscore,
    otherwise it would walk into the .NET window and the job queue and freeze on Windows.
    """

    def __init__(self, app):
        self._app = app
        self._window = None

    def save_export(self, job_id, name, suggested):
        import webview
        source = self._app.jobs.path(job_id) / "results" / Path(name).name
        if not source.is_file():
            return {"error": "Izvoz ne postoji."}
        ext = Path(name).suffix
        kind = "PPF fajl (*.PPF;*.ppf)" if ext.lower() == ".ppf" else "ZIP arhiva (*.zip)"
        chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=suggested, file_types=(kind,))
        if not chosen:
            return {"cancelled": True}
        target = chosen if isinstance(chosen, str) else chosen[0]
        if not target.lower().endswith(ext.lower()):
            target += ext
        shutil.copyfile(source, target)
        return {"path": target}

    def choose_folder(self):
        import webview
        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not chosen:
            return None
        return chosen if isinstance(chosen, str) else chosen[0]

    def open_path(self, path):
        folder = Path(path)
        if not folder.is_dir():
            return False
        if sys.platform == "win32":
            os.startfile(str(folder))  # noqa: S606 - a folder the user configured
        else:
            webbrowser.open(folder.as_uri())
        return True

    def show_in_folder(self, path):
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            webbrowser.open(Path(path).parent.as_uri())
        return True

    def open_data_folder(self):
        folder = str(self._app.root)
        if sys.platform == "win32":
            os.startfile(folder)  # noqa: S606 - user's own data folder
        else:
            webbrowser.open(Path(folder).as_uri())
        return True


def gui_test(window, report, timeout=90):
    """CI check of the real window: page loads, JS runs, profiles arrive from the local server."""
    def fail(message):
        Path(report).write_text("GUITEST FAILED\n" + message + "\n", encoding="utf-8")
        os._exit(2)

    watchdog = threading.Timer(timeout, lambda: fail(f"Prozor nije odgovorio za {timeout} s."))
    watchdog.daemon = True
    watchdog.start()

    def check():
        import time
        for _ in range(120):
            try:
                count = window.evaluate_js("window.__cipState ? window.__cipState.profiles.length : 0")
                api = window.evaluate_js("!!(window.pywebview && window.pywebview.api && window.pywebview.api.choose_folder)")
                setup = window.evaluate_js("!!document.querySelector('#hotsetup')")
                if count and int(count) >= 5 and api and setup:
                    Path(report).write_text(f"Profila u prozoru: {count}\nJS API: {api}\nPitanje za hot foldere: {setup}\nGUITEST OK\n",
                                            encoding="utf-8")
                    watchdog.cancel()
                    window.destroy()
                    return
            except Exception as exc:  # noqa: BLE001 - retried until the watchdog fires
                LOG.info("guitest čeka: %s", exc)
            time.sleep(0.5)
        fail("Profili se nisu pojavili u prozoru.")

    window.events.loaded += lambda: threading.Thread(target=check, daemon=True, name="guitest").start()


def other_instance(data_root):
    """If the program already runs, bring its window to the front and return True."""
    import json
    from urllib.request import Request, urlopen
    info = data_root / "instance.json"
    try:
        meta = json.loads(info.read_text(encoding="utf-8"))
        req = Request(f"http://127.0.0.1:{int(meta['port'])}/api/show", data=b"{}", method="POST",
                      headers={"X-Token": meta["token"], "Content-Type": "application/json"})
        with urlopen(req, timeout=3) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001 - no running instance
        return False


def remember_instance(data_root, server, app):
    import json
    (data_root / "instance.json").write_text(json.dumps({"port": server.server_address[1], "token": app.token, "pid": os.getpid()}),
                                             encoding="utf-8")


def windows_mutex():
    """Named mutex so the installer can ask to close a running program before updating it."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return ctypes.windll.kernel32.CreateMutexW(None, False, "GamaCIPMakerRunning")
    except Exception:  # noqa: BLE001
        return None


def start_tray(app, window, state):
    """Icon next to the clock, so hot folders keep working while the window is closed."""
    try:
        import pystray
        from PIL import Image
        from gama_cip_desktop.server import web_folder
    except Exception:  # noqa: BLE001
        LOG.exception("Ikonica pored sata nije dostupna")
        return None

    def show(*_):
        window.show()
        window.restore()

    def open_root(*_):
        root = app.hot.root
        if root and root.is_dir():
            os.startfile(str(root)) if sys.platform == "win32" else webbrowser.open(root.as_uri())  # noqa: S606

    def quit_app(*_):
        state["quitting"] = True
        icon.stop()
        window.destroy()

    menu = pystray.Menu(
        pystray.MenuItem("Otvori Gama CIP Maker", show, default=True),
        pystray.MenuItem("Otvori hot foldere", open_root, enabled=lambda _: bool(app.hot.root)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Izlaz (zaustavlja hot foldere)", quit_app),
    )
    icon = pystray.Icon("GamaCIPMaker", Image.open(web_folder() / "icon.png"), "Gama CIP Maker", menu)
    try:
        icon.run_detached()
    except Exception:  # noqa: BLE001
        LOG.exception("Ikonica pored sata nije pokrenuta")
        return None
    LOG.info("Ikonica pored sata pokrenuta")
    return icon


def main():
    bundled_ghostscript()
    if "--selftest" in sys.argv:
        from gama_cip_desktop.selftest import main as selftest
        target = sys.argv[sys.argv.index("--selftest") + 1] if len(sys.argv) > sys.argv.index("--selftest") + 1 else "selftest.txt"
        sys.exit(selftest(target))
    if "--hottest" in sys.argv:
        from gama_cip_desktop.selftest import hot_test
        i = sys.argv.index("--hottest")
        setup_logging()
        sys.exit(hot_test(sys.argv[i + 1], sys.argv[i + 2]))
    log_path, _ = setup_logging()
    from gama_cip_desktop import __version__
    from gama_cip_desktop.store import data_dir
    LOG.info("Pokretanje Gama CIP Maker %s, Python %s, %s", __version__, sys.version.split()[0], sys.platform)
    testing = "--guitest" in sys.argv
    if not testing and other_instance(data_dir()):
        LOG.info("Program već radi; prikazan je postojeći prozor")
        return
    mutex = windows_mutex()  # noqa: F841 - held for the lifetime of the process
    from gama_cip_desktop.server import start

    app, server, url = start()
    LOG.info("Lokalni server: %s", url.split("?")[0])
    if not testing:
        remember_instance(app.root, server, app)
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
        LOG.exception("pywebview nije dostupan, otvaram pregledač")
        webbrowser.open(url)
        server.serve_forever()
        return
    bridge = Bridge(app)
    hidden = "--minimized" in sys.argv and app.hot.enabled
    window = webview.create_window("Gama United · CIP Maker", url, js_api=bridge, width=1360, height=880,
                                   min_size=(1024, 680), hidden=hidden)
    bridge._window = window
    state = {"quitting": False, "told": False}
    icon = None

    def on_closing():
        # Runs on the GUI thread: decide quickly, never block here.
        if state["quitting"] or icon is None or not app.hot.enabled:
            return True
        threading.Thread(target=window.hide, daemon=True, name="hide").start()
        if not state["told"]:
            state["told"] = True
            try:
                icon.notify("Hot folderi i dalje rade. Program je pored sata; desni klik → Izlaz za gašenje.", "Gama CIP Maker")
            except Exception:  # noqa: BLE001
                pass
        return False

    def bring_to_front():
        window.show()
        window.restore()

    app.on_show = bring_to_front
    window.events.closing += on_closing
    if testing:
        gui_test(window, sys.argv[sys.argv.index("--guitest") + 1])
    else:
        icon = start_tray(app, window, state)
    try:
        webview.start(private_mode=False, storage_path=str(app.root / "webview"))
    except Exception:
        LOG.exception("Prozor programa nije mogao da se otvori; otvaram pregledač")
        webbrowser.open(url)
        server.serve_forever()
    finally:
        LOG.info("Zatvaranje programa")
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                pass
        if not testing:
            (app.root / "instance.json").unlink(missing_ok=True)
        app.hot.stop.set()
        server.shutdown()


if __name__ == "__main__":
    main()
