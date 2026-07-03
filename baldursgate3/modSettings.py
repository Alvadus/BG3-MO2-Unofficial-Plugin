import hashlib
import configparser
import multiprocessing
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote
import xml.etree.ElementTree as ET

import mobase  # type: ignore

divine_file = Path(__file__).resolve().parent / "tools" / "Divine.exe"
cache_lock = threading.Lock()
_DEFAULT_ATTRIBUTES = ("Folder", "MD5", "Name", "PublishHandle", "UUID", "Version64", "Version")
_IGNORED_PATHS = ("Game/GUI/Assets", "ScriptExtender")
_BUILTIN_MODULE_FOLDERS = frozenset({
    "Shared", "SharedDev", "Gustav", "GustavX", "GustavDev", "MainUI", "CrossplayUI",
    "PhotoMode", "ModBrowser", "DiceSet_01", "DiceSet_02", "DiceSet_03", "DiceSet_04",
    "DiceSet_05", "DiceSet_06", "DiceSet_07", "Honour", "HonourX", "Engine", "Game", "FW3",
})
_PAK_DATA_PATH_PATTERN = re.compile(r"^(Mods|Public)/([^/]+)/[^\t\r\n]+", re.IGNORECASE)

try:
    _CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
except AttributeError:
    _CREATE_NO_WINDOW = 0


def _add_module_attributes(parent, metadata, skip=frozenset({"Override", "LoadOrder", "HasOsiris", "HasScriptExtender"})):
    for attr_id, attr_data in metadata.items():
        if attr_id not in skip:
            el = ET.SubElement(parent, "attribute")
            el.set("id", attr_id)
            el.set("type", attr_data["type"])
            el.set("value", str(attr_data["value"]))


def _metadata_from_module_node(node):
    metadata = {}
    for attribute in node.findall("./attribute"):
        attr_id = attribute.attrib.get("id")
        if attr_id:
            metadata[attr_id] = {
                "value": attribute.attrib.get("value", ""),
                "type": attribute.attrib.get("type", "LSString"),
            }
    return metadata


def _read_existing_modsettings_modules(modsettings_file: Path):
    if not modsettings_file.exists():
        return []

    try:
        root = ET.parse(modsettings_file).getroot()
    except ET.ParseError:
        return []

    modules = []
    for node in root.findall(".//node[@id='ModuleShortDesc']"):
        metadata = _metadata_from_module_node(node)
        uuid = metadata.get("UUID", {}).get("value", "")
        folder = metadata.get("Folder", {}).get("value", "")
        if uuid and folder not in _BUILTIN_MODULE_FOLDERS:
            modules.append(metadata)
    return modules


def _gustav_metadata():
    return {
        "Folder": {"value": "GustavX", "type": "LSString"},
        "MD5": {"value": "", "type": "LSString"},
        "Name": {"value": "GustavX", "type": "LSString"},
        "PublishHandle": {"value": "0", "type": "uint64"},
        "UUID": {"value": "cb555efe-2d9e-131f-8195-a89329d218ea", "type": "guid"},
        "Version64": {"value": "36028797018963968", "type": "int64"},
    }


def _is_mod_active(modlist: mobase.IModList, mod_name):
    return modlist.state(mod_name) & mobase.ModState.ACTIVE != 0


def _get_mod_name(mod):
    return mod.name() if hasattr(mod, "name") else str(mod)


def _mod_root_from_pak(pak_path: Path):
    return pak_path.parent.parent if pak_path.parent.name == "PAK_FILES" else pak_path.parent


def _mod_cache_path(mod_root: Path):
    return mod_root / "meta.ini"


def _mod_cache_section(file_name):
    return unquote(file_name)


def _matching_mod_cache_sections(config, file_name):
    cache_key = _mod_cache_section(file_name)
    return [
        section
        for section in config.sections()
        if section.casefold().endswith(".pak") and _mod_cache_section(section) == cache_key
    ]


def _find_mod_cache_section(config, file_name):
    if config.has_section(file_name):
        return file_name

    matches = _matching_mod_cache_sections(config, file_name)
    return matches[0] if matches else None


def _read_mod_cache_config(mod_root: Path):
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config.read(_mod_cache_path(mod_root), encoding="utf-8")
    return config


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sections_snapshot(config):
    return {
        section: tuple(config[section].items())
        for section in config.sections()
    }


