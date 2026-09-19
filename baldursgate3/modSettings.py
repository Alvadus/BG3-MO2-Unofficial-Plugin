import hashlib
import configparser
import multiprocessing
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote
import xml.etree.ElementTree as ET

import mobase  # type: ignore

try:
    from . import lspk
except ImportError as e:
    lspk = None
    print(f"BG3: native pak reader unavailable, falling back to Divine ({e})")

divine_file = Path(__file__).resolve().parent / "tools" / "Divine.exe"
cache_lock = threading.Lock()
override_cache_lock = threading.Lock()
_override_cache = {}

_WRITE_RETRIES = 5
_WRITE_RETRY_DELAY = 0.4
_DEFAULT_ATTRIBUTES = ("Folder", "MD5", "Name", "PublishHandle", "UUID", "Version64", "Version")
_MODULE_ATTRIBUTE_TYPES = (
    ("Folder", "LSString"),
    ("MD5", "LSString"),
    ("Name", "LSString"),
    ("PublishHandle", "uint64"),
    ("UUID", "guid"),
    ("Version64", "int64"),
)
_DEFAULT_VERSION64 = "36028797018963968"
_PLACEHOLDER_VERSIONS = ("1", "268435456")
_CACHE_VERSION = 3
_IGNORED_PATHS = ("Game/GUI/Assets", "ScriptExtender")
_BUILTIN_MODULE_FOLDERS = frozenset({
    "Shared", "SharedDev", "Gustav", "GustavX", "GustavDev", "MainUI", "CrossplayUI",
    "PhotoMode", "ModBrowser", "DiceSet_01", "DiceSet_02", "DiceSet_03", "DiceSet_04",
    "DiceSet_05", "DiceSet_06", "DiceSet_07", "Honour", "HonourX", "Engine", "Game", "FW3",
})
_PAK_DATA_PATH_PATTERN = re.compile(r"^(Mods|Public)/([^/]+)/[^\t\r\n]+", re.IGNORECASE)
_ATTRIBUTE_VALUE_PATTERN = re.compile(r'value="(.*?)"', re.DOTALL)
_BARE_AMPERSAND_PATTERN = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")

try:
    _CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
except AttributeError:
    _CREATE_NO_WINDOW = 0


_UNREADABLE_ATTR = "_bg3_unreadable"

_native_reader_enabled = True
_native_stats_lock = threading.Lock()
_native_stats = {"native": 0, "divine": 0}
_fallback_lock = threading.Lock()
_fallback_reasons = []
_fallback_paks = set()
_unreadable_reasons = []
_unreadable_paks = set()
_FALLBACK_LIMIT = 50
_last_override_skip = 0


def last_override_skip():
    return _last_override_skip


def set_native_reader(enabled: bool):
    global _native_reader_enabled
    _native_reader_enabled = bool(enabled)


def native_reader_stats():
    with _native_stats_lock:
        return dict(_native_stats)


def _count_reader(kind):
    with _native_stats_lock:
        _native_stats[kind] = _native_stats.get(kind, 0) + 1


def _record_fallback(pak_name):
    with _fallback_lock:
        _fallback_paks.add(pak_name)
        if pak_name not in _fallback_reasons and len(_fallback_reasons) < _FALLBACK_LIMIT:
            _fallback_reasons.append(pak_name)


def take_fallback_reasons():
    with _fallback_lock:
        reasons = list(_fallback_reasons)
        _fallback_reasons.clear()
    return reasons


def fallback_pak_names():
    with _fallback_lock:
        return sorted(_fallback_paks)


def _record_unreadable(pak_name):
    with _fallback_lock:
        _unreadable_paks.add(pak_name)
        if pak_name not in _unreadable_reasons and len(_unreadable_reasons) < _FALLBACK_LIMIT:
            _unreadable_reasons.append(pak_name)


def take_unreadable_paks():
    with _fallback_lock:
        paks = list(_unreadable_reasons)
        _unreadable_reasons.clear()
    return paks


def unreadable_pak_names():
    with _fallback_lock:
        return sorted(_unreadable_paks)


class ModSettingsWriteError(Exception):
    pass


def _discard(path: Path):
    try:
        path.unlink()
    except OSError:
        pass


