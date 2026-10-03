"""Check energy labels against the measured audio.

A ridge regression maps preview audio features to energy (1-5), trained on the
high- and medium-confidence labels. 5-fold cross-validation reports how well
sound alone predicts energy, so the model's own accuracy is known before it is
used. Labels the audio strongly disagrees with are listed for review; nothing
is changed automatically.
"""
import numpy as np

import labels
import metadata
from artist import FEATURES, vector


def dataset(lib, confidences=("high", "medium")):
    audio, eff = metadata.load("audio"), labels.merged()
    rows = []
    for t in lib["tracks"]:
        f, lab = audio.get(t["persistentID"]), eff.get(t["persistentID"])
        v = vector(f or {})
        if v and lab:
            rows.append((t, lab, v))
    x = np.array([r[2] for r in rows], dtype=float)
    return rows, x


class EnergyModel:
    def __init__(self, x, y, alpha=1.0):
        self.mean, self.std = x.mean(axis=0), x.std(axis=0) + 1e-9
        z = (x - self.mean) / self.std
        z1 = np.hstack([z, np.ones((len(z), 1))])
        reg = alpha * np.eye(z1.shape[1])
        reg[-1, -1] = 0  # don't shrink the intercept
        with np.errstate(all="ignore"):  # macOS Accelerate BLAS raises spurious FP warnings
            self.w = np.linalg.solve(z1.T @ z1 + reg, z1.T @ y)

    def predict(self, x):
        z = (np.atleast_2d(x) - self.mean) / self.std
        with np.errstate(all="ignore"):
            return np.clip(np.hstack([z, np.ones((len(z), 1))]) @ self.w, 1, 5)


def cross_validate(x, y, folds=5, seed=0):
    idx = np.random.default_rng(seed).permutation(len(y))
    pred = np.empty(len(y))
    for k in range(folds):
        test = idx[k::folds]
        train = np.setdiff1d(idx, test)
        pred[test] = EnergyModel(x[train], y[train]).predict(x[test])
    return pred


def check(lib, threshold=1.25):
    rows, x = dataset(lib)
    if len(rows) < 60:
        raise SystemExit(f"Only {len(rows)} labelled tracks have audio; run `enrich audio` first.")
    y = np.array([r[1]["energy"] for r in rows], dtype=float)
    trusted = np.array([r[1].get("confidence") in ("high", "medium") or r[1].get("source", "").startswith("manual")
                        for r in rows])
    cv = cross_validate(x[trusted], y[trusted])
    baseline = np.abs(y[trusted] - y[trusted].mean()).mean()
    report = {
        "tracks": len(rows), "trained_on": int(trusted.sum()),
        "cv_mae": float(np.abs(cv - y[trusted]).mean()), "baseline_mae": float(baseline),
        "cv_corr": float(np.corrcoef(cv, y[trusted])[0, 1]),
        "within_1": float((np.abs(cv - y[trusted]) <= 1).mean()),
    }
    model = EnergyModel(x[trusted], y[trusted])
    weights = dict(zip(FEATURES, model.w[:-1].round(3)))
    pred = model.predict(x)
    flagged = [(r[0], r[1], float(p)) for r, p, ok in zip(rows, pred, trusted)
               if not ok and abs(p - r[1]["energy"]) >= threshold]
    flagged.sort(key=lambda f: -abs(f[2] - f[1]["energy"]))
    return report, weights, flagged


# --- Crowd check (Last.fm listener tags) -------------------------------------------

CALM_TAGS = {"chill", "chillout", "chill out", "mellow", "relaxing", "relax", "calm", "sad", "melancholic",
             "melancholy", "slow", "ballad", "ballads", "acoustic", "sleep",
             "soft", "lo-fi", "lofi", "dreamy", "night", "late night", "emotional", "heartbreak",
             "atmospheric", "ambient", "downtempo", "soothing", "sad songs", "rainy day", "piano"}
ENERGY_TAGS = {"hype", "energetic", "energy", "aggressive", "workout", "gym", "party", "dance", "banger",
               "bangers", "upbeat", "hard", "intense", "club", "rage", "high energy", "edm", "drill", "crunk",
               "festival", "motivation", "motivational", "pump up", "running", "dancefloor", "fun", "anthem"}


# Topic tags ("love", "romantic", "beautiful") say what a song is about, not how energetic it is,
# so they're not in either list. Tags only a few listeners applied (weight < MIN_WEIGHT of 100)
# are noise.
MIN_WEIGHT = 10


def crowd_energy(entry):
    """-1 (listeners call it calm) .. +1 (listeners call it energetic); None without clear mood tags."""
    calm = sum(w for t, w in entry.get("tags", []) if t in CALM_TAGS)
    hype = sum(w for t, w in entry.get("tags", []) if t in ENERGY_TAGS)
    return (hype - calm) / (hype + calm) if hype + calm >= MIN_WEIGHT else None


def crowd_check(lib, plan=None):
    """Do listeners agree with the energy labels and with the split groups?"""
    tags, eff = metadata.load("lastfm"), labels.merged()
    rows = []
    for t in lib["tracks"]:
        pid = t["persistentID"]
        e = tags.get(pid)
        if not e or pid not in eff or e.get("from") != "track":
            continue  # artist-level tags say nothing about a particular song's energy
        c = crowd_energy(e)
        if c is not None:
            rows.append((t, eff[pid], c))
    out = {"songs_with_tags": sum(1 for v in tags.values() if v.get("from") == "track"),
           "songs_with_mood_tags": len(rows)}
    if len(rows) >= 20:
        lab = np.array([r[1]["energy"] for r in rows], dtype=float)
        crowd = np.array([r[2] for r in rows])
        out["corr"] = float(np.corrcoef(lab, crowd)[0, 1])
        out["by_energy"] = {int(e): (float(crowd[lab == e].mean()), int((lab == e).sum())) for e in sorted(set(lab))}
        out["disagree"] = sorted([(t, l, c) for t, l, c in rows if (l["energy"] >= 4 and c <= -0.5)
                                  or (l["energy"] <= 2 and c >= 0.5)], key=lambda r: -abs(r[2]))
    if plan:
        groups = {}
        for op in plan["ops"]:
            if op["op"] == "add_tracks" and op["playlist"].get("ref"):
                vals = [crowd_energy(tags[t["id"]]) for t in op["tracks"]
                        if t["id"] in tags and tags[t["id"]].get("from") == "track"]
                vals = [v for v in vals if v is not None]
                groups[op["playlist"]["name"]] = (op["playlist"]["ref"].split("-")[-1], vals)
        out["groups"] = groups
    return out