def _read_download_meta(mod):
    if not mod or not hasattr(mod, "installationFile"):
        return {}

    install_file = mod.installationFile()
    if not install_file:
        return {}

    install_path = Path(install_file)
    candidates = [
        Path(str(install_path) + ".meta"),
        install_path.with_suffix(install_path.suffix + ".meta"),
        install_path.with_suffix(".meta"),
    ]

    for candidate in dict.fromkeys(candidates):
        if not candidate.exists():
            continue
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        config.read(candidate, encoding="utf-8")
        if config.has_section("General"):
            return dict(config["General"])
    return {}


def _seed_minimal_mod_meta(config, mod):
    before = _sections_snapshot(config)
    download_meta = _read_download_meta(mod)
    existing_general = config["General"] if config.has_section("General") else {}
    game_name = (
        download_meta.get("gameName")
        or existing_general.get("gameName")
        or (mod.gameName() if mod and hasattr(mod, "gameName") else "")
        or "baldursgate3"
    )
    mod_id = _safe_int(
        download_meta.get("modID")
        or download_meta.get("modid")
        or existing_general.get("modid")
        or (mod.nexusId() if mod and hasattr(mod, "nexusId") else 0)
    )

    if not config.has_section("General"):
        config.add_section("General")

    if game_name and not config["General"].get("gameName"):
        config["General"]["gameName"] = game_name
    if mod_id > 0 and not config["General"].get("modid"):
        config["General"]["modid"] = str(mod_id)

    return before != _sections_snapshot(config)


def _deserialize_metadata_section(section):
    metadata = {}
    for key, value in section.items():
        if key == "file":
            continue
        if key in {"Override", "LoadOrder", "HasOsiris", "HasScriptExtender"}:
            metadata[key] = value.lower() == "true"
            continue
        if "." in key:
            attr_id, attr_key = key.split(".", 1)
            if attr_key in {"value", "type"}:
                metadata.setdefault(attr_id, {})[attr_key] = value

    return metadata


def _serialize_metadata_section(section, pak_name, metadata):
    section.clear()
    section["Override"] = str(bool(metadata.get("Override", False))).lower()
    section["LoadOrder"] = str(bool(metadata.get("LoadOrder", False))).lower()
    section["HasOsiris"] = str(bool(metadata.get("HasOsiris", False))).lower()
    section["HasScriptExtender"] = str(bool(metadata.get("HasScriptExtender", False))).lower()

    for attr_id in _DEFAULT_ATTRIBUTES:
        attr_data = metadata.get(attr_id)
        if isinstance(attr_data, dict):
            section[f"{attr_id}.value"] = str(attr_data.get("value", ""))
            section[f"{attr_id}.type"] = str(attr_data.get("type", "LSString"))


def _read_mod_metadata_cache(mod_root: Path):
    config = _read_mod_cache_config(mod_root)
    cached_files = {}
    for section in config.sections():
        if not section.casefold().endswith(".pak"):
            continue
        cached_files[_mod_cache_section(section)] = _deserialize_metadata_section(config[section])
        cached_files[section] = cached_files[_mod_cache_section(section)]
    return cached_files


def _write_mod_metadata_cache(pak_path: Path, metadata, mod=None):
    mod_root = _mod_root_from_pak(pak_path)
    cache_path = _mod_cache_path(mod_root)
    config = _read_mod_cache_config(mod_root)
    _seed_minimal_mod_meta(config, mod)
    section = _find_mod_cache_section(config, pak_path.name) or pak_path.name
    if not config.has_section(section):
        config.add_section(section)
    _serialize_metadata_section(config[section], pak_path.name, metadata)
    with cache_path.open("w", encoding="utf-8") as f:
        config.write(f)


def _write_minimal_mod_meta(mod_root: Path, mod):
    cache_path = _mod_cache_path(mod_root)
    config = _read_mod_cache_config(mod_root)
    _seed_minimal_mod_meta(config, mod)
    with cache_path.open("w", encoding="utf-8") as f:
        config.write(f)


