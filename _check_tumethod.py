import json
import urllib.request

def get(url):
    with urllib.request.urlopen(url) as r:
        return json.load(r)

st = get("http://127.0.0.1:8020/api/state")
built = st.get("built") or []
print("built domains:", built)

for dom in built:
    try:
        d = get(f"http://127.0.0.1:8020/api/domain/{dom}")
    except Exception:
        continue
    var = next((v for v in d["variables"] if v["variable"] == "TUMETHOD"), None)
    if var:
        print(dom, "TUMETHOD:", json.dumps(var, indent=2))
