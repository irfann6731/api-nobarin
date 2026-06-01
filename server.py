import os
import re
import sys
import asyncio
import logging
import time
from typing import Optional

# Add the local src directory to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))

# ── CRITICAL: Set env defaults BEFORE importing moviebox_api ──────────────────
# The library reads os.getenv() at module import time to build HOST_URL.
# These MUST be set before `from moviebox_api...` to take effect.
os.environ.setdefault("MOVIEBOX_API_HOST", "h5.aoneroom.com")
os.environ.setdefault("MOVIEBOX_API_HOST_V2", "h5-api.aoneroom.com")
os.environ.setdefault("MOVIEBOX_SITE_HOST", "moviebox.id")  # host= param di URL upstream
os.environ["TZ"] = "Asia/Jakarta"  # Force, karena Vercel default ":UTC"
os.environ.setdefault("COUNTRY", "ID")
os.environ.setdefault("REGION", "ID")
os.environ.setdefault("LOCALE", "id-ID")
# ─────────────────────────────────────────────────────────────────────────────

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse
import httpx
from urllib.parse import quote

# ── Logging setup ─────────────────────────────────────────────────────────────
logger = logging.getLogger("moviebox-server")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_handler)

from moviebox_api.v2 import Homepage, Search, ItemDetails, Session
from moviebox_api.v2.download import DownloadableMovieFilesDetail, DownloadableTVSeriesFilesDetail
from moviebox_api.v1.constants import SubjectType
from moviebox_api.v2.constants import DOWNLOAD_REQUEST_HEADERS

# ── Region lock: selalu kirim sebagai Indonesia ──────────────────────────────
# Header eksplisit agar upstream API mengenali request sebagai dari Indonesia.
# Jika upstream memakai GeoIP (bukan header) untuk menentukan konten,
# maka solusi final adalah menjalankan backend di VPS Indonesia
# atau server dengan IP Indonesia.
INDONESIA_HEADERS = {
    **DOWNLOAD_REQUEST_HEADERS,
    "X-Client-Info": '{"timezone":"Asia/Jakarta"}',
    "upstream": "indonesia",
    "Accept": "application/json",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "X-Forwarded-For": "182.8.250.15",
    "X-Real-IP": "182.8.250.15",
    "Client-IP": "182.8.250.15",
    "CF-Connecting-IP": "182.8.250.15",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
}

_global_session = None

def get_global_session() -> Session:
    """Buat atau kembalikan global Session untuk HTTP connection pooling."""
    global _global_session
    if _global_session is None:
        _global_session = Session(headers=INDONESIA_HEADERS, timeout=30.0)
    return _global_session
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="Moviebox Local API Server", version="1.0.0")

# Enable CORS for frontend clients (Astro app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_event():
    global _global_session
    if _global_session is not None:
        await _global_session.aclose()

@app.middleware("http")
async def add_cache_control_header(request: Request, call_next):
    response = await call_next(request)
    
    # Hanya cache request GET untuk endpoint API v1
    if request.method == "GET" and request.url.path.startswith("/api/v1/"):
        # Jangan cache debug-region atau search atau play-media
        bypass_paths = ["/api/v1/debug-region", "/api/v1/search", "/api/v1/play-media", "/api/v1/play", "/api/v1/player", "/api/v1/sub"]
        if not any(path in request.url.path for path in bypass_paths):
            if "/api/v1/detail" in request.url.path:
                # Cache detail film selama 1 jam di browser, 24 jam di Vercel CDN
                response.headers["Cache-Control"] = "public, max-age=3600, s-maxage=86400, stale-while-revalidate=3600"
            else:
                # Cache kategori/trending selama 1 menit di browser, 5 menit di Vercel CDN
                response.headers["Cache-Control"] = "public, max-age=60, s-maxage=300, stale-while-revalidate=600"
                
    return response

# Map endpoint action parameter to Operating List titles
CATEGORY_MAP = {
    "trending": "Trending Movies",
    "indonesian-movies": "Indonesian Killers",
    "indonesian-drama": "Trending Indonesian Drama",
    "kdrama": "K-Drama: New Release",
    "short-tv": "Hot Short TV",
    "anime": "Into Animeverse",
    "adult-comedy": "Grown-Up Giggle",
    "western-tv": "Trending Western",
    "indo-dub": "Dubbing Indonesia",
}

# English/Indonesian alias variations for homepage lists to handle upstream localization
CATEGORY_ALIASES = {
    "trending": ["trending movies", "trending now", "popular series", "popular movie", "sedang tren"],
    "indonesian-movies": ["indonesian killers", "film indonesia lagi ngetren", "film indonesia"],
    "indonesian-drama": ["trending indonesian drama", "favorite sinetron", "sinetron"],
    "kdrama": ["k-drama: new release", "hot k-drama", "k-drama"],
    "short-tv": ["hot short tv", "short tv"],
    "anime": ["into animeverse", "must-watch top100 anime", "animated film", "anime"],
    "adult-comedy": ["grown-up giggle", "adult comedy"],
    "western-tv": ["trending western", "western tv", "western"],
    "indo-dub": ["dubbing indonesia", "must watch indo dubbed", "indo dub"],
}

# Mapping of actions to fallback search keywords for page > 1
SEARCH_FALLBACK_MAP = {
    "trending": ("popular", SubjectType.MOVIES),
    "indonesian-movies": ("indonesia", SubjectType.MOVIES),
    "indonesian-drama": ("indonesia", SubjectType.TV_SERIES),
    "kdrama": ("korean", SubjectType.TV_SERIES),
    "short-tv": ("short", SubjectType.TV_SERIES),
    "anime": ("anime", SubjectType.ALL),
    "adult-comedy": ("comedy", SubjectType.MOVIES),
    "western-tv": ("western", SubjectType.TV_SERIES),
    "indo-dub": ("indonesian dub", SubjectType.ALL),
}

# Simple cache for homepage data to avoid repeated heavy API requests
homepage_cache = {
    "data": None,
    "timestamp": 0
}
CACHE_TTL = 300  # 5 minutes

async def get_cached_homepage(bypass_cache: bool = False):
    now = time.time()
    if (
        not bypass_cache
        and homepage_cache["data"] is not None
        and (now - homepage_cache["timestamp"]) < CACHE_TTL
    ):
        logger.debug("[Homepage] Returning cached data (age=%.1fs)", now - homepage_cache["timestamp"])
        return homepage_cache["data"]
    
    logger.info("[Homepage] Fetching fresh data from upstream (bypass_cache=%s)", bypass_cache)
    session = get_global_session()
    h = Homepage(session)
    content = await h.get_content_model()
    homepage_cache["data"] = content
    homepage_cache["timestamp"] = now
    return content

# Simple cache for movie/TV details (1 hour)
detail_cache = {}
DETAIL_CACHE_TTL = 3600

# Cache for downloadable media metadata / stream links (5 minutes)
media_cache = {}
MEDIA_CACHE_TTL = 300

async def get_cached_detail(detailPath: str):
    import time
    now = time.time()
    if detailPath in detail_cache:
        entry = detail_cache[detailPath]
        if (now - entry["timestamp"]) < DETAIL_CACHE_TTL:
            return entry["data"]
            
    session = get_global_session()
    det = ItemDetails(session)
    details = await det.get_content_model(detailPath)
    detail_cache[detailPath] = {
        "data": details,
        "timestamp": now
    }
    return details

