import json
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

from PyQt6.QtCore import qDebug, Qt
from PyQt6.QtWidgets import QApplication, QMessageBox, QProgressDialog

_PLUGIN_USER_AGENT = "ModOrganizer2 BG3 Plugin"
_HTTP_TIMEOUT = 30


def _http_get(url, timeout=_HTTP_TIMEOUT):
    request = urllib.request.Request(url, headers={"User-Agent": _PLUGIN_USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _download_to(url, dest_path: Path, progress=None, timeout=_HTTP_TIMEOUT):
    request = urllib.request.Request(url, headers={"User-Agent": _PLUGIN_USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        total = int(response.headers.get("Content-Length") or 0)
        read = 0
        with dest_path.open("wb") as f:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                read += len(chunk)
                if progress is not None:
                    if total > 0:
                        progress.setValue(min(int(read * 100 / total), 100))
                    QApplication.processEvents()
                    if progress.wasCanceled():
                        raise RuntimeError("Download cancelled.")
    return dest_path


def check_bg3_paths(organizer):
    base_dir = Path(__file__).resolve().parent
    temp_dir = base_dir / "temp_extracted"
    tools_dir = base_dir / "tools"
    divine_exe = tools_dir / "Divine.exe"

    if tools_dir.exists() and divine_exe.exists():
        return True

    main_window = organizer.mainWindow() if hasattr(organizer, "mainWindow") else None
    msg_box = QMessageBox(main_window)
    msg_box.setWindowTitle("Baldur's Gate 3 Plugin - Missing dependencies")
    msg_box.setText("LSLib Tools are missing.\nThese are necessary for the plugin to work correctly.")
    msg_box.addButton("Download", QMessageBox.ButtonRole.DestructiveRole)
    exit_btn = msg_box.addButton("Exit", QMessageBox.ButtonRole.ActionRole)
    msg_box.setIcon(QMessageBox.Icon.Warning)
    msg_box.exec()

    if msg_box.clickedButton() == exit_btn:
        qDebug("BG3: LSLib download declined; load order features stay unavailable.")
        return False

    progress = QProgressDialog("Downloading LSLib...", "Cancel", 0, 100, main_window)
    progress.setWindowTitle("BG3 Plugin - Downloading")
    progress.setWindowModality(Qt.WindowModality.ApplicationModal)
    progress.setMinimumDuration(0)
    progress.setValue(0)
    QApplication.processEvents()

    zip_path = None
    try:
        release_url = "https://api.github.com/repos/Norbyte/lslib/releases/latest"
        release_data = json.loads(_http_get(release_url).decode("utf-8"))
        asset = next(
            (
                a for a in release_data.get("assets", [])
                if a.get("name", "").lower().endswith(".zip")
                and "exporttool" in a.get("name", "").lower()
            ),
            None,
        )
        if not asset:
            raise RuntimeError("Could not find an ExportTool zip in the latest LSLib release.")

        zip_path = Path(tempfile.gettempdir()) / asset["name"]
        _download_to(asset["browser_download_url"], zip_path, progress)

        tools_dir.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for archive_info in zip_ref.infolist():
                if archive_info.is_dir():
                    continue
                file_name = Path(archive_info.filename).name
                if not file_name:
                    continue
                extracted_path = Path(zip_ref.extract(archive_info, temp_dir))
                shutil.copy2(extracted_path, tools_dir / file_name)

        progress.setValue(100)

    except Exception as e:
        qDebug(f"BG3: LSLib download failed: {e}")
        progress.close()
        err = QMessageBox(main_window)
        err.setIcon(QMessageBox.Icon.Critical)
        err.setText(f"Failed to download LSLib tools:\n{str(e)}")
        err.exec()
        return False
    finally:
        progress.close()
        for leftover in (temp_dir, zip_path):
            if leftover is None or not leftover.exists():
                continue
            try:
                shutil.rmtree(leftover) if leftover.is_dir() else leftover.unlink()
            except OSError as cleanup_err:
                qDebug(f"BG3: failed to clean up {leftover}: {cleanup_err}")

    return True
