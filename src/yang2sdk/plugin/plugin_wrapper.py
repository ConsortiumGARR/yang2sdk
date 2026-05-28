import os
import sys

from yang2sdk.plugin.src import pyang_plugin_init as real_init

# # Add package to Python path so internal imports work
# plugin_root = os.path.dirname(os.path.abspath(__file__))
# if plugin_root not in sys.path:
#     sys.path.insert(0, plugin_root)


pyang_plugin_init = real_init
