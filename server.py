import re
import os
import json
import yt_dlp
import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
COOKIEFILE = os.getenv("COOKIEFILE", "")
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64; rv:146.0) Gecko/20100101 Firefox/146.0",
)
COMBINED_FORMAT_MATCH = r"\[([^\+]+=[^\+]+)\+.*\]"


class ErrorLogger:
    def __init__(self):
        self.count = 0
        self.errors = []
        self.warnings = []

    def debug(self, msg):
        pass

    def warning(self, msg):
        self.warnings.append(msg)

    def error(self, msg):
        self.errors.append(msg)


# ---------- /info (with fix) ----------
async def info(request):
    url = request.query_params["url"]
    media_format = request.query_params.get("format", "")
    user_agent = request.query_params.get("user-agent", USER_AGENT)

    if re.search(COMBINED_FORMAT_MATCH, media_format):
        in_brackets = False
        i = len(media_format)
        while i > 0:
            i -= 1
            c = media_format[i]
            if c == "[":
                in_brackets = False
            elif c == "]":
                in_brackets = True
            elif in_brackets and c == "+":
                media_format = media_format[:i] + "][" + media_format[i + 1 :]

    if not url:
        return JSONResponse({"success": False, "errors": ["No URL specified"]})

    logger = ErrorLogger()
    opts = {
        "quiet": True,
        "noplaylist": True,
        "logger": logger,
        "no_color": True,
        "http_headers": {"User-Agent": user_agent},
    }
    if media_format:
        opts["format"] = media_format
    if COOKIEFILE:
        opts["cookiefile"] = COOKIEFILE

    with YoutubeDL(opts) as ytdl:
        try:
            data = ytdl.extract_info(url, download=False)
        except Exception as e:
            return JSONResponse({
                "success": False,
                "errors": [str(e)],
                "warnings": logger.warnings,
                "data": [],
            })

        if not data:
            return JSONResponse({
                "success": False,
                "errors": logger.errors or ["No data returned"],
                "warnings": logger.warnings,
                "data": [],
            })

        # Normalise: always work with a list
        entries = [data] if "entries" not in data else data["entries"]

        # Filter out entries that don't have a direct URL
        valid_entries = []
        for entry in entries:
            if "url" in entry and entry["url"]:
                valid_entries.append(entry)
            else:
                logger.warning(f"No direct URL for {entry.get('title', 'unknown')}")

        if not valid_entries:
            return JSONResponse({
                "success": False,
                "errors": logger.errors or ["No playable URL found"],
                "warnings": logger.warnings,
                "data": [],
            })

        return JSONResponse({
            "success": True,
            "errors": logger.errors,
            "warnings": logger.warnings,
            "data": valid_entries,
        })


# ---------- /search (unchanged) ----------
async def search_handler(request):
    q = request.query_params.get("q")
    page_token = request.query_params.get("pageToken")
    if not q:
        return JSONResponse({"error": "Missing q parameter"}, status_code=400)

    params = {
        "part": "snippet",
        "maxResults": 20,
        "q": q,
        "type": "video",
        "key": YOUTUBE_API_KEY,
    }
    if page_token:
        params["pageToken"] = page_token

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://www.googleapis.com/youtube/v3/search", params=params
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return JSONResponse({"error": f"Search failed: {e}"}, status_code=500)

    items = data.get("items", [])
    video_ids = [i["id"]["videoId"] for i in items]

    durations = {}
    if video_ids:
        try:
            dur_params = {
                "part": "contentDetails",
                "id": ",".join(video_ids),
                "key": YOUTUBE_API_KEY,
            }
            async with httpx.AsyncClient() as client:
                dur_resp = await client.get(
                    "https://www.googleapis.com/youtube/v3/videos", params=dur_params
                )
                dur_resp.raise_for_status()
                dur_data = dur_resp.json()
            for item in dur_data.get("items", []):
                durations[item["id"]] = item["contentDetails"]["duration"]
        except Exception:
            pass

    videos = []
    for item in items:
        snippet = item["snippet"]
        video_id = item["id"]["videoId"]
        videos.append({
            "videoId": video_id,
            "title": snippet["title"],
            "author": snippet["channelTitle"],
            "thumbnail": (
                snippet["thumbnails"]["high"]["url"]
                if "high" in snippet["thumbnails"]
                else snippet["thumbnails"]["default"]["url"]
            ),
            "duration": durations.get(video_id, "Unknown"),
        })

    return JSONResponse({
        "videos": videos,
        "nextPageToken": data.get("nextPageToken"),
    })


routes = [
    Route("/", endpoint=info, methods=["GET"]),
    Route("/info", endpoint=info, methods=["GET"]),
    Route("/search", endpoint=search_handler, methods=["GET"]),
]

app = Starlette(routes=routes)
