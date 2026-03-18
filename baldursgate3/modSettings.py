import hashlib
import json
import multiprocessing
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.dom import minidom
import xml.etree.ElementTree as ET

import mobase  # type: ignore

divine_file = Path(__file__).resolve().parent / "tools" / "Divine.exe"
cache_lock = threading.Lock()
_DEFAULT_ATTRIBUTES = ("Folder", "MD5", "Name", "PublishHandle", "UUID", "Version64", "Version")
_IGNORED_PATHS = ("Game/GUI/Assets", "ScriptExtender")
_BUILTIN_FOLDERS = (
    "Public/", "Public/Shared/", "Public/SharedDev/", "Public/Gustav/", "Public/GustavX/",
    "Public/GustavDev/", "Public/MainUI/", "Public/CrossplayUI/", "Public/PhotoMode/",
    "Public/ModBrowser/", "Public/DiceSet_01/", "Public/DiceSet_02/", "Public/DiceSet_03/",
    "Public/DiceSet_04/", "Public/DiceSet_05/", "Public/DiceSet_06/", "Public/DiceSet_07/", 
    "Public/Honour/", "Public/HonourX/", "Public/Engine/", "Public/Game/", "Public/FW3/",

    "Mods/Shared/", "Mods/SharedDev/", "Mods/Gustav/", "Mods/GustavX/",
    "Mods/GustavDev/", "Mods/MainUI/", "Mods/CrossplayUI/", "Mods/PhotoMode/",
    "Mods/ModBrowser/", "Mods/DiceSet_01/", "Mods/DiceSet_02/", "Mods/DiceSet_03/",
    "Mods/DiceSet_04/", "Mods/DiceSet_05/", "Mods/DiceSet_06/", "Mods/DiceSet_07/",
    "Mods/Honour/", "Mods/HonourX/", "Mods/Engine/", "Mods/Game/", "Mods/FW3/", 
)

try:
    _CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
except AttributeError:
    _CREATE_NO_WINDOW = 0


def _add_module_attributes(parent, metadata, skip=frozenset({"Override", "LoadOrder"})):
    for attr_id, attr_data in metadata.items():
        if attr_id not in skip:
            el = ET.SubElement(parent, "attribute")
            el.set("id", attr_id)
            el.set("type", attr_data["type"])
            el.set("value", str(attr_data["value"]))