def _atomic_write(target: Path, render, retries=_WRITE_RETRIES):
    tmp = target.with_name(f"{target.name}.mo2tmp")
    last_error = None
    for attempt in range(retries):
        try:
            render(tmp)
            os.replace(tmp, target)
            return
        except OSError as e:
            last_error = e
            _discard(tmp)
        if attempt + 1 < retries:
            time.sleep(_WRITE_RETRY_DELAY)
    raise ModSettingsWriteError(f"could not write {target}: {last_error}") from last_error


def _write_config(target: Path, config):
    def render(tmp: Path):
        with tmp.open("w", encoding="utf-8") as f:
            config.write(f)

    _atomic_write(target, render)


def _write_config_best_effort(target: Path, config):
    if getattr(config, _UNREADABLE_ATTR, False):
        print(f"Refusing to overwrite unreadable {target}; fix or delete it manually.")
        return False
    try:
        _write_config(target, config)
        return True
    except ModSettingsWriteError as e:
        print(f"Skipping metadata cache update: {e}")
        return False


def write_text_atomically(target: Path, text):
    def render(tmp: Path):
        tmp.write_text(text, encoding="utf-8")

    _atomic_write(target, render)


def _attr_value(metadata, attr_id):
    attr_data = metadata.get(attr_id)
    if isinstance(attr_data, dict):
        value = attr_data.get("value")
        return "" if value is None else str(value)
    return ""


def _attr_type(metadata, attr_id):
    attr_data = metadata.get(attr_id)
    if isinstance(attr_data, dict):
        return str(attr_data.get("type") or "")
    return ""


def _version_to_version64(value, declared_type):
    number = _safe_int(value, -1)
    if number < 0:
        return ""
    if declared_type.casefold() == "int64" or number > 0xFFFFFFFF:
        return str(number)
    major = number >> 28
    minor = (number >> 24) & 0x0F
    revision = (number >> 16) & 0xFF
    build = number & 0xFFFF
    return str((major << 55) | (minor << 47) | (revision << 31) | build)


def _canonical_module_values(metadata):
    version64 = _attr_value(metadata, "Version64") or _attr_value(metadata, "Version")
    if version64 in _PLACEHOLDER_VERSIONS:
        version64 = _DEFAULT_VERSION64
    elif not _attr_value(metadata, "Version64"):
        version64 = _version_to_version64(version64, _attr_type(metadata, "Version"))
    return {
        "Folder": _attr_value(metadata, "Folder"),
        "MD5": _attr_value(metadata, "MD5"),
        "Name": _attr_value(metadata, "Name"),
        "PublishHandle": _attr_value(metadata, "PublishHandle") or "0",
        "UUID": _attr_value(metadata, "UUID"),
        "Version64": version64 or _DEFAULT_VERSION64,
    }


def _add_mod_order_entry(mods_order_children, metadata):
    if mods_order_children is None:
        return
    order_node = ET.SubElement(mods_order_children, "node", id="Module")
    attr = ET.SubElement(order_node, "attribute")
    attr.set("id", "UUID")
    attr.set("type", "FixedString")
    attr.set("value", _attr_value(metadata, "UUID"))


def _add_module_attributes(parent, metadata):
    values = _canonical_module_values(metadata)
    for attr_id, attr_type in _MODULE_ATTRIBUTE_TYPES:
        el = ET.SubElement(parent, "attribute")
        el.set("id", attr_id)
        el.set("type", attr_type)
        el.set("value", values[attr_id])


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
    except (ET.ParseError, OSError) as e:
        print(f"Could not read existing modsettings.lsx: {e}")
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


def is_builtin_module_folder(name):
    return name in _BUILTIN_MODULE_FOLDERS


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


def _new_meta_parser():
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    return config


def read_meta_ini(meta_ini: Path):
    config = _new_meta_parser()
    try:
        config.read(meta_ini, encoding="utf-8-sig")
    except (configparser.Error, OSError, UnicodeDecodeError) as e:
        print(f"Ignoring unreadable {meta_ini}: {e}")
        return None
    return config


def _read_mod_cache_config(mod_root: Path):
    config = read_meta_ini(_mod_cache_path(mod_root))
    if config is None:
        config = _new_meta_parser()
        setattr(config, _UNREADABLE_ATTR, True)
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
        try:
            config.read(candidate, encoding="utf-8-sig")
        except (configparser.Error, OSError, UnicodeDecodeError) as e:
            print(f"Ignoring unreadable {candidate}: {e}")
            continue
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
        if key in {"FileMD5", "CacheVersion"}:
            metadata[key] = value
            continue
        if "." in key:
            attr_id, attr_key = key.split(".", 1)
            if attr_key in {"value", "type"}:
                metadata.setdefault(attr_id, {})[attr_key] = value

    return metadata