def _clean_mod_metadata_cache(mod_root: Path, mod_files, mod=None):
    config = _read_mod_cache_config(mod_root)
    current_sections = {_mod_cache_section(file.name) for file in mod_files}
    changed = _seed_minimal_mod_meta(config, mod)

    preferred_sections = {}
    for file in mod_files:
        matches = _matching_mod_cache_sections(config, file.name)
        if not matches:
            continue

        preferred = file.name if file.name in matches else matches[0]
        preferred_sections[_mod_cache_section(file.name)] = preferred
        for section in matches:
            if section != preferred:
                changed = config.remove_section(section) or changed

    for section in list(config.sections()):
        if section.casefold().endswith(".pak") and _mod_cache_section(section) not in current_sections:
            changed = config.remove_section(section) or changed

    if changed:
        with _mod_cache_path(mod_root).open("w", encoding="utf-8") as f:
            config.write(f)

    cached_files = {}
    for file in mod_files:
        cache_key = _mod_cache_section(file.name)
        section = preferred_sections.get(cache_key) or _find_mod_cache_section(config, file.name)
        if section:
            cached_files[cache_key] = _deserialize_metadata_section(config[section])
    return cached_files


def generate_mod_settings(organizer: mobase.IOrganizer, modlist: mobase.IModList, profile: mobase.IProfile):
    profile_path = Path(profile.absolutePath())
    mod_settings_file = profile_path / "modsettings.lsx"
    mod_settings = {}
    active_pak_count = 0

    max_workers = min(multiprocessing.cpu_count(), 16)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for modName in modlist.allModsByProfilePriority():
            if not _is_mod_active(modlist, modName):
                continue

            mod = modlist.getMod(modName)
            mod_root = Path(mod.absolutePath())
            mod_path = mod_root / "PAK_FILES"
            if not mod_path.exists():
                continue
            mod_files = list(mod_path.glob("*.pak"))
            active_pak_count += len(mod_files)
            cached_files = _clean_mod_metadata_cache(mod_root, mod_files, mod)

            for file in mod_files:
                cache_key = _mod_cache_section(file.name)
                cached_metadata = cached_files.get(cache_key)
                if cached_metadata:
                    if not cached_metadata.get("Override") or cached_metadata.get("LoadOrder"):
                        mod_settings.setdefault(modName, {})[cache_key] = cached_metadata
                else:
                    futures.append(
                        executor.submit(_get_metadata, modName, file, profile.absolutePath(), True, mod)
                    )

        for future in as_completed(futures):
            try:
                meta = future.result()
                if meta and meta["metadata"]:
                    m = meta["metadata"]
                    if not m.get("Override") or m.get("LoadOrder"):
                        if _is_mod_active(modlist, meta["modName"]):
                            mod_settings.setdefault(meta["modName"], {})[meta["file"]] = meta["metadata"]
            except Exception as e:
                print(f"Error processing file: {e}")

    fallback_modules = []
    if active_pak_count and not mod_settings:
        fallback_modules = _read_existing_modsettings_modules(mod_settings_file)
        if not fallback_modules:
            print("Skipping modsettings.lsx write because no active pak metadata was available.")
            return False
                
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
    
    if mod_settings or fallback_modules:
        mods_order_node = ET.SubElement(children, "node")
        mods_order_node.set("id", "ModOrder")
    
    mods_node = ET.SubElement(children, "node")
    mods_node.set("id", "Mods")
    
    mods_children = ET.SubElement(mods_node, "children")
    
    gustav_node = ET.SubElement(mods_children, "node", id="ModuleShortDesc")
    _add_module_attributes(gustav_node, _gustav_metadata())

    for mod_name in modlist.allModsByProfilePriority():
        if _is_mod_active(modlist, mod_name):
            for _file_name, metadata in sorted(mod_settings.get(mod_name, {}).items(), key=lambda x: x[0]):
                if mod_settings or fallback_modules:
                    order_node = ET.SubElement(mods_order_node, "children", id="Module")
                    u = metadata.get("UUID", {})
                    attr = ET.SubElement(order_node, "attribute")
                    attr.set("id", "UUID")
                    attr.set("value", u.get("value", ""))
                    attr.set("type", u.get("type", ""))
                mod_node = ET.SubElement(mods_children, "node", id="ModuleShortDesc")
                _add_module_attributes(mod_node, metadata)

    if fallback_modules:
        for metadata in fallback_modules:
            order_node = ET.SubElement(mods_order_node, "children", id="Module")
            u = metadata.get("UUID", {})
            attr = ET.SubElement(order_node, "attribute")
            attr.set("id", "UUID")
            attr.set("value", u.get("value", ""))
            attr.set("type", u.get("type", ""))
            mod_node = ET.SubElement(mods_children, "node", id="ModuleShortDesc")
            _add_module_attributes(mod_node, metadata)

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(mod_settings_file, encoding="UTF-8", xml_declaration=True)
    
    return True