async def get_cached_media_detail(detailPath: str, season: int = None, episode: int = None, is_tv: bool = False):
    import time
    key = (detailPath, season, episode, is_tv)
    now = time.time()
    if key in media_cache:
        entry = media_cache[key]
        if (now - entry["timestamp"]) < MEDIA_CACHE_TTL:
            return entry["data"]
            
    session = get_global_session()
    details = await get_cached_detail(detailPath)
    if is_tv:
        dl = DownloadableTVSeriesFilesDetail(session, details.subject)
        s = season if season is not None else 1
        ep = episode if episode is not None else 1
        dl_meta = await dl.get_content_model(season=s, episode=ep)
    else:
        dl = DownloadableMovieFilesDetail(session, details.subject)
        dl_meta = await dl.get_content_model()
        
    media_cache[key] = {
        "data": dl_meta,
        "timestamp": now
    }
    return dl_meta

def format_item(item):
    # Determine subject type value
    subject_type_val = item.subjectType.value if hasattr(item.subjectType, 'value') else int(item.subjectType)
    
    # Map SubjectType to "tv" or "movie"
    if subject_type_val == 2 or subject_type_val == 7: # TV_SERIES or ANIME
        item_type = "tv"
    else:
        item_type = "movie"
        
    rating = "N/A"
    if hasattr(item, "imdbRatingValue") and item.imdbRatingValue:
        rating = str(item.imdbRatingValue)
        
    year = ""
    if hasattr(item, "releaseDate") and item.releaseDate:
        year = str(item.releaseDate.year)
    elif hasattr(item, "year") and item.year:
        year = str(item.year)
        
    poster = ""
    if hasattr(item, "cover") and item.cover and hasattr(item.cover, "url"):
        poster = str(item.cover.url)
        
    genres = item.genre if hasattr(item, "genre") and item.genre else []
        
    return {
        "title": item.title,
        "poster": poster,
        "year": year,
        "detailPath": item.detailPath,
        "rating": rating,
        "type": item_type,
        "genres": genres
    }

async def fetch_category_items(action: str, page: int = 1, bypass_cache: bool = False):
    if page == 1:
        content = await get_cached_homepage(bypass_cache=bypass_cache)
        target_title = CATEGORY_MAP.get(action)
        if not target_title:
            return []
            
        target_title_clean = re.sub(r'[^a-zA-Z0-9]', '', target_title).lower()
        
        # Match from operatingList
        for op in content.operatingList:
            op_title_clean = re.sub(r'[^a-zA-Z0-9]', '', op.title).lower()
            if op_title_clean == target_title_clean:
                return [format_item(item) for item in op.subjects]
                
        # Fallback alias match (covers Indonesian and other variations)
        aliases = CATEGORY_ALIASES.get(action, [])
        for op in content.operatingList:
            op_title_clean = re.sub(r'[^a-zA-Z0-9]', '', op.title).lower()
            for alias in aliases:
                alias_clean = re.sub(r'[^a-zA-Z0-9]', '', alias).lower()
                if alias_clean in op_title_clean:
                    return [format_item(item) for item in op.subjects]
                    
            if target_title_clean in op_title_clean:
                return [format_item(item) for item in op.subjects]
                
    # Fallback to search query for page > 1 or if category is not found in homepage list
    fallback = SEARCH_FALLBACK_MAP.get(action)
    if fallback:
        query_str, subj_type = fallback
        session = get_global_session()
        search_inst = Search(session, query=query_str, subject_type=subj_type, page=page)
        search_res = await search_inst.get_content_model()
        return [format_item(item) for item in search_res.items]
        
    return []

@app.get("/")
async def root():
    return RedirectResponse(url="/docs")


# ── Debug endpoint: sementara untuk diagnosa region Vercel ────────────────────
@app.get("/api/v1/debug-region")
async def debug_region(request: Request):
    """Endpoint debug sementara untuk mengecek region, IP, env, dan upstream.
    Hapus endpoint ini setelah masalah region terselesaikan."""
    from moviebox_api.v1.constants import HOST_URL as V1_HOST_URL
    from moviebox_api.v1.constants import SELECTED_HOST as V1_SELECTED_HOST
    from moviebox_api.v2.constants import HOST_URL as V2_HOST_URL
    from moviebox_api.v2.constants import SELECTED_HOST as V2_SELECTED_HOST

    # Ambil public IP server
    public_ip = "unknown"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get("https://api.ipify.org?format=json")
            if resp.status_code == 200:
                public_ip = resp.json().get("ip", "unknown")
    except Exception as e:
        public_ip = f"error: {e}"

    # Env variables yang aman ditampilkan (tanpa secret/token)
    safe_env_keys = [
        "VERCEL", "VERCEL_REGION", "VERCEL_ENV", "VERCEL_URL",
        "MOVIEBOX_API_HOST", "MOVIEBOX_API_HOST_V2", "MOVIEBOX_SITE_HOST",
        "BASE_URL", "API_URL", "REGION", "COUNTRY", "LANG", "LOCALE",
        "TZ", "PYTHONPATH",
    ]
    env_info = {}
    for key in safe_env_keys:
        val = os.getenv(key)
        env_info[key] = val  # None jika tidak diset

    # ── Probe upstream homepage API langsung ──────────────────────────────
    site_host = os.getenv("MOVIEBOX_SITE_HOST", "moviebox.id")
    upstream_url = f"{V2_HOST_URL}wefeed-h5api-bff/home?host={site_host}"
    upstream_status = None
    upstream_length = None
    upstream_preview = ""
    upstream_item_count = None
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=INDONESIA_HEADERS) as client:
            up_resp = await client.get(upstream_url)
            upstream_status = up_resp.status_code
            raw_text = up_resp.text
            upstream_length = len(raw_text)
            upstream_preview = raw_text[:300]
            # Coba parse jumlah item dari operatingList
            try:
                data = up_resp.json()
                op_list = data.get("data", {}).get("operatingList", [])
                upstream_item_count = sum(len(op.get("subjects", [])) for op in op_list)
            except Exception:
                upstream_item_count = "parse_error"
    except Exception as e:
        upstream_preview = f"error: {e}"
    # ─────────────────────────────────────────────────────────────────────

    return {
        "vercel": os.getenv("VERCEL"),
        "vercel_region": os.getenv("VERCEL_REGION"),
        "x_vercel_id": request.headers.get("x-vercel-id"),
        "public_ip": public_ip,
        "mirror_hosts": {
            "MOVIEBOX_API_HOST (v1)": V1_SELECTED_HOST,
            "MOVIEBOX_API_HOST_V2 (v2)": V2_SELECTED_HOST,
            "v1_host_url": V1_HOST_URL,
            "v2_host_url": V2_HOST_URL,
        },
        "moviebox_site_host": site_host,
        "final_home_url": upstream_url,
        "upstream_probe": {
            "url": upstream_url,
            "status_code": upstream_status,
            "response_length": upstream_length,
            "preview_300_chars": upstream_preview,
            "total_item_count_in_operatingList": upstream_item_count,
        },
        "environment_variables": env_info,
        "request_headers_subset": {
            "x-forwarded-for": request.headers.get("x-forwarded-for"),
            "x-real-ip": request.headers.get("x-real-ip"),
            "accept-language": request.headers.get("accept-language"),
        },
        "upstream_headers_being_sent": {
            k: v for k, v in INDONESIA_HEADERS.items()
            if k.lower() not in ("cookie", "authorization")
        },
    }
@app.get("/api/v1/home")
async def get_home_metadata():
    categories = []
    for key, name in CATEGORY_MAP.items():
        categories.append({
            "key": key,
            "name": name
        })
    return {
        "success": True,
        "categories": categories
    }

