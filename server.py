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

from fastapi import FastAPI, Query, HTTPException, Request, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi
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

app = FastAPI(
    title="Moviebox Local API Server",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

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
        await _global_session._client.aclose()

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

# ── V3 Mock Models for V2 Compatibility ─────────────────────────────────────────
class MockMediaFile:
    def __init__(self, resolution, url, size):
        self.resolution = resolution
        self.url = url
        self.size = size

class MockCaptionFile:
    def __init__(self, id_val, lan, lanName, url):
        self.id = id_val
        self.lan = lan
        self.lanName = lanName
        self.url = url

class MockMediaDetail:
    def __init__(self, downloads, captions):
        self.downloads = downloads
        self.captions = captions
        
    @property
    def best_media_file(self):
        if self.downloads:
            return max(self.downloads, key=lambda x: x.resolution)
        return None
        
    def get_media_file_by_resolution(self, resolution):
        for dl in self.downloads:
            if dl.resolution == resolution:
                return dl
        return self.best_media_file


async def get_cached_detail(detailPath: str):
    import time
    now = time.time()
    if detailPath in detail_cache:
        entry = detail_cache[detailPath]
        if (now - entry["timestamp"]) < DETAIL_CACHE_TTL:
            return entry["data"]
            
    if detailPath.startswith("subject-"):
        # Fetch using V3 API
        subject_id = detailPath.split("-")[-1]
        from moviebox_api.v3.http_client import MovieBoxHttpClient
        from moviebox_api.v3.core import ItemDetails as V3ItemDetails
        
        async with MovieBoxHttpClient() as v3_client:
            det = V3ItemDetails(v3_client, include_seasons=True)
            v3_details = await det.get_content_model(subject_id)
            
            # Map V3 RootItemDetailsModel to a structure compatible with V2 SpecificItemDetailsModel
            class MockSubject:
                def __init__(self, v3_subj):
                    self.genre = v3_subj.genre
                    self.subjectType = v3_subj.subject_type
                    self.title = v3_subj.title
                    self.cover = v3_subj.cover
                    self.description = v3_subj.description
                    self.releaseDate = v3_subj.release_date
                    self.imdbRatingValue = v3_subj.imdb_rating_value
                    self.countryName = v3_subj.country_name

            class MockSeason:
                def __init__(self, se, max_ep):
                    self.se = se
                    self.maxEp = max_ep

            class MockResource:
                def __init__(self, v3_seasons):
                    self.seasons = [MockSeason(s.se, s.max_ep) for s in v3_seasons.seasons] if v3_seasons else []

            class MockStar:
                def __init__(self, staff):
                    self.name = staff.name
                    self.character = staff.character
                    self.avatarUrl = staff.avatar_url

            class MockDetails:
                def __init__(self, v3_d):
                    self.subject = MockSubject(v3_d)
                    self.resource = MockResource(v3_d.seasons)
                    self.stars = [MockStar(s) for s in v3_d.staff_list] if v3_d.staff_list else []
            
            details = MockDetails(v3_details)
    else:
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
            
    if detailPath.startswith("subject-"):
        # Fetch using V3 API directly
        subject_id = detailPath.split("-")[-1]
        from moviebox_api.v3.http_client import MovieBoxHttpClient
        
        async with MovieBoxHttpClient() as v3_client:
            params = {"subjectId": subject_id}
            if is_tv:
                params["se"] = season if season is not None else 1
                params["ep"] = episode if episode is not None else 1
                
            res_data = await v3_client.get_from_api("/wefeed-mobile-bff/subject-api/resource", params=params)
            video_list = res_data.get("list", [])
            
            # Build mock downloads list
            downloads = []
            for item in video_list:
                res_val = item.get("resolution", 720)
                url_val = item.get("resourceLink")
                size_val = item.get("size", 0)
                downloads.append(MockMediaFile(res_val, url_val, size_val))
                
            captions = []
            if video_list:
                best_res_id = video_list[0].get("resourceId")
                cap_params = {"subjectId": subject_id, "resourceId": best_res_id}
                try:
                    cap_data = await v3_client.get_from_api("/wefeed-mobile-bff/subject-api/get-ext-captions", params=cap_params)
                    ext_captions = cap_data.get("extCaptions", [])
                    for cap in ext_captions:
                        captions.append(MockCaptionFile(
                            id_val=cap.get("id"),
                            lan=cap.get("lan"),
                            lanName=cap.get("lanName"),
                            url=cap.get("url")
                        ))
                except Exception as e:
                    logger.warning(f"Error fetching V3 captions: {e}")
                    
            dl_meta = MockMediaDetail(downloads, captions)
    else:
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

async def execute_search(query_str: str, subj_type, page: int = 1):
    """
    Melakukan pencarian dengan fallback ke V3 API jika V2 API (h5-api) down/error/400.
    """
    try:
        # Coba V2 Search terlebih dahulu
        session = get_global_session()
        search_inst = Search(session, query=query_str, subject_type=subj_type, page=page)
        search_res = await search_inst.get_content_model()
        return [format_item(item) for item in search_res.items]
    except Exception as e:
        logger.warning("[Search] V2 Search failed for '%s' (error: %s). Falling back to V3 Search...", query_str, str(e))
        try:
            # Fallback ke V3 Search (Android App API)
            from moviebox_api.v3.http_client import MovieBoxHttpClient
            from moviebox_api.v3.core import Search as V3Search
            from moviebox_api.v3.constants import SubjectType as V3SubjectType
            
            # Map V2 SubjectType to V3 SubjectType
            v3_subject_type = V3SubjectType(int(subj_type))
            
            async with MovieBoxHttpClient() as v3_client:
                v3_search_inst = V3Search(
                    v3_client,
                    query=query_str,
                    subject_type=v3_subject_type,
                    page=page
                )
                v3_res_model = await v3_search_inst.get_content_model()
                
                formatted_items = []
                for m_item in v3_res_model.items:
                    subject_type_val = int(m_item.subject_type)
                    item_type = "tv" if subject_type_val in (2, 7) else "movie"
                    rating = str(m_item.imdb_rating_value) if m_item.imdb_rating_value else "N/A"
                    year = str(m_item.release_date.year) if m_item.release_date else ""
                    poster = str(m_item.cover.url) if m_item.cover and m_item.cover.url else ""
                    detail_path = ""
                    if m_item.detail_url and "/detail/" in str(m_item.detail_url):
                        detail_path = str(m_item.detail_url).split("/detail/")[-1]
                    elif m_item.subject_id:
                        detail_path = f"subject-{m_item.subject_id}"
                    genres = m_item.genre or []
                    
                    formatted_items.append({
                        "title": m_item.title,
                        "poster": poster,
                        "year": year,
                        "detailPath": detail_path,
                        "rating": rating,
                        "type": item_type,
                        "genres": genres
                    })
                logger.info("[Search] V3 Search fallback successful! Found %d items.", len(formatted_items))
                return formatted_items
        except Exception as v3_err:
            logger.error("[Search] V3 Search fallback also failed: %s", str(v3_err))
            raise RuntimeError(f"Both V2 and V3 search failed. V2 error: {e}. V3 error: {v3_err}")

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
        return await execute_search(query_str, subj_type, page=page)
        
    return []

def authenticate(request: Request):
    session = request.cookies.get("session_token")
    if session != "moviebox_trico_session":
        if request.url.path == "/openapi.json":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized access"
            )
        # Redirect the user to the login page and pass the current path as a redirect parameter
        redirect_param = quote(str(request.url.path))
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": f"/login?redirect={redirect_param}"}
        )
    return "Trico"

