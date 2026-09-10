import json
import urllib.request

def get(url):
    with urllib.request.urlopen(url) as r:
        return json.load(r)

ct = get("http://127.0.0.1:8020/api/domain/TU/variable/TUMETHOD/ct")
out = {k: v for k, v in ct.items() if k not in ("terms", "submission_values")}
print(json.dumps(out, indent=2)[:4000])
