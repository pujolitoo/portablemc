#!/usr/bin/env python
# encoding: utf8

# PortableMC is a portable Minecraft launcher in only one Python script (without addons).
# Copyright (C) 2021 Théo Rozier
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from sys import exit
import sys


if sys.version_info[0] < 3 or sys.version_info[1] < 6:
    print("PortableMC cannot be used with Python version prior to 3.6.x")
    exit(1)


from typing import Dict, Callable, Optional, Generator, Tuple, List, Union, Type
from http.client import HTTPConnection, HTTPSConnection
from json.decoder import JSONDecodeError
from contextlib import contextmanager
from urllib import parse as url_parse
from zipfile import ZipFile
from uuid import uuid4
from os import path
import subprocess
import platform
import hashlib
import atexit
import shutil
import base64
import json
import re
import os


LAUNCHER_NAME = "portablemc"
LAUNCHER_VERSION = "1.1.4-pre1"
LAUNCHER_AUTHORS = "Théo Rozier"

VERSION_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
ASSET_BASE_URL = "https://resources.download.minecraft.net/{}/{}"
AUTHSERVER_URL = "https://authserver.mojang.com/{}"
JVM_META_URL = "https://launchermeta.mojang.com/v1/products/java-runtime/2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"

MS_OAUTH_CODE_URL = "https://login.live.com/oauth20_authorize.srf"
MS_OAUTH_LOGOUT_URL = "https://login.live.com/oauth20_logout.srf"
MS_OAUTH_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
MS_XBL_AUTH_DOMAIN = "user.auth.xboxlive.com"
MS_XBL_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
MS_XSTS_AUTH_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MS_GRAPH_UPN_REQUEST_URL = "https://graph.microsoft.com/v1.0/me?$select=userPrincipalName"
MC_AUTH_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"

LEGACY_AUTH_FILE_NAME  = "portablemc_tokens"
AUTH_FILE_NAME  = "portablemc_auth.json"

LOGGING_CONSOLE_REPLACEMENT = "<PatternLayout pattern=\"%d{HH:mm:ss.SSS} [%t] %-5level %logger{36} - %msg%n\"/>"

JVM_EXEC_DEFAULT = "java"
JVM_ARGS_DEFAULT = "-Xmx2G",\
                   "-XX:+UnlockExperimentalVMOptions",\
                   "-XX:+UseG1GC",\
                   "-XX:G1NewSizePercent=20",\
                   "-XX:G1ReservePercent=20",\
                   "-XX:MaxGCPauseMillis=50",\
                   "-XX:G1HeapRegionSize=32M"



# This file is split between the Core which is the lib and the CLI launcher which extends the Core.
# Check at the end of this file (in the __main__ check) for the CLI launcher.
# Addons only apply to the CLI, the core lib may be extracted and published as a python lib in the future.


