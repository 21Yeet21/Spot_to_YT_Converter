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

    def search_song(self, query):
        body = {
            "query": query,
            "params": "EgWKAQIIAWoKEAoQBRAFEAM%3D" # Filter: Songs only
        }
        data = self._post("search", body)
        
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

    def create_playlist(self, title, description=""):
        body = {
            "title": title,
            "description": description,
            "privacyStatus": "PRIVATE"
        }
        data = self._post("playlist/create", body)
        return data.get('playlistId')

    def add_items(self, playlist_id, video_ids):
        actions = [{"action": "ACTION_ADD_VIDEO", "addedVideoId": vid} for vid in video_ids]
        body = {
            "playlistId": playlist_id,
            "actions": actions
        }
        return self._post("browse/edit_playlist", body)

def spotify_url_to_query(url):
    m = re.search(r"track/([a-zA-Z0-9]+)", url)
    if not m: return ""
    track_id = m.group(1)
    try:
        r = requests.get(f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{track_id}")
        if r.status_code == 200:
            return r.json().get("title", "")
    except:
        pass
    return ""

def main():
    print("=== Spotify to YouTube Music Converter ===")
    
    # Check if authenticated
    if not os.path.exists("browser.json"):
        print("Error: browser.json not found.")
        print("Please run 'python auto_auth.py' first to log into your Google account.")
        return
        
    # Ask for Playlist Name
    playlist_title = input("Enter a name for your new YouTube Music playlist (or press Enter for 'Spotify Import'): ").strip()
    if not playlist_title:
        playlist_title = "Spotify Import"
        
    # Check for urls.txt
    if not os.path.exists("urls.txt"):
        print("Error: urls.txt not found.")
        print("Please create a file named 'urls.txt' and paste your Spotify track URLs inside (one per line).")
        return

    try:
        with open("urls.txt", encoding="utf-8") as f:
            urls = [l.strip() for l in f if l.strip()]
    except Exception as e:
        print(f"Error reading urls.txt: {e}")
        return

    if not urls:
        print("urls.txt is empty. Please add some Spotify track URLs.")
        return

    print(f"\nLoaded {len(urls)} Spotify URLs.")

    try:
        yt = YTDirect("browser.json")
        print("YouTube Music authenticated.")
    except Exception as e:
        print(f"Auth setup failed: {e}")
        print("Try running 'python auto_auth.py' again to generate a fresh browser.json.")
        return

    try:
        playlist_id = yt.create_playlist(playlist_title)
        print(f"Created playlist '{playlist_title}'")
    except Exception as e:
        print(f"Failed to create playlist: {e}")
        return

    video_ids = []
    print("\nSearching for songs...")
    for i, url in enumerate(urls, 1):
        query = spotify_url_to_query(url)
        if not query:
            print(f"[{i}/{len(urls)}] Could not resolve {url}")
            continue

        try:
            vid = yt.search_song(query)
            if vid:
                video_ids.append(vid)
                print(f"[{i}/{len(urls)}] Found match for: {query}")
            else:
                print(f"[{i}/{len(urls)}] No YT match for: {query}")
        except Exception as e:
            print(f"[{i}/{len(urls)}] Error: {e}")
        time.sleep(0.5) # Be polite to YouTube's servers

    if video_ids:
        print(f"\nAdding {len(video_ids)} songs to YouTube Music playlist...")
        try:
            yt.add_items(playlist_id, video_ids)
            print("Successfully added all songs!")
        except Exception as e:
            print(f"Failed to add songs: {e}")
    else:
        print("\nNo songs were found to add.")

    # Print the final Playlist Link
    playlist_url = f"https://music.youtube.com/playlist?list={playlist_id}"
    print("\n" + "="*50)
    print("Done! Your playlist is ready here:")
    print(f"{playlist_url}")
    print("="*50)

if __name__ == "__main__":
    main()