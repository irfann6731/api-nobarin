import os
import re
import sys
import asyncio
from typing import Optional

# Add the local src directory to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "src")))

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse
import httpx
from urllib.parse import quote

from moviebox_api.v2 import Homepage, Search, ItemDetails, Session
from moviebox_api.v2.download import DownloadableMovieFilesDetail, DownloadableTVSeriesFilesDetail
from moviebox_api.v1.constants import SubjectType

app = FastAPI(title="Moviebox Local API Server", version="1.0.0")

# Enable CORS for frontend clients (Astro app)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Map endpoint action parameter to Homepage Operating List titles
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

async def get_cached_homepage():
    import time
    now = time.time()
    if homepage_cache["data"] is not None and (now - homepage_cache["timestamp"]) < CACHE_TTL:
        return homepage_cache["data"]
    
    session = Session()
    h = Homepage(session)
    content = await h.get_content_model()
    homepage_cache["data"] = content
    homepage_cache["timestamp"] = now
    return content

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
        
    return {
        "title": item.title,
        "poster": poster,
        "year": year,
        "detailPath": item.detailPath,
        "rating": rating,
        "type": item_type
    }

async def fetch_category_items(action: str, page: int = 1):
    if page == 1:
        content = await get_cached_homepage()
        target_title = CATEGORY_MAP.get(action)
        if not target_title:
            return []
            
        target_title_clean = re.sub(r'[^a-zA-Z0-9]', '', target_title).lower()
        
        # Match from operatingList
        for op in content.operatingList:
            op_title_clean = re.sub(r'[^a-zA-Z0-9]', '', op.title).lower()
            if op_title_clean == target_title_clean:
                return [format_item(item) for item in op.subjects]
                
        # Fallback partial match
        for op in content.operatingList:
            if target_title_clean in op_title_clean:
                return [format_item(item) for item in op.subjects]
                
    # Fallback to search query for page > 1 or if category is not found in homepage list
    fallback = SEARCH_FALLBACK_MAP.get(action)
    if fallback:
        query_str, subj_type = fallback
        session = Session()
        search_inst = Search(session, query=query_str, subject_type=subj_type, page=page)
        search_res = await search_inst.get_content_model()
        return [format_item(item) for item in search_res.items]
        
    return []

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
    try:
        items = await fetch_category_items("trending", page)
        return {"success": True, "items": items}
    except Exception as e:
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
        session = Session()
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
    session = Session()
    try:
        det = ItemDetails(session)
        details = await det.get_content_model(detailPath)
        
        genres = ", ".join(details.subject.genre) if details.subject.genre else "Drama"
        subject_type_val = details.subject.subjectType.value if hasattr(details.subject.subjectType, 'value') else int(details.subject.subjectType)
        is_tv = (subject_type_val == 2 or subject_type_val == 7)
        
        base_url = str(request.base_url).rstrip("/")
        poster_url = str(details.subject.cover.url) if details.subject.cover else ""
        encoded_title = quote(details.subject.title)
        encoded_poster = quote(poster_url)
        
        # We do NOT use .mp4 in media URL to prevent frontend Astro code from rendering HTML5 <video> tag.
        # This forces Astro to render it in an <iframe> using our custom Artplayer.
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
    client = httpx.AsyncClient(follow_redirects=True)
    try:
        resp = await client.get(target_url, headers=headers)
        return HTMLResponse(
            content=resp.text,
            status_code=resp.status_code,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Content-Type": resp.headers.get("content-type", "text/plain; charset=utf-8")
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await client.aclose()

@app.get("/api/v1/player", response_class=HTMLResponse)

async def stream_video_proxy(url: str, request: Request):
    headers = {
        "Accept": "*/*",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:137.0) Gecko/20100101 Firefox/137.0",
        "Origin": "https://videodownloader.site/",
        "Referer": "https://videodownloader.site/",
    }
    
    # Forward the Range header if present
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    # Create an HTTPX client
    client = httpx.AsyncClient(follow_redirects=True)
    
    try:
        # Build the streaming request
        req = client.build_request("GET", url, headers=headers)
        resp = await client.send(req, stream=True)
        
        # Propagate content-related headers back to the browser
        send_headers = {}
        for h in ["content-type", "content-length", "content-range", "accept-ranges"]:
            if h in resp.headers:
                send_headers[h] = resp.headers[h]
                
        # Support CORS
        send_headers["Access-Control-Allow-Origin"] = "*"
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
            headers=send_headers
        )
    except Exception as e:
        await client.aclose()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/play-media/{detailPath}")
async def play_movie_media(detailPath: str, request: Request):
    session = Session()
    try:
        det = ItemDetails(session)
        details = await det.get_content_model(detailPath)
        dl = DownloadableMovieFilesDetail(session, details.subject)
        dl_meta = await dl.get_content_model()
        best_file = dl_meta.best_media_file
        return await stream_video_proxy(str(best_file.url), request)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/play-media/{detailPath}/{season}/{episode}")
async def play_episode_media(detailPath: str, season: int, episode: int, request: Request):
    session = Session()
    try:
        det = ItemDetails(session)
        details = await det.get_content_model(detailPath)
        dl = DownloadableTVSeriesFilesDetail(session, details.subject)
        dl_meta = await dl.get_content_model(season=season, episode=episode)
        best_file = dl_meta.best_media_file
        return await stream_video_proxy(str(best_file.url), request)
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