def mod_installed(organizer: mobase.IOrganizer, modlist: mobase.IModList, profile: mobase.IProfile, mod, force_reparse=False):
    mod_name = _get_mod_name(mod)
    profile_path = profile.absolutePath()
    installed_mod = modlist.getMod(mod_name)
    mod_root = Path(installed_mod.absolutePath())
    mod_path = mod_root / "PAK_FILES"
    if not mod_path.exists():
        return False

    _write_minimal_mod_meta(mod_root, installed_mod)
    mod_files = list(mod_path.glob("*.pak"))
    max_workers = max(multiprocessing.cpu_count() - 1, 1)
    existing = _clean_mod_metadata_cache(mod_root, mod_files, installed_mod)

    to_refresh = []
    for f in mod_files:
        cached = existing.get(_mod_cache_section(f.name))
        if force_reparse or not (cached and cached_metadata_matches_file(cached, f)):
            to_refresh.append(f)

    if to_refresh:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_get_metadata, mod_name, f, profile_path, True, installed_mod) for f in to_refresh]
            for future in as_completed(futures):
                try:
                    meta = future.result()
                except Exception as e:
                    print(f"Error processing file: {e}")
    return True


def mod_removed(organizer: mobase.IOrganizer, profile: mobase.IProfile, mod):
    return True


def _extract_pak(file):
    temp_dir = Path(__file__).resolve().parent / "temp_extracted"
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_hash = hashlib.md5(str(file).encode()).hexdigest()[:10]
    output_dir = temp_dir / file_hash

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    meta_file = output_dir / "meta.lsx"
    single_file_command = [
        str(divine_file),
        "-a", "extract-single-file",
        "-g", "bg3",
        "-s", str(file),
        "-d", str(meta_file),
        "-f", "meta.lsx",
        "-l", "off"
    ]
    single_file_result = subprocess.run(
        single_file_command,
        creationflags=_CREATE_NO_WINDOW,
        capture_output=True,
        text=True,
        check=False,
    )
    if single_file_result.returncode == 0 and meta_file.exists():
        return output_dir

    meta_path = _find_package_meta_path(file)
    if meta_path:
        meta_output = output_dir / Path(meta_path).name
        exact_file_command = [
            str(divine_file),
            "-a", "extract-single-file",
            "-g", "bg3",
            "-s", str(file),
            "-d", str(meta_output),
            "-f", meta_path,
            "-l", "off"
        ]
        exact_file_result = subprocess.run(
            exact_file_command,
            creationflags=_CREATE_NO_WINDOW,
            capture_output=True,
            text=True,
            check=False,
        )
        if exact_file_result.returncode == 0 and meta_output.exists():
            return output_dir

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

