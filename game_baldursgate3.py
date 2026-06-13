from abc import ABC
import configparser
from enum import IntEnum, auto
from functools import cached_property
import os
import json
from pathlib import Path
import shutil
import subprocess
import zipfile
import tempfile
import urllib.request
import re

import mobase # type: ignore
from PyQt6.QtCore import QDir, QFileInfo, QDirIterator, QFile, qDebug, QCoreApplication, Qt
from PyQt6.QtWidgets import QMessageBox, QMainWindow, QApplication, QPushButton, QProgressDialog
from PyQt6.QtCore import QCoreApplication

from ..basic_features import (
    BasicGameSaveGameInfo,
    BasicLocalSavegames,
    BasicModDataChecker,
    GlobPatterns,
)
# from ..basic_features.basic_save_game_info import BasicGameSaveGame
from ..basic_features.utils import is_directory
from ..basic_game import BasicGame

from .baldursgate3 import modSettings

_ICON_DIR = Path(__file__).resolve().parent / "baldursgate3" / "icons"


def _bg3_icon(name):
    return str(_ICON_DIR / name)

class BG3Content(IntEnum):
    SCRIPT_EXTENDER_MOD = auto()
    OSIRIS_SCRIPT = auto()
    OVERRIDE = auto()
    OVERRIDE_LOAD_ORDER = auto()
    NATIVE_DLL = auto()
    LOOSE_FILES = auto()

class BG3DataContent(mobase.ModDataContent):
    _contents = [
        (BG3Content.SCRIPT_EXTENDER_MOD, "Script Extender Mod", _bg3_icon("script_extender.svg")),
        (BG3Content.OSIRIS_SCRIPT, "Osiris Script", _bg3_icon("osiris.svg")),
        (BG3Content.OVERRIDE, "Override Mod", _bg3_icon("override.svg")),
        (BG3Content.OVERRIDE_LOAD_ORDER, "Override + Load Order Mod", _bg3_icon("override_load_order.svg")),
        (BG3Content.NATIVE_DLL, "Native DLL Mod", _bg3_icon("native_dll.svg")),
        (BG3Content.LOOSE_FILES, "Loose File Override", _bg3_icon("loose_files.svg")),
    ]

    def __init__(self, organizer: mobase.IOrganizer):
        super().__init__()
        self._organizer = organizer

    def getAllContents(self) -> list[mobase.ModDataContent.Content]:
        return [
            mobase.ModDataContent.Content(content_id, name, icon)
            for content_id, name, icon in self._contents
        ]

    def getContentsFor(self, filetree: mobase.IFileTree) -> list[int]:
        contents: set[int] = set()
        self._scan_contents(filetree, contents)
        self._scan_cached_pak_metadata(filetree, contents)
        return list(contents)

    def _scan_cached_pak_metadata(self, filetree: mobase.IFileTree, contents: set[int]):
        mod_root = self._mod_root_from_tree(filetree)
        if not mod_root:
            return

        meta_ini = mod_root / "meta.ini"
        if not meta_ini.exists():
            return

        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        config.read(meta_ini, encoding="utf-8")

        for section in config.sections():
            if not section.casefold().endswith(".pak"):
                continue
            if config[section].getboolean("HasOsiris", fallback=False):
                contents.add(BG3Content.OSIRIS_SCRIPT)
            if config[section].getboolean("HasScriptExtender", fallback=False):
                contents.add(BG3Content.SCRIPT_EXTENDER_MOD)
            is_override = config[section].getboolean("Override", fallback=False)
            has_load_order = config[section].getboolean("LoadOrder", fallback=False)
            if is_override and has_load_order:
                contents.add(BG3Content.OVERRIDE_LOAD_ORDER)
            elif is_override:
                contents.add(BG3Content.OVERRIDE)

    def _mod_root_from_tree(self, filetree: mobase.IFileTree) -> Path | None:
        if not hasattr(filetree, "name"):
            return None

        mod_name = filetree.name()
        if not mod_name:
            return None

        modlist = self._organizer.modList()
        try:
            mod = modlist.getMod(mod_name)
            if mod:
                return Path(mod.absolutePath())
        except Exception:
            pass

        for candidate in modlist.allMods():
            try:
                mod = modlist.getMod(candidate)
                if mod and mod.name() == mod_name:
                    return Path(mod.absolutePath())
            except Exception:
                continue
        return None

    def _scan_contents(self, filetree: mobase.IFileTree, contents: set[int], prefix=""):
        for entry in filetree:
            name = entry.name()
            lower_name = name.casefold()
            rel_path = f"{prefix}/{name}" if prefix else name
            lower_path = rel_path.replace("\\", "/").casefold()
            if isinstance(entry, mobase.IFileTree):
                if lower_name in {"se_config", "script extender"}:
                    contents.add(BG3Content.SCRIPT_EXTENDER_MOD)
                elif lower_name == "scriptextender" or lower_path.endswith("/scriptextender"):
                    contents.add(BG3Content.SCRIPT_EXTENDER_MOD)
                elif "/story/rawfiles/goals" in lower_path:
                    contents.add(BG3Content.OSIRIS_SCRIPT)
                elif name in {"Root", "bin"}:
                    contents.add(BG3Content.NATIVE_DLL)
                elif name in {"Generated", "Public", "Localization", "Data"}:
                    contents.add(BG3Content.LOOSE_FILES)
                self._scan_contents(entry, contents, rel_path)
            elif lower_name.endswith(".dll"):
                contents.add(BG3Content.NATIVE_DLL)
            elif lower_name.endswith(".json"):
                if (
                    "/script extender/" in lower_path
                    or "/scriptextender/" in lower_path
                    or lower_name == "scriptextendersettings.json"
                ):
                    contents.add(BG3Content.SCRIPT_EXTENDER_MOD)
            elif lower_name.endswith((".osi", ".txt")) and "/story/rawfiles/goals/" in lower_path:
                contents.add(BG3Content.OSIRIS_SCRIPT)
            elif lower_name.endswith(".lua") or lower_name in {"bootstrapserver.lua", "bootstrapclient.lua"}:
                contents.add(BG3Content.SCRIPT_EXTENDER_MOD)