def _serialize_metadata_section(section, pak_name, metadata):
    section.clear()
    section["CacheVersion"] = str(_CACHE_VERSION)
    section["Override"] = str(bool(metadata.get("Override", False))).lower()
    section["LoadOrder"] = str(bool(metadata.get("LoadOrder", False))).lower()
    section["HasOsiris"] = str(bool(metadata.get("HasOsiris", False))).lower()
    section["HasScriptExtender"] = str(bool(metadata.get("HasScriptExtender", False))).lower()
    section["FileMD5"] = str(metadata.get("FileMD5", ""))

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
    config = _read_mod_cache_config(mod_root)
    _seed_minimal_mod_meta(config, mod)
    section = _find_mod_cache_section(config, pak_path.name) or pak_path.name
    if not config.has_section(section):
        config.add_section(section)
    _serialize_metadata_section(config[section], pak_path.name, metadata)
    _write_config_best_effort(_mod_cache_path(mod_root), config)


def _write_minimal_mod_meta(mod_root: Path, mod):
    config = _read_mod_cache_config(mod_root)
    _seed_minimal_mod_meta(config, mod)
    _write_config_best_effort(_mod_cache_path(mod_root), config)


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
        _write_config_best_effort(_mod_cache_path(mod_root), config)

    cached_files = {}
    for file in mod_files:
        cache_key = _mod_cache_section(file.name)
        section = preferred_sections.get(cache_key) or _find_mod_cache_section(config, file.name)
        if section:
            cached_files[cache_key] = _deserialize_metadata_section(config[section])
    return cached_files


def _should_list_module(metadata, include_override_only):
    if not metadata.get("Override") or metadata.get("LoadOrder"):
        return True
    return include_override_only and bool(_attr_value(metadata, "UUID"))


def _collect_active_modules(modlist, profile, include_override_only):
    mod_settings = {}
    active_pak_count = 0
    active_module_count = 0
    stale_cache_entries = 0
    skipped_override_mods = set()

    max_workers = min(multiprocessing.cpu_count(), 16)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for modName in modlist.allModsByProfilePriority():
            if not _is_mod_active(modlist, modName):
                continue

            mod = modlist.getMod(modName)
            mod_root = Path(mod.absolutePath())
            for module_root in _loose_module_roots(mod_root):
                meta = _get_loose_module_metadata(modName, module_root)
                if meta and meta["metadata"]:
                    mod_settings.setdefault(modName, {})[meta["file"]] = meta["metadata"]
                    active_module_count += 1

            mod_path = mod_root / "PAK_FILES"
            if not mod_path.exists():
                continue
            mod_files = list(mod_path.glob("*.pak"))
            active_pak_count += len(mod_files)
            cached_files = _clean_mod_metadata_cache(mod_root, mod_files, mod)

            for file in mod_files:
                cache_key = _mod_cache_section(file.name)
                cached_metadata = cached_files.get(cache_key)
                if cached_metadata and _cache_is_current(cached_metadata):
                    if _should_list_module(cached_metadata, include_override_only):
                        mod_settings.setdefault(modName, {})[cache_key] = cached_metadata
                    else:
                        skipped_override_mods.add(modName)
                else:
                    if cached_metadata:
                        stale_cache_entries += 1
                    futures.append(
                        executor.submit(_get_metadata, modName, file, profile.absolutePath(), True, mod)
                    )

        if stale_cache_entries:
            print(f"Rebuilding outdated metadata cache for {stale_cache_entries} pak(s).")

        for future in as_completed(futures):
            try:
                meta = future.result()
                if meta and meta["metadata"]:
                    m = meta["metadata"]
                    if not _is_mod_active(modlist, meta["modName"]):
                        continue
                    if _should_list_module(m, include_override_only):
                        mod_settings.setdefault(meta["modName"], {})[meta["file"]] = meta["metadata"]
                    else:
                        skipped_override_mods.add(meta["modName"])
            except Exception as e:
                print(f"Error processing file: {e}")

    return {
        "modules": mod_settings,
        "paks": active_pak_count,
        "loose": active_module_count,
        "skipped": skipped_override_mods,
    }


def _is_loose_module_key(file_key):
    return file_key.endswith("/meta.lsx")