def _find_package_meta_path(file):
    try:
        result = subprocess.run(
            [str(divine_file), "-a", "list-package", "-g", "bg3", "-s", str(file)],
            creationflags=_CREATE_NO_WINDOW,
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return None

    for line in result.stdout.splitlines():
        path = line.split("\t", 1)[0].replace("\\", "/")
        if path.casefold().endswith("/meta.lsx"):
            return path
    return None

def get_md5(pak_path):
    hasher = hashlib.md5()
    with open(pak_path, 'rb') as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()

def get_file_signature(pak_path: Path):
    stat = pak_path.stat()
    return {
        "Size": stat.st_size,
        "Modified": stat.st_mtime_ns,
    }

def cached_metadata_matches_file(metadata, pak_path: Path):
    cached_md5 = metadata.get("MD5", {}).get("value")
    return bool(cached_md5) and cached_md5 == get_md5(pak_path)

def _iter_pak_data_paths(list_output):
    for line in list_output.splitlines():
        normalized = line.replace("\\", "/")
        for match in _PAK_DATA_PATH_PATTERN.finditer(normalized):
            yield match.group(0), match.group(1), match.group(2)


def _has_script_extender_path(path):
    lower_path = path.casefold()
    return (
        "/scriptextender/" in lower_path
        or lower_path.endswith("/scriptextender/config.json")
        or lower_path.endswith("/script extender/config.json")
        or lower_path.endswith("/bootstrapserver.lua")
        or lower_path.endswith("/bootstrapclient.lua")
        or "/lua/" in lower_path
    )

def check_override_pak(pak_path, module_info_node):
    cache_key = str(pak_path)
    signature = get_file_signature(pak_path)
    cache = getattr(check_override_pak, "cache", None)
    if cache is not None:
        cached = cache.get(cache_key)
        if cached and cached.get("Signature") == signature:
            return cached.get("Result", {
                "Override": False,
                "LoadOrder": False,
                "HasOsiris": False,
                "HasScriptExtender": False,
            })

    try:
        result = subprocess.run(
            [str(divine_file), "-a", "list-package", "-g", "bg3", "-s", str(pak_path)],
            creationflags=_CREATE_NO_WINDOW,
            capture_output=True,
            text=True,
            check=True,
        )
        list_output = result.stdout
        override = {
            "Override": False,
            "LoadOrder": False,
            "HasOsiris": False,
            "HasScriptExtender": False,
        }
        folder_name = None
        if module_info_node is not None:
            folder_element = module_info_node.find(".//attribute[@id='Folder']")
            if folder_element is not None:
                folder_name = folder_element.attrib.get("value")

        for line in list_output.splitlines():
            normalized = line.replace("\\", "/")
            lower_normalized = normalized.casefold()
            if "/story/rawfiles/goals" in lower_normalized:
                override["HasOsiris"] = True
            if _has_script_extender_path(normalized):
                override["HasScriptExtender"] = True

        for path, _root_folder, mod_folder in _iter_pak_data_paths(list_output):
            if folder_name and mod_folder == folder_name and not path.lower().endswith("/meta.lsx"):
                override["LoadOrder"] = True
                continue

            if any(ignored in path for ignored in _IGNORED_PATHS):
                continue

            if mod_folder in _BUILTIN_MODULE_FOLDERS:
                override["Override"] = True

        if not getattr(check_override_pak, "cache", None):
            check_override_pak.cache = {}
        cache_entry = {"Signature": signature, "Result": override}
        check_override_pak.cache[cache_key] = cache_entry
        return override
    except Exception as e:
        print(f"Error checking override status: {e}")
        return False

def _metadata_result(mod_name, file_name, metadata):
    return {"modName": mod_name, "file": file_name, "metadata": metadata}


def _override_metadata_without_meta(pak_path):
    override_result = check_override_pak(pak_path, None)
    meta_data = dict(override_result) if isinstance(override_result, dict) else {}
    meta_data["Override"] = True
    meta_data["LoadOrder"] = False
    meta_data["MD5"] = {"value": get_md5(pak_path), "type": "LSString"}
    return meta_data


def _get_metadata(mod_name, file, profile_path, refresh_cache=False, mod=None):
    with cache_lock:
        if not refresh_cache:
            files = _read_mod_metadata_cache(_mod_root_from_pak(file))
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
            meta_data = _override_metadata_without_meta(file)
            with cache_lock:
                _write_mod_metadata_cache(file, meta_data, mod)
            return _metadata_result(mod_name, file.name, meta_data)

        tree = ET.parse(str(meta_files[0]))
        module_info_node = tree.getroot().find(".//node[@id='ModuleInfo']")
        if module_info_node is None:
            meta_data = _override_metadata_without_meta(file)
            with cache_lock:
                _write_mod_metadata_cache(file, meta_data, mod)
            return _metadata_result(mod_name, file.name, meta_data)

        override_result = check_override_pak(file, module_info_node)
        meta_data = dict(override_result) if isinstance(override_result, dict) else {}
        for attr in _DEFAULT_ATTRIBUTES:
            el = module_info_node.find(f"./attribute[@id='{attr}']")
            if el is not None:
                meta_data[attr] = {"value": el.attrib.get("value"), "type": el.attrib.get("type", "LSString")}
                if attr == "MD5":
                    meta_data[attr]["value"] = get_md5(file)

        with cache_lock:
            _write_mod_metadata_cache(file, meta_data, mod)
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

