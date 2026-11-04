import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent))
import ast
import json
import sys
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from agent import openrouter, policy, settings

PROMPT = (
    "Answer in exactly this format and nothing else.\n"
    "Line 1: A=<result of 17*23>\n"
    "Line 2: B=<the word 'stressed' reversed>\n"
    "Line 3: C=<the larger of 9.11 and 9.9>\n"
    "Then one python code block with def lcp(strs) returning the longest common prefix "
    "of a list of strings. No comments, no explanation."
)

TIMEOUT = 150
MAX_TOKENS = 1500
_or_lock = threading.Lock()
_or_last = [0.0]


def build_jobs():
    cfg = settings.load()
    jobs = []
    for name in ("gemini", "requesty", "nvidia"):
        key = cfg[name + "_api_key"]
        if key:
            for model in cfg[name + "_models"]:
                jobs.append((name, model, key, cfg[name + "_base_url"]))
    key = cfg["openrouter_api_key"]
    if key:
        ids = list(cfg["openrouter_models"])
        if not ids:
            probe = policy.Decision(kind="explore", complexity="medium")
            ids = [c.model for c in policy.openrouter_candidates(probe, 0, limit=40)]
        for model in ids:
            jobs.append(("openrouter", model, key, openrouter.BASE))
    return jobs


def score(text):
    clean = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    pts = 0
    a = re.search(r"A\s*=\s*([\d,\.]+)", clean)
    if a and a.group(1).replace(",", "").rstrip(".") == "391":
        pts += 1
    b = re.search(r"B\s*=\s*['\"`]?([A-Za-z]+)", clean)
    if b and b.group(1).lower() == "desserts":
        pts += 1
    c = re.search(r"C\s*=\s*([\d\.]+)", clean)
    if c and c.group(1).rstrip(".") == "9.9":
        pts += 1
    code = re.search(r"```(?:python|py)?\s*\n(.*?)```", clean, re.S)
    if code and "def lcp" in code.group(1):
        try:
            ast.parse(code.group(1))
            pts += 1
        except SyntaxError:
            pass
    return pts, clean


def run(job):
    backend, model, key, base = job
    if backend == "openrouter":
        with _or_lock:
            wait = 3.5 - (time.time() - _or_last[0])
            if wait > 0:
                time.sleep(wait)
            _or_last[0] = time.time()
    start = time.time()
    row = {"backend": backend, "model": model, "ok": False, "secs": 0.0, "pts": 0,
           "err_kind": "", "err": "", "ptok": 0, "ctok": 0, "text": ""}
    try:
        r = openrouter.chat(key, model, [{"role": "user", "content": PROMPT}], MAX_TOKENS, TIMEOUT, base)
        pts, clean = score(r["text"])
        row.update(ok=True, pts=pts, text=clean, ptok=r["prompt_tokens"], ctok=r["completion_tokens"])
    except openrouter.BackendError as e:
        row.update(err_kind=str(getattr(e, "kind", "")), err=str(e)[:200])
    except Exception as e:
        row.update(err_kind=type(e).__name__, err=str(e)[:200])
    row["secs"] = round(time.time() - start, 1)
    return row


def main():
    jobs = build_jobs()
    if len(sys.argv) > 2:
        jobs = [j for j in jobs if j[0] in sys.argv[2].split(",")]
    print(f"testing {len(jobs)} models", flush=True)
    rows = []
    with ThreadPoolExecutor(max_workers=int(sys.argv[1]) if len(sys.argv) > 1 else 10) as pool:
        futures = [pool.submit(run, j) for j in jobs]
        for f in as_completed(futures):
            r = f.result()
            rows.append(r)
            if r["ok"]:
                print(f"{r['backend']:10} {r['pts']}/4 {r['secs']:6.1f}s {r['ctok']:5}tok {r['model']}", flush=True)
            else:
                print(f"{r['backend']:10} FAIL {r['secs']:6.1f}s {r['model']} -> {r['err_kind']}: {r['err'][:110]}", flush=True)
    with open(sys.argv[3] if len(sys.argv) > 3 else "_agent_test_results.json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=1)
    print("DONE", flush=True)


main()
