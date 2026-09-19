from enum import IntEnum, auto
from functools import cached_property
import os
from pathlib import Path
import shutil

import mobase # type: ignore
from PyQt6.QtCore import QDir, qDebug, Qt
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QRadioButton,
    QScrollArea,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..basic_features import (
    BasicGameSaveGameInfo,
    BasicLocalSavegames,
    BasicModDataChecker,
    GlobPatterns,
)
# from ..basic_features.basic_save_game_info import BasicGameSaveGame
from ..basic_game import BasicGame

from .baldursgate3 import modSettings
from .baldursgate3.quick_options import BG3QuickOptions
from .baldursgate3.lslib_setup import check_bg3_paths

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
    LOOSE_MODULE = auto()

class BG3DataContent(mobase.ModDataContent):
    _contents = [
        (BG3Content.SCRIPT_EXTENDER_MOD, "Script Extender Mod", _bg3_icon("script_extender.svg")),
        (BG3Content.OSIRIS_SCRIPT, "Osiris Script", _bg3_icon("osiris.svg")),
        (BG3Content.OVERRIDE, "Override Mod", _bg3_icon("override.svg")),
        (BG3Content.OVERRIDE_LOAD_ORDER, "Override + Load Order Mod", _bg3_icon("override_load_order.svg")),
        (BG3Content.NATIVE_DLL, "Native DLL Mod", _bg3_icon("native_dll.svg")),
        (BG3Content.LOOSE_FILES, "Loose File Override", _bg3_icon("loose_files.svg")),
        (BG3Content.LOOSE_MODULE, "Loose Mod", _bg3_icon("loose_module.svg")),
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
        try:
            self._scan_contents(filetree, contents)
            self._scan_loose_data(filetree, contents)
            self._scan_cached_pak_metadata(filetree, contents)
        except Exception as e:
            qDebug(f"BG3: content scan failed: {e}")
        return list(contents)

    def _scan_cached_pak_metadata(self, filetree: mobase.IFileTree, contents: set[int]):
        mod_root = self._mod_root_from_tree(filetree)
        if not mod_root:
            return

        meta_ini = mod_root / "meta.ini"
        if not meta_ini.exists():
            return

        config = modSettings.read_meta_ini(meta_ini)
        if config is None:
            return

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
                elif lower_name == "bin":
                    contents.add(BG3Content.NATIVE_DLL)
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

    def _scan_loose_data(self, filetree: mobase.IFileTree, contents: set[int]):
        for data_root in self._loose_data_roots(filetree):
            owned = self._loose_module_folders(data_root)
            if owned:
                contents.add(BG3Content.LOOSE_MODULE)
            for entry in data_root:
                if not isinstance(entry, mobase.IFileTree):
                    continue
                name = entry.name().casefold()
                if name == "localization":
                    contents.add(BG3Content.LOOSE_FILES)
                elif name in ("mods", "public") and self._touches_other_folders(entry, owned):
                    contents.add(BG3Content.LOOSE_FILES)
                elif name == "generated" and self._generated_touches_other_folders(entry, owned):
                    contents.add(BG3Content.LOOSE_FILES)

    def _loose_data_roots(self, filetree: mobase.IFileTree):
        yield filetree
        root = self._child_dir(filetree, "root")
        data = self._child_dir(root, "data") if root is not None else None
        if data is not None:
            yield data

    def _child_dir(self, filetree: mobase.IFileTree, name: str):
        for entry in filetree:
            if isinstance(entry, mobase.IFileTree) and entry.name().casefold() == name:
                return entry
        return None

    def _loose_module_folders(self, data_root: mobase.IFileTree) -> set[str]:
        mods = self._child_dir(data_root, "mods")
        if mods is None:
            return set()
        return {
            entry.name().casefold()
            for entry in mods
            if isinstance(entry, mobase.IFileTree)
            and not modSettings.is_builtin_module_folder(entry.name())
            and any(
                not isinstance(child, mobase.IFileTree) and child.name().casefold() == "meta.lsx"
                for child in entry
            )
        }

    def _touches_other_folders(self, folder: mobase.IFileTree, owned: set[str]) -> bool:
        return any(
            not isinstance(entry, mobase.IFileTree) or entry.name().casefold() not in owned
            for entry in folder
        )

    def _generated_touches_other_folders(self, generated: mobase.IFileTree, owned: set[str]) -> bool:
        for entry in generated:
            if not isinstance(entry, mobase.IFileTree) or entry.name().casefold() != "public":
                return True
            if self._touches_other_folders(entry, owned):
                return True
        return False

class BG3VariantDialog(QDialog):

    INSTALL_ALL = ""

    @classmethod
    def choose(cls, groups, parent=None):
        dialog = cls(groups, parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return cls.INSTALL_ALL if dialog._install_all else dialog.selected()

    def __init__(self, groups, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Baldur's Gate 3 - choose a variant")
        self._buttons = {}
        self._install_all = False

        layout = QVBoxLayout(self)
        heading = QLabel(
            f"This archive contains {len(groups)} builds of the same mod in separate "
            "folders.\nThey normally share one UUID, so only one should be installed."
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        holder = QWidget()
        holder_layout = QVBoxLayout(holder)
        group = QButtonGroup(self)
        for index, path in enumerate(sorted(groups)):
            pak = groups[path][0]
            folder = path.rsplit("/", 1)[-1]
            label = folder or pak.name()
            detail = pak.name()
            if folder and folder.casefold() in detail.casefold():
                detail = ""
            elif len(detail) > 58:
                detail = detail[:57] + "…"
            button = QRadioButton(f"{label}\n{detail}" if detail else label, holder)
            button.setChecked(index == 0)
            group.addButton(button)
            holder_layout.addWidget(button)
            self._buttons[button] = path
        holder_layout.addStretch(1)

        area = QScrollArea()
        area.setWidget(holder)
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setMaximumHeight(300)
        layout.addWidget(area)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        install_all = buttons.addButton(
            "Install all", QDialogButtonBox.ButtonRole.ActionRole
        )
        install_all.setToolTip(
            "Install every variant. Only useful if these are not actually variants."
        )
        install_all.clicked.connect(self._accept_all)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.adjustSize()

    def _accept_all(self):
        self._install_all = True
        self.accept()

    def selected(self):
        for button, path in self._buttons.items():
            if button.isChecked():
                return path
        return None


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
                    "bin": "Root/",
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
        "bin": "Root/",
    }

    def dataLooksValid(
        self, filetree: mobase.IFileTree
    ) -> mobase.ModDataChecker.CheckReturn:
        try:
            return self._data_looks_valid(filetree)
        except Exception as e:
            qDebug(f"BG3: layout check failed, treating as invalid: {e}")
            return mobase.ModDataChecker.INVALID

    def _data_looks_valid(
        self, filetree: mobase.IFileTree
    ) -> mobase.ModDataChecker.CheckReturn:
        parent = filetree.parent()
        if parent is not None and self.dataLooksValid(parent) is self.FIXABLE:
            return self.FIXABLE

        status = mobase.ModDataChecker.INVALID

        if any(filetree.exists(p) for p in self._extra_move_patterns):
            return mobase.ModDataChecker.FIXABLE
        if self._contains_unsorted_pak(filetree) or self._contains_unsorted_bin(filetree):
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

    def fix(self, filetree: mobase.IFileTree) -> mobase.IFileTree:
        try:
            filetree = super().fix(filetree)
            self._move_unsorted_bins(filetree, filetree)
            self._move_unsorted_paks(filetree, filetree)
            self._delete_junk(filetree)
            self._remove_empty_dirs(filetree)
        except Exception as e:
            qDebug(f"BG3: fixing the mod layout failed part way through: {e}")
        return filetree

    def _delete_junk(self, filetree: mobase.IFileTree):
        rp = self._regex_patterns
        for entry in list(filetree):
            if rp.delete.match(entry.name()):
                entry.detach()
            elif isinstance(entry, mobase.IFileTree):
                self._delete_junk(entry)

    def _variant_pak_groups(self, filetree: mobase.IFileTree):
        groups = {}

        def walk(node, path):
            for entry in node:
                if isinstance(entry, mobase.IFileTree):
                    walk(entry, f"{path}/{entry.name()}" if path else entry.name())
                elif entry.name().casefold().endswith(".pak"):
                    groups.setdefault(path, []).append(entry)

        walk(filetree, "")
        if len(groups) < 2 or any(len(paks) != 1 for paks in groups.values()):
            return {}
        if any(not path for path in groups):
            return {}
        parents = {path.rsplit("/", 1)[0] if "/" in path else "" for path in groups}
        if len(parents) != 1:
            return {}
        return groups

    def keep_only_variant(self, groups, chosen):
        for path, paks in groups.items():
            if path == chosen:
                continue
            for pak in paks:
                pak.detach()

    def _contains_unsorted_pak(self, filetree: mobase.IFileTree, inside_pak_files=False):
        current_inside_pak_files = inside_pak_files or filetree.name().casefold() == "pak_files"
        for entry in filetree:
            if isinstance(entry, mobase.IFileTree):
                if self._contains_unsorted_pak(entry, current_inside_pak_files):
                    return True
            elif entry.name().casefold().endswith(".pak") and not current_inside_pak_files:
                return True
        return False

    def _contains_unsorted_bin(self, filetree: mobase.IFileTree, inside_root=False):
        current_inside_root = inside_root or filetree.name().casefold() == "root"
        for entry in filetree:
            if isinstance(entry, mobase.IFileTree):
                name = entry.name().casefold()
                if name == "bin" and not current_inside_root:
                    return True
                if self._contains_unsorted_bin(entry, current_inside_root):
                    return True
        return False

    def _move_unsorted_paks(self, root: mobase.IFileTree, filetree: mobase.IFileTree, inside_pak_files=False):
        current_inside_pak_files = inside_pak_files or filetree.name().casefold() == "pak_files"
        for entry in list(filetree):
            if isinstance(entry, mobase.IFileTree):
                self._move_unsorted_paks(root, entry, current_inside_pak_files)
            elif entry.name().casefold().endswith(".pak") and not current_inside_pak_files:
                root.move(entry, "PAK_FILES/", mobase.IFileTree.InsertPolicy.MERGE)

    def _move_unsorted_bins(self, root: mobase.IFileTree, filetree: mobase.IFileTree, inside_root=False):
        current_inside_root = inside_root or filetree.name().casefold() == "root"
        for entry in list(filetree):
            if not isinstance(entry, mobase.IFileTree):
                continue
            name = entry.name().casefold()
            if name == "bin" and not current_inside_root:
                root.move(entry, "Root/", mobase.IFileTree.InsertPolicy.MERGE)
            else:
                self._move_unsorted_bins(root, entry, current_inside_root)

    def _remove_empty_dirs(self, filetree: mobase.IFileTree):
        for entry in list(filetree):
            if not isinstance(entry, mobase.IFileTree):
                continue
            self._remove_empty_dirs(entry)
            if len(entry) == 0:
                entry.detach()

class BG3Game(BasicGame, mobase.IPluginFileMapper, mobase.IPluginInstallerSimple):
    Name = "Baldur's Gate 3 Unofficial Support Plugin"
    Author = "Alvadus"
    Version = "1.2.0"

    GameName = "Baldur's Gate 3"
    GameShortName = "baldursgate3"
    GameNexusName = "baldursgate3"
    GameValidShortNames = ["baldursgate3"]

    GameBinary = r"bin\bg3.exe"
    GameDataPath = r"Data"
    GameSavesDirectory = "%GAME_DOCUMENTS%/Savegames/Story"
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
        "Root": {
            "pattern": "*",
            "pathName": "",
            "absolute": True,
        }
    }

    def __init__(self):
        BasicGame.__init__(self)
        mobase.IPluginFileMapper.__init__(self)
        mobase.IPluginInstallerSimple.__init__(self)
        self._data_checker = BG3ModDataChecker()
        self._install_parent = None
        self._quick_options = None

    def init(self, organizer: mobase.IOrganizer) -> bool:
        super().init(organizer)
        self._quick_options = BG3QuickOptions(organizer, self.name(), self.appDataDirectory)
        self._register_feature(self._data_checker)
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

        self._organizer.onUserInterfaceInitialized(self.onUserInterfaceLoad) # on Mod Organizer 2 Load
        self._organizer.onProfileCreated(self.onProfileCreated) # on Profile Created

        try:
            modSettings.set_native_reader(
                bool(self._organizer.pluginSetting(self.name(), "use_native_pak_reader"))
            )
        except Exception as e:
            qDebug(f"BG3: could not read use_native_pak_reader, defaulting to on: {e}")

        return True


    def priority(self) -> int:
        return 120

    def isManualInstaller(self) -> bool:
        return False

    def setParentWidget(self, widget):
        self._install_parent = widget

    def isArchiveSupported(self, tree) -> bool:
        try:
            return bool(self._data_checker._variant_pak_groups(tree))
        except Exception as e:
            qDebug(f"BG3: variant probe failed: {e}")
            return False

    def install(self, name, tree, version, nexus_id):
        try:
            groups = self._data_checker._variant_pak_groups(tree)
            if not groups:
                return mobase.InstallResult.NOT_ATTEMPTED

            chosen = BG3VariantDialog.choose(groups, self._install_parent)
            if chosen is None:
                qDebug("BG3: variant picker closed; installation cancelled.")
                return mobase.InstallResult.CANCELED

            if chosen == BG3VariantDialog.INSTALL_ALL:
                qDebug("BG3: installing every variant on request.")
            else:
                self._data_checker.keep_only_variant(groups, chosen)
                qDebug(f"BG3: keeping only the '{chosen}' variant.")

            return mobase.InstallResult.NOT_ATTEMPTED
        except Exception as e:
            qDebug(f"BG3: variant install failed, deferring to other installers: {e}")
            return mobase.InstallResult.NOT_ATTEMPTED

    def settings(self) -> list[mobase.PluginSetting]:
        return super().settings() + [
            mobase.PluginSetting(
                "list_override_only_mods",
                "List override-only mods in modsettings.lsx, matching BG3 Mod Manager. "
                "Changes which mods the game loads.",
                False,
            ),
            mobase.PluginSetting(
                "use_native_pak_reader",
                "When Divine.exe can't read a .pak file, for example because .NET 8 is "
                "missing, read it with the plugin's built-in reader instead. Turn off to "
                "use Divine only.",
                True,
            ),
            mobase.PluginSetting(
                "script_extender_settings_editor",
                "(Experimental) Show the Script Extender box in the BG3 tab, with an "
                "editor for ScriptExtenderSettings.json and ScriptExtenderUpdaterConfig.json.",
                False,
            ),
        ]

    def appDataDirectory(self) -> Path:
        local_appdata = os.getenv("LOCALAPPDATA") or str(
            Path(QDir.homePath()) / "AppData" / "Local"
        )
        return Path(local_appdata) / "Larian Studios" / "Baldur's Gate 3"

    def documentsDirectory(self) -> QDir:
        return QDir(str(self.appDataDirectory() / "PlayerProfiles" / "Public"))

    def _include_override_only_modules(self) -> bool:
        try:
            return bool(self._organizer.pluginSetting(self.name(), "list_override_only_mods"))
        except Exception:
            return False

    def onAboutToRun(self, executable: str):
        try:
            generated = modSettings.generate_mod_settings(
                self._organizer,
                self._organizer.modList(),
                self._organizer.profile(),
                self._include_override_only_modules(),
            )
        except Exception as e:
            self._quick_options.report_modsettings_failure(str(e))
            generated = False

        if generated:
            self._quick_options.log_modsettings_result()
        else:
            try:
                modSettings.ensure_mod_settings_exists(self._organizer.profile())
            except Exception as e:
                qDebug(f"BG3: could not ensure modsettings.lsx exists: {e}")

        return True


    def onFinishedRun(self, executable: str, exit_code: int, error: str = ""):
        try:
            self._drain_appdata_to_overwrite()
        except Exception as e:
            qDebug(f"BG3: post-run cleanup failed: {e}")
        return True

    def _drain_appdata_to_overwrite(self):
        # Handle Script Extender files
        appdata_path = self.appDataDirectory()

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

        self._prune_level_cache()

    def _level_cache_path(self) -> Path:
        return Path(self._organizer.overwritePath()) / "LevelCache"

    def _prune_level_cache(self):
        level_cache = self._level_cache_path()
        if not level_cache.is_dir():
            return

        removed = 0
        freed = 0
        for entry in level_cache.iterdir():
            if not entry.is_dir():
                continue
            try:
                size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
                shutil.rmtree(entry)
            except OSError as e:
                qDebug(f"BG3: kept LevelCache/{entry.name}: {e}")
                continue
            removed += 1
            freed += size

        if removed:
            qDebug(f"BG3: removed {removed} LevelCache folder(s), freed {freed / 1e6:.0f} MB.")
            folders = "folder" if removed == 1 else "folders"
            self._quick_options.log(
                f"Cleaned up {removed} leftover cache {folders}, {freed / 1e6:.0f} MB freed."
            )

    def onModInstalled(self, mod):
        name = mod.name() if hasattr(mod, "name") else str(mod)
        before = modSettings.native_reader_stats()
        self._quick_options.invalidate_pak_cache()
        try:
            modSettings.mod_installed(
                self._organizer, self._organizer.modList(), self._organizer.profile(), mod
            )
        except Exception as e:
            qDebug(f"BG3: metadata scan failed for {name}: {e}")
            self._quick_options.log(f"{name}: could not read the mod's paks.")
            return True
        self._quick_options.log_reader_activity(name, before)
        self._quick_options.notify_mods_changed([name])
        return True

    def onModRemoved(self, mod):
        self._quick_options.invalidate_pak_cache()
        modSettings.mod_removed(self._organizer, self._organizer.profile(), mod)
        return True

    def onUserInterfaceLoad(self, window):
        try:
            if check_bg3_paths(self._organizer) is False:
                return True
            self._quick_options.install_tab(window)
        except Exception as e:
            qDebug(f"BG3: interface setup failed: {e}")
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

        appdata_path = QDir(str(self.appDataDirectory()))

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

        level_cache = self._level_cache_path()
        try:
            level_cache.mkdir(parents=True, exist_ok=True)
            map.append(mobase.Mapping(
                source=str(level_cache),
                destination=appdata_path.absoluteFilePath("LevelCache"),
                is_directory=True,
                create_target=True,
            ))
        except OSError as e:
            qDebug(f"BG3: could not map LevelCache: {e}")

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
