import json
import re
import time
import requests
import hashlib
import os

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
                    "clientVersion": "1.20240301.01.00"
                }
            }
        }
        self._setup_auth()

    def _setup_auth(self):
        """Extracts SAPISID cookie and generates the SAPISIDHASH auth header."""
        cookie_str = self.headers.get("cookie", "")
        cookies = {c.split("=")[0]: c.split("=", 1)[1] for c in cookie_str.split("; ")}
        sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
        if not sapisid:
            raise Exception("SAPISID cookie not found in browser.json!")
            
        origin = "https://music.youtube.com"
        timestamp = int(time.time())
        hash_input = f"{timestamp} {sapisid} {origin}"
        sapisidhash = hashlib.sha1(hash_input.encode('utf-8')).hexdigest()
        
        auth_string = f"SAPISIDHASH {timestamp}_{sapisidhash}"
        self.session.headers.update({"Authorization": auth_string})
        self.session.headers.update({"Origin": origin})
        self.session.headers.update({"X-Origin": origin})

    def _post(self, endpoint, body=None):
        url = f"https://music.youtube.com/youtubei/v1/{endpoint}"
        payload = self.base_body.copy()
        if body:
            payload.update(body)
        resp = self.session.post(url, json=payload)
        if resp.status_code != 200:
            raise Exception(f"API Error {resp.status_code}: {resp.text}")
        return resp.json()

    def test_auth(self):
        """Tests if the token is actually valid by hitting an authenticated endpoint."""
        try:
            self._post("account/account_menu", {})
            return True
        except:
            return False

    def search_song(self, query, required_artist=""):
        body = {
            "query": query,
            "params": "EgWKAQIIAWoKEAoQBRAFEAM%3D"
        }
        data = self._post("search", body)
        
        songs = []
        try:
            tabs = data['contents']['tabbedSearchResultsRenderer']['tabs']
            for tab in tabs:
                sections = tab.get('tabRenderer', {}).get('content', {}).get('sectionListRenderer', {}).get('contents', [])
                for section in sections:
                    if 'musicShelfRenderer' in section:
                        contents = section['musicShelfRenderer'].get('contents', [])
                        for item in contents:
                            item_r = item.get('musicResponsiveListItemRenderer', {})
                            if not item_r: continue
                            
                            title = ""
                            try:
                                runs = item_r['flexColumns'][0]['musicResponsiveListItemFlexColumnRenderer']['text']['runs']
                                title = "".join([r['text'] for r in runs])
                            except: pass
                            
                            subtitle = ""
                            try:
                                runs = item_r['flexColumns'][1]['musicResponsiveListItemFlexColumnRenderer']['text']['runs']
                                subtitle = "".join([r['text'] for r in runs])
                            except: pass
                            
                            vid = ""
                            try:
                                vid = item_r['overlay']['musicItemThumbnailOverlayRenderer']['content']['musicPlayButtonRenderer']['playNavigationEndpoint']['watchEndpoint']['videoId']
                            except: pass
                            
                            if vid and len(vid) == 11 and title:
                                songs.append({"title": title, "subtitle": subtitle, "videoId": vid})
        except Exception:
            pass

        if not songs:
            def find_video_id(d):
                if isinstance(d, dict):
                    if "videoId" in d and len(str(d.get("videoId"))) == 11:
                        return d["videoId"]
                    if "watchEndpoint" in d and isinstance(d["watchEndpoint"], dict):
                        vid = d["watchEndpoint"].get("videoId")
                        if vid and len(vid) == 11:
                            return vid
                    if "playNavigationEndpoint" in d and isinstance(d["playNavigationEndpoint"], dict):
                        we = d["playNavigationEndpoint"].get("watchEndpoint", {})
                        if isinstance(we, dict) and we.get("videoId"):
                            return we["videoId"]
                    for v in d.values():
                        res = find_video_id(v)
                        if res: return res
                elif isinstance(d, list):
                    for item in d:
                        res = find_video_id(item)
                        if res: return res
                return None
            return find_video_id(data)

        if required_artist:
            req_lower = required_artist.lower()
            for song in songs:
                if req_lower in song['subtitle'].lower():
                    return song['videoId']
            return None
            
        return songs[0]['videoId']

    def create_playlist(self, title, description=""):
        body = {
            "title": title,
            "description": description,
            "privacyStatus": "PRIVATE"
        }
        data = self._post("playlist/create", body)
        return data.get('playlistId')

    def add_items(self, playlist_id, video_ids):
        chunk_size = 20
        for i in range(0, len(video_ids), chunk_size):
            chunk = video_ids[i:i + chunk_size]
            actions = []
            for vid in chunk:
                actions.append({
                    "action": "ACTION_ADD_VIDEO",
                    "addedVideoId": vid,
                    "dedupeOption": "DEDUPE_OPTION_SKIP"
                })
            body = {
                "playlistId": playlist_id,
                "actions": actions
            }
            data = self._post("browse/edit_playlist", body)
            status = data.get("status", "")
            if status != "STATUS_SUCCEEDED":
                raise Exception(f"YouTube rejected the songs. Response: {data}")
            time.sleep(1)
        return True

