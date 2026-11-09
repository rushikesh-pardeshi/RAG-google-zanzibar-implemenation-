"""Seed OpenFGA with synthetic data.

Default: 200,000 documents, 500 folders, 50 groups, 1,000 users.
Usage:  python seed.py [--docs 200000] [--folders 500] [--groups 50] [--users 1000] [--concurrency 8]
"""
import argparse
import asyncio
import random
import time

import httpx

from common import FGA_URL, MODEL, save_state

WRITE_BATCH = 100  # OpenFGA's default max tuples per Write call
RETRIES = 5


async def main(args):
    random.seed(42)
    async with httpx.AsyncClient(base_url=FGA_URL, timeout=60) as c:
        store = (await c.post("/stores", json={"name": f"rag-test-{int(time.time())}"})).json()
        store_id = store["id"]
        r = await c.post(f"/stores/{store_id}/authorization-models", json=MODEL)
        r.raise_for_status()
        model_id = r.json()["authorization_model_id"]
        print(f"store={store_id} model={model_id}")

        tuples = []
        t = lambda u, rel, o: {"user": u, "relation": rel, "object": o}

        # users -> groups (each user in 1-3 groups)
        for u in range(args.users):
            for g in random.sample(range(args.groups), random.randint(1, 3)):
                tuples.append(t(f"user:u{u}", "member", f"group:g{g}"))

        # folders readable by 1-2 groups
        for f in range(args.folders):
            for g in random.sample(range(args.groups), random.randint(1, 2)):
                tuples.append(t(f"group:g{g}#member", "viewer", f"folder:f{f}"))

        # documents -> folder (one parent tuple each) + ~5% direct user shares
        for d in range(args.docs):
            tuples.append(t(f"folder:f{d % args.folders}", "parent", f"document:d{d}"))
            if random.random() < 0.05:
                tuples.append(t(f"user:u{random.randrange(args.users)}", "viewer", f"document:d{d}"))

        print(f"writing {len(tuples):,} tuples in batches of {WRITE_BATCH} ...")
        sem = asyncio.Semaphore(args.concurrency)
        done = 0
        failed = 0
        start = time.time()

        async def write(batch):
            nonlocal done, failed
            async with sem:
                for attempt in range(1, RETRIES + 1):
                    try:
                        r = await c.post(
                            f"/stores/{store_id}/write",
                            json={"writes": {"tuple_keys": batch}, "authorization_model_id": model_id},
                        )
                        # a timed-out write may still have committed; the retry then reports duplicates
                        if r.status_code == 200 or (r.status_code == 400 and "already exist" in r.text):
                            break
                        err = f"{r.status_code} {r.text[:200]}"
                    except httpx.HTTPError as e:
                        err = e.__class__.__name__
                    if attempt == RETRIES:
                        failed += len(batch)
                        print(f"write failed after {RETRIES} attempts: {err}")
                        break
                    await asyncio.sleep(2 ** attempt)
                done += len(batch)
                if done % 20000 < WRITE_BATCH:
                    print(f"  {done:,}/{len(tuples):,}")

        await asyncio.gather(*[write(tuples[i : i + WRITE_BATCH]) for i in range(0, len(tuples), WRITE_BATCH)])
        print(f"done in {time.time() - start:.1f}s, failed tuples: {failed:,}")

    save_state({"store_id": store_id, "model_id": model_id, "docs": args.docs, "users": args.users, "folders": args.folders})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--docs", type=int, default=200_000)
    p.add_argument("--folders", type=int, default=500)
    p.add_argument("--groups", type=int, default=50)
    p.add_argument("--users", type=int, default=1000)
    p.add_argument("--concurrency", type=int, default=8, help="parallel Write requests")
    asyncio.run(main(p.parse_args()))
