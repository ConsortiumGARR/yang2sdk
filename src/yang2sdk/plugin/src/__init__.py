"""yang2sdk Pyang Plugin."""

from yang2sdk.plugin.src.core import pyang_plugin_init, Yang2Restconf

# Expose the initialization function so Pyang can discover and load the plugin
__all__ = ["pyang_plugin_init", "Yang2Restconf"]
