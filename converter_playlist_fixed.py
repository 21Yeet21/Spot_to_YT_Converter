import json
import os
import re
import time
import hashlib
import html
import requests


SPOTIFY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://open.spotify.com/",
}


class YTDirect:
    def __init__(self, browser_json_path="browser.json"):
        with open(browser_json_path, "r", encoding="utf-8") as f:
            self.headers = json.load(f)

        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.base_body = {
            "context": {
                "client": {
                    "clientName": "WEB_REMIX",
                    "clientVersion": "1.20240301.01.00",
                }
            }
        }
        self._setup_auth()

    def _setup_auth(self):
        cookie_str = self.headers.get("cookie", "")
        cookies = {}
        for item in cookie_str.split(";"):
            if "=" in item:
                key, value = item.strip().split("=", 1)
                cookies[key] = value

        sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
        if not sapisid:
            raise RuntimeError("SAPISID cookie not found in browser.json. Run auto_auth.py again.")

        origin = "https://music.youtube.com"
        timestamp = int(time.time())
        hash_input = f"{timestamp} {sapisid} {origin}"
        sapisidhash = hashlib.sha1(hash_input.encode("utf-8")).hexdigest()
        self.session.headers.update({
            "Authorization": f"SAPISIDHASH {timestamp}_{sapisidhash}",
            "Origin": origin,
            "X-Origin": origin,
        })

    def _post(self, endpoint, body=None):
        url = f"https://music.youtube.com/youtubei/v1/{endpoint}"
        payload = dict(self.base_body)
        if body:
            payload.update(body)
        response = self.session.post(url, json=payload, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"YouTube Music API error {response.status_code}: {response.text[:500]}")
        return response.json()

    def test_auth(self):
        try:
            self._post("account/account_menu")
            return True
        except Exception:
            return False

    def search_song(self, query, required_artist=""):
        """Search YouTube Music and return a video ID with a plausible artist match.

        YouTube Music changes its response nesting from time to time, so walk
        the response recursively instead of relying on one fixed path.
        """
        data = self._post("search", {
            "query": query,
            "params": "EgWKAQIIAWoKEAoQBRAFEAM%3D",
        })

        def walk(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from walk(child)
            elif isinstance(value, list):
                for child in value:
                    yield from walk(child)

        def renderer_text(renderer, column_index):
            try:
                runs = renderer["flexColumns"][column_index][
                    "musicResponsiveListItemFlexColumnRenderer"
                ]["text"]["runs"]
                return " ".join(
                    str(run.get("text", "")).strip()
                    for run in runs if run.get("text")
                )
            except (KeyError, IndexError, TypeError, AttributeError):
                return ""

        def find_video_id(value):
            for node in walk(value):
                # Prefer the usual play-button endpoint, but accept other
                # watch endpoints used by different YouTube Music responses.
                if node.get("videoId") and isinstance(node.get("videoId"), str):
                    vid = node["videoId"]
                    if len(vid) == 11:
                        return vid
            return ""

        def normalize(text):
            text = str(text or "").casefold().replace("\u00a0", " ")
            text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
            return re.sub(r"\s+", " ", text).strip()

        required_names = [normalize(name) for name in re.split(r",", required_artist or "")]
        required_names = [name for name in required_names if name]
        songs = []
        seen = set()

        for node in walk(data):
            renderer = node.get("musicResponsiveListItemRenderer")
            if not isinstance(renderer, dict):
                continue
            title = renderer_text(renderer, 0)
            subtitle = renderer_text(renderer, 1)
            video_id = find_video_id(renderer)
            if not title or not video_id or video_id in seen:
                continue
            seen.add(video_id)
            songs.append({"title": title, "subtitle": subtitle, "videoId": video_id})

        # If YouTube Music's filtered result layout changes, retry an unfiltered
        # search once; this also avoids interpreting an empty parsed shelf as no match.
        if not songs:
            data = self._post("search", {"query": query})
            for node in walk(data):
                renderer = node.get("musicResponsiveListItemRenderer")
                if not isinstance(renderer, dict):
                    continue
                title = renderer_text(renderer, 0)
                subtitle = renderer_text(renderer, 1)
                video_id = find_video_id(renderer)
                if title and video_id and video_id not in seen:
                    seen.add(video_id)
                    songs.append({"title": title, "subtitle": subtitle, "videoId": video_id})

        if not songs:
            return None
        if not required_names:
            return songs[0]["videoId"]

        # Spotify may list several artists, while YouTube Music can display only
        # the primary artist. Accept a match for any credited artist, ignoring
        # harmless punctuation/case differences and non-breaking spaces.
        for song in songs:
            subtitle_norm = normalize(song["subtitle"])
            padded_subtitle = f" {subtitle_norm} "
            if any(f" {name} " in padded_subtitle for name in required_names):
                return song["videoId"]

        return None

    def create_playlist(self, title, description=""):
        data = self._post("playlist/create", {
            "title": title,
            "description": description,
            "privacyStatus": "PRIVATE",
        })
        playlist_id = data.get("playlistId")
        if not playlist_id:
            raise RuntimeError(f"YouTube Music did not return a playlist ID: {data}")
        return playlist_id

    def add_items(self, playlist_id, video_ids):
        for start in range(0, len(video_ids), 20):
            actions = [
                {
                    "action": "ACTION_ADD_VIDEO",
                    "addedVideoId": video_id,
                    "dedupeOption": "DEDUPE_OPTION_SKIP",
                }
                for video_id in video_ids[start:start + 20]
            ]
            data = self._post("browse/edit_playlist", {
                "playlistId": playlist_id,
                "actions": actions,
            })
            if data.get("status") != "STATUS_SUCCEEDED":
                raise RuntimeError(f"YouTube Music rejected the songs: {data}")
            time.sleep(1)


def spotify_playlist_id(url):
    match = re.search(r"(?:open\.spotify\.com/|spotify:)(?:playlist/)?([A-Za-z0-9]{22})", url)
    if not match:
        match = re.search(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)", url)
    if not match:
        raise ValueError("That doesn't look like a Spotify playlist URL.")
    return match.group(1)


def walk_for_tracks(value, found):
    """Recursively collect track objects from Spotify's embedded page JSON."""
    if isinstance(value, dict):
        uri = value.get("uri", "")
        track_id = value.get("id", "")
        name = value.get("name") or value.get("title")
        artists = value.get("artists") or value.get("artist") or []
        # Spotify's embed payload often stores a row as {uri, title, subtitle};
        # unlike the Web API's Track object, it may not have name/artists fields.
        if not name:
            name = value.get("trackName")
        subtitle = value.get("subtitle", "")

        is_track = (
            isinstance(uri, str) and uri.startswith("spotify:track:")
        ) or (
            value.get("type") == "track" and isinstance(track_id, str) and track_id
        )

        if is_track and name:
            if isinstance(artists, dict):
                # Newer payloads sometimes wrap artists in an items list.
                artist_items = artists.get("items", [])
                artists = artist_items if artist_items else [artists]
            if isinstance(artists, list):
                artist_names = [
                    (a.get("profile", {}).get("name") or a.get("name", ""))
                    if isinstance(a, dict) else str(a)
                    for a in artists
                ]
            elif isinstance(artists, str):
                artist_names = [artists]
            else:
                artist_names = []
            artist_names = [a.strip() for a in artist_names if a and a.strip()]

            # Embed rows may expose artists as a plain subtitle string instead.
            if not artist_names and isinstance(subtitle, str) and subtitle.strip():
                subtitle = subtitle.strip()
                # The leading E is Spotify's explicit-content marker, not an artist name.
                if subtitle.startswith("E "):
                    subtitle = subtitle[2:].strip()
                artist_names = [subtitle] if subtitle else []

            if artist_names:
                key = uri if isinstance(uri, str) and uri else track_id or f"{name}|{artist_names[0]}"
                clean_title = str(name).strip()
                found.setdefault(key, {
                    "title": clean_title,
                    "artist": ", ".join(artist_names),
                    "query": f"{clean_title} {' '.join(artist_names)}",
                })

        for child in value.values():
            walk_for_tracks(child, found)
    elif isinstance(value, list):
        for child in value:
            walk_for_tracks(child, found)


def get_spotify_playlist(url):
    playlist_id = spotify_playlist_id(url)
    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"

    response = requests.get(embed_url, headers=SPOTIFY_HEADERS, timeout=25)
    if response.status_code == 429:
        raise RuntimeError("Spotify rate-limited this request. Wait a few minutes and try again.")
    response.raise_for_status()
    page = response.text

    title = f"Spotify Import {playlist_id}"
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = html.unescape(re.sub(r"\s+", " ", title_match.group(1))).strip()
        title = re.sub(r"\s*\|\s*Spotify\s*$", "", title, flags=re.IGNORECASE).strip() or title

    found = {}
    # Parse every script block containing JSON. Spotify changes script IDs and
    # attribute order periodically, so don't rely on one exact tag format.
    json_blobs = re.findall(r'<script\b[^>]*>(.*?)</script>', page,
                            flags=re.IGNORECASE | re.DOTALL)

    for blob in json_blobs:
        try:
            walk_for_tracks(json.loads(html.unescape(blob)), found)
        except (json.JSONDecodeError, TypeError):
            continue

    if not found:
        # A few page versions wrap the JSON data in HTML-escaped entities.
        # Try decoding the entire document once before giving up.
        decoded_page = html.unescape(page)
        if decoded_page != page:
            for blob in re.findall(r'<script\b[^>]*>(.*?)</script>', decoded_page,
                                   flags=re.IGNORECASE | re.DOTALL):
                try:
                    walk_for_tracks(json.loads(blob.strip()), found)
                except (json.JSONDecodeError, TypeError):
                    continue

    if not found:
        raise RuntimeError(
            "Spotify returned its playlist page, but the track data format wasn't recognized. "
            "This is a parser limitation, not proof that the playlist is private. "
            "Please send me the full error and I can add a parser for the exact response format."
        )

    return title, list(found.values())


def process_playlist(yt, playlist_title, tracks):
    print(f"\n{'=' * 55}")
    print(f"Spotify playlist: {playlist_title}")
    print(f"Tracks detected: {len(tracks)}")
    print(f"{'=' * 55}")

    try:
        yt_playlist_id = yt.create_playlist(playlist_title)
        print(f"Created YouTube Music playlist: {yt_playlist_id}")
    except Exception as exc:
        print(f"Failed to create YouTube Music playlist: {exc}")
        return None

    video_ids = []
    for index, track in enumerate(tracks, 1):
        title = track["title"]
        artist = track["artist"]
        query = track["query"]
        try:
            video_id = yt.search_song(query, artist)
            if video_id:
                video_ids.append(video_id)
                print(f"[{index}/{len(tracks)}] Matched: {title} — {artist}")
            else:
                print(f"[{index}/{len(tracks)}] Skipped (artist not verified): {title} — {artist}")
        except Exception as exc:
            print(f"[{index}/{len(tracks)}] Error matching {title}: {exc}")
        time.sleep(1.5)

    if video_ids:
        print(f"\nAdding {len(video_ids)} matched songs...")
        try:
            yt.add_items(yt_playlist_id, video_ids)
            print("Songs added.")
        except Exception as exc:
            print(f"Failed while adding songs: {exc}")
    else:
        print("No matching songs were found.")

    return f"https://music.youtube.com/playlist?list={yt_playlist_id}"


def main():
    print("=== Spotify → YouTube Music Playlist Converter ===")
    url = input("Paste your Spotify playlist link: ").strip()
    if not url:
        print("No URL provided.")
        return

    if not os.path.exists("browser.json"):
        print("Error: browser.json not found. Run py auto_auth.py first.")
        return

    try:
        print("\nReading Spotify playlist...")
        title, tracks = get_spotify_playlist(url)
        print(f"Detected playlist: {title}")
        print(f"Found {len(tracks)} track(s).")
    except Exception as exc:
        print(f"\nCould not read Spotify playlist: {exc}")
        return

    new_title = input(f"Name for the YouTube Music playlist [Enter to use '{title}']: ").strip()
    if new_title:
        title = new_title

    try:
        yt = YTDirect("browser.json")
    except Exception as exc:
        print(f"Authentication setup failed: {exc}")
        return

    print("\nVerifying YouTube Music login...")
    if not yt.test_auth():
        print("Your YouTube Music session is invalid or expired. Run py auto_auth.py again.")
        return
    print("YouTube Music authenticated.")

    link = process_playlist(yt, title, tracks)
    if link:
        print("\nFinished! Your YouTube Music playlist:")
        print(link)


if __name__ == "__main__":
    main()