class CorePortableMC:

    def __init__(self):

        self._mc_os = self.get_minecraft_os()
        self._mc_arch = self.get_minecraft_arch()
        self._mc_archbits = self.get_minecraft_archbits()

        self._version_manifest: Optional[VersionManifest] = None
        self._download_buffer: Optional[bytearray] = None

    # Generic methods

    def compute_main_dir(self, main_dir: Optional[str]) -> str:
        return self.get_minecraft_dir() if main_dir is None else path.realpath(main_dir)

    def compute_work_dir(self, main_dir: Optional[str], work_dir: Optional[str]) -> str:
        if main_dir is None:
            main_dir = self.get_minecraft_dir()
        else:
            main_dir = path.realpath(main_dir)
        return main_dir if work_dir is None else path.realpath(work_dir)

    def search_mc(self, main_dir: Optional[str], search: Optional[str], *, local: bool = False) -> list:

        no_version = (search is None)
        versions_dir = path.join(self.compute_main_dir(main_dir), "versions")

        if local:
            if path.isdir(versions_dir):
                for version_id in os.listdir(versions_dir):
                    if no_version or search in version_id:
                        version_jar_file = path.join(versions_dir, version_id, f"{version_id}.jar")
                        if path.isfile(version_jar_file):
                            yield "unknown", version_id, path.getmtime(version_jar_file), False
        else:
            manifest = self.get_version_manifest()
            for version_data in manifest.all_versions() if no_version else manifest.search_versions(search):
                version_id = version_data["id"]
                version_jar_file = path.join(versions_dir, version_id, f"{version_id}.jar")
                yield version_data["type"], version_data["id"], version_data["releaseTime"], path.isfile(version_jar_file)

    def start_mc(self, *,
                   version: str,
                   jvm: Optional[Union[str, List[str]]] = None,     # Default to [JVM_EXEC_DEFAULT, *JVM_ARGS_DEFAULT]
                   main_dir: Optional[str] = None,                  # Default to .minecraft
                   work_dir: Optional[str] = None,                  # Default to main dir
                   uuid: Optional[str] = None,                      # Default to random UUID
                   username: Optional[str] = None,                  # Default to uuid[:8]
                   auth: 'Optional[BaseAuthSession]' = None,        # This parameter will override uuid/username
                   dry_run: bool = False,
                   better_logging: bool = True,
                   resolution: 'Optional[Tuple[int, int]]' = None,
                   demo: bool = False,
                   disable_multiplayer: bool = False,
                   disable_chat: bool = False,
                   server_addr: Optional[str] = None,
                   server_port: Optional[int] = None,
                   version_meta_modifier: 'Optional[Callable[[dict], None]]' = None,
                   libraries_modifier: 'Optional[Callable[[List[str], List[str]], None]]' = None,
                   args_modifier: 'Optional[Callable[[List[str], int], None]]' = None,
                   args_replacement_modifier: 'Optional[Callable[[Dict[str, str]], None]]' = None,
                   runner: 'Optional[Callable[[list, str, dict], None]]' = None) -> None:

        # This method can raise these errors:
        # - VersionNotFoundError: if the given version was not found
        # - URLError: for any URL resolving error
        # - DownloadCorruptedError: if a download is corrupted

        main_dir = self.compute_main_dir(main_dir)
        work_dir = self.compute_work_dir(main_dir, work_dir)
        assets_dir = path.join(main_dir, "assets")

        # Download list
        dl_list = DownloadList()

        # Resolve version metadata
        version, version_alias = self.get_version_manifest().filter_latest(version)
        version_meta, version_dir = self.resolve_version_meta_recursive(main_dir, version)
        if callable(version_meta_modifier):
            version_meta_modifier(version_meta)

        # Ensure version JAR file
        version_jar_file = self.ensure_version_jar(version_dir, version, version_meta, dl_list)

        # Assets loading and Logging configuration
        assets_index_version, assets_virtual_dir, _assets_count = self.ensure_assets(assets_dir, work_dir, version_meta, dl_list)
        logging_arg = self.ensure_logger(assets_dir, version_meta, dl_list, better_logging)

        # Libraries and natives loading
        classpath_libs, native_libs = self.ensure_libraries(main_dir, version_meta, dl_list)
        classpath_libs.append(version_jar_file)
        if callable(libraries_modifier):
            libraries_modifier(classpath_libs, native_libs)

        # Download JVM
        if jvm is None:
            jvm = [None, *JVM_ARGS_DEFAULT]
        elif isinstance(jvm, str):
            jvm = [jvm, *JVM_ARGS_DEFAULT]
        elif isinstance(jvm, tuple):
            jvm = list(jvm)

        if jvm[0] is None:
            version_java_version = version_meta.get("javaVersion")
            version_java_version_type = "jre-legacy" if version_java_version is None else version_java_version["component"]
            _jvm_version, jvm_exec_path = self.ensure_jvm(main_dir, version_java_version_type, dl_list)
            jvm[0] = jvm_exec_path

        # Start download list
        self.download_files(dl_list)

        # Don't run if dry run
        if dry_run:
            return

        # Extracting binaries
        bin_dir = path.join(work_dir, "bin", str(uuid4()))

        @atexit.register
        def _bin_dir_cleanup():
            if path.isdir(bin_dir):
                shutil.rmtree(bin_dir)

        # self.notice("start.extracting_natives")
        for native_lib in native_libs:
            with ZipFile(native_lib, 'r') as native_zip:
                for native_zip_info in native_zip.infolist():
                    if self.can_extract_native(native_zip_info.filename):
                        native_zip.extract(native_zip_info, bin_dir)

        # Computing features
        features = {
            "is_demo_user": demo,
            "has_custom_resolution": resolution is not None
        }

        # Computing arguments
        legacy_args = version_meta.get("minecraftArguments")

        raw_args = []
        raw_args.extend(self.interpret_args(version_meta["arguments"]["jvm"] if legacy_args is None else LEGACY_JVM_ARGUMENTS, features))

        if logging_arg is not None:
            raw_args.append(logging_arg)

        main_class = version_meta["mainClass"]
        if main_class == "net.minecraft.launchwrapper.Launch":
            raw_args.append("-Dminecraft.client.jar={}".format(version_jar_file))

        main_class_idx = len(raw_args)
        raw_args.append(main_class)
        raw_args.extend(self.interpret_args(version_meta["arguments"]["game"], features) if legacy_args is None else legacy_args.split(" "))

        if disable_multiplayer:
            raw_args.append("--disableMultiplayer")
        if disable_chat:
            raw_args.append("--disableChat")

        if server_addr is not None:
            raw_args.extend(("--server", server_addr))
        if server_port is not None:
            raw_args.extend(("--port", str(server_port)))

        if callable(args_modifier):
            args_modifier(raw_args, main_class_idx)

        # Computing username and uuid
        if auth is not None:
            uuid = auth.uuid
            username = auth.username
        else:
            uuid = uuid4().hex if uuid is None else uuid.replace("-", "")
            username = uuid[:8] if username is None else username[:16]  # Max username length is 16

        # Arguments replacements
        start_args_replacements = {
            # Game
            "auth_player_name": username,
            "version_name": version,
            "game_directory": work_dir,
            "assets_root": assets_dir,
            "assets_index_name": assets_index_version,
            "auth_uuid": uuid,
            "auth_access_token": "" if auth is None else auth.format_token_argument(False),
            "user_type": "mojang",
            "version_type": version_meta["type"],
            # Game (legacy)
            "auth_session": "notok" if auth is None else auth.format_token_argument(True),
            "game_assets": assets_virtual_dir,
            "user_properties": "{}",
            # JVM
            "natives_directory": bin_dir,
            "launcher_name": LAUNCHER_NAME,
            "launcher_version": LAUNCHER_VERSION,
            "classpath": self.get_classpath_separator().join(classpath_libs)
        }

        if resolution is not None:
            start_args_replacements["resolution_width"] = str(resolution[0])
            start_args_replacements["resolution_height"] = str(resolution[1])

        if callable(args_replacement_modifier):
            args_replacement_modifier(start_args_replacements)

        start_args = jvm[:]
        for arg in raw_args:
            for repl_id, repl_val in start_args_replacements.items():
                arg = arg.replace("${{{}}}".format(repl_id), repl_val)
            start_args.append(arg)

        os.makedirs(work_dir, exist_ok=True)

        if runner is None:
            subprocess.run(start_args, cwd=work_dir)
        else:
            runner(start_args, work_dir, {
                "version": version,
                "username": username,
                "uuid": uuid
            })

    def ensure_version_jar(self, version_dir: str, version: str, version_meta: dict, dl_list: 'DownloadList') -> 'str':
        """ Returns version JAR file path. """

        version_jar_file = path.join(version_dir, "{}.jar".format(version))
        if not path.isfile(version_jar_file):
            version_downloads = version_meta["downloads"]
            if "client" not in version_downloads:
                raise VersionNotFoundError(version)
            dl_list.append(DownloadEntry.from_version_meta_info(version_downloads["client"], version_jar_file, name="{}.jar".format(version)))

        return version_jar_file


    def ensure_assets(self, assets_dir: str, work_dir: str, version_meta: dict, dl_list: 'DownloadList') -> 'Tuple[str, str, int]':
        """ Returns (index_version, virtual_dir, assets_count). """

        assets_indexes_dir = path.join(assets_dir, "indexes")
        assets_index_version = version_meta["assets"]
        assets_index_file = path.join(assets_indexes_dir, "{}.json".format(assets_index_version))
        assets_index = None

        if path.isfile(assets_index_file):
            with open(assets_index_file, "rb") as assets_index_fp:
                try:
                    assets_index = json.load(assets_index_fp)
                except JSONDecodeError:
                    pass

        if assets_index is None:
            asset_index_info = version_meta["assetIndex"]
            asset_index_url = asset_index_info["url"]
            assets_index = self.json_simple_request(asset_index_url)
            if not path.isdir(assets_indexes_dir):
                os.makedirs(assets_indexes_dir, exist_ok=True)
            with open(assets_index_file, "wt") as assets_index_fp:
                json.dump(assets_index, assets_index_fp)

        assets_objects_dir = path.join(assets_dir, "objects")
        assets_virtual_dir = path.join(assets_dir, "virtual", assets_index_version)
        assets_mapped_to_resources = assets_index.get("map_to_resources", False)  # For version <= 13w23b
        assets_virtual = assets_index.get("virtual", False)  # For 13w23b < version <= 13w48b (1.7.2)

        for asset_id, asset_obj in assets_index["objects"].items():
            asset_hash = asset_obj["hash"]
            asset_hash_prefix = asset_hash[:2]
            asset_size = asset_obj["size"]
            asset_file = path.join(assets_objects_dir, asset_hash_prefix, asset_hash)
            if not path.isfile(asset_file) or path.getsize(asset_file) != asset_size:
                asset_url = ASSET_BASE_URL.format(asset_hash_prefix, asset_hash)
                dl_list.append(DownloadEntry(asset_url, asset_file, size=asset_size, sha1=asset_hash, name=asset_id))

        def finalize():
            if assets_mapped_to_resources or assets_virtual:
                for asset_id_to_cpy in assets_index["objects"].keys():
                    if assets_mapped_to_resources:
                        resources_asset_file = path.join(work_dir, "resources", asset_id_to_cpy)
                        if not path.isfile(resources_asset_file):
                            os.makedirs(path.dirname(resources_asset_file), exist_ok=True)
                            shutil.copyfile(asset_file, resources_asset_file)
                    if assets_virtual:
                        virtual_asset_file = path.join(assets_virtual_dir, asset_id_to_cpy)
                        if not path.isfile(virtual_asset_file):
                            os.makedirs(path.dirname(virtual_asset_file), exist_ok=True)
                            shutil.copyfile(asset_file, virtual_asset_file)

        dl_list.add_callback(finalize)
        return assets_index_version, assets_virtual_dir, len(assets_index["objects"])

    def ensure_logger(self, assets_dir: str, version_meta: dict, dl_list: 'DownloadList', better_logging: bool) -> 'Optional[str]':
        """ Return the logging argument if any. """

        if "logging" in version_meta:
            version_logging = version_meta["logging"]
            if "client" in version_logging:

                log_config_dir = path.join(assets_dir, "log_configs")
                client_logging = version_logging["client"]
                logging_file_info = client_logging["file"]
                logging_file = path.join(log_config_dir, logging_file_info["id"])
                logging_dirty = False

                download_entry = DownloadEntry.from_version_meta_info(logging_file_info, logging_file, name=logging_file_info["id"])
                if not path.isfile(logging_file) or path.getsize(logging_file) != download_entry.size:
                    dl_list.append(download_entry)
                    logging_dirty = True

                if better_logging:
                    real_logging_file = path.join(log_config_dir, "portablemc-{}".format(logging_file_info["id"]))
                else:
                    real_logging_file = logging_dirty

                def finalize():
                    if better_logging:
                        if logging_dirty or not path.isfile(real_logging_file):
                            with open(logging_file, "rt") as logging_fp:
                                with open(real_logging_file, "wt") as custom_logging_fp:
                                    raw = logging_fp.read() \
                                        .replace("<XMLLayout />", LOGGING_CONSOLE_REPLACEMENT) \
                                        .replace("<LegacyXMLLayout />", LOGGING_CONSOLE_REPLACEMENT)
                                    custom_logging_fp.write(raw)

                dl_list.add_callback(finalize)
                return client_logging["argument"].replace("${path}", real_logging_file)

        return None

    def ensure_libraries(self, main_dir: str, version_meta: dict, dl_list: 'DownloadList') -> 'Tuple[List[str], List[str]]':
        """ Returns (classpath_libs, native_libs) """

        libraries_dir = path.join(main_dir, "libraries")
        classpath_libs = []
        native_libs = []

        for lib_obj in version_meta["libraries"]:

            if "rules" in lib_obj:
                if not self.interpret_rule(lib_obj["rules"]):
                    continue

            lib_name = lib_obj["name"]  # type: str
            lib_type = None  # type: Optional[str]

            if "downloads" in lib_obj:

                lib_dl = lib_obj["downloads"]
                lib_dl_info = None

                if "natives" in lib_obj and "classifiers" in lib_dl:
                    lib_natives = lib_obj["natives"]
                    if self._mc_os in lib_natives:
                        lib_native_classifier = lib_natives[self._mc_os]
                        if self._mc_archbits is not None:
                            lib_native_classifier = lib_native_classifier.replace("${arch}", self._mc_archbits)
                        lib_name += ":{}".format(lib_native_classifier)
                        lib_dl_info = lib_dl["classifiers"][lib_native_classifier]
                        lib_type = "native"
                elif "artifact" in lib_dl:
                    lib_dl_info = lib_dl["artifact"]
                    lib_type = "classpath"

                if lib_dl_info is None:
                    continue

                lib_path = path.realpath(path.join(libraries_dir, lib_dl_info["path"]))
                download_entry = DownloadEntry.from_version_meta_info(lib_dl_info, lib_path, name=lib_name)
                if not path.isfile(lib_path) or path.getsize(lib_path) != download_entry.size:
                    dl_list.append(download_entry)

            else:

                # If no 'downloads' trying to parse the maven dependency string "<group>:<product>:<version>
                # to directory path. This may be used by custom configuration that do not provide download
                # links like Optifine.

                lib_name_parts = lib_name.split(":")

                maven_vendor = lib_name_parts[0]
                maven_vendor_split = maven_vendor.split(".")
                maven_package = lib_name_parts[1]
                maven_version = lib_name_parts[2]
                maven_jar = "{}-{}.jar".format(maven_package, maven_version)

                lib_path = path.join(libraries_dir, *maven_vendor_split, maven_package, maven_version, maven_jar)
                lib_type = "classpath"

                if not path.isfile(lib_path):
                    if "url" in lib_obj:
                        lib_url = "{}{}".format(lib_obj["url"], "/".join((*maven_vendor_split, maven_package, maven_version, maven_jar)))
                        dl_list.append(DownloadEntry(lib_url, lib_path, name=lib_name))
                    else:
                        continue

            if lib_type == "classpath":
                classpath_libs.append(lib_path)
            elif lib_type == "native":
                native_libs.append(lib_path)

        return classpath_libs, native_libs

    def ensure_jvm(self, main_dir: str, jvm_version_type: str, dl_list: 'DownloadList') -> 'Tuple[str, str]':
        """ Return (jvm_version, jvm_exec_path). """

        jvm_arch = self.get_minecraft_jvm()
        if jvm_arch is None:
            raise JvmLoadingError("not_found")

        all_jvm_meta = self.json_simple_request(JVM_META_URL)
        jvm_arch_meta = all_jvm_meta.get(jvm_arch)
        if jvm_arch_meta is None:
            raise JvmLoadingError("unsupported_jvm_arch")

        jvm_meta = jvm_arch_meta.get(jvm_version_type)
        if jvm_meta is None:
            raise JvmLoadingError("unsupported_jvm_version")

        jvm_meta = jvm_meta[0]
        jvm_version = jvm_meta["version"]["name"]
        jvm_manifest_url = jvm_meta["manifest"]["url"]
        jvm_manifest = self.json_simple_request(jvm_manifest_url)["files"]

        jvm_dir = path.join(main_dir, "jvm", jvm_version_type)
        os.makedirs(jvm_dir, exist_ok=True)

        jvm_exec = path.join(jvm_dir, "bin", "javaw.exe" if sys.platform == "win32" else "java")
        jvm_exec_files = []

        if not path.isfile(jvm_exec):
            for jvm_file_path_suffix, jvm_file in jvm_manifest.items():
                if jvm_file["type"] == "file":
                    jvm_file_path = path.join(jvm_dir, jvm_file_path_suffix)
                    jvm_download_info = jvm_file["downloads"]["raw"]
                    dl_list.append(DownloadEntry.from_version_meta_info(jvm_download_info, jvm_file_path, name=jvm_file_path_suffix))
                    if jvm_file.get("executable", False):
                        jvm_exec_files.append(jvm_file_path)

        def finalize():
            for exec_file in jvm_exec_files:
                os.chmod(exec_file, 0o777)

        dl_list.add_callback(finalize)
        return jvm_version, jvm_exec

    # Lazy variables getters

    def get_version_manifest(self) -> 'VersionManifest':
        if self._version_manifest is None:
            self._version_manifest = VersionManifest.load_from_url()
        return self._version_manifest

    def get_download_buffer(self) -> bytearray:
        if self._download_buffer is None:
            self._download_buffer = bytearray(65536)
        return self._download_buffer

    def new_auth_database(self, work_dir: Optional[str]) -> 'AuthDatabase':
        return AuthDatabase(path.join(work_dir, AUTH_FILE_NAME), path.join(work_dir, LEGACY_AUTH_FILE_NAME))

    # Public methods to be replaced by addons

    def mixin(self, target: str, func, owner: Optional[object] = None):
        if owner is None:
            owner = self
        old_func = getattr(owner, target, None)
        def wrapper(*args, **kwargs):
            return func(old_func, *args, **kwargs)
        setattr(owner, target, wrapper)

    # General utilities

    def download_files(self, lst: 'DownloadList', *, progress_callback: 'Optional[Callable[[DownloadProgress], None]]' = None):
        """ Downloads the given list of files. Even if some downloads fails, it continue and raise DownloadError(fails)
        only at the end, where 'fails' is a dict associating the entry URL and its error ('not_found', 'invalid_size',
        'invalid_sha1')."""

        if len(lst.entries):

            headers = {}
            buffer = bytearray(65536)
            total_size = 0
            fails = {}  # type: Dict[str, str]
            max_try_count = 3

            if progress_callback is not None:
                progress = DownloadProgress(lst.size)
                entry_progress = DownloadEntryProgress()
                progress.entries.append(entry_progress)
            else:
                progress = None
                entry_progress = None

            for host, entries in lst.entries.items():

                conn_type = HTTPSConnection if (host[0] == "1") else HTTPConnection
                conn = conn_type(host[1:])
                max_entry_idx = len(entries) - 1
                headers["Connection"] = "keep-alive"

                for i, entry in enumerate(entries):

                    last_entry = (i == max_entry_idx)
                    if last_entry:
                        headers["Connection"] = "close"

                    conn.request("GET", entry.url, None, headers)
                    res = conn.getresponse()
                    error = None

                    size_target = 0 if entry.size is None else entry.size
                    known_size = (size_target != 0)

                    for _ in range(max_try_count):

                        if res.status != 200:
                            error = "not_found"
                            continue

                        sha1 = hashlib.sha1()
                        size = 0

                        os.makedirs(path.dirname(entry.dst), exist_ok=True)
                        with open(entry.dst, "wb") as dst_fp:
                            while True:
                                read_len = res.readinto(buffer)
                                if not read_len:
                                    break
                                buffer_view = buffer[:read_len]
                                size += read_len
                                if known_size:
                                    # Only adding to total size if the size is known.
                                    total_size += read_len
                                sha1.update(buffer_view)
                                dst_fp.write(buffer_view)
                                if progress_callback is not None:
                                    progress.size = total_size
                                    entry_progress.name = entry.name
                                    entry_progress.total = size_target
                                    entry_progress.size = size
                                    progress_callback(progress)

                        if entry.size is not None and size != entry.size:
                            error = "invalid_size"
                        elif entry.sha1 is not None and sha1.hexdigest() != entry.sha1:
                            error = "invalid_sha1"
                        else:
                            break

                        if known_size:
                            # When re-trying, reset the total size to the previous state.
                            total_size -= size

                    else:
                        # If the break was not triggered, an error must be set.
                        fails[entry.url] = error

                conn.close()

            if len(fails):
                raise DownloadError(fails)

        for callback in lst.callbacks:
            callback()

    # Version metadata

    def get_version_dir(self, main_dir: str, name: str) -> str:
        return path.join(main_dir, "versions", name)

    def resolve_version_meta(self, main_dir: str, name: str) -> Tuple[dict, str]:
        """ Return (version_meta, version_dir). Raise VersionNotFoundError(name) if fails. """

        version_dir = self.get_version_dir(main_dir, name)
        version_meta_file = path.join(version_dir, "{}.json".format(name))
        content = None

        if path.isfile(version_meta_file):
            with open(version_meta_file, "rb") as version_meta_fp:
                try:
                    content = json.load(version_meta_fp)
                except JSONDecodeError:
                    pass

        if content is None:
            version_data = self.get_version_manifest().get_version(name)
            if version_data is not None:
                version_url = version_data["url"]
                content = self.json_simple_request(version_url)
                os.makedirs(version_dir, exist_ok=True)
                with open(version_meta_file, "wt") as version_meta_fp:
                    json.dump(content, version_meta_fp, indent=2)
            else:
                raise VersionNotFoundError(name)

        return content, version_dir

    def resolve_version_meta_recursive(self, main_dir: str, name: str) -> Tuple[dict, str]:
        """ Return (version_meta, version_dir). Raise VersionNotFoundError(name) if fails. """
        version_meta, version_dir = self.resolve_version_meta(main_dir, name)
        while "inheritsFrom" in version_meta:
            parent_meta, _ = self.resolve_version_meta(main_dir, version_meta["inheritsFrom"])
            del version_meta["inheritsFrom"]
            self.dict_merge(parent_meta, version_meta)
            version_meta = parent_meta
        return version_meta, version_dir

    # Version meta rules interpretation

    def interpret_rule(self, rules: list, features: Optional[dict] = None) -> bool:
        allowed = False
        for rule in rules:
            if "os" in rule:
                ros = rule["os"]
                if "name" in ros and ros["name"] != self._mc_os:
                    continue
                elif "arch" in ros and ros["arch"] != self._mc_arch:
                    continue
                elif "version" in ros and re.compile(ros["version"]).search(platform.version()) is None:
                    continue
            if "features" in rule:
                feature_valid = True
                for feat_name, feat_value in rule["features"].items():
                    if feat_name not in features or feat_value != features[feat_name]:
                        feature_valid = False
                        break
                if not feature_valid:
                    continue
            act = rule["action"]
            if act == "allow":
                allowed = True
            elif act == "disallow":
                allowed = False
        return allowed

    def interpret_args(self, args: list, features: dict) -> list:
        ret = []
        for arg in args:
            if isinstance(arg, str):
                ret.append(arg)
            else:
                if "rules" in arg:
                    if not self.interpret_rule(arg["rules"], features):
                        continue
                arg_value = arg["value"]
                if isinstance(arg_value, list):
                    ret.extend(arg_value)
                elif isinstance(arg_value, str):
                    ret.append(arg_value)
        return ret

    # Static utilities

    @staticmethod
    def get_minecraft_dir() -> str:
        pf = sys.platform
        home = path.expanduser("~")
        if pf.startswith("freebsd") or pf.startswith("linux") or pf.startswith("aix") or pf.startswith("cygwin"):
            return path.join(home, ".minecraft")
        elif pf == "win32":
            return path.join(home, "AppData", "Roaming", ".minecraft")
        elif pf == "darwin":
            return path.join(home, "Library", "Application Support", "minecraft")

    @staticmethod
    def get_minecraft_os() -> Optional[str]:
        pf = sys.platform
        if pf.startswith("freebsd") or pf.startswith("linux") or pf.startswith("aix") or pf.startswith("cygwin"):
            return "linux"
        elif pf == "win32":
            return "windows"
        elif pf == "darwin":
            return "osx"

    @staticmethod
    def get_minecraft_arch() -> str:
        machine = platform.machine().lower()
        return "x86" if machine in ("i386", "i686") else "x86_64" if machine in ("x86_64", "amd64", "ia64") else "unknown"

    @staticmethod
    def get_minecraft_archbits() -> Optional[str]:
        raw_bits = platform.architecture()[0]
        return "64" if raw_bits == "64bit" else "32" if raw_bits == "32bit" else None

    @staticmethod
    def get_minecraft_jvm() -> Optional[str]:
        mc_os = CorePortableMC.get_minecraft_os()
        if mc_os is None:
            return None
        if mc_os == "osx":
            return "mac-os"
        mc_arch = CorePortableMC.get_minecraft_arch()
        if mc_os == "linux":
            arch_jvm = ["linux-i386", "linux"]
        elif mc_os == "windows":
            arch_jvm = ["windows-x86", "windows-x64"]
        else:
            return None
        return arch_jvm[0] if mc_arch == "x86" else arch_jvm[1] if mc_arch == "x86_64" else None

    @staticmethod
    def get_classpath_separator() -> str:
        return ";" if sys.platform == "win32" else ":"

    @staticmethod
    def json_request(url: str, method: str, *,
                     data: Optional[bytes] = None,
                     headers: Optional[dict] = None,
                     ignore_error: bool = False,
                     timeout: Optional[int] = None) -> Tuple[int, dict]:

        url_parsed = url_parse.urlparse(url)
        conn_type = {"http": HTTPConnection, "https": HTTPSConnection}.get(url_parsed.scheme)
        if conn_type is None:
            raise JsonRequestError("Invalid URL scheme '{}'".format(url_parsed.scheme))
        conn = conn_type(url_parsed.netloc, timeout=timeout)
        if headers is None:
            headers = {}
        if data is not None:
            headers["Content-Length"] = len(data)  # Can be removed according to the spec of ".request"
        if "Accept" not in headers:
            headers["Accept"] = "application/json"
        headers["Connection"] = "close"

        try:
            conn.request(method, url, data, headers)
            res = conn.getresponse()
            try:
                return res.status, json.load(res)
            except JSONDecodeError:
                if ignore_error:
                    return res.status, {}
                else:
                    raise JsonRequestError("The request response is not JSON (status: {})".format(res.status))
        except OSError:
            raise JsonRequestError("Invalid host or other socket error")
        finally:
            conn.close()

    @classmethod
    def json_simple_request(cls, url: str, *, ignore_error: bool = False, timeout: Optional[int] = None) -> dict:
        return cls.json_request(url, "GET", ignore_error=ignore_error, timeout=timeout)[1]

    @classmethod
    def dict_merge(cls, dst: dict, other: dict):
        for k, v in other.items():
            if k in dst:
                if isinstance(dst[k], dict) and isinstance(other[k], dict):
                    cls.dict_merge(dst[k], other[k])
                    continue
                elif isinstance(dst[k], list) and isinstance(other[k], list):
                    dst[k].extend(other[k])
                    continue
            dst[k] = other[k]

    @staticmethod
    def can_extract_native(filename: str) -> bool:
        return not filename.startswith("META-INF") and not filename.endswith(".git") and not filename.endswith(".sha1")


