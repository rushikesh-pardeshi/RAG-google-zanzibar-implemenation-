"""Simulate user queries against OpenFGA the way your RAG app would.

Each simulated query:
  1. picks a random user
  2. pretends the vector DB returned N candidate chunks (random docs)
  3. dedupes to unique doc ids and checks them (batch-check in chunks of 50)

Modes:
  batch  - check every candidate document (default)
  check  - same, but one /check per document
  folder - check each candidate's folder once + read the user's direct document shares once;
           doc allowed = folder allowed OR doc directly shared (same answer as document#viewer)

Usage:
  python loadtest.py --requests 1000 --concurrency 10
  python loadtest.py --query-folders 5                 # candidates come from 5 folders (realistic RAG)
  python loadtest.py --query-folders 5 --mode folder   # same candidates, folder-level checks
  python loadtest.py --mode check          # if your OpenFGA has no batch-check
  python loadtest.py --list-objects 20     # also time list-objects for 20 users
"""
import argparse
import asyncio
import random
import statistics
import time

import httpx

from common import FGA_URL, load_state

BATCH = 50  # default server limit for checks per batch-check request


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


def report(name, ms):
    if not ms:
        return
    print(
        f"{name:<22} n={len(ms):<6} avg={statistics.mean(ms):7.1f}ms  "
        f"p50={pct(ms, .5):7.1f}  p95={pct(ms, .95):7.1f}  p99={pct(ms, .99):7.1f}  max={max(ms):7.1f}"
    )


