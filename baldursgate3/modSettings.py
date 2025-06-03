import os
import json
import subprocess
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from audioop import error
from pathlib import Path
from tabnanny import check
from xml.dom import minidom
import xml.etree.ElementTree as ET
import threading
import multiprocessing

import mobase # type: ignore
from PyQt6.QtCore import QDir, QFileInfo, QDirIterator, QFile, qDebug

divine_file = Path(__file__).resolve().parent / 'tools' / 'Divine.exe'

cache_lock = threading.Lock()

def generate_mod_settings(organizer: mobase.IOrganizer, modlist: mobase.IModList, profile: mobase.IProfile):
    _fix_modscache(organizer)
    
    mod_settings = {}
    
    Gustav_Dev = {
       "GustavDev": {
            "Folder": {"value": "GustavDev", "type": "LSString"},
            "MD5": {"value": "", "type": "LSString"},
            "Name": {"value": "GustavDev", "type": "LSString"},
            "PublishHandle": {"value": "0", "type": "uint64"},
            "UUID": {"value": "28ac9ce2-2aba-8cda-b3b5-6e922f71b6b8", "type": "guid"},
            "Version64": {"value": "145100779997082619", "type": "int64"},
        }
    }
    
    mod_settings["GustavDev"] = Gustav_Dev

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
            
            meta_data = future.result()
            if meta_data:
                try:
                    print(f"Successfully processed mod metadata")
                    if meta_data["metadata"] and not meta_data["metadata"].get("Override") or meta_data["metadata"] and meta_data["metadata"].get("Override") and meta_data["metadata"].get("LoadOrder"):
                        if meta_data["modName"] not in mod_settings and (int(modlist.state(meta_data["modName"]) / 2) % 2 != 0):
                            mod_settings[meta_data["modName"]] = {}
                        mod_settings[meta_data["modName"]][meta_data["file"]] = meta_data["metadata"]
                        
                except Exception as e:
                    print(f"Error processing file: {str(e)}")
                
    mod_settings_file = Path(organizer.profile().absolutePath()) / "modsettings.lsx"
    
    root = ET.Element("save")
    version = ET.SubElement(root, "version")
    version.set("major", "4")
    version.set("minor", "7")
    version.set("revision", "1")
    version.set("build", "300")
    
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
    
    gustav_data = mod_settings.get("GustavDev", {}).get("GustavDev", {})
    if gustav_data:
        gustav_node = ET.SubElement(mods_children, "node")
        gustav_node.set("id", "ModuleShortDesc")
        for attr_id, attr_data in gustav_data.items():
            attribute = ET.SubElement(gustav_node, "attribute")
            attribute.set("id", attr_id)
            attribute.set("type", attr_data["type"])
            attribute.set("value", str(attr_data["value"]))

    if "GustavDev" in mod_settings:
        del mod_settings["GustavDev"]
    
    for modName in modlist.allModsByProfilePriority():
        if modlist.state(modName) and (int(modlist.state(modName) / 2) % 2 != 0):
            mod_data = mod_settings.get(modName, {})
            for file_name, metadata in sorted(mod_data.items(), key=lambda x: x[0]):
                mod_order_node = ET.SubElement(mods_order_node, "children")
                mod_order_node.set("id", "Module")
                attribute = ET.SubElement(mod_order_node, "attribute")
                attribute.set("id", "UUID")       
                attribute.set("value", metadata.get("UUID", {}).get("value", ""))
                attribute.set("type", metadata.get("UUID", {}).get("type", ""))
    
                mod_node = ET.SubElement(mods_children, "node")
                mod_node.set("id", "ModuleShortDesc")
                for attr_id, attr_data in metadata.items():
                    if attr_id == "Override" or attr_id == "LoadOrder":
                        continue
                    attribute = ET.SubElement(mod_node, "attribute")
                    attribute.set("id", attr_id)
                    attribute.set("type", attr_data["type"])
                    attribute.set("value", str(attr_data["value"]))
    
    xml_str = ET.tostring(root, encoding='unicode')
    # xml_str = ET.tostring(root, encoding='utf-8').decode('utf-8')
    dom = minidom.parseString(xml_str)
    formatted_xml = dom.toprettyxml(indent="  ", encoding='UTF-8')
    
    with open(mod_settings_file, 'wb') as f:
        f.write(formatted_xml)
    
    return True

