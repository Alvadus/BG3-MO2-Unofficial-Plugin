from abc import ABC
from pathlib import Path

import mobase
from PyQt6.QtCore import QDir, QFileInfo, QDirIterator, QFile, qDebug

from ..basic_features import BasicGameSaveGameInfo, BasicModDataChecker, GlobPatterns
from ..basic_features.basic_save_game_info import BasicGameSaveGame
from ..basic_game import BasicGame

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
                    # "*": "PAK_FILES/",
                    # Script Extender Config Mods
                    "*.json": "SE_CONFIG/",
                    "BG3MCM": "SE_CONFIG/",
                    # DLL Mods
                    "bin": "Root/bin",
                    "*.dll": "Root/bin/",
                }
            )
        )

class BG3GamePlugin(mobase.GamePlugins, ABC):
    def __init__(self, organizer: mobase.IOrganizer):
        super().__init__()
        self._organizer = organizer

class BG3Game(BasicGame):
    Name = "Baldur's Gate 3 Unofficial Support Plugin"
    Author = "Dragozino"

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

    def init(self, organizer: mobase.IOrganizer):
        super().init(organizer)
        self._register_feature(BG3ModDataChecker())
        self._register_feature(BasicGameSaveGameInfo(lambda s: s.with_suffix(".webp")))
        return True

    def iniFiles(self):
        return ["modsettings.lsx"]

    def mappings(self) -> list[mobase.Mapping]:
        mods_path = Path(self._organizer.modsPath())

        pak_mods = self.get_mods_from_type("PAK_FILES")
        se_mods = self.get_mods_from_type("SE_CONFIG")

        return []

    def get_mods_from_type(self, mod_type: str):
        mods_path = Path(self._organizer.modsPath())
        all_mods = self._organizer.modList().allModsByProfilePriority()

        mods: list[str] = []
        for modName in all_mods:
            if self._organizer.modList().state(modName) & mobase.ModState.ACTIVE != 0:
                if mods_path.joinpath(modName, mod_type).exists():
                    mods.append(modName)
        return mods