@app.get("/api/v1/trending")
async def get_trending(page: int = 1):
    """
    Endpoint trending — selalu bypass cache agar data segar.

    CATATAN GEOIP:
    Jika setelah region sin1 dan env disamakan, hasil masih berbeda,
    kemungkinan besar upstream memakai GeoIP berdasarkan IP server.
    Local menggunakan IP residensial Indonesia, sedangkan Vercel
    menggunakan IP datacenter Singapore (18.x.x.x / AWS ap-southeast-1).
    Untuk hasil identik, deploy backend API di VPS Indonesia
    (IDCloudHost, Biznet Gio, dll) dan tetap gunakan Vercel untuk frontend.
    """
    from moviebox_api.v2.constants import HOST_URL as V2_HOST_URL
    vercel_region = os.getenv("VERCEL_REGION", "local")
    site_host = os.getenv("MOVIEBOX_SITE_HOST", "moviebox.id")
    upstream_url = f"{V2_HOST_URL}wefeed-h5api-bff/home?host={site_host}"

    try:
        # Gunakan cache agar loading instan
        items = await fetch_category_items("trending", page, bypass_cache=False)

        # ── Logging aman ──────────────────────────────────────────────────
        logger.info(
            "[Trending] region=%s | upstream=%s | page=%d | items_count=%d",
            vercel_region, upstream_url, page, len(items),
        )
        if items:
            preview = str(items[:2])[:300]
            logger.info("[Trending] preview(300 chars): %s", preview)
        else:
            logger.warning("[Trending] 0 items returned from upstream!")
        # ─────────────────────────────────────────────────────────────────

        return {"success": True, "items": items}
    except Exception as e:
        logger.error(
            "[Trending] ERROR region=%s | upstream=%s | %s",
            vercel_region, upstream_url, str(e),
        )
        return {"success": False, "items": [], "error": str(e)}

@app.get("/api/v1/indonesian-movies")
async def get_indonesian_movies(page: int = 1):
    try:
        items = await fetch_category_items("indonesian-movies", page)
        return {"success": True, "items": items}
    except Exception as e:
        return {"success": False, "items": [], "error": str(e)}

@app.get("/api/v1/indonesian-drama")
async def get_indonesian_drama(page: int = 1):
    try:
        items = await fetch_category_items("indonesian-drama", page)
        return {"success": True, "items": items}
    except Exception as e:
        return {"success": False, "items": [], "error": str(e)}

@app.get("/api/v1/kdrama")
async def get_kdrama(page: int = 1):
    try:
        items = await fetch_category_items("kdrama", page)
        return {"success": True, "items": items}
    except Exception as e:
        return {"success": False, "items": [], "error": str(e)}

@app.get("/api/v1/short-tv")
async def get_short_tv(page: int = 1):
    try:
        items = await fetch_category_items("short-tv", page)
        return {"success": True, "items": items}
    except Exception as e:
        return {"success": False, "items": [], "error": str(e)}

@app.get("/api/v1/anime")
async def get_anime(page: int = 1):
    try:
        items = await fetch_category_items("anime", page)
        return {"success": True, "items": items}
    except Exception as e:
        return {"success": False, "items": [], "error": str(e)}

@app.get("/api/v1/adult-comedy")
async def get_adult_comedy(page: int = 1):
    try:
        items = await fetch_category_items("adult-comedy", page)
        return {"success": True, "items": items}
    except Exception as e:
        return {"success": False, "items": [], "error": str(e)}

@app.get("/api/v1/western-tv")
async def get_western_tv(page: int = 1):
    try:
        items = await fetch_category_items("western-tv", page)
        return {"success": True, "items": items}
    except Exception as e:
        return {"success": False, "items": [], "error": str(e)}

@app.get("/api/v1/indo-dub")
async def get_indo_dub(page: int = 1):
    try:
        items = await fetch_category_items("indo-dub", page)
        return {"success": True, "items": items}
    except Exception as e:
        return {"success": False, "items": [], "error": str(e)}

@app.get("/api/v1/search")
async def get_search(q: str = "*", page: int = 1):
    try:
        session = get_global_session()
        # Default to ALL if no query is given, otherwise search
        query_str = q if q != "*" else "movie"
        search_inst = Search(session, query=query_str, subject_type=SubjectType.ALL, page=page)
        search_res = await search_inst.get_content_model()
        items = [format_item(item) for item in search_res.items]
        return {"success": True, "items": items}
    except Exception as e:
        return {"success": False, "items": [], "error": str(e)}

