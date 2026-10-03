"""Benchmark for playlist fit, using your own playlists as the answer key.

Leave-one-out: each song is taken out of one of its playlists, then a scorer ranks
every playlist that doesn't already contain it (exactly what "Where do these
belong?" sees). A hit is when the playlist it came from ranks first (top-1) or in
the top 3. Mean reciprocal rank (MRR) summarises the whole ranking.

Artist playlists are reported separately: the artist rule makes them nearly free,
so they would flatter the numbers for the hard part (vibe and language playlists).
"""
import math
from collections import Counter, defaultdict

import numpy as np

import labels
import similarity
import suggest
import ui


class Context:
    """Everything a scorer may use, computed once."""

    def __init__(self, lib):
        self.lib = lib
        self.tracks = {t["persistentID"]: t for t in lib["tracks"]}
        self.eff = labels.merged()
        self.z = similarity.sound_vectors(lib)
        self.profiles = ui.profiles(lib, self.eff, self.z)
        self.members = {pid: [i for i in pr["ids"] if i in self.tracks] for pid, pr in self.profiles.items()}


def holdout_profile(ctx, pid, tid):
    """The playlist's profile as if `tid` weren't in it."""
    pr = dict(ctx.profiles[pid])
    lab = ctx.eff.get(tid)
    pr["ids"] = pr["ids"] - {tid}
    pr["size"] -= 1
    if lab:
        for key in ("mood", "style", "language"):
            c = Counter(pr[key])
            c[lab[key]] -= 1
            pr[key] = +c
        n = pr["n"]
        pr["energy"] = (pr["energy"] * n - lab["energy"]) / max(n - 1, 1)
        pr["n"] = max(n - 1, 1)
    if tid in ctx.z and pr.get("sound") is not None:
        k = sum(1 for i in pr["ids"] | {tid} if i in ctx.z)
        pr["sound"] = (pr["sound"] * k - ctx.z[tid]) / max(k - 1, 1)
    return pr


# --- Scorers: (ctx, track id, profile, members without the track) -> score ---------

def score_size(ctx, tid, pr, members):
    return pr["size"]


def score_genre(ctx, tid, pr, members):
    g = ctx.tracks[tid]["genre"]
    return sum(1 for i in members if ctx.tracks[i]["genre"] == g) / max(len(members), 1)


def score_current(ctx, tid, pr, members):
    return ui.fit(ctx.tracks[tid], ctx.eff.get(tid), pr, ctx.z.get(tid))[0]


def score_labels_only(ctx, tid, pr, members):
    return ui.fit(ctx.tracks[tid], ctx.eff.get(tid), pr, None)[0]


def knn_scorer(k=5, weights=None, theme_bonus=False, calibrate=None):
    """Nearest-neighbour scorer with the same artist-playlist and theme rules as the app.

    calibrate: None, "sub" (minus the playlist's typical score for any song) or "div" (divided by it).
    """
    key = ("knn", tuple(sorted((weights or {}).items())))

    def fn(ctx, tid, pr, members):
        from similarity import SimilarityModel
        if key not in ctx.__dict__:
            ctx.__dict__[key] = SimilarityModel(ctx.lib, ctx.eff, ctx.z, weights)
        model = ctx.__dict__[key]
        if pr["artist"]:
            return 2.0 if pr["artist"] in suggest.credited(ctx.tracks[tid]) else -1.0
        if pr["theme"]:
            if pr["theme"] not in ui.album_words(ctx.tracks[tid]["album"]):
                return -1.0
            if theme_bonus:
                return 1.5
        raw = model.playlist_score(tid, members, k)
        if not calibrate:
            return raw
        base_key = ("base", key, k, pr["playlist"]["persistentID"])
        if base_key not in ctx.__dict__:  # typical score for songs outside the playlist (sampled)
            outside = [t for t in model.ids if t not in pr["ids"]][::7]
            ctx.__dict__[base_key] = float(np.mean([model.playlist_score(t, members, k) for t in outside])) or 1e-6
        base = ctx.__dict__[base_key]
        return raw - base if calibrate == "sub" else raw / base
    return fn


def score_app(ctx, tid, pr, members):
    """Exactly what "Where do these belong?" uses."""
    from similarity import SimilarityModel, score_playlist
    if "app_model" not in ctx.__dict__:
        ctx.__dict__["app_model"] = SimilarityModel(ctx.lib, ctx.eff, ctx.z)
    return score_playlist(ctx.__dict__["app_model"], ctx.tracks[tid], tid, pr, members, ui.album_words)[0]