class VersionManifest:

    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def load_from_url(cls):
        return cls(CorePortableMC.json_simple_request(VERSION_MANIFEST_URL))

    def filter_latest(self, version: str) -> Tuple[Optional[str], bool]:
        return (self._data["latest"][version], True) if version in self._data["latest"] else (version, False)

    def get_version(self, version: str) -> Optional[dict]:
        version, _alias = self.filter_latest(version)
        for version_data in self._data["versions"]:
            if version_data["id"] == version:
                return version_data
        return None

    def all_versions(self) -> list:
        return self._data["versions"]

    def search_versions(self, inp: str) -> Generator[dict, None, None]:
        inp, alias = self.filter_latest(inp)
        for version_data in self._data["versions"]:
            if (alias and version_data["id"] == inp) or (not alias and inp in version_data["id"]):
                yield version_data


class BaseAuthSession:

    TYPE = "raw"
    FIELDS = "access_token", "username", "uuid"

    def __init__(self, access_token: str, username: str, uuid: str):
        self.access_token = access_token
        self.username = username
        self.uuid = uuid

    def format_token_argument(self, legacy: bool) -> str:
        return "token:{}:{}".format(self.access_token, self.uuid) if legacy else self.access_token

    def validate(self) -> bool:
        return True

    def refresh(self):
        pass

    def invalidate(self):
        pass