def generate_mod_settings(organizer: mobase.IOrganizer, modlist: mobase.IModList, profile: mobase.IProfile):
    _fix_modscache(organizer)
    mod_settings = {}

    game_data_path = Path(organizer.managedGame().dataDirectory().absolutePath())
    gustav_pak = game_data_path / "GustavX.pak"
    if gustav_pak.exists():
        gustav_metadata = _get_metadata_from_pak(gustav_pak)
        if gustav_metadata:
            folder_name = gustav_metadata.get("Folder", {}).get("value", "GustavX")
            mod_settings["__GustavBase__"] = {folder_name: gustav_metadata}

    max_workers = min(multiprocessing.cpu_count(), 16)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for modName in modlist.allModsByProfilePriority():
            if modlist.state(modName):
                mod_path = Path(modlist.getMod(modName).absolutePath()) / "PAK_FILES"
                mod_files = list(mod_path.glob("*.pak"))
                for file in mod_files:
                    futures.append(
                        executor.submit(_get_metadata, modName, file, profile.absolutePath())
                    )
        
        for future in as_completed(futures):
            try:
                meta = future.result()
                if meta and meta["metadata"]:
                    m = meta["metadata"]
                    if not m.get("Override") or m.get("LoadOrder"):
                        if (int(modlist.state(meta["modName"]) / 2) % 2 != 0):
                            mod_settings.setdefault(meta["modName"], {})[meta["file"]] = meta["metadata"]
            except Exception as e:
                print(f"Error processing file: {e}")
                
    profile_path = Path(organizer.profile().absolutePath())
    root = ET.Element("save")
    version = ET.SubElement(root, "version")
    version.set("major", "4")
    version.set("minor", "8")
    version.set("revision", "0")
    version.set("build", "500")
    
    region = ET.SubElement(root, "region")
    region.set("id", "ModuleSettings")
    
    root_node = ET.SubElement(region, "node")
    root_node.set("id", "root")
    
    children = ET.SubElement(root_node, "children")
    
    if len(mod_settings) > 1:
        mods_order_node = ET.SubElement(children, "node")
        mods_order_node.set("id", "ModOrder")
    
    mods_node = ET.SubElement(children, "node")
    mods_node.set("id", "Mods")
    
    mods_children = ET.SubElement(mods_node, "children")
    
    gustav_base = mod_settings.get("__GustavBase__", {})
    gustav_data = next(iter(gustav_base.values()), None) if gustav_base else None
    if gustav_data:
        gustav_node = ET.SubElement(mods_children, "node", id="ModuleShortDesc")
        _add_module_attributes(gustav_node, gustav_data)
    mod_settings.pop("__GustavBase__", None)

    for mod_name in modlist.allModsByProfilePriority():
        if modlist.state(mod_name) and (int(modlist.state(mod_name) / 2) % 2 != 0):
            for file_name, metadata in sorted(mod_settings.get(mod_name, {}).items(), key=lambda x: x[0]):
                order_node = ET.SubElement(mods_order_node, "children", id="Module")
                u = metadata.get("UUID", {})
                attr = ET.SubElement(order_node, "attribute")
                attr.set("id", "UUID")
                attr.set("value", u.get("value", ""))
                attr.set("type", u.get("type", ""))
                mod_node = ET.SubElement(mods_children, "node", id="ModuleShortDesc")
                _add_module_attributes(mod_node, metadata)

    (profile_path / "modsettings.lsx").write_bytes(
        minidom.parseString(ET.tostring(root, encoding="unicode")).toprettyxml(indent="  ", encoding="UTF-8")
    )
    
    return True

def mod_installed(organizer: mobase.IOrganizer, modlist: mobase.IModList, profile: mobase.IProfile, mod):
    cache_path = Path(profile.absolutePath()) / "modsCache.json"
    if not cache_path.exists():
        return False
    mod_name = mod.name()
    profile_path = profile.absolutePath()

    try:
        mods_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        mods_cache = {}

    mod_data = mods_cache.get(mod_name)
    mod_path = Path(modlist.getMod(mod_name).absolutePath()) / "PAK_FILES"
    mod_files = list(mod_path.glob("*.pak"))
    max_workers = max(multiprocessing.cpu_count() - 1, 1)

    if not mod_data:
        mods_cache[mod_name] = {"Files": {}}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_get_metadata, mod_name, f, profile_path) for f in mod_files]
            for future in as_completed(futures):
                try:
                    meta = future.result()
                    if meta:
                        mods_cache[mod_name]["Files"][meta["file"]] = meta["metadata"]
                except Exception as e:
                    print(f"Error processing file: {e}")
    else:
        existing = mod_data.get("Files", {})
        to_refresh = []
        for f in mod_files:
            cached = existing.get(f.name)
            if not (cached and cached.get("MD5", {}).get("value") == get_md5(f)):
                to_refresh.append(f)
        if to_refresh:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_get_metadata, mod_name, f, profile_path, True) for f in to_refresh]
                for future in as_completed(futures):
                    try:
                        meta = future.result()
                        if meta:
                            mods_cache[mod_name]["Files"][meta["file"]] = meta["metadata"]
                    except Exception as e:
                        print(f"Error processing file: {e}")

    cache_path.write_text(json.dumps(mods_cache, indent=4, ensure_ascii=False), encoding="utf-8")
    return True


def mod_removed(organizer: mobase.IOrganizer, profile: mobase.IProfile, mod):
    cache_path = Path(profile.absolutePath()) / "modsCache.json"
    if not cache_path.exists():
        return True
    mod_name = mod.name() if hasattr(mod, "name") else str(mod)
    try:
        mods_cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if mod_name in mods_cache:
            del mods_cache[mod_name]
            cache_path.write_text(json.dumps(mods_cache, indent=4, ensure_ascii=False), encoding="utf-8")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return True