class BG3ModDataChecker(BasicModDataChecker):
    def __init__(self):
        super().__init__(
            GlobPatterns(
                valid=[
                    "PAK_FILES",
                    "SE_CONFIG",
                    "Generated",
                    "Root",
                    "Localization",
                    "Generated",
                    "Public",
                    "Mods",
                ],
                delete=[
                    "info.json",
                    "README",
                    "*.Ink",
                    "*.url",
                ],
                move={
                    # PAK Mods
                    "*.pak": "PAK_FILES/",
                    # Script Extender Config Mods
                    "*.json": "SE_CONFIG/",
                    "BG3MCM": "SE_CONFIG/",
                    # DLL Mods
                    "*.dll": "Root/bin/",
                }
            )
        )

    _extra_move_patterns = {
        "*.dll": "Root/bin/",
    }

    def dataLooksValid(
        self, filetree: mobase.IFileTree
    ) -> mobase.ModDataChecker.CheckReturn:
        parent = filetree.parent()
        if parent is not None and self.dataLooksValid(parent) is self.FIXABLE:
            return self.FIXABLE

        status = mobase.ModDataChecker.INVALID

        if any(filetree.exists(p) for p in self._extra_move_patterns):
            return mobase.ModDataChecker.FIXABLE
        rp = self._regex_patterns
        for entry in filetree:
            name = entry.name().casefold()
            if rp.move_match(name) is not None:
                status = mobase.ModDataChecker.FIXABLE
            elif rp.valid.match(name):
                if status is mobase.ModDataChecker.INVALID:
                    status = mobase.ModDataChecker.VALID
        return status