@app.get("/api/v1/detail")
async def get_detail(detailPath: str, request: Request):
    try:
        details = await get_cached_detail(detailPath)
        
        genres = ", ".join(details.subject.genre) if details.subject.genre else "Drama"
        subject_type_val = details.subject.subjectType.value if hasattr(details.subject.subjectType, 'value') else int(details.subject.subjectType)
        is_tv = (subject_type_val == 2 or subject_type_val == 7)
        
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.url.netloc)
        base_url = f"{scheme}://{host}"
        poster_url = str(details.subject.cover.url) if details.subject.cover else ""
        encoded_title = quote(details.subject.title)
        encoded_poster = quote(poster_url)
        
        # We do NOT use .mp4 in media URL to prevent frontend Astro code from rendering HTML5 <video> tag.
        # This forces Astro to render it in an <iframe> using our custom Artplayer.
        if is_tv:
            first_season = 1
            if details.resource and details.resource.seasons:
                first_season = details.resource.seasons[0].se
            media_url = f"{base_url}/api/v1/play-media/{detailPath}/{first_season}/1"
            player_url = f"{base_url}/api/v1/player?detailPath={detailPath}&season={first_season}&episode=1&url={quote(media_url)}&title={encoded_title}%20-%20S{first_season}E1&poster={encoded_poster}"
        else:
            media_url = f"{base_url}/api/v1/play-media/{detailPath}"
            player_url = f"{base_url}/api/v1/player?detailPath={detailPath}&url={quote(media_url)}&title={encoded_title}&poster={encoded_poster}"
        
        movie_data = {
            "title": details.subject.title,
            "year": details.subject.releaseDate.year if details.subject.releaseDate else "",
            "description": details.subject.description or "",
            "poster": poster_url,
            "type": "tv" if is_tv else "movie",
            "genre": genres,
            "rating": str(details.subject.imdbRatingValue) if details.subject.imdbRatingValue else "N/A",
            "playerUrl": player_url
        }
        
        if is_tv and details.resource and details.resource.seasons:
            seasons_list = []
            for season in details.resource.seasons:
                season_num = season.se
                episodes_list = []
                for ep_idx in range(1, season.maxEp + 1):
                    ep_media_url = f"{base_url}/api/v1/play-media/{detailPath}/{season_num}/{ep_idx}"
                    ep_player_url = f"{base_url}/api/v1/player?detailPath={detailPath}&season={season_num}&episode={ep_idx}&url={quote(ep_media_url)}&title={encoded_title}%20-%20S{season_num}E{ep_idx}&poster={encoded_poster}"
                    episodes_list.append({
                        "episode": ep_idx,
                        "url": ep_player_url,
                        "playerUrl": ep_player_url
                    })
                seasons_list.append({
                    "season": season_num,
                    "episodes": episodes_list
                })
            movie_data["seasons"] = seasons_list
            
        return {
            "success": True,
            "data": movie_data
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

@app.get("/api/v1/sub")
async def proxy_subtitle(url: str):
    from urllib.parse import unquote
    target_url = unquote(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://videodownloader.site/",
    }
    client = httpx.AsyncClient(follow_redirects=True, timeout=15.0)
    try:
        resp = await client.get(target_url, headers=headers)
        content = resp.text
        
        # Remove UTF-8 BOM if present
        if content.startswith('\ufeff'):
            content = content[1:]
            
        is_vtt = content.strip().startswith("WEBVTT")
        if not is_vtt:
            # Convert SRT to WebVTT
            content = re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', content)
            content = "WEBVTT\n\n" + content

        return HTMLResponse(
            content=content,
            status_code=resp.status_code,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Content-Type": "text/vtt; charset=utf-8"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await client.aclose()


@app.get("/api/v1/player", response_class=HTMLResponse)
async def artplayer_page(
    url: str,
    request: Request,
    title: str = "Video",
    poster: str = "",
    detailPath: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
):
    import json
    from html import escape
    from urllib.parse import quote

    safe_title = escape(title or "Artplayer", quote=True)
    artplayer_cdn = "https://cdn.jsdelivr.net/npm/artplayer@5.1.7/dist/artplayer.js"
    
    qualities = []
    subtitles = []

    if detailPath:
        try:
            details = await get_cached_detail(detailPath)
            subject_type_val = details.subject.subjectType.value if hasattr(details.subject.subjectType, 'value') else int(details.subject.subjectType)
            is_tv = (subject_type_val == 2 or subject_type_val == 7)

            dl_meta = await get_cached_media_detail(detailPath, season=season, episode=episode, is_tv=is_tv)

            if dl_meta.downloads:
                sorted_downloads = sorted(dl_meta.downloads, key=lambda x: x.resolution, reverse=True)
                sep = "&" if "?" in url else "?"
                for item in sorted_downloads:
                    qualities.append({
                        "resolution": item.resolution,
                        "html": f"{item.resolution}p",
                        "url": f"{url}{sep}resolution={item.resolution}",
                        "size": f"{item.size / (1024 * 1024):.1f} MB" if item.size else ""
                    })

            if dl_meta.captions:
                for caption in dl_meta.captions:
                    proxy_sub_url = f"/api/v1/sub?url={quote(str(caption.url))}"
                    subtitles.append({
                        "id": caption.id,
                        "lan": caption.lan,
                        "name": caption.lanName,
                        "url": proxy_sub_url,
                        "type": "vtt"
                    })
        except Exception as e:
            print(f"Error fetching player details: {e}")

    config = {
        "url": url,
        "title": title or "Video",
        "poster": poster or "",
        "artplayerCdn": artplayer_cdn,
        "qualities": qualities,
        "subtitles": subtitles,
    }

    html = f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover" />
  <title>{safe_title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{
      width: 100%;
      height: 100%;
      margin: 0;
      padding: 0;
      overflow: hidden;
      background: #000;
      font-family: 'Outfit', sans-serif;
    }}
    
    #artplayer-app {{
      width: 100vw;
      height: 100vh;
      background: #000;
      position: relative;
    }}
    
    /* Watermark styling */
    .watermark {{
      position: absolute;
      top: 20px;
      right: 25px;
      font-family: 'Outfit', sans-serif;
      font-size: 16px;
      font-weight: 700;
      letter-spacing: 1.5px;
      color: rgba(255, 255, 255, 0.25);
      text-shadow: 0 2px 4px rgba(0,0,0,0.5);
      z-index: 100 !important;
      pointer-events: none;
      user-select: none;
      text-transform: uppercase;
    }}
    
    /* Nobarin theme styling overrides for Artplayer */
    .art-video-container {{
      border-radius: 12px;
      overflow: hidden;
      border: 1px solid rgba(124, 58, 237, 0.2);
    }}
    
    .art-control-progress-played {{
      background: #7c3aed !important;
    }}
    
    .art-control-progress-indicator {{
      background: #7c3aed !important;
      border: 2px solid #ffffff !important;
    }}
    
    .art-volume-slider-inner {{
      background: #7c3aed !important;
    }}
    
    .art-control:hover svg {{
      color: #a78bfa !important;
    }}
    
    .art-state {{
      background: rgba(124, 58, 237, 0.75) !important;
      border-radius: 50% !important;
    }}
    
    .art-loading-icon {{
      border: 3px solid rgba(124, 58, 237, 0.2) !important;
      border-top: 3px solid #7c3aed !important;
    }}
    
    :root {{
      --sub-size-base: clamp(14px, 4.5vmin, 38px);
      --sub-size-scale: 1.0;
    }}

    .art-subtitle {{
      font-family: 'Outfit', sans-serif !important;
      font-weight: 500 !important;
      font-size: calc(var(--sub-size-base) * var(--sub-size-scale)) !important;
      text-shadow: 0 0 4px #000, 0 0 4px #000, 0 0 6px #000, 0 0 6px #000 !important;
      background: transparent !important;
      bottom: 75px !important;
      z-index: 20 !important;
    }}

    .player-error {{
      position: fixed;
      inset: 0;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      color: #fff;
      background: #000;
      text-align: center;
      z-index: 9999;
    }}
    .player-error.show {{ display: flex; }}
    .player-error-box {{
      max-width: 560px;
      padding: 18px 20px;
      border: 1px solid rgba(124, 58, 237, 0.3);
      border-radius: 14px;
      background: rgba(124, 58, 237, 0.1);
      backdrop-filter: blur(8px);
      line-height: 1.5;
    }}
    
    /* Settings Popover styling */
    .custom-settings-panel {{
      position: absolute;
      right: 12px;
      bottom: 65px;
      width: 280px;
      max-height: calc(100% - 80px);
      overflow-y: auto;
      background: rgba(10, 8, 20, 0.95);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border: 1px solid rgba(124, 58, 237, 0.3);
      border-radius: 10px;
      z-index: 99999 !important;
      transform: translateY(10px);
      transition: transform 0.2s ease, opacity 0.2s ease, visibility 0.2s;
      opacity: 0;
      display: flex;
      flex-direction: column;
      color: #fff;
      font-family: 'Outfit', sans-serif;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.6);
      visibility: hidden;
      pointer-events: none;
    }}
    
    .custom-settings-panel.open {{
      transform: translateY(0);
      opacity: 1;
      visibility: visible;
      pointer-events: auto;
    }}
    
    /* Menu panes */
    .settings-menu-pane {{
      display: none;
      flex-direction: column;
      width: 100%;
      padding: 14px 16px;
    }}
    
    .settings-menu-pane.active {{
      display: flex;
    }}
    
    /* Main Menu Header */
    .main-menu-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 8px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      margin-bottom: 8px;
      font-size: 13px;
      font-weight: 600;
      color: #c084fc;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      user-select: none;
    }}
    
    .close-btn {{
      font-size: 18px;
      cursor: pointer;
      color: #9ca3af;
      line-height: 1;
      transition: color 0.2s;
      padding: 0 4px;
    }}
    
    .close-btn:hover {{
      color: #ffffff;
    }}
    
    /* Header of submenu */
    .menu-header {{
      display: flex;
      align-items: center;
      padding: 5px 0 12px 0;
      font-size: 14px;
      font-weight: 600;
      color: #c084fc; /* purple light */
      cursor: pointer;
      border-bottom: 1px solid rgba(255, 255, 255, 0.08);
      margin-bottom: 10px;
      user-select: none;
    }}
    
    .menu-header:hover {{
      color: #e9d5ff;
    }}
    
    .back-arrow {{
      margin-right: 8px;
      font-size: 18px;
    }}
    
    /* Settings item row */
    .settings-item {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 10px 8px;
      border-radius: 6px;
      cursor: pointer;
      transition: background 0.2s, transform 0.1s;
      margin-bottom: 4px;
      user-select: none;
    }}
    
    .settings-item:hover {{
      background: rgba(124, 58, 237, 0.15);
    }}
    
    .settings-item:active {{
      transform: scale(0.98);
    }}
    
    .item-label {{
      display: flex;
      align-items: center;
      font-size: 13px;
      font-weight: 500;
      color: #f3f4f6;
    }}
    
    .item-icon {{
      margin-right: 10px;
      display: flex;
      align-items: center;
      color: #a78bfa;
    }}
    
    .item-value-container {{
      display: flex;
      align-items: center;
    }}
    
    .item-value {{
      font-size: 12px;
      color: #9ca3af;
      margin-right: 6px;
    }}
    
    .item-arrow {{
      color: #6b7280;
      font-size: 12px;
    }}
    
    /* Submenu options */
    .menu-options-list {{
      display: flex;
      flex-direction: column;
      gap: 4px;
      overflow-y: auto;
      max-height: 200px;
      scrollbar-width: thin;
      scrollbar-color: rgba(124, 58, 237, 0.5) rgba(255, 255, 255, 0.02);
      padding-right: 4px;
    }}
    
    .menu-options-list::-webkit-scrollbar {{
      width: 4px;
    }}
    
    .menu-options-list::-webkit-scrollbar-track {{
      background: rgba(255, 255, 255, 0.02);
      border-radius: 10px;
    }}
    
    .menu-options-list::-webkit-scrollbar-thumb {{
      background: rgba(124, 58, 237, 0.5);
      border-radius: 10px;
    }}
    
    .menu-options-list::-webkit-scrollbar-thumb:hover {{
      background: #7c3aed;
    }}
    
    .menu-option {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 10px 12px;
      border-radius: 5px;
      cursor: pointer;
      font-size: 13px;
      color: #d1d5db;
      transition: background 0.2s;
      user-select: none;
    }}
    
    .menu-option:hover {{
      background: rgba(255, 255, 255, 0.05);
      color: #fff;
    }}
    
    .menu-option.selected {{
      background: rgba(124, 58, 237, 0.2);
      color: #c084fc;
      font-weight: 600;
      border-left: 3px solid #7c3aed;
    }}
    
    .selected-icon {{
      display: none;
      color: #c084fc;
    }}
    
    .menu-option.selected .selected-icon {{
      display: block;
    }}

    @media (max-width: 768px) {{
      .custom-settings-panel {{
        width: 240px !important;
        right: 8px !important;
        bottom: 55px !important;
        max-height: calc(100% - 70px) !important;
      }}
      .settings-menu-pane {{
        padding: 10px 12px !important;
      }}
      .settings-item {{
        padding: 8px 6px !important;
        margin-bottom: 2px !important;
      }}
      .item-label {{
        font-size: 12px !important;
      }}
      .item-value {{
        font-size: 11px !important;
      }}
      .menu-header {{
        font-size: 13px !important;
        padding-bottom: 8px !important;
        margin-bottom: 8px !important;
      }}
      .menu-options-list {{
        max-height: 150px !important;
      }}
      .menu-option {{
        padding: 8px 10px !important;
        font-size: 12px !important;
      }}
    }}
    
    /* Landscape mobile where height is very small */
    @media (max-height: 480px) {{
      .custom-settings-panel {{
        bottom: 50px !important;
        max-height: calc(100% - 60px) !important;
      }}
      .menu-options-list {{
        max-height: 120px !important;
      }}
    }}
  </style>