def _extract_pak(file):
    temp_dir = Path(__file__).resolve().parent / "temp_extracted"
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_hash = hashlib.md5(str(file).encode()).hexdigest()[:10]
    output_dir = temp_dir / file_hash

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    command = [
        str(divine_file),
        "-a", "extract-package",
        "-g", "bg3",
        "-s", str(file),
        "-d", str(output_dir),
        "-x", "*/meta.lsx",
        "-l", "off"
    ]

    subprocess.run(command, creationflags=_CREATE_NO_WINDOW, check=True)
    return output_dir

def check_hash(pak_path, module_info_node):
    hash_element = module_info_node.find(".//attribute[@id='MD5']")
    return hash_element is not None and hash_element.attrib.get("value") == get_md5(pak_path)

def get_md5(pak_path):
    hasher = hashlib.md5()
    with open(pak_path, 'rb') as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

def check_override_pak(pak_path, module_info_node):
    cache_key = str(pak_path)
    cache = getattr(check_override_pak, "cache", None)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    try:
        result = subprocess.run(
            [str(divine_file), "-a", "list-package", "-g", "bg3", "-s", str(pak_path)],
            creationflags=_CREATE_NO_WINDOW,
            capture_output=True,
            text=True,
            check=True,
        )
        list_output = result.stdout
        override = {"Override": False, "LoadOrder": False}
        public_folder_path = ""

        if module_info_node is not None:
            folder_element = module_info_node.find(".//attribute[@id='Folder']")
            if folder_element is not None:
                folder_name = folder_element.attrib["value"]
                mods_folder_path = f"Mods/{folder_name}"
                public_folder_path = f"Public/{folder_name}"
                if public_folder_path in list_output:
                    override["LoadOrder"] = True
                files_in_folder = [l.strip() for l in list_output.splitlines() if mods_folder_path in l]
                if len(files_in_folder) > 1:
                    override["LoadOrder"] = True

        for line in list_output.splitlines():
            if any(ignored in line for ignored in _IGNORED_PATHS):
                continue
            if any(folder in line for folder in _BUILTIN_FOLDERS):
                override["Override"] = True
                if public_folder_path and public_folder_path in list_output:
                    override["LoadOrder"] = True
                return override

        if not getattr(check_override_pak, "cache", None):
            check_override_pak.cache = {}
        check_override_pak.cache[cache_key] = override
        return override
    except Exception as e:
        print(f"Error checking override status: {e}")
        return False

def _metadata_result(mod_name, file_name, metadata):
    return {"modName": mod_name, "file": file_name, "metadata": metadata}


def _get_metadata_from_pak(pak_path: Path) -> dict | None:
    extracted_pak = None
    try:
        extracted_pak = _extract_pak(pak_path)
        if not extracted_pak:
            return None
        meta_files = list(extracted_pak.glob("**/meta.lsx"))
        if not meta_files:
            return None
        tree = ET.parse(str(meta_files[0]))
        module_info_node = tree.getroot().find(".//node[@id='ModuleInfo']")
        if module_info_node is None:
            return None
        override_result = check_override_pak(pak_path, module_info_node)
        meta_data = dict(override_result) if isinstance(override_result, dict) else {}
        for attr in _DEFAULT_ATTRIBUTES:
            el = module_info_node.find(f"./attribute[@id='{attr}']")
            if el is not None:
                meta_data[attr] = {"value": el.attrib.get("value"), "type": el.attrib.get("type", "LSString")}
                if attr == "MD5":
                    meta_data[attr]["value"] = get_md5(pak_path)
        return meta_data
    except Exception as e:
        print(f"Error extracting metadata from {pak_path.name}: {e}")
        return None
    finally:
        if extracted_pak and extracted_pak.exists():
            try:
                shutil.rmtree(extracted_pak)
            except Exception:
                pass