def _prefer_loose_modules(mod_settings):
    loose_uuids = {
        _attr_value(metadata, "UUID")
        for modules in mod_settings.values()
        for file_key, metadata in modules.items()
        if _is_loose_module_key(file_key)
    }
    loose_uuids.discard("")
    for mod_name, modules in mod_settings.items():
        duplicates = [
            file_key
            for file_key, metadata in modules.items()
            if not _is_loose_module_key(file_key) and _attr_value(metadata, "UUID") in loose_uuids
        ]
        for file_key in duplicates:
            print(f"Skipping {file_key} in {mod_name}: its mod is also installed as loose files.")
            del modules[file_key]
    return mod_settings


def _mod_settings_skeleton():
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
    return root, ET.SubElement(root_node, "children")


def _build_mod_settings_tree(modlist, mod_settings, fallback_modules):
    root, children = _mod_settings_skeleton()

    mods_order_children = None
    if mod_settings or fallback_modules:
        mods_order_node = ET.SubElement(children, "node")
        mods_order_node.set("id", "ModOrder")
        mods_order_children = ET.SubElement(mods_order_node, "children")

    mods_node = ET.SubElement(children, "node")
    mods_node.set("id", "Mods")
    mods_children = ET.SubElement(mods_node, "children")

    gustav_node = ET.SubElement(mods_children, "node", id="ModuleShortDesc")
    _add_module_attributes(gustav_node, _gustav_metadata())

    def add(metadata):
        _add_mod_order_entry(mods_order_children, metadata)
        mod_node = ET.SubElement(mods_children, "node", id="ModuleShortDesc")
        _add_module_attributes(mod_node, metadata)

    for mod_name in modlist.allModsByProfilePriority():
        if _is_mod_active(modlist, mod_name):
            for _file_name, metadata in sorted(mod_settings.get(mod_name, {}).items(), key=lambda x: x[0]):
                add(metadata)

    for metadata in fallback_modules:
        add(metadata)

    return root


def generate_mod_settings(
    organizer: mobase.IOrganizer,
    modlist: mobase.IModList,
    profile: mobase.IProfile,
    include_override_only=False,
):
    global _last_override_skip
    mod_settings_file = Path(profile.absolutePath()) / "modsettings.lsx"
    collected = _collect_active_modules(modlist, profile, include_override_only)
    mod_settings = _prefer_loose_modules(collected["modules"])

    fallback_modules = []
    if (collected["paks"] or collected["loose"]) and not mod_settings:
        fallback_modules = _read_existing_modsettings_modules(mod_settings_file)
        if not fallback_modules:
            print("Skipping modsettings.lsx write because no active pak metadata was available.")
            return False

    root = _build_mod_settings_tree(modlist, mod_settings, fallback_modules)
    _write_mod_settings_tree(mod_settings_file, root)
    _last_override_skip = len(collected["skipped"] - set(mod_settings))

    return True


def _write_mod_settings_tree(mod_settings_file: Path, root):
    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)

    def render(tmp: Path):
        tree.write(tmp, encoding="UTF-8", xml_declaration=True)

    _atomic_write(mod_settings_file, render)


def _minimal_mod_settings_root():
    return _build_mod_settings_tree(_EmptyModList(), {}, [])


class _EmptyModList:
    def allModsByProfilePriority(self):
        return []


def mod_settings_summary(profile: mobase.IProfile):
    mod_settings_file = Path(profile.absolutePath()) / "modsettings.lsx"
    try:
        root = ET.parse(mod_settings_file).getroot()
    except (ET.ParseError, OSError):
        return None
    modules = root.findall(".//node[@id='Mods']/children/node[@id='ModuleShortDesc']")
    order = root.findall(".//node[@id='ModOrder']/children/node[@id='Module']")
    return len(modules), len(order)


def ensure_mod_settings_exists(profile: mobase.IProfile):
    mod_settings_file = Path(profile.absolutePath()) / "modsettings.lsx"
    try:
        if mod_settings_file.stat().st_size > 0:
            ET.parse(mod_settings_file)
            return False
    except (OSError, ET.ParseError):
        pass

    try:
        _write_mod_settings_tree(mod_settings_file, _minimal_mod_settings_root())
    except ModSettingsWriteError as e:
        print(f"Could not create a placeholder modsettings.lsx: {e}")
        return False
    print("Wrote a placeholder modsettings.lsx (previous file was missing or corrupt).")
    return True

