import asyncio
import re
import json
from html import unescape
import requests
from playwright.async_api import async_playwright
from demjson3 import decode
import aiohttp

ODESLI_API = "https://api.song.link/v1-alpha.1/links"
DEEZER_SEARCH_API = "https://api.deezer.com/search/album"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://google.com"
}


def parse_quietus_title(text: str) -> tuple[str | None, str]:
    """Parse Quietus article title into artist and album."""
    text = unescape(text)
    text = re.sub(r"\s*\|\s*The Quietus.*$", "", text, flags=re.I)
    text = re.sub(r"\s+review$", "", text, flags=re.I)
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    if " by " in text:
        album, artist = text.rsplit(" by ", 1)
        return artist.strip(), album.strip()
    m = re.search(r"([\w\s]+?)['’]s\s+(.+)", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    tokens = text.split()
    if len(tokens) >= 2:
        return tokens[0].strip(), " ".join(tokens[1:]).strip()
    return None, text.strip()

def infer_artist_album_from_url(url: str):
    if "rateyourmusic.com" in url:
        match = re.search(r"/release/album/([^/]+)/([^/]+)/?$", url)
        if match:
            artist = match.group(1).replace("-", " ").title()
            album = match.group(2).replace("-", " ").title()
            return artist, album

    if "albumoftheyear.org" in url:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.ok:
                match = re.search(r'<meta property="og:title" content="([^"]+)"', resp.text)
                if match and " - " in match.group(1):
                    artist, album = match.group(1).split(" - ", 1)
                    return artist.strip(), album.strip()
        except Exception as e:
            print(f"[WARN] AOTY metadata fetch failed: {e}")

        match = re.search(r"/album/\d+-([^.]+)\.php", url)
        if match:
            slug = match.group(1).split("-")
            if len(slug) >= 3:
                artist = " ".join(slug[:-2]).title()
                album = " ".join(slug[-2:]).title()
            elif len(slug) == 2:
                artist = slug[0].title()
                album = slug[1].title()
            else:
                artist = " ".join(slug[:1]).title()
                album = " ".join(slug[1:]).title()
            return artist, album

    if "thequietus.com" in url:
        match = re.search(r"/articles/\d+-([a-z0-9-]+)-review/?$", url)
        if match:
            slug = match.group(1).split("-")
            if len(slug) >= 4:
                artist = " ".join(slug[:2]).title()
                album = " ".join(slug[2:]).title()
            else:
                artist = " ".join(slug[:1]).title()
                album = " ".join(slug[1:]).title()
            return artist, album

        match = re.search(r"/quietus-reviews/album-of-the-week/([a-z0-9-]+)/?", url)
        if match:
            slug = match.group(1)
            slug = slug.removesuffix("-review")
            if "-by-" in slug:
                album_part, artist_part = slug.rsplit("-by-", 1)
                artist = artist_part.replace("-", " ").title()
                album = album_part.replace("-", " ").title()
                return artist, album
            if "-is-our-album-of-the-week" in slug:
                album = slug.split("-is-our-album-of-the-week", 1)[0].replace("-", " ").title()
                tokens = album.split()
                if tokens:
                    artist = tokens[0].title()
                    album = " ".join(tokens[1:]) or album
                else:
                    artist = None
                return artist, album

        # Fallback to fetching OpenGraph metadata
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.ok:
                m = re.search(r'<meta property="og:title" content="([^\"]+)"', resp.text)
                if m:
                    return parse_quietus_title(m.group(1))
        except Exception as e:
            print(f"[WARN] Quietus metadata fetch failed: {e}")

    if "pitchfork.com" in url:
        match = re.search(r"/reviews/albums/([a-z0-9-]+)/?", url)
        if match:
            slug = match.group(1).split("-")
            if len(slug) >= 3:
                artist = slug[0].title()
                album = " ".join(slug[1:]).title()
                return artist, album

    return None, None

def deezer_search_url(artist, album):
    query = f"{artist} {album}"
    try:
        res = requests.get(
            DEEZER_SEARCH_API,
            params={"q": query},
            headers=HEADERS,
            timeout=10,
        )
        res.raise_for_status()
        data = res.json()
        if data["data"]:
            return data["data"][0]["link"]
    except Exception as e:
        print(f"[WARN] Deezer search failed: {e}")
    return None


def parse_plexamp_share(url: str):
    try:
        html = requests.get(url, headers=HEADERS, timeout=10).text
        match = re.search(r"const data = (\[.*?\]);", html, re.S)
        if not match:
            return None
        js = match.group(1)
        js = js.replace("void 0", "null")
        js = re.sub(r"__sveltekit_\w+\.defer\(1\)", "null", js)
        data = decode(js)
        album = data[2]["data"]["response"]
        artist = album.get("parentTitle", "Unknown Artist")
        album_title = album.get("title", "Unknown Album")
        cover = album.get("art", {}).get("thumb")
        return {
            "artist": artist,
            "album": album_title,
            "source": url,
            "cover_url": cover,
            "links": {"plexamp": {"url": url}},
        }
    except Exception as e:
        print(f"[ERROR parse_plexamp_share] {e}")
        return None

async def extract_metadata(url: str, artist: str | None = None, album: str | None = None):
    try:
        title, cover = None, None
        bandcamp_links = {}
        original_url = url

        if "thequietus.com" in url:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=10)
                if resp.ok:
                    html_text = resp.text
                    title_match = re.search(r'<meta property="og:title" content="([^\"]+)"', html_text)
                    if title_match:
                        a2, b2 = parse_quietus_title(title_match.group(1))
                        artist = artist or a2
                        album = album or b2
                    img_match = re.search(r'<meta property="og:image" content="([^\"]+)"', html_text)
                    if img_match:
                        cover = img_match.group(1)
            except Exception as e:
                print(f"[WARN quietus metadata fetch] {e}")

        if any(domain in url for domain in ["listen.plex.tv/album", "app.plexamp.com/album"]):
            plex_data = await asyncio.to_thread(parse_plexamp_share, url)
            if plex_data:
                return plex_data

        # Bandcamp Daily articles
        if "daily.bandcamp.com" in url:
            try:
                resp = requests.get(url, headers=HEADERS, timeout=10)
                if resp.ok:
                    html_text = resp.text
                    m = re.search(r'data-player-infos="([^"]+)"', html_text)
                    if m:
                        try:
                            info = json.loads(unescape(m.group(1)))[0]
                            if info.get("tralbum_url"):
                                url = info["tralbum_url"]
                        except Exception as e:
                            print(f"[WARN daily bandcamp parse] {e}")
                    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html_text)
                    if title_match:
                        t = title_match.group(1)
                        mm = re.match(r"(.+?), [\"\u201C](.+?)[\"\u201D]", t)
                        if mm:
                            artist, album = mm.group(1), mm.group(2)
                        elif " - " in t:
                            artist, album = t.split(" - ", 1)
                        else:
                            artist = album = t
            except Exception as e:
                print(f"[WARN fetch daily bandcamp] {e}")

        # Bandcamp album pages
        if "bandcamp.com" in url:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await (await browser.new_context()).new_page()
                await page.goto(url, timeout=20000)
                await page.wait_for_timeout(3000)
                title = await page.title()
                html = await page.content()
                match = re.search(r'<meta property="og:image" content="([^"]+)"', html)
                cover = match.group(1) if match else None

                artist_match = re.search(r'"artist":"([^"]+)"', html)
                if artist_match:
                    artist = artist_match.group(1)

                album_match = re.search(r'"album_title":"([^"]+)"', html)
                if album_match:
                    album = album_match.group(1)

                if (not artist or not album) and title:
                    title_match = re.search(r"(.+?)\s*\|\s*(.+)", title)
                    if title_match:
                        album = album or title_match.group(1).strip()
                        artist = artist or title_match.group(2).strip()

                await browser.close()
            bandcamp_links = {"bandcamp": {"url": url}}

        elif any(site in url for site in ["rateyourmusic.com", "albumoftheyear.org", "pitchfork.com", "thequietus.com"]):
            if not artist or not album:
                artist, album = await asyncio.to_thread(infer_artist_album_from_url, url)

        stream_url = await asyncio.to_thread(deezer_search_url, artist or "", album or "") if album else None
        odesli_input = stream_url or url

        async with aiohttp.ClientSession() as session:
            async with session.get(
                ODESLI_API,
                params={"url": odesli_input},
                headers=HEADERS,
                timeout=10,
            ) as res:
                data = await res.json()

        entity_id = data.get("entityUniqueId")
        entity = data.get("entitiesByUniqueId", {}).get(entity_id, {})

        artist = entity.get("artistName", artist or "Unknown Artist")
        album = entity.get("title", album or "Unknown Album")
        cover = entity.get("thumbnailUrl", cover)
        links = data.get("linksByPlatform", {})
        if bandcamp_links:
            links = {**bandcamp_links, **links}
        elif "bandcamp.com" in url and "bandcamp" not in links:
            links["bandcamp"] = {"url": url}

        return {
            "artist": artist,
            "album": album,
            "source": original_url,
            "cover_url": cover,
            "links": links
        }

    except Exception as e:
        print(f"[ERROR] Failed to extract from {url}: {e}")
        return {
            "artist": "Unknown",
            "album": "Unknown",
            "source": original_url,
            "cover_url": None,
            "links": {}
        }
