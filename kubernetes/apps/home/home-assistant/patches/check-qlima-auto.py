"""Check a patched profile with Tuya Local's installed mapping parser; no device I/O."""

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import yaml

# Import the parser without initializing the integration or its device connections.
root = Path(sys.argv[1])
for name, path in (
    ("custom_components", root.parent),
    ("custom_components.tuya_local", root),
):
    module = ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module

from custom_components.tuya_local.helpers.device_config import TuyaEntityConfig

profile = yaml.safe_load(Path(sys.argv[2]).read_text())
fan = next(entity for entity in profile["entities"] if entity["entity"] == "fan")
config = TuyaEntityConfig(Mock(), fan)
speed = config.find_dps("speed")
preset = config.find_dps("preset_mode")
device = Mock()
state = {"1": True, "4": "auto", "103": "mid", "105": "dehum30_air"}
device.get_property.side_effect = state.get

for mode in ("dehum_air", "dehum30_air"):
    state["105"] = mode
    for raw, percentage in (("auto", None), ("low", 33), ("mid", 67), ("high", 100)):
        state["4"] = raw
        assert speed.get_value(device) == percentage, (mode, raw)
        assert preset.get_value(device) == ("auto" if raw == "auto" else None)
        assert speed.values(device) == [33, 67, 100]
        assert preset.values(device) == ["auto"]
        assert preset.get_values_to_set(device, "auto") == {"4": "auto"}
        for manual, value in (("low", 33), ("mid", 67), ("high", 100)):
            assert speed.get_values_to_set(device, value) == {"4": manual}

# Purifier speed still redirects to DP103 and does not report the DP4 Auto preset.
state["105"] = "airpurify"
state["4"] = "auto"
assert preset.get_value(device) is None
assert preset.values(device) == []
for raw, percentage in (("low", 33), ("mid", 67), ("high", 100)):
    state["103"] = raw
    assert speed.get_value(device) == percentage
    assert speed.get_values_to_set(device, percentage) == {"103": raw}

print("PASS: Auto preset, manual speeds, write mappings and purifier redirection")
