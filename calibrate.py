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
