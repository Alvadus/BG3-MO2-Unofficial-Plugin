from abc import ABC
from pathlib import Path

import mobase
from PyQt6.QtCore import QDir, QFileInfo, QDirIterator, QFile, QFileInfo, qDebug

from ..basic_features import BasicGameSaveGameInfo, BasicLocalSavegames, BasicModDataChecker
from ..basic_game import BasicGame

class BaldursGate3Game(BasicGame, mobase.IPluginFileMapper, ABC):
    Name = "Baldur's Gate 3 Unofficial Support Plugin"
    Author = "Dragozino"

    GameName = "Baldur's Gate 3"
    GameShortName = "baldursgate3"
    GameNexusName = "baldursgate3"
    GameValidShortNames = ["baldursgate3"]

    GameBinary = r"bin\bg3.exe"
    GameDataPath = r"%LOCALAPPDATA%\\Larian Studios\\Baldur's Gate 3\\Mods"
    GameSavesDirectory = r"%LOCALAPPDATA%\\Larian Studios\\Baldur's Gate 3\\PlayerProfiles\\Public\\Savegames\\Story\\"
    GameSaveExtension = "lsv"

    GameNexusId = 3474
    GameSteamId = 1086940
    GameGogId = 1456460669

    def __init__(self):
        BasicGame.__init__(self)
        mobase.IPluginFileMapper.__init__(self)