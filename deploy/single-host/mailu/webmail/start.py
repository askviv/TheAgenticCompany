#!/usr/bin/env python3
"""Preserve Mailu's configured Roundcube plugins and add TAC correlation."""

import os
import runpy


configured = os.environ.get("ROUNDCUBE_PLUGINS", "")
if configured.strip():
    plugins = [name.strip() for name in configured.split(",") if name.strip()]
else:
    plugins = ["archive", "zipdownload", "markasjunk", "managesieve", "enigma", "carddav"]
if "tac_correlation" not in plugins:
    plugins.append("tac_correlation")
os.environ["ROUNDCUBE_PLUGINS"] = ",".join(plugins)

runpy.run_path("/start-mailu.py", run_name="__main__")