def mod_installed(organizer: mobase.IOrganizer, modlist: mobase.IModList, profile: mobase.IProfile, mod: str):
    cache_json_path = Path(profile.absolutePath()) / "modsCache.json"
    modName = mod.name()
    
    if cache_json_path:
        with open(cache_json_path, "r", encoding="utf-8") as f:
            mods_cache = json.load(f)
            
            mod_data = mods_cache.get(modName)
            if not mod_data:
                print("Generating metadata for mod")
                
                max_workers = min(multiprocessing.cpu_count(), 16)
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = []
                    mod_path = Path(modlist.getMod(modName).absolutePath()) / "PAK_FILES"
                    mod_files = list(mod_path.glob("*.pak"))
                    for file in mod_files:
                        futures.append(
                            executor.submit(_get_metadata, modName, file, profile.absolutePath())
                        )
                        
                for future in as_completed(futures):
                    try:
                        meta_data = future.result()
                        if meta_data:
                            print(f"Successfully processed mod metadata")
                            if modName not in mods_cache:
                                mods_cache[modName] = {"Files": {}}
                            mods_cache[modName]["Files"][meta_data["file"]] = meta_data["metadata"]
                            
                    except Exception as e:
                        print(f"Error processing file: {str(e)}")
            else:
                mod_path = Path(modlist.getMod(modName).absolutePath()) / "PAK_FILES"
                mod_files = list(mod_path.glob("*.pak"))
                existing_files = mod_data.get("Files", {})
                
                max_workers = min(multiprocessing.cpu_count(), 16)
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = []
                    for file in mod_files:
                        if file.name not in existing_files:
                            futures.append(
                                executor.submit(_get_metadata, modName, file, profile.absolutePath())
                            )
                            
                for future in as_completed(futures):
                    try:
                        meta_data = future.result()
                        if meta_data:
                            print(f"Successfully processed new mod metadata")
                            mods_cache[modName]["Files"][meta_data["file"]] = meta_data["metadata"]
                            
                    except Exception as e:
                        print(f"Error processing file: {str(e)}")
                
            print(mods_cache.get(modName))
            return True      
    else:
        return False

def _extract_pak(file):

    temp_dir = Path(__file__).resolve().parent / 'temp_extracted'
    temp_dir.mkdir(parents=True, exist_ok=True)

    import hashlib
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

    result = subprocess.run(
        command,
        creationflags=subprocess.CREATE_NO_WINDOW,
        check=True
    )
    
    if result.returncode != 0:
        return None
    
    return output_dir