def _prepare_pak_refresh(modlist: mobase.IModList, mod, force_reparse=False):
    mod_name = _get_mod_name(mod)
    installed_mod = modlist.getMod(mod_name)
    if not installed_mod:
        return None

    mod_root = Path(installed_mod.absolutePath())
    mod_path = mod_root / "PAK_FILES"
    if not mod_path.exists():
        return None

    mod_files = list(mod_path.glob("*.pak"))
    if not mod_files:
        return None

    _write_minimal_mod_meta(mod_root, installed_mod)
    existing = _clean_mod_metadata_cache(mod_root, mod_files, installed_mod)

    to_refresh = []
    for f in mod_files:
        cached = existing.get(_mod_cache_section(f.name))
        if force_reparse or not (
            cached and _cache_is_current(cached) and cached_metadata_matches_file(cached, f)
        ):
            to_refresh.append((mod_name, installed_mod, f))
    return to_refresh


def _report(progress, done, total, label):
    if progress is None:
        return True
    return progress(done, total, label) is not False


def _run_pak_refresh(tasks, profile_path, progress=None):
    if not tasks:
        return 0, False

    max_workers = min(multiprocessing.cpu_count(), 16)
    total = len(tasks)
    done = 0
    cancelled = False

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_get_metadata, mod_name, file, profile_path, True, installed_mod): file
            for mod_name, installed_mod, file in tasks
        }
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"Error processing file: {e}")

            done += 1
            if not _report(progress, done, total, futures[future].name):
                cancelled = True
                for pending in futures:
                    pending.cancel()
                break

    return done, cancelled


def mod_installed(organizer: mobase.IOrganizer, modlist: mobase.IModList, profile: mobase.IProfile, mod, force_reparse=False):
    tasks = _prepare_pak_refresh(modlist, mod, force_reparse)
    if tasks is None:
        return False

    _run_pak_refresh(tasks, profile.absolutePath())
    return True


def _remove_pak_metadata_sections(mod_root: Path):
    config = _read_mod_cache_config(mod_root)
    removed = 0
    for section in list(config.sections()):
        if section.casefold().endswith(".pak"):
            if config.remove_section(section):
                removed += 1

    if removed and not _write_config_best_effort(_mod_cache_path(mod_root), config):
        return 0
    return removed


def clear_pak_metadata(modlist: mobase.IModList, mod_names=None, progress=None):
    target_names = list(mod_names) if mod_names is not None else list(modlist.allMods())
    touched_mods = 0
    removed_sections = 0
    cancelled = False

    for index, mod_name in enumerate(target_names):
        if not _report(progress, index, len(target_names), mod_name):
            cancelled = True
            break

        mod = modlist.getMod(mod_name)
        if not mod:
            continue

        removed = _remove_pak_metadata_sections(Path(mod.absolutePath()))
        if removed:
            touched_mods += 1
            removed_sections += removed

    return {"mods": touched_mods, "sections": removed_sections, "cancelled": cancelled}


def _escape_attribute_value(match):
    value = _BARE_AMPERSAND_PATTERN.sub("&amp;", match.group(1))
    return 'value="' + value.replace("<", "&lt;").replace(">", "&gt;") + '"'


def parse_meta_xml(data):
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        text = data.decode("utf-8-sig", "replace") if isinstance(data, bytes) else data
        repaired = _ATTRIBUTE_VALUE_PATTERN.sub(_escape_attribute_value, text)
        if repaired == text:
            raise
        try:
            return ET.fromstring(repaired)
        except ET.ParseError:
            raise error


def read_meta_xml(meta_path):
    return parse_meta_xml(Path(meta_path).read_bytes())


def _display_name_from_module_info(module_info_node):
    for attr in ("Name", "Folder"):
        el = module_info_node.find(f"./attribute[@id='{attr}']")
        value = (el.attrib.get("value") or "").strip() if el is not None else ""
        if value:
            return value
    return ""


def _native_display_name(pak_path: Path):
    if lspk is None or not _native_reader_enabled:
        return ""
    try:
        _paths, meta_xml = lspk.read_meta(pak_path)
        if meta_xml is None:
            return ""
        node = parse_meta_xml(meta_xml).find(".//node[@id='ModuleInfo']")
        return _display_name_from_module_info(node) if node is not None else ""
    except Exception as e:
        print(f"BG3: built-in reader could not read a name from {pak_path.name} ({e})")
        return ""


