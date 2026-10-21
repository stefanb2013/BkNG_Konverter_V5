"""Convert legacy Bk2 datapoint definitions into a BkNG ``global.var`` file.

The program has no third-party dependencies.  Run it with::

    python bkng_converter.py

Use the two buttons to select the Automation Studio project files (``.apj``) of
the old and new project, then click *Convert*. The selected new ``global.var`` is replaced.
The old project may also be a ZIP file; it is unpacked into a temporary folder.
Instead of an existing BkNG project, a zipped BkNG base project can be selected
together with a target folder; it is unpacked there as ``<source name>_AS6``.
Optionally, all tasks containing ladder diagrams (``.ld``) are copied from the
old into the new project, keeping their folder structure, and the X20 I/O
modules (without the old PLC) including their I/O mapping are copied into the
active hardware configuration of the new project.  The Ethernet settings of
the PLC (host name, IP mode, IP address, subnet mask, default gateway) can be
taken over as well, all Bk variables can be enabled in OPC UA (OpcUaBkV.uad), and a
VC4 user visu (uvis) can be converted into BkVisu mappView pages.

A BkNG project of Automation Studio 4 (with a program VisuTask) is migrated
instead: its customer-specific parts (declarations, tasks, libraries with source
code, users and roles, OPC UA variables and the customer visu with its bindings)
are taken over into the BkNG base project of Automation Studio 6, whose examples
are removed.  The target is always an Automation Studio 6 project.
"""

from __future__ import annotations

import hashlib
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
import uuid
import zipfile
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Iterable

RESOURCE_DIR = Path(__file__).parent
ICON_PATH = RESOURCE_DIR / "assets" / "icon.ico"


REQUIRED_OLD_FILES = ("dplist.dat", "gruppen.dat", "global.var")
# Files that identify the old project as a Bk2000 project.
BK2000_FILES = ("dplist.dat", "gruppen.dat")
PROJECT_FILE_SUFFIX = ".apj"
ZIP_SUFFIX = ".zip"
# Appended to the name of the source project (Bk2000 or BkNG) for a BkNG project unpacked from a ZIP file.
NEW_PROJECT_SUFFIX = "_AS6"
REQUIRED_VIS_UST_STATUS = "VisUstStat : ARRAY[0..7] OF BOOL;"
VARIABLE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z_][\w]*)\s*:\s*(?P<type>[A-Za-z_][\w.]*)\s*;"
    r"\s*(?P<comment>\(\*.*?\*\))?\s*$"
)
GENERIC_VARIABLE_RE = re.compile(r"^\s*(?P<name>[A-Za-z_][\w]*)\s*:\s*.+;\s*(?:\(\*.*?\*\)\s*)?$")
# A VAR section header, e.g. "VAR", "VAR_GLOBAL", "VAR CONSTANT" or "VAR RETAIN".
# Anchored start-to-end so it never matches an ordinary "name : type;" declaration.
VAR_SECTION_RE = re.compile(r"^\s*VAR(?:_[A-Za-z]+)?((?:\s+[A-Za-z]+)*)\s*$", re.IGNORECASE)
END_VAR_RE = re.compile(r"^\s*END_VAR\s*$", re.IGNORECASE)
# IEC 61131-3 qualifiers that change a section's semantics and must be preserved.
SECTION_QUALIFIER_KEYWORDS = ("CONSTANT", "RETAIN", "PERSISTENT")
DATAPOINT_RE = re.compile(r'^\s*"?\s*@(?P<name>[A-Za-z_][\w]*)\b')
GROUP_RE = re.compile(r'^\s*"?\s*\$(?P<name>[^"\r\n]+)')
BLOCK_RE = re.compile(r'^\s*"?\s*\$(?P<name>[A-Za-z][A-Za-z0-9_]*)\b')
TEXT_RE = re.compile(r'^\s*"?\s*~(?P<text>.*?)(?:"\s*,?\s*)?$')
ASSIGNMENT_RE = re.compile(
    r"(?P<key>[A-Za-z_][\w.]*)\s*=\s*"
    r"(?P<value>'[^']*'|\"[^\"]*\"|[^\s,;]+)"
)


@dataclass
class Parameter:
    key: str
    value: str


@dataclass
class Datapoint:
    name: str
    text: str = ""
    pab_parameters: list[Parameter] = field(default_factory=list)
    vis_parameters: list[Parameter] = field(default_factory=list)
    block_defaults: dict[str, list[Parameter]] = field(default_factory=dict)


@dataclass
class ConversionResult:
    target: Path
    converted: int
    copied: int
    unmatched: int
    warnings: list[str]
    ust_name: str | None = None
    ust_target: Path | None = None


@dataclass
class LadderCopyResult:
    tasks: list[Path]
    files_copied: int
    files_overwritten: int
    packages_updated: list[Path]
    deployed: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ladder_details: str = ""  # conversion of the old Ladder Diagrams into the format of AS6


@dataclass
class IoCopyResult:
    hardware: Path
    io_map: Path
    modules: list[str]
    modules_replaced: int
    mappings_copied: int
    warnings: list[str]
    # PROFIBUS devices copied with their GSD file, the copied description files and DTM folders.
    fieldbus_devices: list[str] = field(default_factory=list)
    descriptions: list[Path] = field(default_factory=list)
    dtm_folders: list[Path] = field(default_factory=list)


@dataclass
class EthernetCopyResult:
    hardware: Path
    cpu: str
    settings: list[tuple[str, str]]
    warnings: list[str]


@dataclass
class OpcUaResult:
    target: Path
    variables: list[str]
    replaced: int
    warnings: list[str]


class ConversionError(RuntimeError):
    """Raised when project files cannot be discovered or converted safely."""


# Called as chooser(filename, root, candidates) when a file is ambiguous; returns
# the selected path, or None if the user cancelled.
FileChooser = Callable[[str, Path, list[Path]], "Path | None"]


LANGUAGES = {"en": "English", "de": "Deutsch"}
TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "not_found": "Could not find '{filename}' below:\n{root}",
        "choice_cancelled": "No '{filename}' was selected; conversion cancelled.",
        "ambiguous": "More than one '{filename}' was found below {root}.\n"
                     "Please select a folder that contains exactly one copy:\n{choices}",
        "no_directories": "Both selections must be existing project directories.",
        "no_project_file": "Please select an Automation Studio project file (*.apj):\n{path}",
        "not_bk2000": "Not found in the Bk2000 project:\n{files}\n\nPlease select a Bk2000 project.\n{root}",
        "warn_no_group": "{name}: no group found; Gruppe was omitted.",
        "warn_unknown_members": "{type} has no element(s) {members}; the initializers in Global.var that use "
                                "them must be corrected (otherwise build error 1176).",
        "warn_unmatched": "{count} datapoint(s) in dplist.dat have no declaration in the old global.var.",
        "no_declarations": "No variable declarations were found in the old global.var.",
        "no_logical": "No 'Logical' folder was found in:\n{root}",
        "no_objects_section": "Cannot add '{child}' to {package}: no <Objects> section found.",
        "no_ladder_tasks": "No tasks with .ld files were found below:\n{root}",
        "choice_title": "Select {filename}",
        "config_title": "Select hardware configuration",
        "config_prompt": "The BkNG project has several hardware configurations:\n{root}\n\n"
                         "Please select the configuration to convert into:",
        "choice_prompt": "More than one '{filename}' was found below:\n{root}\n\nPlease select the file to use:",
        "ok": "OK",
        "cancel": "Cancel",
        "language": "Language",
        "warn_opcua_version": "{path} has FileVersion {version}; an Automation Studio 6 file (FileVersion 10 or "
                              "higher) is expected. Please check the BkNG base project.",
        "old_folder": "Source project (.apj/.zip)",
        "new_folder": "BkNG project (.apj)",
        "browse": "Browse…",
        "copy_ladder": "Copy all ladder diagram (.ld) tasks into the BkNG project",
        "convert": "Convert",
        "start_hint": "Note: the BkNG project must be an empty BkNG project (BkNG base project).\n"
                      "Select the project files (.apj) of the source project and the BkNG project, then click Convert.\n"
                      "A Bk2000 project is converted. A BkNG project of Automation Studio 4 (with VisuTask) is "
                      "migrated: its customer-specific parts (tasks, libraries, users, customer visu with bindings) "
                      "are taken over into the BkNG base project of Automation Studio 6.\n"
                      "The source project may also be a ZIP file; it is unpacked into a temporary folder.\n"
                      "Alternatively, select a zipped BkNG base project and a target folder; it is unpacked "
                      "there as <source name>_AS6.\n"
                      "A report with all open items (to do) is saved in the BkNG project folder.\n",
        "select_old": "Select Bk2000 or BkNG project",
        "select_new": "Select BkNG project",
        "project_files": "Automation Studio project",
        "source_files": "Automation Studio project or ZIP file",
        "no_source_zip": "Please select a ZIP file with a Bk2000 or BkNG project:\n{path}",
        "source_extracted": "Source project unpacked from {archive} into the temporary folder:\n{path}\n",
        "step_unpack_source": "Unpack source project",
        "progress_step": "Step {number} of {total}: {step}",
        "source_unpacking": "Unpacking the source project …",
        "progress_report": "Writing the report …",
        "using_file": "Using {filename}: {path}\n",
        "error": "ERROR: {error}\n",
        "conversion_failed": "Conversion failed",
        "summary": "Converted {converted} datapoint declaration(s); copied {copied} unchanged.\nWritten: {target}\n",
        "ladder_tasks": "Copied ladder tasks:",
        "ladder_summary": "Copied {tasks} ladder task(s) with {files} file(s) ({overwritten} overwritten); "
                          "updated {packages} Package.pkg file(s).\n",
        "no_physical": "No Automation Studio project (Physical/Physical.pkg) was found for:\n{root}",
        "no_configuration": "No hardware configuration was found in:\n{root}",
        "ambiguous_configuration": "The active configuration of the following project could not be determined:\n{root}",
        "no_cpu": "No PLC folder was found in the configuration:\n{root}",
        "cpu_not_in_hardware": "PLC '{cpu}' was not found in {path}.",
        "no_io_modules": "No X20 I/O modules connected to the PLC were found in {path}.",
        "io_name_is_cpu": "An I/O module of the Bk2000 project has the name of the BkNG PLC: '{name}'.",
        "invalid_hardware": "Invalid hardware file: {path}",
        "warn_io_target_skipped": "{module}: connection {connector} to '{target}' was not copied "
                                  "('{target}' is not an X20 I/O module).",
        "warn_io_device_not_copied": "{device} ({type}) is connected to {module} but is not an X20 module and was "
                                     "not copied; please add it manually.",
        "warn_io_connect_cpu": "{module}.{connector} was connected to {old_cpu}.{target_connector}; the I/O block was "
                               "inserted without PLC connection, please connect it to {cpu}.",
        "warn_io_name_clash": "{module} of the BkNG project is connected to '{target}', replaced by Bk2000 "
                              "modules of the same name; please check the topology.",
        "warn_mapping_skipped": "I/O mapping not copied (not an X20 I/O module): {line}",
        "copy_io": "Copy X20 I/O modules incl. I/O mapping into the active configuration",
        "io_summary": "Copied {modules} I/O module(s) ({replaced} replaced) and {mappings} I/O mapping(s) to:\n{hardware}\n",
        "io_fieldbus": "PROFIBUS device(s) with GSD file: {devices}\nDevice description(s) copied to {folder}: {files}\n",
        "io_dtm": "DTM configuration copied: {folders}\n",
        "warn_io_fieldbus_check": "PROFIBUS device {device} was taken over with its GSD file {file}; please open the "
                                  "device configuration (DTM) in Automation Studio and check it.",
        "warn_cpu_mapping": "CPU mapping taken over for {cpu}; please check that the channel exists on the new PLC: {line}",
        "warn_no_deployment": "Tasks were not added to a task class (Cpu.sw): {error}",
        "warn_task_not_deployed": "{task} is not assigned to a task class in the Bk2000 project.",
        "warn_task_name_in_use": "A different task named '{task}' already exists in the new Cpu.sw; it was not added.",
        "warn_task_class_missing": "Task class '{task_class}' does not exist in the new Cpu.sw; '{task}' was not added.",
        "ladder_deployed": "Added {count} task(s) to their task class in Cpu.sw.\n",
        "ladder_converted": "{count} Ladder Diagram file(s) converted into the format of Automation Studio 6 "
                            "(working version {version}).\n",
        "ladder_no_as6": "No Automation Studio 6 found; the Ladder Diagrams could not be converted automatically.",
        "ladder_as6_failed": "The Ladder Diagrams could not be converted with Automation Studio 6: {error}",
        "ladder_file_failed": "{file}: conversion into the format of Automation Studio 6 failed ({error}).",
        "ladder_no_compiler": "C# compiler of the .NET Framework not found ({path}).",
        "ladder_compile_failed": "The helper program for the Ladder Diagram conversion could not be compiled: {error}",
        "ust_written": "UST name '{name}' written to UST_Name in:\n{path}\n",
        "warn_no_visu_variables": "UST name not written: no VisuTask/Variables.var found below {root}.",
        "warn_ust_name_not_written": "UST name '{name}' was not written: no Variables.var selected.",
        "warn_no_ust_name_declaration": "UST name not written: no UST_Name declaration found in {path}.",
        "warn_ust_name_too_long": "UST name '{name}' is longer than STRING[{length}] and will be truncated by the PLC.",
        "copy_ethernet": "Take over the Ethernet settings of the PLC (IF2: IP address, subnet mask, host name, mode; "
                         "gateway; all settings of IF3)",
        "warn_no_interface": "The Bk2000 PLC '{cpu}' has no interface {interface}; it was left unchanged.",
        "copy_opcua": "Enable all Bk datapoints incl. their elements in OpcUaBkV.uad",
        "invalid_opcua": "Invalid OPC UA file (no <Module> found): {path}",
        "opcua_summary": "Enabled {count} Bk datapoint(s) in OPC UA ({replaced} replaced):\n{path}\n",
        "warn_opcua_unknown_type": "{name}: type {type} has no element list; not enabled in OPC UA.",
        "convert_visu": "Convert UVIS pages into BkVisu pages (experimental)",
        "no_user_visu": "The uvis program was found, but no VC4 visualisation with UVis_ pages below:\n{root}",
        "no_bkvisu": "The BkNG project has no BkVisu page folder:\n{path}",
        "invalid_mappview_file": "No <{section}> section found in {path}.",
        "visu_none": "No user visu (uvis.br or uvis sources) found in the Bk2000 project; nothing to convert.\n",
        "visu_summary": "Converted {pages} VC4 page(s) with {widgets} widget(s) and {bindings} binding(s):\n"
                        "{names}\nBindings: {binding_dir}\n",
        "visu_scaling": "VC4 resolution {width}x{height} (header {header} px) scaled to {target_width}x{target_height}.\n",
        "warn_visu_text_datapoint": "{page}/{control}: text from datapoint {datapoint} placed as empty label.",
        "warn_visu_navigation": "{page}/{control}: target page '{target}' is not converted; button left out.",
        "warn_visu_page_no_datapoints": "{page}: dplist.dat has no datapoints with #UVIS page={page_index}; the page "
                                        "was not converted and buttons to it were left out.",
        "warn_visu_not_dynamic": "{page}: {count} element(s) placed without dynamics: {items}.",
        "visu_unbound_unknown": "variable not declared in the Bk2000 project",
        "visu_unbound_pointer": "pointer, not available in OPC UA",
        "visu_unbound_missing": "variable missing in the BkNG project",
        "visu_unbound_undeployed": "task taken over but not in the Cpu.sw",
        "visu_tasks": "Tasks taken over for the bindings of the visu: {tasks}\n",
        "visu_task_not_deployed": " (not added to the Cpu.sw)",
        "visu_unbound_type": "data type not supported",
        "visu_unbound_key": "virtual key of the program, bind the button to a variable",
        "visu_unbound_bitmap": "bitmap not found",
        "warn_visu_no_expressions": "{page}: no Expressions package ({path}); widgets with a status or index datapoint "
                                    "are always visible.",
        "visu_dynamics": "{widgets} widget(s) bound to PLC variables (directly or through {expressions} expression(s) "
                         "for the visibility).\n",
        "warn_visu_no_widget": "{page}/{control}: widget {widget} is missing in BkVisuLib; placed as rectangle.",
        "warn_visu_no_datapoint": "{page}/{control}: no datapoint of type {type} with #UVIS page={page_index} "
                                  "idx={idx}; widget has no binding.",
        "warn_visu_no_name_datapoint": "{page}/{control}: no datapoint of type {type} with #UVIS page={page_index} "
                                       "idx={idx}; name label left out.",
        "warn_visu_no_session_variables": "No session variable file (*.svar) found below {path}; please add the BOOL "
                                          "session variable {variable} (default false) for the ktx switch.",
        "warn_visu_no_library": "BkVisuLib not found ({path}); Bk bitmaps placed as rectangles.",
        "visu_nav_summary": "Navigation buttons in {path}:\n{buttons}\n",
        "warn_visu_no_navigation": "BkV_NavigationContent not found below {path}; no navigation buttons added.",
        "warn_visu_no_navigation_bar": "{path} has no NavigationBar; no navigation buttons added.",
        "warn_visu_navigation_full": "The navigation bar has no room for the page(s) {pages}; no button added.",
        "no_ethernet_interface": "The Bk2000 PLC '{cpu}' has no Ethernet interface {interface}.",
        "ethernet_summary": "Ethernet settings of {cpu} taken over ({mode}) in:\n{hardware}\n",
        "ethernet_mode_manual": "fixed IP address",
        "warn_ethernet_source": "The Bk2000 PLC has no interface {interface} of its own; the settings of the Ethernet "
                                "interface {source} were taken over into {interface} of {cpu}. Please check.",
        "ethernet_mode_dhcp": "DHCP client",
        "warn_ethernet_mode": "{interface}: IP mode '{mode}' was taken over; further settings of this mode (e.g. DHCP leases) were not.",
        "old_group": "Source: Bk2000 project or BkNG project (AS4)",
        "new_group": "Target: BkNG project (AS6)",
        "new_mode_project": "Existing BkNG project",
        "new_mode_zip": "BkNG base project from ZIP file",
        "zip_file": "BkNG base project (.zip)",
        "zip_target": "Target folder",
        "select_zip": "Select zipped BkNG base project",
        "select_zip_target": "Select target folder for the BkNG project",
        "zip_files": "ZIP file",
        "no_zip_file": "Please select a ZIP file with a BkNG base project:\n{path}",
        "no_target_dir": "Please select an existing target folder:\n{path}",
        "target_exists": "The target project folder already exists:\n{path}\n\nPlease delete it or select another target folder.",
        "zip_no_project": "The ZIP file does not contain exactly one Automation Studio project "
                          "(.apj next to Physical/Physical.pkg):\n{path}",
        "zip_extracted": "BkNG base project unpacked to:\n{path}\n",
        "step_unpack": "Unpack BkNG base project",
        "step_analysis": "Analysis of the Bk2000 project",
        "step_data": "Data modules (.dat)",
        "copy_data": "Copy data modules (.dat) except dplist, gruppen and Bk2000 standard modules into the BkNG project",
        "data_none": "No data modules found apart from dplist, gruppen and the Bk2000 standard modules.\n",
        "data_summary": "Copied {objects} data module(s) with {files} file(s) ({overwritten} overwritten); "
                        "updated {packages} Package.pkg file(s).\nAdded {deployed} data module(s) to Cpu.sw.\n",
        "data_objects": "Copied data modules:",
        "warn_data_not_deployed": "Data module {name} is not deployed in the Cpu.sw of the Bk2000 project; "
                                  "it was copied but not added to Cpu.sw.",
        "warn_data_name_in_use": "The Cpu.sw of the BkNG project already contains an object named '{name}'; "
                                 "data module {source} was copied but not added to Cpu.sw.",
        "analysis_libraries": "Libraries of the Bk2000 project that are not standard libraries (B&R, Burklimat) ({path}):\n",
        "analysis_library": "- {name} ({language}){present}\n",
        "analysis_library_present": ", also in the BkNG project",
        "analysis_no_libraries": "All libraries of the Bk2000 project are standard libraries (B&R, Burklimat).\n",
        "analysis_ptop": "PtoP data module: {path}\n",
        "warn_customer_libraries": "Customer-specific libraries found: {names}. Check whether they are needed in "
                                   "the BkNG project; take them over manually or replace them by BkNG functions.",
        "warn_ptop": "PtoP coupling found ({path}). PtoP is discontinued; its tasks and libraries are not taken "
                     "over. Replace the coupling by OPC UA.",
        "warn_ptop_omitted": "PtoP is discontinued; the PtoP tasks {names} were not taken over. Replace the coupling "
                             "by OPC UA.",
        "application_ptop": "PtoP tasks not taken over (PtoP is discontinued): {names}\n",
        "step_global": "Datapoints (global.var)",
        "step_ladder": "Ladder diagram tasks",
        "step_io": "I/O configuration",
        "step_ethernet": "Ethernet settings",
        "step_opcua": "OPC UA",
        "step_visu": "User visu (UVIS)",
        "report_title": "BkNG Converter V4 - conversion report",
        "report_date": "Date",
        "report_old": "Source project",
        "report_new": "BkNG project",
        "report_result_ok": "Conversion complete, {todos} open item(s).",
        "report_result_error": "Conversion aborted in step '{step}'; the following steps were not performed.",
        "report_todo_heading": "TO DO",
        "report_no_todos": "No open items.",
        "report_error_todo": "ERROR: {error}\n    Fix the cause and run the conversion again "
                             "(preferably into a fresh BkNG base project).",
        "report_steps_heading": "STEPS PERFORMED",
        "report_file": "Conversion_report",
        "report_saved": "The report is saved in the BkNG project folder:\n{path}",
        "report_not_saved": "The report could not be saved:\n{error}",
        "report_open": "Open the report now?",
        "report_mode": "Mode",
        "mode_unknown": "",
        "mode_bk2000": "Bk2000 project: conversion into the BkNG project",
        "mode_migration": "BkNG project of Automation Studio {version}: migration into the BkNG base project (AS6)",
        "mode_error": "Project cannot be converted: {error}",
        "already_as6": "The project is already an Automation Studio {version} project; a migration is not needed:\n{path}",
        "migrate_application": "Take over tasks, data modules and customer libraries (with source code)",
        "migrate_users": "Take over users and roles (passwords must be entered again)",
        "migrate_opcua": "Enable the Bk datapoints and the further OPC UA variables of the old project in OpcUaBkV.uad",
        "migrate_visu": "Take over the customer visu (BkVisu pages, bindings, event bindings, media, texts)",
        "convert_ladder_as6": "Convert old Ladder Diagrams into the AS6 format with Automation Studio 6 "
                              "(Automation Studio 6 must be installed)",
        "ladder_as6_title": "Convert Ladder Diagrams with Automation Studio 6",
        "ladder_as6_info": "This option requires Automation Studio 6 to be installed on this computer. The converter "
                           "uses its Ladder Diagram conversion (internal functions of Automation Studio); whether "
                           "this complies with the license terms of B&R must be checked by the user.\n\n"
                           "Without Automation Studio 6 the Ladder Diagrams stay in the old format; the report "
                           "lists them to be opened and saved once in Automation Studio 6.\n\n{status}",
        "ladder_as6_found": "Automation Studio 6 found:\n{path}",
        "ladder_as6_not_found": "Automation Studio 6 was not found on this computer.",
        "step_migration_analysis": "Analysis of the BkNG project",
        "step_cleanup": "Remove examples and test pages of the base project",
        "step_global_migration": "Global declarations (Global.var, Global.typ)",
        "step_visu_task": "VisuTask settings",
        "step_application": "Tasks and data modules",
        "step_libraries": "Libraries",
        "step_users": "Users and roles",
        "step_visu_migration": "Customer visu (mappView)",
        "migration_analysis": "BkNG project of Automation Studio {version}, configuration {old}; "
                              "target configuration: {new}.\n",
        "cleanup_summary": "Removed pages: {pages}\nRemoved programs: {programs}\n"
                           "Removed bindings to removed contents: {bindings} (deleted files: {files})\n",
        "global_merged": "{count} declaration(s) of the old project added to {file}.\n",
        "global_file_in_base": "{file} is part of the base project and was not copied.\n",
        "global_file_copied": "{file} copied.\n",
        "visu_settings": "Settings of the old VisuTask written to {path}:\n",
        "warn_visu_setting_missing": "VisuTask setting {name} not found in {path}; please set it manually.",
        "warn_task_source_missing": "Task {task}: the program folder {path} of the Cpu.sw does not exist; not copied.",
        "warn_program_in_base": "Task {task} exists in the base project already and was not copied; please compare "
                                "the old and the new version.",
        "application_tasks": "{tasks} task(s) with {files} file(s) copied, {deployed} added to Cpu.sw:\n",
        "application_data": "{objects} data module(s) copied, {deployed} added to Cpu.sw:\n",
        "application_files": "Files below Logical copied: {files}\n",
        "warn_not_taken_over": "{name} ({type}) of the old project was not taken over (no task or data module of it is "
                               "deployed in the Cpu.sw); take it over manually if needed.",
        "libraries_summary": "Customer libraries copied and added to Cpu.sw: {copied}\n",
        "libraries_ptop": "PtoP libraries not taken over (PtoP is discontinued): {names}\n",
        "warn_library_not_deployed": "Library {name} was copied but could not be added to {path} (no <Libraries>).",
        "warn_br_libraries_missing": "B&R libraries of the old project missing in the new project: {names}. Add them "
                                     "via the toolbox if they are used.",
        "warn_library_replaced": "Library {name} of the old project is discontinued in Automation Studio 6; "
                                 "replace it by {successor} and adapt the program code that uses it.",
        "warn_library_discontinued": "Library {name} of the old project is discontinued in Automation Studio 6{reason}; "
                                     "remove or replace the program code that uses it.",
        "warn_function_discontinued": "{name} ({library}) is discontinued in Automation Studio 6{reason}; adapt the "
                                      "program code: {files}",
        "reason_sg3": "supports SG3 only",
        "reason_sgc": "supports SGC only",
        "reason_sg3_sgc": "supports SG3 and SGC only",
        "reason_arwin": "supports ARwin only",
        "reason_ina2000": "based on INA2000",
        "reason_dallas": "supports Dallas dongles only, which are no longer available for current AR versions",
        "reason_slip": "SLIP is an outdated protocol",
        "reason_pp500": "supports the discontinued PP500 only",
        "reason_sg3_pp": "only needed for the communication with SG3 Power Panels",
        "reason_system2005": "supports the hardware system 2005 only",
        "reason_net2000": "B&R proprietary protocol for RS485 and RS232 only (CAN for SG3 only)",
        "reason_profibus": "for the outdated PROFIBUS modules X20IF1061, X20IF1065, LS050 and LS166.6 only",
        "reason_legacy_safety": "Legacy Safety is not supported by AS 6 / AR 6",
        "reason_ar_before_4": "works with AR versions < 4.00 only",
        "reason_replaced": "replaced by {successor}",
        "warn_library_without_source": "Customer libraries without source code (.c, .st) were not taken over: {names}. "
                                       "Get a version for Automation Studio 6 or replace them.",
        "users_none": "No users and roles found in the old project.\n",
        "users_summary": "Added roles: {roles}\nAdded users: {users}\nUsers of the base project kept: {kept}\n"
                         "Folder: {path}\n",
        "warn_user_passwords": "The passwords of the users {users} cannot be taken over because Automation Studio 6 "
                               "stores them as Argon2 hash; please enter them again ({path}).",
        "warn_user_role_missing": "User {user} has the unknown role(s) {roles}; please check.",
        "warn_mpuserx": "The users and roles could not be added to the mapp user management {path}; please add them.",
        "opcua_further": "Further OPC UA variables of the old project enabled: {variables}\nTasks: {tasks}\n",
        "warn_opcua_undeclared": "Variables enabled in OPC UA in the old project are not declared in the new one: "
                                 "{names}.",
        "visu_pages": "Pages: {pages}\nDialogs: {dialogs}\nContents: {contents}\nReplaced by the old content: {replaced}\n",
        "visu_widget_libraries": "Widget libraries copied: {names}\n",
        "visu_layout_replaced": "{page}: layout {old} replaced by {new}.\n",
        "visu_layouts_copied": "Layouts copied: {names}\n",
        "warn_layout_missing": "{page}: layout {layout} not found; please assign a layout.",
        "warn_page_missing": "Page {page} of {path} was not found in the old project.",
        "warn_content_missing": "Content {content} of {page} was not found in the old project.",
        "visu_resources": "Media copied: {media}\nTexts copied: {texts}\n",
        "warn_media_missing": "{media} was not found in the old project; please add it.",
        "warn_text_missing": "Text {text} was not found in the old project or the new project has no text module "
                             "for it; please add it.",
        "warn_widget_missing": "{content}/{widget}: widget {type} does not exist in the new project; please replace it.",
        "warn_widget_renamed": "{content}/{widget}: widget {old} replaced by {new}; please check size and orientation.",
        "warn_widget_attributes": "{content}: widget {type} has no properties {names}; please check them in the "
                                  "content.",
        "visu_bindings": "{bindings} binding(s) and {events} event binding(s) taken over: {files}\n"
                         "Bindings of the base project to removed or replaced widgets removed: {removed}\n",
        "warn_bindings_dangling": "{count} taken over (event) binding(s) refer to widgets, pages or dialogs that do "
                                  "not exist and were removed.",
        "visu_session_variables": "Session variables added: {names}\n",
        "warn_session_variable_missing": "Session variable {name} not found; please add it.",
        "warn_variable_undeclared": "Binding to {variable}: the variable is not declared in the new project.",
        "warn_variable_not_enabled": "Binding to {variable}: the variable is not enabled in OPC UA; please enable it.",
        "visu_opcua_enabled": "Variables of the bindings enabled in OPC UA: {names}\n",
        "warn_ladder_format": "{count} Ladder Diagram file(s) are in the format of Automation Studio < 6 (tasks: "
                              "{tasks}); open and save each of them once in Automation Studio 6 to convert them "
                              "(otherwise build error 1113).",
        "warn_library_encrypted": "Library {name}: {count} source file(s) are encrypted; decrypt them with the "
                                  "Decrypt Files Wizard of Automation Studio (password required), otherwise the build "
                                  "fails.",
    },
    "de": {
        "not_found": "'{filename}' wurde nicht gefunden unter:\n{root}",
        "choice_cancelled": "Keine '{filename}' ausgewählt; Konvertierung abgebrochen.",
        "ambiguous": "Mehr als eine '{filename}' wurde unter {root} gefunden.\n"
                     "Bitte einen Ordner wählen, der genau eine Datei enthält:\n{choices}",
        "no_directories": "Beide Auswahlen müssen existierende Projektverzeichnisse sein.",
        "no_project_file": "Bitte eine Automation-Studio-Projektdatei (*.apj) auswählen:\n{path}",
        "not_bk2000": "Im Bk2000-Projekt nicht gefunden:\n{files}\n\nBitte ein Bk2000-Projekt auswählen.\n{root}",
        "warn_no_group": "{name}: keine Gruppe gefunden; Gruppe wurde weggelassen.",
        "warn_unknown_members": "{type} hat kein(e) Element(e) {members}; die Initialisierungen in der Global.var, "
                                "die sie verwenden, müssen korrigiert werden (sonst Build-Fehler 1176).",
        "warn_unmatched": "{count} Datenpunkt(e) in dplist.dat haben keine Deklaration in der alten global.var.",
        "no_declarations": "In der alten global.var wurden keine Variablendeklarationen gefunden.",
        "no_logical": "Kein Ordner 'Logical' gefunden in:\n{root}",
        "no_objects_section": "'{child}' kann nicht in {package} eingetragen werden: kein <Objects>-Abschnitt gefunden.",
        "no_ladder_tasks": "Keine Tasks mit .ld-Dateien gefunden unter:\n{root}",
        "choice_title": "{filename} auswählen",
        "config_title": "Hardware-Konfiguration auswählen",
        "config_prompt": "Das BkNG-Projekt hat mehrere Hardware-Konfigurationen:\n{root}\n\n"
                         "Bitte die Konfiguration auswählen, in die konvertiert werden soll:",
        "choice_prompt": "Mehr als eine '{filename}' wurde gefunden unter:\n{root}\n\n"
                         "Bitte die zu verwendende Datei auswählen:",
        "ok": "OK",
        "cancel": "Abbrechen",
        "language": "Sprache",
        "warn_opcua_version": "{path} hat FileVersion {version}; erwartet wird eine Datei von Automation Studio 6 "
                              "(FileVersion 10 oder höher). Bitte das BkNG-Basisprojekt prüfen.",
        "old_folder": "Quellprojekt (.apj/.zip)",
        "new_folder": "BkNG-Projekt (.apj)",
        "browse": "Durchsuchen…",
        "copy_ladder": "Alle Kontaktplan-Tasks (.ld) in das BkNG-Projekt kopieren",
        "convert": "Konvertieren",
        "start_hint": "Hinweis: Das BkNG-Projekt muss ein leeres BkNG-Projekt (BkNG-Basisprojekt) sein.\n"
                      "Projektdateien (.apj) des Quellprojekts und des BkNG-Projekts auswählen, dann auf Konvertieren klicken.\n"
                      "Ein Bk2000-Projekt wird konvertiert. Ein BkNG-Projekt von Automation Studio 4 (mit VisuTask) "
                      "wird migriert: seine projektspezifischen Teile (Tasks, Bibliotheken, Benutzer, Kundenvisu mit "
                      "Bindings) werden in das BkNG-Basisprojekt von Automation Studio 6 übernommen.\n"
                      "Das Quellprojekt kann auch eine ZIP-Datei sein; es wird in einen temporären Ordner entpackt.\n"
                      "Alternativ ein gezipptes BkNG-Basisprojekt und einen Zielordner auswählen; es wird dort "
                      "als <Quellname>_AS6 entpackt.\n"
                      "Ein Bericht mit allen offenen Punkten (ToDo) wird im BkNG-Projektordner gespeichert.\n",
        "select_old": "Bk2000- oder BkNG-Projekt auswählen",
        "select_new": "BkNG-Projekt auswählen",
        "project_files": "Automation-Studio-Projekt",
        "source_files": "Automation-Studio-Projekt oder ZIP-Datei",
        "no_source_zip": "Bitte eine ZIP-Datei mit einem Bk2000- oder BkNG-Projekt auswählen:\n{path}",
        "source_extracted": "Quellprojekt aus {archive} in den temporären Ordner entpackt:\n{path}\n",
        "step_unpack_source": "Quellprojekt entpacken",
        "progress_step": "Schritt {number} von {total}: {step}",
        "source_unpacking": "Quellprojekt wird entpackt …",
        "progress_report": "Bericht wird geschrieben …",
        "using_file": "Verwende {filename}: {path}\n",
        "error": "FEHLER: {error}\n",
        "conversion_failed": "Konvertierung fehlgeschlagen",
        "summary": "{converted} Datenpunkt-Deklaration(en) konvertiert; {copied} unverändert übernommen.\n"
                   "Geschrieben: {target}\n",
        "ladder_tasks": "Kopierte Kontaktplan-Tasks:",
        "ladder_summary": "{tasks} Kontaktplan-Task(s) mit {files} Datei(en) kopiert ({overwritten} überschrieben); "
                          "{packages} Package.pkg-Datei(en) aktualisiert.\n",
        "no_physical": "Kein Automation-Studio-Projekt (Physical/Physical.pkg) gefunden für:\n{root}",
        "no_configuration": "Keine Hardware-Konfiguration gefunden in:\n{root}",
        "ambiguous_configuration": "Die aktive Konfiguration dieses Projekts konnte nicht ermittelt werden:\n{root}",
        "no_cpu": "Kein SPS-Ordner in der Konfiguration gefunden:\n{root}",
        "cpu_not_in_hardware": "SPS '{cpu}' wurde in {path} nicht gefunden.",
        "no_io_modules": "In {path} wurden keine an der SPS angeschlossenen X20-I/O-Module gefunden.",
        "io_name_is_cpu": "Ein I/O-Modul des Bk2000-Projekts heißt wie die BkNG-SPS: '{name}'.",
        "invalid_hardware": "Ungültige Hardware-Datei: {path}",
        "warn_io_target_skipped": "{module}: Verbindung {connector} zu '{target}' wurde nicht übernommen "
                                  "('{target}' ist kein X20-I/O-Modul).",
        "warn_io_device_not_copied": "{device} ({type}) ist an {module} angeschlossen, ist aber kein X20-Modul und "
                                     "wurde nicht übernommen; bitte manuell ergänzen.",
        "warn_io_connect_cpu": "{module}.{connector} war an {old_cpu}.{target_connector} angeschlossen; der I/O-Block "
                               "wurde ohne SPS-Anbindung eingefügt, bitte an {cpu} anschließen.",
        "warn_io_name_clash": "{module} des BkNG-Projekts ist an '{target}' angeschlossen, ersetzt durch "
                              "gleichnamige Bk2000-Module; bitte Topologie prüfen.",
        "warn_mapping_skipped": "I/O-Mapping nicht übernommen (kein X20-I/O-Modul): {line}",
        "copy_io": "X20-I/O-Module inkl. I/O-Mapping in die aktive Konfiguration kopieren",
        "io_summary": "{modules} I/O-Modul(e) ({replaced} ersetzt) und {mappings} I/O-Mapping(s) kopiert nach:\n{hardware}\n",
        "io_fieldbus": "PROFIBUS-Gerät(e) mit GSD-Datei: {devices}\nGerätebeschreibung(en) kopiert nach {folder}: {files}\n",
        "io_dtm": "DTM-Konfiguration kopiert: {folders}\n",
        "warn_io_fieldbus_check": "PROFIBUS-Gerät {device} wurde mit seiner GSD-Datei {file} übernommen; bitte die "
                                  "Gerätekonfiguration (DTM) in Automation Studio öffnen und prüfen.",
        "warn_cpu_mapping": "CPU-Mapping für {cpu} übernommen; bitte prüfen, ob der Kanal auf der neuen SPS existiert: {line}",
        "warn_no_deployment": "Tasks wurden keiner Taskklasse zugeordnet (Cpu.sw): {error}",
        "warn_task_not_deployed": "{task} ist im Bk2000-Projekt keiner Taskklasse zugeordnet.",
        "warn_task_name_in_use": "In der neuen Cpu.sw gibt es bereits einen anderen Task namens '{task}'; er wurde nicht eingetragen.",
        "warn_task_class_missing": "Taskklasse '{task_class}' existiert in der neuen Cpu.sw nicht; '{task}' wurde nicht eingetragen.",
        "ladder_deployed": "{count} Task(s) in ihre Taskklasse in der Cpu.sw eingetragen.\n",
        "ladder_converted": "{count} Kontaktplan-Datei(en) in das Format von Automation Studio 6 konvertiert "
                            "(Working Version {version}).\n",
        "ladder_no_as6": "Kein Automation Studio 6 gefunden; die Kontaktpläne konnten nicht automatisch konvertiert "
                         "werden.",
        "ladder_as6_failed": "Die Kontaktpläne konnten nicht mit Automation Studio 6 konvertiert werden: {error}",
        "ladder_file_failed": "{file}: Konvertierung in das Format von Automation Studio 6 fehlgeschlagen ({error}).",
        "ladder_no_compiler": "C#-Compiler des .NET Framework nicht gefunden ({path}).",
        "ladder_compile_failed": "Das Hilfsprogramm für die Kontaktplan-Konvertierung konnte nicht übersetzt werden: "
                                 "{error}",
        "ust_written": "UST-Name '{name}' auf UST_Name geschrieben in:\n{path}\n",
        "warn_no_visu_variables": "UST-Name nicht geschrieben: keine VisuTask/Variables.var unter {root} gefunden.",
        "warn_ust_name_not_written": "UST-Name '{name}' wurde nicht geschrieben: keine Variables.var ausgewählt.",
        "warn_no_ust_name_declaration": "UST-Name nicht geschrieben: keine Deklaration UST_Name in {path} gefunden.",
        "warn_ust_name_too_long": "UST-Name '{name}' ist länger als STRING[{length}] und wird von der SPS abgeschnitten.",
        "copy_ethernet": "Ethernet-Einstellungen der SPS übernehmen (IF2: IP-Adresse, Subnetzmaske, Hostname, Modus; "
                         "Gateway; alle Einstellungen der IF3)",
        "warn_no_interface": "Die Bk2000-SPS '{cpu}' hat keine Schnittstelle {interface}; sie wurde nicht verändert.",
        "copy_opcua": "Alle Bk-Datenpunkte inkl. Unterelementen in OpcUaBkV.uad freigeben",
        "invalid_opcua": "Ungültige OPC-UA-Datei (kein <Module> gefunden): {path}",
        "opcua_summary": "{count} Bk-Datenpunkt(e) in OPC UA freigegeben ({replaced} ersetzt):\n{path}\n",
        "warn_opcua_unknown_type": "{name}: für Typ {type} ist keine Elementliste hinterlegt; nicht in OPC UA freigegeben.",
        "convert_visu": "UVIS-Seiten in BkVisu-Seiten umwandeln (experimentell)",
        "no_user_visu": "Das uvis-Programm wurde gefunden, aber keine VC4-Visualisierung mit UVis_-Seiten unter:\n{root}",
        "no_bkvisu": "Das BkNG-Projekt hat keinen BkVisu-Seitenordner:\n{path}",
        "invalid_mappview_file": "Kein <{section}>-Abschnitt in {path} gefunden.",
        "visu_none": "Keine Benutzervisu (uvis.br oder uvis-Quellcode) im Bk2000-Projekt gefunden; nichts umzuwandeln.\n",
        "visu_summary": "{pages} VC4-Seite(n) mit {widgets} Widget(s) und {bindings} Binding(s) umgewandelt:\n"
                        "{names}\nBindings: {binding_dir}\n",
        "visu_scaling": "VC4-Auflösung {width}x{height} (Kopfzeile {header} px) auf {target_width}x{target_height} skaliert.\n",
        "warn_visu_text_datapoint": "{page}/{control}: Text aus Datenpunkt {datapoint} als leeres Label platziert.",
        "warn_visu_navigation": "{page}/{control}: Zielseite '{target}' wird nicht umgewandelt; Button weggelassen.",
        "warn_visu_page_no_datapoints": "{page}: In der dplist.dat gibt es keine Datenpunkte mit #UVIS page={page_index}; "
                                        "die Seite wurde nicht umgewandelt, Buttons zu ihr wurden weggelassen.",
        "warn_visu_not_dynamic": "{page}: {count} Element(e) ohne Dynamik platziert: {items}.",
        "visu_unbound_unknown": "Variable im Bk2000-Projekt nicht deklariert",
        "visu_unbound_pointer": "Zeiger, in OPC UA nicht verfügbar",
        "visu_unbound_missing": "Variable fehlt im BkNG-Projekt",
        "visu_unbound_undeployed": "Task übernommen, aber nicht in der Cpu.sw eingetragen",
        "visu_tasks": "Für die Bindings der Visu übernommene Tasks: {tasks}\n",
        "visu_task_not_deployed": " (nicht in die Cpu.sw eingetragen)",
        "visu_unbound_type": "Datentyp nicht unterstützt",
        "visu_unbound_key": "virtuelle Taste des Programms, Button an eine Variable binden",
        "visu_unbound_bitmap": "Bitmap nicht gefunden",
        "warn_visu_no_expressions": "{page}: Kein Expressions-Paket ({path}); Widgets mit Status- oder Indexdatenpunkt "
                                    "sind immer sichtbar.",
        "visu_dynamics": "{widgets} Widget(s) an SPS-Variablen gebunden (direkt oder über {expressions} Ausdruck/Ausdrücke "
                         "für die Sichtbarkeit).\n",
        "warn_visu_no_widget": "{page}/{control}: Widget {widget} fehlt in der BkVisuLib; als Rechteck platziert.",
        "warn_visu_no_datapoint": "{page}/{control}: kein Datenpunkt vom Typ {type} mit #UVIS page={page_index} "
                                  "idx={idx}; Widget ohne Binding.",
        "warn_visu_no_name_datapoint": "{page}/{control}: kein Datenpunkt vom Typ {type} mit #UVIS page={page_index} "
                                       "idx={idx}; Namensfeld weggelassen.",
        "warn_visu_no_session_variables": "Keine Session-Variablen-Datei (*.svar) unter {path} gefunden; bitte die "
                                          "BOOL-Session-Variable {variable} (Standard false) für den ktx-Schalter anlegen.",
        "warn_visu_no_library": "BkVisuLib nicht gefunden ({path}); Bk-Bitmaps als Rechtecke platziert.",
        "visu_nav_summary": "Navigationsbuttons in {path}:\n{buttons}\n",
        "warn_visu_no_navigation": "BkV_NavigationContent unter {path} nicht gefunden; keine Navigationsbuttons angelegt.",
        "warn_visu_no_navigation_bar": "{path} hat keine NavigationBar; keine Navigationsbuttons angelegt.",
        "warn_visu_navigation_full": "In der Navigationsleiste ist kein Platz für die Seite(n) {pages}; kein Button angelegt.",
        "no_ethernet_interface": "Die Bk2000-SPS '{cpu}' hat keine Ethernet-Schnittstelle {interface}.",
        "ethernet_summary": "Ethernet-Einstellungen von {cpu} übernommen ({mode}) in:\n{hardware}\n",
        "ethernet_mode_manual": "feste IP-Adresse",
        "warn_ethernet_source": "Die Bk2000-SPS hat keine eigene Schnittstelle {interface}; die Einstellungen der "
                                "Ethernet-Schnittstelle {source} wurden in {interface} von {cpu} übernommen. Bitte prüfen.",
        "ethernet_mode_dhcp": "DHCP-Client",
        "warn_ethernet_mode": "{interface}: IP-Modus '{mode}' wurde übernommen, weitere Einstellungen dieses Modus (z. B. DHCP-Leases) nicht.",
        "old_group": "Quelle: Bk2000-Projekt oder BkNG-Projekt (AS4)",
        "new_group": "Ziel: BkNG-Projekt (AS6)",
        "new_mode_project": "Bestehendes BkNG-Projekt",
        "new_mode_zip": "BkNG-Basisprojekt aus ZIP-Datei",
        "zip_file": "BkNG-Basisprojekt (.zip)",
        "zip_target": "Zielordner",
        "select_zip": "Gezipptes BkNG-Basisprojekt auswählen",
        "select_zip_target": "Zielordner für das BkNG-Projekt auswählen",
        "zip_files": "ZIP-Datei",
        "no_zip_file": "Bitte eine ZIP-Datei mit einem BkNG-Basisprojekt auswählen:\n{path}",
        "no_target_dir": "Bitte einen existierenden Zielordner auswählen:\n{path}",
        "target_exists": "Der Ziel-Projektordner existiert bereits:\n{path}\n\nBitte löschen oder einen anderen Zielordner wählen.",
        "zip_no_project": "Die ZIP-Datei enthält nicht genau ein Automation-Studio-Projekt "
                          "(.apj neben Physical/Physical.pkg):\n{path}",
        "zip_extracted": "BkNG-Basisprojekt entpackt nach:\n{path}\n",
        "step_unpack": "BkNG-Basisprojekt entpacken",
        "step_analysis": "Analyse des Bk2000-Projekts",
        "step_data": "Datenmodule (.dat)",
        "copy_data": "Datenmodule (.dat) außer dplist, gruppen und Bk2000-Standardmodulen in das BkNG-Projekt kopieren",
        "data_none": "Außer dplist, gruppen und den Bk2000-Standardmodulen wurden keine Datenmodule gefunden.\n",
        "data_summary": "{objects} Datenmodul(e) mit {files} Datei(en) kopiert ({overwritten} überschrieben); "
                        "{packages} Package.pkg-Datei(en) aktualisiert.\n{deployed} Datenmodul(e) in die Cpu.sw eingetragen.\n",
        "data_objects": "Kopierte Datenmodule:",
        "warn_data_not_deployed": "Datenmodul {name} ist in der Cpu.sw des Bk2000-Projekts nicht eingetragen; "
                                  "es wurde kopiert, aber nicht in die Cpu.sw eingetragen.",
        "warn_data_name_in_use": "In der Cpu.sw des BkNG-Projekts gibt es bereits ein Objekt namens '{name}'; "
                                 "Datenmodul {source} wurde kopiert, aber nicht in die Cpu.sw eingetragen.",
        "analysis_libraries": "Bibliotheken des Bk2000-Projekts, die keine Standardbibliotheken (B&R, Burklimat) sind ({path}):\n",
        "analysis_library": "- {name} ({language}){present}\n",
        "analysis_library_present": ", auch im BkNG-Projekt vorhanden",
        "analysis_no_libraries": "Alle Bibliotheken des Bk2000-Projekts sind Standardbibliotheken (B&R, Burklimat).\n",
        "analysis_ptop": "PtoP-Datenmodul: {path}\n",
        "warn_customer_libraries": "Kundenspezifische Bibliotheken gefunden: {names}. Prüfen, ob sie im BkNG-Projekt "
                                   "benötigt werden; ggf. manuell übernehmen oder durch BkNG-Funktionen ersetzen.",
        "warn_ptop": "PtoP-Kopplung gefunden ({path}). PtoP ist abgekündigt; die PtoP-Tasks und -Bibliotheken werden "
                     "nicht übernommen. Die Kopplung durch OPC UA ersetzen.",
        "warn_ptop_omitted": "PtoP ist abgekündigt; die PtoP-Tasks {names} wurden nicht übernommen. Die Kopplung "
                             "durch OPC UA ersetzen.",
        "application_ptop": "PtoP-Tasks nicht übernommen (PtoP ist abgekündigt): {names}\n",
        "step_global": "Datenpunkte (global.var)",
        "step_ladder": "Kontaktplan-Tasks",
        "step_io": "I/O-Konfiguration",
        "step_ethernet": "Ethernet-Einstellungen",
        "step_opcua": "OPC UA",
        "step_visu": "Benutzervisu (UVIS)",
        "report_title": "BkNG-Konverter V4 - Konvertierungsbericht",
        "report_date": "Datum",
        "report_old": "Quellprojekt",
        "report_new": "BkNG-Projekt",
        "report_result_ok": "Konvertierung abgeschlossen, {todos} offene(r) Punkt(e).",
        "report_result_error": "Konvertierung im Schritt '{step}' abgebrochen; die folgenden Schritte wurden nicht ausgeführt.",
        "report_todo_heading": "ZU ERLEDIGEN",
        "report_no_todos": "Keine offenen Punkte.",
        "report_error_todo": "FEHLER: {error}\n    Ursache beheben und die Konvertierung erneut ausführen "
                             "(am besten in ein frisches BkNG-Basisprojekt).",
        "report_steps_heading": "DURCHGEFÜHRTE SCHRITTE",
        "report_file": "Konvertierungsbericht",
        "report_saved": "Der Bericht ist im BkNG-Projektordner gespeichert:\n{path}",
        "report_not_saved": "Der Bericht konnte nicht gespeichert werden:\n{error}",
        "report_open": "Bericht jetzt öffnen?",
        "report_mode": "Modus",
        "mode_unknown": "",
        "mode_bk2000": "Bk2000-Projekt: Konvertierung in das BkNG-Projekt",
        "mode_migration": "BkNG-Projekt von Automation Studio {version}: Migration in das BkNG-Basisprojekt (AS6)",
        "mode_error": "Projekt kann nicht umgewandelt werden: {error}",
        "already_as6": "Das Projekt ist bereits ein Projekt von Automation Studio {version}; eine Migration ist nicht "
                       "nötig:\n{path}",
        "migrate_application": "Tasks, Datenmodule und Kundenbibliotheken (mit Quellcode) übernehmen",
        "migrate_users": "Benutzer und Rollen übernehmen (Passwörter müssen neu eingegeben werden)",
        "migrate_opcua": "Bk-Datenpunkte und die weiteren OPC-UA-Variablen des alten Projekts in OpcUaBkV.uad freigeben",
        "migrate_visu": "Kundenvisu übernehmen (BkVisu-Seiten, Bindings, Event-Bindings, Medien, Texte)",
        "convert_ladder_as6": "Alte Kontaktpläne mit Automation Studio 6 in das AS6-Format konvertieren "
                              "(Automation Studio 6 muss installiert sein)",
        "ladder_as6_title": "Kontaktpläne mit Automation Studio 6 konvertieren",
        "ladder_as6_info": "Für diese Option muss Automation Studio 6 auf diesem Rechner installiert sein. Der "
                           "Konverter nutzt dessen Kontaktplan-Konvertierung (interne Funktionen von Automation "
                           "Studio); ob das mit den Lizenzbedingungen von B&R vereinbar ist, muss der Anwender selbst "
                           "prüfen.\n\nOhne Automation Studio 6 bleiben die Kontaktpläne im alten Format; der Bericht "
                           "nennt sie dann zum einmaligen Öffnen und Speichern in Automation Studio 6.\n\n{status}",
        "ladder_as6_found": "Automation Studio 6 gefunden:\n{path}",
        "ladder_as6_not_found": "Automation Studio 6 wurde auf diesem Rechner nicht gefunden.",
        "step_migration_analysis": "Analyse des BkNG-Projekts",
        "step_cleanup": "Beispiele und Testseiten des Basisprojekts entfernen",
        "step_global_migration": "Globale Deklarationen (Global.var, Global.typ)",
        "step_visu_task": "VisuTask-Einstellungen",
        "step_application": "Tasks und Datenmodule",
        "step_libraries": "Bibliotheken",
        "step_users": "Benutzer und Rollen",
        "step_visu_migration": "Kundenvisu (mappView)",
        "migration_analysis": "BkNG-Projekt von Automation Studio {version}, Konfiguration {old}; "
                              "Zielkonfiguration: {new}.\n",
        "cleanup_summary": "Entfernte Seiten: {pages}\nEntfernte Programme: {programs}\n"
                           "Entfernte Bindings zu entfernten Contents: {bindings} (gelöschte Dateien: {files})\n",
        "global_merged": "{count} Deklaration(en) des alten Projekts in {file} ergänzt.\n",
        "global_file_in_base": "{file} gehört zum Basisprojekt und wurde nicht kopiert.\n",
        "global_file_copied": "{file} kopiert.\n",
        "visu_settings": "Einstellungen der alten VisuTask geschrieben in {path}:\n",
        "warn_visu_setting_missing": "VisuTask-Einstellung {name} nicht in {path} gefunden; bitte manuell setzen.",
        "warn_task_source_missing": "Task {task}: Der Programmordner {path} aus der Cpu.sw existiert nicht; nicht kopiert.",
        "warn_program_in_base": "Task {task} gibt es bereits im Basisprojekt; er wurde nicht kopiert. Bitte alte und "
                                "neue Version vergleichen.",
        "application_tasks": "{tasks} Task(s) mit {files} Datei(en) kopiert, {deployed} in die Cpu.sw eingetragen:\n",
        "application_data": "{objects} Datenmodul(e) kopiert, {deployed} in die Cpu.sw eingetragen:\n",
        "application_files": "Dateien unter Logical übernommen: {files}\n",
        "warn_not_taken_over": "{name} ({type}) des alten Projekts wurde nicht übernommen (keine Task und kein "
                               "Datenmodul daraus ist in der Cpu.sw eingetragen); bei Bedarf manuell übernehmen.",
        "libraries_summary": "Kundenbibliotheken kopiert und in die Cpu.sw eingetragen: {copied}\n",
        "libraries_ptop": "PtoP-Bibliotheken nicht übernommen (PtoP ist abgekündigt): {names}\n",
        "warn_library_not_deployed": "Bibliothek {name} wurde kopiert, konnte aber nicht in {path} eingetragen werden "
                                     "(kein <Libraries>).",
        "warn_br_libraries_missing": "B&R-Bibliotheken des alten Projekts fehlen im neuen Projekt: {names}. Bei "
                                     "Verwendung über die Toolbox hinzufügen.",
        "warn_library_replaced": "Bibliothek {name} des alten Projekts ist in Automation Studio 6 abgekündigt; "
                                 "durch {successor} ersetzen und den Programmcode, der sie verwendet, anpassen.",
        "warn_library_discontinued": "Bibliothek {name} des alten Projekts ist in Automation Studio 6 "
                                     "abgekündigt{reason}; den Programmcode, der sie verwendet, entfernen oder ersetzen.",
        "warn_function_discontinued": "{name} ({library}) ist in Automation Studio 6 abgekündigt{reason}; "
                                      "Programmcode anpassen: {files}",
        "reason_sg3": "unterstützt nur SG3",
        "reason_sgc": "unterstützt nur SGC",
        "reason_sg3_sgc": "unterstützt nur SG3 und SGC",
        "reason_arwin": "unterstützt nur ARwin",
        "reason_ina2000": "basiert auf INA2000",
        "reason_dallas": "unterstützt nur Dallas-Dongles, die für aktuelle AR-Versionen nicht mehr verfügbar sind",
        "reason_slip": "SLIP ist ein veraltetes Protokoll",
        "reason_pp500": "unterstützt nur das abgekündigte PP500",
        "reason_sg3_pp": "nur für die Kommunikation mit SG3-Power-Panels nötig",
        "reason_system2005": "unterstützt nur das Hardwaresystem 2005",
        "reason_net2000": "B&R-proprietäres Protokoll nur für RS485 und RS232 (CAN nur für SG3)",
        "reason_profibus": "nur für die veralteten PROFIBUS-Module X20IF1061, X20IF1065, LS050 und LS166.6",
        "reason_legacy_safety": "Legacy Safety wird von AS 6 / AR 6 nicht unterstützt",
        "reason_ar_before_4": "funktioniert nur mit AR-Versionen < 4.00",
        "reason_replaced": "ersetzt durch {successor}",
        "warn_library_without_source": "Kundenbibliotheken ohne Quellcode (.c, .st) wurden nicht übernommen: {names}. "
                                       "Eine Version für Automation Studio 6 besorgen oder ersetzen.",
        "users_none": "Im alten Projekt wurden keine Benutzer und Rollen gefunden.\n",
        "users_summary": "Ergänzte Rollen: {roles}\nErgänzte Benutzer: {users}\nBenutzer des Basisprojekts "
                         "beibehalten: {kept}\nOrdner: {path}\n",
        "warn_user_passwords": "Die Passwörter der Benutzer {users} können nicht übernommen werden, da Automation "
                               "Studio 6 sie als Argon2-Hash speichert; bitte neu eingeben ({path}).",
        "warn_user_role_missing": "Benutzer {user} hat die unbekannte(n) Rolle(n) {roles}; bitte prüfen.",
        "warn_mpuserx": "Die Benutzer und Rollen konnten nicht in die mapp-Benutzerverwaltung {path} eingetragen "
                        "werden; bitte ergänzen.",
        "opcua_further": "Weitere OPC-UA-Variablen des alten Projekts freigegeben: {variables}\nTasks: {tasks}\n",
        "warn_opcua_undeclared": "Im alten Projekt in OPC UA freigegebene Variablen sind im neuen Projekt nicht "
                                 "deklariert: {names}.",
        "visu_pages": "Seiten: {pages}\nDialoge: {dialogs}\nContents: {contents}\nDurch den alten Content ersetzt: "
                      "{replaced}\n",
        "visu_widget_libraries": "Widget-Bibliotheken kopiert: {names}\n",
        "visu_layout_replaced": "{page}: Layout {old} durch {new} ersetzt.\n",
        "visu_layouts_copied": "Layouts übernommen: {names}\n",
        "warn_layout_missing": "{page}: Layout {layout} nicht gefunden; bitte ein Layout zuweisen.",
        "warn_page_missing": "Seite {page} aus {path} wurde im alten Projekt nicht gefunden.",
        "warn_content_missing": "Content {content} von {page} wurde im alten Projekt nicht gefunden.",
        "visu_resources": "Medien kopiert: {media}\nTexte kopiert: {texts}\n",
        "warn_media_missing": "{media} wurde im alten Projekt nicht gefunden; bitte ergänzen.",
        "warn_text_missing": "Text {text} wurde im alten Projekt nicht gefunden oder das neue Projekt hat kein "
                             "Textmodul dafür; bitte ergänzen.",
        "warn_widget_missing": "{content}/{widget}: Widget {type} gibt es im neuen Projekt nicht; bitte ersetzen.",
        "warn_widget_renamed": "{content}/{widget}: Widget {old} durch {new} ersetzt; bitte Größe und Ausrichtung prüfen.",
        "warn_widget_attributes": "{content}: Widget {type} hat keine Eigenschaften {names}; bitte im Content prüfen.",
        "visu_bindings": "{bindings} Binding(s) und {events} Event-Binding(s) übernommen: {files}\n"
                         "Bindings des Basisprojekts zu entfernten oder ersetzten Widgets entfernt: {removed}\n",
        "warn_bindings_dangling": "{count} übernommene (Event-)Binding(s) verweisen auf nicht vorhandene Widgets, "
                                  "Seiten oder Dialoge und wurden entfernt.",
        "visu_session_variables": "Session-Variablen ergänzt: {names}\n",
        "warn_session_variable_missing": "Session-Variable {name} nicht gefunden; bitte ergänzen.",
        "warn_variable_undeclared": "Binding auf {variable}: Die Variable ist im neuen Projekt nicht deklariert.",
        "warn_variable_not_enabled": "Binding auf {variable}: Die Variable ist nicht in OPC UA freigegeben; bitte "
                                     "freigeben.",
        "visu_opcua_enabled": "Variablen der Bindings in OPC UA freigegeben: {names}\n",
        "warn_ladder_format": "{count} Kontaktplan-Datei(en) liegen im Format von Automation Studio < 6 vor (Tasks: "
                              "{tasks}); jede einmal in Automation Studio 6 öffnen und speichern, um sie zu "
                              "konvertieren (sonst Build-Fehler 1113).",
        "warn_library_encrypted": "Bibliothek {name}: {count} Quelldatei(en) sind verschlüsselt; mit dem Decrypt Files "
                                  "Wizard von Automation Studio entschlüsseln (Passwort nötig), sonst schlägt der "
                                  "Build fehl.",
    },
}
_language = "de"


def set_language(language: str) -> None:
    global _language
    if language not in TEXTS:
        raise ValueError(f"Unsupported language: {language}")
    _language = language


def tr(key: str, **values: object) -> str:
    """Return the text for ``key`` in the current language."""
    return TEXTS[_language][key].format(**values)


def read_text_with_encoding(path: Path) -> tuple[str, str]:
    """Read legacy files while retaining German characters where possible."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1", errors="replace"), "latin-1"


def read_text(path: Path) -> str:
    return read_text_with_encoding(path)[0]


def discover_file(root: Path, filename: str, chooser: FileChooser | None = None) -> Path:
    matches = sorted(
        (path for path in root.rglob("*") if path.is_file() and path.name.casefold() == filename.casefold()),
        key=lambda path: (len(path.relative_to(root).parts), str(path).casefold()),
    )
    if not matches:
        raise ConversionError(tr("not_found", filename=filename, root=root))
    # Automation Studio keeps generated copies below Temp.  Prefer the editable
    # source file below Logical when a project root is selected.
    logical_matches = [path for path in matches if any(part.casefold() == "logical" for part in path.relative_to(root).parts)]
    if len(logical_matches) == 1:
        return logical_matches[0]
    if len(matches) > 1 and chooser is not None:
        selected = chooser(filename, root, matches)
        if selected is None:
            raise ConversionError(tr("choice_cancelled", filename=filename))
        return selected
    if len(matches) > 1:
        choices = "\n".join(str(path.relative_to(root)) for path in matches)
        raise ConversionError(tr("ambiguous", filename=filename, root=root, choices=choices))
    return matches[0]


def project_dir_from_file(project_file: Path) -> Path:
    """Return the project folder of an Automation Studio project file (``.apj``)."""
    if project_file.suffix.casefold() != PROJECT_FILE_SUFFIX or not project_file.is_file():
        raise ConversionError(tr("no_project_file", path=project_file))
    return project_file.resolve().parent


def extract_base_project(archive: Path, target_dir: Path, old_project_file: Path,
                         suffix: str = NEW_PROJECT_SUFFIX) -> Path:
    """Unpack a zipped BkNG base project into ``target_dir/<old name><suffix>`` (e.g. A0320_AS6).

    The project may lie at the top of the archive or in a folder of it.  Its
    ``.apj`` file is renamed like the folder; the path of the renamed file is returned.
    """
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise ConversionError(tr("no_zip_file", path=archive))
    if not target_dir.is_dir():
        raise ConversionError(tr("no_target_dir", path=target_dir))
    name = old_project_file.stem + suffix
    destination = target_dir.resolve() / name
    if destination.exists():
        raise ConversionError(tr("target_exists", path=destination))
    project = destination / (name + PROJECT_FILE_SUFFIX)
    unpack_project(archive, destination).rename(project)
    return project


def extract_source_project(archive: Path, work_dir: Path) -> Path:
    """Unpack a zipped source project (Bk2000 or BkNG) into ``work_dir/<archive name>``.

    The source project is only read, so it keeps its name; ``work_dir`` is
    usually a temporary folder.  Returns the path of its ``.apj`` file.
    """
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise ConversionError(tr("no_source_zip", path=archive))
    return unpack_project(archive, work_dir.resolve() / archive.stem)


def unpack_project(archive: Path, destination: Path) -> Path:
    """Unpack the Automation Studio project of ``archive`` into the new folder ``destination``.

    The project (an ``.apj`` file next to Physical/Physical.pkg) may lie at the
    top of the archive or in a folder of it; only its folder is unpacked.  Returns
    the path of the unpacked ``.apj`` file.
    """
    with zipfile.ZipFile(archive) as zip_file:
        members = [(info, info.filename.replace("\\", "/")) for info in zip_file.infolist()]
        names = {member.casefold() for _, member in members}
        projects = [member for _, member in members
                    if member.casefold().endswith(PROJECT_FILE_SUFFIX)
                    and (member.rpartition("/")[0] + "/Physical/Physical.pkg").lstrip("/").casefold() in names]
        if len(projects) != 1:
            raise ConversionError(tr("zip_no_project", path=archive))
        prefix, _, project_file = projects[0].rpartition("/")
        prefix = prefix + "/" if prefix else ""
        destination.mkdir()
        try:
            for info, member in members:
                if not member.startswith(prefix) or info.is_dir():
                    continue
                target = (destination / member[len(prefix):]).resolve()
                if destination not in target.parents:
                    continue  # skip entries that would leave the project folder
                target.parent.mkdir(parents=True, exist_ok=True)
                with zip_file.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        except BaseException:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    return destination / project_file


def check_bk2000_project(root: Path) -> None:
    """Raise if ``root`` lacks the files of a Bk2000 project, naming every missing one."""
    present = {path.name.casefold() for path in root.rglob("*") if path.is_file()}
    missing = [name for name in BK2000_FILES if name.casefold() not in present]
    if missing:
        raise ConversionError(tr("not_bk2000", files="\n".join(f"- {name}" for name in missing), root=root))


def strip_legacy_wrapping(line: str) -> str:
    """Remove the optional double quote/comma wrapper used in .dat modules."""
    text = line.strip()
    if text.startswith('"'):
        text = text[1:]
        closing = text.rfind('"')
        if closing >= 0:
            text = text[:closing] + text[closing + 1 :]
    return text.strip().rstrip(",").strip()


def parse_assignments(line: str) -> list[Parameter]:
    return [Parameter(match.group("key"), match.group("value")) for match in ASSIGNMENT_RE.finditer(line)]


def parse_block_defaults(dplist_text: str) -> dict[str, list[Parameter]]:
    """Return default parameter blocks keyed by legacy type suffix (RK, AP, ...)."""
    defaults: dict[str, list[Parameter]] = {}
    active: str | None = None
    for raw_line in dplist_text.splitlines():
        line = strip_legacy_wrapping(raw_line)
        if not line or line.startswith(";"):
            continue
        block = BLOCK_RE.match(line)
        if block:
            active = block.group("name").upper()
            # A new block replaces the preceding definition for that type.
            defaults[active] = []
            continue
        if DATAPOINT_RE.match(line):
            active = None
            continue
        if active and re.match(r"^#(?:PAB|VIS)\b", line, re.IGNORECASE):
            defaults[active].extend(parse_assignments(line))
    return defaults


def parse_datapoints(dplist_text: str) -> dict[str, Datapoint]:
    """Parse datapoints and the latest preceding defaults for each one."""
    datapoints: dict[str, Datapoint] = {}
    active: Datapoint | None = None
    active_block: str | None = None
    current_defaults: dict[str, list[Parameter]] = {}
    for raw_line in dplist_text.splitlines():
        line = strip_legacy_wrapping(raw_line)
        if not line or line.startswith(";"):
            continue
        block = BLOCK_RE.match(line)
        if block:
            active = None
            active_block = block.group("name").upper()
            # The last block definition is authoritative from this point on.
            current_defaults[active_block] = []
            continue
        match = DATAPOINT_RE.match(line)
        if match:
            active = Datapoint(
                match.group("name"),
                block_defaults={key: list(value) for key, value in current_defaults.items()},
            )
            datapoints[active.name.casefold()] = active
            active_block = None
            continue
        if active_block and re.match(r"^#(?:PAB|VIS)\b", line, re.IGNORECASE):
            current_defaults[active_block].extend(parse_assignments(line))
            continue
        if active is None:
            continue
        text = TEXT_RE.match(line)
        if text:
            active.text = text.group("text").strip().strip('"').strip()
        elif re.match(r"^#PAB\b", line, re.IGNORECASE):
            active.pab_parameters.extend(parse_assignments(line))
        elif re.match(r"^#VIS\b", line, re.IGNORECASE):
            active.vis_parameters.extend(parse_assignments(line))
        # #VI1 and #UVIS, along with unrelated module settings, are ignored.
    return datapoints


def parse_groups(groups_text: str) -> dict[str, str]:
    groups: dict[str, str] = {}
    active_group: str | None = None
    for raw_line in groups_text.splitlines():
        line = strip_legacy_wrapping(raw_line)
        if not line or line.startswith(";"):
            continue
        group = GROUP_RE.match(line)
        if group:
            # The complete text after "$" is the user-visible group name.
            active_group = group.group("name").strip()
            continue
        datapoint = DATAPOINT_RE.match(line)
        if datapoint and active_group:
            groups[datapoint.group("name").casefold()] = active_group
    return groups


def type_block_name(var_type: str) -> str:
    return re.sub(r"^Bk", "", var_type, flags=re.IGNORECASE).upper()


def mapped_key(key: str, var_type: str) -> str | None:
    key_lower = key.casefold()
    if key_lower == "y.einh":
        return "UnitY"
    if key_lower.endswith(".einh"):
        return "Unit"
    # These legacy fields are used by the old runtime but are not fields of
    # the corresponding BkNG structures (confirmed by the BkNG example).
    if var_type.casefold() == "bkrk" and key_lower in {"w", "xw"}:
        return None
    if var_type.casefold() == "bksw" and key_lower == "sw":
        return None
    if key_lower.endswith(".kpos"):
        prefix = key_lower[:-5]
        if var_type.casefold() == "bkrk":
            if prefix == "w":
                return "KposXW"
            if prefix == "y":
                return "KposY"
        return "Kpos"
    replacements = {
        "w.max": "Wmax", "w.min": "Wmin", "sw.max": "SWmax", "sw.min": "SWmin",
        "yrmin": "YrMin", "yrmax": "YrMax", "smax": "Smax", "wert.t0": "BP0_Name",
        "wert.t1": "BP1_Name", "xwg": "Xwg", "bamax": "BAmax", "swaut": "SWaut",
        "ba.t0": "BA0_Name", "ba.t1": "BA1_Name", "ba.t2": "BA2_Name", "ba.t3": "BA3_Name",
        "ba.t4": "BA4_Name", "saus.t0": "SB0_Name", "saus.t1": "SB1_Name",
        "saus.t2": "SB2_Name", "saus.t3": "SB3_Name", "saus.t4": "SB4_Name",
        "rmgsb.t0": "SB0_RmName", "rmgsb.t1": "SB1_RmName", "rmgsb.t2": "SB2_RmName",
        "rmgsb.t3": "SB3_RmName", "rmg.t0": "SBX_RmName", "rmg.t1": "SB0_RmName",
        "rmg.t2": "SB1_RmName", "rmg.t3": "SBY_RmName",
    }
    return replacements.get(key_lower, key)


def format_value(value: str, key: str) -> str:
    value = value.strip()
    if key.casefold() == "hand" and value.casefold() == "a":
        return "0"
    if key.casefold() == "int" and value.casefold() in {"i", "e"}:
        return "1" if value.casefold() == "i" else "0"
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        return "'" + value[1:-1].replace("'", "''") + "'"
    string_key = key in {"Unit", "UnitY"} or key.endswith(("_Name", "RmName"))
    if string_key and not re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", value):
        return "'" + value.replace("'", "''") + "'"
    return value


def add_parameter(parameters: OrderedDict[str, str], key: str, value: str, var_type: str) -> None:
    new_key = mapped_key(key, var_type)
    if new_key is None:
        return
    # A later explicit datapoint parameter replaces a default of the same key.
    parameters[new_key.casefold()] = f"{new_key}:={format_value(value, new_key)}"


def render_datapoint(declaration: re.Match[str], datapoint: Datapoint, group: str | None,
                     defaults: Iterable[Parameter]) -> str:
    name, var_type = declaration.group("name"), declaration.group("type")
    parameters: OrderedDict[str, str] = OrderedDict()
    # The order is deliberately readable: visual data/text, explicit PAB data, then defaults.
    for parameter in datapoint.vis_parameters:
        add_parameter(parameters, parameter.key, parameter.value, var_type)
    if datapoint.text:
        parameters["klartext"] = "Klartext:='" + datapoint.text.replace("'", "''") + "'"
    for parameter in datapoint.pab_parameters:
        add_parameter(parameters, parameter.key, parameter.value, var_type)
    for parameter in defaults:
        key = mapped_key(parameter.key, var_type)
        if key is None:
            continue
        parameters.setdefault(key.casefold(), f"{key}:={format_value(parameter.value, key)}")
    if group:
        parameters["gruppe"] = "Gruppe:='" + group.replace("'", "''") + "'"
    comment = datapoint.text or (declaration.group("comment") or "").removeprefix("(*").removesuffix("*)").strip()
    suffix = f" (*{comment}*)" if comment else ""
    if not parameters:
        return f"{name} : {var_type};{suffix}"
    return f"{name} : {var_type} := ({','.join(parameters.values())});{suffix}"


FUNCTION_BLOCK_RE = re.compile(r"\bFUNCTION_BLOCK\s+(?P<name>[A-Za-z_]\w*)(?P<body>.*?)\bEND_FUNCTION_BLOCK\b",
                               re.IGNORECASE | re.DOTALL)
IEC_MEMBER_NAMES_RE = re.compile(r"^\s*(?P<names>[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*:(?!=)", re.MULTILINE)
# A declaration with an initializer of a Bk type: "Name : BkXy := (...);"
BK_INITIALIZER_RE = re.compile(r"^(?P<head>\s*[A-Za-z_]\w*\s*:\s*(?P<type>Bk\w*)\s*:=\s*)\((?P<init>.*)\)\s*;",
                               re.IGNORECASE | re.MULTILINE)
INITIALIZER_MEMBER_RE = re.compile(r"(?P<prefix>[(,]\s*)(?P<name>[A-Za-z_]\w*)(?=\s*:=)")


def bk_type_members(logical: Path) -> tuple[dict[str, dict[str, str]], bool]:
    """Return the members of the Bk function blocks by casefolded type: {casefolded member: member}.

    They are read from the declarations (.fun) of the project, e.g. bkfbklib.fun, so that
    initializers can use the exact spelling (Automation Studio 6 distinguishes the case of
    members).  Without them, the members known to the converter are used.  The flag tells
    whether the members come from the project (only then unknown members are reported).
    """
    members: dict[str, dict[str, str]] = {}
    for path in sorted(logical.rglob("*.fun")) if logical.is_dir() else []:
        for block in FUNCTION_BLOCK_RE.finditer(blank_iec_comments(read_text(path))):
            if block.group("name").casefold().startswith("bk"):
                names = members.setdefault(block.group("name").casefold(), {})
                for match in IEC_MEMBER_NAMES_RE.finditer(block.group("body")):
                    for name in match.group("names").split(","):
                        names.setdefault(name.strip().casefold(), name.strip())
    if members:
        return members, True
    return {name: {member.casefold(): member for member in names} for name, names in OPCUA_TYPE_MEMBERS.items()}, False


def normalize_initializer_members(text: str, members: dict[str, dict[str, str]]) -> tuple[str, list[tuple[str, str]]]:
    """Write the members in the initializers of Bk declarations as the function block declares them.

    Returns the text and the (type, member) pairs that the function block does not have.
    """
    unknown: list[tuple[str, str]] = []

    def fix_declaration(declaration: re.Match[str]) -> str:
        names = members.get(declaration.group("type").casefold())
        if not names:
            return declaration.group(0)

        init = "(" + declaration.group("init")

        def fix_member(member: re.Match[str]) -> str:
            if init.count("'", 0, member.start()) % 2:
                return member.group(0)  # inside a string, e.g. Klartext:='a, b:=c'
            name = names.get(member.group("name").casefold())
            if name is None:
                unknown.append((declaration.group("type"), member.group("name")))
                return member.group(0)
            return member.group("prefix") + name

        fixed = INITIALIZER_MEMBER_RE.sub(fix_member, init)[1:]
        start, end = declaration.start("init") - declaration.start(), declaration.end("init") - declaration.start()
        return declaration.group(0)[:start] + fixed + declaration.group(0)[end:]

    return BK_INITIALIZER_RE.sub(fix_declaration, text), unknown


def unknown_member_warnings(unknown: list[tuple[str, str]]) -> list[str]:
    """One warning per type for the initializer members its function block does not have."""
    by_type: dict[str, list[str]] = {}
    for var_type, member in unknown:
        names = by_type.setdefault(var_type, [])
        if member not in names:
            names.append(member)
    return [tr("warn_unknown_members", type=var_type, members=", ".join(names)) for var_type, names in by_type.items()]


def normalize_var_header(section: re.Match[str]) -> str:
    """Rebuild a VAR section header, keeping only qualifiers that matter (CONSTANT/RETAIN/...)."""
    qualifiers = [word.upper() for word in section.group(1).split() if word.upper() in SECTION_QUALIFIER_KEYWORDS]
    return " ".join(["VAR", *qualifiers])


def convert(old_root: Path, new_root: Path, chooser: FileChooser | None = None) -> ConversionResult:
    old_root, new_root = old_root.resolve(), new_root.resolve()
    if not old_root.is_dir() or not new_root.is_dir():
        raise ConversionError(tr("no_directories"))
    check_bk2000_project(old_root)
    old_files = {name: discover_file(old_root, name, chooser) for name in REQUIRED_OLD_FILES}
    target = discover_file(new_root, "global.var", chooser)

    old_global = read_text(old_files["global.var"])
    target_text, target_encoding = read_text_with_encoding(target)
    dplist = read_text(old_files["dplist.dat"])
    datapoints = parse_datapoints(dplist)
    groups = parse_groups(read_text(old_files["gruppen.dat"]))

    output: list[str] = []
    converted = copied = unmatched = 0
    warnings: list[str] = []
    visuststat_inserted = False
    for line in old_global.splitlines():
        section = VAR_SECTION_RE.match(line)
        if section:
            header = normalize_var_header(section)
            output.append(header)
            # The mandatory declaration must live in a plain (unqualified) section;
            # it is inserted once, into the first such section encountered.
            if header == "VAR" and not visuststat_inserted:
                output.append(REQUIRED_VIS_UST_STATUS)
                visuststat_inserted = True
            continue
        if END_VAR_RE.match(line):
            output.append("END_VAR")
            continue
        generic_declaration = GENERIC_VARIABLE_RE.match(line)
        if not generic_declaration:
            continue
        if generic_declaration.group("name").casefold() == "visuststat":
            # This mandatory declaration is emitted once, above, instead.
            continue
        declaration = VARIABLE_RE.match(line)
        if declaration is None:
            output.append(line)
            copied += 1
            continue
        name = declaration.group("name")
        datapoint = datapoints.get(name.casefold())
        if datapoint is None:
            output.append(line)
            copied += 1
            continue
        is_status = declaration.group("type").casefold() == "bkstat"
        # BkStat datapoints never carry a Gruppe, even if gruppen.dat lists them.
        group = None if is_status else groups.get(name.casefold())
        if group is None and not is_status and name.casefold() != "uststatus":
            warnings.append(tr("warn_no_group", name=name))
        output.append(render_datapoint(declaration, datapoint, group,
                                      datapoint.block_defaults.get(type_block_name(declaration.group("type")), [])))
        converted += 1

    declared_names = {
        match.group("name").casefold()
        for match in map(GENERIC_VARIABLE_RE.match, old_global.splitlines()) if match
    }
    unmatched = len(set(datapoints) - declared_names)
    if unmatched:
        warnings.append(tr("warn_unmatched", count=unmatched))
    if not output:
        raise ConversionError(tr("no_declarations"))
    if not visuststat_inserted:
        # The old file had no plain VAR section at all (unusual); add one up front.
        output = ["VAR", REQUIRED_VIS_UST_STATUS, "END_VAR", *output]

    newline = "\r\n" if "\r\n" in target_text else "\n"
    # The dplist spells parameters freely (YMin, Ymin); AS6 needs the members as declared.
    try:
        logical = logical_dir(new_root)
    except ConversionError:
        logical = target.parent
    members, from_project = bk_type_members(logical)
    text, unknown = normalize_initializer_members(newline.join(output) + newline, members)
    if from_project:
        warnings += unknown_member_warnings(unknown)
    target.write_text(text, encoding=target_encoding, newline="")
    ust_name = parse_ust_name(dplist)
    ust_target = write_ust_name(new_root, ust_name, chooser, warnings) if ust_name else None
    return ConversionResult(target, converted, copied, unmatched, warnings, ust_name, ust_target)


UST_NAME_RE = re.compile(r'^\s*"?\s*#VIS\s+(?:.*?\s)?UST\.Name\s*=\s*(?P<value>.*)$', re.IGNORECASE)
UST_NAME_DECLARATION_RE = re.compile(
    r"^(?P<head>\s*UST_Name\s*:\s*STRING(?:\s*\[\s*(?P<length>\d+)\s*\])?)\s*(?::=\s*'(?:\$.|[^'$])*'\s*)?;",
    re.IGNORECASE | re.MULTILINE)
VISU_TASK_FOLDER = "visutask"
VISU_VARIABLES_FILE = "variables.var"


def parse_ust_name(dplist_text: str) -> str | None:
    """Return the UST name from the ``#VIS UST.Name=...`` line of the dplist, if any.

    Commented lines and alternative languages (#VI1, #VIS1, ...) are ignored.
    """
    for raw_line in dplist_text.splitlines():
        match = UST_NAME_RE.match(raw_line)
        if not match:
            continue
        value = match.group("value").strip()
        if value.startswith("'") and "'" in value[1:]:
            return value[1:value.index("'", 1)].strip() or None
        if raw_line.lstrip().startswith('"'):
            value = value.split('"', 1)[0]  # the closing quote of the dplist line
        value = value.split(";", 1)[0]
        # The name may contain spaces, but ends where a further "Key=" assignment starts.
        value = re.split(r"\s+[A-Za-z_][\w.]*\s*=", value, maxsplit=1)[0]
        return value.strip().rstrip(",").strip() or None
    return None


def iec_string(text: str) -> str:
    """Quote ``text`` as an IEC 61131-3 string literal."""
    return "'" + text.replace("$", "$$").replace("'", "$'") + "'"


def write_ust_name(new_root: Path, ust_name: str, chooser: FileChooser | None, warnings: list[str]) -> Path | None:
    """Write ``ust_name`` to UST_Name in the VisuTask's Variables.var. Problems become warnings."""
    candidates = sorted(
        (path for path in new_root.rglob("*") if path.is_file() and path.name.casefold() == VISU_VARIABLES_FILE
         and path.parent.name.casefold() == VISU_TASK_FOLDER),
        key=lambda path: (len(path.parts), str(path).casefold()))
    logical = [path for path in candidates if any(part.casefold() == "logical" for part in path.relative_to(new_root).parts)]
    candidates = logical or candidates
    if not candidates:
        warnings.append(tr("warn_no_visu_variables", root=new_root))
        return None
    target = candidates[0]
    if len(candidates) > 1:
        target = chooser(VISU_VARIABLES_FILE, new_root, candidates) if chooser else None
        if target is None:
            warnings.append(tr("warn_ust_name_not_written", name=ust_name))
            return None
    text, encoding = read_text_with_encoding(target)
    match = UST_NAME_DECLARATION_RE.search(text)
    if match is None:
        warnings.append(tr("warn_no_ust_name_declaration", path=target))
        return None
    if match.group("length") and len(ust_name) > int(match.group("length")):
        warnings.append(tr("warn_ust_name_too_long", name=ust_name, length=match.group("length")))
    try:
        ust_name.encode(encoding)
    except UnicodeEncodeError:
        encoding = "utf-8-sig"
    replacement = f"{match.group('head')} := {iec_string(ust_name)};"
    target.write_text(text[:match.start()] + replacement + text[match.end():], encoding=encoding, newline="")
    return target


PACKAGE_FILE = "Package.pkg"
NEW_PACKAGE_TEMPLATE = (
    '﻿<?xml version="1.0" encoding="utf-8"?>\r\n'
    '<?AutomationStudio FileVersion="4.9"?>\r\n'
    '<Package xmlns="http://br-automation.co.at/AS/Package">\r\n'
    "  <Objects>\r\n"
    "  </Objects>\r\n"
    "</Package>"
)


def logical_dir(root: Path) -> Path:
    """Return the Logical folder of an Automation Studio project (or the folder itself)."""
    if root.name.casefold() == "logical":
        return root
    for child in root.iterdir():
        if child.is_dir() and child.name.casefold() == "logical":
            return child
    raise ConversionError(tr("no_logical", root=root))


def find_ladder_tasks(logical: Path) -> list[Path]:
    """Return the task folders below ``logical`` that contain .ld files, outermost first."""
    folders = sorted({path.parent for path in logical.rglob("*") if path.is_file() and path.suffix.casefold() == ".ld"},
                     key=lambda path: (len(path.parts), str(path).casefold()))
    tasks: list[Path] = []
    for folder in folders:
        # A nested folder is copied together with its enclosing task.
        if folder != logical and not any(task in folder.parents for task in tasks):
            tasks.append(folder)
    return tasks


def package_object_line(package_text: str, child: str) -> str | None:
    """Return the ``<Object ...>child</Object>`` entry of a Package.pkg, if listed."""
    match = re.search(rf"<Object\b[^>]*>\s*{re.escape(child)}\s*</Object>", package_text, re.IGNORECASE)
    return match.group(0) if match else None


def empty_package(package_text: str) -> str:
    """Return a Package.pkg with the header (package type) of ``package_text`` but without objects."""
    text = re.sub(r"<Objects\s*/>|<Objects>.*?</Objects>", "<Objects>\r\n  </Objects>", package_text, count=1,
                  flags=re.DOTALL)
    return text if "</Objects>" in text else NEW_PACKAGE_TEMPLATE


def ensure_package_entry(old_parent: Path, new_parent: Path, child: str, object_type: str = "Package",
                         package_file: str = PACKAGE_FILE, keep_old_header: bool = False) -> bool:
    """List ``child`` in the new parent's Package.pkg, reusing the old entry. Returns True if changed.

    With ``keep_old_header`` a missing Package.pkg is created with the header of the
    old one, so that e.g. mappView page packages keep their package type.
    """
    new_package = new_parent / package_file
    if new_package.exists():
        text, encoding = read_text_with_encoding(new_package)
    elif keep_old_header and (old_parent / package_file).exists():
        text, encoding = empty_package(read_text(old_parent / package_file)), "utf-8"
    else:
        text, encoding = NEW_PACKAGE_TEMPLATE, "utf-8"
    if package_object_line(text, child):
        return False
    old_package = old_parent / package_file
    entry = package_object_line(read_text(old_package), child) if old_package.exists() else None
    if entry is None:
        language = {"Program": ' Language="IEC"', "DataObject": ' Language="Simple"'}.get(object_type, "")
        entry = f'<Object Type="{object_type}"{language}>{child}</Object>'
    newline = "\r\n" if "\r\n" in text else "\n"
    closing = re.search(r"^([ \t]*)</Objects>", text, re.IGNORECASE | re.MULTILINE)
    if closing is None:
        # Automation Studio saves an empty package as <Objects />.
        empty = re.search(r"^([ \t]*)<Objects\s*/>", text, re.IGNORECASE | re.MULTILINE)
        if empty is None:
            raise ConversionError(tr("no_objects_section", child=child, package=new_package))
        indent = empty.group(1)
        text = (text[:empty.start()] + f"{indent}<Objects>{newline}{indent}  {entry}{newline}{indent}</Objects>"
                + text[empty.end():])
    else:
        text = text[:closing.start()] + closing.group(1) + "  " + entry + newline + text[closing.start():]
    new_package.write_text(text, encoding=encoding, newline="")
    return True


def copy_logical_object(old_logical: Path, new_logical: Path, relative: Path, object_type: str,
                        packages_updated: list[Path], keep_old_header: bool = False) -> tuple[int, int]:
    """Copy the object folder ``relative`` into the new Logical folder, keeping the folder structure.

    The object and every package folder above it are registered with their parent
    Package.pkg; changed packages are appended to ``packages_updated``.
    Returns the number of copied and of overwritten files.
    """
    copied = overwritten = 0
    for source in sorted((old_logical / relative).rglob("*")):
        if not source.is_file():
            continue
        destination = new_logical / relative / source.relative_to(old_logical / relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        overwritten += destination.exists()
        shutil.copy2(source, destination)
        copied += 1
    for depth in range(len(relative.parts)):
        parent_relative = Path(*relative.parts[:depth])
        new_parent = new_logical / parent_relative
        if ensure_package_entry(old_logical / parent_relative, new_parent, relative.parts[depth],
                                object_type if depth == len(relative.parts) - 1 else "Package",
                                keep_old_header=keep_old_header):
            package = new_parent / PACKAGE_FILE
            if package not in packages_updated:
                packages_updated.append(package)
    return copied, overwritten


def copy_ladder_tasks(old_root: Path, new_root: Path, chooser: FileChooser | None = None,
                      convert_ladder: bool = False) -> LadderCopyResult:
    """Copy every task containing .ld files into the new project, keeping the folder structure.

    With ``convert_ladder`` old Ladder Diagrams are converted with Automation Studio 6.

    Missing package folders are created and each parent Package.pkg is extended
    so that Automation Studio shows the copied packages and programs.  Each task
    is then added to the same task class in the Cpu.sw of the new project's
    active configuration as in the old project.
    """
    old_logical, new_logical = logical_dir(old_root.resolve()), logical_dir(new_root.resolve())
    tasks = find_ladder_tasks(old_logical)
    if not tasks:
        raise ConversionError(tr("no_ladder_tasks", root=old_logical))
    copied = overwritten = 0
    packages_updated: list[Path] = []
    for task in tasks:
        files, replaced = copy_logical_object(old_logical, new_logical, task.relative_to(old_logical), "Program",
                                              packages_updated)
        copied += files
        overwritten += replaced
    relatives = [task.relative_to(old_logical) for task in tasks]
    deployed, warnings = deploy_tasks(old_root, new_root, relatives, chooser)
    upgrade = upgrade_old_ladder_files(old_ladder_files(new_logical / task for task in relatives),
                                       new_logical.parent, convert_ladder)
    ladder_details, ladder_warnings = ladder_upgrade_report(upgrade)
    warnings += ladder_warnings
    warnings += discontinued_function_warnings(new_logical, (new_logical / task for task in relatives))
    return LadderCopyResult(relatives, copied, overwritten, packages_updated, deployed, warnings, ladder_details)


SW_TASK_CLASS_RE = re.compile(r'<TaskClass\b(?P<attributes>[^>]*?)(?:/>|>(?P<body>.*?)</TaskClass>)', re.DOTALL)
SW_TASK_RE = re.compile(r'^[ \t]*<Task\b[^>]*?(?:/>|>.*?</Task>)', re.DOTALL | re.MULTILINE)


def task_source(relative: Path) -> str:
    """Return the Cpu.sw ``Source`` of a program, e.g. ``Bk2_Anwendung.Allgemein.AL00.prg``."""
    return ".".join(relative.parts) + ".prg"


def deploy_tasks(old_root: Path, new_root: Path, tasks: list[Path],
                 chooser: FileChooser | None) -> tuple[list[tuple[str, str]], list[str]]:
    """Add the copied tasks to the same task classes of the new Cpu.sw as in the old one.

    Returns the deployed (task, task class) pairs and warnings.
    """
    try:
        old_sw = configuration_cpu(active_configuration(project_root(old_root), chooser)) / "Cpu.sw"
        new_sw = configuration_cpu(active_configuration(project_root(new_root), chooser, ask=True)) / "Cpu.sw"
    except ConversionError as error:
        return [], [tr("warn_no_deployment", error=error)]
    if not old_sw.exists() or not new_sw.exists():
        return [], [tr("warn_no_deployment", error=old_sw if not old_sw.exists() else new_sw)]

    wanted = {task_source(task).casefold() for task in tasks}
    # Old deployment in file order: (task class, task element, name, source).
    old_tasks: list[tuple[str, str, str, str]] = []
    for task_class in SW_TASK_CLASS_RE.finditer(read_text(old_sw)):
        class_name = xml_attribute(task_class.group("attributes"), "Name")
        for task in SW_TASK_RE.finditer(task_class.group("body") or ""):
            element = task.group(0).strip()
            source = xml_attribute(element, "Source")
            if source.casefold() in wanted:
                old_tasks.append((class_name, element, xml_attribute(element, "Name"), source))
    deployed_sources = {source.casefold() for _, _, _, source in old_tasks}
    warnings = [tr("warn_task_not_deployed", task=task) for task in tasks
                if task_source(task).casefold() not in deployed_sources]

    text, encoding = read_text_with_encoding(new_sw)
    newline = "\r\n" if "\r\n" in text else "\n"
    deployed: list[tuple[str, str]] = []
    for class_name, element, name, source in old_tasks:
        if any(xml_attribute(task.group(0), "Source").casefold() == source.casefold()
               for task in SW_TASK_RE.finditer(text)):
            continue  # already deployed in the new project
        if any(xml_attribute(task.group(0), "Name").casefold() == name.casefold() for task in SW_TASK_RE.finditer(text)):
            warnings.append(tr("warn_task_name_in_use", task=name))
            continue
        target = next((match for match in SW_TASK_CLASS_RE.finditer(text)
                       if xml_attribute(match.group("attributes"), "Name") == class_name), None)
        if target is None:
            warnings.append(tr("warn_task_class_missing", task=name, task_class=class_name))
            continue
        line_start = text.rfind("\n", 0, target.start()) + 1
        indent = re.match(r"[ \t]*", text[line_start:]).group(0)
        task_line = f"{indent}  {element}{newline}"
        if target.group("body") is None or "\n" not in target.group("body"):
            # Expand <TaskClass ... /> (or one written on a single line) into a multi-line element.
            body = (target.group("body") or "").strip()
            replacement = (f"<TaskClass{target.group('attributes').rstrip()}>{newline}"
                           + (f"{indent}  {body}{newline}" if body else "")
                           + f"{task_line}{indent}</TaskClass>")
            text = text[:target.start()] + replacement + text[target.end():]
        else:
            # Insert as the last task, at the start of the line holding </TaskClass>.
            closing = target.end() - len("</TaskClass>")
            insert_at = text.rfind("\n", 0, closing) + 1
            text = text[:insert_at] + task_line + text[insert_at:]
        deployed.append((name, class_name))
    if deployed:
        new_sw.write_text(text, encoding=encoding, newline="")
    return deployed, warnings


# Data modules that the converter turns into global.var instead of copying them.
CONVERTED_DATA_OBJECTS = ("dplist", "gruppen")
# Bk2000 standard data modules (system texts of Bk2_System/Bk2_Texte); they are not copied either.
BK_STANDARD_DATA_OBJECTS = ("text1int", "text2int", "text3int")
PTOP_FILE = "ptop.dat"
# Example configurations of the PtoP module (e.g. PtopData/Sample1/ptop/ptop.dat) are no PtoP coupling.
PTOP_SAMPLE_FOLDER_RE = re.compile(r"^Sample\d*$", re.IGNORECASE)
SW_DATA_OBJECTS_RE = re.compile(r"^(?P<indent>[ \t]*)<DataObjects\b[^>]*?(?:/>|>(?P<body>.*?)</DataObjects>)",
                                re.DOTALL | re.MULTILINE)
SW_DATA_OBJECT_RE = re.compile(r"^[ \t]*<DataObject\b[^>]*?/>", re.MULTILINE)
# Every deployed object of a Cpu.sw (task, data object, library, ...) except the task classes.
SW_NAMED_OBJECT_RE = re.compile(r"<(?!TaskClass\b)(?P<tag>\w+)\b[^>]*?\bName=\"(?P<name>[^\"]*)\"")
# Sections that follow <DataObjects> in a Cpu.sw; a new <DataObjects> goes in front of the first one.
SW_SECTIONS_AFTER_DATA_OBJECTS = ("NcDataObjects", "VcDataObjects", "Binaries", "Libraries")
SW_LIBRARY_RE = re.compile(r"<LibraryObject\b[^>]*>")
# B&R standard libraries; libraries starting with As (AsTime, AsString, ...), Mp, Mc or Arn are B&R as well.
BR_LIBRARIES = {name.casefold() for name in (
    "sys_lib", "standard", "runtime", "brsystem", "operator", "convert", "fileio", "dataobj", "dvframe",
    "ethernet", "ethsock", "visapi", "vcresman", "can_lib", "loopcont", "loopconr", "drv_mbus", "powerlnk",
    "dmrout", "iecstring", "iecconv", "inaclnt", "astime", "asstring", "mpcom", "mpbase", "cotrace")}
BR_LIBRARY_PREFIX_RE = re.compile(r"^(?:As|Mp|Mc|Arn)[A-Z0-9_]", re.IGNORECASE)
# Burklimat standard libraries; like the B&R libraries they are not reported.
BK_STANDARD_LIBRARIES = {name.casefold() for name in ("bkfbklib", "bkuselib", "bktools", "MyCheck")}


# B&R libraries discontinued in Automation Studio 6: casefolded name -> (reason, successor).  The
# reason is the key of a "reason_..." text ("" for none); "replaced" names the successor.
DISCONTINUED_LIBRARIES: dict[str, tuple[str, str]] = {name.casefold(): (reason, successor) for name, reason, successor in (
    ("AsARCNET", "sg3", ""), ("AsSGCIO", "sgc", ""), ("AsTPU", "sg3", ""), ("C220man", "sg3", ""),
    ("CANIO", "sg3", ""), ("DM_Lib", "sg3_sgc", ""), ("FDD_lib", "sg3_sgc", ""), ("IF361", "sg3", ""),
    ("IO_lib", "sg3", ""), ("IOConfig", "sg3", ""), ("IOCtrl", "sg3", ""), ("PB_lib", "sg3", ""),
    ("PBIXMAN", "sg3", ""), ("RIO_lib", "sg3", ""), ("SPSIOMAN", "sg3", ""), ("TCPIPMGR", "sg3", ""),
    ("AsMc", "sg3", ""), ("AsNetX", "sgc", ""), ("Logging", "replaced", "AsArProf"), ("AsIMA", "ina2000", ""),
    ("Commserv", "ina2000", ""), ("INAclnt", "ina2000", ""), ("AsKey", "dallas", ""), ("AsSLIP", "slip", ""),
    ("AsArLog", "replaced", "ArEventLog"), ("AsSound", "pp500", ""), ("AsErrTxt", "", ""), ("AsPlkSup", "sg3", ""),
    ("IF661", "sg3", ""), ("ppdpr", "sg3_pp", ""), ("printer", "", ""), ("Spooler", "system2005", ""),
    ("SRAM200x", "", ""), ("AsString", "", ""), ("AsMath", "", ""), ("AsCisMan", "", ""),
    ("CONVERT", "replaced", "AsIecCon"), ("AsPciExt", "", ""), ("AsWStr", "replaced", "AsBrWStr"),
    ("NET2000", "net2000", ""), ("FB_lib", "profibus", ""), ("DPMaster", "profibus", ""),
    ("AsPROFIBUS", "profibus", ""), ("AsSafety", "legacy_safety", ""),
    ("LoopCont", "replaced", "mapp Temperature, mapp Control Tools"),
    ("AsHydCon", "replaced", "mapp Hydraulics, mapp Control Tools"), ("ArAutoId", "", ""))}
# Single function blocks/functions discontinued in Automation Studio 6: (name, library, reason, successor).
# A name ending in "_*" stands for every function with this prefix (SYS_Lib AVT_*, DA_*, ...).
DISCONTINUED_FUNCTIONS: list[tuple[str, str, str, str]] = [
    *((name, "AsIOAcc", "sgc", "") for name in ("AsIOAccWriteReg", "AsIOAccReadReg")),
    *((name, "BRSystem", "arwin", "") for name in ("ARwinEthWinInfo", "ARwinWindowsInfo")),
    *((name, "BRSystem", "sg3", "") for name in ("ZYKVLenable", "EXCInfo", "PMemGet", "PMemPut", "PMemSize")),
    ("SysconfInfo", "BRSystem", "replaced", "AsARCfg"), ("SysconfSet", "BRSystem", "replaced", "AsARCfg"),
    ("BatteryInfo", "BRSystem", "replaced", "HwGetBatteryInfo (AsHw)"),
    ("MEMInfo", "BRSystem", "replaced", "MEMxInfo"), ("HWInfo", "BRSystem", "replaced", "AsIODiag"),
    *((name, "CAN_Lib", "sg3", "") for name in ("CANnode", "CANxnode", "GetNdNr", "SetNdNr")),
    ("ArUserExport", "ArUser", "replaced", "ArUserExportEx"), ("ArUserImport", "ArUser", "replaced", "ArUserImportEx"),
    ("ETHnode", "Ethernet", "sg3", ""),
    ("ArCertGenerate", "ArCert", "", ""), ("ArCertGenerateOpcUA", "ArCert", "", ""),
    ("CfgSetBroadcastAddr", "AsArCfg", "ar_before_4", ""),
    ("CfgSetSntpServer", "AsArCfg", "replaced", "CfgSetNtpServer"),
    ("CfgSetSntpClient", "AsArCfg", "replaced", "CfgSetNtpClient"),
    ("CfgGetSntpcData", "AsArCfg", "replaced", "CfgGetNtpcData"),
    ("CfgGetSntpsData", "AsArCfg", "replaced", "CfgGetNtpsData"),
    ("ERR_read", "SYS_Lib", "replaced", "ArEventLog"), ("ERRxread", "SYS_Lib", "replaced", "ArEventLog"),
    *((name, "SYS_Lib", "sg3", "") for name in (
        "AVT_*", "DA_*", "DIS_*", "KEY_*", "SM_*", "UT_*", "MEM_alloc", "MEM_free", "ST_resume", "ST_suspend",
        "SYS_info", "SYSxinfo", "SYS_battery", "FORCE_info", "SW_gettime", "SW_settime", "PV_ident", "PV_getadr",
        "PV_setval", "PV_getval", "PV_xsetval", "PV_xgetval", "PV_info", "PV_list")),
]
# Prefixes match case-sensitively and only before a lower-case letter (DA_create, not DA_Temperatur).
_DISCONTINUED_FUNCTION_NAMES = "|".join(
    rf"(?-i:{re.escape(name[:-1])}[a-z]\w*)" if name.endswith("_*") else re.escape(name)
    for name, _, _, _ in sorted(DISCONTINUED_FUNCTIONS, key=lambda entry: -len(entry[0])))
# A use of a discontinued function: a call, an IEC declaration (inst : MEMInfo), a C struct of a
# function block instance or a block of a Ladder Diagram (old format type="...", PLCopen typeName="...").
DISCONTINUED_FUNCTION_RE = re.compile(
    rf"\b(?P<call>{_DISCONTINUED_FUNCTION_NAMES})\s*\(|:\s*(?P<declaration>{_DISCONTINUED_FUNCTION_NAMES})\b"
    rf"|\bstruct\s+(?P<struct>{_DISCONTINUED_FUNCTION_NAMES})\b|\btype(?:Name)?=\"(?P<block>{_DISCONTINUED_FUNCTION_NAMES})\"",
    re.IGNORECASE)
# Source files searched for discontinued functions.
PROGRAM_SOURCE_SUFFIXES = (".st", ".c", ".cpp", ".h", ".ab", ".ld", ".var", ".typ", ".fun")


def discontinued_reason(reason: str, successor: str) -> str:
    """Return " (reason)" of a discontinued library or function, or "" without a reason."""
    return f" ({tr('reason_' + reason, successor=successor)})" if reason else ""


def missing_library_warnings(missing: list[str]) -> list[str]:
    """Warnings for B&R libraries of the old project missing in the new one; discontinued ones get their own."""
    warnings = []
    for name in missing:
        if name.casefold() in DISCONTINUED_LIBRARIES:
            reason, successor = DISCONTINUED_LIBRARIES[name.casefold()]
            warnings.append(tr("warn_library_replaced", name=name, successor=successor) if successor
                            else tr("warn_library_discontinued", name=name, reason=discontinued_reason(reason, "")))
    available = [name for name in missing if name.casefold() not in DISCONTINUED_LIBRARIES]
    if available:
        warnings.insert(0, tr("warn_br_libraries_missing", names=", ".join(available)))
    return warnings


def discontinued_function(name: str) -> tuple[str, str, str, str]:
    """Return the DISCONTINUED_FUNCTIONS entry of a found function name (with its own name for a prefix)."""
    for entry in DISCONTINUED_FUNCTIONS:
        if entry[0].endswith("_*") and name.startswith(entry[0][:-1]):
            return (name, *entry[1:])
        if entry[0].casefold() == name.casefold():
            return entry
    raise KeyError(name)


def discontinued_function_warnings(logical: Path, folders: Iterable[Path]) -> list[str]:
    """Warnings for the functions/function blocks discontinued in AS 6 that the sources in ``folders`` use."""
    uses: dict[str, tuple[tuple[str, str, str, str], set[str]]] = {}
    for folder in folders:
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.casefold() not in PROGRAM_SOURCE_SUFFIXES or is_encrypted_source(path):
                continue
            for match in DISCONTINUED_FUNCTION_RE.finditer(read_text(path)):
                entry = discontinued_function(next(group for group in match.groups() if group))
                uses.setdefault(entry[0].casefold(), (entry, set()))[1].add(str(path.relative_to(logical)))
    return [tr("warn_function_discontinued", name=name, library=library,
               reason=discontinued_reason(reason, successor), files=", ".join(sorted(files)))
            for (name, library, reason, successor), files in sorted(uses.values(), key=lambda use: use[0][0].casefold())]


def is_ptop_name(name: str) -> bool:
    """True for an object of the PtoP coupling (PtopLib, PtoP_Module, chkptop, PeerToPeer, ...).

    PtoP is discontinued in Automation Studio 6 and replaced by OPC UA; it is not taken over.
    """
    return "ptop" in name.casefold() or "peertopeer" in name.casefold()


def is_ptop_object(relative: Path) -> bool:
    """True for a task, data module or library of the PtoP coupling, by its path relative to Logical."""
    return any(is_ptop_name(part) for part in relative.parts)


@dataclass
class DataObjectCopyResult:
    objects: list[Path]
    files_copied: int
    files_overwritten: int
    packages_updated: list[Path]
    deployed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    # (name, language, also deployed in the BkNG project) of every non-B&R library.
    libraries: list[tuple[str, str, bool]]
    library_source: Path | None
    ptop: list[Path]
    warnings: list[str] = field(default_factory=list)


def find_data_objects(logical: Path) -> list[Path]:
    """Return the data module folders (a .dob file next to a .dat file) below ``logical``."""
    folders = sorted({path.parent for path in logical.rglob("*.dat") if path.is_file()
                      and any(dob.suffix.casefold() == ".dob" for dob in path.parent.iterdir())},
                     key=lambda path: str(path).casefold())
    return [folder for folder in folders
            if folder.name.casefold() not in CONVERTED_DATA_OBJECTS + BK_STANDARD_DATA_OBJECTS]


def data_object_source(relative: Path) -> str:
    """Return the Cpu.sw ``Source`` of a data module, e.g. ``CONFIG.version.dob``."""
    return ".".join(relative.parts) + ".dob"


def copy_data_objects(old_root: Path, new_root: Path, chooser: FileChooser | None = None,
                      objects: list[Path] | None = None) -> DataObjectCopyResult:
    """Copy the data modules except dplist and gruppen into the new project and deploy them like in the old one.

    ``objects`` (folders relative to Logical) replaces the search for data modules.
    """
    old_logical, new_logical = logical_dir(old_root.resolve()), logical_dir(new_root.resolve())
    if objects is None:
        objects = [folder.relative_to(old_logical) for folder in find_data_objects(old_logical)]
    result = DataObjectCopyResult(objects, 0, 0, [])
    if not objects:
        return result
    for relative in objects:
        files, replaced = copy_logical_object(old_logical, new_logical, relative, "DataObject", result.packages_updated)
        result.files_copied += files
        result.files_overwritten += replaced

    try:
        old_sw = configuration_cpu(active_configuration(project_root(old_root), chooser)) / "Cpu.sw"
        new_sw = configuration_cpu(active_configuration(project_root(new_root), chooser, ask=True)) / "Cpu.sw"
    except ConversionError as error:
        result.warnings.append(tr("warn_no_deployment", error=error))
        return result
    if not old_sw.exists() or not new_sw.exists():
        result.warnings.append(tr("warn_no_deployment", error=old_sw if not old_sw.exists() else new_sw))
        return result
    old_entries = {xml_attribute(match.group(0), "Source").casefold(): match.group(0).strip()
                   for match in SW_DATA_OBJECT_RE.finditer(read_text(old_sw))}
    text, encoding = read_text_with_encoding(new_sw)
    newline = "\r\n" if "\r\n" in text else "\n"
    entries: list[str] = []
    for relative in objects:
        source = data_object_source(relative)
        entry = old_entries.get(source.casefold())
        if entry is None:
            result.warnings.append(tr("warn_data_not_deployed", name=relative))
            continue
        if any(xml_attribute(match.group(0), "Source").casefold() == source.casefold()
               for match in SW_DATA_OBJECT_RE.finditer(text)):
            continue  # already deployed in the new project
        name = xml_attribute(entry, "Name")
        if any(match.group("name").casefold() == name.casefold() for match in SW_NAMED_OBJECT_RE.finditer(text)):
            result.warnings.append(tr("warn_data_name_in_use", name=name, source=relative))
            continue
        entries.append(entry)
        result.deployed.append(name)
    if not entries:
        return result

    section = SW_DATA_OBJECTS_RE.search(text)
    if section is None:
        following = [match for match in (re.search(rf"^[ \t]*<{tag}\b", text, re.MULTILINE)
                                         for tag in SW_SECTIONS_AFTER_DATA_OBJECTS) if match]
        at = min(match.start() for match in following) if following else text.rfind("</")
        at = text.rfind("\n", 0, at) + 1
        indent = re.match(r"[ \t]*", text[at:]).group(0) or "  "
        text = text[:at] + f"{indent}<DataObjects>{newline}{indent}</DataObjects>{newline}" + text[at:]
        section = SW_DATA_OBJECTS_RE.search(text)
    elif section.group("body") is None:
        indent = section.group("indent")
        text = (text[:section.start()] + f"{indent}<DataObjects>{newline}{indent}</DataObjects>"
                + text[section.end():])
        section = SW_DATA_OBJECTS_RE.search(text)
    indent = section.group("indent")
    closing = section.end() - len("</DataObjects>")
    insert_at = text.rfind("\n", 0, closing) + 1
    text = text[:insert_at] + "".join(f"{indent}  {entry}{newline}" for entry in entries) + text[insert_at:]
    new_sw.write_text(text, encoding=encoding, newline="")
    return result


def find_ptop_files(logical: Path) -> list[Path]:
    """Return the PtoP data modules (ptop.dat) below ``logical``, except the examples in Sample folders."""
    return sorted((path for path in logical.rglob("*")
                   if path.is_file() and path.name.casefold() == PTOP_FILE
                   and not any(PTOP_SAMPLE_FOLDER_RE.match(part) for part in path.relative_to(logical).parts[:-1])),
                  key=lambda path: str(path).casefold())


def is_standard_library(name: str, language: str) -> bool:
    """True for B&R and Burklimat standard libraries, which are not reported as customer-specific."""
    if name.casefold() in BK_STANDARD_LIBRARIES:
        return True
    # A library delivered as source code (IEC, ANSI C) is never a B&R standard library.
    return language.casefold() == "binary" and (name.casefold() in BR_LIBRARIES
                                                 or name.casefold() in DISCONTINUED_LIBRARIES
                                                 or BR_LIBRARY_PREFIX_RE.match(name) is not None)


def analyse_bk2000_project(old_root: Path, new_root: Path, chooser: FileChooser | None = None) -> AnalysisResult:
    """Find what the converter cannot take over: customer-specific libraries and a PtoP coupling."""
    old_root, new_root = old_root.resolve(), new_root.resolve()
    result = AnalysisResult([], None, [])
    result.ptop = find_ptop_files(logical_dir(old_root))
    for path in result.ptop:
        result.warnings.append(tr("warn_ptop", path=path.relative_to(old_root)))

    old_sw = configuration_cpu(active_configuration(project_root(old_root), chooser)) / "Cpu.sw"
    if not old_sw.exists():
        raise ConversionError(tr("not_found", filename="Cpu.sw", root=old_sw.parent))
    new_libraries: set[str] = set()
    try:
        new_sw = configuration_cpu(active_configuration(project_root(new_root), chooser, ask=True)) / "Cpu.sw"
        if new_sw.exists():
            new_libraries = {xml_attribute(match.group(0), "Name").casefold()
                             for match in SW_LIBRARY_RE.finditer(read_text(new_sw))}
    except ConversionError:
        pass  # without the BkNG libraries, no library is marked as present
    result.library_source = old_sw
    for match in SW_LIBRARY_RE.finditer(read_text(old_sw)):
        name, language = xml_attribute(match.group(0), "Name"), xml_attribute(match.group(0), "Language")
        if is_ptop_name(name):
            continue  # PtoP is reported as a whole (warn_ptop)
        if not is_standard_library(name, language):
            result.libraries.append((name, language, name.casefold() in new_libraries))
        elif name.casefold() in DISCONTINUED_LIBRARIES:
            result.warnings += missing_library_warnings([name])
    if result.libraries:
        result.warnings.append(tr("warn_customer_libraries", names=", ".join(name for name, _, _ in result.libraries)))
    return result


HW_MODULE_RE = re.compile(r'^[ \t]*<Module\b[^>]*?(?:/>|>.*?</Module>)[ \t]*\r?\n?', re.DOTALL | re.MULTILINE)
HW_CONNECTION_RE = re.compile(r'<Connection\b[^>]*\bConnector="(?P<connector>[^"]*)"[^>]*'
                              r'\bTargetModule="(?P<target>[^"]*)"[^>]*\bTargetConnector="(?P<target_connector>[^"]*)"')
IO_MAPPING_RE = re.compile(r'\bAT\s+%[A-Z]+\s*\.\s*"(?P<module>[^"]+)"', re.IGNORECASE)
# Connectors that only join modules mechanically (bus base, terminal block, power
# supply slot, system connector of a Power Panel CPU to its panel).  Every other
# connector (X2X, POWERLINK, Ethernet, ...) is a bus.
MECHANICAL_CONNECTOR_RE = re.compile(r"^(?:SL|SS|PS|SY)\d*$", re.IGNORECASE)


@dataclass
class HardwareModule:
    name: str
    type: str
    text: str
    connections: list[tuple[str, str, str]]


def xml_attribute(element: str, name: str) -> str:
    match = re.search(rf'\b{name}="([^"]*)"', element)
    return match.group(1) if match else ""


def parse_hardware(text: str) -> dict[str, HardwareModule]:
    modules: dict[str, HardwareModule] = {}
    for match in HW_MODULE_RE.finditer(text):
        block = match.group(0)
        start_tag = block[:block.index(">") + 1]
        name = xml_attribute(start_tag, "Name")
        connections = [(c.group("connector"), c.group("target"), c.group("target_connector"))
                       for c in HW_CONNECTION_RE.finditer(block)]
        modules[name] = HardwareModule(name, xml_attribute(start_tag, "Type"), block, connections)
    return modules


def physical_configurations(project: Path) -> list[Path]:
    physical = project / "Physical"
    package = physical / "Physical.pkg"
    if not package.exists():
        raise ConversionError(tr("no_physical", root=project))
    names = re.findall(r'<Object\b[^>]*Type="Configuration"[^>]*>\s*([^<]+?)\s*</Object>', read_text(package))
    return [physical / name for name in names if (physical / name / "Hardware.hw").exists()]


def active_configuration(project: Path, chooser: FileChooser | None, ask: bool = False) -> Path:
    """Return the active hardware configuration folder of an Automation Studio project.

    With ``ask`` the user chooses among several configurations (the active one
    is offered first) instead of taking the active one.
    """
    configurations = physical_configurations(project)
    if not configurations:
        raise ConversionError(tr("no_configuration", root=project))
    settings = project / "LastUser.set"
    active = xml_attribute(read_text(settings), "ActiveConfigurationName") if settings.exists() else ""
    configurations.sort(key=lambda configuration: configuration.name.casefold() != active.casefold())
    if len(configurations) == 1 or (configurations[0].name.casefold() == active.casefold()
                                    and not (ask and chooser)):
        return configurations[0]
    if chooser is None:
        raise ConversionError(tr("ambiguous_configuration", root=project))
    selected = chooser("Hardware.hw", project, [configuration / "Hardware.hw" for configuration in configurations])
    if selected is None:
        raise ConversionError(tr("choice_cancelled", filename="Hardware.hw"))
    return selected.parent


def configuration_cpu(configuration: Path) -> Path:
    package = read_text(configuration / "Config.pkg")
    match = re.search(r'<Object\b[^>]*Type="Cpu"[^>]*>\s*([^<]+?)\s*</Object>', package)
    if match is None or not (configuration / match.group(1)).is_dir():
        raise ConversionError(tr("no_cpu", root=configuration))
    return configuration / match.group(1)


def project_root(selection: Path) -> Path:
    """Accept a project folder or a folder below it (e.g. Logical) and return the project folder."""
    selection = selection.resolve()
    for folder in (selection, *selection.parents):
        if (folder / "Physical" / "Physical.pkg").exists():
            return folder
    raise ConversionError(tr("no_physical", root=selection))


def hardware_edges(modules: dict[str, HardwareModule]) -> dict[str, list[tuple[str, bool]]]:
    """Return the neighbours of every module with a flag whether they are joined mechanically."""
    edges: dict[str, list[tuple[str, bool]]] = {name: [] for name in modules}
    for module in modules.values():
        for connector, target, _ in module.connections:
            if target in modules:
                mechanical = bool(MECHANICAL_CONNECTOR_RE.match(connector))
                edges[module.name].append((target, mechanical))
                edges[target].append((module.name, mechanical))
    return edges


def plc_modules(modules: dict[str, HardwareModule], cpu: str) -> set[str]:
    """Return the CPU and every module attached to it mechanically (bus base, power supply,
    interface modules, the panel of a Power Panel with its interface cards, terminal blocks)."""
    edges = hardware_edges(modules)
    plc = {cpu}
    pending = [cpu]
    while pending:
        for neighbour, mechanical in edges[pending.pop()]:
            if mechanical and neighbour not in plc:
                plc.add(neighbour)
                pending.append(neighbour)
    return plc


def select_io_modules(modules: dict[str, HardwareModule], cpu: str) -> list[str]:
    """Return the X20 I/O modules connected to the PLC over a bus.

    Modules that are only attached mechanically to the CPU belong to the PLC and
    are left out (see plc_modules).  Everything reached over a bus connector (X2X,
    POWERLINK, Ethernet ...) is I/O, including bus couplers (X20BC...) with their modules.
    """
    edges = hardware_edges(modules)
    plc = plc_modules(modules, cpu)
    is_x20 = lambda name: modules[name].type.upper().startswith("X20")
    selected: set[str] = set()
    pending = [neighbour for name in plc for neighbour, mechanical in edges[name]
               if not mechanical and neighbour not in plc and is_x20(neighbour)]
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        selected.add(name)
        pending.extend(neighbour for neighbour, _ in edges[name]
                       if neighbour not in plc and neighbour not in selected and is_x20(neighbour))
    return sorted(selected)


# Type prefixes of PROFIBUS slaves described by a GSD file: generic DTM of Automation Studio
# ("FBD.GSDDTM.DTMDev.<product>[.<version>]") and native GSD import ("FBP.<ident>.<GSD file name>").
GSD_DEVICE_PREFIX = "FBD.GSDDTM.DTMDev."
GSD_FILE_DEVICE_RE = re.compile(r"^FBP\.[^.]+\.(?P<file>.+)$", re.IGNORECASE)
# Folder of an Automation Studio project with the fieldbus description files used in it.
DESCRIPTION_FOLDER = Path("AS", "Hardware")
DTM_FOLDER = "DTM"
META_TAG_RE = re.compile(r'<Tag\b[^>]*\bName="(?P<name>[^"]*)"[^>]*\bValue="(?P<value>[^"]*)"')


def child_folder(parent: Path, name: str) -> Path | None:
    """Return the sub folder ``name`` of ``parent`` regardless of case, if it exists."""
    if parent.is_dir():
        for child in parent.iterdir():
            if child.is_dir() and child.name.casefold() == name.casefold():
                return child
    return None


def gsd_descriptions(project: Path) -> list[tuple[str, str, Path, Path]]:
    """Return (product name, file name, GSD file, .meta file) of the GSD files stored in the project (AS/Hardware)."""
    folder = project
    for part in DESCRIPTION_FOLDER.parts:
        folder = child_folder(folder, part) if folder else None
    if folder is None:
        return []
    descriptions = []
    for meta in sorted(folder.glob("*.meta")):
        description = meta.with_suffix("")
        tags = {match.group("name"): match.group("value") for match in META_TAG_RE.finditer(read_text(meta))}
        if description.is_file():
            descriptions.append((tags.get("ProductName", ""), tags.get("FileName", ""), description, meta))
    return descriptions


def gsd_description_of(module_type: str, descriptions: list[tuple[str, str, Path, Path]]) -> tuple[Path, Path] | None:
    """Return GSD and .meta file of a PROFIBUS slave type such as
    ``FBD.GSDDTM.DTMDev.PCD3.M5xxx Onboard Slave.1.1`` (product name, optionally followed by the version)
    or ``FBP.0x0d44.96RM0D44.gsd`` (GSD file name)."""
    file_device = GSD_FILE_DEVICE_RE.match(module_type)
    for product, file_name, description, meta in descriptions:
        if file_device:
            if file_device.group("file").casefold() in (file_name.casefold(), description.name.casefold().split(".", 1)[-1]):
                return description, meta
        elif module_type.startswith(GSD_DEVICE_PREFIX) and product:
            device = module_type[len(GSD_DEVICE_PREFIX):]
            if device == product or device.startswith(product + "."):
                return description, meta
    return None


def copy_dtm_configuration(old_cpu_folder: Path, new_cpu_folder: Path, modules: list[str],
                           old_modules: dict[str, HardwareModule]) -> list[Path]:
    """Copy the DTM configuration (DTM/<module>) of the copied modules into the new CPU folder.

    Sub folders of modules that are not copied are left out.  Returns the copied folders.
    """
    old_dtm = child_folder(old_cpu_folder, DTM_FOLDER)
    if old_dtm is None:
        return []
    left_out = {name.casefold() for name in old_modules if name not in modules}
    copied: list[Path] = []
    for name in modules:
        source = child_folder(old_dtm, name)
        if source is None:
            continue
        new_dtm = new_cpu_folder / DTM_FOLDER
        destination = new_dtm / source.name
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if path.is_dir() or any(part.casefold() in left_out for part in relative.parts[:-1]):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.name.casefold() == PACKAGE_FILE.casefold():
                text = read_text(path)
                # Drop the entries of left-out sub folders from the copied package.
                text = re.sub(r"^[ \t]*<Object\b[^>]*>\s*(?P<child>[^<]+?)\s*</Object>[ \t]*\r?\n?",
                              lambda match: "" if match.group("child").casefold() in left_out else match.group(0),
                              text, flags=re.MULTILINE)
                target.write_text(text, encoding=read_text_with_encoding(path)[1], newline="")
            else:
                shutil.copy2(path, target)
        ensure_package_entry(old_dtm, new_dtm, source.name)
        ensure_package_entry(old_cpu_folder, new_cpu_folder, DTM_FOLDER, package_file="Cpu.pkg")
        copied.append(Path(DTM_FOLDER, source.name))
    return copied


def copy_io_configuration(old_root: Path, new_root: Path, chooser: FileChooser | None = None) -> IoCopyResult:
    """Copy the X20 I/O modules and their I/O mapping into the new project's active configuration."""
    old_project, new_project = project_root(old_root), project_root(new_root)
    old_configuration = active_configuration(old_project, chooser)
    new_configuration = active_configuration(new_project, chooser, ask=True)
    old_cpu_folder, new_cpu_folder = configuration_cpu(old_configuration), configuration_cpu(new_configuration)
    old_cpu, new_cpu = old_cpu_folder.name, new_cpu_folder.name

    old_modules = parse_hardware(read_text(old_configuration / "Hardware.hw"))
    new_hardware = new_configuration / "Hardware.hw"
    new_text, new_encoding = read_text_with_encoding(new_hardware)
    new_modules = parse_hardware(new_text)
    if old_cpu not in old_modules:
        raise ConversionError(tr("cpu_not_in_hardware", cpu=old_cpu, path=old_configuration / "Hardware.hw"))
    if new_cpu not in new_modules:
        raise ConversionError(tr("cpu_not_in_hardware", cpu=new_cpu, path=new_hardware))
    selected = select_io_modules(old_modules, old_cpu)
    if not selected:
        raise ConversionError(tr("no_io_modules", path=old_configuration / "Hardware.hw"))
    # PROFIBUS slaves at copied modules are taken over if their GSD file is stored in the old project.
    # A master interface module (X20IF...) plugged into the PLC itself is taken over with them.
    descriptions = gsd_descriptions(old_project)
    plc = plc_modules(old_modules, old_cpu)
    fieldbus: dict[str, tuple[Path, Path]] = {}
    masters: set[str] = set()
    for module in old_modules.values():
        found = gsd_description_of(module.type, descriptions)
        if found is None or module.name in selected or module.name in plc:
            continue
        neighbours = {target for _, target, _ in module.connections}
        neighbours |= {other.name for other in old_modules.values()
                       if any(target == module.name for _, target, _ in other.connections)}
        plc_masters = {name for name in neighbours & plc
                       if name != old_cpu and old_modules[name].type.upper().startswith("X20IF")}
        if neighbours & set(selected) or plc_masters:
            fieldbus[module.name] = found
            masters |= plc_masters
    selected = sorted(set(selected) | set(fieldbus) | masters)
    if new_cpu in selected:
        raise ConversionError(tr("io_name_is_cpu", name=new_cpu))

    warnings: list[str] = []
    # The modules are inserted as a block without connection to the PLC; the user
    # connects the block to the right interface of the new PLC.  Connections to
    # modules that are not copied are removed, so that no connection points nowhere.
    copied_blocks: dict[str, str] = {}
    for name in selected:
        module = old_modules[name]
        block = module.text
        for connector, target, target_connector in module.connections:
            if target not in old_modules or target in selected:
                continue
            if target in plc:
                warnings.append(tr("warn_io_connect_cpu", module=name, connector=connector, cpu=new_cpu,
                                   old_cpu=target, target_connector=target_connector))
            else:
                warnings.append(tr("warn_io_target_skipped", module=name, connector=connector, target=target))
            block = re.sub(rf'^[ \t]*<Connection\b[^>]*\bConnector="{re.escape(connector)}"[^>]*'
                           rf'\bTargetModule="{re.escape(target)}"[^>]*?(?:/>|>.*?</Connection>)[ \t]*\r?\n?',
                           "", block, flags=re.DOTALL | re.MULTILINE)
        copied_blocks[name] = block
    # Devices that are not X20 modules (e.g. a PROFIBUS slave at an X20 interface module) stay behind.
    for module in old_modules.values():
        if module.name in selected or module.name in plc:
            continue
        for target in sorted({target for _, target, _ in module.connections if target in selected}):
            warnings.append(tr("warn_io_device_not_copied", device=module.name, type=module.type, module=target))

    # Modules of the new project that are replaced by a copied module of the same
    # name although other modules of the new project are still connected to them.
    for module in new_modules.values():
        clashes = sorted({target for _, target, _ in module.connections if target in copied_blocks})
        if clashes and module.name not in copied_blocks:
            warnings.append(tr("warn_io_name_clash", module=module.name, target="', '".join(clashes)))

    replaced = sum(name in new_modules for name in copied_blocks)
    blocks = {name: module.text for name, module in new_modules.items() if name not in copied_blocks}
    newline = "\r\n" if "\r\n" in new_text else "\n"
    for name, block in copied_blocks.items():
        block = block.replace("\r\n", "\n").replace("\n", newline)
        blocks[name] = block if block.endswith(newline) else block + newline
    first = HW_MODULE_RE.search(new_text)
    closing = new_text.rfind("</Hardware>")
    if closing < 0:
        raise ConversionError(tr("invalid_hardware", path=new_hardware))
    head = new_text[:first.start()] if first else new_text[:closing]
    body = "".join(blocks[name] for name in sorted(blocks))
    new_hardware.write_text(head + body + new_text[closing:], encoding=new_encoding, newline="")

    copied_mappings, cpu_lines, skipped = merge_io_mapping(old_cpu_folder / "IoMap.iom", new_cpu_folder / "IoMap.iom",
                                                           set(selected), old_cpu, new_cpu)
    for line in cpu_lines:
        warnings.append(tr("warn_cpu_mapping", line=line, cpu=new_cpu))
    for line in skipped:
        warnings.append(tr("warn_mapping_skipped", line=line))

    copied_descriptions: list[Path] = []
    for name, (description, meta) in sorted(fieldbus.items()):
        folder = new_project / DESCRIPTION_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        for source in (description, meta):
            if folder / source.name not in copied_descriptions:
                shutil.copy2(source, folder / source.name)
                copied_descriptions.append(folder / source.name)
        warnings.append(tr("warn_io_fieldbus_check", device=name, file=description.name))
    dtm_folders = copy_dtm_configuration(old_cpu_folder, new_cpu_folder, selected, old_modules)
    return IoCopyResult(new_hardware, new_cpu_folder / "IoMap.iom", selected, replaced, copied_mappings, warnings,
                        sorted(fieldbus), copied_descriptions, dtm_folders)


def merge_io_mapping(old_map: Path, new_map: Path, modules: set[str], old_cpu: str,
                     new_cpu: str) -> tuple[int, list[str], list[str]]:
    """Copy the mapping lines of ``modules`` and of the old CPU into the new IoMap.iom.

    Mappings of the old CPU are renamed to the new CPU.  Existing lines for the
    copied modules are replaced and CPU lines already present are not added
    again, so running twice is harmless.  Returns the number of copied lines,
    the copied CPU lines and the old lines that were not copied.
    """
    if not old_map.exists():
        return 0, [], []
    if new_map.exists():
        text, encoding = read_text_with_encoding(new_map)
    else:
        text, encoding = "VAR_CONFIG\r\nEND_VAR\r\n", "utf-8"
    lines = [line for line in text.splitlines()
             if not ((match := IO_MAPPING_RE.search(line)) and match.group("module") in modules)]
    present = {line.strip() for line in lines}
    copied: list[str] = []
    cpu_lines: list[str] = []
    skipped: list[str] = []
    for line in read_text(old_map).splitlines():
        match = IO_MAPPING_RE.search(line)
        if not match:
            continue
        line = line.strip()
        if match.group("module") == old_cpu:
            line = line.replace(f'"{old_cpu}"', f'"{new_cpu}"', 1)
            cpu_lines.append(line)
            if line in present:
                continue
        elif match.group("module") not in modules:
            skipped.append(line)
            continue
        copied.append("\t" + line)
    newline = "\r\n" if "\r\n" in text else "\n"
    if not copied:
        return 0, cpu_lines, skipped
    end = next((index for index in range(len(lines) - 1, -1, -1) if END_VAR_RE.match(lines[index])), None)
    if end is None:
        lines += ["VAR_CONFIG", *copied, "END_VAR"]
    else:
        lines[end:end] = copied
    new_map.write_text(newline.join(lines) + newline, encoding=encoding, newline="")
    return len(copied), cpu_lines, skipped


ETHERNET_INTERFACE = "IF2"
# Parameters of the Ethernet interface and of the CPU (system host name, default
# gateway) that are taken over.  Without "Mode" the interface is a DHCP client
# (the Automation Studio default); "Manual" means a fixed IP address.
ETHERNET_INTERFACE_PARAMETERS = ("HostName", "Mode", "InternetAddress", "SubnetMask")
ETHERNET_CPU_PARAMETERS = ("EthernetHostName", "EthernetDefaultGateway")
# Interfaces whose settings are taken over completely.
ETHERNET_COPIED_INTERFACES = ("IF3",)
HW_PARAMETER_RE = re.compile(r'<Parameter\b[^>]*\bID="(?P<id>[^"]*)"[^>]*\bValue="(?P<value>[^"]*)"')


def hw_connector_re(name: str) -> re.Pattern[str]:
    return re.compile(rf'^[ \t]*<Connector\b[^>]*\bName="{re.escape(name)}"[^>]*?(?:/>|>.*?</Connector>)[ \t]*\r?\n?',
                      re.DOTALL | re.MULTILINE)


def hw_parameters(text: str) -> dict[str, str]:
    return {match.group("id"): match.group("value") for match in HW_PARAMETER_RE.finditer(text)}


def replace_hw_parameters(block: str, values: list[tuple[str, str]], ids: Iterable[str], fallback: int,
                          indent: str, newline: str) -> str:
    """Replace the parameter lines ``ids`` of ``block`` with ``values``.

    The new lines go where the first old one was, otherwise to ``fallback``.
    """
    pattern = re.compile(rf'^[ \t]*<Parameter\b[^>]*\bID="(?:{"|".join(map(re.escape, ids))})"[^>]*/>[ \t]*\r?\n',
                         re.MULTILINE)
    first = pattern.search(block)
    position = first.start() if first else fallback
    lines = "".join(f'{indent}<Parameter ID="{key}" Value="{value}" />{newline}' for key, value in values)
    return pattern.sub("", block[:position]) + lines + pattern.sub("", block[position:])


def line_end(text: str, index: int) -> int:
    """Return the index after the line break of the line containing ``index``."""
    end = text.find("\n", index)
    return len(text) if end < 0 else end + 1


HW_ANY_CONNECTOR_RE = re.compile(r'^[ \t]*<Connector\b[^>]*\bName="(?P<name>[^"]*)"[^>]*?(?:/>|>.*?</Connector>)',
                                 re.DOTALL | re.MULTILINE)


def panel_ethernet_interface(modules: dict[str, HardwareModule], cpu: str) -> tuple[str, re.Match[str]] | None:
    """Return "module.connector" and the connector of the Ethernet interface of a Power Panel.

    The CPU of a Power Panel has no Ethernet interface; it belongs to the panel or
    an interface card attached to the CPU.  An interface with a host name and
    without POWERLINK parameters (Epl...) is taken as Ethernet interface.
    """
    for name in sorted(plc_modules(modules, cpu) - {cpu}):
        for connector in HW_ANY_CONNECTOR_RE.finditer(modules[name].text):
            parameters = hw_parameters(connector.group(0))
            if "HostName" in parameters and not any(key.startswith("Epl") for key in parameters):
                return f"{name}.{connector.group('name')}", connector
    return None


def copy_ethernet_settings(old_root: Path, new_root: Path, chooser: FileChooser | None = None) -> EthernetCopyResult:
    """Take over host names, IP mode, IP address, subnet mask and default gateway of the old PLC.

    The settings of the old Ethernet interface IF2 and of the old CPU replace
    those of the new PLC in the active configuration; settings the old PLC does
    not have (e.g. the IP address of a DHCP client) are removed.  Interface IF3
    is taken over with all of its settings.  For a Power Panel, whose CPU has no
    IF2, the Ethernet interface of the panel replaces IF2.
    """
    old_configuration = active_configuration(project_root(old_root), chooser)
    new_configuration = active_configuration(project_root(new_root), chooser, ask=True)
    old_cpu, new_cpu = configuration_cpu(old_configuration).name, configuration_cpu(new_configuration).name
    old_modules = parse_hardware(read_text(old_configuration / "Hardware.hw"))
    new_hardware = new_configuration / "Hardware.hw"
    new_text, new_encoding = read_text_with_encoding(new_hardware)
    new_modules = parse_hardware(new_text)
    if old_cpu not in old_modules:
        raise ConversionError(tr("cpu_not_in_hardware", cpu=old_cpu, path=old_configuration / "Hardware.hw"))
    if new_cpu not in new_modules:
        raise ConversionError(tr("cpu_not_in_hardware", cpu=new_cpu, path=new_hardware))

    warnings: list[str] = []
    connector_re = hw_connector_re(ETHERNET_INTERFACE)
    old_module = old_modules[old_cpu].text
    old_connector = connector_re.search(old_module)
    if old_connector is None:
        panel = panel_ethernet_interface(old_modules, old_cpu)
        if panel is None:
            raise ConversionError(tr("no_ethernet_interface", interface=ETHERNET_INTERFACE, cpu=old_cpu))
        source, old_connector = panel
        warnings.append(tr("warn_ethernet_source", interface=ETHERNET_INTERFACE, source=source, cpu=new_cpu))
    old_interface = hw_parameters(old_connector.group(0))
    old_cpu_parameters = hw_parameters(re.sub(r"<Connector\b[^>]*?(?:/>|>.*?</Connector>)", "", old_module,
                                              flags=re.DOTALL))
    interface_values = [(key, old_interface[key]) for key in ETHERNET_INTERFACE_PARAMETERS if key in old_interface]
    cpu_values = [(key, old_cpu_parameters[key]) for key in ETHERNET_CPU_PARAMETERS if key in old_cpu_parameters]

    newline = "\r\n" if "\r\n" in new_text else "\n"
    module = new_modules[new_cpu].text
    indent = re.match(r"[ \t]*", module).group(0) + "  "
    connector = connector_re.search(module)
    if connector is None or connector.group(0).rstrip().endswith("/>"):
        # Missing or empty (<Connector Name="IF2" />): start with an empty connector.
        empty = f'{indent}<Connector Name="{ETHERNET_INTERFACE}">{newline}{indent}</Connector>{newline}'
        if connector is None:
            last = None
            for last in re.finditer(r"</Connector>", module):
                pass
            at = line_end(module, last.end() if last else 0)
            module = module[:at] + empty + module[at:]
        else:
            module = module[:connector.start()] + empty + module[connector.end():]
        connector = connector_re.search(module)
    block = connector.group(0)
    anchor = re.search(r'<Parameter\b[^>]*\bID="ActivateDevice"', block)
    block = replace_hw_parameters(block, interface_values, ETHERNET_INTERFACE_PARAMETERS,
                                  line_end(block, anchor.start() if anchor else 0), indent + "  ", newline)
    module = module[:connector.start()] + block + module[connector.end():]
    settings = [(f"{ETHERNET_INTERFACE}.{key}", value) for key, value in interface_values]
    mode = old_interface.get("Mode")
    if mode not in (None, "Manual"):
        warnings.append(tr("warn_ethernet_mode", mode=mode, interface=ETHERNET_INTERFACE))

    old_indent = re.match(r"[ \t]*", old_module).group(0) + "  "
    for name in ETHERNET_COPIED_INTERFACES:
        interface_re = hw_connector_re(name)
        source = interface_re.search(old_module)
        if source is None:
            warnings.append(tr("warn_no_interface", interface=name, cpu=old_cpu))
            continue
        # The complete connector is taken over, re-indented for the new file.
        lines = [indent + line[len(old_indent):] if line.startswith(old_indent) else line
                 for line in source.group(0).splitlines()]
        block = newline.join(lines) + newline
        settings.extend((f"{name}.{key}", value) for key, value in hw_parameters(block).items())
        target = interface_re.search(module)
        if target is None:
            # Insert after the last connector that precedes it (e.g. IF2), else after the start tag.
            previous = [match for match in re.finditer(r"^[ \t]*<Connector\b[^>]*\bName=\"(?P<name>[^\"]*)\"", module,
                                                       re.MULTILINE) if match.group("name") < name]
            at = (hw_connector_re(previous[-1].group("name")).search(module).end() if previous
                  else line_end(module, 0))
            module = module[:at] + block + module[at:]
        else:
            module = module[:target.start()] + block + module[target.end():]

    closing = module.rfind("\n", 0, module.rfind("</Module>")) + 1
    module = replace_hw_parameters(module, cpu_values, ETHERNET_CPU_PARAMETERS, closing, indent, newline)
    settings += cpu_values

    start = new_text.index(new_modules[new_cpu].text)
    new_hardware.write_text(new_text[:start] + module + new_text[start + len(new_modules[new_cpu].text):],
                            encoding=new_encoding, newline="")
    return EthernetCopyResult(new_hardware, new_cpu, settings, warnings)


OPCUA_FILE = "OpcUaBkV.uad"
# The first OpcUaSource FileVersion written by Automation Studio 6 (the only target version).
OPCUA_AS6_FILE_VERSION = 10
# Elements of the Bk types that are enabled in OPC UA.
OPCUA_TYPE_MEMBERS: dict[str, tuple[str, ...]] = {name.casefold(): tuple(members.split()) for name, members in {
    "BkAl": "Al Quit Ausl AlMk Gruppe Klartext Name Off Quit2",
    "BkAp": "Ap Wert WkUG WgOG Msg Kpos Gruppe Klartext Unit OG UG Off WertReal",
    "BkBa": "BAkop Kop BA BA0 BA1 BA2 BA3 BA4 BAmax BAaut Hand ZLT BAzlt Klartext Gruppe BAVorw "
            "BA0_Name BA1_Name BA2_Name BA3_Name BA4_Name BaNameAkt",
    "BkBp": "Bp Wert SyErr Off Gruppe Klartext BpNameAkt BP0_Name BP1_Name",
    "BkRk": "Wint X Wirk ToZo Pass Ypass Stfr W Y XkUG XgOG XgXwg SyErr WReal XReal YReal KposXW KposY Klartext "
            "UnitY Unit Gruppe Ymax Ymin Kp Ti Td OG UG Int Xw Xwg Y0 Hand Msg ZLT Yzlt Status Aktiv Wmax Wmin "
            "TuningStart OscillationTuning TuningError TuningDone TuningQuality TuningChillFact",
    "BkSb": "FrgS1 FrgS2 FrgS3 RmgS1 RmgS2 RmgS3 LfzSx RmgSb LfzSb Stoe S1 S2 S3 RmvSx RmvSb SyErr LfzSxInt "
            "LfzSbInt LfzSxVisu LfzSbVisu Klartext Gruppe Saut Hand Saus Rmg Smax ZLT Szlt SbNameAkt SbRmNameAkt "
            "SB0_Name SB1_Name SB2_Name SB3_Name SB0_RmName SB1_RmName SB2_RmName SB3_RmName",
    "BkSw": "SWkop Kop SW Kpos SWaut Hand ZLT SWzlt SWmin SWmax Gruppe Klartext Unit WertReal",
    "BkSbIm": "FrgS0 FrgS1 FrgS2 FrgS3 RmgS1 RmgS2 RmgS3 ImpD Stoe S0 S1 S2 S3 Klartext Gruppe Saut Hand Saus Rmg "
              "Smax ZLT S0Imp S1Imp S2Imp S3Imp Szlt SbImNameAkt SbImRmNameAkt SB0_Name SB1_Name SB2_Name SB3_Name "
              "SB0_RmName SB1_RmName SB2_RmName SB3_RmName",
    "BkSbKl": "Frg Rmg0 Rmg1 Lfz Stoe StoeA S1 Rmv Klartext Gruppe Saut Hand Saus Rmg ZLT Szlt SbKlNameAkt "
              "SbKlRmNameAkt SB0_Name SB1_Name SB0_RmName SB1_RmName",
    "BkSgSt": "Yr Yist Hyst Lfz Y YrMax YrMin Rmv LfzVisu Klartext Gruppe Hand Yaut ZLT Yzlt Reset rY rYist "
              "TimerLfz YReal",
    "BkSgQs": "Yr Yist Hyst Lfz Stoe StoeA Y YrMax YrMin Auf Zu Rmv Klartext Gruppe Hauf Hzu Hand Yaut ZLT Yzlt "
              "rY rYist YReal",
    "BkSgIn": "Yr EndA EndZ Stoe StoeA Y YrMax YrMin Auf Zu Rmv Yist Tmin Tvst Klartext Gruppe Hauf Hzu Hand Yaut "
              "ZLT Yzlt MinImpStart Yint YReal",
    "BkZl": "Imp Fakt Teil Reset Wert RstW WgOG SyErr Klartext Gruppe Unit Kpos Rst OG Msg Off Counter",
    "BkZs": "StrtW AktKo Kop Wert Aktiv Klartext Gruppe Uhrzeit",
    "BkStat": "Oertl SammH KplSt ALnLe AlUnq SammO SyErr Klartext Stat0 Stat1",
}.items()}
BK_DECLARATION_RE = re.compile(r"^\s*(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<type>Bk\w*)\s*(?::=|;)", re.IGNORECASE | re.MULTILINE)
UA_VARIABLE_TAG_RE = re.compile(r"<Variable\b[^>]*?(?P<empty>/)?>|</Variable>")
UA_GLOBAL_VARIABLES_RE = re.compile(r'^(?P<indent>[ \t]*)<GlobalVariables\b[^>]*?(?:/>|>(?P<body>.*?)</GlobalVariables>)',
                                    re.DOTALL | re.MULTILINE)


BK_LIBRARY_FILE = "bkfbklib.fun"
FUNCTION_BLOCK_RE = re.compile(r"\bFUNCTION_BLOCK\s+(?P<name>\w+)(?P<body>.*?)\bEND_FUNCTION_BLOCK\b", re.DOTALL)
FB_MEMBER_RE = re.compile(r"^\s*(?P<name>[A-Za-z_]\w*)\s*:", re.MULTILINE)


def bk_library_members(new_root: Path) -> dict[str, set[str]] | None:
    """Return the members of each function block in the Bk library of the new project (by casefolded name).

    Returns None if the project contains no bkfbklib.fun below Logical.
    """
    libraries = sorted((path for path in new_root.rglob("*") if path.is_file()
                        and path.name.casefold() == BK_LIBRARY_FILE
                        and any(part.casefold() == "logical" for part in path.relative_to(new_root).parts)),
                       key=lambda path: (len(path.parts), str(path).casefold()))
    if not libraries:
        return None
    return {block.group("name").casefold():
            {member.group("name").casefold() for member in FB_MEMBER_RE.finditer(block.group("body"))}
            for block in FUNCTION_BLOCK_RE.finditer(read_text(libraries[0]))}


def bk_variables(global_var_text: str) -> list[tuple[str, str]]:
    """Return the (name, type) of every variable of a Bk type, in declaration order."""
    return [(match.group("name"), match.group("type")) for match in BK_DECLARATION_RE.finditer(global_var_text)]


def ua_top_level_variables(body: str) -> list[tuple[int, int, str]]:
    """Return (start, end, name) of the outermost <Variable> elements in ``body``, including their lines."""
    elements: list[tuple[int, int, str]] = []
    depth = 0
    start = 0
    for tag in UA_VARIABLE_TAG_RE.finditer(body):
        if tag.group(0).startswith("</"):
            depth -= 1
            if depth == 0:
                elements.append((start, line_end(body, tag.end()), name))
            continue
        if depth == 0:
            start = body.rfind("\n", 0, tag.start()) + 1
            name = xml_attribute(tag.group(0), "Name")
            if tag.group("empty"):
                elements.append((start, line_end(body, tag.end()), name))
                continue
        if not tag.group("empty"):
            depth += 1
    return elements


def opcua_target(cpu: Path, chooser: FileChooser | None) -> Path:
    """Return the OpcUaBkV.uad of the PLC folder ``cpu``."""
    candidates = sorted((path for path in cpu.rglob("*") if path.is_file() and path.name.casefold() == OPCUA_FILE.casefold()),
                        key=lambda path: (len(path.parts), str(path).casefold()))
    if not candidates:
        raise ConversionError(tr("not_found", filename=OPCUA_FILE, root=cpu))
    target = candidates[0]
    if len(candidates) > 1:
        target = chooser(OPCUA_FILE, cpu, candidates) if chooser else None
        if target is None:
            raise ConversionError(tr("choice_cancelled", filename=OPCUA_FILE))
    return target


def enable_opcua_variables(new_root: Path, global_var: Path, chooser: FileChooser | None = None) -> OpcUaResult:
    """Enable every Bk variable of ``global_var`` with its elements in the OpcUaBkV.uad of the new PLC.

    Existing entries of these variables are replaced, all other entries are kept.
    The entries are written in the format of Automation Studio 6.
    """
    cpu = configuration_cpu(active_configuration(project_root(new_root), chooser, ask=True))
    target = opcua_target(cpu, chooser)

    warnings: list[str] = []
    variables: list[tuple[str, tuple[str, ...]]] = []
    # Elements missing in the project's library version would make the .uad invalid; leave them out.
    library = bk_library_members(new_root)
    for name, var_type in bk_variables(read_text(global_var)):
        members = OPCUA_TYPE_MEMBERS.get(var_type.casefold())
        if members is None:
            warnings.append(tr("warn_opcua_unknown_type", name=name, type=var_type))
            continue
        if library is not None and var_type.casefold() in library:
            members = tuple(member for member in members if member.casefold() in library[var_type.casefold()])
        variables.append((name, members))

    text, encoding = read_text_with_encoding(target)
    newline = "\r\n" if "\r\n" in text else "\n"
    version = re.search(r'<OpcUaSource\b[^>]*\bFileVersion="(\d+)"', text)
    if version and int(version.group(1)) < OPCUA_AS6_FILE_VERSION:
        warnings.append(tr("warn_opcua_version", path=target, version=version.group(1)))
    section = UA_GLOBAL_VARIABLES_RE.search(text)
    if section is None:
        module = re.search(r'^([ \t]*)<Module\b[^>]*>[ \t]*\r?\n', text, re.MULTILINE)
        if module is None:
            raise ConversionError(tr("invalid_opcua", path=target))
        indent = module.group(1) + "  "
        text = (text[:module.end()] + f'{indent}<GlobalVariables Name="Globale Variablen">{newline}'
                f"{indent}</GlobalVariables>{newline}" + text[module.end():])
        section = UA_GLOBAL_VARIABLES_RE.search(text)
    elif section.group("body") is None:
        # <GlobalVariables ... />: open it up.
        indent = section.group("indent")
        opening = section.group(0).strip()[:-2].rstrip() + ">"
        text = (text[:section.start()] + f"{indent}{opening}{newline}{indent}</GlobalVariables>"
                + text[section.end():])
        section = UA_GLOBAL_VARIABLES_RE.search(text)

    indent = section.group("indent") + "  "
    body_start = section.start("body")
    body = section.group("body")
    wanted = {name.casefold() for name, _ in variables}
    kept = [body[start:end] for start, end, name in ua_top_level_variables(body) if name.casefold() not in wanted]
    replaced = len(ua_top_level_variables(body)) - len(kept)
    head = body[:line_end(body, 0)] if "\n" in body else newline
    closing_indent = body[body.rfind("\n") + 1:]
    entries = "".join(
        f'{indent}<Variable Name="{name}">{newline}'
        + "".join(f'{indent}  <Variable Name="{member}" />{newline}' for member in members)
        + f"{indent}</Variable>{newline}"
        for name, members in variables)
    new_body = head + "".join(kept) + entries + closing_indent
    text = text[:body_start] + new_body + text[body_start + len(body):]
    target.write_text(text, encoding=encoding, newline="")
    return OpcUaResult(target, [name for name, _ in variables], replaced, warnings)


# --- VC4 user visualisation (uvis) -> mappView -------------------------------------------------

# Files of the uvis program: the module uvis.br or its sources (uvis.h, uvis.var, uvisCyclic.c, ...).
# The UVis_ pages and bitmaps are part of every Bk2000 visu and do not count.
USER_VISU_FILE_RE = re.compile(r"^uvis(?:Cyclic|Init|Exit)?\.(?:br|h|c|st|var|typ)$", re.IGNORECASE)
VC4_NS = "{http://br-automation.co.at/AS/VC/Project}"
VC4_SHAPE, VC4_BUTTON, VC4_TEXT, VC4_BITMAP, VC4_LINE = (
    "0x00001001", "0x00001002", "0x00001004", "0x00001005", "0x0000100d")
# Pages of the Bk2 standard visualisation, the UVis templates and the SDM page (SDM or e.g. S78_SDM,
# a viewer of the System Diagnostics Manager, which BkNG has itself) are not converted.
VC4_IGNORED_PAGE_RE = re.compile(r"^(?:S00_Bk2_STD|UVis_.*|S\d+_Bk2_.*|(?:S\d+_)?SDM)$", re.IGNORECASE)
VC4_BK_BITMAP_RE = re.compile(r"^Bk(?P<type>SbIm|SbKl|SgSt|SgQs|SgIn|Al|Ap|Ba|Bp|Rk|Sb|Sw|Zl|Zs)(?P<idx>\d+)"
                              r"(?P<variant>\w*?)_Bmp$", re.IGNORECASE)
# Text fields showing the name of a Bk datapoint, e.g. BkBp02LED_Name or BkSgSt09Anzg_Name.
VC4_BK_NAME_RE = re.compile(r"^Bk(?P<type>SbIm|SbKl|SgSt|SgQs|SgIn|Al|Ap|Ba|Bp|Rk|Sb|Sw|Zl|Zs)(?P<idx>\d+)"
                            r"(?P<variant>\w*?)_Name$", re.IGNORECASE)
# Labels with the datapoint names: uniform size and font; shown while the session variable is true,
# which the "ktx" toggle button on every page switches (default: hidden).
NAME_LABEL_SIZE, NAME_LABEL_FONT_SIZE = (140, 10), "11px"
NAMES_SESSION_VARIABLE = "ShowUVisNames"
NAMES_TOGGLE_TEXT, NAMES_TOGGLE_SIZE, NAMES_TOGGLE_MARGIN = "ktx", (50, 30), 5
# Page title of the UVis header (Vis.Title), defined in dplist.dat: "#UVIS page=3 ... title='...'".
UVIS_TITLE_RE = re.compile(r"^\s*\"?\s*#UVIS\b(?=.*\bpage\s*=\s*(?P<page>\d+))(?=.*\btitle\s*=\s*'(?P<title>[^']*)')",
                           re.IGNORECASE)
# The title is shown at the top of every page; the page content is scaled into the area below it.
TITLE_HEIGHT, TITLE_FONT_SIZE = 30, "20px"
# Grid in px on which all widgets of the converted pages are placed.
GRID = 5
VC4_UVIS_RE = re.compile(r"^#UVIS\b(?=.*\bpage\s*=\s*(?P<page>\d+))(?=.*\bidx\s*=\s*(?P<idx>\d+))", re.IGNORECASE)
# Texts that are left out (the W/X/Y letters next to a controller).
VC4_IGNORED_TEXTS = {"W", "X", "Y"}
# Default VC4 page (XGA 1024x768) and header (0..42) if the visu does not define them.
# The header is replaced by the BkNG header; the area below it fills the mappView content.
VC4_PAGE_WIDTH, VC4_PAGE_HEIGHT, VC4_HEADER_HEIGHT = 1024, 768, 42
# Shape of the UVis header layer whose bottom edge ends the header.
VC4_HEADER_SHAPE = "Shape_Header"
# Defaults of the VC4 style classes Line, Shape and Text when a control has no own color.
VC4_DEFAULT_FORE_COLOR, VC4_DEFAULT_SHAPE_BACK_COLOR = 0, 7
MAPPVIEW_BKVISU_PAGES = ("mappView", "Visualization", "Pages", "BkVisu")
MAPPVIEW_BKVISU_LIBRARY = ("mappView", "Widgets", "BkVisuLib")
# Physical package (next to the .vis file) holding one bindings set per converted page.
MAPPVIEW_BINDINGS_PACKAGE = "BkVisu"
MAPPVIEW_CONTENT_SIZE = (1280, 650)
PAGE_PACKAGE_TEMPLATE = (
    '﻿<?xml version="1.0" encoding="utf-8"?>\r\n'
    '<?AutomationStudio FileVersion="4.9"?>\r\n'
    '<Package SubType="PagePackage" PackageType="PagePackage" xmlns="http://br-automation.co.at/AS/Package">\r\n'
    "  <Objects>\r\n"
    '    <Object Type="File" Description="Page description">{page}.page</Object>\r\n'
    "    <Object Type=\"File\">{content}.content</Object>\r\n"
    "  </Objects>\r\n"
    "</Package>")
DEFAULT_PAGE_TEMPLATE = (
    '﻿<?xml version="1.0" encoding="utf-8"?>\r\n'
    '<pdef:Page xmlns:pdef="http://www.br-automation.com/iat2015/pageDefinition/v2" id="{page}" '
    'layoutRefId="BkNG_Layout">\r\n'
    "  <Assignments>\r\n"
    '    <Assignment type="Content" baseContentRefId="HeaderContent" areaRefId="AreaHeader" />\r\n'
    '    <Assignment type="Content" baseContentRefId="{content}" areaRefId="AreaContent" />\r\n'
    '    <Assignment type="Content" baseContentRefId="BkV_NavigationContent" areaRefId="AreaNavigation" />\r\n'
    "  </Assignments>\r\n"
    "</pdef:Page>")


@dataclass
class VisuConversionResult:
    visu: Path | None
    pages: list[str]
    widgets: int
    bindings: int
    binding_dir: Path | None
    warnings: list[str]
    navigation: Path | None = None
    # Navigation button text of each converted page.
    navigation_buttons: dict[str, str] = field(default_factory=dict)
    # VC4 page (width, height, header height) and mappView content (width, height).
    source_size: tuple[int, int, int] | None = None
    target_size: tuple[int, int] | None = None
    # Widgets bound to PLC variables, expressions for their visibility, variables enabled in OPC UA.
    variable_bindings: int = 0
    expressions: int = 0
    opcua_enabled: list[str] = field(default_factory=list)
    # Tasks taken over for the bindings: (task, deployed in the Cpu.sw).
    tasks: list[tuple[str, bool]] = field(default_factory=list)


def xml_quote(value: object) -> str:
    return (str(value).replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;"))


def vc4_properties(element: ET.Element) -> dict[str, str]:
    return {prop.get("Name"): prop.get("Value") for prop in element.findall(VC4_NS + "Property")}


def vc4_reference(value: str | None) -> str | None:
    """Return the name in a VC4 reference like ``Source[local].Page[S02_Name]``."""
    match = re.search(r"\.\w+\[([^\]]*)\]$", value or "")
    return match.group(1) if match else None


def find_user_visu(old_root: Path, chooser: FileChooser | None) -> Path | None:
    """Return the folder of the VC4 visualisation of the user visu, or None without uvis program."""
    logical = logical_dir(old_root.resolve())
    if not any(USER_VISU_FILE_RE.match(path.name) for path in logical.rglob("*") if path.is_file()):
        return None
    # The user visu is the VC4 visualisation containing the UVis_... template pages.
    visus = sorted((path.parent for path in logical.rglob("VCObject.vc")
                    if any(page.stem.casefold().startswith("uvis_") for page in (path.parent / "Pages").glob("*.page"))),
                   key=lambda path: str(path).casefold())
    if not visus:
        raise ConversionError(tr("no_user_visu", root=logical))
    if len(visus) == 1:
        return visus[0]
    selected = chooser("VCObject.vc", logical, [visu / "VCObject.vc" for visu in visus]) if chooser else None
    if selected is None:
        raise ConversionError(tr("choice_cancelled", filename="VCObject.vc"))
    return selected.parent


def vc4_page_geometry(visu: Path) -> tuple[int, int, int]:
    """Return width, height and header height of the VC4 pages, e.g. (640, 480, 42) for a VGA visu."""
    width, height = VC4_PAGE_WIDTH, VC4_PAGE_HEIGHT
    vc_object = visu / "VCObject.vc"
    if vc_object.exists():
        text = read_text(vc_object)
        found = {key: re.search(rf'<Property\b[^>]*\bName="{key}"[^>]*\bValue="(\d+)"', text) for key in ("Width", "Height")}
        if found["Width"] and found["Height"]:
            width, height = int(found["Width"].group(1)), int(found["Height"].group(1))
    header = VC4_HEADER_HEIGHT
    for layer in sorted((visu / "Layers").glob("*.layer")):
        shape = re.search(rf'<Control\b[^>]*\bName="{VC4_HEADER_SHAPE}".*?</Control>', read_text(layer), re.DOTALL)
        if shape:
            properties = dict(re.findall(r'<Property\b[^>]*\bName="(\w+)"[^>]*\bValue="([^"]*)"', shape.group(0)))
            try:
                header = round(float(properties.get("Top", 0)) + float(properties["Height"]))
            except (KeyError, ValueError):
                pass
            break
    return width, height, header


def vc4_palette(visu: Path) -> dict[int, str]:
    palette = visu / "Palette.vcr"
    if not palette.exists():
        return {}
    colors = {}
    for entry in ET.parse(palette).getroot().iter(VC4_NS + "PaletteColor"):
        value = entry.get("Value", "")
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
            red, green, blue = (int(value[i:i + 2], 16) for i in (1, 3, 5))
            colors[int(entry.get("Index"))] = f"rgba({red}, {green}, {blue}, 1)"
    return colors


def vc4_fonts(visu: Path) -> dict[str, tuple[float, bool]]:
    """Return {font name: (size in pt, bold)} of the German (or first) font mapping."""
    fonts = {}
    for path in (visu / "Fonts").glob("*.fninfo"):
        root = ET.parse(path).getroot()
        mappings = root.findall(VC4_NS + "FontMapping")
        mapping = next((m for m in mappings if m.get("LanguageId") == "de"), mappings[0] if mappings else None)
        if mapping is not None:
            properties = vc4_properties(mapping)
            try:
                fonts[root.get("Name", path.stem)] = (float(properties.get("Size", "0")), properties.get("Bold") == "True")
            except ValueError:
                pass
    return fonts


def vc4_virtual_keys(root: ET.Element) -> dict[str, str]:
    """Return {virtual key: target page} of the keys in ``root`` that change the page."""
    keys = {}
    for key in root.iter(VC4_NS + "VirtualKey"):
        for action in key.iter(VC4_NS + "KeyAction"):
            page = vc4_reference(vc4_properties(action).get("Page"))
            if page and key.get("Name"):
                keys[key.get("Name")] = page
                break
    return keys


def vc4_texts(layer: ET.Element) -> dict[int, str]:
    """Return {text index: text} of the text group embedded in a layer, German preferred."""
    group = layer.find(VC4_NS + "TextGroup")
    if group is None:
        return {}
    indexes = {entry.get("ID"): int(entry.get("Value")) for entry in group.iter(VC4_NS + "Index")}
    texts: dict[int, str] = {}
    layers = group.findall(VC4_NS + "TextLayer")
    layers.sort(key=lambda text_layer: text_layer.get("LanguageId") != "de")
    for text_layer in layers:
        for text in text_layer.findall(VC4_NS + "Text"):
            index = indexes.get(text.get("ID"))
            if index is not None and text.get("Value") and index not in texts:
                texts[index] = text.get("Value")
    return texts


def vc4_bitmap_names(visu: Path, group: str, cache: dict[str, dict[int, str]]) -> dict[int, str]:
    """Return {bitmap index: bitmap name} of a VC4 bitmap group."""
    if group not in cache:
        path = visu / "BitmapGroups" / f"{group}.bmgrp"
        names: dict[int, str] = {}
        if path.exists():
            root = ET.parse(path).getroot()
            indexes = {entry.get("ID"): int(entry.get("Value")) for entry in root.iter(VC4_NS + "Index")}
            for ref in root.iter(VC4_NS + "BitmapRef"):
                name = vc4_reference(ref.get("Value"))
                if ref.get("ID") in indexes and name:
                    names[indexes[ref.get("ID")]] = name
        cache[group] = names
    return cache[group]


def uvis_datapoints(dplist_text: str, types: dict[str, str]) -> dict[tuple[int, int, str], str]:
    """Return {(page, idx, type): datapoint} from the ``#UVIS page=.. idx=..`` lines of the dplist."""
    datapoints: dict[tuple[int, int, str], str] = {}
    active: str | None = None
    for raw_line in dplist_text.splitlines():
        line = strip_legacy_wrapping(raw_line)
        if not line or line.startswith(";"):
            continue
        match = DATAPOINT_RE.match(line)
        if match:
            active = match.group("name")
            continue
        uvis = VC4_UVIS_RE.match(line)
        if uvis and active and active.casefold() in types:
            key = (int(uvis.group("page")), int(uvis.group("idx")), types[active.casefold()].casefold())
            datapoints.setdefault(key, active)
    return datapoints


def uvis_titles(dplist_text: str) -> dict[int, str]:
    """Return the page title of each UVis page index from dplist.dat."""
    titles: dict[int, str] = {}
    for line in dplist_text.splitlines():
        match = UVIS_TITLE_RE.match(line)
        if match and match.group("title").strip():
            titles[int(match.group("page"))] = match.group("title").strip()
    return titles


def bk_widget_name(var_type: str, variant: str, bitmap: str) -> str:
    """Return the BkVisuLib widget for a VC4 Bk bitmap, e.g. (Sb, Ve, UVis_BkSb_Vent_R_00) -> Bk_Sb_Vent_r.

    The orientation of pumps, fans, valves and dampers is taken from the name of
    the bitmap that the VC4 control shows (BitmapIndex).
    """
    var_type, variant = var_type.casefold(), variant.casefold()
    bitmap = bitmap.casefold()
    direction = next((side for side in "lrou" if f"_{side}_" in bitmap + "_"), "l")
    vertical = "senk" in bitmap or "_ver" in bitmap
    if var_type == "al":
        if variant.startswith("filter"):
            return "Bk_Al_Filter_l" if "_li" in bitmap else "Bk_Al_Filter_r"
        return f"Bk_Al_{variant}"
    if var_type == "bp":
        return {"eis": "Bk_Bp_Eis", "rep": "Bk_Bp_RepSch", "led": "Bk_Bp_LED", "aus": "Bk_Bp_LED"}.get(
            variant, f"Bk_Bp_{variant}")
    if var_type in ("sb", "sbim"):
        prefix = "Bk_Sb_Im_" if var_type == "sbim" else "Bk_Sb_"
        if variant == "m":
            return prefix + "Motor"
        if variant == "pu":
            return f"{prefix}Pumpe_{direction}"
        if variant == "ve":
            return f"{prefix}Vent_{direction}"
        if variant.startswith("zwv"):
            return prefix + ("2WVentil_v" if vertical else "2WVentil_h")
        if variant.startswith("anz"):
            return prefix + "BetrArt"
        return prefix + variant
    if var_type == "sbkl":
        return "Bk_Sb_Kl_v" if vertical else "Bk_Sb_Kl_h"
    if var_type.startswith("sg"):
        prefix = f"Bk_Sg_{var_type[2:].capitalize()}_"
        if variant.startswith("anz"):
            return prefix + "Anz"
        if variant.startswith("kl"):
            return prefix + ("Klappe_v" if vertical else "Klappe_h")
        return prefix + variant
    return f"Bk_{var_type.capitalize()}"


def bkvisu_library(new_logical: Path) -> dict[str, tuple[str, int, int]]:
    """Return {widget name casefolded: (widget id, width, height)} of the BkVisuLib in the new project."""
    library = {}
    for path in new_logical.joinpath(*MAPPVIEW_BKVISU_LIBRARY).glob("*/Widget.compoundwidget"):
        match = re.search(r"<CompoundWidget\b[^>]*", read_text(path))
        if match:
            widget = xml_attribute(match.group(0), "id") or path.parent.name
            size = [int(xml_attribute(match.group(0), key) or 0) for key in ("width", "height")]
            library[path.parent.name.casefold()] = library[widget.casefold()] = (widget, *size)
    return library


def snap_to_grid(attributes: dict[str, object], grid: int = GRID) -> None:
    """Place a widget on the grid: positions and line ends are rounded to it, and so are the right and
    bottom edges of a widget with a size (a size that would vanish, e.g. of a 1 px line, is kept)."""
    def snap(value: int) -> int:
        return round(value / grid) * grid

    for position, size in (("left", "width"), ("top", "height")):
        if isinstance(attributes.get(position), int):
            start = attributes[position]
            attributes[position] = snap(start)
            if isinstance(attributes.get(size), int):
                end = snap(start + attributes[size])
                if end > attributes[position]:
                    attributes[size] = end - attributes[position]
    for key in ("x1", "x2", "y1", "y2"):
        if isinstance(attributes.get(key), int):
            attributes[key] = snap(attributes[key])


class _ContentBuilder:
    """Collects the widgets and bindings of one mappView content."""

    def __init__(self, content: str, size: tuple[int, int],
                 source: tuple[int, int, int] = (VC4_PAGE_WIDTH, VC4_PAGE_HEIGHT, VC4_HEADER_HEIGHT),
                 top: int = 0) -> None:
        self.content = content
        # The VC4 page below its header is stretched over the mappView content below ``top``
        # (the height of the page title, if any).
        width, height, self.header = source
        self.top = top
        self.scale_x = size[0] / width
        self.scale_y = (size[1] - top) / (height - self.header)
        # Fonts grow with a smaller visu (e.g. VGA); an XGA visu keeps its font sizes.
        self.font_scale = max(1.0, min(self.scale_x, self.scale_y))
        self.widgets: list[str] = []
        self.bindings: list[tuple[str, str]] = []
        # (widget, attribute) bound to the session variable of the datapoint names.
        self.session_bindings: list[tuple[str, str]] = []
        self.counters: dict[str, int] = {}
        # Dynamics: bindings of PLC variables and expressions (complete <Binding> elements),
        # expression instances (id, type), the definitions of their types, the bound variables
        # (scope, top-level name) and the datapoints that could not be bound (variable, reason).
        self.value_bindings: list[str] = []
        self.expressions: list[tuple[str, str]] = []
        self.expression_types: dict[str, str] = {}
        self.variables: set[tuple[str, str]] = set()
        self.unbound: list[tuple[str, str]] = []
        self.dynamic_widgets: set[str] = set()

    def bind(self, widget: str, ref: str, attribute: str = "value", mode: str = "oneWay") -> None:
        """Bind the attribute of a widget to the OPC UA variable ``ref`` (::scope:name)."""
        self.dynamic_widgets.add(widget)
        self.value_bindings.append(
            f'\t\t<Binding mode="{mode}">\r\n'
            f'\t\t\t<Source xsi:type="opcUa" refId="{ref}" attribute="value" />\r\n'
            f'\t\t\t<Target xsi:type="brease" contentRefId="{self.content}" widgetRefId="{widget}" '
            f'attribute="{attribute}" />\r\n\t\t</Binding>\r\n')

    def show_if(self, widget: str, conditions: list[tuple[str, str, str, int | None]]) -> None:
        """Bind the visibility of a widget to an expression of (role, ref, kind, value) conditions."""
        if not conditions:
            return
        self.dynamic_widgets.add(widget)
        type_name, definition = visibility_expression_type([(role, kind, value) for role, _, kind, value in conditions])
        self.expression_types[type_name] = definition
        expression = f"{self.content}_{widget}"
        self.expressions.append((expression, type_name))
        for number, (_, ref, _, _) in enumerate(conditions):
            self.value_bindings.append(
                '\t\t<Binding mode="oneWay">\r\n'
                f'\t\t\t<Source xsi:type="opcUa" refId="{ref}" attribute="value" />\r\n'
                f'\t\t\t<Target xsi:type="expression" refId="{expression}" attribute="op{number}" />\r\n'
                "\t\t</Binding>\r\n")
        self.value_bindings.append(
            '\t\t<Binding mode="oneWay">\r\n'
            f'\t\t\t<Source xsi:type="expression" refId="{expression}" attribute="result" />\r\n'
            f'\t\t\t<Target xsi:type="brease" contentRefId="{self.content}" widgetRefId="{widget}" '
            'attribute="visible" />\r\n\t\t</Binding>\r\n')

    def x(self, value: float) -> int:
        return max(0, round(value * self.scale_x))

    def y(self, value: float) -> int:
        return max(self.top, self.top + round((value - self.header) * self.scale_y))

    def add(self, widget_type: str, name: str, **attributes: object) -> str:
        snap_to_grid(attributes)
        self.counters[name] = self.counters.get(name, 0) + 1
        widget_id = f"{name}{self.counters[name]}"
        values = "".join(f' {key}="{xml_quote(value)}"' for key, value in attributes.items() if value is not None)
        self.widgets.append(f'<Widget xsi:type="{widget_type}" id="{widget_id}"{values} zIndex="{len(self.widgets)}" />')
        return widget_id


def vc4_page_index(page: ET.Element) -> int:
    """Return the index of a VC4 page, which is its #UVIS page number."""
    return int(vc4_properties(page).get("Index", "0") or 0)


def vc4_shows_datapoints(page: ET.Element) -> bool:
    """True if the page shows Bk datapoints of the UVis runtime (Bk bitmaps or name fields)."""
    return any(VC4_BK_BITMAP_RE.match(name) or VC4_BK_NAME_RE.match(name)
               for name in (control.get("Name", "") for control in page.iter(VC4_NS + "Control")))


# --- VC4 controls bound to PLC variables (dynamics) -> mappView widgets, bindings and expressions --

VC4_NUMERIC = "0x00001007"
# Number format of numeric widgets bound to integer variables (no decimal places).
INTEGER_FORMAT = ("{'metric': {'decimalPlaces': 0, 'minimumIntegerDigits': 1}, "
                  "'imperial': {'decimalPlaces': 0, 'minimumIntegerDigits': 1}, "
                  "'imperial-us': {'decimalPlaces': 0, 'minimumIntegerDigits': 1}}")
VC4_DATAPOINT_RE = re.compile(r"^Source\[global\]\.Variable\[(?P<path>.+)\]$")
UVIS_VARIABLE_RE = re.compile(r"uvis|uservis", re.IGNORECASE)
# Virtual key of the UVis runtime that changes to the page with this index.
UVIS_PAGE_KEY_RE = re.compile(r"^vk_?UVisPage(?P<index>\d+)$", re.IGNORECASE)
MAPPVIEW_EXPRESSIONS = ("mappView", "Visualization", "Expressions")
EXPRESSION_TYPES_FILE = "BkVisu.expressiontype"
MAPPVIEW_MEDIA = ("mappView", "Resources", "Media")
# Bitmaps of dynamic VC4 bitmaps are copied into this media package.
UVIS_MEDIA_PACKAGE = "BkVisu_UVis"
VC4_BITMAP_SUFFIXES = (".png", ".bmp", ".jpg", ".gif", ".svg")
IEC_INT_TYPES = {"SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT", "BYTE", "WORD", "DWORD"}
C_TYPES = {"plcbit": "BOOL", "plcstring": "STRING", "plcwstring": "WSTRING", "signed char": "SINT",
           "unsigned char": "USINT", "char": "SINT", "short": "INT", "unsigned short": "UINT", "int": "DINT",
           "unsigned int": "UDINT", "long": "DINT", "unsigned long": "UDINT", "float": "REAL", "double": "LREAL"}
C_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\r\n]*", re.DOTALL)
C_DECLARATION_RE = re.compile(r"^\s*_(?P<scope>LOCAL|GLOBAL)(?:_RETAIN)?\s+(?:struct\s+)?"
                              r"(?P<type>(?:unsigned\s+|signed\s+)?\w+)\s+(?P<names>[^;]+);", re.MULTILINE)
C_STRUCT_RE = re.compile(r"typedef\s+struct\s*\w*\s*\{(?P<body>.*?)\}\s*(?P<name>\w+)\s*;", re.DOTALL)
C_MEMBER_RE = re.compile(r"(?:struct\s+)?(?P<type>(?:unsigned\s+|signed\s+)?\w+)\s+(?P<names>[^;{}()]+);")
IEC_STRUCT_RE = re.compile(r"(?P<name>[A-Za-z_]\w*)\s*:\s*STRUCT\b(?P<body>.*?)\bEND_STRUCT", re.DOTALL | re.IGNORECASE)
IEC_MEMBER_RE = re.compile(r"(?P<names>[A-Za-z_][\w\s,]*?)\s*:\s*(?P<type>[^;]*?)\s*(?::=[^;]*)?;", re.DOTALL)
IEC_ARRAY_RE = re.compile(r"^ARRAY\s*\[[^\]]*\]\s*OF\s+(?P<type>.+)$", re.IGNORECASE | re.DOTALL)


@dataclass
class PlcDeclarations:
    """Variables and structure types of a project, used to bind VC4 datapoints in mappView.

    Types are IEC type names; a type starting with "*" is a pointer (declared in C), which OPC UA
    cannot follow.
    """
    globals: dict[str, str] = field(default_factory=dict)
    # Casefolded task name: (task name, {casefolded variable: type}).
    tasks: dict[str, tuple[str, dict[str, str]]] = field(default_factory=dict)
    structs: dict[str, dict[str, str]] = field(default_factory=dict)


def c_declarations(type_name: str, declarators: str) -> list[tuple[str, str]]:
    """Return (name, IEC type) of the C declarators (e.g. "*pServer, Counter[4]") of ``type_name``."""
    base = C_TYPES.get(" ".join(type_name.split()), type_name)
    result = []
    for declarator in declarators.split(","):
        name = re.sub(r"\[.*", "", declarator.split("=")[0]).replace("*", "").strip()
        if not IEC_IDENTIFIER_RE.fullmatch(name):
            continue
        var_type = "STRING" if "[" in declarator and base in ("SINT", "USINT", "STRING") else base
        result.append((name, "*" + var_type if "*" in declarator.split("=")[0] else var_type))
    return result


def plc_declarations(logical: Path) -> PlcDeclarations:
    """Collect the global and task variables (IEC and C) and the structure types of a Logical view."""
    declarations = PlcDeclarations()
    programs = sorted(logical_programs(logical).values(), key=lambda folder: -len(folder.parts))
    for path in sorted(logical.rglob("*")):
        suffix = path.suffix.casefold()
        if suffix not in (".var", ".c", ".typ", ".h") or not path.is_file():
            continue
        owner = next((folder for folder in programs if folder == path.parent or folder in path.parents), None)
        task = declarations.tasks.setdefault(owner.name.casefold(), (owner.name, {}))[1] if owner else None
        text = read_text(path)
        if suffix == ".var":
            target = task if task is not None else declarations.globals
            for _, names, source in iec_entries(text, "var"):
                match = re.search(r":\s*(?P<type>[^;]*?)\s*(?::=[^;]*)?;", blank_iec_comments(source), re.DOTALL)
                for name in names:
                    if match:
                        target.setdefault(name.casefold(), " ".join(match.group("type").split()))
        elif suffix == ".typ":
            for struct in IEC_STRUCT_RE.finditer(blank_iec_comments(text)):
                members = declarations.structs.setdefault(struct.group("name").casefold(), {})
                for member in IEC_MEMBER_RE.finditer(struct.group("body")):
                    for name in member.group("names").split(","):
                        members.setdefault(name.strip().casefold(), " ".join(member.group("type").split()))
        else:
            code = C_COMMENT_RE.sub(" ", text)
            for struct in C_STRUCT_RE.finditer(code):
                members = declarations.structs.setdefault(struct.group("name").casefold(), {})
                for member in C_MEMBER_RE.finditer(struct.group("body")):
                    for name, var_type in c_declarations(member.group("type"), member.group("names")):
                        members.setdefault(name.casefold(), var_type)
            for declaration in C_DECLARATION_RE.finditer(code):
                target = declarations.globals if declaration.group("scope") == "GLOBAL" or task is None else task
                for name, var_type in c_declarations(declaration.group("type"), declaration.group("names")):
                    target.setdefault(name.casefold(), var_type)
    return declarations


def element_type(var_type: str | None) -> str | None:
    """Return the element type of an IEC array type, or the type itself."""
    match = IEC_ARRAY_RE.match(var_type or "")
    return " ".join(match.group("type").split()) if match else var_type


def resolve_vc4_variable(declarations: PlcDeclarations, path: str) -> tuple[str, str, str | None] | None:
    """Return (OPC UA scope, variable path, type) of a VC4 datapoint path, or None if it is not declared.

    VC4 names a global variable "Local.<name>" and a task variable "Local.<packages>.<task>.<name>".
    The type is None if unknown; it starts with "*" if the path leads through a pointer.
    """
    parts = path.split(".")
    if parts and parts[0] == "Local":
        parts = parts[1:]
    if not parts:
        return None

    def root(part: str) -> str:
        return re.sub(r"\[.*", "", part).strip().casefold()

    if root(parts[0]) in declarations.globals:
        scope, variables, rest = "AsGlobalPV", declarations.globals, parts
    else:
        for index, part in enumerate(parts[:-1]):
            if part.casefold() in declarations.tasks:
                scope, variables = declarations.tasks[part.casefold()]
                rest = parts[index + 1:]
                break
        else:
            return None
    var_type = variables.get(root(rest[0]))
    for previous, member in zip(rest, rest[1:]):
        if var_type is None or var_type.startswith("*"):
            break
        if "[" in previous:
            var_type = element_type(var_type)
        var_type = declarations.structs.get(var_type.casefold(), {}).get(root(member))
    if var_type and not var_type.startswith("*") and "[" in rest[-1]:
        var_type = element_type(var_type)
    return scope, ".".join(rest), var_type


def value_kind(var_type: str | None) -> str | None:
    """Return "bool", "int", "real" or "string" for an elementary IEC type, else None."""
    if not var_type or var_type.startswith("*"):
        return None
    upper = var_type.upper()
    if upper == "BOOL":
        return "bool"
    if upper in IEC_INT_TYPES:
        return "int"
    if upper in ("REAL", "LREAL"):
        return "real"
    if upper.startswith(("STRING", "WSTRING")):
        return "string"
    return None


def visibility_expression_type(conditions: list[tuple[str, str, int | None]]) -> tuple[str, str]:
    """Return the name and definition of a BOOL expression type that is true if all conditions hold.

    Each condition is (role, kind, value): role "index" is true if the operand equals ``value``,
    role "status" if bit 0 of the VC4 status datapoint (1 = invisible) is not set.
    """
    name, operands, terms = "BkVisible", [], []
    for number, (role, kind, value) in enumerate(conditions):
        operand = f"op{number}"
        operands.append(f'<Operand name="{operand}" datatype="{"BOOL" if kind == "bool" else "ANY_INT"}" />')
        if role == "index":
            name += f"_{'B' if kind == 'bool' else 'I'}{value}"
            terms.append((operand if value else f"NOT {operand}") if kind == "bool" else f"{operand} = {value}")
        else:
            name += "_S" if kind == "int" else "_SB"
            terms.append(f"({operand} MOD 2) = 0" if kind == "int" else f"NOT {operand}")
    definition = (f'    <ExpressionType name="{name}" datatype="BOOL">\r\n      <Operands>\r\n'
                  + "".join(f"        {operand}\r\n" for operand in operands)
                  + f"      </Operands>\r\n      <Operation>{' AND '.join(f'({term})' for term in terms)}</Operation>\r\n"
                  "    </ExpressionType>\r\n")
    return name, definition


def vc4_page_dynamic(page: ET.Element) -> bool:
    """True if a control of the page is bound to a PLC variable outside the UVis runtime."""
    for control in page.iter(VC4_NS + "Control"):
        for key, value in vc4_properties(control).items():
            path = vc4_datapoint(value) if key.endswith("Datapoint") else None
            if path and not UVIS_VARIABLE_RE.search(path):
                return True
    return False


def vc4_datapoint(value: str | None) -> str | None:
    """Return the variable path of a VC4 datapoint reference (Source[global].Variable[...]), else None."""
    match = VC4_DATAPOINT_RE.match(value or "")
    return match.group("path") if match else None


def vc4_text_group(visu: Path, logical: Path, reference: str | None, embedded: dict[int, str],
                   offset: int) -> dict[int, str]:
    """Return {index: text} of the text group of a VC4 control with multiple texts.

    The group is embedded in the layer (with an index offset), a TextGroups file of the visu
    (Source[local]) or of VCShared (Source[global]).
    """
    if not reference or "[embedded]" in reference:
        return {index - offset: text for index, text in embedded.items() if index >= offset}
    name = vc4_reference(reference)
    files = [visu / "TextGroups" / f"{name}.txtgrp"] if "[local]" in reference else sorted(
        logical.rglob(f"{name}.txtgrp"), key=lambda path: (len(path.parts), str(path).casefold()))
    path = next((file for file in files if file.is_file()), None)
    if path is None:
        return {}
    root = ET.parse(path).getroot()
    indexes = {entry.get("ID"): int(entry.get("Value")) for entry in root.iter(VC4_NS + "Index")}
    texts: dict[int, str] = {}
    layers = sorted(root.findall(VC4_NS + "TextLayer"), key=lambda text_layer: text_layer.get("LanguageId") != "de")
    for text_layer in layers:
        for text in text_layer.findall(VC4_NS + "Text"):
            index = indexes.get(text.get("ID"))
            if index is not None and text.get("Value") and index not in texts:
                texts[index] = text.get("Value")
    return texts


def convert_vc4_page(page: ET.Element, page_name: str, content: str, builder_size: tuple[int, int],
                     source: tuple[int, int, int] = (VC4_PAGE_WIDTH, VC4_PAGE_HEIGHT, VC4_HEADER_HEIGHT), *,
                     titles: dict[int, str] | None = None,
                     palette: dict[int, str], fonts: dict[str, tuple[float, bool]], keys: dict[str, str],
                     pages: dict[str, str], datapoints: dict[tuple[int, int, str], str],
                     library: dict[str, tuple[str, int, int]], visu: Path,
                     bitmap_cache: dict[str, dict[int, str]], warnings: list[str],
                     skipped: Iterable[str] = (), plc: PlcDeclarations | None = None,
                     available: Callable[[str, str], str | None] | None = None,
                     media: Callable[[str], str | None] | None = None,
                     logical: Path | None = None, page_indexes: dict[int, str] | None = None) -> _ContentBuilder:
    """Convert the controls of one VC4 page (without referenced common layers) into mappView widgets.

    Buttons to the pages in ``skipped`` (casefolded names, already reported) are left out silently.
    A button with a key vk_UVisPage<n> of the UVis runtime leads to the page with index n of
    ``page_indexes``.
    Controls bound to PLC variables (numeric fields, texts from variables, multiple texts and
    bitmaps, status datapoints) become mappView widgets bound to the variables declared in ``plc``;
    ``available`` returns why a variable cannot be bound in the new project (None if it can be,
    e.g. "missing" or "undeployed"); ``media`` copies a VC4 bitmap into the media
    package and returns its path.  Controls that cannot be bound are listed in ``builder.unbound``.
    """
    page_index = vc4_page_index(page)
    skipped = set(skipped)
    # With titles, every page of the visu reserves the title row so that all pages are scaled alike.
    builder = _ContentBuilder(content, builder_size, source, TITLE_HEIGHT if titles else 0)
    title = (titles or {}).get(page_index)
    if title:
        builder.add("widgets.brease.Label", "Title", top=0, left=0, width=builder_size[0], height=TITLE_HEIGHT,
                    text=title, textAlign="center", fontSize=TITLE_FONT_SIZE, bold="true")
    keys = {**keys, **vc4_virtual_keys(page)}

    def color(value: str | None, default: int | None = None) -> str | None:
        if value is None or not value.strip().isdigit():
            return palette.get(default) if default is not None else None
        return palette.get(int(value))

    def box(properties: dict[str, str]) -> dict[str, int]:
        left, top = float(properties.get("Left", 0)), float(properties.get("Top", 0))
        width, height = float(properties.get("Width", 0)), float(properties.get("Height", 0))
        return {"top": builder.y(top), "left": builder.x(left),
                "width": max(1, builder.x(left + width) - builder.x(left)),
                "height": max(1, builder.y(top + height) - builder.y(top))}

    def font(properties: dict[str, str]) -> dict[str, object]:
        size, bold = fonts.get(vc4_reference(properties.get("Font")) or "DefaultFont", (0, False))
        return {"fontSize": f"{round(size * 4 / 3 * builder.font_scale)}px" if size else None,
                "bold": "true" if bold else None}

    def variable(value: str | None, kinds: tuple[str, ...]) -> tuple[str, str] | None:
        """Return (OPC UA ref, kind) of a datapoint bound to a usable variable; record why not otherwise."""
        path = vc4_datapoint(value)
        if path is None or UVIS_VARIABLE_RE.search(path):
            return None
        resolved = resolve_vc4_variable(plc, path) if plc else None
        if resolved is None:
            builder.unbound.append((path.removeprefix("Local."), "unknown"))
            return None
        scope, name, var_type = resolved
        label = name if scope == "AsGlobalPV" else f"{scope}:{name}"
        reason = available(scope, name) if available and not (var_type and var_type.startswith("*")) else None
        if var_type and var_type.startswith("*"):
            builder.unbound.append((label, "pointer"))
        elif reason:
            builder.unbound.append((label, reason))
        elif value_kind(var_type) not in kinds:
            builder.unbound.append((label, "type"))
        else:
            builder.variables.add((scope, re.sub(r"\[.*", "", name.split(".")[0])))
            return f"::{scope}:{name}", value_kind(var_type)
        return None

    def fill(properties: dict[str, str], default: int | None = None) -> str:
        """Background of a VC4 control for a shape widget, transparent unless VC4 fills it with a color.

        The mappView Rectangle is orange (#FF8800) without a background color.
        """
        if properties.get("FillStyle", "").casefold() == "transparent":
            return "transparent"
        return color(properties.get("BackColor"), default) or "transparent"

    def status(properties: dict[str, str]) -> list[tuple[str, str, str, int | None]]:
        """The visibility condition of the VC4 status datapoint (bit 0 = invisible), if bound."""
        bound = variable(properties.get("StatusDatapoint"), ("int", "bool"))
        return [("status", bound[0], bound[1], None)] if bound else []

    layers = sorted(page.iter(VC4_NS + "Layer"), key=lambda layer: int(vc4_properties(layer).get("Z-Order", "0") or 0))
    for layer in layers:
        texts = vc4_texts(layer)
        controls = layer.find(VC4_NS + "Controls")
        for control in (controls if controls is not None else []):
            class_id, name = (control.get("ClassId") or "").casefold(), control.get("Name", "")
            properties = vc4_properties(control)
            if class_id == VC4_LINE:
                builder.add("widgets.brease.Line", "Line",
                            x1=builder.x(float(properties.get("StartX", 0))),
                            x2=builder.x(float(properties.get("EndX", 0))),
                            y1=builder.y(float(properties.get("StartY", 0))),
                            y2=builder.y(float(properties.get("EndY", 0))),
                            lineColor=color(properties.get("ForeColor"), VC4_DEFAULT_FORE_COLOR),
                            lineWidth=f"{properties.get('LineWidth', '1')}px")
            elif class_id == VC4_SHAPE:
                ellipse = properties.get("Shape", "").casefold() == "ellipse"
                builder.add("widgets.brease.Ellipse" if ellipse else "widgets.brease.Rectangle",
                            "Ellipse" if ellipse else "Rectangle", **box(properties),
                            backColor=fill(properties, VC4_DEFAULT_SHAPE_BACK_COLOR),
                            lineColor=color(properties.get("ForeColor"), VC4_DEFAULT_FORE_COLOR),
                            borderStyle="solid", borderWidth=f"{properties.get('LineWidth', '1')}px")
            elif class_id == VC4_TEXT and VC4_BK_NAME_RE.match(name):
                # The UVis runtime shows the name of the datapoint (Local.Uvis.uservis.UVis..[i].Name);
                # mappView gets a label with that name.
                match = VC4_BK_NAME_RE.match(name)
                index, var_type = int(match.group("idx")), "bk" + match.group("type").casefold()
                datapoint = datapoints.get((page_index, index, var_type)) or datapoints.get((0, index, var_type))
                if datapoint is None:
                    warnings.append(tr("warn_visu_no_name_datapoint", page=page_name, control=name,
                                       page_index=page_index, idx=index, type="Bk" + match.group("type")))
                    continue
                align = properties.get("AlignmentHorizontal", "Left").casefold()
                area = box(properties)
                width, height = NAME_LABEL_SIZE
                ref = builder.add("widgets.brease.Label", "Name",
                                  top=max(0, area["top"] + (area["height"] - height) // 2),
                                  left=max(0, area["left"] + (area["width"] - width) // 2), width=width, height=height,
                                  text=datapoint, textColor=color(properties.get("ForeColor"), VC4_DEFAULT_FORE_COLOR),
                                  backColor=color(properties.get("BackColor")),
                                  textAlign=align if align in ("left", "center", "right") else "left",
                                  fontSize=NAME_LABEL_FONT_SIZE, bold=font(properties)["bold"])
                builder.session_bindings.append((ref, "visible"))
            elif class_id == VC4_TEXT:
                datapoint = properties.get("TextDatapoint", "None")
                if datapoint != "None" and UVIS_VARIABLE_RE.search(datapoint):
                    continue  # dynamic text of the UVis runtime
                align = properties.get("AlignmentHorizontal", "Left").casefold()
                style = {"textColor": color(properties.get("ForeColor"), VC4_DEFAULT_FORE_COLOR),
                         "backColor": None if properties.get("FillStyle", "").casefold() == "transparent"
                         else color(properties.get("BackColor")),
                         "textAlign": align if align in ("left", "center", "right") else "left", **font(properties)}
                visible = status(properties)
                source = properties.get("TextSource")
                if source == "SingleString" and vc4_datapoint(datapoint):
                    # Text of a string variable.
                    widget = builder.add("widgets.brease.TextOutput", "TextOutput", **box(properties), **style)
                    bound = variable(datapoint, ("string",))
                    if bound:
                        builder.bind(widget, bound[0])
                    builder.show_if(widget, visible)
                    continue
                if source == "MultipleTexts":
                    # One label per text of the group, shown while the index variable has its value.
                    group = vc4_text_group(visu, logical or visu, properties.get("TextGroup"), texts,
                                           int(properties.get("TextIndexOffset", "0") or 0))
                    bound = variable(properties.get("IndexDatapoint"), ("int", "bool"))
                    shown = [(value, text) for value, text in sorted(group.items())
                             if bound and (bound[1] == "int" or value in (0, 1))]
                    for value, text in shown:
                        widget = builder.add("widgets.brease.Label", "Label", **box(properties), text=text, **style)
                        builder.show_if(widget, [("index", bound[0], bound[1], value)] + visible)
                    if not shown:
                        builder.show_if(builder.add("widgets.brease.Label", "Label", **box(properties), text="",
                                                    **style), visible)
                    continue
                text = ""
                if source == "SingleText":
                    text = texts.get(int(properties.get("TextIndexOffset", properties.get("TextIndex", "0")) or 0), "")
                elif datapoint != "None":
                    warnings.append(tr("warn_visu_text_datapoint", page=page_name, control=name, datapoint=datapoint))
                if text.strip() in VC4_IGNORED_TEXTS:
                    continue  # axis letters of the Rk template
                builder.show_if(builder.add("widgets.brease.Label", "Label", **box(properties), text=text, **style),
                                visible)
            elif class_id == VC4_NUMERIC:
                datapoint = properties.get("ValueDatapoint")
                if not vc4_datapoint(datapoint) or UVIS_VARIABLE_RE.search(datapoint or ""):
                    continue  # numeric fields of the UVis runtime (Bk templates)
                editable = properties.get("Input") == "True"
                bound = variable(datapoint, ("int", "real"))
                kind = "NumericInput" if editable else "NumericOutput"
                widget = builder.add(f"widgets.brease.{kind}", kind, **box(properties), **font(properties),
                                     format=INTEGER_FORMAT if bound and bound[1] == "int" else None)
                if bound:
                    builder.bind(widget, bound[0], mode="twoWay" if editable else "oneWay")
                builder.show_if(widget, status(properties))
            elif class_id == VC4_BUTTON:
                key = vc4_reference(properties.get("VirtualKey"))
                target = keys.get(key or "")
                uvis_page = UVIS_PAGE_KEY_RE.match(key or "")
                if target is None and uvis_page:
                    # The UVis runtime changes to the page with this index.
                    target = (page_indexes or {}).get(int(uvis_page.group("index")), f"#{uvis_page.group('index')}")
                if target is None:
                    if key:
                        # The program evaluates the key through the VC4 key matrix; mappView cannot.
                        builder.show_if(builder.add("widgets.brease.Button", "Button", **box(properties),
                                                    text=texts.get(int(properties.get("TextIndex", "0") or 0), ""),
                                                    **font(properties)), status(properties))
                        builder.unbound.append((key, "key"))
                    continue  # not a navigation button
                if target.casefold() not in pages:
                    if target.casefold() not in skipped:
                        warnings.append(tr("warn_visu_navigation", page=page_name, control=name, target=target))
                    continue
                target = pages[target.casefold()]  # VC4 page names are not case-sensitive
                builder.add("widgets.brease.NavigationButton", "NavigationButton", **box(properties), pageId=target,
                            text=texts.get(int(properties.get("TextIndex", "0") or 0), target), **font(properties))
            elif class_id == VC4_BITMAP:
                index_path = vc4_datapoint(properties.get("IndexDatapoint"))
                if index_path and not UVIS_VARIABLE_RE.search(index_path):
                    # One image per bitmap of the group, shown while the index variable has its value.
                    names = vc4_bitmap_names(visu, vc4_reference(properties.get("BitmapGroup")) or "", bitmap_cache)
                    bound = variable(properties.get("IndexDatapoint"), ("int", "bool"))
                    if bound:
                        images = [(value, bitmap) for value, bitmap in sorted(names.items())
                                  if bound[1] == "int" or value in (0, 1)]
                    else:
                        images = [(None, bitmap) for bitmap in [names.get(int(properties.get("BitmapIndex", "0") or 0))]
                                  if bitmap]
                    added = False
                    for value, bitmap in images:
                        path = media(bitmap) if media else None
                        if path is None:
                            builder.unbound.append((bitmap, "bitmap"))
                            continue
                        widget = builder.add("widgets.brease.Image", "Image", **box(properties), image=path,
                                             sizeMode="contain")
                        builder.show_if(widget, ([("index", bound[0], bound[1], value)] if bound else [])
                                        + status(properties))
                        added = True
                    if not added:
                        builder.add("widgets.brease.Rectangle", "Rectangle", **box(properties),
                                    backColor="transparent", borderStyle="solid")
                    continue
                match = VC4_BK_BITMAP_RE.match(name)
                if match is None:
                    # A general bitmap (single bitmap or fixed index of a group) becomes an image; one
                    # switched by the UVis runtime (e.g. a copy BkAl03LED_Bmp1) would show a wrong state.
                    if index_path:
                        bitmap = None
                    elif properties.get("BitmapSource") == "SingleBitmap":
                        bitmap = vc4_reference(properties.get("Bitmap"))
                    else:
                        bitmap = vc4_bitmap_names(visu, vc4_reference(properties.get("BitmapGroup")) or "",
                                                  bitmap_cache).get(int(properties.get("BitmapIndex", "0") or 0))
                    path = media(bitmap) if bitmap and media else None
                    if path:
                        builder.show_if(builder.add("widgets.brease.Image", "Image", **box(properties), image=path,
                                                    sizeMode="contain"), status(properties))
                        continue
                    if bitmap:
                        builder.unbound.append((bitmap, "bitmap"))
                    builder.add("widgets.brease.Rectangle", "Rectangle", **box(properties),
                                backColor=fill(properties), borderStyle="none")
                    continue
                group = vc4_reference(properties.get("BitmapGroup")) or ""
                bitmap = vc4_bitmap_names(visu, group, bitmap_cache).get(
                    int(properties.get("BitmapIndex", "0") or 0), group)
                wanted = bk_widget_name(match.group("type"), match.group("variant"), bitmap)
                widget = library.get(wanted.casefold())
                if widget is None:
                    warnings.append(tr("warn_visu_no_widget", page=page_name, control=name, widget=wanted))
                    builder.add("widgets.brease.Rectangle", "Rectangle", **box(properties),
                                backColor="transparent", borderStyle="solid")
                    continue
                widget_id, width, height = widget
                left, top = float(properties.get("Left", 0)), float(properties.get("Top", 0))
                center_x = builder.x(left + float(properties.get("Width", 0)) / 2)
                center_y = builder.y(top + float(properties.get("Height", 0)) / 2)
                ref = builder.add(f"widgets.BkVisuLib.{widget_id}", widget_id,
                                  top=max(0, round(center_y - height / 2)), left=max(0, round(center_x - width / 2)))
                index, var_type = int(match.group("idx")), "bk" + match.group("type").casefold()
                datapoint = datapoints.get((page_index, index, var_type)) or datapoints.get((0, index, var_type))
                if datapoint is None:
                    warnings.append(tr("warn_visu_no_datapoint", page=page_name, control=name,
                                       page_index=page_index, idx=index, type="Bk" + match.group("type")))
                else:
                    builder.bindings.append((ref, datapoint))
            # All other controls (numeric fields, hotspots, bar graphs, ...) are ignored.
    # Every page gets the switch that shows and hides the datapoint names.
    width, height = NAMES_TOGGLE_SIZE
    toggle = builder.add("widgets.brease.ToggleButton", "ToggleButton",
                         top=builder_size[1] - height - NAMES_TOGGLE_MARGIN, left=NAMES_TOGGLE_MARGIN,
                         width=width, height=height, text=NAMES_TOGGLE_TEXT)
    builder.session_bindings.append((toggle, "value"))
    return builder


def write_bindings(path: Path, set_id: str, bindings: list[tuple[str, str]],
                   session_bindings: Iterable[tuple[str, str]] = (), value_bindings: Iterable[str] = ()) -> None:
    """Write a bindings set that binds each (widget, datapoint) of the content ``set_id``, each
    (widget, attribute) to the session variable of the datapoint names and the complete
    ``value_bindings`` of PLC variables and expressions."""
    entries = "".join(
        '\t\t<Binding mode="twoWay">\r\n'
        f'\t\t\t<Source xsi:type="opcUaComplexObject" refId="::AsGlobalPV:{datapoint}" />\r\n'
        f'\t\t\t<Target xsi:type="brease" contentRefId="{set_id}" widgetRefId="{widget}" attribute="value" />\r\n'
        "\t\t</Binding>\r\n"
        for widget, datapoint in bindings)
    entries += "".join(
        '\t\t<Binding mode="twoWay">\r\n'
        f'\t\t\t<Source xsi:type="session" refId="{NAMES_SESSION_VARIABLE}" attribute="value" />\r\n'
        f'\t\t\t<Target xsi:type="brease" contentRefId="{set_id}" widgetRefId="{widget}" attribute="{attribute}" />\r\n'
        "\t\t</Binding>\r\n"
        for widget, attribute in session_bindings)
    entries += "".join(value_bindings)
    write_utf8(path, '\ufeff<?xml version="1.0" encoding="utf-8"?>\r\n'
                     f'<BindingsSet id="{set_id}" '
                     'xmlns="http://www.br-automation.com/iat2015/binding/engineering/v2" '
                     'xmlns:bt="http://www.br-automation.com/iat2015/bindingListTypes/engineering/v2" '
                     'xmlns:be="http://www.br-automation.com/iat2015/bindingListEmbedded/engineering/v2" '
                     'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\r\n'
                     f"\t<Bindings>\r\n{entries}\t</Bindings>\r\n</BindingsSet>")


def ensure_session_variable(mappview: Path, name: str, warnings: list[str]) -> Path | None:
    """Declare the BOOL session variable ``name`` (default false) in the project's *.svar file."""
    files = sorted(mappview.rglob("*.svar"), key=lambda path: (len(path.parts), str(path).casefold()))
    if not files:
        warnings.append(tr("warn_visu_no_session_variables", path=mappview, variable=name))
        return None
    text, encoding = read_text_with_encoding(files[0])
    if re.search(rf'<Variable\b[^>]*\bname="{re.escape(name)}"', text):
        return files[0]
    closing = re.search(r"^([ \t]*)</Variables>", text, re.MULTILINE)
    if closing is None:
        warnings.append(tr("invalid_mappview_file", section="Variables", path=files[0]))
        return None
    newline = "\r\n" if "\r\n" in text else "\n"
    entry = f'{closing.group(1)}  <Variable name="{name}" xsi:type="BOOL" value="false" />{newline}'
    files[0].write_text(text[:closing.start()] + entry + text[closing.start():], encoding=encoding, newline="")
    return files[0]


def write_utf8(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def add_xml_entry(path: Path, section: str, entry: str, key: str) -> bool:
    """Add ``entry`` to the <section> of an XML file unless the section already lists ``key``."""
    text, encoding = read_text_with_encoding(path)
    existing = re.search(rf"<{section}>(.*?)</{section}>", text, re.DOTALL)
    if existing and re.search(rf'refId="{re.escape(key)}"|>\s*{re.escape(key)}\s*<', existing.group(1)):
        return False
    newline = "\r\n" if "\r\n" in text else "\n"
    closing = re.search(rf"^([ \t]*)</{section}>", text, re.MULTILINE)
    if closing is None:
        empty = re.search(rf"^([ \t]*)<{section}\s*/>", text, re.MULTILINE)
        if empty is None:
            raise ConversionError(tr("invalid_mappview_file", path=path, section=section))
        indent = empty.group(1)
        text = (text[:empty.start()] + f"{indent}<{section}>{newline}{indent}  {entry}{newline}{indent}</{section}>"
                + text[empty.end():])
    else:
        text = text[:closing.start()] + f"{closing.group(1)}  {entry}{newline}" + text[closing.start():]
    path.write_text(text, encoding=encoding, newline="")
    return True


# --- BkVisu navigation bar ----------------------------------------------------------------------

MAPPVIEW_NAVIGATION_CONTENT = "BkV_NavigationContent"
# Buttons of the example pages of the BkNG basis project are replaced by the converted pages.
NAV_SAMPLE_PAGE_RE = re.compile(r"^BkV_Sample\d*$", re.IGNORECASE)
NAV_GAP, NAV_MAX_WIDTH, NAV_MIN_WIDTH = 5, 235, 80
NAV_FONT_SIZE, NAV_FONT_SIZE_TWO_ROWS = 18, 14
VC4_PAGE_PREFIX_RE = re.compile(r"^S\d+_", re.IGNORECASE)
# Common abbreviations of building services words (keys casefolded, with umlauts).
NAV_ABBREVIATIONS = {
    "abluft": "Abl", "abschnitt": "Abschn", "allgemein": "Allg", "allgemeine": "Allg", "anlage": "Anl",
    "anlagen": "Anl", "außenluft": "AUL", "betrieb": "Betr", "bereich": "Ber", "dachgeschoss": "DG",
    "einstellungen": "Einst", "energie": "Energ", "erdgeschoss": "EG", "fernwärme": "FW", "fortluft": "FOL",
    "gebäude": "Geb", "gruppe": "Gr", "hauptmenü": "Menü", "heizkreis": "HK", "heizung": "Hzg",
    "infoseite": "Info", "istwerte": "Istw", "kaltwasser": "KW", "kellergeschoss": "KG", "kessel": "Kes",
    "kühlung": "Kühl", "lüftung": "Lüft", "meldungen": "Meld", "meldung": "Meld", "obergeschoss": "OG",
    "parameter": "Param", "pumpe": "Pu", "pumpen": "Pu", "regelung": "Reg", "sollwerte": "Sollw",
    "sollwert": "Sollw", "station": "Stat", "steuerung": "Steu", "störungen": "Stör", "störung": "Stör",
    "temperatur": "Temp", "temperaturen": "Temp", "trinkwasser": "TW", "übergabe": "Üb", "übersicht": "Übers",
    "untergeschoss": "UG", "versorgung": "Vers", "verteilung": "Vert", "wärmepumpe": "WP", "warmwasser": "WW",
    "zähler": "Zähl", "zeitprogramm": "ZP", "zeitprogramme": "ZP", "zentrale": "Zentr", "zuluft": "Zul",
}
# Words that are left out when the text is still too long.
NAV_FILLER_WORDS = {"abschnitt", "anlage", "anlagen", "bereich", "bild", "seite", "stat", "station"}
NAV_DIRECTIONS = {"nord": "N", "ost": "O", "süd": "S", "west": "W"}


def german_umlauts(word: str) -> str:
    """Replace the transcriptions ae/oe/ue, e.g. Lueftung -> Lüftung (not in Steuerung, Quelle, aktuell)."""
    if word.isupper():
        return word  # abbreviation like KUE
    for plain, umlaut in (("a", "ä"), ("o", "ö"), ("u", "ü")):
        word = re.sub(rf"(?<![aeqAEQ]){plain}e(?!ll)", umlaut, word)
        word = re.sub(rf"^{plain.upper()}e", umlaut.upper(), word)
    return word


def nav_words(page_name: str) -> list[str]:
    """Split a VC4 page name like S03_StatHzgOstWest into words: Stat, Hzg, Ost, West."""
    name = VC4_PAGE_PREFIX_RE.sub("", page_name) or page_name
    words = re.findall(r"[A-ZÄÖÜ]+(?![a-zäöüß])|[A-ZÄÖÜ]?[a-zäöüß]+|\d+", name)
    return [word.upper() if len(word) == 1 else german_umlauts(word) for word in words] or [page_name]


def word_stem(word: str) -> str:
    """Return the first syllable of a word, e.g. Meldungen -> Meld, Sollwerte -> Soll, Übergabe -> Üb."""
    if word.isupper() or word.isdigit():
        return word
    match = re.match(r"[^aeiouyäöü]*[aeiouyäöü]+[^aeiouyäöü]{0,2}", word, re.IGNORECASE)
    return match.group(0) if match else word


def nav_label(page_name: str, max_chars: int) -> str:
    """Return a button text for a page that is at most ``max_chars`` long.

    The text is shortened step by step until it fits: known abbreviations
    (Heizung -> Hzg, Ost West -> O/W), leaving out filler words (Station,
    Bereich, ...), shortening words to their first syllable and finally
    leaving out words at the end.
    """
    words = [(word.casefold(), word) for word in nav_words(page_name)]

    def text() -> str:
        return " ".join(short for _, short in words)

    if len(text()) <= max_chars:
        return text()
    abbreviated: list[tuple[str, str]] = []
    for word, short in words:
        if word in NAV_DIRECTIONS and abbreviated and abbreviated[-1][0].split("/")[-1] in NAV_DIRECTIONS:
            previous, previous_short = abbreviated.pop()
            if "/" not in previous_short:
                previous_short = NAV_DIRECTIONS[previous]
            abbreviated.append((f"{previous}/{word}", f"{previous_short}/{NAV_DIRECTIONS[word]}"))
        else:
            abbreviated.append((word, NAV_ABBREVIATIONS.get(word, short)))
    words = abbreviated
    if len(text()) > max_chars and len(words) > 1:
        words = [entry for entry in words if entry[0] not in NAV_FILLER_WORDS] or words[:1]
    while len(text()) > max_chars:
        candidates = [index for index, (_, short) in enumerate(words) if len(word_stem(short)) < len(short)]
        if not candidates:
            break
        index = max(candidates, key=lambda index: len(words[index][1]))
        words[index] = (words[index][0], word_stem(words[index][1]))
    while len(text()) > max_chars and len(words) > 1:
        words.pop()
    return text()[:max_chars]


def nav_labels(pages: list[str], max_chars: int) -> list[str]:
    """Return a distinct button text for each page; equal texts get the VC4 page number appended."""
    labels = [nav_label(page, max_chars) for page in pages]
    duplicates = {label for label in labels if labels.count(label) > 1}
    for index, page in enumerate(pages):
        if labels[index] in duplicates:
            number = re.match(r"^S(\d+)_", page, re.IGNORECASE)
            suffix = f" {int(number.group(1)) if number else index + 1}"
            labels[index] = nav_label(page, max(1, max_chars - len(suffix))) + suffix
    return labels


def find_navigation_content(pages_dir: Path) -> Path | None:
    candidates = sorted(pages_dir.rglob("*.content"), key=lambda path: ("navigation" not in path.name.casefold(),
                                                                         str(path).casefold()))
    return next((path for path in candidates
                 if re.search(rf'<Content\b[^>]*\bid="{MAPPVIEW_NAVIGATION_CONTENT}"', read_text(path))), None)


def update_navigation(pages_dir: Path, pages: list[str], warnings: list[str]) -> tuple[Path | None, dict[str, str]]:
    """Add a NavigationButton for each converted page to the NavigationBar of BkV_NavigationContent.

    Buttons of the example pages (BkV_Sample...) and of previously converted
    pages are replaced; the other buttons stay where they are.  The new buttons
    fill the space between the buttons on the left and on the right side of the
    bar, in two rows if one row is too narrow.  Returns the file and {page: text}.
    """
    path = find_navigation_content(pages_dir)
    if path is None:
        warnings.append(tr("warn_visu_no_navigation", path=pages_dir))
        return None, {}
    text, encoding = read_text_with_encoding(path)
    bar = re.search(r'(<Widget\b[^>]*xsi:type="widgets\.brease\.NavigationBar"[^>]*>\s*<Widgets>)(.*?)'
                    r'(\r?\n?[ \t]*</Widgets>)', text, re.DOTALL)
    if bar is None:
        warnings.append(tr("warn_visu_no_navigation_bar", path=path))
        return path, {}
    newline = "\r\n" if "\r\n" in text else "\n"
    buttons = re.findall(r"<Widget\b[^>]*/>", bar.group(2))
    converted = {page.casefold() for page in pages}
    kept = [button for button in buttons if not NAV_SAMPLE_PAGE_RE.match(xml_attribute(button, "pageId"))
            and xml_attribute(button, "pageId").casefold() not in converted]
    removed = {xml_attribute(button, "id") for button in buttons if button not in kept}
    indent = re.search(r"\n([ \t]*)<Widget", bar.group(2))
    indent = indent.group(1) if indent else re.match(r"[ \t]*", text[text.rfind("\n", 0, bar.start()) + 1:]).group(0) + "    "

    def number(button: str, name: str, default: int = 0) -> int:
        value = xml_attribute(button, name)
        return int(value) if value.lstrip("-").isdigit() else default

    bar_width, bar_height = number(bar.group(1), "width", 1190), number(bar.group(1), "height", 90)
    left_side = [button for button in kept if number(button, "left") < bar_width / 2]
    right_side = [button for button in kept if button not in left_side]
    start = max((number(button, "left") + number(button, "width") for button in left_side), default=0) + NAV_GAP
    end = min((number(button, "left") for button in right_side), default=bar_width) - NAV_GAP
    top, height = (number(kept[0], "top", 20), number(kept[0], "height", 50)) if kept else (20, 50)

    def width_for(count: int) -> int:
        return min(NAV_MAX_WIDTH, (end - start - NAV_GAP * (count - 1)) // count) if count else 0

    rows, per_row, font_size = 1, len(pages), NAV_FONT_SIZE
    if pages and width_for(per_row) < NAV_MIN_WIDTH:
        rows, per_row, font_size = 2, (len(pages) + 1) // 2, NAV_FONT_SIZE_TWO_ROWS
        height = (bar_height - 3 * NAV_GAP) // 2
        while per_row and width_for(per_row) < NAV_MIN_WIDTH:
            per_row -= 1
    placed = pages[:rows * per_row]
    if len(placed) < len(pages):
        warnings.append(tr("warn_visu_navigation_full", pages=", ".join(pages[len(placed):])))
    width = width_for(per_row)
    # Average width of a bold character is about 0.6 of the font size.
    labels = nav_labels(placed, max(3, int((width - 10) / (font_size * 0.6))))

    used = set(re.findall(r'\bid="([^"]*)"', text)) - removed
    new_buttons, counter = [], 0
    for index, (page, label) in enumerate(zip(placed, labels)):
        counter += 1
        while f"NavigationButton{counter}" in used:
            counter += 1
        row, column = divmod(index, per_row) if rows > 1 else (0, index)
        button_top = NAV_GAP + row * (height + NAV_GAP) if rows > 1 else top
        new_buttons.append(
            f'<Widget xsi:type="widgets.brease.NavigationButton" id="NavigationButton{counter}" top="{button_top}" '
            f'left="{start + column * (width + NAV_GAP)}" width="{width}" height="{height}" zIndex="0" '
            f'pageId="{xml_quote(page)}" text="{xml_quote(label)}" fontSize="{font_size}px" bold="true" />')
    ordered = [re.sub(r'\bzIndex="[^"]*"', f'zIndex="{index}"', button)
               for index, button in enumerate(left_side + new_buttons + right_side)]
    body = "".join(f"{newline}{indent}{button}" for button in ordered)
    text = text[:bar.start(2)] + body + text[bar.end(2):]
    path.write_text(text, encoding=encoding, newline="")
    return path, dict(zip(placed, labels))


def find_vis_file(cpu: Path, chooser: FileChooser | None) -> Path:
    """Return the mappView visualisation file (.vis) of the PLC folder ``cpu``."""
    vis_files = sorted(cpu.rglob("*.vis"), key=lambda path: str(path).casefold())
    if not vis_files:
        raise ConversionError(tr("not_found", filename="*.vis", root=cpu))
    vis_file = vis_files[0]
    if len(vis_files) > 1:
        vis_file = chooser(".vis", cpu, vis_files) if chooser else None
        if vis_file is None:
            raise ConversionError(tr("choice_cancelled", filename=".vis"))
    return vis_file


def convert_user_visu(old_root: Path, new_root: Path, global_var: Path,
                      chooser: FileChooser | None = None, convert_ladder: bool = False) -> VisuConversionResult:
    """Convert the VC4 user visualisation (uvis) of the Bk2000 project into BkVisu mappView pages.

    Each user page becomes a page below Logical/mappView/Visualization/Pages/BkVisu.
    Lines, shapes, texts, navigation buttons and bitmaps are converted; Bk bitmaps
    become BkVisuLib widgets bound to the datapoint with the same #UVIS page/idx
    and type.  The pages and the bindings are registered in the .vis file of the
    new project's active configuration.
    """
    visu = find_user_visu(old_root, chooser)
    if visu is None:
        return VisuConversionResult(None, [], 0, 0, None, [])
    old_root, new_root = old_root.resolve(), new_root.resolve()
    new_logical = logical_dir(new_root)
    pages_dir = new_logical.joinpath(*MAPPVIEW_BKVISU_PAGES)
    if not pages_dir.is_dir():
        raise ConversionError(tr("no_bkvisu", path=pages_dir))
    cpu = configuration_cpu(active_configuration(project_root(new_root), chooser, ask=True))
    vis_file = find_vis_file(cpu, chooser)

    warnings: list[str] = []
    types = {name.casefold(): var_type for name, var_type in bk_variables(read_text(global_var))}
    dplist = read_text(discover_file(old_root, "dplist.dat", chooser))
    datapoints, titles = uvis_datapoints(dplist, types), uvis_titles(dplist)
    library = bkvisu_library(new_logical)
    if not library:
        warnings.append(tr("warn_visu_no_library", path=new_logical.joinpath(*MAPPVIEW_BKVISU_LIBRARY)))
    palette, fonts = vc4_palette(visu), vc4_fonts(visu)
    keys = vc4_virtual_keys(ET.parse(visu / "VirtualKeys.vcvk").getroot()) if (visu / "VirtualKeys.vcvk").exists() else {}
    all_pages = sorted((visu / "Pages").glob("*.page"), key=lambda path: path.stem.casefold())
    page_indexes = {vc4_page_index(ET.parse(path).getroot()): path.stem for path in reversed(all_pages)}
    page_files = [path for path in all_pages if not VC4_IGNORED_PAGE_RE.match(path.stem)]
    # A page showing Bk datapoints without any #UVIS datapoint of its page index showed no values
    # in the old visu either; it is left out with a single warning (also for buttons to it) unless
    # it has controls bound to other PLC variables.
    page_roots = {path: ET.parse(path).getroot() for path in page_files}
    used_pages = {page for page, _, _ in datapoints}
    skipped: set[str] = set()
    for path in page_files:
        index = vc4_page_index(page_roots[path])
        if (index not in used_pages and vc4_shows_datapoints(page_roots[path])
                and not vc4_page_dynamic(page_roots[path])):
            warnings.append(tr("warn_visu_page_no_datapoints", page=path.stem, page_index=index))
            skipped.add(path.stem.casefold())
    page_files = [path for path in page_files if path.stem.casefold() not in skipped]
    page_names = {path.stem.casefold(): path.stem for path in page_files}

    # Controls bound to PLC variables are bound to the variables that exist in the new project.  A task
    # of the old project whose variables are bound is taken over with its include packages; it is
    # added to the Cpu.sw only if the old project deploys it.
    old_logical = logical_dir(old_root)
    plc = plc_declarations(old_logical)
    new_globals, new_programs = global_variable_names(new_logical), logical_programs(new_logical)
    old_programs = logical_programs(old_logical)
    taken_over: dict[str, tuple[str, bool]] = {}  # casefolded task: (task, deployed)

    def take_over_task(scope: str) -> bool:
        """Copy the task ``scope`` of the old project into the new one; True if it is deployed."""
        key = scope.casefold()
        if key not in taken_over:
            folder = old_programs[key]
            relative = folder.relative_to(old_logical)
            packages: list[Path] = []
            copy_logical_object(old_logical, new_logical, relative, "Program", packages)
            for include in task_include_packages(old_logical, folder):
                copy_logical_object(old_logical, new_logical, include, "Package", packages)
            deployed, task_warnings = deploy_tasks(old_root, new_root, [relative], chooser)
            warnings.extend(task_warnings)
            if deployed:
                missing = missing_task_libraries(old_root, new_root, folder, chooser)
                warnings.extend(missing_library_warnings(missing))
            warnings.extend(ladder_upgrade_report(
                upgrade_old_ladder_files(old_ladder_files([new_logical / relative]), new_logical.parent,
                                         convert_ladder))[1])
            warnings.extend(discontinued_function_warnings(new_logical, [new_logical / relative]))
            new_programs[key] = new_logical / relative
            taken_over[key] = (folder.name, bool(deployed))
        return taken_over[key][1]

    def available(scope: str, name: str) -> str | None:
        """Why the variable ``name`` of ``scope`` cannot be bound in the new project, or None."""
        if scope == "AsGlobalPV":
            return None if re.sub(r"\[.*", "", name.split(".")[0]).casefold() in new_globals else "missing"
        key = scope.casefold()
        if key in new_programs and key not in taken_over:
            return None  # e.g. a copied ladder task
        if key not in old_programs or is_ptop_object(old_programs[key].relative_to(old_logical)):
            return "missing"  # PtoP tasks are not taken over
        return None if take_over_task(scope) else "undeployed"

    media_dir = new_logical.joinpath(*MAPPVIEW_MEDIA)
    copied_media: dict[str, str | None] = {}

    def media(bitmap: str) -> str | None:
        """Copy a VC4 bitmap of the visu (or a global one, e.g. of VCShared) into the media package
        BkVisu_UVis; return its mappView path."""
        if bitmap not in copied_media:
            source = next((visu / "Bitmaps" / f"{bitmap}{suffix}" for suffix in VC4_BITMAP_SUFFIXES
                           if (visu / "Bitmaps" / f"{bitmap}{suffix}").is_file()), None)
            if source is None:
                source = next((path for path in sorted(old_logical.rglob(f"{bitmap}.*"))
                               if path.parent.name.casefold() == "bitmaps"
                               and path.suffix.casefold() in VC4_BITMAP_SUFFIXES and path.is_file()), None)
            copied_media[bitmap] = None
            if source is not None and (media_dir / PACKAGE_FILE).exists():
                target = media_dir / UVIS_MEDIA_PACKAGE / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                if not (target.parent / PACKAGE_FILE).exists():
                    write_utf8(target.parent / PACKAGE_FILE, NEW_PACKAGE_TEMPLATE)
                add_xml_entry(target.parent / PACKAGE_FILE, "Objects", f'<Object Type="File">{source.name}</Object>',
                              source.name)
                add_xml_entry(media_dir / PACKAGE_FILE, "Objects",
                              f'<Object Type="Package">{UVIS_MEDIA_PACKAGE}</Object>', UVIS_MEDIA_PACKAGE)
                copied_media[bitmap] = f"Media/{UVIS_MEDIA_PACKAGE}/{source.name}"
        return copied_media[bitmap]

    expressions_dir = new_logical.joinpath(*MAPPVIEW_EXPRESSIONS)
    expression_types: dict[str, str] = {}
    variables: set[tuple[str, str]] = set()

    # The page file of the BkNG start page is the template for the area assignments.
    template_path = next(iter(sorted(pages_dir.glob("BkV_Start/*.page"))), None)
    template = read_text(template_path) if template_path else None
    size = MAPPVIEW_CONTENT_SIZE
    start_content = next(iter(sorted(pages_dir.glob("BkV_Start/*.content"))), None)
    if start_content:
        header = re.search(r"<Content\b[^>]*", read_text(start_content))
        if header and xml_attribute(header.group(0), "width").isdigit() and xml_attribute(header.group(0), "height").isdigit():
            size = (int(xml_attribute(header.group(0), "width")), int(xml_attribute(header.group(0), "height")))

    source = vc4_page_geometry(visu)
    bitmap_cache: dict[str, dict[int, str]] = {}
    converted: list[str] = []
    widget_count = binding_count = variable_widgets = expression_count = 0
    # Each page gets its own bindings set in a BkVisu package next to the BkNG bindings.
    binding_dir = vis_file.parent / MAPPVIEW_BINDINGS_PACKAGE
    if not (binding_dir / PACKAGE_FILE).exists():
        write_utf8(binding_dir / PACKAGE_FILE, NEW_PACKAGE_TEMPLATE)
    vis_package = vis_file.parent / PACKAGE_FILE
    if vis_package.exists():
        add_xml_entry(vis_package, "Objects", f'<Object Type="Package">{MAPPVIEW_BINDINGS_PACKAGE}</Object>',
                      MAPPVIEW_BINDINGS_PACKAGE)
    for page_file in page_files:
        page_name, content = page_file.stem, f"{page_file.stem}Content"
        builder = convert_vc4_page(page_roots[page_file], page_name, content, size, source, titles=titles,
                                   palette=palette,
                                   fonts=fonts, keys=keys, pages=page_names, datapoints=datapoints, library=library,
                                   visu=visu, bitmap_cache=bitmap_cache, warnings=warnings, skipped=skipped,
                                   plc=plc, available=available, media=media, logical=old_logical,
                                   page_indexes=page_indexes)
        if builder.unbound:
            # One warning per page; the elements grouped by reason.
            grouped: dict[str, list[str]] = {}
            for item, reason in builder.unbound:
                if item not in grouped.setdefault(reason, []):
                    grouped[reason].append(item)
            warnings.append(tr("warn_visu_not_dynamic", page=page_name, count=len(builder.unbound),
                               items="; ".join(f"{tr(f'visu_unbound_{reason}')}: {', '.join(items)}"
                                               for reason, items in grouped.items())))
        folder = pages_dir / page_name
        guid = f' guid="{uuid.uuid4()}"'
        widgets = "".join(f"    {widget}\r\n" for widget in builder.widgets)
        write_utf8(folder / f"{content}.content",
                         '﻿<?xml version="1.0" encoding="utf-8"?>\r\n'
                         f'<Content id="{content}"{guid} height="{size[1]}" width="{size[0]}" '
                         'xmlns="http://www.br-automation.com/iat2015/contentDefinition/v2" '
                         'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\r\n'
                         f"  <Widgets>\r\n{widgets}  </Widgets>\r\n</Content>")
        if template:
            page_text = re.sub(r'(<pdef:Page\b[^>]*\bid=")[^"]*', rf"\g<1>{page_name}", template, count=1)
            page_text = re.sub(r'(baseContentRefId=")[^"]*("[^>]*areaRefId="AreaContent")', rf"\g<1>{content}\g<2>",
                               page_text, count=1)
        else:
            page_text = DEFAULT_PAGE_TEMPLATE.format(page=page_name, content=content)
        write_utf8(folder / f"{page_name}.page", page_text)
        write_utf8(folder / PACKAGE_FILE, PAGE_PACKAGE_TEMPLATE.format(page=page_name, content=content))
        ensure_package_entry(pages_dir, pages_dir, page_name)
        add_xml_entry(vis_file, "Pages", f'<Page refId="{page_name}" />', page_name)
        add_xml_entry(vis_file, "Contents", f'<Content refId="{content}" />', content)
        if builder.expressions and not (expressions_dir / PACKAGE_FILE).exists():
            warnings.append(tr("warn_visu_no_expressions", page=page_name, path=expressions_dir))
            builder.value_bindings = [binding for binding in builder.value_bindings if 'xsi:type="expression"' not in binding]
            builder.expressions = []
        if builder.expressions:
            # Expressions of the content: shows widgets depending on index and status variables.
            set_id = f"{content}Expressions"
            entries = "".join(f'    <Expression id="{expression}" xsi:type="content" contentRefId="{content}" '
                              f'type="{type_name}" />\r\n' for expression, type_name in builder.expressions)
            write_utf8(expressions_dir / f"{content}.expression",
                       '﻿<?xml version="1.0" encoding="utf-8"?>\r\n'
                       f'<ExpressionsSet id="{set_id}" '
                       'xmlns="http://www.br-automation.com/iat2015/expression/engineering/v3" '
                       'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\r\n'
                       f"  <Expressions>\r\n{entries}  </Expressions>\r\n</ExpressionsSet>")
            add_xml_entry(expressions_dir / PACKAGE_FILE, "Objects",
                          f'<Object Type="File">{content}.expression</Object>', f"{content}.expression")
            add_xml_entry(vis_file, "ExpressionsSets", f'<ExpressionsSet refId="{set_id}" />', set_id)
            expression_types.update(builder.expression_types)
        binding_file = binding_dir / f"{content}.binding"
        write_bindings(binding_file, content, builder.bindings, builder.session_bindings, builder.value_bindings)
        add_xml_entry(binding_dir / PACKAGE_FILE, "Objects", f'<Object Type="File">{binding_file.name}</Object>',
                      binding_file.name)
        add_xml_entry(vis_file, "BindingsSets", f'<BindingsSet refId="{content}" />', content)
        binding_count += len(builder.bindings)
        widget_count += len(builder.widgets)
        variable_widgets += len(builder.dynamic_widgets)
        expression_count += len(builder.expressions)
        variables |= builder.variables
        converted.append(page_name)

    if expression_types:
        write_expression_types(expressions_dir / EXPRESSION_TYPES_FILE, expression_types)
        add_xml_entry(expressions_dir / PACKAGE_FILE, "Objects", f'<Object Type="File">{EXPRESSION_TYPES_FILE}</Object>',
                      EXPRESSION_TYPES_FILE)
    enabled_variables = enable_visu_variables(cpu, chooser, variables, warnings) if variables else []
    if converted:
        ensure_session_variable(new_logical / "mappView", NAMES_SESSION_VARIABLE, warnings)
    navigation, buttons = update_navigation(pages_dir, converted, warnings)
    return VisuConversionResult(visu, converted, widget_count, binding_count, binding_dir, warnings, navigation,
                                buttons, source, size, variable_widgets, expression_count, enabled_variables,
                                list(taken_over.values()))


QUOTED_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*"(?P<name>[^"]+)"', re.MULTILINE)


def task_include_packages(logical: Path, folder: Path) -> list[Path]:
    """Return the packages (relative to ``logical``) holding the headers a task includes with "...".

    Headers of the task folder itself and of libraries are not returned.
    """
    packages: list[Path] = []
    names = {Path(match.group("name").replace("\\", "/")).name.casefold()
             for path in folder.rglob("*") if path.is_file() and path.suffix.casefold() in (".c", ".cpp", ".h")
             for match in QUOTED_INCLUDE_RE.finditer(read_text(path))}
    for header in sorted(logical.rglob("*.h")):
        if header.name.casefold() not in names or folder in header.parents:
            continue
        package = header.parent
        if any(any(parent.glob("*.lby")) for parent in (package, *package.parents) if logical in parent.parents):
            continue  # header of a library
        relative = package.relative_to(logical)
        if relative.parts and relative not in packages:
            packages.append(relative)
    return packages


def missing_task_libraries(old_root: Path, new_root: Path, folder: Path, chooser: FileChooser | None) -> list[str]:
    """Return the libraries of the old Cpu.sw whose headers the task includes but the new Cpu.sw lacks."""
    try:
        old_sw = configuration_cpu(active_configuration(project_root(old_root), chooser)) / "Cpu.sw"
        new_sw = configuration_cpu(active_configuration(project_root(new_root), chooser)) / "Cpu.sw"
        old_libraries = {xml_attribute(match.group(0), "Name").casefold(): xml_attribute(match.group(0), "Name")
                         for match in SW_LIBRARY_RE.finditer(read_text(old_sw))}
        new_libraries = {xml_attribute(match.group(0), "Name").casefold()
                         for match in SW_LIBRARY_RE.finditer(read_text(new_sw))}
    except (ConversionError, OSError):
        return []
    included = {Path(name.replace("\\", "/")).stem.casefold()
                for path in folder.rglob("*") if path.is_file() and path.suffix.casefold() in (".c", ".cpp", ".h")
                for name in re.findall(r'^\s*#\s*include\s*[<"]([^>"]+)', read_text(path), re.MULTILINE)}
    return sorted(old_libraries[name] for name in included if name in old_libraries and name not in new_libraries)


def write_expression_types(path: Path, types: dict[str, str]) -> None:
    """Write the expression types to ``path``, keeping the types it already defines."""
    existing = read_text(path) if path.exists() else ""
    for match in re.finditer(r'[ \t]*<ExpressionType name="(?P<name>[^"]+)".*?</ExpressionType>\r?\n?', existing, re.DOTALL):
        types.setdefault(match.group("name"), match.group(0).rstrip() + "\r\n")
    write_utf8(path, '﻿<?xml version="1.0" encoding="utf-8"?>\r\n'
                     '<ExpressionTypeSet id="BkVisuExpressionTypes" '
                     'xmlns="http://www.br-automation.com/iat2016/expressionType/v1">\r\n  <ExpressionTypes>\r\n'
                     + "".join(definition for _, definition in sorted(types.items()))
                     + "  </ExpressionTypes>\r\n</ExpressionTypeSet>")


def enable_visu_variables(cpu: Path, chooser: FileChooser | None, variables: set[tuple[str, str]],
                          warnings: list[str]) -> list[str]:
    """Enable the (scope, variable) bound by the visu in OPC UA; returns the newly enabled ones."""
    enabled, enabled_tasks = opcua_enabled(cpu)
    globals_ = sorted({name for scope, name in variables if scope == "AsGlobalPV" and name.casefold() not in enabled})
    tasks: dict[str, set[str]] = {}
    for scope, name in sorted(variables):
        if scope == "AsGlobalPV" or name.casefold() in enabled_tasks.get(scope.casefold(), set()):
            continue
        if scope.casefold() in enabled_tasks:
            # The task already has an entry; it is not extended automatically.
            warnings.append(tr("warn_variable_not_enabled", variable=f"::{scope}:{name}"))
        else:
            tasks.setdefault(scope, set()).add(name)
    if globals_ or tasks:
        add_opcua_entries(opcua_target(cpu, chooser), globals_, [(task, sorted(names)) for task, names in tasks.items()])
    return globals_ + [f"{task}:{name}" for task, names in tasks.items() for name in sorted(names)]


# --- BkNG migration: BkNG project of Automation Studio 4 -> BkNG base project of Automation Studio 6 --

AS_VERSION_RE = re.compile(r'<\?AutomationStudio\b[^?]*?(?<![\w])Version="?(?P<major>\d+)\.(?P<minor>\d+)')
# A project with a VisuTask from this Automation Studio version on is a BkNG project to be migrated.
MIGRATION_MIN_VERSION = (4, 4)
MIGRATION_TARGET_MAJOR = 6
PROJECT_KIND_BK2000, PROJECT_KIND_BKNG = "bk2000", "bkng"
VISU_TASK = "VisuTask"
# Objects directly below Logical that belong to the BkNG base; the new base project brings its own.
BKNG_BASE_LOGICAL_OBJECTS = {name.casefold() for name in (
    "Global.var", "Global.typ", "Libraries", "Project.language", "System", "mappView", "VCShared", "Docu")}
# Test pages of the old BkNG base; they are not taken over and are removed from the new project.
BKNG_TEST_PAGES = {name.casefold() for name in ("PageTest", "PageLineTest", "PageLineTest3px")}
# Example programs and pages of the BkNG base project (BkV_Sample, BkV_Sample1..3); they are removed.
BKNG_SAMPLE_RE = re.compile(r"^BkV_Sample\d*$", re.IGNORECASE)
# Settings of the VisuTask taken over from the old project (all others, e.g. *_MAXDP, come from the new base).
VISU_TASK_SETTINGS = ("UST_Name", "TimeAutoLogout", "InfosAufStartAusblenden", "NavNurBeiLogin")
# A customer library is taken over only together with its source code.
LIBRARY_SOURCE_SUFFIXES = {".c", ".cpp", ".st"}
PACKAGE_OBJECT_RE = re.compile(r"<Object\b(?P<attributes>[^>]*)>\s*(?P<name>[^<]+?)\s*</Object>")
IEC_COMMENT_RE = re.compile(r"'(?:\$.|[^'$\r\n])*'|\(\*.*?\*\)|//[^\r\n]*", re.DOTALL)
IEC_STATEMENT_TOKEN_RE = re.compile(r"'(?:\$.|[^'$])*'|\bEND_STRUCT\b|\bSTRUCT\b|[(\[]|[)\]]|;", re.IGNORECASE)
IEC_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
IEC_SECTIONS = {
    "var": (re.compile(r"^[ \t]*(?P<header>VAR(?:_\w+)?(?:[ \t]+\w+)*)[ \t]*\r?$", re.IGNORECASE | re.MULTILINE),
            re.compile(r"^[ \t]*END_VAR\b", re.IGNORECASE | re.MULTILINE), "END_VAR"),
    "type": (re.compile(r"^[ \t]*(?P<header>TYPE)[ \t]*\r?$", re.IGNORECASE | re.MULTILINE),
             re.compile(r"^[ \t]*END_TYPE\b", re.IGNORECASE | re.MULTILINE), "END_TYPE"),
}
# Users and roles (AccessAndSecurity/UserRoleSystem) and the mapp user management.
CONFIG_ELEMENT_RE = re.compile(r'^[ \t]*<Element\b[^>]*\bID="(?P<id>[^"]*)"[^>]*?(?:/>|>.*?</Element>)[ \t]*\r?\n?',
                               re.DOTALL | re.MULTILINE)
USER_ROLE_RE = re.compile(r'<Property\b[^>]*\bID="Role\[\d+\]"[^>]*\bValue="([^"]*)"')
UA_TASK_RE = re.compile(r'^[ \t]*<Task\b[^>]*\bName="(?P<name>[^"]*)"[^>]*?(?:/>|>(?P<body>.*?)</Task>)[ \t]*\r?\n?',
                        re.DOTALL | re.MULTILINE)
# mappView files.
MV_ROOT_ID_RE = re.compile(r'<(?:\w+:)?(?:Page|Content|Dialog|Layout)\b[^>]*?\sid="(?P<id>[^"]*)"')
MV_WIDGET_ID_RE = re.compile(r'<Widget\b[^>]*?\sid="([^"]*)"')
MV_WIDGET_TAG_RE = re.compile(r"<Widget\b[^>]*>")
MV_ATTRIBUTE_RE = re.compile(r'\s(?P<name>[\w:.\-]+)="')
MV_CONTENT_ASSIGNMENT_RE = re.compile(r'\bbaseContentRefId="([^"]*)"')
MV_AREA_RE = re.compile(r'\bareaRefId="([^"]*)"')
MV_AREA_ID_RE = re.compile(r'<Area\b[^>]*?\sid="([^"]*)"')
MV_BINDING_RE = re.compile(r"^[ \t]*<Binding\b(?:[^>]*/>|.*?</Binding>)[ \t]*\r?\n?", re.DOTALL | re.MULTILINE)
MV_EVENT_BINDING_RE = re.compile(r"^[ \t]*<EventBinding\b(?:[^>]*/>|.*?</EventBinding>)[ \t]*\r?\n?",
                                 re.DOTALL | re.MULTILINE)
MV_REFERENCE_TAG_RE = re.compile(r'<[\w.:]+\b[^>]*?\bcontentRefId="[^"]*"[^>]*>')
MV_SOURCE_RE = re.compile(r"<Source\b[^>]*>")
MV_SET_ID_RE = re.compile(r'<(?P<tag>BindingsSet|EventBindingSet)\b[^>]*?\sid="(?P<id>[^"]*)"')
MV_NAMESPACE_RE = re.compile(r'\sxmlns(?::\w+)?="[^"]*"')
MV_MEDIA_RE = re.compile(r"""Media/[^"'\],;<>]+""")
MV_TEXT_REF_RE = re.compile(r"\$(?P<namespace>[A-Za-z_]\w*)/(?P<key>[\w.\-]+)")
MV_OPCUA_REF_RE = re.compile(r'refId="::(?P<scope>[^:"]+):(?P<name>[A-Za-z_]\w*)')
MV_SESSION_TAG_RE = re.compile(r'<(?:Source|Target)\b[^>]*\bxsi:type="session[^"]*"[^>]*>')
TMX_NAMESPACE_RE = re.compile(r'<prop\b[^>]*type="x-BR-TS:Namespace"[^>]*>\s*([^<]*?)\s*</prop>')
TMX_UNIT_RE = re.compile(r'^[ \t]*<tu\b[^>]*\btuid="(?P<id>[^"]*)"[^>]*>.*?</tu>[ \t]*\r?\n?', re.DOTALL | re.MULTILINE)
# Attributes every widget has; the other attributes of a BkVisuLib widget must be properties of the compound widget.
MV_COMMON_WIDGET_ATTRIBUTES = {name.casefold() for name in (
    "xsi:type", "id", "top", "left", "width", "height", "zIndex", "enable", "visible", "margin", "permissionView",
    "permissionOperate", "description", "style", "tabIndex", "tooltip")}
# BkVisuLib widgets of the old BkNG base that were replaced in the new one.
BKVISU_WIDGET_RENAMES = {"bk_sb_kl": "Bk_Sb_Kl_h"}
# Bindings sets of the migrated customer pages, in a package next to the .vis file.
MIGRATED_BINDINGS_SET, MIGRATED_EVENT_BINDINGS_SET = "BkV_Bindings", "BkV_EventBindings"
EVENT_BINDINGS_SET_HEAD = (
    '﻿<?xml version="1.0" encoding="utf-8"?>\r\n'
    '<EventBindingSet id="{id}"{namespaces}>\r\n  <Bindings>\r\n')
EVENT_BINDINGS_NAMESPACES = (' xmlns="http://www.br-automation.com/iat2014/eventbinding/v2"'
                             ' xmlns:types="http://www.br-automation.com/iat2015/widgetTypes/v2"'
                             ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')
BINDINGS_NAMESPACES = (' xmlns="http://www.br-automation.com/iat2015/binding/engineering/v2"'
                       ' xmlns:bt="http://www.br-automation.com/iat2015/bindingListTypes/engineering/v2"'
                       ' xmlns:be="http://www.br-automation.com/iat2015/bindingListEmbedded/engineering/v2"'
                       ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')


@dataclass
class MigrationResult:
    """Details (report text) and warnings (open items) of one migration step."""
    details: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class MappViewIndex:
    """Path of every mappView page, content, dialog and layout of the Logical view by id."""
    pages: dict[str, Path]
    contents: dict[str, Path]
    dialogs: dict[str, Path]
    layouts: dict[str, Path]


def project_as_version(project_file: Path) -> tuple[int, int] | None:
    """Return (major, minor) of the Automation Studio version of a project file (.apj)."""
    match = AS_VERSION_RE.search(read_text(project_file))
    return (int(match.group("major")), int(match.group("minor"))) if match else None


def child_path(parent: Path, name: str) -> Path | None:
    """Return the file or folder ``name`` of ``parent`` regardless of case, if it exists."""
    if parent.is_dir():
        for child in parent.iterdir():
            if child.name.casefold() == name.casefold():
                return child
    return None


def write_like(path: Path, text: str, encoding: str) -> None:
    """Write ``text`` in the encoding the file had, or as UTF-8 with BOM if the text does not fit into it."""
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        encoding = "utf-8-sig"
    path.write_text(text, encoding=encoding, newline="")


def package_objects(package: Path) -> list[tuple[str, str]]:
    """Return (type, name) of every object listed in a Package.pkg."""
    if not package.is_file():
        return []
    return [(xml_attribute(match.group("attributes"), "Type"), match.group("name"))
            for match in PACKAGE_OBJECT_RE.finditer(read_text(package))]


def logical_programs(logical: Path) -> dict[str, Path]:
    """Return the folder of every program of the Logical view by casefolded name."""
    programs: dict[str, Path] = {}
    for package in sorted(logical.rglob(PACKAGE_FILE), key=lambda path: (len(path.parts), str(path).casefold())):
        for object_type, name in package_objects(package):
            if object_type.casefold() == "program":
                programs.setdefault(name.casefold(), package.parent / name)
    return programs


def detect_project_kind(project_file: Path) -> tuple[str, tuple[int, int] | None]:
    """Return the kind of a source project and its Automation Studio version.

    A project with a program VisuTask from AS 4.4 on is a BkNG project that is
    migrated into the new BkNG base project; every other project is converted as
    Bk2000 project.  A BkNG project of Automation Studio 6 needs no migration.
    """
    root = project_dir_from_file(project_file)
    version = project_as_version(project_file)
    try:
        logical = logical_dir(root)
    except ConversionError:
        return PROJECT_KIND_BK2000, version
    if version and version >= MIGRATION_MIN_VERSION and VISU_TASK.casefold() in logical_programs(logical):
        if version[0] >= MIGRATION_TARGET_MAJOR:
            raise ConversionError(tr("already_as6", path=project_file, version=f"{version[0]}.{version[1]}"))
        return PROJECT_KIND_BKNG, version
    return PROJECT_KIND_BK2000, version


def remove_package_entry(parent: Path, child: str) -> bool:
    """Remove ``child`` from the Package.pkg of ``parent``. Returns True if it was listed."""
    package = parent / PACKAGE_FILE
    if not package.is_file():
        return False
    text, encoding = read_text_with_encoding(package)
    new_text = re.sub(rf"^[ \t]*<Object\b[^>]*>\s*{re.escape(child)}\s*</Object>[ \t]*\r?\n?", "", text,
                      flags=re.IGNORECASE | re.MULTILINE)
    if new_text == text:
        return False
    package.write_text(new_text, encoding=encoding, newline="")
    return True


def remove_logical_object(path: Path) -> None:
    """Delete a file or folder of the project and its entry in the parent's Package.pkg."""
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    remove_package_entry(path.parent, path.name)


def remove_empty_packages(folder: Path, stop: Path) -> None:
    """Delete ``folder`` and its parents below ``stop`` as long as they contain nothing but an empty Package.pkg."""
    while folder != stop and stop in folder.parents and folder.is_dir():
        if package_objects(folder / PACKAGE_FILE) or any(
                child.name.casefold() != PACKAGE_FILE.casefold() for child in folder.iterdir()):
            return
        parent = folder.parent
        remove_logical_object(folder)
        folder = parent


def old_ladder_files(folders: Iterable[Path]) -> list[Path]:
    """Return the Ladder Diagram files in the format of Automation Studio < 6.

    These files keep their networks in a ``<networks>`` element (with a ``<?ladder ...?>``
    or ``<?AutomationStudio Version=3.x?>`` header, often after a long comment); the
    PLCopen format of Automation Studio 6 has none.  Automation Studio 6 builds them
    only after they were opened and saved once (build error 1113).
    """
    return sorted(path for folder in folders for path in folder.rglob("*")
                  if path.is_file() and path.suffix.casefold() == ".ld" and "<networks" in read_text(path))


LD_NETWORK_RE = re.compile(r"<network\b.*?</network>", re.DOTALL)
LD_ROW_RE = re.compile(r"<row\b[^>]*>(?P<cells>.*?)</row>", re.DOTALL)
# One cell of a row: an empty element or an element with content (function blocks, functions).
LD_CELL_RE = re.compile(r"<(?P<tag>[\w:]+)\b(?P<attributes>[^>]*?)(?:/>|>.*?</(?P=tag)>)", re.DOTALL)


LD_BLOCK_TAGS = ("functionblock", "function")
# Positions that start a block: a function block starts with its header (instance name), a function
# with its top row; "topbottom" is a block (or the part below a header) of one row.
LD_BLOCK_STARTS = {"functionblock": ("header",), "function": ("top", "topbottom")}
# Position a part may follow, by the position of the part above it in the same block.
LD_BLOCK_NEXT = {"header": ("top", "topbottom"), "top": ("middle", "bottom"), "middle": ("middle", "bottom")}


def ladder_blocks(network: str) -> list[list[re.Match[str]]] | None:
    """Return the parts (cells) of every function block and function of an old Ladder Diagram network.

    The column of a cell cannot be told from its index (functions, constants etc. are of
    different widths), so the parts are assigned by order: a block covers consecutive rows
    with one part in each, and blocks keep their order from left to right.  Returns None if
    a row does not fit this, e.g. a part continues no block.
    """
    blocks: list[list[re.Match[str]]] = []
    open_blocks: list[list[re.Match[str]]] = []  # left to right
    for row in LD_ROW_RE.finditer(network):
        parts = [cell for cell in LD_CELL_RE.finditer(network, row.start("cells"), row.end("cells"))
                 if cell.group("tag") in LD_BLOCK_TAGS and xml_attribute(cell.group("attributes"), "position")]
        waiting = {tag: [block for block in open_blocks if block[0].group("tag") == tag] for tag in LD_BLOCK_TAGS}
        row_blocks: list[list[re.Match[str]]] = []
        for cell in parts:
            tag, position = cell.group("tag"), xml_attribute(cell.group("attributes"), "position")
            if position in LD_BLOCK_STARTS[tag] or (position == "topbottom" and not waiting[tag]):
                block = [cell]
                blocks.append(block)
            else:
                if not waiting[tag]:
                    return None
                block = waiting[tag].pop(0)
                if position not in LD_BLOCK_NEXT.get(xml_attribute(block[-1].group("attributes"), "position"), ()):
                    return None
                block.append(cell)
            row_blocks.append(block)
        if any(waiting.values()):
            return None  # an open block has no part in this row
        open_blocks = [block for block in row_blocks
                       if xml_attribute(block[-1].group("attributes"), "position") not in ("bottom", "topbottom")]
    return blocks


def complete_ladder_block_types(text: str) -> tuple[str, int]:
    """Give every part of a function block (or function) of an old Ladder Diagram the type of the block.

    A block spans several rows (header, top, middle, bottom).  The format
    ``<?ladder Version = 1.0?>`` has the type only at the top part, but Automation Studio 6
    takes it from the header when it converts the file; without it the block gets
    ``typeName=""`` and is not shown.  Returns the text and the number of completed parts.
    """
    insertions: list[tuple[int, str]] = []  # (position after the tag name, type)
    for network in LD_NETWORK_RE.finditer(text):
        for block in ladder_blocks(network.group(0)) or []:
            types = [xml_attribute(part.group("attributes"), "type") for part in block]
            block_type = next((value for value in types if value), "")
            if block_type:
                insertions += [(network.start() + part.start() + 1 + len(part.group("tag")), block_type)
                               for part, value in zip(block, types) if not value]
    for position, block_type in sorted(insertions, reverse=True):
        text = text[:position] + f' type="{block_type}"' + text[position:]
    return text, len(insertions)


def complete_old_ladder_files(paths: Iterable[Path]) -> int:
    """Complete the block types of the old Ladder Diagram files (see complete_ladder_block_types)."""
    count = 0
    for path in paths:
        text, encoding = read_text_with_encoding(path)
        new_text, completed = complete_ladder_block_types(text)
        if completed:
            path.write_text(new_text, encoding=encoding, newline="")
            count += completed
    return count


# --- Conversion of old Ladder Diagrams into the format of Automation Studio 6 ---
# The conversion of Automation Studio 6 itself is used (see as6_ladder/LdConvert.cs); the helper
# program is compiled once with the C# compiler of the .NET Framework and kept in a cache folder.
AS6_LADDER_SOURCE = RESOURCE_DIR / "as6_ladder" / "LdConvert.cs"
AS6_LADDER_LIBRARY = "BR.AS.Iec.GraphicalEditor.Data.dll"
# Environment variable with the bin folder of Automation Studio 6 ("" switches the conversion off).
AS6_BIN_VARIABLE = "BKNG_AS6_BIN"
# Helper programs run without a console window (the converter is a windowed program).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LD_EXTENSIBLE_INPUT_RE = re.compile(r'<input name=""(?P<rest>[^>]*\bextensible="true")')
WORKING_VERSION_RE = re.compile(r'\bWorkingVersion="(?P<version>\d+\.\d+)')


@dataclass
class LadderUpgradeResult:
    converted: list[Path] = field(default_factory=list)  # now in the format of Automation Studio 6
    remaining: list[Path] = field(default_factory=list)  # still in the old format
    errors: list[str] = field(default_factory=list)  # why files (or all) could not be converted
    version: str = ""


def find_as6_bin() -> Path | None:
    """Return the bin folder of an installed Automation Studio 6 with the Ladder Diagram libraries."""
    override = os.environ.get(AS6_BIN_VARIABLE)
    if override is not None:
        return Path(override) if override and (Path(override) / AS6_LADDER_LIBRARY).is_file() else None
    roots = {os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")} - {None, ""}
    libraries = sorted((path for root in roots for path in Path(root, "BRAutomation").glob(f"AS6*/Bin-*/{AS6_LADDER_LIBRARY}")),
                       key=lambda path: str(path).casefold(), reverse=True)
    return libraries[0].parent if libraries else None


def ladder_converter_program() -> Path:
    """Return the helper program LdConvert.exe, compiling it from its source if necessary."""
    source = AS6_LADDER_SOURCE.read_bytes()
    folder = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "BkNG-Konverter"
    program = folder / f"LdConvert_{hashlib.sha1(source).hexdigest()[:12]}.exe"
    if program.is_file():
        return program
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe"
    if not compiler.is_file():
        raise ConversionError(tr("ladder_no_compiler", path=compiler))
    folder.mkdir(parents=True, exist_ok=True)
    run = subprocess.run([str(compiler), "-nologo", "-platform:x86", "-optimize", f"-out:{program}", str(AS6_LADDER_SOURCE)],
                         capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)
    if run.returncode != 0 or not program.is_file():
        raise ConversionError(tr("ladder_compile_failed", error=(run.stdout + run.stderr).strip()[:500]))
    return program


def project_working_version(project_dir: Path) -> str:
    """Return the working version (e.g. 6.7) of the Automation Studio project in ``project_dir``."""
    for path in sorted(project_dir.glob("*.apj")):
        text = read_text(path)
        if match := WORKING_VERSION_RE.search(text):
            return match.group("version")
        if match := AS_VERSION_RE.search(text):
            return f"{match.group('major')}.{match.group('minor')}"
    return f"{MIGRATION_TARGET_MAJOR}.0"


def prepare_ladder_for_as6(text: str) -> str:
    """Complete an old Ladder Diagram for the conversion of Automation Studio 6.

    Like the editor of Automation Studio 6 (which takes the missing details from the libraries):
    every part of a block gets its type, the unnamed extensible inputs of functions (ADD, EQ,
    AND, ...) are named IN1, IN2, ..., and an empty diagram gets one empty network.
    """
    text, _ = complete_ladder_block_types(text)
    if not LD_NETWORK_RE.search(text):
        text = re.sub(r"(<networks\b[^>]*>)(\s*)(</networks>)",
                      '\\1\\2\t<network label="" comment="" row="1" column="0" commented="0">\\2\t\t<row><empty/></row>'
                      "\\2\t</network>\\2\\3", text, count=1)
    replacements = []
    for network in LD_NETWORK_RE.finditer(text):
        for block in ladder_blocks(network.group(0)) or []:
            number = 0
            for part in block:
                for match in LD_EXTENSIBLE_INPUT_RE.finditer(network.group(0), part.start(), part.end()):
                    number += 1
                    replacements.append((network.start() + match.start(), network.start() + match.end(),
                                         f'<input name="IN{number}"{match.group("rest")}'))
    for start, end, value in sorted(replacements, reverse=True):
        text = text[:start] + value + text[end:]
    return text


def upgrade_old_ladder_files(paths: list[Path], project_dir: Path, convert: bool = True) -> LadderUpgradeResult:
    """Convert old Ladder Diagram files into the format of Automation Studio 6, in place.

    The block types are completed in any case; without ``convert`` (option switched off)
    or if the files cannot be converted (no Automation Studio 6, an error of its
    conversion) they stay in the old format.
    """
    complete_old_ladder_files(paths)
    result = LadderUpgradeResult(remaining=list(paths))
    if not paths or not convert:
        return result
    bin_dir = find_as6_bin()
    if bin_dir is None:
        result.errors.append(tr("ladder_no_as6"))
        return result
    result.version = project_working_version(project_dir)
    try:
        program = ladder_converter_program()
        with tempfile.TemporaryDirectory(prefix="BkNG_ld_") as temp:
            sources: dict[str, tuple[Path, Path]] = {}
            for index, path in enumerate(paths):
                # The file keeps its name: the conversion may take information from it.
                source = Path(temp) / str(index) / path.name
                source.parent.mkdir()
                source.write_text(prepare_ladder_for_as6(read_text(path)), encoding="utf-8")
                sources[str(source)] = (path, source.with_name(source.stem + "_as6" + source.suffix))
            listing = Path(temp) / "files.txt"
            listing.write_text("".join(f"{source}|{target}\n" for source, (_, target) in sources.items()),
                               encoding="utf-8")
            run = subprocess.run([str(program), str(bin_dir), result.version, str(listing)], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=900)
            if run.returncode not in (0, 2):
                raise ConversionError(run.stderr.strip() or f"exit code {run.returncode}")
            for line in run.stdout.splitlines():
                status, _, rest = line.partition(" ")
                source, _, message = rest.partition("\t")
                if source not in sources:
                    continue
                path, target = sources[source]
                if status == "OK" and target.is_file():
                    path.write_bytes(target.read_bytes())
                    result.converted.append(path)
                else:
                    result.errors.append(tr("ladder_file_failed", file=path.name, error=message or status))
    except (ConversionError, OSError, subprocess.SubprocessError) as error:
        result.errors.append(tr("ladder_as6_failed", error=error))
    result.remaining = [path for path in paths if path not in result.converted]
    return result


def ladder_upgrade_report(upgrade: LadderUpgradeResult) -> tuple[str, list[str]]:
    """Return the report details and warnings of a Ladder Diagram conversion."""
    details = tr("ladder_converted", count=len(upgrade.converted), version=upgrade.version) if upgrade.converted else ""
    warnings = list(upgrade.errors)
    if upgrade.remaining:
        warnings.append(tr("warn_ladder_format", count=len(upgrade.remaining),
                           tasks=", ".join(sorted({path.parent.name for path in upgrade.remaining}))))
    return details, warnings


def is_encrypted_source(path: Path) -> bool:
    """True for a source file encrypted by Automation Studio (binary data instead of text)."""
    data = path.read_bytes()[:512]
    control = sum(byte < 9 or 13 < byte < 32 for byte in data)
    return bool(data) and control > len(data) // 20


def copy_logical_file(old_logical: Path, new_logical: Path, relative: Path, packages_updated: list[Path]) -> Path:
    """Copy one file of the Logical view and register it and every package folder above it."""
    target = new_logical / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(old_logical / relative, target)
    for depth in range(len(relative.parts)):
        parent_relative = Path(*relative.parts[:depth])
        new_parent = new_logical / parent_relative
        if ensure_package_entry(old_logical / parent_relative, new_parent, relative.parts[depth],
                                "File" if depth == len(relative.parts) - 1 else "Package", keep_old_header=True):
            if new_parent / PACKAGE_FILE not in packages_updated:
                packages_updated.append(new_parent / PACKAGE_FILE)
    return target


# --- IEC declarations (Global.var, Global.typ) ---

def blank_iec_comments(text: str) -> str:
    """Replace the comments of IEC source text by blanks, keeping line breaks, strings and all positions."""
    return IEC_COMMENT_RE.sub(lambda match: match.group(0) if match.group(0).startswith("'")
                              else re.sub(r"[^\r\n]", " ", match.group(0)), text)


def iec_statements(code: str, start: int, end: int) -> list[tuple[int, int]]:
    """Return (start, end) of the statements terminated by ';' (outside brackets and structures)."""
    statements: list[tuple[int, int]] = []
    depth, begin = 0, start
    for token in IEC_STATEMENT_TOKEN_RE.finditer(code, start, end):
        value = token.group(0).upper()
        if value in ("(", "[", "STRUCT"):
            depth += 1
        elif value in (")", "]", "END_STRUCT"):
            depth -= 1
        elif value == ";" and depth <= 0:
            statements.append((begin, token.end()))
            begin, depth = token.end(), 0
    return statements


def iec_entries(text: str, kind: str) -> list[tuple[str, list[str], str]]:
    """Return (section header, names, source text) of every variable (kind "var") or type (kind "type").

    The source text spans the whole lines of the declaration including a comment at its end.
    """
    code = blank_iec_comments(text)
    start_re, end_re, _ = IEC_SECTIONS[kind]
    entries: list[tuple[str, list[str], str]] = []
    for header in start_re.finditer(code):
        section_end = end_re.search(code, header.end())
        for begin, finish in iec_statements(code, header.end(), section_end.start() if section_end else len(code)):
            first = begin + len(code[begin:finish]) - len(code[begin:finish].lstrip())
            names = [name.strip() for name in code[first:finish].split(":", 1)[0].split(",")]
            if ":" not in code[first:finish] or not all(IEC_IDENTIFIER_RE.fullmatch(name) for name in names):
                continue
            line_start = text.rfind("\n", 0, first) + 1
            start = line_start if not code[line_start:first].strip() else first
            stop = line_end(text, finish)
            source = text[start:stop if not code[finish:stop].strip() else finish]
            entries.append((" ".join(header.group("header").split()).upper(), names,
                            source if source.endswith("\n") else source + "\n"))
    return entries


def merge_iec_declarations(old_text: str, new_text: str, kind: str) -> tuple[str, list[str]]:
    """Append the declarations of ``old_text`` that ``new_text`` lacks, in sections like the old ones.

    Declarations present in both are kept as in ``new_text``.  Returns the merged
    text and the names of the added declarations.
    """
    present = {name.casefold() for _, names, _ in iec_entries(new_text, kind) for name in names}
    sections: OrderedDict[str, list[str]] = OrderedDict()
    added: list[str] = []
    for header, names, source in iec_entries(old_text, kind):
        if any(name.casefold() in present for name in names):
            continue
        sections.setdefault(header, []).append(source)
        added += names
        present.update(name.casefold() for name in names)
    if not sections:
        return new_text, added
    newline = "\n" if "\n" in new_text and "\r\n" not in new_text else "\r\n"
    end = IEC_SECTIONS[kind][2]
    block = "".join(f"{header}\n" + "".join(sources) + f"{end}\n" for header, sources in sections.items())
    block = block.replace("\r\n", "\n").replace("\n", newline)
    if new_text.strip() and not new_text.endswith("\n"):
        new_text += newline
    return (new_text if new_text.strip() else "") + block, added


def global_variable_names(logical: Path) -> set[str]:
    """Return the casefolded names of all global variables: the .var files outside of programs."""
    programs = list(logical_programs(logical).values())
    names: set[str] = set()
    for path in logical.rglob("*.var"):
        if not any(program == path.parent or program in path.parents for program in programs):
            names.update(name.casefold() for _, entry, _ in iec_entries(read_text(path), "var") for name in entry)
    return names


def migrate_global_declarations(old_root: Path, new_root: Path) -> tuple[MigrationResult, Path]:
    """Add the declarations of the old Global.var and Global.typ to those of the new base project.

    Declarations of the base project are kept; further .var/.typ files directly
    below the old Logical folder are copied unless the base project has them.
    Returns the result and the new Global.var.
    """
    old_logical, new_logical = logical_dir(old_root.resolve()), logical_dir(new_root.resolve())
    result = MigrationResult()
    for filename, kind in (("Global.var", "var"), ("Global.typ", "type")):
        old_file = child_path(old_logical, filename)
        if old_file is None or not old_file.is_file():
            continue
        new_file = child_path(new_logical, filename)
        if new_file is None:
            new_file = new_logical / filename
            write_utf8(new_file, "")
            ensure_package_entry(old_logical, new_logical, filename, "File")
        text, encoding = read_text_with_encoding(new_file)
        merged, added = merge_iec_declarations(read_text(old_file), text, kind)
        if added and kind == "var":
            # AS4 accepted members in any case (YMin), AS6 needs them as the function block declares them.
            members, from_project = bk_type_members(new_logical)
            merged, unknown = normalize_initializer_members(merged, members)
            if from_project:
                result.warnings += unknown_member_warnings(unknown)
        if added:
            write_like(new_file, merged, encoding)
        result.details += tr("global_merged", file=new_file, count=len(added))

    existing = {path.name.casefold() for path in new_logical.rglob("*")}
    packages_updated: list[Path] = []
    for object_type, name in package_objects(old_logical / PACKAGE_FILE):
        path = old_logical / name
        if (object_type.casefold() != "file" or name.casefold() in BKNG_BASE_LOGICAL_OBJECTS
                or path.suffix.casefold() not in (".var", ".typ") or not path.is_file()):
            continue
        if name.casefold() in existing:
            result.details += tr("global_file_in_base", file=name)
            continue
        copy_logical_file(old_logical, new_logical, Path(name), packages_updated)
        result.details += tr("global_file_copied", file=name)

    target = child_path(new_logical, "Global.var")
    if target is None:
        raise ConversionError(tr("not_found", filename="Global.var", root=new_logical))
    return result, target


def migrate_visu_task_settings(old_root: Path, new_root: Path) -> MigrationResult:
    """Take over the settings (UST name, auto logout, ...) of the old VisuTask into the new one."""
    result = MigrationResult()
    files = []
    for root in (old_root.resolve(), new_root.resolve()):
        folder = logical_programs(logical_dir(root)).get(VISU_TASK.casefold())
        variables = child_path(folder, "Variables.var") if folder else None
        if variables is None:
            raise ConversionError(tr("not_found", filename=f"{VISU_TASK}/Variables.var", root=logical_dir(root)))
        files.append(variables)
    old_text = read_text(files[0])
    text, encoding = read_text_with_encoding(files[1])
    for name in VISU_TASK_SETTINGS:
        pattern = re.compile(rf"^(?P<head>[ \t]*{name}\s*:[^:;=]*?)\s*:=\s*(?P<value>(?:'(?:\$.|[^'$])*'|[^;'])*?)\s*;",
                             re.IGNORECASE | re.MULTILINE)
        old, new = pattern.search(old_text), pattern.search(text)
        if old is None:
            continue
        if new is None:
            result.warnings.append(tr("warn_visu_setting_missing", name=name, path=files[1]))
            continue
        if old.group("value") != new.group("value"):
            text = text[:new.start("value")] + old.group("value") + text[new.end("value"):]
        result.details += f"{name} := {old.group('value')}\n"
    write_like(files[1], text, encoding)
    result.details = tr("visu_settings", path=files[1]) + result.details
    return result


# --- Removal of the examples of the base project ---

def mappview_index(logical: Path) -> MappViewIndex:
    """Index the mappView pages, contents, dialogs and layouts of a Logical folder by id."""
    index = MappViewIndex({}, {}, {}, {})
    targets = {".page": index.pages, ".content": index.contents, ".dialog": index.dialogs, ".layout": index.layouts}
    for path in sorted(logical.rglob("*"), key=lambda path: str(path).casefold()):
        target = targets.get(path.suffix.casefold())
        if target is not None and path.is_file():
            match = MV_ROOT_ID_RE.search(read_text(path))
            if match:
                target.setdefault(match.group("id"), path)
    return index


def vis_entries(text: str, tag: str) -> list[tuple[str, str]]:
    """Return (refId, element) of the <tag refId=... /> entries of a .vis file."""
    return [(match.group("id"), match.group(0).strip())
            for match in re.finditer(rf'^[ \t]*<{tag}\b[^>]*\brefId="(?P<id>[^"]*)"[^>]*/>', text, re.MULTILINE)]


def remove_vis_entries(vis_file: Path, tag: str, ids: Iterable[str]) -> list[str]:
    """Remove the <tag refId=... /> entries with the given ids from a .vis file; returns the removed ids."""
    ids = set(ids)
    if not ids:
        return []
    text, encoding = read_text_with_encoding(vis_file)
    removed: list[str] = []

    def drop(match: re.Match[str]) -> str:
        if match.group("id") in ids:
            removed.append(match.group("id"))
            return ""
        return match.group(0)

    text = re.sub(rf'^[ \t]*<{tag}\b[^>]*\brefId="(?P<id>[^"]*)"[^>]*/>[ \t]*\r?\n?', drop, text, flags=re.MULTILINE)
    if removed:
        vis_file.write_text(text, encoding=encoding, newline="")
    return removed


def mappview_binding_files(cpu: Path) -> list[Path]:
    """Return the bindings and event bindings files of the mappView configuration of ``cpu``."""
    mappview = child_folder(cpu, "mappView")
    if mappview is None:
        return []
    return sorted((path for path in mappview.rglob("*") if path.is_file()
                   and path.suffix.casefold() in (".binding", ".eventbinding")), key=lambda path: str(path).casefold())


def dangling_reference(element: str, index: MappViewIndex, widgets: Callable[[str], set[str]]) -> bool:
    """True if a (event) binding refers to a content, widget, page or dialog that does not exist."""
    for tag in MV_REFERENCE_TAG_RE.finditer(element):
        content, widget = xml_attribute(tag.group(0), "contentRefId"), xml_attribute(tag.group(0), "widgetRefId")
        if content not in index.contents or (widget and widget not in widgets(content)):
            return True
    return (any(page not in index.pages for page in re.findall(r'\bpageId="([^"]*)"', element))
            or any(dialog not in index.dialogs for dialog in re.findall(r'\bdialogId="([^"]*)"', element)))


def content_widgets(index: MappViewIndex) -> Callable[[str], set[str]]:
    """Return a function giving the widget ids of a content (read once per content)."""
    cache: dict[str, set[str]] = {}

    def widgets(content: str) -> set[str]:
        if content not in cache:
            cache[content] = set(MV_WIDGET_ID_RE.findall(read_text(index.contents[content])))
        return cache[content]
    return widgets


def prune_mappview(new_logical: Path, cpu: Path, vis_file: Path) -> tuple[int, list[Path]]:
    """Remove (event) bindings and .vis entries that refer to pages, contents or widgets that do not exist.

    A bindings file that loses all its bindings is deleted together with its
    package and .vis entries.  Returns the number of removed bindings and the deleted files.
    """
    index = mappview_index(new_logical)
    widgets = content_widgets(index)
    removed, deleted = 0, []
    for path in mappview_binding_files(cpu):
        text, encoding = read_text_with_encoding(path)
        event = path.suffix.casefold() == ".eventbinding"
        elements = list((MV_EVENT_BINDING_RE if event else MV_BINDING_RE).finditer(text))
        dangling = [match for match in elements if dangling_reference(match.group(0), index, widgets)]
        if not dangling:
            continue
        removed += len(dangling)
        if len(dangling) == len(elements):
            set_id = MV_SET_ID_RE.search(text)
            remove_logical_object(path)
            if set_id:
                remove_vis_entries(vis_file, "EventBindingsSet" if event else "BindingsSet", [set_id.group("id")])
            deleted.append(path)
            continue
        for match in reversed(dangling):
            text = text[:match.start()] + text[match.end():]
        path.write_text(text, encoding=encoding, newline="")
    vis_text = read_text(vis_file)
    for tag, existing in (("Page", index.pages), ("Content", index.contents), ("Dialog", index.dialogs)):
        remove_vis_entries(vis_file, tag, [ref for ref, _ in vis_entries(vis_text, tag) if ref not in existing])
    return removed, deleted


def remove_opcua_entries(cpu: Path, variables: set[str], tasks: set[str]) -> int:
    """Remove the global variables and tasks (casefolded names) from every .uad file of ``cpu``."""
    count = 0
    for path in sorted(cpu.rglob("*.uad")):
        text, encoding = read_text_with_encoding(path)
        section = UA_GLOBAL_VARIABLES_RE.search(text)
        if section and section.group("body"):
            body = section.group("body")
            for start, end, name in reversed(ua_top_level_variables(body)):
                if name.casefold() in variables:
                    body = body[:start] + body[end:]
                    count += 1
            text = text[:section.start("body")] + body + text[section.end("body"):]
        new_text = UA_TASK_RE.sub(lambda match: "" if match.group("name").casefold() in tasks else match.group(0), text)
        count += new_text != text
        path.write_text(new_text, encoding=encoding, newline="")
    return count


def remove_base_samples(new_root: Path, chooser: FileChooser | None = None) -> MigrationResult:
    """Remove the examples (BkV_Sample program and pages) and test pages from the new base project."""
    new_root = new_root.resolve()
    logical = logical_dir(new_root)
    cpu = configuration_cpu(active_configuration(project_root(new_root), chooser, ask=True))
    vis_file = find_vis_file(cpu, chooser)
    result = MigrationResult()
    index = mappview_index(logical)
    pages = [page for page in index.pages if BKNG_SAMPLE_RE.match(page) or page.casefold() in BKNG_TEST_PAGES]
    for page in pages:
        path = index.pages[page]
        folder = path.parent
        contents = [index.contents[content] for content in MV_CONTENT_ASSIGNMENT_RE.findall(read_text(path))
                    if content in index.contents and index.contents[content].parent == folder]
        if {child.name.casefold() for child in folder.iterdir()} <= {
                PACKAGE_FILE.casefold(), path.name.casefold(), *(content.name.casefold() for content in contents)}:
            remove_logical_object(folder)
        else:
            for file in (path, *contents):
                remove_logical_object(file)
        remove_empty_packages(folder.parent, logical)
    # Navigation buttons to the removed pages.
    for content in mappview_index(logical).contents.values():
        text, encoding = read_text_with_encoding(content)
        new_text = re.sub(r'^[ \t]*<Widget\b[^>]*\bpageId="(?:' + "|".join(map(re.escape, pages)) + r')"[^>]*/>[ \t]*\r?\n?',
                          "", text, flags=re.MULTILINE) if pages else text
        if new_text != text:
            content.write_text(new_text, encoding=encoding, newline="")

    programs = [folder for folder in logical_programs(logical).values() if BKNG_SAMPLE_RE.match(folder.name)]
    variables: set[str] = set()
    for folder in programs:
        for object_type, child in package_objects(folder.parent / PACKAGE_FILE):
            file = folder.parent / child
            if object_type.casefold() == "file" and BKNG_SAMPLE_RE.match(file.stem) and file.is_file():
                if file.suffix.casefold() == ".var":
                    variables.update(entry.casefold() for _, names, _ in iec_entries(read_text(file), "var")
                                     for entry in names)
                remove_logical_object(file)
        remove_logical_object(folder)
        sw = cpu / "Cpu.sw"
        if sw.exists():
            text, encoding = read_text_with_encoding(sw)
            text = re.sub(rf'^[ \t]*<Task\b[^>]*\bName="{re.escape(folder.name)}"[^>]*/>[ \t]*\r?\n?', "", text,
                          flags=re.MULTILINE | re.IGNORECASE)
            sw.write_text(text, encoding=encoding, newline="")
    remove_opcua_entries(cpu, variables, {folder.name.casefold() for folder in programs})
    removed, deleted = prune_mappview(logical, cpu, vis_file)
    result.details = tr("cleanup_summary", pages=", ".join(pages) or "-",
                        programs=", ".join(folder.name for folder in programs) or "-", bindings=removed,
                        files=", ".join(path.name for path in deleted) or "-")
    return result


# --- Application: tasks, data modules, libraries ---

def sw_sources(sw_text: str, element_re: re.Pattern[str]) -> list[tuple[str, Path, str]]:
    """Return (name, folder relative to Logical, element) of the Cpu.sw entries matched by ``element_re``."""
    entries = []
    for match in element_re.finditer(sw_text):
        element = match.group(0).strip()
        source = xml_attribute(element, "Source")
        if "." in source:
            entries.append((xml_attribute(element, "Name"), Path(*source.split(".")[:-1]), element))
    return entries


def migrate_application(old_root: Path, new_root: Path, chooser: FileChooser | None = None,
                        convert_ladder: bool = False) -> MigrationResult:
    """Copy the tasks and data modules deployed in the old Cpu.sw (except the BkNG base) and deploy them alike.

    Files of the packages above the tasks (e.g. package-wide .var files) are copied
    as well.  Objects directly below Logical that are neither taken over nor part of
    the BkNG base are reported.
    """
    old_root, new_root = old_root.resolve(), new_root.resolve()
    old_logical, new_logical = logical_dir(old_root), logical_dir(new_root)
    old_sw = configuration_cpu(active_configuration(project_root(old_root), chooser)) / "Cpu.sw"
    if not old_sw.exists():
        raise ConversionError(tr("not_found", filename="Cpu.sw", root=old_sw.parent))
    sw_text = read_text(old_sw)
    result = MigrationResult()
    new_programs = logical_programs(new_logical)
    tasks: list[Path] = []
    ptop: list[str] = []  # PtoP tasks, which are not taken over
    for name, relative, _ in sw_sources(sw_text, SW_TASK_RE):
        if name.casefold() == VISU_TASK.casefold():
            continue
        if is_ptop_name(name) or is_ptop_object(relative):
            ptop.append(name)
        elif not (old_logical / relative).is_dir():
            result.warnings.append(tr("warn_task_source_missing", task=name, path=relative))
        elif name.casefold() in new_programs:
            result.warnings.append(tr("warn_program_in_base", task=name))
        else:
            tasks.append(relative)
    packages_updated: list[Path] = []
    files = 0
    for relative in tasks:
        files += copy_logical_object(old_logical, new_logical, relative, "Program", packages_updated,
                                     keep_old_header=True)[0]
        # Files of the packages above the task, e.g. a package-wide .var file.
        for depth in range(1, len(relative.parts)):
            package = Path(*relative.parts[:depth])
            for object_type, child in package_objects(old_logical / package / PACKAGE_FILE):
                if object_type.casefold() == "file" and (old_logical / package / child).is_file() \
                        and not (new_logical / package / child).exists():
                    copy_logical_file(old_logical, new_logical, package / child, packages_updated)
                    files += 1
    deployed, warnings = deploy_tasks(old_root, new_root, tasks, chooser)
    result.warnings += warnings
    result.details += tr("application_tasks", tasks=len(tasks), files=files, deployed=len(deployed)) + "".join(
        f"- {task}\n" for task in tasks)
    ladder_details, ladder_warnings = ladder_upgrade_report(
        upgrade_old_ladder_files(old_ladder_files(new_logical / task for task in tasks), new_root, convert_ladder))
    result.details += ladder_details
    result.warnings += ladder_warnings
    result.warnings += discontinued_function_warnings(new_logical, (new_logical / task for task in tasks))

    # Data modules are taken over, the PtoP data module ptop as well.
    data = [relative for _, relative, _ in sw_sources(sw_text, SW_DATA_OBJECT_RE)
            if relative.parts[0].casefold() != "system" and (old_logical / relative).is_dir()]
    data_result = copy_data_objects(old_root, new_root, chooser, data)
    result.warnings += data_result.warnings
    result.details += tr("application_data", objects=len(data), deployed=len(data_result.deployed)) + "".join(
        f"- {folder}\n" for folder in data)

    taken = {relative.parts[0].casefold() for relative in tasks + data}
    existing = {path.name.casefold() for path in new_logical.rglob("*")}
    root_files: list[str] = []
    for object_type, name in package_objects(old_logical / PACKAGE_FILE):
        path = old_logical / name
        if (name.casefold() in BKNG_BASE_LOGICAL_OBJECTS or name.casefold() in taken or is_ptop_name(name)
                or (path.is_file() and path.suffix.casefold() in (".var", ".typ"))):
            continue
        # Further files directly below Logical (e.g. Visu.url, SDM.url) are taken over unless the base has them.
        if object_type.casefold() == "file" and path.is_file():
            if child_path(new_logical, name) is None:
                copy_logical_file(old_logical, new_logical, Path(name), packages_updated)
                root_files.append(name)
            continue
        if name.casefold() in existing:
            continue
        if object_type.casefold() == "package" and not package_objects(path / PACKAGE_FILE):
            continue
        result.warnings.append(tr("warn_not_taken_over", name=name, type=object_type))
    if root_files:
        result.details += tr("application_files", files=", ".join(root_files))
    if ptop:
        result.details += tr("application_ptop", names=", ".join(ptop))
        if not find_ptop_files(old_logical):  # otherwise the analysis reports the PtoP coupling (warn_ptop)
            result.warnings.append(tr("warn_ptop_omitted", names=", ".join(ptop)))
    return result


def migrate_libraries(old_root: Path, new_root: Path, chooser: FileChooser | None = None) -> MigrationResult:
    """Copy the customer libraries with source code into the new project and deploy them.

    Burklimat standard libraries (bkfbklib, bkuselib, bktools, ...) come from the
    base project; B&R libraries missing in it and customer libraries without
    source code are reported.
    """
    old_root, new_root = old_root.resolve(), new_root.resolve()
    old_logical, new_logical = logical_dir(old_root), logical_dir(new_root)
    old_sw = configuration_cpu(active_configuration(project_root(old_root), chooser)) / "Cpu.sw"
    new_sw = configuration_cpu(active_configuration(project_root(new_root), chooser, ask=True)) / "Cpu.sw"
    text, encoding = read_text_with_encoding(new_sw)
    present = {xml_attribute(match.group(0), "Name").casefold() for match in SW_LIBRARY_RE.finditer(text)}
    result = MigrationResult()
    copied, missing, binary, ptop = [], [], [], []
    library_folders: list[Path] = []
    packages_updated: list[Path] = []
    for match in SW_LIBRARY_RE.finditer(read_text(old_sw)):
        element = match.group(0)
        name, language, source = (xml_attribute(element, key) for key in ("Name", "Language", "Source"))
        if name.casefold() in BK_STANDARD_LIBRARIES or name.casefold() in present or not source:
            continue
        if is_ptop_name(name) or is_ptop_object(Path(*source.split(".")[:-1])):
            ptop.append(name)
            continue
        if is_standard_library(name, language):
            missing.append(name)
            continue
        relative = Path(*source.split(".")[:-1])
        folder = old_logical / relative
        if not folder.is_dir() or not any(path.suffix.casefold() in LIBRARY_SOURCE_SUFFIXES for path in folder.rglob("*")):
            binary.append(name)
            continue
        copy_logical_object(old_logical, new_logical, relative, "Library", packages_updated, keep_old_header=True)
        encrypted = [path for path in folder.rglob("*")
                     if path.suffix.casefold() in LIBRARY_SOURCE_SUFFIXES and is_encrypted_source(path)]
        if encrypted:
            result.warnings.append(tr("warn_library_encrypted", name=name, count=len(encrypted)))
        closing = re.search(r"^([ \t]*)</Libraries>", text, re.MULTILINE)
        if closing is None:
            result.warnings.append(tr("warn_library_not_deployed", name=name, path=new_sw))
        else:
            newline = "\r\n" if "\r\n" in text else "\n"
            text = text[:closing.start()] + f"{closing.group(1)}  {element.strip()}{newline}" + text[closing.start():]
        copied.append(name)
        library_folders.append(new_logical / relative)
    if copied:
        new_sw.write_text(text, encoding=encoding, newline="")
    result.warnings += discontinued_function_warnings(new_logical, library_folders)
    result.details = tr("libraries_summary", copied=", ".join(copied) or "-")
    if ptop:
        result.details += tr("libraries_ptop", names=", ".join(ptop))
    result.warnings += missing_library_warnings(missing)
    if binary:
        result.warnings.append(tr("warn_library_without_source", names=", ".join(binary)))
    return result


# --- Users and roles ---

def config_file(cpu: Path, suffix: str) -> Path | None:
    files = sorted((path for path in cpu.rglob("*" + suffix) if path.is_file()), key=lambda path: str(path).casefold())
    return files[0] if files else None


def insert_before_closing(text: str, tag: str, entries: list[str]) -> str:
    """Insert ``entries`` (one or more lines each) before the closing tag ``</tag>`` (or open <tag />)."""
    newline = "\r\n" if "\r\n" in text else "\n"
    closing = re.search(rf"^([ \t]*)</{tag}>", text, re.MULTILINE)
    if closing is None:
        empty = re.search(rf"^([ \t]*)<{tag}\b([^>]*?)\s*/>", text, re.MULTILINE)
        if empty is None:
            raise ConversionError(tr("invalid_mappview_file", section=tag, path=tag))
        text = (text[:empty.start()] + f"{empty.group(1)}<{tag}{empty.group(2)}>{newline}{empty.group(1)}</{tag}>"
                + text[empty.end():])
        closing = re.search(rf"^([ \t]*)</{tag}>", text, re.MULTILINE)
    block = "".join(entry.replace("\r\n", "\n").replace("\n", newline) for entry in entries)
    return text[:closing.start()] + block + text[closing.start():]


def add_mpuserx_entries(path: Path, group: str, entries: list[list[tuple[str, str | None]]]) -> bool:
    """Add groups DefUser#n / DefRole#n (with the given properties) after the last existing one."""
    text, encoding = read_text_with_encoding(path)
    groups = list(re.finditer(rf'^(?P<indent>[ \t]*)<Group ID="{group}#(?P<n>\d+)">.*?^(?P=indent)</Group>[ \t]*\r?\n?',
                              text, re.DOTALL | re.MULTILINE))
    if not groups:
        return False
    newline = "\r\n" if "\r\n" in text else "\n"
    indent = groups[-1].group("indent")
    number = max(int(match.group("n")) for match in groups)
    block = ""
    for properties in entries:
        number += 1
        block += f'{indent}<Group ID="{group}#{number}">{newline}' + "".join(
            f'{indent}  <Property ID="{key}" Value="{value}" />{newline}' if value is not None
            else f'{indent}  <Property ID="{key}" />{newline}' for key, value in properties) + f"{indent}</Group>{newline}"
    end = groups[-1].end()
    if not groups[-1].group(0).endswith("\n"):
        block = newline + block.rstrip("\r\n")
    path.write_text(text[:end] + block + text[end:], encoding=encoding, newline="")
    return True


def migrate_users(old_root: Path, new_root: Path, chooser: FileChooser | None = None) -> MigrationResult:
    """Take over the users and roles of the old PLC that the new base project lacks.

    Passwords cannot be taken over (AS6 stores them as Argon2 hash); the users are
    created without password and reported.  The users and roles are also entered
    in the mapp user management (.mpuserx) if the base project has one.
    """
    old_cpu = configuration_cpu(active_configuration(project_root(old_root.resolve()), chooser))
    new_cpu = configuration_cpu(active_configuration(project_root(new_root.resolve()), chooser, ask=True))
    result = MigrationResult()
    old_users, old_roles = config_file(old_cpu, ".user"), config_file(old_cpu, ".role")
    if old_users is None and old_roles is None:
        result.details = tr("users_none")
        return result
    new_users, new_roles = config_file(new_cpu, ".user"), config_file(new_cpu, ".role")
    if new_users is None or new_roles is None:
        raise ConversionError(tr("not_found", filename="User.user / Role.role", root=new_cpu))
    newline = "\r\n"

    roles_text, roles_encoding = read_text_with_encoding(new_roles)
    present_roles = {match.group("id") for match in CONFIG_ELEMENT_RE.finditer(roles_text)}
    roles = [match.group("id") for match in CONFIG_ELEMENT_RE.finditer(read_text(old_roles))] if old_roles else []
    added_roles = [role for role in roles if role not in present_roles]
    if added_roles:
        roles_text = insert_before_closing(roles_text, "Configuration",
                                           [f'  <Element ID="{role}" Type="Role" />{newline}' for role in added_roles])
        write_like(new_roles, roles_text, roles_encoding)

    users_text, users_encoding = read_text_with_encoding(new_users)
    present_users = {match.group("id") for match in CONFIG_ELEMENT_RE.finditer(users_text)}
    added_users, kept_users = [], []
    entries = []
    for match in CONFIG_ELEMENT_RE.finditer(read_text(old_users)) if old_users else []:
        user = match.group("id")
        if user in present_users:
            kept_users.append(user)
            continue
        user_roles = USER_ROLE_RE.findall(match.group(0))
        missing_roles = [role for role in user_roles if role not in present_roles and role not in added_roles]
        if missing_roles:
            result.warnings.append(tr("warn_user_role_missing", user=user, roles=", ".join(missing_roles)))
        entries.append(f'  <Element ID="{user}" Type="User">{newline}    <Group ID="Roles">{newline}'
                       + "".join(f'      <Property ID="Role[{number}]" Value="{role}" />{newline}'
                                 for number, role in enumerate(user_roles, 1))
                       + f"    </Group>{newline}  </Element>{newline}")
        added_users.append(user)
    if entries:
        write_like(new_users, insert_before_closing(users_text, "Configuration", entries), users_encoding)

    mpuserx = config_file(new_cpu, ".mpuserx")
    if mpuserx is not None:
        text = read_text(mpuserx)
        roles_in_mapp = set(re.findall(r'<Group ID="DefRole#\d+">\s*<Property ID="id" Value="([^"]*)"', text))
        users_in_mapp = set(re.findall(r'<Group ID="DefUser#\d+">\s*<Property ID="id" Value="([^"]*)"', text))
        mapp_roles = [role for role in roles if role not in roles_in_mapp]
        mapp_users = [user for user in added_users if user not in users_in_mapp]
        if mapp_roles and not add_mpuserx_entries(mpuserx, "DefRole", [
                [("id", role), ("level", "10"), ("admin", "FALSE")] for role in mapp_roles]):
            result.warnings.append(tr("warn_mpuserx", path=mpuserx))
        if mapp_users and not add_mpuserx_entries(mpuserx, "DefUser", [
                [("id", user), ("name", None), ("lang", None), ("measSystem", "0")] for user in mapp_users]):
            result.warnings.append(tr("warn_mpuserx", path=mpuserx))

    result.details = tr("users_summary", roles=", ".join(added_roles) or "-", users=", ".join(added_users) or "-",
                        kept=", ".join(kept_users) or "-", path=new_users.parent)
    if added_users:
        result.warnings.append(tr("warn_user_passwords", users=", ".join(added_users), path=new_users))
    return result


# --- OPC UA ---

def opcua_enabled(cpu: Path) -> tuple[set[str], dict[str, set[str]]]:
    """Return the enabled global variables and the enabled variables of each task (casefolded) of all .uad files."""
    variables: set[str] = set()
    tasks: dict[str, set[str]] = {}
    for path in cpu.rglob("*.uad"):
        text = read_text(path)
        section = UA_GLOBAL_VARIABLES_RE.search(text)
        if section and section.group("body"):
            variables.update(name.casefold() for _, _, name in ua_top_level_variables(section.group("body")))
        for task in UA_TASK_RE.finditer(text):
            tasks.setdefault(task.group("name").casefold(), set()).update(
                name.casefold() for _, _, name in ua_top_level_variables(task.group("body") or ""))
    return variables, tasks


def add_opcua_entries(target: Path, variables: list[str], tasks: list[tuple[str, list[str]]]) -> None:
    """Enable global variables and task variables (recursively, AS6 format) in ``target``."""
    if not variables and not tasks:
        return
    text, encoding = read_text_with_encoding(target)
    newline = "\r\n" if "\r\n" in text else "\n"
    if variables:
        section = UA_GLOBAL_VARIABLES_RE.search(text)
        if section is None or section.group("body") is None:
            raise ConversionError(tr("invalid_opcua", path=target))
        indent = section.group("indent") + "  "
        closing = text.rfind("\n", 0, section.end() - len("</GlobalVariables>")) + 1
        text = text[:closing] + "".join(f'{indent}  <Variable Name="{name}" RecursiveEnable="1" />{newline}'
                                        for name in variables) + text[closing:]
    if tasks:
        module = re.search(r"^([ \t]*)</Module>", text, re.MULTILINE)
        if module is None:
            raise ConversionError(tr("invalid_opcua", path=target))
        indent = module.group(1) + "  "
        block = "".join(f'{indent}<Task Name="{task}">{newline}'
                        + "".join(f'{indent}  <Variable Name="{name}" RecursiveEnable="1" />{newline}' for name in names)
                        + f"{indent}</Task>{newline}" for task, names in tasks)
        text = text[:module.start()] + block + text[module.start():]
    target.write_text(text, encoding=encoding, newline="")


def migrate_opcua(old_root: Path, new_root: Path, global_var: Path, chooser: FileChooser | None = None) -> MigrationResult:
    """Enable the Bk datapoints and all further variables that were enabled in the old project in OPC UA."""
    old_root, new_root = old_root.resolve(), new_root.resolve()
    bk = enable_opcua_variables(new_root, global_var, chooser)
    result = MigrationResult(tr("opcua_summary", count=len(bk.variables), replaced=bk.replaced, path=bk.target),
                             list(bk.warnings))
    old_cpu = configuration_cpu(active_configuration(project_root(old_root), chooser))
    new_cpu = bk.target.parents[0]
    while new_cpu.parent != new_cpu and not (new_cpu / "Cpu.sw").exists():
        new_cpu = new_cpu.parent
    enabled, enabled_tasks = opcua_enabled(new_cpu)
    declared = global_variable_names(logical_dir(new_root))
    programs = logical_programs(logical_dir(new_root))
    variables: list[str] = []
    undeclared: list[str] = []
    tasks: list[tuple[str, list[str]]] = []
    # Files of the BkNG base (e.g. OpcUaBkNG.uad) exist in the new project, too; their
    # variables that the new base no longer declares are left out silently.
    base_files = {path.name.casefold() for path in new_cpu.rglob("*.uad")} - {OPCUA_FILE.casefold()}
    for path in sorted(old_cpu.rglob("*.uad")):
        text = read_text(path)
        section = UA_GLOBAL_VARIABLES_RE.search(text)
        for _, _, name in ua_top_level_variables(section.group("body") or "") if section else []:
            if name.casefold() in enabled or name in variables or name in undeclared:
                continue
            if name.casefold() in declared:
                variables.append(name)
            elif path.name.casefold() not in base_files:
                undeclared.append(name)
        for task in UA_TASK_RE.finditer(text):
            name = task.group("name")
            if name.casefold() in programs and name.casefold() not in enabled_tasks:
                tasks.append((name, [variable for _, _, variable in ua_top_level_variables(task.group("body") or "")]))
    add_opcua_entries(bk.target, variables, tasks)
    if variables or tasks:
        result.details += tr("opcua_further", variables=", ".join(variables) or "-",
                             tasks=", ".join(task for task, _ in tasks) or "-")
    if undeclared:
        result.warnings.append(tr("warn_opcua_undeclared", names=", ".join(undeclared)))
    return result


# --- Customer visualisation (BkVisu pages) ---

def compound_widgets(logical: Path) -> dict[str, tuple[str, set[str]]]:
    """Return the type and property names (casefolded) of every compound widget by casefolded widget type."""
    widgets: dict[str, tuple[str, set[str]]] = {}
    folder = child_folder(logical, "mappView")
    folder = child_folder(folder, "Widgets") if folder else None
    if folder is None:
        return widgets
    for path in sorted(folder.rglob("*.compoundwidget")):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            continue
        library = path.relative_to(folder).parts[0]
        widget_type = f"widgets.{library}.{root.get('id', '')}"
        properties = {element.get("name", "").casefold() for element in root.iter()
                      if element.tag.endswith("Property") and element.get("name")}
        widgets[widget_type.casefold()] = (widget_type, properties)
    return widgets


def with_guid(text: str, guid: str | None) -> str:
    """Give the <Content> element of a content file the attribute guid (AS6), keeping an existing one."""
    match = re.search(r"<Content\b[^>]*>", text)
    if match is None or re.search(r'\sguid="', match.group(0)):
        return text
    guid = guid or str(uuid.uuid4())
    return re.sub(r'(<Content\b[^>]*?\sid="[^"]*")', rf'\g<1> guid="{guid}"', text, count=1)


def write_mappview_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig", newline="")


def check_bkvisu_widgets(path: Path, widgets: dict[str, tuple[str, set[str]]], warnings: list[str]) -> None:
    """Check the compound widgets of a content against the widget libraries of the new project.

    Widget types written in another case or renamed in the new BkVisuLib are
    corrected; unknown widgets and attributes that are no properties are reported.
    """
    text = read_text(path)
    content = path.stem
    unknown_attributes: dict[str, set[str]] = {}
    changed = text
    for tag in MV_WIDGET_TAG_RE.finditer(text):
        widget_type = xml_attribute(tag.group(0), "xsi:type")
        if not widget_type or widget_type.startswith("widgets.brease."):
            continue
        known = widgets.get(widget_type.casefold())
        if known is None:
            library, _, name = widget_type.rpartition(".")
            renamed = BKVISU_WIDGET_RENAMES.get(name.casefold())
            known = widgets.get(f"{library}.{renamed}".casefold()) if renamed else None
            if known is None:
                warnings.append(tr("warn_widget_missing", content=content, widget=xml_attribute(tag.group(0), "id"),
                                   type=widget_type))
                continue
            warnings.append(tr("warn_widget_renamed", content=content, widget=xml_attribute(tag.group(0), "id"),
                               old=widget_type, new=known[0]))
        if known[0] != widget_type:
            changed = changed.replace(f'xsi:type="{widget_type}"', f'xsi:type="{known[0]}"')
        for attribute in MV_ATTRIBUTE_RE.finditer(tag.group(0)):
            name = attribute.group("name")
            if name.casefold() not in MV_COMMON_WIDGET_ATTRIBUTES and name.casefold() not in known[1]:
                unknown_attributes.setdefault(known[0], set()).add(name)
    for widget_type, names in unknown_attributes.items():
        warnings.append(tr("warn_widget_attributes", content=content, type=widget_type, names=", ".join(sorted(names))))
    if changed != text:
        write_mappview_file(path, changed)


def find_media(logical: Path, reference: str) -> Path | None:
    """Return the file of a reference 'Media/...' below the mappView folder of ``logical``."""
    suffix = "/" + reference.casefold()
    mappview = child_folder(logical, "mappView")
    if mappview is None:
        return None
    for path in sorted(mappview.rglob("*"), key=lambda path: str(path).casefold()):
        if path.is_file() and path.as_posix().casefold().endswith(suffix):
            return path
    return None


def tmx_files(logical: Path) -> list[tuple[Path, str]]:
    """Return every text module (.tmx) of the mappView folder with its namespace."""
    mappview = child_folder(logical, "mappView")
    files = []
    for path in sorted(mappview.rglob("*.tmx"), key=lambda path: str(path).casefold()) if mappview else []:
        namespace = TMX_NAMESPACE_RE.search(read_text(path))
        files.append((path, namespace.group(1) if namespace else ""))
    return files


def migrate_texts(old_logical: Path, new_logical: Path, references: set[tuple[str, str]],
                  warnings: list[str]) -> list[str]:
    """Copy the text units ($namespace/key) the new project lacks from the old text modules. Returns the keys."""
    new_files = tmx_files(new_logical)
    present = {(namespace, match.group("id")) for path, namespace in new_files
               for match in TMX_UNIT_RE.finditer(read_text(path))}
    old_units = {}
    for path, namespace in tmx_files(old_logical):
        for match in TMX_UNIT_RE.finditer(read_text(path)):
            old_units.setdefault((namespace, match.group("id")), match.group(0))
    added: list[str] = []
    for namespace, key in sorted(references - present):
        unit = old_units.get((namespace, key))
        target = next((path for path, file_namespace in new_files if file_namespace == namespace), None)
        if unit is None or target is None:
            warnings.append(tr("warn_text_missing", text=f"${namespace}/{key}"))
            continue
        text, encoding = read_text_with_encoding(target)
        newline = "\r\n" if "\r\n" in text else "\n"
        closing = re.search(r"^[ \t]*</body>", text, re.MULTILINE)
        if closing is None:
            warnings.append(tr("warn_text_missing", text=f"${namespace}/{key}"))
            continue
        unit = unit.replace("\r\n", "\n").replace("\n", newline)
        write_like(target, text[:closing.start()] + unit + (newline if not unit.endswith(newline) else "")
                   + text[closing.start():], encoding)
        added.append(f"${namespace}/{key}")
    return added


def migrate_session_variables(old_logical: Path, new_logical: Path, names: set[str], warnings: list[str]) -> list[str]:
    """Copy the session variables the new project lacks from the old *.svar file. Returns their names."""
    new_files = sorted(new_logical.rglob("*.svar"), key=lambda path: (len(path.parts), str(path).casefold()))
    if not names or not new_files:
        return []
    new_text, encoding = read_text_with_encoding(new_files[0])
    present = set(re.findall(r'<Variable\b[^>]*\bname="([^"]*)"', new_text))
    old_variables = {}
    for path in old_logical.rglob("*.svar"):
        for match in re.finditer(r'<Variable\b[^>]*\bname="([^"]*)"[^>]*/>', read_text(path)):
            old_variables.setdefault(match.group(1), match.group(0))
    added = []
    for name in sorted(names - present):
        if name not in old_variables:
            warnings.append(tr("warn_session_variable_missing", name=name))
            continue
        new_text = insert_before_closing(new_text, "Variables", [f"    {old_variables[name]}\n"])
        added.append(name)
    if added:
        write_like(new_files[0], new_text, encoding)
    return added


def migrate_visu(old_root: Path, new_root: Path, chooser: FileChooser | None = None) -> MigrationResult:
    """Take over the customer-specific mappView visualisation of the old BkNG project.

    The customer pages (BkVisu pages and further pages unknown to the base
    project) with their contents and dialogs are copied; the BkVisu contents the
    base project has as well (start page, navigation) are replaced by the old
    ones.  Media and texts they use are copied if missing.  Every binding and
    event binding of the old project that refers to one of these contents is
    taken over into an own bindings set, and the .vis file is completed.
    """
    old_root, new_root = old_root.resolve(), new_root.resolve()
    old_logical, new_logical = logical_dir(old_root), logical_dir(new_root)
    old_cpu = configuration_cpu(active_configuration(project_root(old_root), chooser))
    new_cpu = configuration_cpu(active_configuration(project_root(new_root), chooser, ask=True))
    old_vis, new_vis = find_vis_file(old_cpu, chooser), find_vis_file(new_cpu, chooser)
    old_index, new_index = mappview_index(old_logical), mappview_index(new_logical)
    old_vis_text = read_text(old_vis)
    result = MigrationResult()
    warnings = result.warnings
    bkvisu = old_logical
    for part in MAPPVIEW_BKVISU_PAGES:
        bkvisu = child_folder(bkvisu, part) if bkvisu else None

    def in_bkvisu(path: Path) -> bool:
        return bkvisu is not None and bkvisu in path.parents

    def ignored(page: str) -> bool:
        return page.casefold() in BKNG_TEST_PAGES or BKNG_SAMPLE_RE.match(page) is not None

    vis_pages = [page for page, _ in vis_entries(old_vis_text, "Page")]
    candidates = vis_pages + [page for page, path in old_index.pages.items() if in_bkvisu(path)]
    pages = [page for page in dict.fromkeys(candidates)
             if page in old_index.pages and page not in new_index.pages and not ignored(page)]
    for page in vis_pages:
        if page not in old_index.pages:
            warnings.append(tr("warn_page_missing", page=page, path=old_vis))
    dialogs = [dialog for dialog, _ in vis_entries(old_vis_text, "Dialog")
               if dialog in old_index.dialogs and dialog not in new_index.dialogs]
    replaced = [content for content, path in old_index.contents.items()
                if in_bkvisu(path) and content in new_index.contents]
    contents: list[str] = []
    for path in [old_index.pages[page] for page in pages] + [old_index.dialogs[dialog] for dialog in dialogs]:
        for content in MV_CONTENT_ASSIGNMENT_RE.findall(read_text(path)):
            if content in new_index.contents or content in contents:
                continue
            if content in old_index.contents:
                contents.append(content)
            else:
                warnings.append(tr("warn_content_missing", content=content, page=path.stem))
    contents += [content for content, path in old_index.contents.items()
                 if in_bkvisu(path) and content not in new_index.contents and content not in contents]
    contents += [content for content, _ in vis_entries(old_vis_text, "Content")
                 if content in old_index.contents and content not in new_index.contents and content not in contents]

    # Custom widget libraries.
    packages_updated: list[Path] = []
    old_widgets = child_folder(child_folder(old_logical, "mappView") or old_logical, "Widgets")
    new_widgets = child_folder(child_folder(new_logical, "mappView") or new_logical, "Widgets")
    libraries = []
    if old_widgets and new_widgets:
        for folder in sorted(path for path in old_widgets.iterdir() if path.is_dir()):
            if child_folder(new_widgets, folder.name) is None:
                copy_logical_object(old_logical, new_logical, folder.relative_to(old_logical), "Package",
                                    packages_updated, keep_old_header=True)
                libraries.append(folder.name)

    # Pages, dialogs and contents.
    start_layout = None
    start_page = new_index.pages.get("BkV_Start")
    if start_page:
        match = re.search(r'\blayoutRefId="([^"]*)"', read_text(start_page))
        start_layout = match.group(1) if match else None
    start_areas = (set(MV_AREA_ID_RE.findall(read_text(new_index.layouts[start_layout])))
                   if start_layout in new_index.layouts else set())
    copied_layouts: list[str] = []
    migrated_files: list[Path] = []
    for object_id, path, is_page in [(page, old_index.pages[page], True) for page in pages] + [
            (dialog, old_index.dialogs[dialog], False) for dialog in dialogs]:
        folder = path.parent
        relative = path.relative_to(old_logical)
        if folder != bkvisu and len(list(folder.glob("*.page")) + list(folder.glob("*.dialog"))) == 1:
            copy_logical_object(old_logical, new_logical, folder.relative_to(old_logical), "Package",
                                packages_updated, keep_old_header=True)
        else:
            copy_logical_file(old_logical, new_logical, relative, packages_updated)
        target = new_logical / relative
        text = read_text(target)
        layout = re.search(r'\blayoutRefId="([^"]*)"', text)
        if layout and layout.group(1) not in new_index.layouts and layout.group(1) not in copied_layouts:
            # The layout of the BkNG start page replaces a missing one only if it has all areas of the page
            # (e.g. not for BkNG_Layout_User with a navigation bar split into AreaNavigationLi/Re).
            if is_page and start_layout and set(MV_AREA_RE.findall(text)) <= start_areas:
                text = text.replace(layout.group(0), f'layoutRefId="{start_layout}"', 1)
                write_mappview_file(target, text)
                result.details += tr("visu_layout_replaced", page=object_id, old=layout.group(1), new=start_layout)
            elif layout.group(1) in old_index.layouts:
                copy_logical_file(old_logical, new_logical, old_index.layouts[layout.group(1)].relative_to(old_logical),
                                  packages_updated)
                copied_layouts.append(layout.group(1))
            else:
                warnings.append(tr("warn_layout_missing", page=object_id, layout=layout.group(1)))
        migrated_files.append(target)
    for content in contents:
        relative = old_index.contents[content].relative_to(old_logical)
        target = new_logical / relative
        if not target.exists():
            copy_logical_file(old_logical, new_logical, relative, packages_updated)
        write_mappview_file(target, with_guid(read_text(target), None))
        migrated_files.append(target)
    for content in replaced:
        target = new_index.contents[content]
        guid = re.search(r'<Content\b[^>]*?\sguid="([^"]*)"', read_text(target))
        write_mappview_file(target, with_guid(read_text(old_index.contents[content]), guid.group(1) if guid else None))
        migrated_files.append(target)
    migrated_contents = set(contents) | set(replaced)
    result.details += tr("visu_pages", pages=", ".join(pages) or "-", dialogs=", ".join(dialogs) or "-",
                         contents=", ".join(contents) or "-", replaced=", ".join(replaced) or "-")
    if libraries:
        result.details += tr("visu_widget_libraries", names=", ".join(libraries))
    if copied_layouts:
        result.details += tr("visu_layouts_copied", names=", ".join(copied_layouts))

    # Media, texts and widgets used by the migrated contents.
    content_texts = {path: read_text(path) for path in migrated_files}
    media: list[str] = []
    for reference in sorted({match.group(0).strip() for text in content_texts.values()
                             for match in MV_MEDIA_RE.finditer(text)}):
        if find_media(new_logical, reference):
            continue
        source = find_media(old_logical, reference)
        if source is None:
            warnings.append(tr("warn_media_missing", media=reference))
            continue
        copy_logical_file(old_logical, new_logical, source.relative_to(old_logical), packages_updated)
        media.append(reference)
    texts = migrate_texts(old_logical, new_logical, {(match.group("namespace"), match.group("key"))
                                                     for text in content_texts.values()
                                                     for match in MV_TEXT_REF_RE.finditer(text)}, warnings)
    if media or texts:
        result.details += tr("visu_resources", media=", ".join(media) or "-", texts=", ".join(texts) or "-")
    widgets = compound_widgets(new_logical)
    for path in migrated_files:
        if path.suffix.casefold() == ".content":
            check_bkvisu_widgets(path, widgets, warnings)

    # Bindings and event bindings of the migrated contents.
    removed_before, _ = prune_mappview(new_logical, new_cpu, new_vis)
    new_elements = {".binding": set(), ".eventbinding": set()}
    new_sources: set[str] = set()
    new_ids: set[str] = set()
    for path in mappview_binding_files(new_cpu):
        text = read_text(path)
        if path.suffix.casefold() == ".binding":
            new_elements[".binding"].update(" ".join(match.group(0).split()) for match in MV_BINDING_RE.finditer(text))
        else:
            for match in MV_EVENT_BINDING_RE.finditer(text):
                source = MV_SOURCE_RE.search(match.group(0))
                if source:
                    new_sources.add(" ".join(source.group(0).split()))
                new_ids.add(xml_attribute(match.group(0)[:match.group(0).index(">")], "id"))
    bindings, events, namespaces = [], [], {".binding": set(), ".eventbinding": set()}
    for path in mappview_binding_files(old_cpu):
        text = read_text(path)
        suffix = path.suffix.casefold()
        root = MV_SET_ID_RE.search(text)
        for match in (MV_BINDING_RE if suffix == ".binding" else MV_EVENT_BINDING_RE).finditer(text):
            element = match.group(0)
            if not any(xml_attribute(tag.group(0), "contentRefId") in migrated_contents
                       for tag in MV_REFERENCE_TAG_RE.finditer(element)):
                continue
            if suffix == ".binding":
                if " ".join(element.split()) in new_elements[".binding"]:
                    continue
                bindings.append(element)
            else:
                source = MV_SOURCE_RE.search(element)
                if source and " ".join(source.group(0).split()) in new_sources:
                    continue
                start_tag = element[:element.index(">")]
                event_id = xml_attribute(start_tag, "id")
                if event_id in new_ids:
                    element = element.replace(f'id="{event_id}"', f'id="{event_id}_BkV"', 1)
                    event_id += "_BkV"
                new_ids.add(event_id)
                events.append(element)
            if root:
                line_end_index = text.index(">", root.start())
                namespaces[suffix].update(MV_NAMESPACE_RE.findall(text[root.start():line_end_index]))
    binding_dir = new_vis.parent / MAPPVIEW_BINDINGS_PACKAGE
    vis_package = new_vis.parent / PACKAGE_FILE
    written = []
    for elements, set_id, suffix, head, namespace_defaults, tag in (
            (bindings, MIGRATED_BINDINGS_SET, ".binding",
             '﻿<?xml version="1.0" encoding="utf-8"?>\r\n<BindingsSet id="{id}"{namespaces}>\r\n\t<Bindings>\r\n',
             BINDINGS_NAMESPACES, "BindingsSet"),
            (events, MIGRATED_EVENT_BINDINGS_SET, ".eventbinding", EVENT_BINDINGS_SET_HEAD,
             EVENT_BINDINGS_NAMESPACES, "EventBindingsSet")):
        if not elements:
            continue
        declared = MV_NAMESPACE_RE.findall(namespace_defaults)
        prefixes = {item.split("=")[0] for item in declared}
        declared += sorted(item for item in namespaces[suffix] if item.split("=")[0] not in prefixes)
        body = "".join(("\t\t" + element.strip() + "\n").replace("\r\n", "\n").replace("\n", "\r\n")
                       for element in elements)
        closing = "\t</Bindings>\r\n</BindingsSet>" if suffix == ".binding" else "  </Bindings>\r\n</EventBindingSet>"
        path = binding_dir / f"{set_id}{suffix}"
        write_utf8(path, head.format(id=set_id, namespaces="".join(declared)) + body + closing)
        if not (binding_dir / PACKAGE_FILE).exists():
            write_utf8(binding_dir / PACKAGE_FILE, NEW_PACKAGE_TEMPLATE)
        add_xml_entry(binding_dir / PACKAGE_FILE, "Objects", f'<Object Type="File">{path.name}</Object>', path.name)
        if vis_package.exists():
            add_xml_entry(vis_package, "Objects", f'<Object Type="Package">{MAPPVIEW_BINDINGS_PACKAGE}</Object>',
                          MAPPVIEW_BINDINGS_PACKAGE)
        add_xml_entry(new_vis, tag + "s", f'<{tag} refId="{set_id}" />', set_id)
        written.append(path)

    # .vis entries of the pages, dialogs and contents.
    old_content_entries = dict(vis_entries(old_vis_text, "Content"))
    for page in pages:
        if page in vis_pages:
            add_xml_entry(new_vis, "Pages", f'<Page refId="{page}" />', page)
    for dialog in dialogs:
        add_xml_entry(new_vis, "Dialogs", f'<Dialog refId="{dialog}" />', dialog)
    for content in contents:
        if content in old_content_entries:
            add_xml_entry(new_vis, "Contents", old_content_entries[content], content)
    removed_after, deleted = prune_mappview(new_logical, new_cpu, new_vis)
    if removed_after:
        warnings.append(tr("warn_bindings_dangling", count=removed_after))
    result.details += tr("visu_bindings", bindings=len(bindings), events=len(events),
                         files=", ".join(str(path.relative_to(new_cpu)) for path in written if path.exists()) or "-",
                         removed=removed_before)

    # Session variables and PLC variables of the bindings.
    binding_texts = [read_text(path) for path in written if path.exists()]
    sessions = {xml_attribute(tag.group(0), "refId") for text in binding_texts for tag in MV_SESSION_TAG_RE.finditer(text)}
    added_sessions = migrate_session_variables(old_logical, new_logical, sessions, warnings)
    if added_sessions:
        result.details += tr("visu_session_variables", names=", ".join(added_sessions))
    declared = global_variable_names(new_logical)
    programs = logical_programs(new_logical)
    enabled, enabled_tasks = opcua_enabled(new_cpu)
    enable: list[str] = []
    for scope, name in sorted({(match.group("scope"), match.group("name"))
                               for text in binding_texts for match in MV_OPCUA_REF_RE.finditer(text)}):
        if scope == "AsGlobalPV":
            if name.casefold() not in declared:
                warnings.append(tr("warn_variable_undeclared", variable=f"::{scope}:{name}"))
            elif name.casefold() not in enabled and name not in enable:
                enable.append(name)
            continue
        folder = programs.get(scope.casefold())
        task_variables = {entry.casefold() for path in (folder.glob("*.var") if folder else [])
                          for _, names, _ in iec_entries(read_text(path), "var") for entry in names}
        if name.casefold() not in task_variables:
            warnings.append(tr("warn_variable_undeclared", variable=f"::{scope}:{name}"))
        elif name.casefold() not in enabled_tasks.get(scope.casefold(), set()):
            warnings.append(tr("warn_variable_not_enabled", variable=f"::{scope}:{name}"))
    if enable:
        add_opcua_entries(opcua_target(new_cpu, chooser), enable, [])
        result.details += tr("visu_opcua_enabled", names=", ".join(enable))
    return result


# --- Conversion report --------------------------------------------------------------------------

@dataclass
class ReportSection:
    title: str
    details: str
    # Warnings of the step; each one is an open item (to do) for the user.
    warnings: list[str] = field(default_factory=list)


@dataclass
class ConversionReport:
    old_project: Path
    new_project: Path
    created: datetime
    sections: list[ReportSection] = field(default_factory=list)
    # (step title, message) of the error that aborted the conversion.
    error: tuple[str, str] | None = None
    # Kind of conversion (Bk2000 conversion or BkNG migration), shown in the header.
    mode: str = ""

    @property
    def todos(self) -> int:
        return sum(len(section.warnings) for section in self.sections) + (self.error is not None)


def render_report(report: ConversionReport) -> str:
    """Return the report as plain text: the open items first, then the details of every step."""
    def indented(text: str) -> list[str]:
        return ["  " + line if line else "" for line in text.rstrip("\n").splitlines()]

    def heading(text: str, underline: str) -> list[str]:
        return [text, underline * len(text)]

    lines = heading(tr("report_title"), "=") + [
        f"{tr('report_date')}: {report.created:%d.%m.%Y %H:%M}",
        f"{tr('report_old')}: {report.old_project}",
        f"{tr('report_new')}: {report.new_project}",
        *([f"{tr('report_mode')}: {report.mode}"] if report.mode else []),
        "",
        tr("report_result_error", step=report.error[0]) if report.error else tr("report_result_ok", todos=report.todos),
        "",
    ]
    lines += heading(f"{tr('report_todo_heading')} ({report.todos})", "-")
    if report.error:
        lines += [report.error[0], f"  [ ] {tr('report_error_todo', error=report.error[1])}", ""]
    for section in report.sections:
        if section.warnings:
            lines += [section.title] + [f"  [ ] {warning}" for warning in section.warnings] + [""]
    if not report.todos:
        lines += [tr("report_no_todos"), ""]
    lines += heading(tr("report_steps_heading"), "-")
    for section in report.sections:
        lines += [section.title] + indented(section.details) + [""]
    return "\n".join(lines)


def write_report(report: ConversionReport, folder: Path) -> Path:
    """Save the report as text file in ``folder`` and return its path."""
    path = folder / f"{tr('report_file')}_{report.created:%Y-%m-%d_%H%M%S}.txt"
    # BOM and CRLF so that every Windows editor shows umlauts and line breaks correctly.
    path.write_text(render_report(report) + "\n", encoding="utf-8-sig", newline="\r\n")
    return path


class FileChoiceDialog(tk.Toplevel):
    """Modal dialog that lets the user pick one of several files by full path."""

    def __init__(self, master: tk.Misc, filename: str, root: Path, candidates: list[Path]) -> None:
        super().__init__(master)
        self.withdraw()  # shown once it is centred
        self.candidates = candidates
        self.result: Path | None = None
        # Hardware configurations are shown by name, other files by their full path.
        configuration = filename.casefold() == "hardware.hw"
        labels = [f"{path.parent.name}    ({path})" if configuration else str(path) for path in candidates]
        self.title(tr("config_title") if configuration else tr("choice_title", filename=filename))
        self.transient(master.winfo_toplevel())
        self.minsize(500, 250)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        ttk.Label(
            self, padding=(10, 10, 10, 6), justify="left",
            text=tr("config_prompt", root=root) if configuration else tr("choice_prompt", filename=filename, root=root),
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        width = min(max(len(label) for label in labels) + 2, 160)
        self.listbox = tk.Listbox(self, width=width, height=min(len(candidates), 12),
                                  activestyle="dotbox", exportselection=False)
        yscroll = ttk.Scrollbar(self, orient="vertical", command=self.listbox.yview)
        xscroll = ttk.Scrollbar(self, orient="horizontal", command=self.listbox.xview)
        self.listbox.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        for label in labels:
            self.listbox.insert(tk.END, label)
        self.listbox.selection_set(0)
        self.listbox.activate(0)
        self.listbox.grid(row=1, column=0, sticky="nsew", padx=(10, 0))
        yscroll.grid(row=1, column=1, sticky="ns", padx=(0, 10))
        xscroll.grid(row=2, column=0, sticky="ew", padx=(10, 0))

        buttons = ttk.Frame(self, padding=10)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text=tr("ok"), command=self._ok).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text=tr("cancel"), command=self.destroy).pack(side="left")

        self.listbox.bind("<Double-Button-1>", lambda _event: self._ok())
        self.bind("<Return>", lambda _event: self._ok())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self._center_on(master.winfo_toplevel())
        self.deiconify()
        self.listbox.focus_set()
        self.grab_set()
        self.wait_window()

    def _center_on(self, window: tk.Misc) -> None:
        self.update_idletasks()
        min_width, min_height = self.minsize()
        width, height = max(self.winfo_reqwidth(), min_width), max(self.winfo_reqheight(), min_height)
        # geometry() places the outer frame; subtract border and title bar (taken from the main window).
        x = window.winfo_rootx() + (window.winfo_width() - width) // 2 - (window.winfo_rootx() - window.winfo_x())
        y = window.winfo_rooty() + (window.winfo_height() - height) // 2 - (window.winfo_rooty() - window.winfo_y())
        # Keep the title bar on screen if the main window is near the screen edge.
        x = max(0, min(x, self.winfo_screenwidth() - width))
        y = max(0, min(y, self.winfo_screenheight() - height))
        self.geometry(f"+{x}+{y}")

    def _ok(self) -> None:
        selection = self.listbox.curselection()
        if selection:
            self.result = self.candidates[selection[0]]
            self.destroy()


PATH_LABEL_WIDTH = 26
# Interval in which the window takes over the calls of the conversion thread, and the spinner.
PROGRESS_POLL_MS = 50
PROGRESS_SPINNER = "|/-\\"
PROGRESS_BUSY_MS = 15


class ConverterApp(ttk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=14)
        self.old_path = tk.StringVar()
        self.new_path = tk.StringVar()
        # "project": convert into an existing BkNG project; "zip" (default): unpack a zipped base project first.
        self.new_mode = tk.StringVar(value="zip")
        self.zip_path = tk.StringVar()
        self.zip_target = tk.StringVar()
        # A zipped source project is unpacked once into a temporary folder that is
        # deleted on exit: (archive path, modification time, size), folder, .apj file.
        self.source_zip: tuple[tuple[Path, int, int], tempfile.TemporaryDirectory[str], Path] | None = None
        self.copy_ladder = tk.BooleanVar(value=True)
        self.copy_data = tk.BooleanVar(value=True)
        self.copy_io = tk.BooleanVar(value=True)
        self.copy_ethernet = tk.BooleanVar(value=True)
        self.enable_opcua = tk.BooleanVar(value=True)
        self.convert_visu = tk.BooleanVar(value=True)
        self.migrate_application = tk.BooleanVar(value=True)
        self.migrate_users = tk.BooleanVar(value=True)
        self.migrate_visu = tk.BooleanVar(value=True)
        # Off by default: it needs Automation Studio 6 and uses its internal functions.
        self.convert_ladder_as6 = tk.BooleanVar(value=False)
        # Options (text key, variable) offered for each kind of source project.
        self.options_by_kind = {
            PROJECT_KIND_BK2000: [("copy_ladder", self.copy_ladder), ("convert_ladder_as6", self.convert_ladder_as6),
                                  ("copy_data", self.copy_data), ("copy_io", self.copy_io),
                                  ("copy_ethernet", self.copy_ethernet), ("copy_opcua", self.enable_opcua),
                                  ("convert_visu", self.convert_visu)],
            PROJECT_KIND_BKNG: [("migrate_application", self.migrate_application),
                                ("convert_ladder_as6", self.convert_ladder_as6), ("copy_io", self.copy_io),
                                ("copy_ethernet", self.copy_ethernet), ("migrate_users", self.migrate_users),
                                ("migrate_opcua", self.enable_opcua), ("migrate_visu", self.migrate_visu)],
        }
        self.kind = PROJECT_KIND_BK2000
        self.kind_text = ("mode_unknown", {})
        self.option_widgets: list[tk.Widget] = []
        # File and configuration choices of the current conversion run.
        self.choices: dict[tuple[str, Path, tuple[Path, ...]], Path | None] = {}
        self.language = tk.StringVar(value=LANGUAGES[_language])
        # Widgets whose text follows the selected language, with their text keys.
        self.translated: list[tuple[tk.Widget, str, str]] = []
        master.title("BkNG Converter V4")
        if ICON_PATH.exists():
            master.iconbitmap(default=str(ICON_PATH))
        master.minsize(700, 400)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(5, weight=1)

        language_row = ttk.Frame(self)
        language_row.grid(row=0, column=1, columnspan=2, pady=(0, 6), sticky="e")
        self._translate(ttk.Label(language_row), "language", suffix=":").pack(side="left", padx=(12, 6))
        language_box = ttk.Combobox(language_row, textvariable=self.language, values=list(LANGUAGES.values()),
                                    state="readonly", width=10)
        language_box.pack(side="left")
        language_box.bind("<<ComboboxSelected>>", lambda _event: self.change_language())

        old_group = self._group(1, "old_group")
        self._path_row(old_group, 0, "old_folder", self.old_path, self.choose_old)
        self.kind_label = ttk.Label(old_group, foreground="#1f5fa8")
        self.kind_label.grid(row=1, column=0, columnspan=3, padx=(20, 0), sticky="w")
        self.old_path.trace_add("write", lambda *_: self.detect_kind())
        new_group = self._group(2, "new_group")
        # Each mode has a radio button as heading above its path rows; the rows of
        # the other mode are disabled.
        self._mode_heading(new_group, 0, "project", "new_mode_project")
        ttk.Separator(new_group).grid(row=2, column=0, columnspan=3, pady=8, sticky="ew")
        self._mode_heading(new_group, 3, "zip", "new_mode_zip")
        self.mode_widgets = {
            "project": self._path_row(new_group, 1, "new_folder", self.new_path, self.choose_new),
            "zip": self._path_row(new_group, 4, "zip_file", self.zip_path, self.choose_zip)
                   + self._path_row(new_group, 5, "zip_target", self.zip_target, self.choose_zip_target),
        }
        self.update_new_mode()
        self.options = ttk.Frame(self)
        self.options.grid(row=3, column=0, columnspan=3, pady=(12, 2), sticky="ew")
        self.show_options()
        # Progress of a running conversion: bar over the steps and the current step with a spinner.
        progress_row = ttk.Frame(self)
        progress_row.grid(row=4, column=0, columnspan=2, pady=(2, 10), padx=(0, 12), sticky="ew")
        progress_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(progress_row, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.progress_label = ttk.Label(progress_row)
        self.progress_label.grid(row=1, column=0, sticky="w")
        self.progress_text = ""
        self.spinner = 0
        self.convert_button = self._translate(ttk.Button(self, command=self.run_conversion), "convert")
        self.convert_button.grid(row=4, column=2, pady=(2, 10), sticky="e")
        # Conversion in a worker thread; it hands Tk calls (dialogs, log, progress) to the main thread.
        self.worker: threading.Thread | None = None
        # A source ZIP file is unpacked in a thread as well: the thread, the key of its file and the
        # first line of an error of an earlier attempt for a key.
        self.unpacking: threading.Thread | None = None
        self.unpack_key: tuple[Path, int, int] | None = None
        self.source_error: tuple[tuple[Path, int, int], str] | None = None
        self.polling = False
        self.calls: queue.Queue[tuple[Callable[..., object], tuple[object, ...], threading.Event, dict]] = queue.Queue()
        self.log = scrolledtext.ScrolledText(self, height=15, wrap=tk.WORD, state="disabled")
        self.log.grid(row=5, column=0, columnspan=3, sticky="nsew")
        self.write_log(tr("start_hint"))

    def _translate(self, widget: tk.Widget, key: str, suffix: str = "") -> tk.Widget:
        widget.configure(text=tr(key) + suffix)
        self.translated.append((widget, key, suffix))
        return widget

    def change_language(self) -> None:
        code = next(code for code, name in LANGUAGES.items() if name == self.language.get())
        set_language(code)
        for widget, key, suffix in self.translated:
            widget.configure(text=tr(key) + suffix)
        self.kind_label.configure(text=tr(self.kind_text[0], **self.kind_text[1]))

    def show_options(self) -> None:
        """Show the options of the detected kind of source project."""
        self.translated = [entry for entry in self.translated if entry[0] not in self.option_widgets]
        for widget in self.option_widgets:
            widget.destroy()
        self.option_widgets = []
        for row, (key, variable) in enumerate(self.options_by_kind[self.kind]):
            command = self._ladder_option_changed if key == "convert_ladder_as6" else None
            widget = self._translate(ttk.Checkbutton(self.options, variable=variable, command=command), key)
            widget.grid(row=row, column=0, pady=2, sticky="w")
            self.option_widgets.append(widget)

    def _ladder_option_changed(self) -> None:
        """When the conversion of Ladder Diagrams is switched on, tell that it needs Automation Studio 6."""
        if not self.convert_ladder_as6.get():
            return
        bin_dir = find_as6_bin()
        status = tr("ladder_as6_found", path=bin_dir) if bin_dir else tr("ladder_as6_not_found")
        (messagebox.showinfo if bin_dir else messagebox.showwarning)(
            tr("ladder_as6_title"), tr("ladder_as6_info", status=status), parent=self)

    def detect_kind(self) -> None:
        """Detect whether the selected source project is a Bk2000 project or a BkNG project to be migrated."""
        path = Path(self.old_path.get())
        if path.suffix.casefold() == ZIP_SUFFIX and path.is_file():
            # A ZIP file is unpacked in a thread first; this method runs again afterwards.
            key = self._zip_key(path)
            if self.source_error and self.source_error[0] == key:
                self.kind_text = ("mode_error", {"error": self.source_error[1]})
                self.kind_label.configure(text=tr(*self.kind_text[:1], **self.kind_text[1]))
                return
            if self.source_zip is None or self.source_zip[0] != key:
                self._unpack_source(path, key)
                return
        elif self.unpack_key is not None:
            self._unpacking_done()  # the ZIP file being unpacked is no longer selected
        kind = PROJECT_KIND_BK2000
        try:
            kind, version = detect_project_kind(self.source_project_file())
            text = (("mode_migration", {"version": f"{version[0]}.{version[1]}"}) if kind == PROJECT_KIND_BKNG
                    else ("mode_bk2000", {}))
        except (ConversionError, OSError) as error:
            text = (("mode_error", {"error": str(error).splitlines()[0]})
                    if path.suffix.casefold() in (PROJECT_FILE_SUFFIX, ZIP_SUFFIX) else ("mode_unknown", {}))
        self.kind_text = text
        self.kind_label.configure(text=tr(text[0], **text[1]))
        if kind != self.kind:
            self.kind = kind
            self.show_options()

    def _group(self, row: int, label: str) -> ttk.LabelFrame:
        group = self._translate(ttk.LabelFrame(self, padding=(10, 4, 10, 8)), label)
        group.grid(row=row, column=0, columnspan=3, pady=(0, 10), sticky="ew")
        group.columnconfigure(1, weight=1)
        return group

    def _mode_heading(self, parent: tk.Misc, row: int, mode: str, label: str) -> None:
        self._translate(ttk.Radiobutton(parent, value=mode, variable=self.new_mode, command=self.update_new_mode),
                        label).grid(row=row, column=0, columnspan=3, pady=(2, 0), sticky="w")

    def _path_row(self, parent: tk.Misc, row: int, label: str, variable: tk.StringVar,
                  command: object) -> list[tk.Widget]:
        # A fixed label width lines up the entries of all groups.
        widgets = [self._translate(ttk.Label(parent, width=PATH_LABEL_WIDTH), label, suffix=":"),
                   ttk.Entry(parent, textvariable=variable),
                   self._translate(ttk.Button(parent, command=command), "browse")]
        widgets[0].grid(row=row, column=0, padx=(20, 8), pady=5, sticky="w")
        widgets[1].grid(row=row, column=1, pady=5, sticky="ew")
        widgets[2].grid(row=row, column=2, padx=(8, 0), pady=5)
        return widgets

    def update_new_mode(self) -> None:
        for mode, widgets in self.mode_widgets.items():
            state = "normal" if mode == self.new_mode.get() else "disabled"
            for widget in widgets:
                widget.configure(state=state)

    def source_project_file(self) -> Path:
        """Return the ``.apj`` file of the source project; a ZIP file is unpacked into a temporary folder.

        The archive is unpacked again only when another or a changed file is selected.
        """
        path = Path(self.old_path.get())
        if path.suffix.casefold() != ZIP_SUFFIX:
            return path
        if not path.is_file():
            raise ConversionError(tr("no_source_zip", path=path))
        key = self._zip_key(path)
        if self.source_zip is None or self.source_zip[0] != key:
            folder = tempfile.TemporaryDirectory(prefix="BkNG_source_")
            try:
                project = extract_source_project(path, Path(folder.name))
            except BaseException:
                folder.cleanup()
                raise
            self._keep_source(path, key, folder, project)
        return self.source_zip[2]

    @staticmethod
    def _zip_key(path: Path) -> tuple[Path, int, int]:
        """Identifies a ZIP file and its version: (path, modification time, size)."""
        status = path.stat()
        return path.resolve(), status.st_mtime_ns, status.st_size

    def _keep_source(self, path: Path, key: tuple[Path, int, int], folder: tempfile.TemporaryDirectory[str],
                     project: Path) -> None:
        """Use the unpacked source project; the folder of the one unpacked before is deleted."""
        if self.source_zip is not None:
            self.source_zip[1].cleanup()
        self.source_zip = (key, folder, project)
        self.write_log(tr("source_extracted", archive=path, path=project.parent))

    def _unpack_source(self, path: Path, key: tuple[Path, int, int]) -> None:
        """Unpack the selected source ZIP file in a thread; the window shows that it is busy."""
        self.kind_text = ("source_unpacking", {})
        self.kind_label.configure(text=tr("source_unpacking"))
        if self.unpack_key == key:
            return  # already being unpacked
        self.unpack_key = key
        self.convert_button.state(["disabled"])
        self._show_busy(tr("source_unpacking"))

        def unpack() -> None:
            folder = tempfile.TemporaryDirectory(prefix="BkNG_source_")
            try:
                outcome = (folder, extract_source_project(path, Path(folder.name)), None)
            except Exception as error:
                folder.cleanup()
                outcome = (None, None, str(error).splitlines()[0])
            self._call(self._source_unpacked, path, key, *outcome, wait=False)

        self.unpacking = threading.Thread(target=unpack, daemon=True)
        self.unpacking.start()
        self._start_polling()

    def _source_unpacked(self, path: Path, key: tuple[Path, int, int], folder: tempfile.TemporaryDirectory[str] | None,
                         project: Path | None, error: str | None) -> None:
        """Main thread: take over the result of :meth:`_unpack_source` unless another file was selected."""
        if key != self.unpack_key:
            if folder is not None:
                folder.cleanup()
            return
        self._unpacking_done()
        if error is not None:
            self.source_error = (key, error)
        elif self.source_zip is not None and self.source_zip[0] == key:
            folder.cleanup()  # unpacked meanwhile, e.g. by a conversion run
        else:
            self._keep_source(path, key, folder, project)
        self.detect_kind()

    def _unpacking_done(self) -> None:
        self.unpacking = self.unpack_key = None
        self._end_busy()
        if self.worker is None:
            self.convert_button.state(["!disabled"])

    def choose_old(self) -> None:
        selection = filedialog.askopenfilename(
            title=tr("select_old"), filetypes=[(tr("source_files"), f"*{PROJECT_FILE_SUFFIX} *{ZIP_SUFFIX}"),
                                               (tr("project_files"), "*" + PROJECT_FILE_SUFFIX),
                                               (tr("zip_files"), "*" + ZIP_SUFFIX)])
        if selection:
            self.old_path.set(selection)

    def choose_new(self) -> None:
        self._choose_project(self.new_path, "select_new")

    def choose_zip(self) -> None:
        selection = filedialog.askopenfilename(title=tr("select_zip"), filetypes=[(tr("zip_files"), "*.zip")])
        if selection:
            self.zip_path.set(selection)

    def choose_zip_target(self) -> None:
        selection = filedialog.askdirectory(title=tr("select_zip_target"), mustexist=True)
        if selection:
            self.zip_target.set(selection)

    def _choose_project(self, variable: tk.StringVar, title: str) -> None:
        selection = filedialog.askopenfilename(
            title=tr(title), filetypes=[(tr("project_files"), "*" + PROJECT_FILE_SUFFIX)])
        if selection:
            variable.set(selection)


    def write_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(tk.END, text)
        self.log.see(tk.END)
        self.log.configure(state="disabled")

    def choose_file(self, filename: str, root: Path, candidates: list[Path]) -> Path | None:
        # Each choice is asked only once per conversion run.
        key = (filename.casefold(), root, tuple(candidates))
        if key not in self.choices:
            selected = FileChoiceDialog(self, filename, root, candidates).result
            if selected is not None:
                self.write_log(tr("using_file", filename=filename, path=selected))
            self.choices[key] = selected
        return self.choices[key]

    def run_conversion(self) -> None:
        """Check the selection, then convert in a worker thread while the window shows the progress."""
        if self.worker is not None or self.unpacking is not None:
            return
        self.choices = {}
        try:
            source = self.source_project_file()
            kind, version = detect_project_kind(source)
            if kind != self.kind:
                self.detect_kind()
            old_root = project_dir_from_file(source)
            archive: tuple[Path, Path] | None = None
            if self.new_mode.get() == "zip":
                if kind == PROJECT_KIND_BK2000:
                    check_bk2000_project(old_root)
                archive = (Path(self.zip_path.get()), Path(self.zip_target.get()))
            else:
                project_dir_from_file(Path(self.new_path.get()))
        except (ConversionError, OSError) as error:
            self.write_log(tr("error", error=error))
            messagebox.showerror(tr("conversion_failed"), str(error))
            return
        options = {name: variable.get() for name, variable in (
            ("copy_ladder", self.copy_ladder), ("copy_data", self.copy_data), ("copy_io", self.copy_io),
            ("copy_ethernet", self.copy_ethernet), ("enable_opcua", self.enable_opcua),
            ("convert_visu", self.convert_visu), ("migrate_application", self.migrate_application),
            ("migrate_users", self.migrate_users), ("migrate_visu", self.migrate_visu),
            ("convert_ladder_as6", self.convert_ladder_as6))}
        self.convert_button.state(["disabled"])
        self.progress.configure(value=0, maximum=1)
        self.progress_label.configure(text="")
        self.worker = threading.Thread(target=self._convert, daemon=True,
                                       args=(Path(self.old_path.get()), source, kind, version, old_root, archive,
                                             Path(self.new_path.get()), options))
        self.worker.start()
        self._start_polling()

    def _start_polling(self) -> None:
        if not self.polling:
            self.polling = True
            self.after(PROGRESS_POLL_MS, self._process_calls)

    def _call(self, function: Callable[..., object], *args: object, wait: bool = True) -> object:
        """Run ``function`` in the Tk main thread; from the worker thread, wait for its result if ``wait``."""
        if threading.current_thread() is threading.main_thread():
            return function(*args)
        done, result = threading.Event(), {}
        self.calls.put((function, args, done, result))
        if not wait:
            return None
        done.wait()
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def _process_calls(self) -> None:
        """Run the calls queued by the worker threads; keep polling while one is running."""
        while True:
            try:
                function, args, done, result = self.calls.get_nowait()
            except queue.Empty:
                break
            try:
                result["value"] = function(*args)
            except BaseException as error:  # handed back to the waiting worker
                result["error"] = error
            finally:
                done.set()
        if self.worker is not None or self.unpacking is not None:
            self.spinner += 1
            if self.progress_text:
                self.progress_label.configure(
                    text=f"{self.progress_text}  {PROGRESS_SPINNER[self.spinner // 4 % len(PROGRESS_SPINNER)]}")
            self.after(PROGRESS_POLL_MS, self._process_calls)
        else:
            self.polling = False

    def _choose_in_main_thread(self, filename: str, root: Path, candidates: list[Path]) -> Path | None:
        return self._call(self.choose_file, filename, root, candidates)

    def _show_progress(self, done: int, total: int, text: str) -> None:
        if str(self.progress.cget("mode")) == "indeterminate":
            self.progress.stop()
            self.progress.configure(mode="determinate")
        self.progress.configure(value=done, maximum=max(total, 1))
        self.progress_text = text
        self.progress_label.configure(text=text)

    def _show_busy(self, text: str) -> None:
        """Animated bar for a step of unknown duration, e.g. unpacking a ZIP file."""
        # The indicator moves by 1 per interval and wraps at maximum; a run sets maximum to its step count.
        self.progress.configure(mode="indeterminate", value=0, maximum=100)
        self.progress.start(PROGRESS_BUSY_MS)
        self.progress_text = text
        self.progress_label.configure(text=text)

    def _end_busy(self) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        if self.worker is None:
            self.progress_text = ""
            self.progress_label.configure(text="")

    def _conversion_finished(self) -> None:
        self.worker = None
        if self.unpacking is None:
            if str(self.progress.cget("mode")) == "indeterminate":
                self.progress.stop()  # e.g. unpacking the base project failed
                self.progress.configure(mode="determinate", value=0)
            self.convert_button.state(["!disabled"])
            self.progress_text = ""
            self.progress_label.configure(text="")

    def _convert(self, old_project: Path, source: Path, kind: str, version: tuple[int, int] | None, old_root: Path,
                 archive: tuple[Path, Path] | None, new_project: Path, options: dict[str, bool]) -> None:
        """Worker thread: run all conversion steps; dialogs and log output go through the main thread."""
        try:
            self._convert_steps(old_project, source, kind, version, old_root, archive, new_project, options)
        except Exception as error:  # an unexpected error must not leave the window locked
            self._call(self.write_log, tr("error", error=f"{type(error).__name__}: {error}"), wait=False)
            self._call(messagebox.showerror, tr("conversion_failed"), f"{type(error).__name__}: {error}", wait=False)
        finally:
            self._call(self._conversion_finished, wait=False)

    def _use_unpacked_project(self, project: Path) -> None:
        self.new_path.set(str(project))
        self.new_mode.set("project")
        self.update_new_mode()

    def _convert_steps(self, old_project: Path, source: Path, kind: str, version: tuple[int, int] | None,
                       old_root: Path, archive: tuple[Path, Path] | None, new_project: Path,
                       options: dict[str, bool]) -> None:
        chooser = self._choose_in_main_thread
        unpacked: Path | None = None
        try:
            if archive is not None:
                self._call(self._show_busy, tr("step_unpack") + " …", wait=False)
                unpacked = extract_base_project(*archive, source)
                # Unpacked: no animation while the configuration is chosen.
                self._call(self._show_progress, 0, 1, tr("step_unpack"), wait=False)
                self._call(self.write_log, tr("zip_extracted", path=unpacked.parent), wait=False)
                # A further run converts into the unpacked project instead of unpacking again.
                self._call(self._use_unpacked_project, unpacked, wait=False)
                new_project = unpacked
            new_root = project_dir_from_file(new_project)
            # Choose the target configuration first if the BkNG project has several.
            active_configuration(project_root(new_root), chooser, ask=True)
        except (ConversionError, OSError) as error:
            self._call(self.write_log, tr("error", error=error), wait=False)
            self._call(messagebox.showerror, tr("conversion_failed"), str(error), wait=False)
            return

        migration = kind == PROJECT_KIND_BKNG
        mode = (tr("mode_migration", version=f"{version[0]}.{version[1]}") if migration else tr("mode_bk2000"))
        report = ConversionReport(old_project, new_project, datetime.now(), mode=mode)
        if source != old_project:
            report.sections.append(ReportSection(tr("step_unpack_source"), tr(
                "source_extracted", archive=old_project, path=old_root)))
        if unpacked is not None:
            report.sections.append(ReportSection(tr("step_unpack"), tr("zip_extracted", path=unpacked.parent)))
        global_var: list[Path] = []  # the converted global.var, needed by the OPC UA and visu steps

        def convert_global() -> ReportSection:
            result = convert(old_root, new_root, chooser)
            global_var.append(result.target)
            details = tr("summary", converted=result.converted, copied=result.copied, target=result.target)
            if result.ust_target:
                details += tr("ust_written", name=result.ust_name, path=result.ust_target)
            return ReportSection(tr("step_global"), details, result.warnings)

        def ladder_tasks() -> ReportSection:
            ladder = copy_ladder_tasks(old_root, new_root, chooser, options["convert_ladder_as6"])
            details = (tr("ladder_summary", tasks=len(ladder.tasks), files=ladder.files_copied,
                          overwritten=ladder.files_overwritten, packages=len(ladder.packages_updated))
                       + tr("ladder_deployed", count=len(ladder.deployed))
                       + tr("ladder_tasks") + "\n" + "".join(f"- {task}\n" for task in ladder.tasks))
            details += ladder.ladder_details
            return ReportSection(tr("step_ladder"), details, ladder.warnings)

        def data_objects() -> ReportSection:
            data = copy_data_objects(old_root, new_root, chooser)
            if not data.objects:
                return ReportSection(tr("step_data"), tr("data_none"), data.warnings)
            details = (tr("data_summary", objects=len(data.objects), files=data.files_copied,
                          overwritten=data.files_overwritten, packages=len(data.packages_updated),
                          deployed=len(data.deployed))
                       + tr("data_objects") + "\n" + "".join(f"- {folder}\n" for folder in data.objects))
            return ReportSection(tr("step_data"), details, data.warnings)

        def analysis() -> ReportSection:
            result = analyse_bk2000_project(old_root, new_root, chooser)
            details = "".join(tr("analysis_ptop", path=path) for path in result.ptop)
            if result.libraries:
                details += tr("analysis_libraries", path=result.library_source) + "".join(
                    tr("analysis_library", name=name, language=language,
                       present=tr("analysis_library_present") if present else "")
                    for name, language, present in result.libraries)
            else:
                details += tr("analysis_no_libraries")
            return ReportSection(tr("step_analysis"), details, result.warnings)

        def io_configuration() -> ReportSection:
            io = copy_io_configuration(old_root, new_root, chooser)
            details = tr("io_summary", modules=len(io.modules), replaced=io.modules_replaced,
                         mappings=io.mappings_copied, hardware=io.hardware)
            if io.fieldbus_devices:
                details += tr("io_fieldbus", devices=", ".join(io.fieldbus_devices),
                              folder=io.descriptions[0].parent, files=", ".join(path.name for path in io.descriptions))
            if io.dtm_folders:
                details += tr("io_dtm", folders=", ".join(str(folder) for folder in io.dtm_folders))
            return ReportSection(tr("step_io"), details, io.warnings)

        def ethernet_settings() -> ReportSection:
            ethernet = copy_ethernet_settings(old_root, new_root, chooser)
            mode = dict(ethernet.settings).get(f"{ETHERNET_INTERFACE}.Mode")
            mode = tr("ethernet_mode_dhcp") if mode is None else tr("ethernet_mode_manual") if mode == "Manual" else mode
            details = (tr("ethernet_summary", cpu=ethernet.cpu, mode=mode, hardware=ethernet.hardware)
                       + "".join(f"  {key} = {value}\n" for key, value in ethernet.settings))
            return ReportSection(tr("step_ethernet"), details, ethernet.warnings)

        def opcua_variables() -> ReportSection:
            opcua = enable_opcua_variables(new_root, global_var[0], chooser)
            return ReportSection(tr("step_opcua"), tr("opcua_summary", count=len(opcua.variables),
                                                      replaced=opcua.replaced, path=opcua.target), opcua.warnings)

        def user_visu() -> ReportSection:
            visu = convert_user_visu(old_root, new_root, global_var[0], chooser, options["convert_ladder_as6"])
            if visu.visu is None:
                details = tr("visu_none")
            else:
                details = tr("visu_summary", pages=len(visu.pages), widgets=visu.widgets, bindings=visu.bindings,
                             names=", ".join(visu.pages), binding_dir=visu.binding_dir)
                if visu.source_size and visu.target_size:
                    details += tr("visu_scaling", width=visu.source_size[0], height=visu.source_size[1],
                                  header=visu.source_size[2], target_width=visu.target_size[0],
                                  target_height=visu.target_size[1])
                if visu.variable_bindings or visu.expressions:
                    details += tr("visu_dynamics", widgets=visu.variable_bindings, expressions=visu.expressions)
                if visu.tasks:
                    details += tr("visu_tasks", tasks=", ".join(task + ("" if deployed else tr("visu_task_not_deployed"))
                                                                for task, deployed in visu.tasks))
                if visu.opcua_enabled:
                    details += tr("visu_opcua_enabled", names=", ".join(visu.opcua_enabled))
                if visu.navigation_buttons:
                    details += tr("visu_nav_summary", path=visu.navigation, buttons="\n".join(
                        f"  {page}: {text}" for page, text in visu.navigation_buttons.items()))
            return ReportSection(tr("step_visu"), details, visu.warnings)

        def migration_step(title: str, function: Callable[[], MigrationResult]) -> Callable[[], ReportSection]:
            def step() -> ReportSection:
                result = function()
                return ReportSection(tr(title), result.details, result.warnings)
            return step

        def migration_analysis() -> MigrationResult:
            result = MigrationResult(tr("migration_analysis", version=f"{version[0]}.{version[1]}",
                                        old=active_configuration(project_root(old_root), chooser).name,
                                        new=active_configuration(project_root(new_root), chooser,
                                                                 ask=True).name))
            for path in find_ptop_files(logical_dir(old_root)):
                result.warnings.append(tr("warn_ptop", path=path.relative_to(old_root)))
            return result

        def migrate_global() -> MigrationResult:
            result, target = migrate_global_declarations(old_root, new_root)
            global_var.append(target)
            return result

        if migration:
            steps = [("step_migration_analysis", True, migration_step("step_migration_analysis", migration_analysis)),
                     ("step_cleanup", True, migration_step("step_cleanup",
                                                           lambda: remove_base_samples(new_root, chooser))),
                     ("step_global_migration", True, migration_step("step_global_migration", migrate_global)),
                     ("step_visu_task", True, migration_step("step_visu_task",
                                                             lambda: migrate_visu_task_settings(old_root, new_root))),
                     ("step_application", options["migrate_application"], migration_step(
                         "step_application", lambda: migrate_application(old_root, new_root, chooser,
                                                                         options["convert_ladder_as6"]))),
                     ("step_libraries", options["migrate_application"], migration_step(
                         "step_libraries", lambda: migrate_libraries(old_root, new_root, chooser))),
                     ("step_io", options["copy_io"], io_configuration),
                     ("step_ethernet", options["copy_ethernet"], ethernet_settings),
                     ("step_users", options["migrate_users"], migration_step(
                         "step_users", lambda: migrate_users(old_root, new_root, chooser))),
                     ("step_opcua", options["enable_opcua"], migration_step(
                         "step_opcua", lambda: migrate_opcua(old_root, new_root, global_var[0], chooser))),
                     ("step_visu_migration", options["migrate_visu"], migration_step(
                         "step_visu_migration", lambda: migrate_visu(old_root, new_root, chooser)))]
        else:
            steps = [("step_analysis", True, analysis),
                     ("step_global", True, convert_global),
                     ("step_ladder", options["copy_ladder"], ladder_tasks),
                     ("step_data", options["copy_data"], data_objects),
                     ("step_io", options["copy_io"], io_configuration),
                     ("step_ethernet", options["copy_ethernet"], ethernet_settings),
                     ("step_opcua", options["enable_opcua"], opcua_variables),
                     ("step_visu", options["convert_visu"], user_visu)]
        enabled_steps = [(title, step) for title, enabled, step in steps if enabled]
        for number, (title, step) in enumerate(enabled_steps):
            self._call(self._show_progress, number, len(enabled_steps),
                       tr("progress_step", number=number + 1, total=len(enabled_steps), step=tr(title)), wait=False)
            try:
                report.sections.append(step())
            except (ConversionError, OSError, ET.ParseError) as error:
                report.error = (tr(title), str(error))
                break
        self._call(self._show_progress, len(enabled_steps), len(enabled_steps), tr("progress_report"), wait=False)

        self._call(self.write_log, render_report(report) + "\n", wait=False)
        try:
            path = write_report(report, new_root)
        except OSError as error:
            message = tr("report_not_saved", error=error)
            self._call(self.write_log, message + "\n", wait=False)
            path = None
        else:
            message = tr("report_saved", path=path)
        self._call(self._show_result, report, path, message, wait=False)

    def _show_result(self, report: ConversionReport, path: Path | None, message: str) -> None:
        """Final dialog: result, number of open items and the offer to open the report."""
        result = tr("report_result_error", step=report.error[0]) if report.error else tr("report_result_ok",
                                                                                         todos=report.todos)
        title = tr("conversion_failed") if report.error else tr("report_title")
        icon = "error" if report.error else "warning" if report.todos else "info"
        if path is None:
            messagebox.showerror(title, f"{result}\n\n{message}")
        elif messagebox.askyesno(title, f"{result}\n\n{message}\n\n{tr('report_open')}", icon=icon):
            try:
                os.startfile(path)
            except OSError as error:
                messagebox.showerror(title, str(error))

def main() -> None:
    root = tk.Tk()
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
