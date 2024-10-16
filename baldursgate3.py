from abc import ABC
from pathlib import Path

import mobase
from PyQt6.QtCore import QDir, QDirIterator, QFileInfo
from PyQt6.QtGui import QImage

from ..basic_features import BasicGameSaveGameInfo, BasicModDataChecker, GlobPatterns
from ..basic_features.basic_save_game_info import BasicGameSaveGame
from ..basic_game import BasicGame

class BG3SaveGame(BasicGameSaveGame):
    