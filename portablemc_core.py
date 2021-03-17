#!/usr/bin/env python
# encoding: utf8
from sys import exit
import sys


if sys.version_info[0] < 3 or sys.version_info[1] < 6:
    print("PortableMC cannot be used with Python version prior to 3.6.x")
    exit(1)


from typing import cast, Dict, Callable, Optional, Generator, Tuple, List
from urllib import request as url_request
from json.decoder import JSONDecodeError
from urllib.error import HTTPError
from zipfile import ZipFile
from uuid import uuid4
from os import path
import subprocess
import platform
import hashlib
import atexit
import shutil
import json
import re
import os


LAUNCHER_NAME = "portablemc"
LAUNCHER_VERSION = "1.1.0"
LAUNCHER_AUTHORS = "Théo Rozier"

VERSION_MANIFEST_URL = "https://launchermeta.mojang.com/mc/game/version_manifest.json"
ASSET_BASE_URL = "https://resources.download.minecraft.net/{}/{}"
AUTHSERVER_URL = "https://authserver.mojang.com/{}"

LOGGING_CONSOLE_REPLACEMENT = "<PatternLayout pattern=\"%d{HH:mm:ss.SSS} [%t] %-5level %logger{36} - %msg%n\"/>"


# This file is splitted between the Core which is the lib and the CLI launcher which extends the Core.
# Check at the end of this file (in the __main__ check) for the CLI launcher.
# Addons only apply to the CLI, the core lib may be extracted and published as a python lib in the future.