async def main(args):
    s = load_state()
    base = f"/stores/{s['store_id']}"
    folders_n = s.get("folders", 500)
    random.seed(args.seed)
    req_ms, call_ms, denied, allowed_n, errors, check_errors = [], [], 0, 0, 0, 0

    async with httpx.AsyncClient(
        base_url=FGA_URL, timeout=60, limits=httpx.Limits(max_connections=args.concurrency * 4)
    ) as c:

        async def one_call(payload_checks):
            nonlocal errors, check_errors
            t0 = time.perf_counter()
            if args.mode in ("batch", "folder"):
                r = await c.post(
                    f"{base}/batch-check",
                    json={"checks": payload_checks, "authorization_model_id": s["model_id"]},
                )
                ok = r.status_code == 200
                res = r.json().get("result", {}) if ok else {}
                item_errs = [v["error"] for v in res.values() if v.get("error")]
                if item_errs:
                    check_errors += len(item_errs)
                    if check_errors <= len(item_errs):
                        print("check error inside batch:", str(item_errs[0])[:200])
                out = {k: v.get("allowed", False) for k, v in res.items() if not v.get("error")}
            else:  # plain /check, run in parallel
                async def single(chk):
                    rr = await c.post(
                        f"{base}/check",
                        json={"tuple_key": chk["tuple_key"], "authorization_model_id": s["model_id"]},
                    )
                    return chk["correlation_id"], rr.status_code == 200 and rr.json().get("allowed", False), rr.status_code == 200

                results = await asyncio.gather(*[single(x) for x in payload_checks])
                ok = all(x[2] for x in results)
                out = {k: a for k, a, _ in results}
            call_ms.append((time.perf_counter() - t0) * 1000)
            if not ok:
                errors += 1
                if errors <= 3:
                    print("error:", r.status_code if args.mode != "check" else "check", (r.text[:200] if args.mode != "check" else ""))
            return out

        async def direct_shares(user):
            """All documents shared directly with the user (one paginated Read)."""
            nonlocal errors
            t0 = time.perf_counter()
            shared, token = set(), None
            while True:
                body = {"tuple_key": {"user": user, "relation": "viewer", "object": "document:"}, "page_size": 100}
                if token:
                    body["continuation_token"] = token
                r = await c.post(f"{base}/read", json=body)
                if r.status_code != 200:
                    errors += 1
                    if errors <= 3:
                        print("read error:", r.status_code, r.text[:200])
                    break
                data = r.json()
                shared.update(t["key"]["object"] for t in data.get("tuples", []))
                token = data.get("continuation_token")
                if not token:
                    break
            call_ms.append((time.perf_counter() - t0) * 1000)
            return shared

        def pick_candidates():
            if not args.query_folders:
                return {f"d{random.randrange(s['docs'])}" for _ in range(args.candidates)}
            # seed.py puts doc d in folder d % folders, so docs of folder f are f, f+folders, f+2*folders, ...
            fs = random.sample(range(folders_n), args.query_folders)
            per_folder = s["docs"] // folders_n
            cand = set()
            for _ in range(args.candidates):
                cand.add(f"d{random.choice(fs) + folders_n * random.randrange(per_folder)}")
            return cand

        async def one_query(_):
            nonlocal denied, allowed_n
            user = f"user:u{random.randrange(s['users'])}"
            cand = pick_candidates()
            t0 = time.perf_counter()
            if args.mode == "folder":
                folder_of = {d: f"f{int(d[1:]) % folders_n}" for d in cand}
                checks = [
                    {"tuple_key": {"user": user, "relation": "viewer", "object": f"folder:{f}"}, "correlation_id": f}
                    for f in set(folder_of.values())
                ]
                chunks = [checks[i : i + BATCH] for i in range(0, len(checks), BATCH)]
                *outs, shared = await asyncio.gather(*[one_call(ch) for ch in chunks], direct_shares(user))
                folder_ok = {k: v for o in outs for k, v in o.items()}
                req_ms.append((time.perf_counter() - t0) * 1000)
                ok_n = sum(1 for d, f in folder_of.items() if folder_ok.get(f) or f"document:{d}" in shared)
                allowed_n += ok_n
                denied += len(cand) - ok_n
                return
            checks = [
                {
                    "tuple_key": {"user": user, "relation": "viewer", "object": f"document:{d}"},
                    "correlation_id": d,
                }
                for d in cand
            ]
            chunks = [checks[i : i + BATCH] for i in range(0, len(checks), BATCH)]
            outs = await asyncio.gather(*[one_call(ch) for ch in chunks])
            req_ms.append((time.perf_counter() - t0) * 1000)
            for o in outs:
                allowed_n += sum(o.values())
                denied += len(o) - sum(o.values())

        sem = asyncio.Semaphore(args.concurrency)

        async def guarded(i):
            async with sem:
                await one_query(i)

        print(
            f"mode={args.mode} requests={args.requests} concurrency={args.concurrency} "
            f"candidates/query={args.candidates} query-folders={args.query_folders or 'all'}"
        )
        t0 = time.time()
        await asyncio.gather(*[guarded(i) for i in range(args.requests)])
        wall = time.time() - t0

        print(f"\nwall time {wall:.1f}s  -> {args.requests / wall:.1f} queries/s sustained, errors={errors}, check errors={check_errors}")
        print(f"allowed={allowed_n:,} denied={denied:,} (denied share {denied / max(1, allowed_n + denied):.0%})")
        report("per query (all checks)", req_ms)
        report("per OpenFGA call", call_ms)

        if args.list_objects:
            lo_ms, sizes = [], []
            for _ in range(args.list_objects):
                u = f"user:u{random.randrange(s['users'])}"
                t0 = time.perf_counter()
                r = await c.post(
                    f"{base}/list-objects",
                    json={"user": u, "relation": "viewer", "type": "document", "authorization_model_id": s["model_id"]},
                )
                lo_ms.append((time.perf_counter() - t0) * 1000)
                sizes.append(len(r.json().get("objects", [])) if r.status_code == 200 else -1)
            print()
            report("list-objects", lo_ms)
            print(f"list-objects returned {min(sizes)}..{max(sizes)} docs (server caps this; compare with real access)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--requests", type=int, default=1000)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--candidates", type=int, default=100, help="chunks returned by the vector DB per query")
    p.add_argument("--mode", choices=["batch", "check", "folder"], default="batch")
    p.add_argument("--query-folders", type=int, default=0, help="candidates come from this many folders (0 = random docs)")
    p.add_argument("--list-objects", type=int, default=0)
    p.add_argument("--seed", type=int, default=7, help="use a different seed per process when running several in parallel")
    asyncio.run(main(p.parse_args()))
