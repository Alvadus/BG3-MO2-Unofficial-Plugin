from abc import ABC
import os
import json
from pathlib import Path

import mobase # type: ignore
from PyQt6.QtCore import QDir, QFileInfo, QDirIterator, QFile, qDebug

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

class BG3ModDataChecker(BasicModDataChecker):
    def __init__(self):
        super().__init__(
            GlobPatterns(
                valid=[
                    "PAK_FILES",
                    "SE_CONFIG",
                    "Generated",
                    "Root",
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
                    "bin": "Root/bin",
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
    Version = "0.7.0"

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
        }
    }

    def __init__(self):
        BasicGame.__init__(self)
        mobase.IPluginFileMapper.__init__(self)
        
    def create_modscache(self, profile_path):
        profile_path = Path(profile_path)
        mod_cache_path = profile_path / "modsCache.json"
        if not mod_cache_path.exists():
            with open(mod_cache_path, "w") as f:
                json.dump({}, f)

    def init(self, organizer: mobase.IOrganizer) -> bool:
        super().init(organizer)
        self._register_feature(BG3ModDataChecker())
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

        return True

    def onAboutToRun(self, executable: str):
        self.create_modscache(self._organizer.profile().absolutePath())
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

            for file in appdata_path.glob("**/*"):
                if file.is_file():
                    rel_path = file.relative_to(appdata_path)
                    dest_file = overwrite_path / rel_path
                    
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    
                    try:
                        dest_file.write_bytes(file.read_bytes())
                        file.unlink()
                        
                        parent = file.parent
                        while parent != appdata_path:
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
        modSettings.mod_installed(self._organizer, self._organizer.modList(), self._organizer.profile(), mod)
        return True

    def onModRemoved(self, mod: str):
        return True

    def onUserInterfaceLoad(self, window):
        self.create_modscache(self._organizer.profile().absolutePath())
        return True

    def onProfileCreated(self, profile: mobase.IProfile):
        profile_path = Path(profile.absolutePath())
        print(profile_path)
        self.create_modscache(profile_path)
        return True

    def iniFiles(self):
        return ["modsettings.lsx"]

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

            # Handle mods from mod directory
            for modName in self._get_mods_from_type(mod_type):
                mod_path = Path(modlist.getMod(modName).absolutePath()) / mod_type
                mod_files = list(mod_path.glob(mod_pattern))

                for file in mod_files:
                    map.append(mobase.Mapping(
                        source=str(file),
                        destination=os.path.join(appdata_path.absoluteFilePath(mod_destpath), str(file.name)),
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
                        destination=os.path.join(appdata_path.absoluteFilePath(mod_destpath), str(file.name)),
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
        mods_path = Path(self._organizer.modsPath())
        all_mods = self._organizer.modList().allModsByProfilePriority()

        mods: list[str] = []
        for modName in all_mods:
            if self._organizer.modList().state(modName) & mobase.ModState.ACTIVE != 0:
                if mods_path.joinpath(modName, mod_type).exists():
                    mods.append(modName)
        return mods