class CorePortableMC:

    def __init__(self):

        self._main_dir: Optional[str] = None

        self._mc_os = self.get_minecraft_os()
        self._mc_arch = self.get_minecraft_arch()
        self._mc_archbits = self.get_minecraft_archbits()

        self._version_manifest: Optional[VersionManifest] = None
        self._auth_database: Optional[AuthDatabase] = None
        self._download_buffer: Optional[bytearray] = None

    # Generic methods

    def init_main_dir(self, main_dir: Optional[str]) -> bool:
        self._main_dir = self.get_minecraft_dir() if main_dir is None else path.realpath(main_dir)
        return path.isdir(self._main_dir)

    def make_main_dir(self):
        os.makedirs(self._main_dir, 0o777, True)

    def core_search(self, search: Optional[str], *, local: bool = False) -> list:

        no_version = (search is None)
        versions_dir = path.join(self._main_dir, "versions")
        versions = []

        if local:
            for version_id in os.listdir(versions_dir):
                if no_version or search in version_id:
                    version_jar_file = path.join(versions_dir, version_id, f"{version_id}.jar")
                    if path.isfile(version_jar_file):
                        versions.append((
                            {"type": "unknown", "id": version_id, "releaseTime": path.getmtime(version_jar_file)}, False
                        ))
        else:
            manifest = self.get_version_manifest()
            for version_data in manifest.all_versions() if no_version else manifest.search_versions(search):
                version_id = version_data["id"]
                version_jar_file = path.join(versions_dir, version_id, f"{version_id}.jar")
                versions.append((version_data, path.isfile(version_jar_file)))

        return versions

    def core_start(self, *,
                   work_dir: str,
                   dry_run: bool,
                   uuid: str,
                   username: str,
                   version: str,
                   jvm: str,
                   no_better_logging: bool = False,
                   work_dir_bin: bool = False,
                   resolution: 'Optional[Tuple[int, int]]' = None,
                   demo: bool = False,
                   disable_multiplayer: bool = False,
                   disable_chat: bool = False,
                   server_addr: Optional[str] = None,
                   server_port: Optional[int] = None,
                   auth_entry: 'Optional[AuthEntry]' = None,
                   version_meta_modifier: 'Optional[Callable[[dict], None]]' = None,
                   libraries_modifier: 'Optional[Callable[[List[str], List[str]], None]]' = None,
                   args_modifier: 'Optional[Callable[[List[str], int], None]]' = None,
                   args_replacement_modifier: 'Optional[Callable[[Dict[str, str]], None]]' = None,
                   runner: 'Optional[Callable[[list, str], None]]' = None) -> None:

        # This method can raise these errors:
        # - VersionNotFoundError: if the given version was not found
        # - URLError: for any URL resolving error
        # - DownloadCorruptedError: if a download is corrupted

        self.notice("start.welcome")

        # Resolve version metadata
        version, version_alias = self.get_version_manifest().filter_latest(version)
        version_meta, version_dir = self.resolve_version_meta_recursive(version)

        # Starting version dependencies resolving
        version_type = version_meta["type"]
        self.notice("start.loading_version", version_type, version)

        if callable(version_meta_modifier):
            version_meta_modifier(version_meta)

        # JAR file loading
        self.notice("start.loading_jar_file")
        version_jar_file = path.join(version_dir, "{}.jar".format(version))
        if not path.isfile(version_jar_file):
            version_downloads = version_meta["downloads"]
            if "client" not in version_downloads:
                self.notice("start.no_client_jar_file")
                raise VersionNotFoundError()
            download_entry = DownloadEntry.from_version_meta_info(version_downloads["client"], version_jar_file, name="{}.jar".format(version))
            self.download_file(download_entry)

        # Assets loading
        self.notice("start.loading_assets")
        assets_dir = path.join(self._main_dir, "assets")
        assets_indexes_dir = path.join(assets_dir, "indexes")
        assets_index_version = version_meta["assets"]
        assets_index_file = path.join(assets_indexes_dir, "{}.json".format(assets_index_version))
        assets_index = None

        if path.isfile(assets_index_file):
            with open(assets_index_file, "rb") as assets_index_fp:
                try:
                    assets_index = json.load(assets_index_fp)
                except JSONDecodeError:
                    self.notice("start.failed_to_decode_asset_index")

        if assets_index is None:
            asset_index_info = version_meta["assetIndex"]
            asset_index_url = asset_index_info["url"]
            self.notice("start.found_asset_index", asset_index_url)
            assets_index = self.read_url_json(asset_index_url)
            if not path.isdir(assets_indexes_dir):
                os.makedirs(assets_indexes_dir, 0o777, True)
            with open(assets_index_file, "wt") as assets_index_fp:
                json.dump(assets_index, assets_index_fp)

        assets_objects_dir = path.join(assets_dir, "objects")
        assets_total_size = version_meta["assetIndex"]["totalSize"]
        assets_current_size = 0
        assets_virtual_dir = path.join(assets_dir, "virtual", assets_index_version)
        assets_mapped_to_resources = assets_index.get("map_to_resources", False)  # For version <= 13w23b
        assets_virtual = assets_index.get("virtual", False)  # For 13w23b < version <= 13w48b (1.7.2)

        if assets_mapped_to_resources:
            self.notice("start.legacy_assets", path.join(work_dir, "resources"))
        if assets_virtual:
            self.notice("start.virtual_assets", assets_virtual_dir)

        self.notice("start.verifying_assets")
        for asset_id, asset_obj in assets_index["objects"].items():

            asset_hash = asset_obj["hash"]
            asset_hash_prefix = asset_hash[:2]
            asset_size = asset_obj["size"]
            asset_hash_dir = path.join(assets_objects_dir, asset_hash_prefix)
            asset_file = path.join(asset_hash_dir, asset_hash)

            if not path.isfile(asset_file) or path.getsize(asset_file) != asset_size:
                os.makedirs(asset_hash_dir, 0o777, True)
                asset_url = ASSET_BASE_URL.format(asset_hash_prefix, asset_hash)
                download_entry = DownloadEntry(asset_url, asset_size, asset_hash, asset_file, name=asset_id)
                self.download_file(download_entry,
                                   start_size=assets_current_size,
                                   total_size=assets_total_size)
            else:
                assets_current_size += asset_size

            if assets_mapped_to_resources:
                resources_asset_file = path.join(work_dir, "resources", asset_id)
                if not path.isfile(resources_asset_file):
                    os.makedirs(path.dirname(resources_asset_file), 0o777, True)
                    shutil.copyfile(asset_file, resources_asset_file)

            if assets_virtual:
                virtual_asset_file = path.join(assets_virtual_dir, asset_id)
                if not path.isfile(virtual_asset_file):
                    os.makedirs(path.dirname(virtual_asset_file), 0o777, True)
                    shutil.copyfile(asset_file, virtual_asset_file)

        # Logging configuration
        self.notice("start.loading_logger")
        logging_arg = None
        if "logging" in version_meta:
            version_logging = version_meta["logging"]
            if "client" in version_logging:
                log_config_dir = path.join(assets_dir, "log_configs")
                os.makedirs(log_config_dir, 0o777, True)
                client_logging = version_logging["client"]
                logging_file_info = client_logging["file"]
                logging_file = path.join(log_config_dir, logging_file_info["id"])
                logging_dirty = False
                download_entry = DownloadEntry.from_version_meta_info(logging_file_info, logging_file,
                                                                      name=logging_file_info["id"])
                if not path.isfile(logging_file) or path.getsize(logging_file) != download_entry.size:
                    self.download_file(download_entry)
                    logging_dirty = True
                if not no_better_logging:
                    better_logging_file = path.join(log_config_dir, "portablemc-{}".format(logging_file_info["id"]))
                    if logging_dirty or not path.isfile(better_logging_file):
                        self.notice("start.generating_better_logging_config")
                        with open(logging_file, "rt") as logging_fp:
                            with open(better_logging_file, "wt") as custom_logging_fp:
                                raw = logging_fp.read() \
                                    .replace("<XMLLayout />", LOGGING_CONSOLE_REPLACEMENT) \
                                    .replace("<LegacyXMLLayout />", LOGGING_CONSOLE_REPLACEMENT)
                                custom_logging_fp.write(raw)
                    logging_file = better_logging_file
                logging_arg = client_logging["argument"].replace("${path}", logging_file)

        # Libraries and natives loading
        self.notice("start.loading_libraries")
        libraries_dir = path.join(self._main_dir, "libraries")
        classpath_libs = [version_jar_file]
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
                    self.notice("start.no_download_for_library", lib_name)
                    continue

                lib_path = path.join(libraries_dir, lib_dl_info["path"])
                lib_dir = path.dirname(lib_path)

                os.makedirs(lib_dir, 0o777, True)
                download_entry = DownloadEntry.from_version_meta_info(lib_dl_info, lib_path, name=lib_name)

                if not path.isfile(lib_path) or path.getsize(lib_path) != download_entry.size:
                    self.download_file(download_entry)

            else:

                # If no 'downloads' trying to parse the maven dependency string "<group>:<product>:<version>
                # to directory path. This may be used by custom configuration that do not provide download
                # links like Optifine.

                lib_name_parts = lib_name.split(":")
                lib_path = path.join(libraries_dir, *lib_name_parts[0].split("."), lib_name_parts[1],
                                     lib_name_parts[2], "{}-{}.jar".format(lib_name_parts[1], lib_name_parts[2]))
                lib_type = "classpath"

                if not path.isfile(lib_path):
                    self.notice("start.cached_library_not_found", lib_name, lib_path)
                    continue

            if lib_type == "classpath":
                classpath_libs.append(lib_path)
            elif lib_type == "native":
                native_libs.append(lib_path)

        if callable(libraries_modifier):
            libraries_modifier(classpath_libs, native_libs)

        # Don't run if dry run
        if dry_run:
            self.notice("start.dry")
            return

        # Start game
        self.notice("start.starting")

        # Extracting binaries
        bin_dir = path.join(work_dir if work_dir_bin else self._main_dir, "bin", str(uuid4()))

        @atexit.register
        def _bin_dir_cleanup():
            if path.isdir(bin_dir):
                shutil.rmtree(bin_dir)

        self.notice("start.extracting_natives")
        for native_lib in native_libs:
            with ZipFile(native_lib, 'r') as native_zip:
                for native_zip_info in native_zip.infolist():
                    if self.can_extract_native(native_zip_info.filename):
                        native_zip.extract(native_zip_info, bin_dir)

        features = {
            "is_demo_user": demo,
            "has_custom_resolution": resolution is not None
        }

        legacy_args = version_meta.get("minecraftArguments")

        raw_args = []
        raw_args.extend(
            self.interpret_args(version_meta["arguments"]["jvm"] if legacy_args is None else LEGACY_JVM_ARGUMENTS,
                                features))

        if logging_arg is not None:
            raw_args.append(logging_arg)

        main_class = version_meta["mainClass"]
        if main_class == "net.minecraft.launchwrapper.Launch":
            # raw_args.append("-Dminecraft.client.jar={}".format(version_jar_file))
            main_class = "net.minecraft.client.Minecraft"

        main_class_idx = len(raw_args)
        raw_args.append(main_class)
        raw_args.extend(self.interpret_args(version_meta["arguments"]["game"],
                                            features) if legacy_args is None else legacy_args.split(" "))

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

        # Arguments replacements
        start_args_replacements = {
            # Game
            "auth_player_name": username,
            "version_name": version,
            "game_directory": work_dir,
            "assets_root": assets_dir,
            "assets_index_name": assets_index_version,
            "auth_uuid": uuid,
            "auth_access_token": "" if auth_entry is None else auth_entry.format_token_argument(False),
            "user_type": "mojang",
            "version_type": version_type,
            # Game (legacy)
            "auth_session": "notok" if auth_entry is None else auth_entry.format_token_argument(True),
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

        start_args = [jvm]
        for arg in raw_args:
            for repl_id, repl_val in start_args_replacements.items():
                arg = arg.replace("${{{}}}".format(repl_id), repl_val)
            start_args.append(arg)

        self.notice("start.running")
        os.makedirs(work_dir, 0o777, True)

        if runner is None:
            subprocess.run(start_args, cwd=work_dir)
        else:
            runner(start_args, work_dir)

        self.notice("start.stopped")

    # Lazy variables getters

    def get_main_dir(self) -> str:
        return self._main_dir

    def get_version_manifest(self) -> 'VersionManifest':
        if self._version_manifest is None:
            self._version_manifest = VersionManifest.load_from_url()
        return self._version_manifest

    def get_auth_database(self) -> 'AuthDatabase':
        if self._auth_database is None:
            self._auth_database = AuthDatabase(path.join(self._main_dir, "portablemc_tokens"))
        return self._auth_database

    def get_download_buffer(self) -> bytearray:
        if self._download_buffer is None:
            self._download_buffer = bytearray(32768)
        return self._download_buffer

    # Public methods to be replaced by addons

    def notice(self, key: str, *args):
        pass

    def mixin(self, target: str, func, owner: Optional[object] = None):
        if owner is None:
            owner = self
        old_func = getattr(owner, target, None)
        def wrapper(*args, **kwargs):
            return func(old_func, *args, **kwargs)
        setattr(owner, target, wrapper)

    # General utilities

    def download_file(self,
                      entry: 'DownloadEntry', *,
                      start_size: int = 0,
                      total_size: int = 0,
                      progress_callback: Optional[Callable[[int, int, int, int], None]] = None) -> int:

        with url_request.urlopen(entry.url) as req:
            with open(entry.dst, "wb") as dst_fp:

                dl_sha1 = hashlib.sha1()
                dl_size = 0

                buffer = self.get_download_buffer()

                while True:

                    read_len = req.readinto(buffer)
                    if not read_len:
                        break

                    buffer_view = buffer[:read_len]
                    dl_size += read_len
                    dl_sha1.update(buffer_view)
                    dst_fp.write(buffer_view)

                    if total_size != 0:
                        start_size += read_len

                    if progress_callback is not None:
                        progress_callback(dl_size, entry.size, start_size, total_size)

                if dl_size != entry.size:
                    raise DownloadCorruptedError("invalid_size")
                elif dl_sha1.hexdigest() != entry.sha1:
                    raise DownloadCorruptedError("invalid_sha1")
                else:
                    return start_size

    # Version metadata

    def resolve_version_meta(self, name: str) -> Tuple[dict, str]:

        version_dir = path.join(self._main_dir, "versions", name)
        version_meta_file = path.join(version_dir, "{}.json".format(name))
        content = None

        self.notice("version.resolving", name)

        if path.isfile(version_meta_file):
            self.notice("version.found_cached")
            with open(version_meta_file, "rb") as version_meta_fp:
                try:
                    content = json.load(version_meta_fp)
                    self.notice("version.loaded")
                except JSONDecodeError:
                    self.notice("version.failed_to_decode_cached")

        if content is None:
            version_data = self.get_version_manifest().get_version(name)
            if version_data is not None:
                version_url = version_data["url"]
                self.notice("version.found_in_manifest")
                content = self.read_url_json(version_url)
                os.makedirs(version_dir, 0o777, True)
                with open(version_meta_file, "wt") as version_meta_fp:
                    json.dump(content, version_meta_fp, indent=2)
            else:
                self.notice("version.not_found_in_manifest")
                raise VersionNotFoundError(name)

        return content, version_dir

    def resolve_version_meta_recursive(self, name: str) -> Tuple[dict, str]:
        version_meta, version_dir = self.resolve_version_meta(name)
        while "inheritsFrom" in version_meta:
            self.notice("version.parent_version", version_meta["inheritsFrom"])
            parent_meta, _ = self.resolve_version_meta(version_meta["inheritsFrom"])
            if parent_meta is None:
                self.notice("version.parent_version_not_found", version_meta["inheritsFrom"])
                raise VersionNotFoundError(version_meta["inheritsFrom"])
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
    def get_minecraft_os() -> str:
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
        return "x86" if machine == "i386" else "x86_64" if machine in ("x86_64", "amd64") else "unknown"

    @staticmethod
    def get_minecraft_archbits() -> Optional[str]:
        raw_bits = platform.architecture()[0]
        return "64" if raw_bits == "64bit" else "32" if raw_bits == "32bit" else None

    @staticmethod
    def get_classpath_separator() -> str:
        return ";" if sys.platform == "win32" else ":"

    @staticmethod
    def read_url_json(url: str) -> dict:
        return json.load(url_request.urlopen(url))

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
        return cls(CorePortableMC.read_url_json(VERSION_MANIFEST_URL))

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


class AuthEntry:

    def __init__(self, client_token: str, username: str, uuid: str, access_token: str):
        self.client_token = client_token
        self.username = username
        self.uuid = uuid  # No dashes
        self.access_token = access_token

    def format_token_argument(self, legacy: bool) -> str:
        if legacy:
            return "token:{}:{}".format(self.access_token, self.uuid)
        else:
            return self.access_token

    def validate(self) -> bool:
        return self.auth_request("validate", {
            "accessToken": self.access_token,
            "clientToken": self.client_token
        }, False)[0] == 204

    def refresh(self):

        _, res = self.auth_request("refresh", {
            "accessToken": self.access_token,
            "clientToken": self.client_token
        })

        self.access_token = res["accessToken"]

    def invalidate(self):
        self.auth_request("invalidate", {
            "accessToken": self.access_token,
            "clientToken": self.client_token
        }, False)

    @classmethod
    def authenticate(cls, email_or_username: str, password: str) -> 'AuthEntry':

        _, res = cls.auth_request("authenticate", {
            "agent": {
                "name": "Minecraft",
                "version": 1
            },
            "username": email_or_username,
            "password": password,
            "clientToken": uuid4().hex
        })

        return AuthEntry(
            res["clientToken"],
            res["selectedProfile"]["name"],
            res["selectedProfile"]["id"],
            res["accessToken"]
        )

    @staticmethod
    def auth_request(req: str, payload: dict, error: bool = True) -> (int, dict):

        from http.client import HTTPResponse
        from urllib.request import Request

        req_url = AUTHSERVER_URL.format(req)
        data = json.dumps(payload).encode("ascii")
        req = Request(req_url, data, headers={
            "Content-Type": "application/json",
            "Content-Length": len(data)
        }, method="POST")

        try:
            res = url_request.urlopen(req)  # type: HTTPResponse
        except HTTPError as err:
            res = cast(HTTPResponse, err.fp)

        try:
            res_data = json.load(res)
        except JSONDecodeError:
            res_data = {}

        if error and res.status != 200:
            raise AuthError(res_data["errorMessage"])

        return res.status, res_data


class AuthDatabase:

    def __init__(self, filename: str):
        self._filename = filename
        self._entries = {}  # type: Dict[str, AuthEntry]

    def load(self):
        self._entries.clear()
        if path.isfile(self._filename):
            with open(self._filename, "rt") as fp:
                for line in fp.readlines():
                    parts = line.split(" ")
                    if len(parts) == 5:
                        self._entries[parts[0]] = AuthEntry(
                            parts[1],
                            parts[2],
                            parts[3],
                            parts[4]
                        )

    def save(self):
        with open(self._filename, "wt") as fp:
            fp.writelines(("{} {} {} {} {}".format(
                email_or_username,
                entry.client_token,
                entry.username,
                entry.uuid,
                entry.access_token
            ) for email_or_username, entry in self._entries.items()))

    def get_entry(self, email_or_username: str) -> Optional[AuthEntry]:
        return self._entries.get(email_or_username, None)

    def add_entry(self, email_or_username: str, entry: AuthEntry):
        self._entries[email_or_username] = entry

    def remove_entry(self, email_or_username: str):
        if email_or_username in self._entries:
            del self._entries[email_or_username]


class DownloadEntry:

    __slots__ = "url", "size", "sha1", "dst", "name"

    def __init__(self, url: str, size: int, sha1: str, dst: str, *, name: Optional[str] = None):
        self.url = url
        self.size = size
        self.sha1 = sha1
        self.dst = dst
        self.name = url if name is None else name

    @classmethod
    def from_version_meta_info(cls, info: dict, dst: str, *, name: Optional[str] = None) -> 'DownloadEntry':
        return DownloadEntry(info["url"], info["size"], info["sha1"], dst, name=name)


class AuthError(Exception): ...
class VersionNotFoundError(Exception): ...
class DownloadCorruptedError(Exception): ...


LEGACY_JVM_ARGUMENTS = [
    {
        "rules": [
            {
                "action": "allow",
                "os": {
                    "name": "osx"
                }
            }
        ],
        "value": [
            "-XstartOnFirstThread"
        ]
    },
    {
        "rules": [
            {
                "action": "allow",
                "os": {
                    "name": "windows"
                }
            }
        ],
        "value": "-XX:HeapDumpPath=MojangTricksIntelDriversForPerformance_javaw.exe_minecraft.exe.heapdump"
    },
    {
        "rules": [
            {
                "action": "allow",
                "os": {
                    "name": "windows",
                    "version": "^10\\."
                }
            }
        ],
        "value": [
            "-Dos.name=Windows 10",
            "-Dos.version=10.0"
        ]
    },
    "-Djava.library.path=${natives_directory}",
    "-Dminecraft.launcher.brand=${launcher_name}",
    "-Dminecraft.launcher.version=${launcher_version}",
    "-cp",
    "${classpath}"
]


