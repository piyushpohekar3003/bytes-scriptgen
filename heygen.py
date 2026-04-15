"""
HeyGen API client — avatar list, voice list, submit video, poll status, download MP4.
Docs: https://docs.heygen.com/reference/api-reference
"""
import os
import time
from typing import Optional, Dict, Any, List

import requests


def _api_key() -> str:
    return os.environ.get("HEYGEN_API_KEY", "")


def _headers_v2() -> Dict[str, str]:
    return {
        "X-API-KEY": _api_key(),
        "Content-Type": "application/json",
    }


# ──────────────────────────────────────────────────────────────────────
# List avatars / voices (cached in-memory for 10 min)
# ──────────────────────────────────────────────────────────────────────

_cache: Dict[str, Any] = {}


def _cached(key: str, ttl: int, fetch_fn):
    now = time.time()
    entry = _cache.get(key)
    if entry and entry["expires_at"] > now:
        return entry["value"]
    value = fetch_fn()
    _cache[key] = {"value": value, "expires_at": now + ttl}
    return value


def list_avatars() -> List[Dict[str, Any]]:
    """
    Fetch CUSTOM (private) avatars only — from the user's own avatar groups.
    Skips HeyGen's public template library.
    Returns flattened list of avatars across all custom groups.
    """
    def fetch():
        # Step 1: list all avatar groups
        r = requests.get(
            "https://api.heygen.com/v2/avatar_group.list",
            headers=_headers_v2(),
            timeout=15,
        )
        r.raise_for_status()
        groups_data = r.json()
        groups = groups_data.get("data", {}).get("avatar_group_list", [])

        # Filter to PRIVATE / PHOTO groups (user's custom avatars)
        custom_groups = [g for g in groups if g.get("group_type") in ("PRIVATE", "PHOTO")]

        # Step 2: for each group, fetch its avatars
        all_avatars = []
        for g in custom_groups:
            group_id = g.get("id")
            group_name = g.get("name", "Custom")
            group_default_voice = g.get("default_voice_id")
            try:
                gr = requests.get(
                    f"https://api.heygen.com/v2/avatar_group/{group_id}/avatars",
                    headers=_headers_v2(),
                    timeout=15,
                )
                if gr.status_code != 200:
                    continue
                avatars = gr.json().get("data", {}).get("avatar_list", [])
                # Tag each with group info for the UI
                for a in avatars:
                    a["group_name"] = group_name
                    a["group_id"] = group_id
                    if group_default_voice and not a.get("default_voice_id"):
                        a["default_voice_id"] = group_default_voice
                all_avatars.extend(avatars)
            except Exception as e:
                print(f"[heygen] failed to fetch group {group_id}: {e}")
                continue

        return all_avatars
    return _cached("avatars", ttl=600, fetch_fn=fetch)


def list_voices() -> List[Dict[str, Any]]:
    """Fetch all available voices."""
    def fetch():
        r = requests.get(
            "https://api.heygen.com/v2/voices",
            headers=_headers_v2(),
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:
            return data["data"].get("voices", [])
        return []
    return _cached("voices", ttl=600, fetch_fn=fetch)


# ──────────────────────────────────────────────────────────────────────
# Submit video for generation
# ──────────────────────────────────────────────────────────────────────

def strip_script_markers(script: str) -> str:
    """
    The A1 Bytes script has visual markers like [Avatar + Stock Card] and | Supers: ...
    These are for the editor, NOT to be spoken. Extract just the voiceover.
    """
    import re
    lines = []
    for raw_line in script.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        # Remove [Avatar + ...] / [B-roll + ...] prefix
        line = re.sub(r"^\[[^\]]+\]\s*", "", line)
        # Remove | Supers: ... suffix
        line = re.sub(r"\s*\|\s*Supers?\s*:.*$", "", line, flags=re.IGNORECASE)
        line = line.strip()
        if line:
            lines.append(line)
    return " ".join(lines)


def submit_video(
    script: str,
    avatar_id: str,
    voice_id: str,
    dimension: Optional[Dict[str, int]] = None,
) -> str:
    """
    Submit a video for generation. Returns video_id.
    """
    if dimension is None:
        dimension = {"width": 1080, "height": 1920}  # vertical 9:16 for reels

    voiceover_text = strip_script_markers(script)

    payload = {
        "video_inputs": [
            {
                "character": {
                    "type": "avatar",
                    "avatar_id": avatar_id,
                    "avatar_style": "normal",
                },
                "voice": {
                    "type": "text",
                    "input_text": voiceover_text,
                    "voice_id": voice_id,
                },
            }
        ],
        "dimension": dimension,
    }

    r = requests.post(
        "https://api.heygen.com/v2/video/generate",
        headers=_headers_v2(),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()

    # Handle error response
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"HeyGen submit error: {data['error']}")

    # Extract video_id
    if isinstance(data, dict) and "data" in data:
        return data["data"].get("video_id", "")
    raise RuntimeError(f"Unexpected HeyGen response: {data}")


# ──────────────────────────────────────────────────────────────────────
# Poll status
# ──────────────────────────────────────────────────────────────────────

def poll_video(video_id: str) -> Dict[str, Any]:
    """
    Get the status of a video generation job.
    Returns {status, video_url?, thumbnail_url?, duration?, error?}
    Status values: 'pending' | 'processing' | 'waiting' | 'completed' | 'failed'
    """
    r = requests.get(
        f"https://api.heygen.com/v1/video_status.get",
        params={"video_id": video_id},
        headers={"X-API-KEY": _api_key()},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "data" in data:
        inner = data["data"]
        return {
            "status": inner.get("status", "unknown"),
            "video_url": inner.get("video_url"),
            "thumbnail_url": inner.get("thumbnail_url"),
            "duration": inner.get("duration"),
            "error": inner.get("error"),
        }
    return {"status": "unknown", "error": str(data)}


# Note: We don't store videos. The HeyGen CDN URL is kept in the job log instead.
# When the user clicks Download, the server streams it from HeyGen on-the-fly.