def check_override_pak(pak_path, module_info_node):
    cache_key = str(pak_path)
    
    if hasattr(check_override_pak, 'cache') and cache_key in check_override_pak.cache:
        return check_override_pak.cache[cache_key]
    
    if not hasattr(check_override_pak, 'cache'):
        check_override_pak.cache = {}
    
    command = [
        str(divine_file),
        "-a", "list-package",
        "-g", "bg3",
        "-s", str(pak_path),
    ]
    
    try:
        result = subprocess.run(
            command,
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        
        if result.returncode != 0:
            return False
            
        list_package_output = result.stdout
        
        ignored_paths = [
            'Game/GUI/Assets',
            'ScriptExtender'
        ]
        
        builtin_folders = [
            'Public/'
            'Public/Shared/',
            'Public/SharedDev/',
            'Public/Gustav/',
            'Public/GustavX/',
            'Public/GustavDev/',
            'Public/MainUI/',
            'Public/CrossplayUI'
            'Public/PhotoMode'
            'Public/ModBrowser/',
            'Public/DiceSet_01/',
            'Public/DiceSet_02/',
            'Public/DiceSet_03/',
            'Public/DiceSet_04/',
            'Public/DiceSet_06/',
            'Public/Honour/',
            'Public/HonourX/',
            'Public/Engine/',
            'Public/Game/',
            'Public/FW3/'
        ]
        
        override = {
            "Override": False,
            "LoadOrder": False
        }
        
        if module_info_node is not None:
            folder_element = module_info_node.find(".//attribute[@id='Folder']")
            if folder_element is not None:
                folder_name = folder_element.attrib['value']
                mods_folder_path = f"Mods/{folder_name}"
                
                public_folder_path = f"Public/{folder_name}"
                
                if public_folder_path in list_package_output:
                    override["LoadOrder"] = True
                
                files_in_folder = [
                    line.strip() 
                    for line in list_package_output.splitlines()
                    if mods_folder_path in line
                ]
                
                if files_in_folder and len(files_in_folder) > 1:
                    override["LoadOrder"] = True
                 
        for line in list_package_output.splitlines():
            if any(ignored in line for ignored in ignored_paths):
                continue
            if any(folder in line for folder in builtin_folders):
                 override["Override"] = True     
                 if public_folder_path in list_package_output:
                    override["LoadOrder"] = True
                 
                 return override
                    
        override["Override"] = False
        
        check_override_pak.cache[cache_key] = override
        return override
        
    except Exception as e:
        print(f"Error checking override status: {str(e)}")
        return False

def _get_metadata(modName, file, profile_path):
    _default_attributes = ["Folder", "MD5", "Name", "PublishHandle", "UUID", "Version64", "Version"]
    cache_json_path = os.path.join(profile_path, "modsCache.json")

    with cache_lock:
        if os.path.exists(cache_json_path):
            with open(cache_json_path, "r", encoding="utf-8") as f:
                mods_cache = json.load(f)
                if modName in mods_cache and file.name in mods_cache[modName].get("Files", {}):
                    cached_data = mods_cache[modName]["Files"][file.name]
                    return {"modName": modName, "file": file.name, "metadata": cached_data}
        else:
            mods_cache = {}
            if not mods_cache.get(modName):
                mods_cache[modName] = {"Files": {}}
    
    file_str = file.name
    extracted_pak = None
    
    try:
        meta_data = {}
        extracted_pak = _extract_pak(file)
        if extracted_pak:
            meta_files = list(extracted_pak.glob("**/meta.lsx"))
            
            if not meta_files:
                print(f"No meta.lsx files found in extracted PAK: {file.name}")
                return {"modName": modName, "file": file.name, "metadata": {}}
            
            meta_file = str(meta_files[0])
            print(f"Found meta.lsx at: {meta_file}")
            
            try:
                tree = ET.parse(meta_file)
                root = tree.getroot()
                module_info_node = root.find(".//node[@id='ModuleInfo']")
                
                if module_info_node is None:
                    print(f"No ModuleInfo node found in {meta_file}")
                    return {"modName": modName, "file": file.name, "metadata": {}}
                
                meta_data.update(check_override_pak(file, module_info_node))

                for attribute in _default_attributes:
                    element = module_info_node.find(f"./attribute[@id='{attribute}']")
                    if element is not None:
                        meta_data[attribute] = {
                            'value': element.attrib['value'],
                            'type': element.attrib.get('type')
                        }
            except ET.ParseError as e:
                print(f"Error parsing XML in {meta_file}: {str(e)}")
                return {"modName": modName, "file": file.name, "metadata": {}}

        with cache_lock:
            try:
                with open(cache_json_path, "r", encoding="utf-8") as f:
                    mods_cache = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                mods_cache = {}
            
            if not mods_cache.get(modName):
                mods_cache[modName] = {"Files": {}}
            
            mods_cache[modName]["Files"][file_str] = meta_data
            
            with open(cache_json_path, "w", encoding="utf-8") as f:
                json.dump(mods_cache, f, indent=4, ensure_ascii=False)

        return {"modName": modName, "file": file.name, "metadata": meta_data}
    except Exception as e:
        print(f"Error in _get_metadata for {file.name}: {str(e)}")
        return {"modName": modName, "file": file.name, "metadata": {}}
    finally:
        if extracted_pak and os.path.exists(extracted_pak):
            try:
                shutil.rmtree(extracted_pak)
            except Exception as e:
                print(f"Error cleaning up temp directory {extracted_pak}: {str(e)}")

def _fix_modscache(organizer: mobase.IOrganizer):
    try:
        profile_path = Path(organizer.profile().absolutePath())
        cache_json_path = profile_path / "modsCache.json"

        if not cache_json_path.exists():
            print(f"{cache_json_path} does not exist. Exiting.")
            return True

        with open(cache_json_path, "r", encoding="utf-8") as f:
            mods_cache = json.load(f)

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

            current_files = set(file.name for file in mod_path.glob("*.pak"))
            cached_files = set(mod_data.get("Files", {}).keys())

            missing_files = cached_files - current_files
            for missing_file in missing_files:
                print(f"Removing missing file {missing_file} from {mod_name}.")
                del mod_data["Files"][missing_file]

            if not mod_data["Files"]:
                mods_to_remove.append(mod_name)

        for mod_name in mods_to_remove:
            print(f"Removing mod {mod_name} from mods cache.")
            del mods_cache[mod_name]

        with open(cache_json_path, "w", encoding="utf-8") as f:
            json.dump(mods_cache, f, indent=4, ensure_ascii=False)

        print("Successfully fixed mods cache.")
        return True

    except Exception as e:
        print(f"Failed to fix mods cache: {str(e)}")