SCORERS = {"playlist size": score_size, "same genre share": score_genre,
           "old: average profile": score_current, "app: nearest neighbours": score_app}


def run(lib, scorers=None, include_artist=False):
    ctx = Context(lib)
    scorers = scorers or SCORERS
    cases = []
    for pid, pr in ctx.profiles.items():
        artist_pl = bool(pr["artist"])
        if artist_pl and not include_artist:
            continue
        for tid in ctx.members[pid]:
            if len(ctx.members[pid]) >= 4:
                cases.append((pid, tid, artist_pl))
    results = {}
    for name, fn in scorers.items():
        ranks, per, confusion = [], defaultdict(list), Counter()
        for pid, tid, _ in cases:
            members = [i for i in ctx.members[pid] if i != tid]
            scores = []
            for cand, pr in ctx.profiles.items():
                if cand == pid:
                    scores.append((fn(ctx, tid, holdout_profile(ctx, pid, tid), members), cand))
                elif tid not in pr["ids"]:
                    scores.append((fn(ctx, tid, pr, ctx.members[cand]), cand))
            scores.sort(key=lambda s: -s[0])
            rank = next(i for i, (_, c) in enumerate(scores, 1) if c == pid)
            ranks.append(rank)
            per[ctx.profiles[pid]["playlist"]["name"].strip()].append(rank)
            if rank > 1:
                confusion[(ctx.profiles[pid]["playlist"]["name"].strip(),
                           ctx.profiles[scores[0][1]]["playlist"]["name"].strip())] += 1
        r = np.array(ranks)
        results[name] = {
            "cases": len(r), "top1": float((r == 1).mean()), "top3": float((r <= 3).mean()),
            "mrr": float((1 / r).mean()),
            "per_playlist": {k: float((np.array(v) == 1).mean()) for k, v in per.items()},
            "confusion": confusion.most_common(8),
        }
    return results


def report(results):
    names = list(results)
    print(f"{'scorer':28} {'top-1':>7} {'top-3':>7} {'MRR':>6}")
    for n in names:
        r = results[n]
        print(f"{n:28} {r['top1']:7.1%} {r['top3']:7.1%} {r['mrr']:6.3f}")
    print(f"\n({results[names[0]]['cases']} held-out songs)\n\ntop-1 per playlist:")
    playlists = sorted(results[names[0]]["per_playlist"])
    print(f"{'':28}" + "".join(f"{n[:12]:>14}" for n in names))
    for p in playlists:
        print(f"{p[:28]:28}" + "".join(f"{results[n]['per_playlist'][p]:14.0%}" for n in names))
    best = names[-1]
    print(f"\nmost common mix-ups for {best!r} (true playlist -> picked instead):")
    for (a, b), n in results[best]["confusion"]:
        print(f"  {n:3}  {a} -> {b}")


# --- Split cohesion ------------------------------------------------------------------

def split_cohesion(lib, plan, samples=30, seed=0):
    """How much more alike each new playlist's songs *sound* than a random group of the same
    size from the same source (x1.0 = no better than random).

    Splits are formed from labels, so this uses measured preview audio only: an independent
    check rather than the model grading itself. (Clustering on the full similarity looks
    better on a similarity-based score but no better on this one.)
    """
    z = similarity.sound_vectors(lib)
    rng = np.random.default_rng(seed)

    def cohesion(ids):
        x = np.array([z[i] for i in ids if i in z])
        if len(x) < 2:
            return float("nan")
        d2 = ((x[:, None, :] - x[None, :, :]) ** 2).mean(axis=2)
        return float(np.exp(-d2)[~np.eye(len(x), dtype=bool)].mean())

    groups = {}
    for op in plan["ops"]:
        if op["op"] == "add_tracks" and op["playlist"].get("ref"):
            source = op["reason"].split("from ")[-1].split(";")[0].strip("'")
            groups.setdefault(source, []).append((op["playlist"]["name"], [t["id"] for t in op["tracks"]]))
    out = {}
    for source, gs in groups.items():
        pool = [i for _, ids in gs for i in ids if i in z]
        rows = []
        for name, ids in gs:
            n = sum(1 for i in ids if i in z)
            if n < 2:
                continue
            rand = np.mean([cohesion(list(rng.choice(pool, n, replace=False))) for _ in range(samples)])
            rows.append((name, n, cohesion(ids) / rand))
        out[source] = {"groups": rows, "weighted": sum(n * r for _, n, r in rows) / max(sum(n for _, n, _ in rows), 1)}
    return out