class LoginPayload(BaseModel):
    username: str
    password: str

@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, redirect: str = "/docs"):
    html_content = """<!doctype html>
<html lang="id">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Login - Nobarin API Docs</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-color: #0b0914;
      --card-bg: rgba(18, 14, 33, 0.7);
      --primary-color: #7c3aed;
      --primary-hover: #6d28d9;
      --accent-color: #a78bfa;
      --text-main: #ffffff;
      --text-muted: #94a3b8;
      --error-color: #f43f5e;
      --border-color: rgba(124, 58, 237, 0.25);
    }
    
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    
    body {
      background-color: var(--bg-color);
      color: var(--text-main);
      font-family: 'Outfit', sans-serif;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow-x: hidden;
      position: relative;
    }
    
    /* Background gradients */
    body::before {
      content: "";
      position: absolute;
      width: 500px;
      height: 500px;
      background: radial-gradient(circle, rgba(124, 58, 237, 0.15) 0%, rgba(0,0,0,0) 70%);
      top: -100px;
      left: -100px;
      z-index: 0;
      pointer-events: none;
    }
    
    body::after {
      content: "";
      position: absolute;
      width: 600px;
      height: 600px;
      background: radial-gradient(circle, rgba(167, 139, 250, 0.1) 0%, rgba(0,0,0,0) 70%);
      bottom: -150px;
      right: -150px;
      z-index: 0;
      pointer-events: none;
    }
    
    .login-container {
      width: 100%;
      max-width: 440px;
      padding: 24px;
      z-index: 10;
    }
    
    .login-card {
      background: var(--card-bg);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border: 1px solid var(--border-color);
      border-radius: 20px;
      padding: 40px 32px;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5), 0 0 50px rgba(124, 58, 237, 0.05);
      transition: transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1), box-shadow 0.3s ease;
    }
    
    .login-card:hover {
      box-shadow: 0 24px 48px rgba(0, 0, 0, 0.6), 0 0 60px rgba(124, 58, 237, 0.1);
    }
    
    .logo-area {
      text-align: center;
      margin-bottom: 32px;
    }
    
    .logo-title {
      font-size: 32px;
      font-weight: 700;
      letter-spacing: -0.5px;
      background: linear-gradient(135deg, #ffffff 0%, var(--accent-color) 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      margin-bottom: 8px;
    }
    
    .logo-subtitle {
      font-size: 14px;
      color: var(--text-muted);
      font-weight: 400;
    }
    
    .form-group {
      margin-bottom: 20px;
      position: relative;
    }
    
    .form-label {
      display: block;
      font-size: 14px;
      font-weight: 500;
      color: var(--text-muted);
      margin-bottom: 8px;
      transition: color 0.2s ease;
    }
    
    .input-wrapper {
      position: relative;
    }
    
    .form-input {
      width: 100%;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 12px;
      padding: 14px 16px;
      color: #fff;
      font-family: inherit;
      font-size: 15px;
      transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
    }
    
    .form-input:focus {
      outline: none;
      background: rgba(255, 255, 255, 0.08);
      border-color: var(--primary-color);
      box-shadow: 0 0 0 4px rgba(124, 58, 237, 0.15), 0 4px 12px rgba(124, 58, 237, 0.08);
    }
    
    .btn-submit {
      width: 100%;
      background: linear-gradient(135deg, var(--primary-color) 0%, #6d28d9 100%);
      border: none;
      border-radius: 12px;
      padding: 14px;
      color: #fff;
      font-family: inherit;
      font-size: 16px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
      box-shadow: 0 4px 12px rgba(124, 58, 237, 0.3);
      margin-top: 10px;
      display: flex;
      justify-content: center;
      align-items: center;
      gap: 8px;
    }
    
    .btn-submit:hover {
      background: linear-gradient(135deg, #8b5cf6 0%, var(--primary-color) 100%);
      box-shadow: 0 6px 20px rgba(124, 58, 237, 0.45);
      transform: translateY(-1px);
    }
    
    .btn-submit:active {
      transform: translateY(1px);
      box-shadow: 0 2px 8px rgba(124, 58, 237, 0.2);
    }
    
    .error-container {
      background: rgba(244, 63, 94, 0.1);
      border: 1px solid rgba(244, 63, 94, 0.2);
      border-radius: 10px;
      padding: 12px;
      margin-bottom: 20px;
      font-size: 14px;
      color: var(--error-color);
      display: none;
      align-items: center;
      gap: 8px;
      animation: shake 0.4s cubic-bezier(.36,.07,.19,.97) both;
    }
    
    @keyframes shake {
      10%, 90% { transform: translate3d(-1px, 0, 0); }
      20%, 80% { transform: translate3d(2px, 0, 0); }
      30%, 50%, 70% { transform: translate3d(-4px, 0, 0); }
      40%, 60% { transform: translate3d(4px, 0, 0); }
    }
    
    .loading-spinner {
      width: 20px;
      height: 20px;
      border: 2.5px solid rgba(255, 255, 255, 0.3);
      border-top-color: #fff;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      display: none;
    }
    
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
  </style>
</head>
<body>
  <div class="login-container">
    <div class="login-card">
      <div class="logo-area">
        <h1 class="logo-title">Nobarin API</h1>
        <p class="logo-subtitle">Silakan login untuk mengakses dokumentasi</p>
      </div>
      
      <div class="error-container" id="error-box">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16">
          <path d="M8.982 1.566a1.13 1.13 0 0 0-1.96 0L.165 13.233c-.457.778.091 1.767.98 1.767h13.713c.889 0 1.438-.99.98-1.767L8.982 1.566zM8 5c.535 0 .954.462.9.995l-.35 3.507a.552.552 0 0 1-1.1 0L7.1 5.995A.905.905 0 0 1 8 5zm.002 6a1 1 0 1 1 0 2 1 1 0 0 1 0-2z"/>
        </svg>
        <span id="error-message">Username atau password salah.</span>
      </div>
      
      <form id="login-form">
        <div class="form-group">
          <label class="form-label" for="username">Username</label>
          <div class="input-wrapper">
            <input class="form-input" type="text" id="username" required autocomplete="username" autofocus />
          </div>
        </div>
        
        <div class="form-group">
          <label class="form-label" for="password">Password</label>
          <div class="input-wrapper">
            <input class="form-input" type="password" id="password" required autocomplete="current-password" />
          </div>
        </div>
        
        <button class="btn-submit" type="submit">
          <span class="loading-spinner" id="spinner"></span>
          <span id="btn-text">Masuk</span>
        </button>
      </form>
    </div>
  </div>

  <script>
    const form = document.getElementById('login-form');
    const errorBox = document.getElementById('error-box');
    const errorMessage = document.getElementById('error-message');
    const spinner = document.getElementById('spinner');
    const btnText = document.getElementById('btn-text');
    
    const urlParams = new URLSearchParams(window.location.search);
    const redirectUrl = urlParams.get('redirect') || '/docs';
    
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      errorBox.style.display = 'none';
      
      const username = document.getElementById('username').value;
      const password = document.getElementById('password').value;
      
      spinner.style.display = 'block';
      btnText.textContent = 'Memproses...';
      
      try {
        const response = await fetch('/login', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({ username, password })
        });
        
        const data = await response.json();
        
        if (response.ok && data.success) {
          window.location.href = redirectUrl;
        } else {
          errorMessage.textContent = data.message || 'Username atau password salah.';
          errorBox.style.display = 'flex';
          const oldBox = errorBox;
          const newBox = oldBox.cloneNode(true);
          oldBox.parentNode.replaceChild(newBox, oldBox);
        }
      } catch (err) {
        errorMessage.textContent = 'Terjadi kesalahan koneksi server.';
        errorBox.style.display = 'flex';
      } finally {
        spinner.style.display = 'none';
        btnText.textContent = 'Masuk';
      }
    });
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)

@app.post("/login", include_in_schema=False)
async def login_api(payload: LoginPayload):
    if payload.username == "Trico" and payload.password == "Trico2000":
        response = JSONResponse(content={"success": True})
        response.set_cookie(
            key="session_token",
            value="moviebox_trico_session",
            httponly=True,
            samesite="lax",
            max_age=86400 * 30  # 30 days
        )
        return response
    return JSONResponse(
        status_code=400,
        content={"success": False, "message": "Username atau password salah."}
    )

@app.get("/logout", include_in_schema=False)
async def logout_api():
    response = RedirectResponse(url="/login")
    response.delete_cookie(key="session_token")
    return response

@app.get("/docs", include_in_schema=False)
async def get_swagger_documentation(username: str = Depends(authenticate)):
    html_content = """<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Nobarin - API Documentation</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap" rel="stylesheet">
  <link type="text/css" rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  <style>
    :root {
      --bg-color: #0b0914;
      --card-bg: rgba(18, 14, 33, 0.7);
      --primary-color: #7c3aed;
      --primary-hover: #6d28d9;
      --accent-color: #a78bfa;
      --text-main: #ffffff;
      --text-muted: #94a3b8;
      --border-color: rgba(124, 58, 237, 0.25);
    }

    body {
      background-color: var(--bg-color) !important;
      color: var(--text-main) !important;
      font-family: 'Outfit', sans-serif !important;
      margin: 0;
      padding: 0;
    }

    /* Custom Header */
    .custom-header {
      background: rgba(11, 9, 20, 0.85);
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border-color);
      position: sticky;
      top: 0;
      z-index: 1000;
      padding: 16px 24px;
    }

    .header-content {
      max-width: 1460px;
      margin: 0 auto;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .header-logo {
      font-size: 22px;
      font-weight: 700;
      background: linear-gradient(135deg, #ffffff 0%, var(--accent-color) 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      letter-spacing: -0.5px;
    }

    .btn-logout {
      background: rgba(244, 63, 94, 0.1);
      border: 1px solid rgba(244, 63, 94, 0.3);
      color: #f43f5e;
      text-decoration: none;
      padding: 8px 16px;
      border-radius: 8px;
      font-size: 14px;
      font-weight: 500;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
    }

    .btn-logout:hover {
      background: #f43f5e;
      color: #fff;
      box-shadow: 0 0 12px rgba(244, 63, 94, 0.4);
    }

    /* Swagger UI Overrides */
    .swagger-ui {
      background-color: var(--bg-color) !important;
      font-family: 'Outfit', sans-serif !important;
    }

    .swagger-ui .topbar {
      display: none !important;
    }

    .swagger-ui .info {
      margin: 40px 0 20px 0 !important;
    }

    .swagger-ui .info .title {
      color: var(--text-main) !important;
      font-family: 'Outfit', sans-serif !important;
      font-size: 36px !important;
    }

    .swagger-ui .info p, .swagger-ui .info li, .swagger-ui .info td, .swagger-ui .info a {
      color: var(--text-muted) !important;
    }

    .swagger-ui .scheme-container {
      background-color: var(--card-bg) !important;
      border: 1px solid var(--border-color) !important;
      border-radius: 16px !important;
      backdrop-filter: blur(10px);
      box-shadow: 0 10px 25px rgba(0,0,0,0.3) !important;
      padding: 20px !important;
      margin: 20px 0 !important;
    }

    .swagger-ui .opblock-tag-section {
      font-family: 'Outfit', sans-serif !important;
    }

    .swagger-ui .opblock-tag {
      color: var(--text-main) !important;
      border-bottom: 1px solid var(--border-color) !important;
      font-family: 'Outfit', sans-serif !important;
    }

    .swagger-ui select {
      background-color: #1a162b !important;
      color: var(--text-main) !important;
      border: 1px solid var(--border-color) !important;
      border-radius: 8px !important;
      padding: 6px 10px !important;
    }

    /* Opblocks styling */
    .swagger-ui .opblock {
      background: rgba(18, 14, 33, 0.4) !important;
      border: 1px solid rgba(255, 255, 255, 0.04) !important;
      border-radius: 12px !important;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15) !important;
      margin-bottom: 12px !important;
    }

    .swagger-ui .opblock.opblock-get {
      border-color: rgba(124, 58, 237, 0.3) !important;
      background: rgba(124, 58, 237, 0.03) !important;
    }

    .swagger-ui .opblock.opblock-get .opblock-summary-method {
      background-color: var(--primary-color) !important;
      color: #ffffff !important;
      border-radius: 6px !important;
    }

    .swagger-ui .opblock.opblock-post {
      border-color: rgba(167, 139, 250, 0.3) !important;
      background: rgba(167, 139, 250, 0.03) !important;
    }

    .swagger-ui .opblock.opblock-post .opblock-summary-method {
      background-color: var(--accent-color) !important;
      color: #0b0914 !important;
      border-radius: 6px !important;
    }

    .swagger-ui .opblock .opblock-summary-path {
      color: var(--text-main) !important;
      font-weight: 600 !important;
    }

    .swagger-ui .opblock .opblock-summary-description {
      color: var(--text-muted) !important;
    }

    /* Response tables & models */
    .swagger-ui .tabli, .swagger-ui .tab {
      color: var(--text-muted) !important;
    }

    .swagger-ui .opblock-description-wrapper p, 
    .swagger-ui .opblock-external-docs-wrapper p, 
    .swagger-ui .opblock-title_normal p {
      color: var(--text-muted) !important;
    }

    .swagger-ui .btn {
      background-color: rgba(124, 58, 237, 0.1) !important;
      color: var(--accent-color) !important;
      border: 1px solid rgba(124, 58, 237, 0.3) !important;
      border-radius: 8px !important;
      transition: all 0.2s ease !important;
      box-shadow: none !important;
    }

    .swagger-ui .btn:hover {
      background-color: var(--primary-color) !important;
      color: #ffffff !important;
      box-shadow: 0 0 10px rgba(124, 58, 237, 0.4) !important;
    }

    .swagger-ui input[type=text] {
      background-color: #1a162b !important;
      color: var(--text-main) !important;
      border: 1px solid var(--border-color) !important;
      border-radius: 8px !important;
      padding: 8px 12px !important;
    }

    .swagger-ui input[type=text]::placeholder {
      color: rgba(255,255,255,0.3) !important;
    }

    .swagger-ui table thead tr td, .swagger-ui table thead tr th {
      color: var(--text-main) !important;
      border-bottom: 1px solid var(--border-color) !important;
    }

    .swagger-ui .parameter__name {
      color: var(--text-main) !important;
    }

    .swagger-ui .parameter__type {
      color: var(--accent-color) !important;
    }

    .swagger-ui .responses-table {
      background-color: transparent !important;
    }

    .swagger-ui .response-col_status {
      color: var(--text-main) !important;
    }

    .swagger-ui .response-col_links {
      color: var(--text-muted) !important;
    }

    .swagger-ui .opblock .opblock-section-header {
      background: rgba(18, 14, 33, 0.6) !important;
      border-bottom: 1px solid var(--border-color) !important;
      color: var(--text-main) !important;
    }
    
    .swagger-ui .opblock .opblock-section-header h4 {
      color: var(--text-main) !important;
    }
    
    .swagger-ui .tabli.active {
      border-bottom: 3px solid var(--accent-color) !important;
      color: var(--accent-color) !important;
    }

    .swagger-ui .btn.cancel {
      border-color: rgba(244, 63, 94, 0.4) !important;
      color: #f43f5e !important;
      background: rgba(244, 63, 94, 0.1) !important;
    }

    .swagger-ui .btn.cancel:hover {
      background: #f43f5e !important;
      color: #ffffff !important;
    }

    .swagger-ui .btn.execute {
      background-color: var(--primary-color) !important;
      color: #ffffff !important;
      border: 1px solid var(--primary-color) !important;
    }

    .swagger-ui .btn.execute:hover {
      background-color: var(--primary-hover) !important;
      box-shadow: 0 0 10px rgba(124, 58, 237, 0.5) !important;
    }

    .swagger-ui section.models {
      border: 1px solid var(--border-color) !important;
      border-radius: 16px !important;
      background: var(--card-bg) !important;
      margin-top: 40px !important;
    }

    .swagger-ui section.models h4 {
      border-bottom: 1px solid var(--border-color) !important;
      color: var(--text-main) !important;
    }

    .swagger-ui section.models h4 span {
      color: var(--text-main) !important;
    }

    .swagger-ui .model-box {
      background-color: #120e21 !important;
      border: 1px solid var(--border-color) !important;
      border-radius: 8px !important;
      padding: 12px !important;
    }

    .swagger-ui .model-box-control {
      background: transparent !important;
      color: var(--text-main) !important;
      border: none !important;
    }

    .swagger-ui .model-box-control:focus {
      outline: none !important;
    }

    .swagger-ui .model-box-control .model-toggle {
      filter: invert(1) !important; /* Ensure toggle arrow stands out in dark theme */
    }

    .swagger-ui section.models button.json-schema-2020-12-accordion {
      background: transparent !important;
      color: var(--text-main) !important;
      border: none !important;
      font-family: 'Outfit', sans-serif !important;
    }

    .swagger-ui section.models button.json-schema-2020-12-accordion:hover {
      background: rgba(255, 255, 255, 0.05) !important;
    }

    .swagger-ui section.models button.json-schema-2020-12-expand-deep-button {
      background: rgba(124, 58, 237, 0.1) !important;
      color: var(--accent-color) !important;
      border: 1px solid rgba(124, 58, 237, 0.3) !important;
      border-radius: 6px !important;
      padding: 4px 8px !important;
      font-size: 12px !important;
      font-family: 'Outfit', sans-serif !important;
    }

    .swagger-ui section.models button.json-schema-2020-12-expand-deep-button:hover {
      background: var(--primary-color) !important;
      color: #ffffff !important;
    }

    .swagger-ui .model {
      color: #e2e8f0 !important;
    }

    .swagger-ui .prop-type {
      color: var(--accent-color) !important;
    }

    .swagger-ui .prop-format {
      color: var(--text-muted) !important;
    }

    .swagger-ui .dialog-ux .modal-ux {
      background-color: #0b0914 !important;
      border: 1px solid rgba(124, 58, 237, 0.3) !important;
      border-radius: 16px !important;
      box-shadow: 0 15px 40px rgba(0, 0, 0, 0.8) !important;
    }

    .swagger-ui .dialog-ux .modal-ux-header {
      border-bottom: 1px solid var(--border-color) !important;
    }

    .swagger-ui .dialog-ux .modal-ux-header h3 {
      color: var(--text-main) !important;
    }

    .swagger-ui .dialog-ux .modal-ux-content {
      color: #e2e8f0 !important;
    }

    .swagger-ui .model-viewer {
      background: #0f0b1e !important;
      border-radius: 8px !important;
      padding: 10px !important;
    }

    .swagger-ui .servers-title {
      color: var(--accent-color) !important;
    }

    .swagger-ui .copy-to-clipboard {
      background-color: #1a162b !important;
      border-radius: 6px !important;
      border: 1px solid var(--border-color) !important;
    }
  </style>
</head>
<body>
  <div class="custom-header">
    <div class="header-content">
      <div class="header-logo">Nobarin API Docs</div>
      <a href="/logout" class="btn-logout">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" viewBox="0 0 16 16" style="margin-right:6px; vertical-align:middle;">
          <path fill-rule="evenodd" d="M10 12.5a.5.5 0 0 1-.5.5h-8a.5.5 0 0 1-.5-.5v-9a.5.5 0 0 1 .5-.5h8a.5.5 0 0 1 .5.5v2a.5.5 0 0 0 1 0v-2A1.5 1.5 0 0 0 9.5 2h-8A1.5 1.5 0 0 0 0 3.5v9A1.5 1.5 0 0 0 1.5 14h8a1.5 1.5 0 0 0 1.5-1.5v-2a.5.5 0 0 0-1 0v2z"/>
          <path fill-rule="evenodd" d="M15.854 8.354a.5.5 0 0 0 0-.708l-3-3a.5.5 0 0 0-.708.708L14.293 7.5H5.5a.5.5 0 0 0 0 1h8.793l-2.147 2.146a.5.5 0 0 0 .708.708l3-3z"/>
        </svg>Keluar
      </a>
    </div>
  </div>

  <div id="swagger-ui"></div>

  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = function() {
      window.ui = SwaggerUIBundle({
        url: "/openapi.json",
        dom_id: "#swagger-ui",
        deepLinking: true,
        presets: [
          SwaggerUIBundle.presets.apis,
          SwaggerUIBundle.SwaggerUIStandalonePreset
        ],
        layout: "BaseLayout"
      });
    };
  </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)

