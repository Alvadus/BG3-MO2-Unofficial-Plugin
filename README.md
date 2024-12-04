
> [!IMPORTANT]
> [Visual C++ x64](https://aka.ms/vs/17/release/vc_redist.x64.exe), [.NET Runtime 8.X.X Desktop x64](https://dotnet.microsoft.com/en-us/download/dotnet/8.0), [.NET 6.0 Runtime Desktop x64](https://dotnet.microsoft.com/en-us/download/dotnet/thank-you/runtime-desktop-6.0.30-windows-x64-installer) are required for this plugin to work.

# Introduction
Unofficial Mod Organizer 2 Plugin that offers support for Baldur's Gate 3 modding.

> [!NOTE]
> Using this with [Root Builder](https://www.nexusmods.com/skyrimspecialedition/mods/31720) is recommended!

# Features
- Automatic generation of Mods Load Order.
- Support for mods that contain multiple .pak files.
- Support for [Norbyte's Baldur's Gate 3 Script Extender](https://github.com/Norbyte/bg3se) configuration mods.
- Overwrite generation for [Norbyte's Baldur's Gate 3 Script Extender](https://github.com/Norbyte/bg3se) configuration mods.

# Planned features in the future if possible:
- Support for [mod.io](https://mod.io/g/baldursgate3) mods, intended for [Wabbajack](https://www.wabbajack.org/) modlists.
- Support for WIP Toolkit unpacked mods.

# How to install?
Download the latest release of the plugin, extract all the files inside "\plugins\basic_games\games" in your Mod Organizer 2 directory, replace if you are updating.

# FAQ
- **When I click run, my Mod Organizer 2 freezes for a bit**
*That is normal, the plugin is generating a mods cache for all mods in your load order, the duration depends on the amount of mods you have, and it only happens once if the mods cache file was never generated.*
- **What is mods cache?**
*This plugin uses [LSLib](https://github.com/Norbyte/lslib) to extract metadata from .pak files, some specific data is needed in order to generate a load order, extracted mods are put in "\plugins\basic_games\games\baldursgate3\temp_extracted" before getting deleted, modsCache.json file is found inside your Mod Organizer 2 profile.*


# Credits
- Thanks to [Norbyte](https://github.com/Norbyte) for [LSLib](https://github.com/Norbyte/lslib), which allows this plugin to extract data from pak files in order to manage load order.


 