class YggdrasilAuthSession(BaseAuthSession):

    TYPE = "yggdrasil"
    FIELDS = "access_token", "username", "uuid", "client_token"

    def __init__(self, access_token: str, username: str, uuid: str, client_token: str):
        super().__init__(access_token, username, uuid)
        self.client_token = client_token

    def validate(self) -> bool:
        return self.request("validate", {
            "accessToken": self.access_token,
            "clientToken": self.client_token
        }, False)[0] == 204

    def refresh(self):
        _, res = self.request("refresh", {
            "accessToken": self.access_token,
            "clientToken": self.client_token
        })
        self.access_token = res["accessToken"]
        self.username = res["selectedProfile"]["name"]  # Refresh username if renamed (does it works? to check.).

    def invalidate(self):
        self.request("invalidate", {
            "accessToken": self.access_token,
            "clientToken": self.client_token
        }, False)

    @classmethod
    def authenticate(cls, email_or_username: str, password: str) -> 'YggdrasilAuthSession':
        _, res = cls.request("authenticate", {
            "agent": {
                "name": "Minecraft",
                "version": 1
            },
            "username": email_or_username,
            "password": password,
            "clientToken": uuid4().hex
        })
        return cls(res["accessToken"], res["selectedProfile"]["name"], res["selectedProfile"]["id"], res["clientToken"])

    @classmethod
    def request(cls, req: str, payload: dict, error: bool = True) -> Tuple[int, dict]:
        code, res = CorePortableMC.json_request(AUTHSERVER_URL.format(req), "POST",
                                                data=json.dumps(payload).encode("ascii"),
                                                headers={"Content-Type": "application/json"},
                                                ignore_error=True)
        if error and code != 200:
            raise AuthError("yggdrasil", res["errorMessage"])
        return code, res


class MicrosoftAuthSession(BaseAuthSession):

    TYPE = "microsoft"
    FIELDS = "access_token", "username", "uuid", "refresh_token", "client_id", "redirect_uri"

    def __init__(self, access_token: str, username: str, uuid: str, refresh_token: str, client_id: str, redirect_uri: str):
        super().__init__(access_token, username, uuid)
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self._new_username = None  # type: Optional[str]

    def validate(self) -> bool:
        self._new_username = None
        code, res = self.mc_request(MC_PROFILE_URL, self.access_token)
        if code == 200:
            username = res["name"]
            if self.username != username:
                self._new_username = username
                return False
            return True
        return False

    def refresh(self):
        if self._new_username is not None:
            self.username = self._new_username
            self._new_username = None
        else:
            res = self.authenticate_base({
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
                "scope": "xboxlive.signin"
            })
            self.access_token = res["access_token"]
            self.username = res["username"]
            self.uuid = res["uuid"]
            self.refresh_token = res["refresh_token"]

    @staticmethod
    def get_authentication_url(app_client_id: str, redirect_uri: str, email: str, nonce: str):
        return "{}?{}".format(MS_OAUTH_CODE_URL, url_parse.urlencode({
            "client_id": app_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code id_token",
            "scope": "xboxlive.signin offline_access openid email",
            "login_hint": email,
            "nonce": nonce,
            "response_mode": "form_post"
        }))

    @staticmethod
    def get_logout_url(app_client_id: str, redirect_uri: str):
        return "{}?{}".format(MS_OAUTH_LOGOUT_URL, url_parse.urlencode({
            "client_id": app_client_id,
            "redirect_uri": redirect_uri
        }))

    @classmethod
    def check_token_id(cls, token_id: str, email: str, nonce: str) -> bool:
        id_token_payload = json.loads(cls.base64url_decode(token_id.split(".")[1]))
        return id_token_payload["nonce"] == nonce and id_token_payload["email"] == email

    @classmethod
    def authenticate(cls, app_client_id: str, code: str, redirect_uri: str) -> 'MicrosoftAuthSession':
        res = cls.authenticate_base({
            "client_id": app_client_id,
            "redirect_uri": redirect_uri,
            "code": code,
            "grant_type": "authorization_code",
            "scope": "xboxlive.signin"
        })
        return cls(res["access_token"], res["username"], res["uuid"], res["refresh_token"], app_client_id, redirect_uri)

    @classmethod
    def authenticate_base(cls, request_token_payload: dict) -> dict:

        # Microsoft OAuth
        _, res = cls.ms_request(MS_OAUTH_TOKEN_URL, request_token_payload, payload_url_encoded=True)
        ms_refresh_token = res["refresh_token"]

        # Xbox Live Token
        _, res = cls.ms_request(MS_XBL_AUTH_URL, {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": MS_XBL_AUTH_DOMAIN,
                "RpsTicket": "d={}".format(res["access_token"])
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT"
        })

        xbl_token = res["Token"]
        xbl_user_hash = res["DisplayClaims"]["xui"][0]["uhs"]

        # Xbox Live XSTS Token
        _, res = cls.ms_request(MS_XSTS_AUTH_URL, {
            "Properties": {
                "SandboxId": "RETAIL",
                "UserTokens": [xbl_token]
            },
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT"
        })
        xsts_token = res["Token"]

        if xbl_user_hash != res["DisplayClaims"]["xui"][0]["uhs"]:
            raise AuthError("microsoft.inconsistent_user_hash")

        # MC Services Auth
        _, res = cls.ms_request(MC_AUTH_URL, {
            "identityToken": "XBL3.0 x={};{}".format(xbl_user_hash, xsts_token)
        })
        mc_access_token = res["access_token"]

        # MC Services Profile
        code, res = cls.mc_request(MC_PROFILE_URL, mc_access_token)

        if code == 404:
            raise AuthError("microsoft.does_not_own_minecraft")
        elif code == 401:
            raise AuthError("microsoft.outdated_token")
        elif "error" in res or code != 200:
            raise AuthError("microsoft.error", res.get("errorMessage", res.get("error", "Unknown error")))

        return {
            "refresh_token": ms_refresh_token,
            "access_token": mc_access_token,
            "username": res["name"],
            "uuid": res["id"]
        }

    @classmethod
    def ms_request(cls, url: str, payload: dict, *, payload_url_encoded: bool = False) -> Tuple[int, dict]:
        data = (url_parse.urlencode(payload) if payload_url_encoded else json.dumps(payload)).encode("ascii")
        content_type = "application/x-www-form-urlencoded" if payload_url_encoded else "application/json"
        return CorePortableMC.json_request(url, "POST", data=data, headers={"Content-Type": content_type})

    @classmethod
    def mc_request(cls, url: str, bearer: str) -> Tuple[int, dict]:
        return CorePortableMC.json_request(url, "GET", headers={"Authorization": "Bearer {}".format(bearer)})

    @classmethod
    def base64url_decode(cls, s: str) -> bytes:
        rem = len(s) % 4
        if rem > 0:
            s += "=" * (4 - rem)
        return base64.urlsafe_b64decode(s)