@app.get("/redoc", include_in_schema=False)
async def get_redoc_documentation(username: str = Depends(authenticate)):
    return get_redoc_html(openapi_url="/openapi.json", title=app.title)

@app.get("/openapi.json", include_in_schema=False)
async def openapi(username: str = Depends(authenticate)):
    return get_openapi(title=app.title, version=app.version, routes=app.routes)

@app.get("/")
async def root():
    return RedirectResponse(url="/docs")


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
        # Default to ALL if no query is given, otherwise search
        query_str = q if q != "*" else "movie"
        items = await execute_search(query_str, SubjectType.ALL, page=page)
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
        
        cast_list = []
        if details.stars:
            for star in details.stars:
                cast_list.append({
                    "name": star.name,
                    "character": star.character,
                    "avatar": str(star.avatarUrl) if star.avatarUrl else ""
                })

        movie_data = {
            "title": details.subject.title,
            "year": details.subject.releaseDate.year if details.subject.releaseDate else "",
            "description": details.subject.description or "",
            "poster": poster_url,
            "type": "tv" if is_tv else "movie",
            "genre": genres,
            "rating": str(details.subject.imdbRatingValue) if details.subject.imdbRatingValue else "N/A",
            "playerUrl": player_url,
            "cast": cast_list,
            "country": details.subject.countryName if hasattr(details.subject, "countryName") else ""
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
      color: var(--sub-color, #ffffff) !important;
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
        const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
        const isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent);
        const useNativeTracks = isIOS || isSafari;

        if (useNativeTracks) {{
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
        }}
        
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

      art.on('subtitleLoad', () => {{
        applySubtitleStyles();
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
        
        const activeSub = subtitles.find(sub => sub.name === currentSubtitle);
        const activeSubUrl = activeSub ? activeSub.url : '';
        
        art.switchUrl(url, activeSubUrl);
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
        const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
        const isSafari = /^((?!chrome|android).)*safari/i.test(navigator.userAgent);
        const useNativeTracks = isIOS || isSafari;
        
        if (useNativeTracks) {{
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
        document.documentElement.style.setProperty('--sub-color', colorHex);
        
        art.subtitle.style({{
          color: colorHex
        }});
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

    method = request.method
    range_header = request.headers.get("range")
    
    # Enforce maximum chunk size of 2MB (2,097,152 bytes)
    # This ensures requests complete in < 500ms and never hit the 10s timeout
    CHUNK_SIZE = 2 * 1024 * 1024
    
    start = 0
    end = None
    
    if range_header and range_header.startswith("bytes="):
        try:
            parts = range_header.replace("bytes=", "").split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if (len(parts) > 1 and parts[1]) else None
            
            if end is None:
                target_end = start + CHUNK_SIZE - 1
            else:
                target_end = min(end, start + CHUNK_SIZE - 1)
                
            headers["Range"] = f"bytes={start}-{target_end}"
        except Exception as e:
            logger.warning(f"Error parsing range header '{range_header}': {e}")
            headers["Range"] = range_header
    else:
        # Request is GET but has no range header; default to first chunk
        if method == "GET":
            headers["Range"] = f"bytes=0-{CHUNK_SIZE - 1}"
        # For HEAD request without range, we do not set Range header to get full length

    client = httpx.AsyncClient(follow_redirects=True, timeout=15.0)

    try:
        if method == "HEAD":
            req = client.build_request("HEAD", url, headers=headers)
            resp = await client.send(req)
        else:
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

        if method == "HEAD":
            await resp.aclose()
            await client.aclose()
            from fastapi.responses import Response
            return Response(status_code=resp.status_code, headers=send_headers)

        async def iterate_bytes():
            try:
                async for chunk in resp.aiter_bytes(chunk_size=1024 * 64):
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
        logger.error(f"Error in proxy_stream_url: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.api_route("/api/v1/play-media/{detailPath}", methods=["GET", "HEAD"])
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

@app.api_route("/api/v1/play-media/{detailPath}/{season}/{episode}", methods=["GET", "HEAD"])
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
@app.api_route("/api/v1/play/{detailPath}/video.mp4", methods=["GET", "HEAD"])
async def play_movie(detailPath: str, request: Request):
    return await play_movie_media(detailPath, request)

@app.api_route("/api/v1/play/{detailPath}/{season}/{episode}/video.mp4", methods=["GET", "HEAD"])
async def play_episode(detailPath: str, season: int, episode: int, request: Request):
    return await play_episode_media(detailPath, season, episode, request)


# ── Telegram Bot Webhook Endpoint ─────────────────────────────────────────────
@app.post("/api/v1/telegram-webhook")
async def telegram_webhook(request: Request):
    import html
    
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "error": "Invalid JSON"})
        
    message = data.get("message")
    if not message:
        return {"success": True}
        
    chat = message.get("chat")
    if not chat:
        return {"success": True}
        
    chat_id = chat.get("id")
    text = message.get("text", "").strip()
    
    if not text:
        return {"success": True}
        
    # Command /start
    if text == "/start":
        welcome_text = (
            "👋 <b>Halo! Selamat datang di Nobarin Bot.</b>\n\n"
            "Gunakan perintah <code>!s &lt;judul film&gt;</code> atau <code>/search &lt;judul film&gt;</code> untuk mencari film/serial TV.\n\n"
            "Contoh:\n"
            "<code>!s pretty little liar</code>"
        )
        await send_telegram_message(chat_id, welcome_text)
        
    # Command !s atau /search
    elif text.startswith("!s ") or text.startswith("/search "):
        query_str = ""
        if text.startswith("!s "):
            query_str = text[3:].strip()
        elif text.startswith("/search "):
            query_str = text[8:].strip()
            
        if not query_str:
            await send_telegram_message(chat_id, "⚠️ Silakan masukkan judul film. Contoh: <code>!s pretty little liar</code>")
            return {"success": True}
            
        try:
            items = await execute_search(query_str, SubjectType.ALL, page=1)
            
            if not items:
                await send_telegram_message(chat_id, f"❌ Tidak ditemukan hasil untuk <b>{html.escape(query_str)}</b>.")
                return {"success": True}
                
            # Ambil maksimal 10 item untuk ditampilkan
            results_to_show = items[:10]
            total_found = len(items)
            
            response_text = f"🔍 <b>Results for {html.escape(query_str)} ({len(results_to_show)} of {total_found}):</b>\n\n"
            
            # Gunakan URL website Nobarin dari env (default ke nobarin.netlify.app)
            website_url = os.getenv("NOBARIN_WEBSITE_URL", "https://nobarin.netlify.app").rstrip("/")
            
            for idx, item in enumerate(results_to_show, 1):
                title = html.escape(item.get("title", "Unknown"))
                year = item.get("year", "")
                year_str = f" ({year})" if year else ""
                
                item_type = "TV" if item.get("type") == "tv" else "Movie"
                detail_path = item.get("detailPath", "")
                
                # Format URL Detail/Watch sesuai struktur website Nobarin Anda
                watch_link = f"{website_url}/nonton/{detail_path}" if detail_path else "#"
                
                response_text += f"{idx}. <b>{title}</b>{year_str} - <i>{item_type}</i>\n"
                response_text += f"🔗 <a href='{watch_link}'>Nonton di Nobarin</a>\n\n"
                
            await send_telegram_message(chat_id, response_text)
            
        except Exception as e:
            logger.error(f"Error in telegram search webhook: {e}")
            await send_telegram_message(chat_id, f"⚠️ Terjadi kesalahan saat mencari film: {html.escape(str(e))}")
            
    return {"success": True}

async def send_telegram_message(chat_id: int, text: str):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set")
        return
        
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.error(f"Failed to send Telegram message: {resp.text}")
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)

# Vercel serverless handler (Mangum wraps ASGI app for AWS Lambda / Vercel)
try:
    from mangum import Mangum
    handler = Mangum(app, lifespan="off")
except ImportError:
    pass
