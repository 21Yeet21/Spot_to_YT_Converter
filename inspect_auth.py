import json, os
from ytmusicapi import YTMusic

filepath = "browser.json"
print(f"Current working directory: {os.getcwd()}")
print(f"Looking for file at absolute path: {os.path.abspath(filepath)}")

if os.path.exists(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    
    print("\n--- FILE CONTENTS ---")
    print(content)
    print("--- END FILE CONTENTS ---\n")
    
    try:
        data = json.loads(content)
        print("Keys found in JSON:", list(data.keys()))
        print("\nAttempting to initialize YTMusic with this data...")
        yt = YTMusic(auth=data)
        print("✅ SUCCESS! Auth is valid.")
    except Exception as e:
        print(f"❌ ERROR: {e}")
else:
    print("❌ browser.json does not exist in this folder!")