class AuthDatabase:

    TYPES = {
        YggdrasilAuthSession.TYPE: YggdrasilAuthSession,
        MicrosoftAuthSession.TYPE: MicrosoftAuthSession
    }

    def __init__(self, filename: str, legacy_filename: str):
        self._filename = filename
        self._legacy_filename = legacy_filename
        self._sessions = {}  # type: Dict[str, Dict[str, BaseAuthSession]]

    def load(self):
        self._sessions.clear()
        if not path.isfile(self._filename):
            self._load_legacy_and_delete()
        try:
            with open(self._filename, "rb") as fp:
                data = json.load(fp)
                for typ, typ_data in data.items():
                    if typ not in self.TYPES:
                        continue
                    sess_type = self.TYPES[typ]
                    sessions = self._sessions[typ] = {}
                    sessions_data = typ_data["sessions"]
                    for email, sess_data in sessions_data.items():
                        sess_params = []
                        for field in sess_type.FIELDS:
                            sess_params.append(sess_data.get(field, ""))
                        sessions[email] = sess_type(*sess_params)
        except (OSError, KeyError, TypeError, JSONDecodeError):
            pass

    def _load_legacy_and_delete(self):
        try:
            with open(self._legacy_filename, "rt") as fp:
                for line in fp.readlines():
                    parts = line.split(" ")
                    if len(parts) == 5:
                        self.put(parts[0], YggdrasilAuthSession(parts[4], parts[2], parts[3], parts[1]))
            os.remove(self._legacy_filename)
        except OSError:
            pass

    def save(self):
        with open(self._filename, "wt") as fp:
            data = {}
            for typ, sessions in self._sessions.items():
                if typ not in self.TYPES:
                    continue
                sess_type = self.TYPES[typ]
                sessions_data = {}
                data[typ] = {"sessions": sessions_data}
                for email, sess in sessions.items():
                    sess_data = sessions_data[email] = {}
                    for field in sess_type.FIELDS:
                        sess_data[field] = getattr(sess, field)
            json.dump(data, fp, indent=2)

    def get(self, email_or_username: str, sess_type: Type[BaseAuthSession]) -> Optional[BaseAuthSession]:
        sessions = self._sessions.get(sess_type.TYPE)
        return None if sessions is None else sessions.get(email_or_username)

    def put(self, email_or_username: str, sess: BaseAuthSession):
        sessions = self._sessions.get(sess.TYPE)
        if sessions is None:
            if sess.TYPE not in self.TYPES:
                raise ValueError("Given session's type is not supported.")
            sessions = self._sessions[sess.TYPE] = {}
        sessions[email_or_username] = sess

    def remove(self, email_or_username: str, sess_type: Type[BaseAuthSession]) -> Optional[BaseAuthSession]:
        sessions = self._sessions.get(sess_type.TYPE)
        if sessions is not None:
            session = sessions.get(email_or_username)
            if session is not None:
                del sessions[email_or_username]
                return session


class DownloadEntry:

    __slots__ = "url", "size", "sha1", "dst", "name"

    def __init__(self, url: str, dst: str, *, size: 'Optional[int]' = None, sha1: 'Optional[str]' = None, name: Optional[str] = None):
        self.url = url
        self.dst = dst
        self.size = size
        self.sha1 = sha1
        self.name = url if name is None else name

    @classmethod
    def from_version_meta_info(cls, info: dict, dst: str, *, name: Optional[str] = None) -> 'DownloadEntry':
        return DownloadEntry(info["url"], dst, size=info["size"], sha1=info["sha1"], name=name)


class DownloadList:

    __slots__ = "entries", "callbacks", "count", "size"

    def __init__(self):
        self.entries = {}    # type: Dict[str, List[DownloadEntry]]
        self.callbacks = []  # type: List[Callable[[], None]]
        self.count = 0
        self.size = 0

    def append(self, entry: DownloadEntry):
        url_parsed = url_parse.urlparse(entry.url)
        if url_parsed.scheme not in ("http", "https"):
            raise ValueError("Illegal URL scheme for HTTP connection.")
        host_key = "{}{}".format(int(url_parsed.scheme == "https"), url_parsed.netloc)
        entries = self.entries.get(host_key)
        if entries is None:
            self.entries[host_key] = entries = []
        entries.append(entry)
        self.count += 1
        if entry.size is not None:
            self.size += entry.size

    def add_callback(self, callback: 'Callable[[], None]'):
        self.callbacks.append(callback)


class DownloadEntryProgress:
    __slots__ = "name", "size", "total"
    def __init__(self):
        self.name = ""
        self.size = 0
        self.total = 0


class DownloadProgress:
    __slots__ = "entries", "size", "total"
    def __init__(self, total: int):
        self.entries = []  # type: List[DownloadEntryProgress]
        self.size = 0
        self.total = total


class AuthError(Exception): ...
class VersionNotFoundError(Exception): ...
class DownloadError(Exception): ...
class JsonRequestError(Exception): ...
class JvmLoadingError(Exception): ...


LEGACY_JVM_ARGUMENTS = [
    {
        "rules": [{"action": "allow", "os": {"name": "osx"}}],
        "value": ["-XstartOnFirstThread"]
    },
    {
        "rules": [{"action": "allow", "os": {"name": "windows"}}],
        "value": "-XX:HeapDumpPath=MojangTricksIntelDriversForPerformance_javaw.exe_minecraft.exe.heapdump"
    },
    {
        "rules": [{"action": "allow", "os": {"name": "windows", "version": "^10\\."}}],
        "value": ["-Dos.name=Windows 10", "-Dos.version=10.0"]
    },
    "-Djava.library.path=${natives_directory}",
    "-Dminecraft.launcher.brand=${launcher_name}",
    "-Dminecraft.launcher.version=${launcher_version}",
    "-cp",
    "${classpath}"
]


