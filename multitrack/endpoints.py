"""Private Linux listener paths shared by the gateway and process launcher."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACKS = ("coding", "multimodal")


def backend_socket(track):
    if track not in TRACKS:
        raise ValueError("Unknown socket backend")
    default = ROOT / "data/runtime" / f"{track}.sock"
    return Path(os.getenv(f"{track.upper()}_BACKEND_SOCKET", default))


def gateway_sockets():
    # Windows development can still use explicit TCP targets or mock transports.
    return {track: str(backend_socket(track)) for track in TRACKS} if os.name == "posix" else {}