class BG3Game(BasicGame, mobase.IPluginFileMapper):
    Name = "Baldur's Gate 3 Unofficial Support Plugin"
    Author = "Alvadus"
    Version = "1.1.1"

    GameName = "Baldur's Gate 3"
    GameShortName = "baldursgate3"
    GameNexusName = "baldursgate3"
    GameValidShortNames = ["baldursgate3"]

    GameBinary = r"bin\bg3.exe"
    GameDataPath = r"Data"
    GameSavesDirectory = "%USERPROFILE%/AppData/Local/Larian Studios/Baldur's Gate 3/PlayerProfiles/Public/Savegames/Story"
    GameDocumentsDirectory = "%USERPROFILE%/AppData/Local/Larian Studios/Baldur's Gate 3/PlayerProfiles/Public/"
    GameSaveExtension = "lsv"

    GameNexusId = 3474
    GameSteamId = 1086940
    GameGogId = 1456460669

    _mods_paths = {
        "PAK_FILES": {
            "pattern": "*.pak",
            "pathName": "Mods"
        },
        "SE_CONFIG": {
            "pattern": "*",
            "pathName": "Script Extender"
        },
        "LevelCache": {
            "pattern": "*",
            "pathName": "LevelCache"
        },
        "Root": {
            "pattern": "*",
            "pathName": "",
            "absolute": True,
        }
    }

    def __init__(self):
        BasicGame.__init__(self)
        mobase.IPluginFileMapper.__init__(self)

    def init(self, organizer: mobase.IOrganizer) -> bool:
        super().init(organizer)
        self._register_feature(BG3ModDataChecker())
        self._register_feature(BG3DataContent(organizer))
        self._register_feature(BasicGameSaveGameInfo(
            lambda s: s.with_suffix(".webp")
        ))
        self._register_feature(
            BasicLocalSavegames(self.savesDirectory())
        )

        self._organizer.onAboutToRun(self.onAboutToRun) # on Executable Start
        self._organizer.onFinishedRun(self.onFinishedRun)  # on Executable Stop
        self._organizer.modList().onModInstalled(self.onModInstalled)  # on Mod Installed
        self._organizer.modList().onModRemoved(self.onModRemoved)  # on Mod Removed

        # self._organizer.onNextRefresh(self.onRefresh)

        self._organizer.onUserInterfaceInitialized(self.onUserInterfaceLoad) # on Mod Organizer 2 Load
        self._organizer.onProfileCreated(self.onProfileCreated) # on Profile Created

        return True
    
    def onRefresh(self):
        hasDependencies = check_bg3_paths(self._organizer)

        if hasDependencies is False:
            return True
        
        return True

    def onAboutToRun(self, executable: str):
        modSettings.generate_mod_settings(self._organizer, self._organizer.modList(), self._organizer.profile())
        return True

    def onFinishedRun(self, executable: str, exit_code: int, error: str = ""):
        # Handle Script Extender files
        appdata_path = Path(os.getenv("LOCALAPPDATA")) / "Larian Studios" / "Baldur's Gate 3"
        
        # Handle Script Extender configs
        se_path = appdata_path / "Script Extender"
        if se_path.exists() and any(se_path.iterdir()):
            overwrite_path = Path(self._organizer.overwritePath()) / "SE_CONFIG"
            overwrite_path.mkdir(parents=True, exist_ok=True)

            for file in se_path.glob("**/*"):
                if file.is_file():
                    rel_path = file.relative_to(se_path)
                    dest_file = overwrite_path / rel_path
                    
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    
                    try:
                        dest_file.write_bytes(file.read_bytes())
                        file.unlink()
                        
                        parent = file.parent
                        while parent != se_path:
                            try:
                                parent.rmdir()
                                parent = parent.parent
                            except OSError:
                                break
                                
                    except Exception as e:
                        qDebug(f"Failed to move {file} to overwrite: {str(e)}")

        # Handle LevelCache files
        levelcache_path = appdata_path / "LevelCache"
        if levelcache_path.exists() and any(levelcache_path.iterdir()):
            overwrite_path = Path(self._organizer.overwritePath()) / "LevelCache"
            overwrite_path.mkdir(parents=True, exist_ok=True)

            for file in levelcache_path.glob("**/*"):
                if file.is_file():
                    rel_path = file.relative_to(levelcache_path)
                    dest_file = overwrite_path / rel_path
                    
                    if not dest_file.exists():
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            # Copy file to overwrite but don't delete the original
                            dest_file.write_bytes(file.read_bytes())
                        except Exception as e:
                            qDebug(f"Failed to copy {file} to overwrite: {str(e)}")

        return True
    
    def onModInstalled(self, mod: str):
        qDebug("BUCK U")
        modSettings.mod_installed(self._organizer, self._organizer.modList(), self._organizer.profile(), mod)
        return True

    def onModRemoved(self, mod):
        modSettings.mod_removed(self._organizer, self._organizer.profile(), mod)
        return True

    def onUserInterfaceLoad(self, window):
        hasDependencies = check_bg3_paths(self._organizer)

        if hasDependencies is False:
            return True
        
        return True

    def onProfileCreated(self, profile: mobase.IProfile):
        return True

    def iniFiles(self):
        return ["modsettings.lsx"]

    def executables(self) -> list[mobase.ExecutableInfo]:
        return [
            mobase.ExecutableInfo(
                f"{self.gameName()}: DX11",
                self.gameDirectory().absoluteFilePath(r"bin\bg3_dx11.exe"),
            ),
            mobase.ExecutableInfo(
                f"{self.gameName()}: Vulkan",
                self.gameDirectory().absoluteFilePath(self.binaryName()),
            ),
            mobase.ExecutableInfo(
                "Larian Launcher",
                self.gameDirectory().absoluteFilePath(self.getLauncherName()),
            ),
        ]

    @cached_property
    def _base_dlls(self) -> set[str]:
        base_bin = Path(self.gameDirectory().absoluteFilePath("bin"))
        return {str(f.relative_to(base_bin)).casefold() for f in base_bin.glob("*.dll")}

    def executableForcedLoads(self) -> list[mobase.ExecutableForcedLoadSetting]:
        try:
            forced_loads = super().executableForcedLoads()
        except AttributeError:
            forced_loads = []

        libs: set[str] = set()

        tree = self._organizer.virtualFileTree().find("bin")
        if isinstance(tree, mobase.IFileTree):
            def find_dlls(_, entry: mobase.FileTreeEntry) -> mobase.IFileTree.WalkReturn:
                relpath = entry.pathFrom(tree)
                if relpath and entry.hasSuffix("dll") and relpath.casefold() not in self._base_dlls:
                    libs.add(relpath)
                return mobase.IFileTree.WalkReturn.CONTINUE

            tree.walk(find_dlls)

        for root_bin in self._active_root_bin_paths():
            for dll in root_bin.rglob("*.dll"):
                relpath = str(dll.relative_to(root_bin))
                if relpath.casefold() not in self._base_dlls:
                    libs.add(relpath)

        qDebug(f"BG3 DLLs to force load: {libs}")
        return forced_loads + [
            mobase.ExecutableForcedLoadSetting(exe.binary().fileName(), lib).withEnabled(True)
            for lib in libs
            for exe in self.executables()
        ]

    def mappings(self) -> list[mobase.Mapping]:
        map = []
        modlist = self._organizer.modList()

        appdata_path = QDir(os.getenv("LOCALAPPDATA") + "/Larian Studios/Baldur's Gate 3/")

        required_dirs = ["Script Extender", "Mods", "LevelCache"]
        for dir_name in required_dirs:
            dir_path = appdata_path.absoluteFilePath(dir_name)
            if not QDir(dir_path).exists():
                if not appdata_path.mkdir(dir_name):
                    qDebug(f"Failed to create directory: {dir_path}")

        # Handle regular mods
        for mod_type, mod_map_data in self._mods_paths.items():
            mod_pattern = mod_map_data["pattern"]
            mod_destpath = mod_map_data["pathName"]
            if mod_map_data.get("absolute"):
                mod_destpath = self.gameDirectory().absoluteFilePath(mod_destpath)
            else:
                mod_destpath = appdata_path.absoluteFilePath(mod_destpath)

            # Handle mods from mod directory
            for modName in self._get_mods_from_type(mod_type):
                mod_path = Path(modlist.getMod(modName).absolutePath()) / mod_type
                mod_files = list(mod_path.glob(mod_pattern))

                for file in mod_files:
                    map.append(mobase.Mapping(
                        source=str(file),
                        destination=os.path.join(mod_destpath, str(file.name)),
                        is_directory=file.is_dir(),
                        create_target=True,
                    ))

            # Handle files from overwrite directory
            overwrite_path = Path(self._organizer.overwritePath()) / mod_type
            if overwrite_path.exists():
                overwrite_files = list(overwrite_path.glob(mod_pattern))
                for file in overwrite_files:
                    map.append(mobase.Mapping(
                        source=str(file),
                        destination=os.path.join(mod_destpath, str(file.name)),
                        is_directory=file.is_dir(),
                        create_target=True,
                    ))

        map.append(mobase.Mapping(
            source=self._organizer.profile().absolutePath() + "/modsettings.lsx",
            destination=os.path.join(appdata_path.absoluteFilePath("PlayerProfiles/Public"), "modsettings.lsx"),
            is_directory=False,
            create_target=True,
        ))

        return map

    def _get_mods_from_type(self, mod_type: str):
        all_mods = self._organizer.modList().allModsByProfilePriority()

        mods: list[str] = []
        for modName in all_mods:
            if self._organizer.modList().state(modName) & mobase.ModState.ACTIVE != 0:
                mod = self._organizer.modList().getMod(modName)
                if Path(mod.absolutePath(), mod_type).exists():
                    mods.append(modName)
        return mods

    def _active_root_bin_paths(self) -> list[Path]:
        modlist = self._organizer.modList()
        paths = []
        for modName in modlist.allModsByProfilePriority():
            if modlist.state(modName) & mobase.ModState.ACTIVE == 0:
                continue
            root_bin = Path(modlist.getMod(modName).absolutePath()) / "Root" / "bin"
            if root_bin.exists():
                paths.append(root_bin)

        overwrite_root_bin = Path(self._organizer.overwritePath()) / "Root" / "bin"
        if overwrite_root_bin.exists():
            paths.append(overwrite_root_bin)
        return paths