if __name__ == '__main__':

    from argparse import ArgumentParser, Namespace, HelpFormatter
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from datetime import datetime
    from typing import cast, Any
    import webbrowser
    import time

    EXIT_VERSION_NOT_FOUND = 10
    # EXIT_CLIENT_JAR_NOT_FOUND = 11
    # EXIT_NATIVES_DIR_ALREADY_EXITS = 12
    EXIT_DOWNLOAD_ERROR = 13
    EXIT_AUTHENTICATION_FAILED = 14
    EXIT_VERSION_SEARCH_NOT_FOUND = 15
    EXIT_DEPRECATED_ARGUMENT = 16
    EXIT_LOGOUT_FAILED = 17
    EXIT_HTTP_ERROR = 18
    EXIT_JVM_LOADING_ERROR = 19

    ADDONS_DIR = "addons"
    ADDONS_PKG_INIT_CONTENT = "# This file was generated by PortableMC.\n" \
                              "# It's only purpose is to make this directory a valid python package.\n" \
                              "# Do not modify this file unless you know what you are doing, because this file " \
                              "is not intended to be shared.\n"
    ADDONS_TPL_INIT_CONTENT = "# Entry module for the addon\n\n" \
                              "NAME = \"{name}\"\n" \
                              "VERSION = \"0.0.1\"\n" \
                              "AUTHORS = ()\n" \
                              "REQUIRES = ()\n" \
                              "DESCRIPTION = \"\"\n\n\n" \
                              "def addon_build(pmc):\n" \
                              "    return None\n"

    MS_AZURE_APP_ID = "708e91b5-99f8-4a1d-80ec-e746cbb24771"

    GH_LATEST_RELEASE_URL = "https://api.github.com/repos/mindstorm38/portablemc/releases/latest"

    class PortableMC(CorePortableMC):

        VersionManifest = VersionManifest
        BaseAuthSession = BaseAuthSession
        YggdrasilAuthSession = YggdrasilAuthSession
        MicrosoftAuthSession = MicrosoftAuthSession
        AuthDatabase = AuthDatabase
        DownloadEntry = DownloadEntry
        AuthError = AuthError
        VersionNotFoundError = VersionNotFoundError
        DownloadCorruptedError = DownloadError

        def __init__(self):

            super().__init__()

            self._addons_dir = path.join(path.dirname(__file__), ADDONS_DIR)
            self._addons: Dict[str, PortableAddon] = {}

            self._messages = {

                "addon.defined_twice": "The addon '{}' is defined twice, both single-file and package, loaded the package one.",
                "addon.missing_requirement.module": "Addon '{0}' requires module '{1}' to load. You can try to install it using 'pip install {1}' or search for it on the web.",
                "addon.missing_requirement.ext": "Addon '{}' requires another addon '{}' to load.",
                "addon.failed_to_build": "Failed to build addon '{}' (contact addon's authors):",

                "args": "PortableMC is an easy to use portable Minecraft launcher in only one Python "
                        "script! This single-script launcher is still compatible with the official "
                        "(Mojang) Minecraft Launcher stored in .minecraft and use it.",
                "args.main_dir": "Set the main directory where libraries, assets and versions. "
                                 "This argument can be used or not by subcommand.",
                "args.work_dir": "Set the working directory where the game run and place for examples "
                                 "saves, screenshots (and resources for legacy versions), it also store "
                                 "runtime binaries and authentication. "
                                 "This argument can be used or not by subcommand.",
                "args.search": "Search for Minecraft versions.",
                "args.search.local": "Search only for local installed Minecraft versions.",
                "args.start": "Start a Minecraft version, default to the latest release.",
                "args.start.dry": "Simulate game starting.",
                "args.start.disable_multiplayer": "Disable the multiplayer buttons (>= 1.16).",
                "args.start.disable_chat": "Disable the online chat (>= 1.16).",
                "args.start.demo": "Start game in demo mode.",
                "args.start.resol": "Set a custom start resolution (<width>x<height>).",
                "args.start.jvm": "Set a custom JVM 'javaw' executable path. If this argument is omitted a public build of a JVM is downloaded from Mojang services.",
                "args.start.jvm_args": "Change the default JVM arguments.",
                "args.start.no_better_logging": "Disable the better logging configuration built by the launcher in "
                                                "order to improve the log readability in the console.",
                "args.start.temp_login": "Flag used with -l (--login) to tell launcher not to cache your session if "
                                         "not already cached, deactivated by default.",
                "args.start.login": "Use a email or username (legacy) to authenticate using mojang servers (you "
                                    "will be asked for password, it override --username and --uuid).",
                "args.start.microsoft": "Log-in using Microsoft account, to use with -l (--login).",
                "args.start.username": "Set a custom user name to play.",
                "args.start.uuid": "Set a custom user UUID to play.",
                "args.start.server": "Start the game and auto-connect to this server address (since 1.6).",
                "args.start.server_port": "Set the server address port (given with -s, --server, since 1.6).",
                "args.login": "Login into your account, this will cache your session.",
                "args.login.microsoft": "Log-in using Microsoft account.",
                "args.logout": "Logout and invalidate a session.",
                "args.logout.microsoft": "Logout from a Microsoft account.",
                "args.addon": "Addons management subcommands.",
                "args.addon.list": "List addons.",
                "args.addon.init": "For developers: Given an addon's name, initialize its package if it doesn't already exists.",
                "args.addon.init.single_file": "Make a single-file addon instead of a package one.",
                "args.addon.show": "Show an addon details.",

                "abort": "=> Abort",
                "continue_using_main_dir": "Continue using this main directory ({})? (y/N) ",
                "http_request_error": "HTTP request error: {}",

                "cmd.search.pending": "Searching for version '{}'...",
                "cmd.search.pending_local": "Searching for local version '{}'...",
                "cmd.search.pending_all": "Searching for all versions...",
                "cmd.search.result": "=> {:10s} {:16s} {:24s} {}",
                "cmd.search.result.more.local": "[LOCAL]",
                "cmd.search.not_found": "=> No version found",

                "cmd.logout.pending": "Logging out from {}...",
                "cmd.logout.microsoft.pending": "Logging out from {} (Microsoft account)...",
                "cmd.logout.success": "=> Logged out.",
                "cmd.logout.unknown_session": "=> This session is not cached.",

                "cmd.addon.list.title": "Addons list ({}):",
                "cmd.addon.list.result": "=> {:20s} v{} by {} [{}]",
                "cmd.addon.init.already_exits": "An addon '{}' already exists at '{}'.",
                "cmd.addon.init.done": "The addon '{}' was initialized at '{}'.",
                "cmd.addon.show.unknown": "No addon named '{}' exists.",
                "cmd.addon.show.title": "Addon {} ({}):",
                "cmd.addon.show.version": "=> Version: {}",
                "cmd.addon.show.authors": "=> Authors: {}",
                "cmd.addon.show.description": "=> Description: {}",
                "cmd.addon.show.requires": "=> Requires: {}",

                "download.downloading": "Downloading",
                "download.downloaded": "Downloaded {} files, {} in {:.1f}s.",
                "download.errors": "{} Errors happened, can't continue.",
                "download.error.not_found": "Not found",
                "download.error.invalid_size": "Invalid size",
                "download.error.invalid_sha1": "Invalid SHA1",

                "auth.refreshing": "Invalid session, refreshing...",
                "auth.refreshed": "Session refreshed for {}.",
                "auth.validated": "Session validated for {}.",
                "auth.caching": "Caching your session...",
                "auth.logged_in": "Logged in",

                "auth.yggdrasil": "Authenticating {} with Mojang...",
                "auth.yggdrasil.enter_password": "         Password: ",
                "auth.error.yggdrasil": "{}",

                "auth.microsoft": "Authenticating {} with Microsoft...",
                "auth.microsoft.no_browser": "Failed to open Microsoft login page, no web browser is supported.",
                "auth.microsoft.opening_browser_and_listening": "Opened authentication page in browser...",
                "auth.microsoft.failed_to_authenticate": "Failed to authenticate.",
                "auth.microsoft.processing": "Processing authentication against Minecraft services...",
                "auth.microsoft.incoherent_data": "Incoherent authentication data, please retry.",
                "auth.error.microsoft.inconsistent_user_hash": "Inconsistent user hash.",
                "auth.error.microsoft.does_not_own_minecraft": "This account does not own Minecraft.",
                "auth.error.microsoft.outdated_token": "The token is no longer valid.",
                "auth.error.microsoft.error": "Misc error: {}.",

                "version.resolving": "Resolving version {}... ",
                "version.resolved": "Resolved version {}.",
                "version.jar.loading": "Loading version JAR... ",
                "version.jar.loaded": "Loaded version JAR.",
                "version.error.not_found": "Version {} not found.",
                "version.error.jar_not_found": "Version JAR not found.",
                "assets.checking": "Checking assets... ",
                "assets.checked": "Checked {} assets.",
                "logger.loading": "Loading logger... ",
                "logger.loaded": "Loaded.",
                "logger.loaded_pretty": "Loaded pretty logger.",
                "libraries.loading": "Loading libraries... ",
                "libraries.loaded": "Loaded {} libraries.",
                "jvm.loading": "Loading java... ",
                "jvm.loaded": "Loaded Mojang Java {}.",
                "jvm.error.not_found": "No JVM was found for your platform architecture, use --jvm argument to set the JVM executable of path to it.",
                "jvm.error.unsupported_jvm_arch": "No JVM download was found for your platform architecture '{}', use --jvm argument to set the JVM executable of path to it.",
                "jvm.error.unsupported_jvm_version": "No JVM download was found for version '{}', use --jvm argument to set the JVM executable of path to it.",

                "start.dry": "Dry run, stopping.",
                "start.starting": "Starting game...",
                "start.extracting_natives": "=> Extracting natives...",
                "start.running": "Running...",
                "start.stopped": "Game stopped, clearing natives.",
                "start.run.session": "=> Username: {}, UUID: {}",
                "start.run.command_line": "=> Command line: {}",

            }

        def start(self, in_args):

            self._register_addons()

            parser = self.register_arguments()
            args = parser.parse_args(in_args)
            subcommand = args.subcommand

            if subcommand is None:
                parser.print_help()
                return

            exit(self.start_subcommand(subcommand, args))

        def start_subcommand(self, subcommand: str, args: Namespace) -> int:
            builtin_func_name = "cmd_{}".format(subcommand)
            if hasattr(self, builtin_func_name) and callable(getattr(self, builtin_func_name)):
                return getattr(self, builtin_func_name)(args)
            else:
                return 0

        # Addons management

        def _prepare_addons(self, create_dir: bool):
            if not path.isdir(self._addons_dir):
                if not create_dir:
                    return
                os.mkdir(self._addons_dir)
            addons_init = path.join(self._addons_dir, "__init__.py")
            if not path.isfile(addons_init):
                with open(addons_init, "wt") as fp:
                    fp.write(ADDONS_PKG_INIT_CONTENT)

        def _register_addons(self):
            import importlib
            self._prepare_addons(False)
            if path.isdir(self._addons_dir):
                for addon_name in os.listdir(self._addons_dir):
                    if not addon_name.endswith(".dis") and addon_name not in ("__init__.py", "__pycache__"):
                        if addon_name.endswith(".py"):
                            addon_name = addon_name[:-3]
                        else:
                            addon_path = path.join(self._addons_dir, addon_name)
                            if path.isfile(addon_path) or not path.isfile(path.join(addon_path, "__init__.py")):
                                # If entry was not terminated by ".py" and is a file OR
                                # <name>/__init__.py doesn't exists (maybe not a directory).
                                continue
                        if addon_name in self._addons:
                            self.print("addon.defined_twice", addon_name)
                            continue
                        module = importlib.import_module(f"{ADDONS_DIR}.{addon_name}")
                        if PortableAddon.is_valid(module):
                            self._addons[addon_name] = PortableAddon(module, addon_name)
            for addon in self._addons.values():
                addon.build(self)
            for addon in self._addons.values():
                addon.load()

        # Arguments

        def register_arguments(self) -> ArgumentParser:
            parser = ArgumentParser(allow_abbrev=False, prog="portablemc", description=self.get_message("args"))
            parser.add_argument("--main-dir", help=self.get_message("args.main_dir"), dest="main_dir")
            parser.add_argument("--work-dir", help=self.get_message("args.work_dir"), dest="work_dir")
            self.register_subcommands(parser.add_subparsers(title="subcommands", dest="subcommand"))
            return parser

        def get_arg_main_dir(self, args: Namespace) -> str:
            return self.compute_main_dir(args.main_dir)

        def get_arg_work_dir(self, args: Namespace) -> str:
            return self.compute_work_dir(args.main_dir, args.work_dir)

        def register_subcommands(self, subcommands):
            self.register_search_arguments(subcommands.add_parser("search", help=self.get_message("args.search")))
            self.register_start_arguments(subcommands.add_parser("start", help=self.get_message("args.start")))
            self.register_login_arguments(subcommands.add_parser("login", help=self.get_message("args.login")))
            self.register_logout_arguments(subcommands.add_parser("logout", help=self.get_message("args.logout")))
            self.register_addon_arguments(subcommands.add_parser("addon", help=self.get_message("args.addon")))

        def register_search_arguments(self, parser: ArgumentParser):
            parser.add_argument("-l", "--local", help=self.get_message("args.search.local"), default=False, action="store_true")
            parser.add_argument("input", nargs="?")

        def register_start_arguments(self, parser: ArgumentParser):
            parser.formatter_class = self.new_help_formatter(32)
            parser.add_argument("--dry", help=self.get_message("args.start.dry"), default=False, action="store_true")
            parser.add_argument("--disable-mp", help=self.get_message("args.start.disable_multiplayer"), default=False, action="store_true")
            parser.add_argument("--disable-chat", help=self.get_message("args.start.disable_chat"), default=False, action="store_true")
            parser.add_argument("--demo", help=self.get_message("args.start.demo"), default=False, action="store_true")
            parser.add_argument("--resol", help=self.get_message("args.start.resol"), type=self._decode_resolution, dest="resolution")
            parser.add_argument("--jvm", help=self.get_message("args.start.jvm"), default=None)
            parser.add_argument("--jvm-args", help=self.get_message("args.start.jvm_args"), default=None, dest="jvm_args")
            parser.add_argument("--no-better-logging", help=self.get_message("args.start.no_better_logging"), default=False, action="store_true", dest="no_better_logging")
            parser.add_argument("-t", "--temp-login", help=self.get_message("args.start.temp_login"), default=False, action="store_true", dest="templogin")
            parser.add_argument("-l", "--login", help=self.get_message("args.start.login"))
            parser.add_argument("-m", "--microsoft", help=self.get_message("args.start.microsoft"), default=False, action="store_true")
            parser.add_argument("-u", "--username", help=self.get_message("args.start.username"), metavar="NAME")
            parser.add_argument("-i", "--uuid", help=self.get_message("args.start.uuid"))
            parser.add_argument("-s", "--server", help=self.get_message("args.start.server"))
            parser.add_argument("-p", "--server-port", type=int, help=self.get_message("args.start.server_port"), metavar="PORT")
            parser.add_argument("version", nargs="?", default="release")

        def register_login_arguments(self, parser: ArgumentParser):
            parser.add_argument("-m", "--microsoft", help=self.get_message("args.login.microsoft"), default=False, action="store_true")
            parser.add_argument("email_or_username")

        def register_logout_arguments(self, parser: ArgumentParser):
            parser.add_argument("-m", "--microsoft", help=self.get_message("args.logout.microsoft"), default=False, action="store_true")
            parser.add_argument("email_or_username")

        def register_addon_arguments(self, parser: ArgumentParser):

            subparsers = parser.add_subparsers(title="subcommands", dest="addon_subcommand", required=True)
            subparsers.add_parser("list", help=self.get_message("args.addon.list"))

            init_parser = subparsers.add_parser("init", help=self.get_message("args.addon.init"))
            init_parser.add_argument("--single-file", help=self.get_message("args.addon.init.single_file"), default=False, action="store_true", dest="single_file")
            init_parser.add_argument("addon_name")

            show_parser = subparsers.add_parser("show", help=self.get_message("args.addon.show"))
            show_parser.add_argument("addon_name")

        @staticmethod
        def new_help_formatter(max_help_position: int):

            class CustomHelpFormatter(HelpFormatter):
                def __init__(self, prog):
                    super().__init__(prog, max_help_position=max_help_position)

            return CustomHelpFormatter

        # Builtin subcommands

        def cmd_search(self, args: Namespace) -> int:

            if args.input is None:
                self.print("cmd.search.pending_all")
            else:
                self.print("cmd.search.pending_local" if args.local else "cmd.search.pending", args.input)

            found = False
            for version_type, version_id, version_date, is_local in self.search_mc(self.get_arg_main_dir(args), args.input, local=args.local):
                found = True
                self.print("cmd.search.result",
                           version_type,
                           version_id,
                           self.format_iso_date(version_date),
                           self.get_message("cmd.search.result.more.local") if is_local else "")

            if not found:
                self.print("cmd.search.not_found")
                return EXIT_VERSION_SEARCH_NOT_FOUND
            else:
                return 0

        def cmd_login(self, args: Namespace) -> int:
            email_or_username = args.email_or_username
            work_dir = self.get_arg_work_dir(args)
            entry = self.prompt_authenticate(work_dir, email_or_username, True, args.microsoft)
            return EXIT_AUTHENTICATION_FAILED if entry is None else 0

        def cmd_logout(self, args: Namespace) -> int:
            email_or_username = args.email_or_username
            self.print("cmd.logout.microsoft.pending" if args.microsoft else "cmd.logout.pending", email_or_username)
            auth_db = self.new_auth_database(self.get_arg_work_dir(args))
            auth_db.load()
            session = auth_db.remove(email_or_username, MicrosoftAuthSession if args.microsoft else YggdrasilAuthSession)
            if session is not None:
                session.invalidate()
                auth_db.save()
                self.print("cmd.logout.success")
                return 0
            else:
                self.print("cmd.logout.unknown_session")
                return EXIT_LOGOUT_FAILED

        def cmd_addon(self, args: Namespace) -> int:
            subcommand = args.addon_subcommand
            if subcommand == "list":
                self.print("cmd.addon.list.title", len(self._addons))
                for addon in self._addons.values():
                    self.print("cmd.addon.list.result", addon.name, addon.version, ", ".join(addon.authors), addon.id)
            elif subcommand == "init":
                self._prepare_addons(True)
                addon_file = path.join(self._addons_dir, args.addon_name)
                for check_file in (addon_file, f"{addon_file}.py"):
                    if path.exists(check_file):
                        self.print("cmd.addon.init.already_exits", args.addon_name, check_file)
                        return 0
                if args.single_file:
                    addon_file = f"{addon_file}.py"
                else:
                    os.mkdir(addon_file)
                    addon_file = path.join(addon_file, "__init__.py")
                with open(addon_file, "wt") as fp:
                    fp.write(ADDONS_TPL_INIT_CONTENT.format(name=args.addon_name))
                self.print("cmd.addon.init.done", args.addon_name, addon_file)
            elif subcommand == "show":
                addon_name = args.addon_name
                addon = self._addons.get(addon_name)
                if addon is None:
                    self.print("cmd.addon.show.unknown", addon_name)
                else:
                    self.print("cmd.addon.show.title", addon.name, addon_name)
                    self.print("cmd.addon.show.version", addon.version)
                    self.print("cmd.addon.show.authors", ", ".join(addon.authors))
                    if len(addon.description):
                        self.print("cmd.addon.show.description", addon.description)
                    if len(addon.requires):
                        self.print("cmd.addon.show.requires", ", ".join(addon.requires))
            return 0

        def cmd_start(self, args: Namespace) -> int:

            main_dir = self.get_arg_main_dir(args)
            work_dir = self.get_arg_work_dir(args)

            # Login if needed
            if args.login is not None:
                auth = self.prompt_authenticate(work_dir, args.login, not args.templogin, args.microsoft)
                if auth is None:
                    return EXIT_AUTHENTICATION_FAILED
            else:
                auth = None

            # Decode resolution
            custom_resol = args.resolution  # type: Optional[Tuple[int, int]]
            if custom_resol is not None and len(custom_resol) != 2:
                custom_resol = None

            def runner(proc_args: list, proc_cwd: str, options: dict):
                options["cmd_args"] = args
                self.game_runner(proc_args, proc_cwd, options)

            jvm_args = JVM_ARGS_DEFAULT if args.jvm_args is None else args.jvm_args.split(" ")

            # Actual start
            try:
                self.start_mc_from_cmd(
                    cmd_args=args,
                    main_dir=main_dir,
                    work_dir=work_dir,
                    version=args.version,
                    uuid=args.uuid,
                    username=args.username,
                    auth=auth,
                    jvm=[args.jvm, *jvm_args],
                    dry_run=args.dry,
                    better_logging=not args.no_better_logging,
                    resolution=custom_resol,
                    demo=args.demo,
                    disable_multiplayer=args.disable_mp,
                    disable_chat=args.disable_chat,
                    server_addr=args.server,
                    server_port=args.server_port,
                    runner=runner
                )
            except VersionNotFoundError:
                return EXIT_VERSION_NOT_FOUND
            except JsonRequestError as err:
                self.print("http_request_error", err.args[0])
                return EXIT_HTTP_ERROR
            except DownloadError:
                return EXIT_DOWNLOAD_ERROR
            except JvmLoadingError:
                return EXIT_JVM_LOADING_ERROR

        # Messages

        def get_messages(self) -> Dict[str, str]:
            return self._messages

        def add_message(self, key: str, value: str):
            self._messages[key] = value

        def get_message(self, message_key: str, *args) -> str:
            if not len(message_key):
                return args[0]
            msg = self._messages.get(message_key, message_key)
            try:
                return msg.format(*args)
            except IndexError:
                return msg

        def print(self, message_key: str, *args, traceback: bool = False, end: str = "\n"):
            print(self.get_message(message_key, *args), end=end)
            if traceback:
                import traceback
                traceback.print_exc()

        @contextmanager
        def print_task(self, start_message_key: Optional[str] = None, *start_args):
            len_limit = max(0, shutil.get_terminal_size().columns - 9)
            last_len = 0
            def complete(status: Optional[str], message_key: str, *args):
                nonlocal last_len
                msg = self.get_message(message_key, *args)[:len_limit]
                missing_len = max(0, last_len - len(msg) - 1)
                print("\r        " if status is None else "\r[{:^6s}]".format(status), msg, " " * missing_len, end="", flush=True)
                last_len = len(msg)
            if start_message_key is not None:
                complete("", start_message_key, *start_args)
            try:
                yield complete
            finally:
                print()

        def prompt(self, message_key: str, *args, password: bool = False) -> Optional[str]:
            print(self.get_message(message_key, *args), end="", flush=True)
            try:
                if password:
                    import getpass
                    return getpass.getpass("")
                else:
                    return input("")
            except KeyboardInterrupt:
                return None

        # Addons

        def get_addons(self) -> 'Dict[str, PortableAddon]':
            return self._addons

        def get_addon(self, name: str) -> 'Optional[PortableAddon]':
            return self._addons.get(name)

        # Start mixin

        def start_mc_from_cmd(self, *, cmd_args: Namespace, **kwargs) -> None:
            # Define this method to accept "cmd_args"
            super().start_mc(**kwargs)

        def resolve_version_meta(self, main_dir: str, name: str) -> Tuple[dict, str]:
            with self.print_task("version.resolving", name) as task:
                try:
                    ret = super().resolve_version_meta(main_dir, name)
                    task("OK", "version.resolved", name)
                    return ret
                except VersionNotFoundError:
                    task("FAILED", "version.error.not_found", name)
                    raise

        def ensure_version_jar(self, version_dir: str, version: str, version_meta: dict, dl_list: 'DownloadList') -> 'str':
            with self.print_task("version.jar.loading") as task:
                try:
                    ret = super().ensure_version_jar(version_dir, version, version_meta, dl_list)
                    task("OK", "version.jar.loaded")
                    return ret
                except VersionNotFoundError:
                    task("FAILED", "version.error.jar_not_found")
                    raise

        def ensure_assets(self, assets_dir: str, work_dir: str, version_meta: dict, dl_list: 'DownloadList') -> 'Tuple[str, str, int]':
            with self.print_task("assets.checking") as task:
                ret = super().ensure_assets(assets_dir, work_dir, version_meta, dl_list)
                task("OK", "assets.checked", ret[2])
            return ret

        def ensure_logger(self, assets_dir: str, version_meta: dict, dl_list: 'DownloadList', better_logging: bool) -> 'Optional[str]':
            with self.print_task("logger.loading") as task:
                ret = super().ensure_logger(assets_dir, version_meta, dl_list, better_logging)
                task("OK", "logger.loaded_pretty" if better_logging else "start.loaded_logger")
            return ret

        def ensure_libraries(self, main_dir: str, version_meta: dict, dl_list: 'DownloadList') -> 'Tuple[List[str], List[str]]':
            with self.print_task("libraries.loading") as task:
                ret = super().ensure_libraries(main_dir, version_meta, dl_list)
                task("OK", "libraries.loaded", len(ret[0]) + len(ret[1]))
            return ret

        def ensure_jvm(self, main_dir: str, jvm_version_type: str, dl_list: 'DownloadList') -> 'Optional[Tuple[str, str]]':
            with self.print_task("jvm.loading") as task:
                try:
                    ret = super().ensure_jvm(main_dir, jvm_version_type, dl_list)
                    task("OK", "jvm.loaded", ret[0])
                except JvmLoadingError as err:
                    task("FAILED", "jvm.error.{}".format(err.args[0]))
                    raise
            return ret

        def game_runner(self, proc_args: list, proc_cwd: str, options: dict):
            self.print("", "====================================================")
            self.print("start.run.session", options["username"], options["uuid"])
            self.print("start.run.command_line", " ".join(proc_args))
            subprocess.run(proc_args, cwd=proc_cwd)
            self.print("", "====================================================")

        # Authentication

        def prompt_authenticate(self,
                                work_dir: str,
                                email_or_username: str,
                                cache_in_db: bool,
                                microsoft: bool) -> 'Optional[BaseAuthSession]':

            auth_db = self.new_auth_database(work_dir)
            auth_db.load()

            task_text = "auth.microsoft" if microsoft else "auth.yggdrasil"
            with self.print_task(task_text, email_or_username) as task:
                session = auth_db.get(email_or_username, MicrosoftAuthSession if microsoft else YggdrasilAuthSession)
                if session is not None:
                    try:
                        if not session.validate():
                            task("", "auth.refreshing")
                            session.refresh()
                            auth_db.save()
                            task("OK", "auth.refreshed", email_or_username)
                        else:
                            task("OK", "auth.validated", email_or_username)
                        return session
                    except AuthError as err:
                        task("FAILED", "auth.error.{}".format(err.args[0]), *err.args[1:])
                else:
                    task("..", task_text, email_or_username)

            with self.print_task() as task:
                try:
                    session = self.prompt_microsoft_authenticate(email_or_username, task) if microsoft else self.prompt_yggdrasil_authenticate(email_or_username, task)
                    if session is None:
                        return None
                    if cache_in_db:
                        task("", "auth.caching")
                        auth_db.put(email_or_username, session)
                        auth_db.save()
                    task("OK", "auth.logged_in")
                    return session
                except AuthError as err:
                    task("FAILED", "auth.error.{}".format(err.args[0]), *err.args[1:])
                    return None

        def prompt_yggdrasil_authenticate(self, email_or_username: str, _task) -> 'Optional[YggdrasilAuthSession]':
            password = self.prompt("auth.yggdrasil.enter_password", password=True)
            return None if password is None else YggdrasilAuthSession.authenticate(email_or_username, password)

        def prompt_microsoft_authenticate(self, email: str, task) -> 'Optional[MicrosoftAuthSession]':

            server_port = 12782
            client_id = MS_AZURE_APP_ID
            redirect_auth = "http://localhost:{}".format(server_port)
            code_redirect_uri = "{}/code".format(redirect_auth)
            exit_redirect_uri = "{}/exit".format(redirect_auth)

            nonce = uuid4().hex

            if not webbrowser.open(MicrosoftAuthSession.get_authentication_url(client_id, code_redirect_uri, email, nonce)):
                task("FAILED", "auth.microsoft.no_browser")
                return None

            class AuthServer(HTTPServer):

                def __init__(self):
                    super().__init__(("", server_port), RequestHandler)
                    self.timeout = 0.5
                    self.ms_auth_done = False
                    self.ms_auth_id_token = None  # type: Optional[str]
                    self.ms_auth_code = None      # type: Optional[str]

            class RequestHandler(BaseHTTPRequestHandler):

                server_version = "PortableMC/{}".format(LAUNCHER_VERSION)

                def __init__(self, request: bytes, client_address: Tuple[str, int], auth_server: AuthServer) -> None:
                    super().__init__(request, client_address, auth_server)

                def log_message(self, _format: str, *args: Any):
                    return

                def send_auth_response(self, msg: str):
                    self.end_headers()
                    self.wfile.write("{}{}".format(msg, "\n\nClose this tab and return to the launcher." if cast(AuthServer, self.server).ms_auth_done else "").encode())
                    self.wfile.flush()

                def do_POST(self):
                    if self.path.startswith("/code") and self.headers.get_content_type() == "application/x-www-form-urlencoded":
                        content_length = int(self.headers.get("Content-Length"))
                        qs = url_parse.parse_qs(self.rfile.read(content_length).decode())
                        auth_server = cast(AuthServer, self.server)
                        if "code" in qs and "id_token" in qs:
                            self.send_response(307)
                            # We logout the user directly after authorization, this just clear the browser cache to allow
                            # another user to authenticate with another email after. This doesn't invalide the access token.
                            self.send_header("Location", MicrosoftAuthSession.get_logout_url(client_id, exit_redirect_uri))
                            auth_server.ms_auth_id_token = qs["id_token"][0]
                            auth_server.ms_auth_code = qs["code"][0]
                            self.send_auth_response("Redirecting...")
                        elif "error" in qs:
                            self.send_response(400)
                            auth_server.ms_auth_done = True
                            self.send_auth_response("Error: {} ({}).".format(qs["error_description"][0], qs["error"][0]))
                        else:
                            self.send_response(404)
                            self.send_auth_response("Missing parameters.")
                    else:
                        self.send_response(404)
                        self.send_auth_response("Unexpected page.")

                def do_GET(self):
                    auth_server = cast(AuthServer, self.server)
                    if self.path.startswith("/exit"):
                        self.send_response(200)
                        auth_server.ms_auth_done = True
                        self.send_auth_response("Logged in.")
                    else:
                        self.send_response(404)
                        self.send_auth_response("Unexpected page.")

            task("", "auth.microsoft.opening_browser_and_listening")

            try:
                with AuthServer() as server:
                    while not server.ms_auth_done:
                        server.handle_request()
            except KeyboardInterrupt:
                pass

            if server.ms_auth_code is None:
                task("FAILED", "auth.microsoft.failed_to_authenticate")
                return None
            else:
                task("", "auth.microsoft.processing")
                if MicrosoftAuthSession.check_token_id(server.ms_auth_id_token, email, nonce):
                    return MicrosoftAuthSession.authenticate(client_id, server.ms_auth_code, code_redirect_uri)
                else:
                    task("FAILED", "auth.microsoft.incoherent_dat")
                    return None

        # Downloading

        def download_files(self, lst: 'DownloadList', *, progress_callback: 'Optional[Callable[[DownloadProgress], None]]' = None):

            start_time = time.perf_counter()
            last_print_time = None  # type: Optional[float]
            called_once = False

            dl_text = self.get_message("download.downloading")

            columns = min(80, shutil.get_terminal_size().columns)
            path_len = columns - 21 - len(dl_text)

            def _progress_callback(progress: 'DownloadProgress'):
                nonlocal called_once, last_print_time
                now = time.perf_counter()
                if last_print_time is None or (now - last_print_time) > 0.1:
                    last_print_time = now
                    speed = self.format_bytes(int(progress.size / (now - start_time)))
                    percentage = progress.size / progress.total * 100
                    entries = ", ".join((entry.name for entry in progress.entries))
                    real_path_len = path_len - len(speed)
                    print("[      ] {} {} {:6.2f}% {}/s\r".format(dl_text, entries[:real_path_len].ljust(real_path_len), percentage, speed), end="")
                    called_once = True
                    if progress_callback is not None:
                        progress_callback(progress)

            def _complete_task(error: bool = False):
                if called_once:
                    result_text = self.get_message("download.downloaded", lst.count, self.format_bytes(lst.size).lstrip(" "), time.perf_counter() - start_time)
                    if error:
                        result_text = self.get_message("download.errors", result_text)
                    result_len = columns - 9
                    template = "\r[FAILED] {}" if error else "\r[  OK  ] {}"
                    print(template.format(result_text[:result_len].ljust(result_len)))

            try:
                lst.callbacks.insert(0, _complete_task)
                super().download_files(lst, progress_callback=_progress_callback)
            except DownloadError as err:
                _complete_task(True)
                for entry_url, entry_error in err.args[0]:
                    print("         {}: {}", entry_url, self.get_message("download.error.{}".format(entry_error)))
                raise
            finally:
                lst.callbacks.pop(0)

        # Miscellaneous utilities

        @staticmethod
        def _decode_resolution(raw: str):
            return tuple(int(size) for size in raw.split("x"))

        @staticmethod
        def format_iso_date(raw: Union[str, float]) -> str:
            if isinstance(raw, float):
                return datetime.fromtimestamp(raw).strftime("%c")
            else:
                return datetime.strptime(str(raw).rsplit("+", 2)[0], "%Y-%m-%dT%H:%M:%S").strftime("%c")

        @staticmethod
        def format_bytes(n: int) -> str:
            """ Return a byte with suffix B, kB, MB and GB. The string is always 7 chars unless the size exceed 1 TB. """
            if n < 1000:
                return "{:6d}B".format(int(n))
            elif n < 1000000:
                return "{:5.1f}kB".format(int(n / 100) / 10)
            elif n < 1000000000:
                return "{:5.1f}MB".format(int(n / 100000) / 10)
            else:
                return "{:5.1f}GB".format(int(n / 100000000) / 10)


    class TaskPrinter:

        def __enter__(self):
            pass

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

        pass


    class PortableAddon:

        def __init__(self, module: Any, name: str):

            if not self.is_valid(module):
                raise ValueError("Missing 'addon_build' method.")

            self.module = module
            self.id = name
            self.name = str(module.NAME) if hasattr(module, "NAME") else name
            self.version = str(module.VERSION) if hasattr(module, "VERSION") else "unknown"
            self.authors = module.AUTHORS if hasattr(module, "AUTHORS") else tuple()
            self.requires = module.REQUIRES if hasattr(module, "REQUIRES") else tuple()
            self.description = str(module.DESCRIPTION) if hasattr(module, "DESCRIPTION") else ""

            if not isinstance(self.authors, tuple):
                self.authors = (str(self.authors),)

            if not isinstance(self.requires, tuple):
                self.requires = (str(self.requires),)

            self.built = False
            self.instance: Optional[Any] = None

        @staticmethod
        def is_valid(module: Any) -> bool:
            return hasattr(module, "addon_build") and callable(module.addon_build)

        def build(self, pmc: PortableMC):

            from importlib import import_module

            for requirement in self.requires:
                if requirement.startswith("addon:"):
                    requirement = requirement[6:]
                    if pmc.get_addon(requirement) is None:
                        pmc.print("addon.missing_requirement.ext", self.name, requirement)
                else:
                    try:
                        import_module(requirement)
                    except ModuleNotFoundError:
                        pmc.print("addon.missing_requirement.module", self.name, requirement)
                        return False

            try:
                self.instance = self.module.addon_build(pmc)
                self.built = True
            except (Exception,):
                pmc.print("addon.failed_to_build", self.name, traceback=True)

        def load(self):
            if self.built and hasattr(self.instance, "load") and callable(self.instance.load):
                self.instance.load()

    PortableMC().start(sys.argv[1:])
