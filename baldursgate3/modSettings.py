import os
import json
import subprocess
import shutil
from pathlib import Path
from tabnanny import check
from xml.dom import minidom
import xml.etree.ElementTree as ET

import zlib
import struct
import io

import mobase
from PyQt6.QtCore import QDir, QFileInfo, QDirIterator, QFile, qDebug

divine_file = Path(__file__).resolve().parent / 'tools' / 'Divine.exe'

def generate_mod_settings(modlist: mobase.IModList, profile: mobase.IProfile):
    mod_settings = {}

    modSequence = modlist.allModsByProfilePriority()
    for modName in modSequence:
        if modlist.state(modName) & mobase.ModState.ACTIVE != 0:
            mod_path = Path(modlist.getMod(modName).absolutePath()) / "PAK_FILES"
            mod_files = list(mod_path.glob("*.pak"))
            for file in mod_files:
                meta_data = _extract_meta(file)
                print(meta_data)

    return True

def _extract_meta(file):
    meta_data = []

    temp_dir = Path(__file__).resolve().parent / 'temp_extracted'
    temp_dir.mkdir(parents=True, exist_ok=True)

    command = [
        str(divine_file),
        "-a", "extract-package",
        "-g", "bg3",
        "-s", str(file),
        "-d", str(temp_dir),
        "-x", "*/meta.lsx",
        "-l", "off"
    ]

    result = subprocess.run(
        command,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=True
    )
    
    return meta_data

def _fix_modscache():
    return True
