import json
import os
import time
import requests
import hashlib
import undetected_chromedriver as uc

def is_auth_valid():
    """Actually tests the token against YouTube's servers."""
    if not os.path.exists("browser.json"):
        return False
        
    try:
        with open("browser.json", "r", encoding="utf-8") as f:
            headers = json.load(f)
            
        cookie_str = headers.get("cookie", "")
        cookies = {c.split("=")[0]: c.split("=", 1)[1] for c in cookie_str.split("; ") if "=" in c}
        sapisid = cookies.get("SAPISID") or cookies.get("__Secure-3PAPISID")
        if not sapisid:
            return False
            
        origin = "https://music.youtube.com"
        timestamp = int(time.time())
        hash_input = f"{timestamp} {sapisid} {origin}"
        sapisidhash = hashlib.sha1(hash_input.encode('utf-8')).hexdigest()
        auth_string = f"SAPISIDHASH {timestamp}_{sapisidhash}"
        
        session = requests.Session()
        session.headers.update({
            "Authorization": auth_string,
            "Origin": origin,
            "X-Origin": origin,
            "User-Agent": headers.get("User-Agent", ""),
            "Content-Type": "application/json"
        })
        
        # Ping YouTube's account menu. If it returns 200, the token is alive.
        body = {"context": {"client": {"clientName": "WEB_REMIX", "clientVersion": "1.20240301.01.00"}}}
        resp = session.post("https://music.youtube.com/youtubei/v1/account/account_menu", json=body)
        return resp.status_code == 200
        
    except Exception:
        return False

def do_browser_login():
    """Launches UNDETECTED browser, waits for login, extracts perfect cookies."""
    profile_dir = os.path.join(os.getcwd(), "chrome_profile")
    os.makedirs(profile_dir, exist_ok=True)

    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile_dir}")
    
    print("Launching undetected Chrome...")
    try:
        driver = uc.Chrome(options=options)
    except Exception as e:
        print(f"Failed to launch Chrome: {e}")
        print("Try deleting the 'chrome_profile' folder and run this script again.")
        return

    try:
        driver.get("https://music.youtube.com")
        
        print("\n" + "="*50)
        print("ACTION REQUIRED IN BROWSER:")
        print("1. If you are not logged in, click 'Sign In' and log in.")
        print("2. If you ARE already logged in (you see your avatar), do nothing.")
        print("3. Wait for the page to fully load.")
        print("DO NOT close the Chrome window yourself!")
        print("="*50)
        input(">>> Press ENTER in THIS terminal when the page is loaded <<<\n")

        print("Extracting cookies and building browser.json...")
        
        # Poll for the critical cookie
        for _ in range(15):
            cookies = driver.get_cookies()
            names = {c["name"] for c in cookies}
            if "SAPISID" in names or "__Secure-3PAPISID" in names:
                break
            time.sleep(2)
        else:
            raise SystemExit("Login failed. SAPISID cookie not found. Try again.")

        # Deduplicate cookies
        cookies_dict = {}
        for c in cookies:
            cookies_dict[c["name"]] = c["value"]
            
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
        ua = driver.execute_script("return navigator.userAgent;")
        
        headers = {
            "User-Agent": ua,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
            "content-type": "application/json",
            "origin": "https://music.youtube.com",
            "referer": "https://music.youtube.com/",
            "cookie": cookie_str  # MUST BE LOWERCASE
        }
        
        with open("browser.json", "w", encoding="utf-8") as f:
            json.dump(headers, f, indent=4)
            
        print("Success! browser.json created.")
    except Exception as e:
        print(f"\nError during cookie extraction: {e}")
        print("The Chrome window may have closed unexpectedly. Please try running the script again.")
    finally:
        try:
            driver.quit()
        except:
            pass

def main():
    if is_auth_valid():
        print("You are already authenticated! browser.json is valid.")
        return
        
    print("Authentication missing or expired. Starting browser login...")
    do_browser_login()
    
    if is_auth_valid():
        print("\nVerification successful! You are ready to run your converter.")
    else:
        print("\nVerification failed. Check if you completed the login properly.")

if __name__ == "__main__":
    main()