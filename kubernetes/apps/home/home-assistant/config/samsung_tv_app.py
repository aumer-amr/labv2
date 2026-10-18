"""Report the foreground Samsung TV app using read-only LAN requests."""

import json
from concurrent.futures import ThreadPoolExecutor
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

TV_URL = "http://10.40.125.202:8001/api/v2/applications/"
# shortcut: installed apps captured from this TV; refresh IDs when its apps change.
APP_IDS = (
    "111299001912",  # YouTube
    "3201606009684",  # Spotify
    "3201901017640",  # Disney+
    "3201511006428",  # Rakuten TV
    "20172100006",  # e-Manual
    "11101200001",  # Netflix
    "3201512006785",  # Prime Video
    "3201706014250",  # NPO
    "3201803015869",  # Canal Digitaal
    "3201506003175",  # Pathe Thuis
    "3201909019271",  # Privacyvoorkeuren
    "11111300404",  # Viaplay
    "3202106024097",  # Apple TV
    "3202207027987",  # Pathé Thuis
    "3202208028071",  # SkyShowtime
    "3202203026693",  # CANAL+ App
    "3202104023374",  # Samsung TV Plus Privacy Settings
)


def read_app(app_id):
    try:
        with build_opener(ProxyHandler({})).open(TV_URL + app_id, timeout=2) as response:
            data = json.loads(response.read(65536))
        if (
            isinstance(data, dict)
            and isinstance(data.get("visible"), bool)
            and isinstance(data.get("name"), str)
            and 0 < len(data["name"]) <= 255
        ):
            return data
    except (URLError, OSError, ValueError):
        pass
    return None


def current_app():
    with ThreadPoolExecutor(max_workers=4) as pool:
        apps = [app for app in pool.map(read_app, APP_IDS) if app is not None]
    visible = {app["name"] for app in apps if app["visible"]}
    return {
        "app": next(iter(visible)) if len(visible) == 1 else "unknown",
        "available": bool(apps),
    }


if __name__ == "__main__":
    print(json.dumps(current_app()))
