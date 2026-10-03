"""Song-to-song similarity and nearest-neighbour playlist scoring.

A playlist's average profile is a poor target when the playlist is deliberately
mixed: the average resembles everything, so it attracts everything. Scoring a song
against its k most similar songs in each playlist avoids that, because a mixed
playlist only wins when it really holds songs like this one.

Similarity of two songs (0..1) combines:
  language   same language (instrumental matches anything halfway); gates the rest
  style      same style 1, same style family 0.5
  mood       same mood 1, same mood group 0.5
  energy     1 - |difference| / 4
  sound      exp(-mean squared distance) of the standardised preview features
  artist     a credited artist in common (added on top; mostly discounted across languages)
"""
import numpy as np

import labels
from suggest import credited

MOOD_GROUPS = {
    "hype": "intense", "aggressive": "intense", "confident": "swagger", "dark": "swagger",
    "chill": "smooth", "dreamy": "smooth", "romantic": "feels", "heartbreak": "feels",
    "melancholic": "feels", "introspective": "feels", "nostalgic": "feels",
    "feel-good": "upbeat", "euphoric": "upbeat",
}
# Chosen on the leave-one-out benchmark (evaluate.py): 86.8% right first pick vs 70.7% for the
# old average-profile fit and 40.6% for "biggest playlist". Nearby settings score within ~1 point.
WEIGHTS = {"style": 0.2, "mood": 0.3, "energy": 0.15, "sound": 0.7, "artist": 0.8, "language": 0.0,
           "artist_cross_language": 0.25}
K = 5
SHOW_AT = 0.5   # on the benchmark, top picks scoring 0.5+ were right 91% of the time
SHOW_BEST_AT = 0.15  # the single best match is shown with less (top picks are right 87% overall)


class SimilarityModel:
    def __init__(self, lib, eff, z, weights=None):
        self.w = dict(WEIGHTS, **(weights or {}))
        tracks = [t for t in lib["tracks"] if t["persistentID"] in eff]
        self.ids = [t["persistentID"] for t in tracks]
        self.index = {pid: i for i, pid in enumerate(self.ids)}
        lab = [eff[p] for p in self.ids]

        def codes(values):
            vocab = {v: i for i, v in enumerate(sorted(set(values)))}
            return np.array([vocab[v] for v in values])

        def same(values):
            c = codes(values)
            return (c[:, None] == c[None, :]).astype(float)

        lang = [l["language"] for l in lab]
        same_lang = same(lang)
        inst = np.array([x == "instrumental" for x in lang])
        same_lang = np.maximum(same_lang, 0.5 * (inst[:, None] | inst[None, :]))
        style = same([l["style"] for l in lab])
        family = same([labels.FAMILY.get(l["style"], "other") for l in lab])
        style_sim = np.maximum(style, 0.5 * family)
        mood = same([l["mood"] for l in lab])
        mood_sim = np.maximum(mood, 0.5 * same([MOOD_GROUPS.get(l["mood"], l["mood"]) for l in lab]))
        e = np.array([l["energy"] for l in lab], dtype=float)
        energy_sim = 1 - np.abs(e[:, None] - e[None, :]) / 4

        has = np.array([p in z for p in self.ids])
        dim = len(next(iter(z.values()))) if z else 1
        zz = np.array([z[p] if p in z else np.zeros(dim) for p in self.ids])
        with np.errstate(all="ignore"):
            sq = (zz ** 2).sum(axis=1)
            d2 = np.maximum(sq[:, None] + sq[None, :] - 2 * zz @ zz.T, 0) / dim
        sound_sim = np.exp(-d2)
        sound_ok = has[:, None] & has[None, :]

        artists = [credited(t) for t in tracks]
        by_artist = {}
        for i, names in enumerate(artists):
            for a in names:
                by_artist.setdefault(a, []).append(i)
        artist_sim = np.zeros((len(tracks), len(tracks)))
        for members in by_artist.values():
            if len(members) < 60:  # skip giant "artists" like "various artists"
                artist_sim[np.ix_(members, members)] = 1

        w = self.w
        with_sound = w["style"] * style_sim + w["mood"] * mood_sim + w["energy"] * energy_sim + w["sound"] * sound_sim
        without = (w["style"] * style_sim + w["mood"] * mood_sim + w["energy"] * energy_sim) \
            * (w["style"] + w["mood"] + w["energy"] + w["sound"]) / (w["style"] + w["mood"] + w["energy"])
        vibe = np.where(sound_ok, with_sound, without) / (w["style"] + w["mood"] + w["energy"] + w["sound"])
        # Sharing a language counts for something on its own (a Telugu dance song still belongs
        # with Telugu melodies more than with English pop), the rest of the vibe scales the remainder.
        # A shared artist across languages (A.R. Rahman in Tamil and in Hindi) counts much less.
        artist_part = artist_sim * (same_lang + w["artist_cross_language"] * (1 - same_lang))
        self.sim = same_lang * (w["language"] + (1 - w["language"]) * vibe) + w["artist"] * artist_part
        np.fill_diagonal(self.sim, 0)

    def playlist_score(self, tid, members, k=5):
        """Mean similarity of `tid` to its k most similar songs among `members`."""
        i = self.index.get(tid)
        rows = [self.index[m] for m in members if m in self.index and m != tid]
        if i is None or not rows:
            return 0.0
        sims = np.sort(self.sim[i, rows])[::-1][:k]
        return float(sims.mean())

    def neighbours(self, tid, members, n=2):
        """The most similar members, for explaining a suggestion."""
        i = self.index.get(tid)
        rows = [m for m in members if m in self.index and m != tid]
        if i is None or not rows:
            return []
        return sorted(rows, key=lambda m: -self.sim[i, self.index[m]])[:n]


def score_playlist(model, track, tid, prof, members, album_words):
    """(score, reason) for adding a song to a playlist; the one rule set the app and benchmark share.

    Artist playlists only take that artist (score 2); a themed playlist (e.g. a soundtrack) takes
    songs from matching albums (1.5) and nothing else; everything else is nearest-neighbour similarity.
    """
    if prof.get("artist"):
        if prof["artist"] in credited(track):
            return 2.0, f"artist playlist for {prof['artist'].title()}"
        return -1.0, "different artist"
    if prof.get("theme"):
        if prof["theme"] not in album_words(track["album"]):
            return -1.0, f"themed playlist ({prof['theme']})"
        return 1.5, f"same {prof['theme']} soundtrack"
    return model.playlist_score(tid, members, K), ""