def read_pak_display_name(pak_path: Path):
    extracted_pak = None
    try:
        extracted_pak = _extract_pak(pak_path) if divine_file.is_file() else None
        if not extracted_pak:
            return _native_display_name(pak_path)
        meta_files = list(extracted_pak.glob("**/meta.lsx"))
        if not meta_files:
            return ""
        module_info_node = read_meta_xml(meta_files[0]).find(".//node[@id='ModuleInfo']")
        if module_info_node is None:
            return ""
        return _display_name_from_module_info(module_info_node)
    except Exception as e:
        print(f"Could not read a name from {pak_path.name}: {e}")
        return _native_display_name(pak_path)
    finally:
        if extracted_pak and extracted_pak.exists():
            try:
                shutil.rmtree(extracted_pak)
            except Exception:
                pass


def _managed_pak_names(modlist: mobase.IModList):
    managed = set()
    for mod_name in modlist.allMods():
        mod = modlist.getMod(mod_name)
        if not mod:
            continue
        pak_dir = Path(mod.absolutePath()) / "PAK_FILES"
        if pak_dir.is_dir():
            managed.update(f.name.casefold() for f in pak_dir.glob("*.pak"))
    return managed


def find_manual_mods(mods_dir: Path, modlist: mobase.IModList):
    if not mods_dir.is_dir():
        return []
    managed = _managed_pak_names(modlist)
    return [p for p in sorted(mods_dir.glob("*.pak")) if p.name.casefold() not in managed]


def _unique_mod_name(modlist: mobase.IModList, base_name):
    base_name = re.sub(r'[<>:"/\\|?*]', "_", base_name).strip() or "Imported mod"
    name = base_name
    counter = 2
    while modlist.getMod(name):
        name = f"{base_name} ({counter})"
        counter += 1
    return name


def import_manual_mods(organizer: mobase.IOrganizer, modlist: mobase.IModList, profile: mobase.IProfile, mods_dir, progress=None):
    candidates = find_manual_mods(Path(mods_dir), modlist)
    imported = 0
    skipped = 0
    cancelled = False

    for index, pak in enumerate(candidates):
        if not _report(progress, index, len(candidates), pak.name):
            cancelled = True
            break

        mod_name = _unique_mod_name(modlist, read_pak_display_name(pak) or pak.stem)
        try:
            mod = organizer.createMod(mobase.GuessedString(mod_name))
        except Exception as e:
            print(f"Could not create a mod for {pak.name}: {e}")
            skipped += 1
            continue

        if mod is None:
            skipped += 1
            continue

        target_dir = Path(mod.absolutePath()) / "PAK_FILES"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(pak), str(target_dir / pak.name))
        except OSError as e:
            print(f"Could not move {pak.name}: {e}")
            skipped += 1
            continue

        try:
            mod_installed(organizer, modlist, profile, mod_name, force_reparse=True)
        except Exception as e:
            print(f"Imported {pak.name} but could not read its metadata: {e}")

        try:
            modlist.setActive(mod_name, True)
        except Exception as e:
            print(f"Imported {mod_name} but could not enable it: {e}")

        imported += 1

    return {
        "found": len(candidates),
        "imported": imported,
        "skipped": skipped,
        "cancelled": cancelled,
    }