</head>
<body>
  <div id="artplayer-app"></div>
  <div id="watermark" class="watermark">NOBARIN</div>
  
  <div id="player-error" class="player-error">
    <div class="player-error-box">
      <strong>Video gagal dimuat.</strong><br />
      <span id="player-error-text">Coba refresh halaman atau cek URL video.</span>
    </div>
  </div>

  <!-- Custom Sidebar Settings Menu -->
  <div id="custom-settings-panel" class="custom-settings-panel">
    <!-- Main Menu -->
    <div id="settings-main-menu" class="settings-menu-pane active">
      <div class="main-menu-header">
        <span>Pengaturan</span>
        <span class="close-btn" onclick="closeCustomSettings()">&times;</span>
      </div>
      
      <div class="settings-item" onclick="showSubMenu('speed')">
        <span class="item-label">
          <span class="item-icon">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polygon points="10 8 16 12 10 16 10 8"/></svg>
          </span>
          Play Speed
        </span>
        <span class="item-value-container">
          <span id="val-speed" class="item-value">Normal</span>
          <span class="item-arrow">&gt;</span>
        </span>
      </div>
      
      <div class="settings-item" onclick="showSubMenu('quality')">
        <span class="item-label">
          <span class="item-icon">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><path d="M7 9h4M9 9v6M14 9h3M14 15h3M14 12h2"/></svg>
          </span>
          Quality
        </span>
        <span class="item-value-container">
          <span id="val-quality" class="item-value">Auto</span>
          <span class="item-arrow">&gt;</span>
        </span>
      </div>
      
      <div class="settings-item" onclick="showSubMenu('subtitle')">
        <span class="item-label">
          <span class="item-icon">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2" ry="2"/><line x1="7" y1="8" x2="17" y2="8"/><line x1="7" y1="12" x2="17" y2="12"/><line x1="7" y1="16" x2="13" y2="16"/></svg>
          </span>
          Subtitle
        </span>
        <span class="item-value-container">
          <span id="val-subtitle" class="item-value">Off</span>
          <span class="item-arrow">&gt;</span>
        </span>
      </div>
      
      <div class="settings-item" onclick="showSubMenu('size')">
        <span class="item-label">
          <span class="item-icon">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 7 4 4 20 4 20 7"/><line x1="9" y1="20" x2="15" y2="20"/><line x1="12" y1="4" x2="12" y2="20"/></svg>
          </span>
          Subtitle Size
        </span>
        <span class="item-value-container">
          <span id="val-size" class="item-value">100%</span>
          <span class="item-arrow">&gt;</span>
        </span>
      </div>
      
      <div class="settings-item" onclick="showSubMenu('color')">
        <span class="item-label">
          <span class="item-icon">
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22C17.5228 22 22 17.5228 22 12C22 6.47715 17.5228 2 12 2C6.47715 2 2 6.47715 2 12C2 14.7255 3.09032 17.1962 4.85857 19C5.02845 19.1723 5.2842 19.2319 5.51341 19.1524C5.74261 19.0728 5.91264 18.8654 5.94939 18.6212C6.07923 17.7601 6.81881 17 7.72727 17H8C9.10457 17 10 16.1046 10 15V13.5C10 12.6716 9.32843 12 8.5 12H4.07221C4.02462 12.3275 4 12.6609 4 13C4 17.4183 7.58172 21 12 21V22ZM12 22V21V22Z"/></svg>
          </span>
          Subtitle Color
        </span>
        <span class="item-value-container">
          <span id="val-color" class="item-value">White</span>
          <span class="item-arrow">&gt;</span>
        </span>
      </div>
    </div>
    
    <!-- Sub Menus -->
    <div id="settings-speed-menu" class="settings-menu-pane">
      <div class="menu-header" onclick="showMainMenu()">
        <span class="back-arrow">&lt;</span> Play Speed
      </div>
      <div class="menu-options-list" id="opts-speed"></div>
    </div>
    
    <div id="settings-quality-menu" class="settings-menu-pane">
      <div class="menu-header" onclick="showMainMenu()">
        <span class="back-arrow">&lt;</span> Quality
      </div>
      <div class="menu-options-list" id="opts-quality"></div>
    </div>
    
    <div id="settings-subtitle-menu" class="settings-menu-pane">
      <div class="menu-header" onclick="showMainMenu()">
        <span class="back-arrow">&lt;</span> Subtitle
      </div>
      <div class="menu-options-list" id="opts-subtitle"></div>
    </div>
    
    <div id="settings-size-menu" class="settings-menu-pane">
      <div class="menu-header" onclick="showMainMenu()">
        <span class="back-arrow">&lt;</span> Subtitle Size
      </div>
      <div class="menu-options-list" id="opts-size"></div>
    </div>
    
    <div id="settings-color-menu" class="settings-menu-pane">
      <div class="menu-header" onclick="showMainMenu()">
        <span class="back-arrow">&lt;</span> Subtitle Color
      </div>
      <div class="menu-options-list" id="opts-color"></div>
    </div>
  </div>

  <script>
    window.PLAYER_CONFIG = {json.dumps(config, ensure_ascii=False)};
  </script>
  <script src="{artplayer_cdn}"></script>
  <script>
    (() => {{
      const cfg = window.PLAYER_CONFIG || {{}};
      const errorEl = document.getElementById('player-error');
      const errorTextEl = document.getElementById('player-error-text');

      function showError(message) {{
        console.error('[Artplayer]', message);
        errorTextEl.textContent = message || 'Terjadi error saat memuat video.';
        errorEl.classList.add('show');
      }}

      if (!cfg.url) {{
        showError('URL video kosong. Parameter url wajib ada.');
        return;
      }}

      if (typeof Artplayer === 'undefined') {{
        showError('Library Artplayer gagal dimuat dari CDN.');
        return;
      }}

      // Find default subtitle (Indonesian first, otherwise first available)
      const qualities = cfg.qualities || [];
      const subtitles = cfg.subtitles || [];
      let defaultSubUrl = '';
      let defaultSubName = 'Off';
      let defaultSubType = 'srt';
      const indoSub = subtitles.find(sub => 
        sub.name.toLowerCase().includes('indo') || 
        sub.lan.toLowerCase().includes('id') || 
        sub.lan.toLowerCase().includes('in')
      );
      if (indoSub) {{
        defaultSubUrl = indoSub.url;
        defaultSubName = indoSub.name;
        defaultSubType = indoSub.type || 'srt';
      }} else if (subtitles.length > 0) {{
        defaultSubUrl = subtitles[0].url;
        defaultSubName = subtitles[0].name;
        defaultSubType = subtitles[0].type || 'srt';
      }}

      const art = new Artplayer({{
        container: '#artplayer-app',
        url: cfg.url,
        poster: cfg.poster,
        title: cfg.title,
        type: 'mp4',
        theme: '#7c3aed',
        volume: 0.8,
        autoplay: true,
        autoSize: false,
        autoMini: false,
        screenshot: false,
        setting: false, // disable default setting gear icon menu
        pip: true,
        hotkey: true,
        mutex: true,
        fullscreen: true,
        fullscreenWeb: true,
        playbackRate: false, // handled by our custom setting menu
        aspectRatio: true,
        fastForward: true,
        lock: true,
        moreVideoAttr: {{
          preload: 'metadata',
          playsInline: true,
          crossorigin: 'anonymous',
        }},
        subtitle: {{
          url: defaultSubUrl,
          type: 'vtt',
          style: {{
            color: '#ffffff',
          }},
          encoding: 'utf-8',
        }},
        controls: [
          {{
            name: 'custom-settings',
            position: 'right',
            index: 10,
            html: `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="cursor: pointer;"><circle cx="12" cy="12" r="3" style="fill: none !important;"></circle><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" style="fill: none !important;"></path></svg>`,
            click: function(art, event) {{
              const e = event || window.event;
              if (e) {{
                if (typeof e.stopPropagation === 'function') e.stopPropagation();
                if (typeof e.preventDefault === 'function') e.preventDefault();
              }}
              toggleCustomSettings();
              return false;
            }}
          }},
          {{
            name: 'backward-10',
            position: 'left',
            index: 11,
            html: `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="cursor: pointer;"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" style="fill: none !important;"/><path d="M3 3v5h5" style="fill: none !important;"/><text x="12" y="15" font-size="8" font-family="'Outfit', sans-serif" font-weight="700" fill="currentColor" style="fill: currentColor !important; stroke: none !important;" text-anchor="middle">10</text></svg>`,
            click: function(art, event) {{
              const e = event || window.event;
              if (e) {{
                if (typeof e.stopPropagation === 'function') e.stopPropagation();
                if (typeof e.preventDefault === 'function') e.preventDefault();
              }}
              try {{
                const p = art || window.art;
                if (!p) {{
                  console.error("[Seek] Artplayer instance not found.");
                  return false;
                }}
                const video = p.video || document.querySelector('#artplayer-app video');
                if (!video) {{
                  console.error("[Seek] Video element not found.");
                  return false;
                }}
                const current = video.currentTime || 0;
                const target = Math.max(0, current - 10);
                console.log("[Seek] Backward 10s. Current:", current, "Target:", target);
                video.currentTime = target;
                if (typeof p.seek === 'function') {{
                  p.seek(target);
                }} else {{
                  p.currentTime = target;
                }}
              }} catch (err) {{
                console.error("[Seek] Backward error:", err);
              }}
              return false;
            }}
          }},
          {{
            name: 'forward-10',
            position: 'left',
            index: 12,
            html: `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="cursor: pointer;"><path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" style="fill: none !important;"/><path d="M21 3v5h-5" style="fill: none !important;"/><text x="12" y="15" font-size="8" font-family="'Outfit', sans-serif" font-weight="700" fill="currentColor" style="fill: currentColor !important; stroke: none !important;" text-anchor="middle">10</text></svg>`,
            click: function(art, event) {{
              const e = event || window.event;
              if (e) {{
                if (typeof e.stopPropagation === 'function') e.stopPropagation();
                if (typeof e.preventDefault === 'function') e.preventDefault();
              }}
              try {{
                const p = art || window.art;
                if (!p) {{
                  console.error("[Seek] Artplayer instance not found.");
                  return false;
                }}
                const video = p.video || document.querySelector('#artplayer-app video');
                if (!video) {{
                  console.error("[Seek] Video element not found.");
                  return false;
                }}
                const current = video.currentTime || 0;
                const duration = video.duration || p.duration;
                let target = current + 10;
                if (duration && !isNaN(duration)) {{
                  target = Math.min(duration, target);
                }}
                console.log("[Seek] Forward 10s. Current:", current, "Target:", target);
                video.currentTime = target;
                if (typeof p.seek === 'function') {{
                  p.seek(target);
                }} else {{
                  p.currentTime = target;
                }}
              }} catch (err) {{
                console.error("[Seek] Forward error:", err);
              }}
              return false;
            }}
          }}
        ]
      }});

      art.on('ready', () => {{
        const video = art.video;
        video.setAttribute('playsinline', '');
        video.setAttribute('webkit-playsinline', '');
        errorEl.classList.remove('show');
        
        // Preserve and apply custom playback speed across switches
        art.playbackRate = currentSpeed;
        
        // Move settings panel inside Artplayer player wrapper to ensure fullscreen support
        const targetContainer = art.template.$player || art.template.$container || art.container;
        const panel = document.getElementById('custom-settings-panel');
        if (targetContainer && panel) {{
          targetContainer.appendChild(panel);
          
          // Prevent all pointer/touch events from bubbling up to Artplayer (fixing non-selectable/closable bug on mobile)
          const stopBubble = (e) => {{
            if (e && typeof e.stopPropagation === 'function') e.stopPropagation();
          }};
          ['click', 'mousedown', 'mouseup', 'touchstart', 'touchend', 'touchmove'].forEach(evt => {{
            panel.addEventListener(evt, stopBubble, {{ passive: true }});
          }});
        }}
        
        // Move watermark inside Artplayer container to prevent it being cleared and enable fullscreen display
        const watermark = document.getElementById('watermark');
        if (targetContainer && watermark) {{
          targetContainer.appendChild(watermark);
        }}
        
        // Add native subtitle tracks for iOS native player support
        const subtitlesList = cfg.subtitles || [];
        subtitlesList.forEach(sub => {{
          const track = document.createElement('track');
          track.kind = 'subtitles';
          track.label = sub.name;
          track.srclang = sub.lan;
          track.src = sub.url;
          if (sub.url === defaultSubUrl) {{
            track.default = true;
          }}
          video.appendChild(track);
        }});
        
        // Hide all native text tracks initially so they don't double-render inline
        if (video.textTracks) {{
          for (let i = 0; i < video.textTracks.length; i++) {{
            video.textTracks[i].mode = 'hidden';
          }}
        }}
        
        // Toggle native tracks when entering/exiting native iOS fullscreen
        video.addEventListener('webkitbeginfullscreen', () => {{
          for (let i = 0; i < video.textTracks.length; i++) {{
            if (video.textTracks[i].label === currentSubtitle) {{
              video.textTracks[i].mode = 'showing';
            }} else {{
              video.textTracks[i].mode = 'disabled';
            }}
          }}
        }});
        
        video.addEventListener('webkitendfullscreen', () => {{
          for (let i = 0; i < video.textTracks.length; i++) {{
            video.textTracks[i].mode = 'hidden';
          }}
        }});
        
        // Attempt unmuted autoplay
        const playPromise = art.play();
        if (playPromise !== undefined) {{
          playPromise.then(() => {{
            console.log("[Artplayer] Autoplay unmuted success");
          }}).catch(err => {{
            console.warn("[Artplayer] Autoplay unmuted blocked, attempting muted autoplay", err);
            art.muted = true;
            art.play().catch(e => {{
              console.error("[Artplayer] Muted autoplay blocked", e);
            }});
          }});
        }}
        
        // Update subtitle state if default subtitle is loaded
        if (defaultSubUrl) {{
          currentSubtitle = defaultSubName;
          document.getElementById('val-subtitle').textContent = defaultSubName;
          art.subtitle.show = true;
          applySubtitleStyles();
          renderSubtitleOpts();
        }}
      }});

      art.on('video:error', () => {{
        showError('Stream video error. Cek endpoint /api/v1/play-media dan header Range/CORS.');
      }});

      art.on('error', (err) => {{
        console.error('[Artplayer error]', err);
      }});

      // Close settings menu when clicking the video element
      art.on('video:click', () => {{
        closeCustomSettings();
      }});
      
      art.on('play', () => {{
        closeCustomSettings();
      }});

      window.art = art;

      // Settings Navigation & Dynamic Rendering
      const speeds = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0];
      let currentSpeed = 1.0;
      let currentQuality = 'Auto';
      let currentSubtitle = 'Off';
      let currentSize = '100%';
      let currentColor = 'White';

      const speedLabels = {{
        0.5: '0.5x',
        0.75: '0.75x',
        1.0: 'Normal',
        1.25: '1.25x',
        1.5: '1.5x',
        2.0: '2.0x'
      }};

      const subtitleColors = {{
        'White': '#ffffff',
        'Yellow': '#ffff00',
        'Cyan': '#00ffff',
        'Green': '#00ff00',
        'Purple': '#a78bfa'
      }};

      const subtitleSizes = {{
        '50%': '0.5',
        '75%': '0.75',
        '100%': '1.0',
        '125%': '1.25',
        '150%': '1.5',
        '200%': '2.0'
      }};

      window.showSubMenu = function(menuName) {{
        document.querySelectorAll('.settings-menu-pane').forEach(p => p.classList.remove('active'));
        document.getElementById(`settings-${{menuName}}-menu`).classList.add('active');
      }};

      window.showMainMenu = function() {{
        document.querySelectorAll('.settings-menu-pane').forEach(p => p.classList.remove('active'));
        document.getElementById('settings-main-menu').classList.add('active');
      }};

      window.toggleCustomSettings = function() {{
        const panel = document.getElementById('custom-settings-panel');
        if (panel.classList.contains('open')) {{
          closeCustomSettings();
        }} else {{
          panel.classList.add('open');
        }}
      }};

      window.closeCustomSettings = function() {{
        const panel = document.getElementById('custom-settings-panel');
        if (panel) {{
          panel.classList.remove('open');
        }}
        showMainMenu();
      }};

      // Speed Options
      function renderSpeedOpts() {{
        const container = document.getElementById('opts-speed');
        container.innerHTML = speeds.map(sp => `
          <div class="menu-option ${{currentSpeed === sp ? 'selected' : ''}}" onclick="setSpeed(${{sp}})">
            <span>${{speedLabels[sp]}}</span>
            <svg class="selected-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
          </div>
        `).join('');
      }}
      
      window.setSpeed = function(sp) {{
        currentSpeed = sp;
        art.playbackRate = sp;
        document.getElementById('val-speed').textContent = speedLabels[sp];
        renderSpeedOpts();
        showMainMenu();
      }};

      // Quality Options
      function renderQualityOpts() {{
        const container = document.getElementById('opts-quality');
        let html = `
          <div class="menu-option ${{currentQuality === 'Auto' ? 'selected' : ''}}" onclick="setQuality('Auto', '${{cfg.url}}')">
            <span>Auto (Terbaik)</span>
            <svg class="selected-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
          </div>
        `;
        qualities.forEach(q => {{
          html += `
            <div class="menu-option ${{currentQuality === q.html ? 'selected' : ''}}" onclick="setQuality('${{q.html}}', '${{q.url}}')">
              <span>${{q.html}} ${{q.size ? `(${{q.size}})` : ''}}</span>
              <svg class="selected-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
            </div>
          `;
        }});
        container.innerHTML = html;
      }}
      
      window.setQuality = function(name, url) {{
        currentQuality = name;
        document.getElementById('val-quality').textContent = name;
        
        const currentTime = art.currentTime;
        const isPlaying = art.playing;
        
        art.switchUrl(url);
        art.once('video:loadedmetadata', () => {{
          art.currentTime = currentTime;
          if (isPlaying) {{
            art.play().catch(() => {{}});
          }}
        }});
        
        renderQualityOpts();
        showMainMenu();
      }};

      // Subtitle Options
      function renderSubtitleOpts() {{
        const container = document.getElementById('opts-subtitle');
        let html = `
          <div class="menu-option ${{currentSubtitle === 'Off' ? 'selected' : ''}}" onclick="setSubtitle('Off', '')">
            <span>Matikan Subtitle</span>
            <svg class="selected-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
          </div>
        `;
        subtitles.forEach(sub => {{
          html += `
            <div class="menu-option ${{currentSubtitle === sub.name ? 'selected' : ''}}" onclick="setSubtitle('${{sub.name}}', '${{sub.url}}')">
              <span>${{sub.name}}</span>
              <svg class="selected-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
            </div>
          `;
        }});
        container.innerHTML = html;
      }}
      
      window.setSubtitle = function(name, url) {{
        currentSubtitle = name;
        document.getElementById('val-subtitle').textContent = name;
        
        if (name === 'Off') {{
          art.subtitle.show = false;
        }} else {{
          art.subtitle.switch(url, {{
            name: name,
            type: 'vtt'
          }});
          art.subtitle.show = true;
          
          // Apply size and color settings
          applySubtitleStyles();
        }}
        
        // Also update the active native track for iOS native player
        const video = art.video;
        if (video && video.textTracks) {{
          for (let i = 0; i < video.textTracks.length; i++) {{
            if (video.textTracks[i].label === name) {{
              const inNativeFullscreen = document.webkitFullscreenElement === video || video.webkitDisplayingFullscreen;
              video.textTracks[i].mode = inNativeFullscreen ? 'showing' : 'hidden';
            }} else {{
              video.textTracks[i].mode = 'disabled';
            }}
          }}
        }}
        
        renderSubtitleOpts();
        showMainMenu();
      }};

      // Subtitle Size Options
      const sizes = ['50%', '75%', '100%', '125%', '150%', '200%'];
      function renderSizeOpts() {{
        const container = document.getElementById('opts-size');
        container.innerHTML = sizes.map(sz => `
          <div class="menu-option ${{currentSize === sz ? 'selected' : ''}}" onclick="setSize('${{sz}}')">
            <span>${{sz}}</span>
            <svg class="selected-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
          </div>
        `).join('');
      }}
      
      window.setSize = function(sz) {{
        currentSize = sz;
        document.getElementById('val-size').textContent = sz;
        applySubtitleStyles();
        renderSizeOpts();
        showMainMenu();
      }};

      // Subtitle Color Options
      const colors = ['White', 'Yellow', 'Cyan', 'Green', 'Purple'];
      function renderColorOpts() {{
        const container = document.getElementById('opts-color');
        container.innerHTML = colors.map(cl => `
          <div class="menu-option ${{currentColor === cl ? 'selected' : ''}}" onclick="setColor('${{cl}}')">
            <span style="display: flex; align-items: center;">
              <span style="width: 12px; height: 12px; border-radius: 50%; background-color: ${{subtitleColors[cl]}}; margin-right: 8px; border: 1px solid rgba(255,255,255,0.2);"></span>
              ${{cl}}
            </span>
            <svg class="selected-icon" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
          </div>
        `).join('');
      }}
      
      window.setColor = function(cl) {{
        currentColor = cl;
        document.getElementById('val-color').textContent = cl;
        applySubtitleStyles();
        renderColorOpts();
        showMainMenu();
      }};

      function applySubtitleStyles() {{
        const scale = subtitleSizes[currentSize] || '1.0';
        const colorHex = subtitleColors[currentColor] || '#ffffff';
        
        document.documentElement.style.setProperty('--sub-size-scale', scale);
        
        art.subtitle.style({{
          color: colorHex
        }});
        
        const subEl = document.querySelector('.art-subtitle');
        if (subEl) {{
          subEl.style.color = colorHex;
        }}
      }}

      // Init menus
      renderSpeedOpts();
      renderQualityOpts();
      renderSubtitleOpts();
      renderSizeOpts();
      renderColorOpts();

      // Keyboard Esc to close settings and 'f'/'F' to toggle fullscreen
      window.addEventListener('keydown', (e) => {{
        const activeEl = document.activeElement;
        const isTyping = activeEl && (
          activeEl.tagName === 'INPUT' || 
          activeEl.tagName === 'TEXTAREA' || 
          activeEl.isContentEditable
        );
        
        if (e.key === 'Escape') {{
          closeCustomSettings();
        }}
        
        if (!isTyping && (e.key === 'f' || e.key === 'F')) {{
          e.preventDefault();
          const p = art || window.art;
          if (p) {{
            p.fullscreen = !p.fullscreen;
          }}
        }}
      }});
    }})();
  </script>