def check_bg3_paths(organizer):
    base_dir = Path(__file__).parent / "baldursgate3"
    temp_dir = base_dir / "temp_extracted"
    tools_dir = base_dir / "tools"
    divine_exe = tools_dir / "Divine.exe"

    if tools_dir.exists() and divine_exe.exists():
        return True

    main_window = organizer.mainWindow() if hasattr(organizer, "mainWindow") else None
    msg_box = QMessageBox(main_window)
    msg_box.setWindowTitle("Baldur's Gate 3 Plugin - Missing dependencies")
    msg_box.setText("LSLib Tools are missing.\nThese are necessary for the plugin to work correctly.")
    download_btn = msg_box.addButton("Download", QMessageBox.ButtonRole.DestructiveRole)
    exit_btn = msg_box.addButton("Exit", QMessageBox.ButtonRole.ActionRole)
    msg_box.setIcon(QMessageBox.Icon.Warning)
    msg_box.exec()

    if msg_box.clickedButton() == exit_btn:
        subprocess.Popen(["taskkill", "/im", "ModOrganizer.exe", "/f"], creationflags=subprocess.CREATE_NO_WINDOW)
        return False

    progress = QProgressDialog("Downloading LSLib...", "Cancel", 0, 100, main_window)
    progress.setWindowTitle("BG3 Plugin - Downloading")
    progress.setWindowModality(Qt.WindowModality.ApplicationModal)
    progress.show()

    try:
        release_url = "https://api.github.com/repos/Norbyte/lslib/releases/latest"
        with urllib.request.urlopen(release_url) as response:
            release_data = json.loads(response.read().decode("utf-8"))
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

        zip_url = asset["browser_download_url"]
        zip_filename = asset["name"]
        zip_path = Path(tempfile.gettempdir()) / zip_filename

        def reporthook(block_num, block_size, total_size):
            if total_size > 0:
                read_so_far = block_num * block_size
                percent = int(read_so_far * 100 / total_size)
                progress.setValue(min(percent, 100))
                QApplication.processEvents()

        urllib.request.urlretrieve(zip_url, str(zip_path), reporthook)

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

        try:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
        except Exception as cleanup_err:
            qDebug(f"Failed to clean up temp_extracted: {cleanup_err}")

    except Exception as e:
        qDebug(f"Download failed: {e}")
        err = QMessageBox(main_window)
        err.setIcon(QMessageBox.Icon.Critical)
        err.setText(f"Failed to download LSLib tools:\n{str(e)}")
        err.exec()
        return False

    return True