def get_spotify_track_info(url):
    m = re.search(r"track/([a-zA-Z0-9]+)", url)
    if not m: return None
    track_id = m.group(1)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://open.spotify.com/"
    }
    
    try:
        r = requests.get(f"https://open.spotify.com/embed/track/{track_id}", headers=headers, timeout=10)
        if r.status_code == 429:
            print("   [Spotify rate limit reached. Waiting 10 seconds...]")
            time.sleep(10)
            return get_spotify_track_info(url)
            
        if r.status_code == 200:
            match = re.search(r'<title>(.*?) - song by (.*?) \| Spotify</title>', r.text)
            if match:
                return {"title": match.group(1), "artist": match.group(2), "query": f"{match.group(1)} {match.group(2)}"}
                
            match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', r.text, re.DOTALL)
            if match:
                data = json.loads(match.group(1))
                def find_track(d):
                    if isinstance(d, dict):
                        if d.get("uri", "").startswith("spotify:track:"):
                            name = d.get("name", "")
                            artists_list = d.get("artists", [])
                            artist_names = [a.get("name") for a in artists_list if isinstance(a, dict) and a.get("name")]
                            if name and artist_names:
                                return {"title": name, "artist": " ".join(artist_names), "query": f"{name} {' '.join(artist_names)}"}
                        for v in d.values():
                            res = find_track(v)
                            if res: return res
                    elif isinstance(d, list):
                        for item in d:
                            res = find_track(item)
                            if res: return res
                    return None
                track_data = find_track(data)
                if track_data:
                    return track_data
    except:
        pass
        
    try:
        r = requests.get(f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{track_id}", headers=headers, timeout=10)
        if r.status_code == 429:
            print("   [Spotify rate limit reached. Waiting 10 seconds...]")
            time.sleep(10)
            return get_spotify_track_info(url)
            
        if r.status_code == 200:
            title = r.json().get("title", "")
            if " - " in title:
                parts = title.rsplit(" - ", 1)
                return {"title": parts[0], "artist": parts[1], "query": f"{parts[0]} {parts[1]}"}
    except:
        pass
    return None

def parse_batch_file(filepath):
    playlists = []
    current_playlist = None
    
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
                
            if line.startswith("http"):
                if current_playlist:
                    current_playlist['urls'].append(line)
            else:
                if current_playlist and current_playlist['urls']:
                    playlists.append(current_playlist)
                current_playlist = {"name": line, "urls": []}
                
    if current_playlist and current_playlist['urls']:
        playlists.append(current_playlist)
        
    return playlists

def process_single_playlist(yt, playlist_title, urls):
    print(f"\n{'='*50}")
    print(f"Processing Playlist: {playlist_title} ({len(urls)} songs)")
    print(f"{'='*50}")
    
    try:
        playlist_id = yt.create_playlist(playlist_title)
        print(f"Created playlist '{playlist_title}' (ID: {playlist_id})")
    except Exception as e:
        print(f"Failed to create playlist: {e}")
        return None

    video_ids = []
    print("\nSearching for songs...")
    for i, url in enumerate(urls, 1):
        track_info = get_spotify_track_info(url)
        
        if not track_info or not track_info.get("artist"):
            print(f"[{i}/{len(urls)}] Skipped (Could not extract artist): {url}")
            time.sleep(1.5)
            continue

        query = track_info["query"]
        artist = track_info["artist"]

        try:
            vid = yt.search_song(query, artist)
            if vid:
                video_ids.append(vid)
                print(f"[{i}/{len(urls)}] Found match for: {query}")
            else:
                print(f"[{i}/{len(urls)}] Skipped (artist mismatch on YouTube): {query}")
        except Exception as e:
            print(f"[{i}/{len(urls)}] Error: {e}")
        
        time.sleep(1.5)

    if video_ids:
        print(f"\nAdding {len(video_ids)} songs to YouTube Music playlist...")
        try:
            yt.add_items(playlist_id, video_ids)
            print("Successfully added all songs!")
        except Exception as e:
            print(f"Failed to add songs: {e}")
    else:
        print("\nNo songs were found to add.")
        
    return f"https://music.youtube.com/playlist?list={playlist_id}"

def main():
    print("=== Spotify to YouTube Music Converter ===")
    
    if not os.path.exists("browser.json"):
        print("Error: browser.json not found.")
        print("Please run 'python auto_auth.py' first to log into your Google account.")
        return
        
    try:
        yt = YTDirect("browser.json")
    except Exception as e:
        print(f"Auth setup failed: {e}")
        return

    # VERIFY AUTH BEFORE DOING ANYTHING
    print("Verifying YouTube Music login...")
    if not yt.test_auth():
        print("\nERROR: Your login token is invalid or expired!")
        print("Please delete 'browser.json' and run 'python auto_auth.py' again to get a fresh login.")
        return
    print("YouTube Music authenticated.")

    final_links = []

    if os.path.exists("batch.txt"):
        print("\nFound 'batch.txt'. Starting Batch Mode...")
        try:
            playlists = parse_batch_file("batch.txt")
        except Exception as e:
            print(f"Error reading batch.txt: {e}")
            return

        if not playlists:
            print("batch.txt is empty or formatted incorrectly.")
            return

        print(f"Loaded {len(playlists)} playlists from batch file.")
        
        for idx, pl in enumerate(playlists, 1):
            print(f"\nStarting playlist {idx} of {len(playlists)}...")
            link = process_single_playlist(yt, pl['name'], pl['urls'])
            if link:
                final_links.append({"name": pl['name'], "link": link})
            
            if idx < len(playlists):
                print("\nWaiting 5 seconds before next playlist to avoid rate limits...")
                time.sleep(5)

    elif os.path.exists("urls.txt"):
        print("\nFound 'urls.txt'. Starting Single Playlist Mode...")
        playlist_title = input("Enter a name for your new YouTube Music playlist (or press Enter for 'Spotify Import'): ").strip()
        if not playlist_title:
            playlist_title = "Spotify Import"
            
        try:
            with open("urls.txt", encoding="utf-8") as f:
                urls = [l.strip() for l in f if l.strip()]
        except Exception as e:
            print(f"Error reading urls.txt: {e}")
            return

        if not urls:
            print("urls.txt is empty.")
            return

        link = process_single_playlist(yt, playlist_title, urls)
        if link:
            final_links.append({"name": playlist_title, "link": link})
            
    else:
        print("Error: Neither 'batch.txt' nor 'urls.txt' found.")
        return

    print("\n" + "="*50)
    print("ALL DONE! Here are your playlists:")
    for item in final_links:
        print(f"\n{item['name']}:\n{item['link']}")
    print("="*50)

if __name__ == "__main__":
    main()