</body>
</html>'''

    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-store",
        },
    )


async def proxy_stream_url(url: str, request: Request):
    headers = {
        "Accept": "*/*",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
        "Origin": "https://videodownloader.site/",
        "Referer": "https://videodownloader.site/",
    }

    # Forward the Range header so seeking, duration, and 10-second skip work properly.
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    client = httpx.AsyncClient(follow_redirects=True, timeout=None)

    try:
        req = client.build_request("GET", url, headers=headers)
        resp = await client.send(req, stream=True)

        send_headers = {}
        for h in ["content-type", "content-length", "content-range", "accept-ranges"]:
            if h in resp.headers:
                send_headers[h] = resp.headers[h]

        send_headers["Access-Control-Allow-Origin"] = "*"
        send_headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        send_headers["Access-Control-Allow-Headers"] = "Range, Content-Type, Origin, Accept"
        send_headers["Accept-Ranges"] = "bytes"

        if "content-type" not in send_headers:
            send_headers["Content-Type"] = "video/mp4"

        async def iterate_bytes():
            try:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 128):
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        return StreamingResponse(
            iterate_bytes(),
            status_code=resp.status_code,
            headers=send_headers,
        )
    except Exception as e:
        await client.aclose()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/play-media/{detailPath}")
async def play_movie_media(detailPath: str, request: Request, resolution: Optional[int] = None):
    try:
        details = await get_cached_detail(detailPath)
        subject_type_val = details.subject.subjectType.value if hasattr(details.subject.subjectType, 'value') else int(details.subject.subjectType)
        is_tv = (subject_type_val == 2 or subject_type_val == 7)
        if is_tv:
            first_season = 1
            if details.resource and details.resource.seasons:
                first_season = details.resource.seasons[0].se
            return await play_episode_media(detailPath, first_season, 1, request, resolution)

        dl_meta = await get_cached_media_detail(detailPath, is_tv=False)
        if resolution:
            try:
                target_file = dl_meta.get_media_file_by_resolution(int(resolution))
            except Exception:
                target_file = dl_meta.best_media_file
        else:
            target_file = dl_meta.best_media_file
        return await proxy_stream_url(str(target_file.url), request)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/play-media/{detailPath}/{season}/{episode}")
async def play_episode_media(detailPath: str, season: int, episode: int, request: Request, resolution: Optional[int] = None):
    try:
        dl_meta = await get_cached_media_detail(detailPath, season=season, episode=episode, is_tv=True)
        if resolution:
            try:
                target_file = dl_meta.get_media_file_by_resolution(int(resolution))
            except Exception:
                target_file = dl_meta.best_media_file
        else:
            target_file = dl_meta.best_media_file
        return await proxy_stream_url(str(target_file.url), request)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Backward-compatible direct video URL endpoints
@app.get("/api/v1/play/{detailPath}/video.mp4")
async def play_movie(detailPath: str, request: Request):
    return await play_movie_media(detailPath, request)

@app.get("/api/v1/play/{detailPath}/{season}/{episode}/video.mp4")
async def play_episode(detailPath: str, season: int, episode: int, request: Request):
    return await play_episode_media(detailPath, season, episode, request)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

# Vercel serverless handler (Mangum wraps ASGI app for AWS Lambda / Vercel)
try:
    from mangum import Mangum
    handler = Mangum(app, lifespan="off")
except ImportError:
    pass
