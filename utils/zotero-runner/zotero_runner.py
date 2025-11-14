import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional

from .prefs_manager import PrefsManager
from .preference import DEFAULT_PREFS
from .remote_zotero import RemoteFirefox, find_free_tcp_port

logger = logging.getLogger(__name__)

DEFAULT_OPTIONS = {
    "binary": {
        "args": [],
        "devtools": True,
    },
    "profile": {
        "path": "./.scaffold/profile",
        "dataDir": "",
        "createIfMissing": True,
        "customPrefs": {},
    },
    "plugins": {
        "asProxy": False,
        "list": [],
    },
}

def deep_merge(source, destination):
    """Deep merge two dictionaries."""
    for key, value in source.items():
        if isinstance(value, dict):
            node = destination.setdefault(key, {})
            deep_merge(value, node)
        else:
            destination[key] = value
    return destination

class ZoteroRunner:
    def __init__(self, options: Dict[str, Any]):
        self.options = deep_merge(DEFAULT_OPTIONS, options)
        self.remote_firefox = RemoteFirefox()
        self.zotero_process: Optional[subprocess.Popen] = None

        if not self.options.get("binary", {}).get("path"):
            raise ValueError("Binary path must be provided.")

        if not self.options["profile"]["path"] and not self.options["profile"]["dataDir"]:
            self.options["profile"]["dataDir"] = "./.scaffold/data"
        
        logger.debug(f"ZoteroRunner initialized with options: {self.options}")

    @property
    def default_profile_path(self) -> str:
        return DEFAULT_OPTIONS["profile"]["path"]

    async def run(self):
        await self._setup_profile()
        await self._start_zotero_instance()

        if not self.options["plugins"]["asProxy"]:
            await self._install_temporary_plugins()

    async def _setup_profile(self):
        profile_opts = self.options["profile"]
        profile_path = Path(profile_opts["path"]).resolve()

        if not profile_path.exists():
            if profile_opts["createIfMissing"]:
                logger.debug(f"Creating profile at {profile_path}...")
                profile_path.mkdir(parents=True, exist_ok=True)
            else:
                raise FileNotFoundError(
                    "The 'profile.path' must be provided when 'createIfMissing' is false."
                )

        prefs_path = profile_path / "prefs.js"
        prefs_manager = PrefsManager("user_pref")
        prefs_manager.set_prefs(DEFAULT_PREFS)
        prefs_manager.set_prefs(profile_opts["customPrefs"])
        
        if prefs_path.exists():
            await prefs_manager.read(str(prefs_path))
        
        prefs_manager.set_prefs({
            "extensions.lastAppBuildId": None,
            "extensions.lastAppVersion": None,
        })
        await prefs_manager.write(str(prefs_path))

        if self.options["plugins"]["asProxy"]:
            await self._install_proxy_plugins()

    async def _start_zotero_instance(self):
        binary_opts = self.options["binary"]
        profile_opts = self.options["profile"]
        
        args: List[str] = [binary_opts["path"], "--purgecaches", "no-remote"]
        if profile_opts["path"]:
            args.extend(["-profile", os.path.abspath(profile_opts["path"])])
        if profile_opts["dataDir"]:
            args.extend(["--dataDir", os.path.abspath(profile_opts["dataDir"])])
        if binary_opts["devtools"]:
            args.append("--jsdebugger")
        if binary_opts["args"]:
            args.extend(binary_opts["args"])

        remote_port = find_free_tcp_port()
        args.extend(["-start-debugger-server", str(remote_port)])

        logger.debug(f"Zotero start args: {args}")

        env = os.environ.copy()
        env.update({
            "XPCOM_DEBUG_BREAK": "stack",
            "NS_TRACE_MALLOC_DISABLE_STACKS": "1",
        })

        if not os.path.exists(binary_opts["path"]):
            raise FileNotFoundError("The Zotero binary not found.")

        self.zotero_process = subprocess.Popen(
            args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        logger.debug(f"Zotero started, pid: {self.zotero_process.pid}")

        # Allow Zotero time to start the debugger server
        await asyncio.sleep(5)

        logger.debug("Connecting to the remote Firefox debugger...")
        await self.remote_firefox.connect(remote_port)
        logger.debug(f"Connected to the remote Firefox debugger on port: {remote_port}")

    async def _install_temporary_plugins(self):
        for plugin in self.options["plugins"]["list"]:
            install_result = await self.remote_firefox.install_temporary_addon(
                os.path.abspath(plugin["sourceDir"])
            )
            addon_id = install_result.get("addon", {}).get("id")
            if not addon_id:
                raise RuntimeError("Unexpected missing addonId in install result")
            logger.debug(f"Temporarily installed plugin {addon_id}")

    async def _install_proxy_plugin(self, plugin_id: str, source_dir: str):
        profile_path = Path(self.options["profile"]["path"])
        addon_proxy_file = profile_path / "extensions" / plugin_id
        build_path = os.path.abspath(source_dir)

        addon_proxy_file.parent.mkdir(exist_ok=True)
        with open(addon_proxy_file, "w") as f:
            f.write(build_path)
        logger.debug(f"Addon proxy file updated: {addon_proxy_file} -> {build_path}")

        addon_xpi_file = profile_path / "extensions" / f"{plugin_id}.xpi"
        if addon_xpi_file.exists():
            os.remove(addon_xpi_file)
            logger.debug("XPI file found and removed.")

        addon_info_file = profile_path / "extensions.json"
        if addon_info_file.exists():
            with open(addon_info_file, "r+") as f:
                content = json.load(f)
                for addon in content.get("addons", []):
                    if addon.get("id") == plugin_id and not addon.get("active", True):
                        addon["active"] = True
                        addon["userDisabled"] = False
                        logger.debug(f"Activated plugin {plugin_id} in extensions.json.")
                f.seek(0)
                json.dump(content, f)
                f.truncate()

    async def _install_proxy_plugins(self):
        for plugin in self.options["plugins"]["list"]:
            await self._install_proxy_plugin(plugin["id"], plugin["sourceDir"])

    async def reload_temporary_plugin_by_id(self, plugin_id: str):
        await self.remote_firefox.reload_addon(plugin_id)

    async def reload_temporary_plugin_by_source_dir(self, source_dir: str) -> dict:
        addon_id = next((p["id"] for p in self.options["plugins"]["list"] if p["sourceDir"] == source_dir), None)

        if not addon_id:
            return {
                "sourceDir": source_dir,
                "reloadError": ValueError(f"No addonId mapped to '{source_dir}'"),
            }
        
        try:
            await self.remote_firefox.reload_addon(addon_id)
            return {"sourceDir": source_dir, "reloadError": None}
        except Exception as e:
            return {"sourceDir": source_dir, "reloadError": e}

    async def _reload_all_temporary_plugins(self):
        for plugin in self.options["plugins"]["list"]:
            res = await self.reload_temporary_plugin_by_source_dir(plugin["sourceDir"])
            if res["reloadError"]:
                logger.error(res["reloadError"])

    def reload_proxy_plugin_by_ztoolkit(self, plugin_id: str, name: str, version: str):
        # This method is more complex to port directly due to its reliance on
        # Zotero-specific JS execution via a URL. A similar approach can be taken.
        # For simplicity, this is a placeholder.
        logger.warning("reload_proxy_plugin_by_ztoolkit is not fully implemented in this Python port.")
        reload_script = f"""
        (async () => {{
            Services.obs.notifyObservers(null, "startupcache-invalidate", null);
            const {{ AddonManager }} = ChromeUtils.import("resource://gre/modules/AddonManager.jsm");
            const addon = await AddonManager.getAddonByID("{plugin_id}");
            await addon.reload();
        }})()
        """
        # The command execution part would be similar to the original.
        # exec_sync(command)

    async def _reload_all_proxy_plugins(self):
        for plugin in self.options["plugins"]["list"]:
            self.reload_proxy_plugin_by_ztoolkit(plugin["id"], plugin["id"], plugin["id"])
            await asyncio.sleep(2)

    async def reload_all_plugins(self):
        if self.options["plugins"]["asProxy"]:
            await self._reload_all_proxy_plugins()
        else:
            await self._reload_all_temporary_plugins()

    def exit(self):
        if self.zotero_process:
            self.zotero_process.kill()
        kill_zotero()

def is_running(process_name: str) -> bool:
    """Check if a process with the given name is running."""
    try:
        if sys.platform == "win32":
            output = subprocess.check_output(["tasklist"], text=True)
            return process_name in output
        else:
            output = subprocess.check_output(["pgrep", "-f", process_name], text=True)
            return bool(output.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def kill_zotero():
    """Force kills the Zotero process based on the OS."""
    def kill():
        try:
            if "ZOTERO_PLUGIN_KILL_COMMAND" in os.environ:
                subprocess.run(os.environ["ZOTERO_PLUGIN_KILL_COMMAND"], shell=True, check=True)
            elif sys.platform == "win32":
                subprocess.run("taskkill /f /im zotero.exe", shell=True, check=True)
            elif sys.platform == "darwin": # macOS
                # Using pkill is safer than parsing ps output
                subprocess.run("pkill -9 -f zotero", shell=True, check=True)
            elif sys.platform.startswith("linux"):
                subprocess.run("pkill -9 zotero", shell=True, check=True)
            else:
                logger.error("No kill command found for this operating system.")
        except subprocess.CalledProcessError:
            # This can happen if the process is already gone, which is fine.
            logger.debug("Kill command failed, process likely already terminated.")
        except Exception as e:
            logger.error(f"Kill Zotero failed: {e}")

    if is_running("zotero"):
        kill()
    else:
        logger.warning("No Zotero instance is currently running.")