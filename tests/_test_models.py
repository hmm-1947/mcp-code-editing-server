import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent))
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from agent import modelcache, openrouter, settings

PROMPT = "What is 17 * 23? Reply with exactly one line: ANSWER: <number>. Nothing else."
cfg = settings.load()


def jobs_for(which):
    key = cfg[f"{which}_api_key"]
    if not key:
        return []
    models = list(cfg[f"{which}_models"])
    if not models and which == "openrouter":
        models = modelcache.load(which)
    base = openrouter.BASE if which == "openrouter" else cfg[f"{which}_base_url"]
    return [(which, base, key, m) for m in models]


def run(job):
    which, base, key, model = job
    start = time.time()
    try:
        reply = openrouter.chat(key, model, [{"role": "user", "content": PROMPT}], max_tokens=400, timeout=60, base=base)
        text = re.sub(r"<think>.*?</think>", "", reply["text"], flags=re.S).strip()
        status = "ok"
    except openrouter.BackendError as error:
        text, status = str(error)[:120], error.kind
    except Exception as error:
        text, status = str(error)[:120], "error"
    secs = round(time.time() - start, 1)
    correct = status == "ok" and "391" in text
    exact = status == "ok" and re.fullmatch(r"ANSWER:\s*391\.?", text) is not None
    return {"provider": which, "model": model, "status": status, "secs": secs,
            "correct": correct, "exact": exact, "reply": text[:120].replace("\n", " ")}


def run_provider(which):
    jobs = jobs_for(which)
    out = []
    if which in ("openrouter", "requesty"):
        for job in jobs:
            row = run(job)
            if row["status"] in ("rate", "transient"):
                time.sleep(8)
                row = run(job)
            out.append(row)
            time.sleep(3.5 if which == "openrouter" else 6)
        return out
    with ThreadPoolExecutor(3) as pool:
        return list(pool.map(run, jobs))


providers = sys.argv[1:] or ["openrouter", "requesty", "gemini", "nvidia"]
with ThreadPoolExecutor(len(providers)) as pool:
    results = [r for batch in pool.map(run_provider, providers) for r in batch]

with open("_test_models_results.json", "w", encoding="utf-8") as fh:
    json.dump(results, fh, indent=1)

for r in sorted(results, key=lambda r: (r["provider"], not r["exact"], not r["correct"], r["secs"])):
    flag = "EXACT" if r["exact"] else "RIGHT" if r["correct"] else r["status"].upper() if r["status"] != "ok" else "WRONG"
    print(f'{r["provider"][:4]} {flag:6} {r["secs"]:5}s {r["model"]} | {r["reply"][:60]}')
print(len(results), "models tested")