def regenerate_pak_metadata(organizer: mobase.IOrganizer, modlist: mobase.IModList, profile: mobase.IProfile, mod_names=None, progress=None):
    target_names = list(mod_names) if mod_names is not None else list(modlist.allMods())
    tasks = []
    regenerated = 0
    cancelled = False

    for index, mod_name in enumerate(target_names):
        if not _report(progress, index, len(target_names), f"Scanning {mod_name}"):
            cancelled = True
            break

        mod = modlist.getMod(mod_name)
        if not mod:
            continue

        mod_tasks = _prepare_pak_refresh(modlist, mod, force_reparse=True)
        if mod_tasks is None:
            continue

        tasks.extend(mod_tasks)
        regenerated += 1

    processed = 0
    if not cancelled:
        processed, cancelled = _run_pak_refresh(tasks, profile.absolutePath(), progress)

    return {"mods": regenerated, "paks": processed, "cancelled": cancelled}


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

    result = subprocess.run(
        command,
        creationflags=_CREATE_NO_WINDOW,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"exit code {result.returncode}"
        print(f"Divine could not extract {file.name}: {detail}")
        try:
            shutil.rmtree(output_dir)
        except OSError:
            pass
        return None
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
        while chunk := f.read(1 << 20):
            hasher.update(chunk)
    return hasher.hexdigest()

def get_file_signature(pak_path: Path):
    stat = pak_path.stat()
    return {
        "Size": stat.st_size,
        "Modified": stat.st_mtime_ns,
    }

def _cache_is_current(metadata):
    version = _safe_int(metadata.get("CacheVersion"), 0)
    if version >= _CACHE_VERSION:
        return True
    return version == 2 and not (metadata.get("Override") and not metadata.get("LoadOrder"))


def cached_metadata_matches_file(metadata, pak_path: Path):
    cached_md5 = metadata.get("FileMD5")
    return bool(cached_md5) and cached_md5 == get_md5(pak_path)

def _iter_pak_data_paths(lines):
    if isinstance(lines, str):
        lines = lines.splitlines()
    for line in lines:
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

def _default_override():
    return {
        "Override": False,
        "LoadOrder": False,
        "HasOsiris": False,
        "HasScriptExtender": False,
    }


def _override_flags_from_paths(paths, module_info_node):
    override = _default_override()

    folder_name = None
    if module_info_node is not None:
        folder_element = module_info_node.find(".//attribute[@id='Folder']")
        if folder_element is not None:
            folder_name = folder_element.attrib.get("value")

    for line in paths:
        normalized = line.replace("\\", "/")
        if "/story/rawfiles/goals" in normalized.casefold():
            override["HasOsiris"] = True
        if _has_script_extender_path(normalized):
            override["HasScriptExtender"] = True

    for path, _root_folder, mod_folder in _iter_pak_data_paths(paths):
        if mod_folder not in _BUILTIN_MODULE_FOLDERS:
            lower_path = path.casefold()
            if folder_name and not lower_path.endswith("/meta.lsx") and "scriptextender/config.json" not in lower_path:
                override["LoadOrder"] = True
            continue

        if any(ignored in path for ignored in _IGNORED_PATHS):
            continue

        override["Override"] = True

    return override


def check_override_pak(pak_path, module_info_node, paths=None):
    cache_key = str(pak_path)
    signature = get_file_signature(pak_path)
    with override_cache_lock:
        cached = _override_cache.get(cache_key)
    if cached is not None:
        if cached.get("Signature") == signature:
            return cached.get("Result", _default_override())

    try:
        if paths is None:
            result = subprocess.run(
                [str(divine_file), "-a", "list-package", "-g", "bg3", "-s", str(pak_path)],
                creationflags=_CREATE_NO_WINDOW,
                capture_output=True,
                text=True,
                check=True,
            )
            paths = result.stdout.splitlines()

        override = _override_flags_from_paths(paths, module_info_node)
        with override_cache_lock:
            _override_cache[cache_key] = {"Signature": signature, "Result": override}
        return override
    except Exception as e:
        print(f"Error checking override status: {e}")
        return False

def _metadata_result(mod_name, file_name, metadata):
    return {"modName": mod_name, "file": file_name, "metadata": metadata}


def _loose_module_roots(mod_root: Path):
    candidates = [
        mod_root / "Mods",
        mod_root / "Root" / "Data" / "Mods",
    ]
    seen = set()
    for mods_root in candidates:
        if not mods_root.exists():
            continue
        for module_root in mods_root.iterdir():
            if not module_root.is_dir() or module_root.name in _BUILTIN_MODULE_FOLDERS:
                continue
            key = module_root.resolve()
            if key in seen:
                continue
            seen.add(key)
            if (module_root / "meta.lsx").exists():
                yield module_root


def _has_loose_osiris(module_root: Path):
    for path in module_root.rglob("*"):
        if path.is_file() and "/story/rawfiles/goals/" in path.as_posix().casefold():
            return True
    return False


def _has_loose_script_extender(module_root: Path):
    for path in module_root.rglob("*"):
        normalized = path.as_posix()
        if _has_script_extender_path(normalized):
            return True
    return False


def _get_loose_module_metadata(mod_name, module_root: Path):
    try:
        meta_path = module_root / "meta.lsx"
        module_info_node = read_meta_xml(meta_path).find(".//node[@id='ModuleInfo']")
        if module_info_node is None:
            return _metadata_result(mod_name, module_root.name, {})

        meta_data = {
            "Override": False,
            "LoadOrder": True,
            "HasOsiris": _has_loose_osiris(module_root),
            "HasScriptExtender": _has_loose_script_extender(module_root),
        }
        for attr in _DEFAULT_ATTRIBUTES:
            el = module_info_node.find(f"./attribute[@id='{attr}']")
            if el is not None:
                meta_data[attr] = {"value": el.attrib.get("value"), "type": el.attrib.get("type", "LSString")}

        if "Folder" not in meta_data:
            meta_data["Folder"] = {"value": module_root.name, "type": "LSString"}
        if "Name" not in meta_data:
            meta_data["Name"] = {"value": module_root.name, "type": "FixedString"}
        if "MD5" not in meta_data:
            meta_data["MD5"] = {"value": "", "type": "LSString"}

        uuid = meta_data.get("UUID", {}).get("value")
        if not uuid:
            return _metadata_result(mod_name, module_root.name, {})
        return _metadata_result(mod_name, f"{module_root.name}/meta.lsx", meta_data)
    except Exception as e:
        print(f"Error reading loose mod metadata from {module_root}: {e}")
        return _metadata_result(mod_name, module_root.name, {})


def _override_metadata_without_meta(pak_path):
    override_result = check_override_pak(pak_path, None)
    meta_data = dict(override_result) if isinstance(override_result, dict) else {}
    meta_data["Override"] = True
    meta_data["LoadOrder"] = False
    meta_data["FileMD5"] = get_md5(pak_path)
    return meta_data


def _metadata_from_module_info(pak_path, module_info_node, paths):
    override_result = check_override_pak(pak_path, module_info_node, paths)
    meta_data = dict(override_result) if isinstance(override_result, dict) else {}
    for attr in _DEFAULT_ATTRIBUTES:
        el = module_info_node.find(f"./attribute[@id='{attr}']")
        if el is not None:
            meta_data[attr] = {
                "value": el.attrib.get("value"),
                "type": el.attrib.get("type", "LSString"),
            }
    meta_data["FileMD5"] = get_md5(pak_path)
    return meta_data


def _native_metadata(file):
    if lspk is None or not _native_reader_enabled:
        return None

    try:
        paths, meta_xml = lspk.read_meta(file)
    except Exception as e:
        print(f"BG3: built-in reader could not read {file.name} either ({e})")
        return None

    if meta_xml is None:
        override_result = check_override_pak(file, None, paths)
        meta_data = dict(override_result) if isinstance(override_result, dict) else {}
        meta_data["Override"] = True
        meta_data["LoadOrder"] = False
        meta_data["FileMD5"] = get_md5(file)
        return meta_data

    try:
        module_info_node = parse_meta_xml(meta_xml).find(".//node[@id='ModuleInfo']")
    except ET.ParseError as e:
        print(f"BG3: meta.lsx in {file.name} is not valid XML ({e})")
        return None
    if module_info_node is None:
        return None

    return _metadata_from_module_info(file, module_info_node, paths)


def _divine_metadata(file):
    if not divine_file.is_file():
        return None
    extracted_pak = None
    try:
        extracted_pak = _extract_pak(file)
        if not extracted_pak:
            return None
        meta_files = list(extracted_pak.glob("**/meta.lsx"))
        if not meta_files:
            print(f"No meta.lsx files found in extracted PAK: {file.name}")
            return _override_metadata_without_meta(file)
        module_info_node = read_meta_xml(meta_files[0]).find(".//node[@id='ModuleInfo']")
        if module_info_node is None:
            return _override_metadata_without_meta(file)
        return _metadata_from_module_info(file, module_info_node, None)
    except Exception as e:
        print(f"BG3: Divine could not read {file.name} ({e})")
        return None
    finally:
        if extracted_pak and extracted_pak.exists():
            try:
                shutil.rmtree(extracted_pak)
            except Exception as e:
                print(f"Error cleaning up {extracted_pak}: {e}")


def _get_metadata(mod_name, file, profile_path, refresh_cache=False, mod=None):
    with cache_lock:
        if not refresh_cache:
            files = _read_mod_metadata_cache(_mod_root_from_pak(file))
            if file.name in files:
                return _metadata_result(mod_name, file.name, files[file.name])

    meta_data = _divine_metadata(file)
    if meta_data is not None:
        _count_reader("divine")
    else:
        try:
            meta_data = _native_metadata(file)
        except Exception as e:
            print(f"BG3: built-in reader failed on {file.name} ({e})")
            meta_data = None
        if meta_data is None:
            _record_unreadable(file.name)
            return _metadata_result(mod_name, file.name, {})
        _count_reader("native")
        _record_fallback(file.name)

    with cache_lock:
        _write_mod_metadata_cache(file, meta_data, mod)
    return _metadata_result(mod_name, file.name, meta_data)