def _get_metadata(mod_name, file, profile_path, refresh_cache=False):
    cache_path = Path(profile_path) / "modsCache.json"
    with cache_lock:
        if cache_path.exists() and not refresh_cache:
            try:
                mods_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            else:
                files = mods_cache.get(mod_name, {}).get("Files", {})
                if file.name in files:
                    return _metadata_result(mod_name, file.name, files[file.name])

    extracted_pak = None
    try:
        extracted_pak = _extract_pak(file)
        if not extracted_pak:
            return _metadata_result(mod_name, file.name, {})
        meta_files = list(extracted_pak.glob("**/meta.lsx"))
        if not meta_files:
            print(f"No meta.lsx files found in extracted PAK: {file.name}")
            return _metadata_result(mod_name, file.name, {})

        tree = ET.parse(str(meta_files[0]))
        module_info_node = tree.getroot().find(".//node[@id='ModuleInfo']")
        if module_info_node is None:
            return _metadata_result(mod_name, file.name, {})

        override_result = check_override_pak(file, module_info_node)
        meta_data = dict(override_result) if isinstance(override_result, dict) else {}
        for attr in _DEFAULT_ATTRIBUTES:
            el = module_info_node.find(f"./attribute[@id='{attr}']")
            if el is not None:
                meta_data[attr] = {"value": el.attrib.get("value"), "type": el.attrib.get("type", "LSString")}
                if attr == "MD5":
                    meta_data[attr]["value"] = get_md5(file)

        with cache_lock:
            try:
                mods_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                mods_cache = {}
            mods_cache.setdefault(mod_name, {"Files": {}})["Files"][file.name] = meta_data
            cache_path.write_text(json.dumps(mods_cache, indent=4, ensure_ascii=False), encoding="utf-8")
        return _metadata_result(mod_name, file.name, meta_data)
    except Exception as e:
        print(f"Error in _get_metadata for {file.name}: {e}")
        return _metadata_result(mod_name, file.name, {})
    finally:
        if extracted_pak and extracted_pak.exists():
            try:
                shutil.rmtree(extracted_pak)
            except Exception as e:
                print(f"Error cleaning up {extracted_pak}: {e}")

def _fix_modscache(organizer: mobase.IOrganizer):
    try:
        profile_path = Path(organizer.profile().absolutePath())
        cache_json_path = profile_path / "modsCache.json"

        if not cache_json_path.exists():
            print(f"{cache_json_path} does not exist. Exiting.")
            return True
        mods_cache = json.loads(cache_json_path.read_text(encoding="utf-8"))

        modlist = organizer.modList()
        installed_mods = {mod: True for mod in modlist.allMods()}

        mods_to_remove = []
        for mod_name, mod_data in mods_cache.items():
            if mod_name not in installed_mods:
                mods_to_remove.append(mod_name)
                continue

            mod_path = Path(modlist.getMod(mod_name).absolutePath()) / "PAK_FILES"
            if not mod_path.exists():
                print(f"Mod path {mod_path} does not exist. Skipping {mod_name}.")
                continue

            current_files = {f.name: f for f in mod_path.glob("*.pak")}
            cached_files = mod_data.get("Files", {})

            missing_files = set(cached_files.keys()) - set(current_files.keys())
            for missing_file in missing_files:
                print(f"Removing missing file {missing_file} from {mod_name}.")
                del mod_data["Files"][missing_file]

            for file_name, pak_path in current_files.items():
                cached_file = cached_files.get(file_name)
                if cached_file:
                    cached_md5 = cached_file.get("MD5", {}).get("value")
                    if cached_md5 and cached_md5 != get_md5(pak_path):
                        print(f"PAK {file_name} changed (MD5 mismatch), invalidating cache for {mod_name}.")
                        del mod_data["Files"][file_name]

            if not mod_data["Files"]:
                mods_to_remove.append(mod_name)

        for mod_name in mods_to_remove:
            print(f"Removing mod {mod_name} from mods cache.")
            del mods_cache[mod_name]
        cache_json_path.write_text(json.dumps(mods_cache, indent=4, ensure_ascii=False), encoding="utf-8")

        print("Successfully fixed mods cache.")
        return True

    except Exception as e:
        print(f"Failed to fix mods cache: {str(e)}")
