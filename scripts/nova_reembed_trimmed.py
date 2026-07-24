#!/usr/bin/env python3
"""
nova_reembed_trimmed.py — re-embed the 13,277 wiki memories whose cruft tails were
trimmed in #553. Their TEXT was shortened in `memories` but their embeddings still
reflect the old (cruft-laden) text, so recall is degraded. This re-embeds each row's
CURRENT trimmed text via the .10 CPU embed node and updates memories.embedding (#632).

Idempotent + resumable: re-running just re-embeds again (no harm). Run with nohup.
"""
import json
import sys
import time
import urllib.request

import psycopg2

EMBED_URL = "http://192.168.1.10:11434/api/embed"
DSN = "host=pg-primary.digitalnoise.net dbname=nova_memories user=kochj"
BATCH_COMMIT = 200


def embed(text):
    req = urllib.request.Request(
        EMBED_URL,
        data=json.dumps({"model": "nomic-embed-text", "input": text[:6000]}).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=45).read())["embeddings"][0]


def main():
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    cur.execute("SELECT b.id FROM memory_cruft_backup_553 b JOIN memories m ON m.id = b.id ORDER BY b.id")
    ids = [r[0] for r in cur.fetchall()]
    total = len(ids)
    print(f"[reembed] {total} trimmed memories to re-embed via .10", flush=True)

    done = errs = 0
    t0 = time.time()
    for i, mid in enumerate(ids, 1):
        cur.execute("SELECT text FROM memories WHERE id = %s", (mid,))
        row = cur.fetchone()
        if not row or not row[0]:
            continue
        try:
            emb = embed(row[0])
            cur.execute("UPDATE memories SET embedding = %s::vector WHERE id = %s", (str(emb), mid))
            done += 1
        except Exception as e:
            errs += 1
            if errs <= 10:
                print(f"[reembed] err id={mid}: {e}", flush=True)
        if i % BATCH_COMMIT == 0:
            conn.commit()
            rate = i / max(1, time.time() - t0)
            eta = (total - i) / max(0.1, rate)
            print(f"[reembed] {i}/{total} ({done} ok, {errs} err) ~{rate:.1f}/s ETA {eta/60:.0f}m", flush=True)
    conn.commit()
    print(f"[reembed] DONE: {done}/{total} re-embedded, {errs} errors, {(time.time()-t0)/60:.0f}m", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
