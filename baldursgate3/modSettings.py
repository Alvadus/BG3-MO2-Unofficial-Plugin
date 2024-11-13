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
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = []
        for modName in modlist.allModsByProfilePriority():
            if modlist.state(modName) & mobase.ModState.ACTIVE != 0:
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
                    qDebug(f"Successfully processed mod metadata: {meta_data}")
                    
                    if meta_data["metadata"] and not meta_data["metadata"].get("Override"):
                        if meta_data["modName"] not in mod_settings:
                            mod_settings[meta_data["modName"]] = {}
                        mod_settings[meta_data["modName"]][meta_data["file"]] = meta_data["metadata"]
                    
            except Exception as e:
                qDebug(f"Error processing file: {str(e)}")
                
    mod_settings_file = Path(organizer.profile().absolutePath()) / "modsettings.lsx"
    
    # Create XML structure
    root = ET.Element("save")
    version = ET.SubElement(root, "version")
    version.set("major", "4")
    version.set("minor", "7")
    version.set("revision", "1")
    version.set("build", "3")
    
    region = ET.SubElement(root, "region")
    region.set("id", "ModuleSettings")
    
    root_node = ET.SubElement(region, "node")
    root_node.set("id", "root")
    
    children = ET.SubElement(root_node, "children")
    
    mods_order_node = ET.SubElement(children, "node")
    mods_order_node.set("id", "ModOrder")
    
    mods_node = ET.SubElement(children, "node")
    mods_node.set("id", "Mods")
    
    mods_children = ET.SubElement(mods_node, "children")
    
    for modName, mod_data in mod_settings.items():
        for file_name, metadata in sorted(mod_data.items()):
            if modName != "GustavDev":
                mod_order_node = ET.SubElement(mods_order_node, "children")
                mod_order_node.set("id", "Module")
                attribute = ET.SubElement(mod_order_node, "attribute")
                attribute.set("id", "UUID")       
                attribute.set("value", metadata.get("UUID", {}).get("value", ""))
                attribute.set("type", metadata.get("UUID", {}).get("type", ""))
    
            mod_node = ET.SubElement(mods_children, "node")
            mod_node.set("id", "ModuleShortDesc")
            for attr_id, attr_data in metadata.items():
                # if attr_id != "Override":  # Skip override flag 
                attribute = ET.SubElement(mod_node, "attribute")
                attribute.set("id", attr_id)
                attribute.set("type", attr_data["type"])
                attribute.set("value", str(attr_data["value"]))
    
    xml_str = ET.tostring(root, encoding='unicode')
    dom = minidom.parseString(xml_str)
    formatted_xml = dom.toprettyxml(indent="  ", encoding='UTF-8')
    
    with open(mod_settings_file, 'wb') as f:
        f.write(formatted_xml)
    
    return True

def _extract_pak(file):

    temp_dir = Path(__file__).resolve().parent / 'temp_extracted'
    temp_dir.mkdir(parents=True, exist_ok=True)

    output_dir = temp_dir / file.name

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


def _get_metadata(modName, file, profile_path):
    _default_attributes = ["Folder", "MD5", "Name", "PublishHandle", "UUID", "Version64", "Version"]
    cache_json_path = os.path.join(profile_path, "modsCache.json")

    # Read cache with lock
    with cache_lock:
        if not os.path.exists(cache_json_path):
            mods_cache = {}
        else:
            with open(cache_json_path, "r") as f:
                mods_cache = json.load(f)
        
        if not mods_cache.get(modName):
            print(f"No cache found for {modName}")
            mods_cache[modName] = {"Files": {}}
    
    file_str = file.name
    extracted_pak = None
    
    try:
        meta_data = {}
        extracted_pak = _extract_pak(file)
        if extracted_pak:
            meta_file = next((os.path.join(root, "meta.lsx") for root, _, files in os.walk(extracted_pak) if "meta.lsx" in files), None)
            if meta_file:
                tree = ET.parse(meta_file)
                root = tree.getroot()
                module_info_node = root.find(".//node[@id='ModuleInfo']")
                
                mod_folder = module_info_node.find(".//attribute[@id='Folder']").attrib['value']
                
                command = [
                    str(divine_file),
                    "-a", "list-package",
                    "-g", "bg3",
                    "-s", str(file),
                ]
                
                result = subprocess.run(
                    command,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=True
                )
                
                if result.returncode != 0:
                    return None
                
                list_package_output = result.stdout
                
                mod_folder_exists = f"Public/{mod_folder}" in list_package_output
                
                if not mod_folder_exists and any(f"Public/{mod_folder}" in list_package_output for folder in ["Game", "GUI"]) or not mod_folder_exists and any(f"Mods/{mod_folder}" in list_package_output for folder in ["MainUI"]):
                    meta_data["Override"] = True

            for attribute in _default_attributes:
                element = module_info_node.find(f"./attribute[@id='{attribute}']")
                if element is not None:
                    meta_data[attribute] = {
                        'value': element.attrib['value'],
                        'type': element.attrib.get('type')
                    }
            
            

        with cache_lock:
            with open(cache_json_path, "r") as f:
                mods_cache = json.load(f)
            
            if not mods_cache.get(modName):
                mods_cache[modName] = {"Files": {}}
            
            mods_cache[modName]["Files"][file_str] = meta_data
            
            with open(cache_json_path, "w") as f:
                json.dump(mods_cache, f, indent=4)

        return {"modName": modName, "file": file.name, "metadata": meta_data}

    finally:
        if extracted_pak and os.path.exists(extracted_pak):
            shutil.rmtree(extracted_pak)

def _fix_modscache(organizer: mobase.IOrganizer):
    try:
        profile_path = Path(organizer.profile().absolutePath())
        cache_json_path = profile_path / "modsCache.json"

        if not cache_json_path.exists():
            return True

        with open(cache_json_path, "r") as f:
            mods_cache = json.load(f)

        modlist = organizer.modList()
        installed_mods = {mod: True for mod in modlist.allMods()}
        
        mods_to_remove = []
        for mod_name in mods_cache:
            if mod_name not in installed_mods:
                mods_to_remove.append(mod_name)
        
        if mods_to_remove:
            for mod_name in mods_to_remove:
                del mods_cache[mod_name]
            
            with open(cache_json_path, "w") as f:
                json.dump(mods_cache, f, indent=4)
    except Exception as e:
        qDebug(f"Failed to fix mods cache: {str(e)}")
        return False
    return True