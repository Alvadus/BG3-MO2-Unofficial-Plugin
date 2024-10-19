from abc import ABC
from pathlib import Path

import mobase
from PyQt6.QtCore import QDir, QFileInfo, QDirIterator, QFile, qDebug

from ..basic_features import BasicGameSaveGameInfo, BasicModDataChecker, GlobPatterns
from ..basic_features.basic_save_game_info import BasicGameSaveGame
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

class BG3Game(BasicGame):
    Name = "Baldur's Gate 3 Unofficial Support Plugin"
    Author = "Alvadus"

    def version(self):
        return mobase.VersionInfo(0, 0, 1, mobase.ReleaseType.PRE_ALPHA)

    GameName = "Baldur's Gate 3"
    GameShortName = "baldursgate3"
    GameNexusName = "baldursgate3"
    GameValidShortNames = ["baldursgate3"]

    GameBinary = r"bin\bg3.exe"
    GameDataPath = r"Data"
    GameSavesDirectory = r"%LOCALAPPDATA%\\Larian Studios\\Baldur's Gate 3\\PlayerProfiles\\Public\\Savegames\\Story\\"
    GameSaveExtension = "lsv"

    GameNexusId = 3474
    GameSteamId = 1086940
    GameGogId = 1456460669

    def __init__(self):
        super().__init__()

    def init(self, organizer: mobase.IOrganizer) -> bool:
        super().init(organizer)
        self._register_feature(BG3ModDataChecker())
        self._register_feature(BasicGameSaveGameInfo(lambda s: s.with_suffix(".webp")))
        return True

    def iniFiles(self):
        return ["modsettings.lsx"]

    def mappings(self) -> list[mobase.Mapping]:
        mods_path = Path(self._organizer.modsPath())

        pak_mods = self._get_mods_from_type("PAK_FILES")
        se_mods = self._get_mods_from_type("SE_CONFIG")

        return []

    def _get_mods_from_type(self, mod_type: str):
        mods_path = Path(self._organizer.modsPath())
        all_mods = self._organizer.modList().allModsByProfilePriority()

        mods: list[str] = []
        for modName in all_mods:
            if self._organizer.modList().state(modName) & mobase.ModState.ACTIVE != 0:
                if mods_path.joinpath(modName, mod_type).exists():
                    mods.append(modName)
        return mods