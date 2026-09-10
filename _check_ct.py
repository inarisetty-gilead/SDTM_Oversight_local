import json
import urllib.request

def get(url):
    with urllib.request.urlopen(url) as r:
        return json.load(r)

ct = get("http://127.0.0.1:8020/api/domain/TU/variable/TUMETHOD/ct")
print(json.dumps({k: v for k, v in ct.items() if k != "terms"}, indent=2))
