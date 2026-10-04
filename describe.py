"""Build a playlist from a plain-English description.

    "hard gym rap like Kendrick and Travis, no slow songs"
    "late-night Telugu melodies"      "2010s boyband pop to sing along"

The description is read into constraints (sound, language, mood, use, energy, era,
artists to lean on or leave out, a source playlist, a size). Sound and language
are hard filters, so a playlist never mixes genres; when no sound is named, the
playlist keeps to the one style family that fits best. Mood, use and artists rank
the songs. Nothing is created here: the result is a song list to review.
"""
import re
from collections import Counter

import labels
import names
import plans
import reorg
import similarity
from suggest import credit_names, norm, song_key

# phrase -> (styles, families)
SOUNDS = {
    "hip hop": ([], ["rap"]), "hip-hop": ([], ["rap"]), "hiphop": ([], ["rap"]), "rap": ([], ["rap"]),
    "trap": (["trap", "melodic-rap", "rage"], []), "drill": (["drill"], []), "uk rap": (["uk-rap", "drill"], []),
    "grime": (["uk-rap"], []), "boom bap": (["boom-bap"], []), "conscious": (["conscious-rap"], []),
    "melodic rap": (["melodic-rap"], []), "rage": (["rage"], []), "west coast": (["west-coast"], []),
    "r&b": ([], ["rnb"]), "rnb": ([], ["rnb"]), "soul": ([], ["rnb"]), "funk": (["funk"], []),
    "boyband": (["teen-pop"], []), "boy band": (["teen-pop"], []), "teen pop": (["teen-pop"], []),
    "dance pop": (["dance-pop"], []), "synth": (["synth-pop"], []), "synthpop": (["synth-pop"], []),
    "ballad": (["pop-ballad", "acoustic-pop"], []), "ballads": (["pop-ballad", "acoustic-pop"], []),
    "acoustic": (["acoustic-pop", "folk"], []), "indie pop": (["indie-pop", "dream-pop"], []),
    "bedroom pop": (["indie-pop", "dream-pop"], []), "dream pop": (["dream-pop"], []), "pop": ([], ["pop"]),
    "indie": (["indie-rock", "indie-pop", "dream-pop"], []), "alt": (["alt-rock", "indie-rock"], []),
    "alternative": (["alt-rock", "indie-rock"], []), "hard rock": (["hard-rock"], []), "metal": (["hard-rock"], []),
    "classic rock": (["hard-rock"], []), "rock": ([], ["rock"]),
    "edm": (["edm"], []), "house": (["house"], []), "electronic": ([], ["electronic"]), "phonk": (["phonk"], []),
    "reggaeton": (["reggaeton"], []), "latin": ([], ["latin"]),
    "mass": (["film-dance"], []), "kuthu": (["film-dance"], []), "dance numbers": (["film-dance"], []),
    "melody": (["film-melody"], []), "melodies": (["film-melody"], []), "bgm": (["film-bgm"], []),
    "film": ([], ["indian-film"]), "movie": ([], ["indian-film"]), "sufi": (["sufi"], []),
    "devotional": (["devotional"], []), "desi indie": (["indian-indie"], []),
    "folk": (["folk"], []), "country": (["country"], []), "jazz": (["jazz"], []),
}
# Sounds that are really about who's singing.
SOUND_ARTISTS = {
    "boyband": ["One Direction", "Backstreet Boys", "*NSYNC", "Blue", "Jonas Brothers", "Westlife", "5 Seconds of Summer"],
    "boy band": ["One Direction", "Backstreet Boys", "*NSYNC", "Blue", "Jonas Brothers", "Westlife", "5 Seconds of Summer"],
}
LANGUAGES = {"telugu": "telugu", "tollywood": "telugu", "tamil": "tamil", "kollywood": "tamil", "hindi": "hindi",
             "bollywood": "hindi", "punjabi": "punjabi", "spanish": "spanish", "english": "english",
             "korean": "korean", "kpop": "korean", "k-pop": "korean", "instrumental": "instrumental"}
MOODS = {"sad": ["heartbreak", "melancholic"], "heartbreak": ["heartbreak"], "heartbroken": ["heartbreak"],
         "crying": ["heartbreak", "melancholic"], "breakup": ["heartbreak"], "melancholic": ["melancholic"],
         "chill": ["chill", "dreamy"], "calm": ["chill", "dreamy"], "relaxing": ["chill", "dreamy"],
         "lofi": ["chill", "dreamy"], "romantic": ["romantic"], "love": ["romantic"], "hype": ["hype"],
         "aggressive": ["aggressive"], "angry": ["aggressive"], "dark": ["dark"], "happy": ["feel-good", "euphoric"],
         "feel good": ["feel-good"], "upbeat": ["feel-good", "euphoric"], "fun": ["feel-good"],
         "nostalgic": ["nostalgic"], "throwback": ["nostalgic"], "dreamy": ["dreamy"], "confident": ["confident"],
         "swag": ["confident"], "flex": ["confident"], "deep": ["introspective"], "introspective": ["introspective"],
         "euphoric": ["euphoric"]}
CONTEXTS = {"gym": "workout", "workout": "workout", "lifting": "workout", "running": "workout",
            "party": "party", "club": "party", "pregame": "pregame", "drive": "drive", "driving": "drive",
            "road trip": "drive", "cruise": "drive", "late night": "late-night", "night": "late-night",
            "midnight": "late-night", "2am": "late-night", "3am": "late-night", "sleep": "wind-down",
            "wind down": "wind-down", "study": "focus", "focus": "focus", "sing along": "sing-along",
            "karaoke": "sing-along"}
LOUD = {"gym", "workout", "lifting", "running", "hype", "hard", "aggressive", "angry", "banger", "bangers", "party",
        "club", "pregame", "loud", "rage", "mass"}
SOFT = {"chill", "calm", "relaxing", "sleep", "sad", "slow", "soft", "lofi", "crying", "wind down", "ballad",
        "ballads", "melody", "melodies", "late night", "2am", "3am", "midnight"}
NEGATIONS = ("no ", "not ", "without ", "except ", "minus ", "skip ")


def _has(text, phrase):
    return re.search(rf"(?<![\w&]){re.escape(phrase)}(?![\w&])", text) is not None


def artist_index(lib):
    """Spoken artist name -> credited name: full names, nicknames, and unambiguous first words."""
    full = {}
    for t in lib["tracks"]:
        for n in credit_names(t):
            full.setdefault(norm(n), n)
    index = dict(full)
    first = Counter(k.split()[0] for k in full if len(k.split()) > 1)
    for k, n in full.items():
        w = k.split()
        if len(w) > 1 and first[w[0]] == 1 and len(w[0]) >= 4:
            index.setdefault(w[0], n)
    for real, nicks in names.NICKNAMES.items():
        for nick in nicks:
            index.setdefault(norm(nick).replace("the ", ""), real)
    for short in ("dsp", "arr"):  # common abbreviations
        index.pop(short, None)
    index.update({"dsp": "Devi Sri Prasad", "arr": "A.R. Rahman"})
    return {k: v for k, v in index.items() if len(k) >= 3}


def parse(text, lib):
    """Description -> constraints. Each found constraint is also returned as a readable chip."""
    raw = " " + text.lower().replace("’", "'") + " "
    # Split off negated parts ("no slow songs", "without Drake").
    neg_parts = re.findall(r"\b(?:no|not|without|except|minus|skip)\s+([^,.;]+?)(?=,|\.|;| and | but |$)", raw)
    neg = " " + " ".join(neg_parts) + " "
    pos = raw
    for part in neg_parts:
        pos = pos.replace(part, " ")
    c = {"styles": set(), "families": set(), "languages": set(), "moods": set(), "contexts": set(),
         "energy": [1, 5], "years": None, "artists": [], "exclude_artists": [], "exclude_styles": set(),
         "exclude_families": set(), "exclude_languages": set(), "source": None, "size": 30, "chips": [],
         "sound_artists": set()}

    for phrase, (styles, fams) in sorted(SOUNDS.items(), key=lambda kv: -len(kv[0])):
        if _has(pos, phrase):
            c["styles"] |= set(styles)
            c["families"] |= set(fams)
            c["sound_artists"] |= {norm(a) for a in SOUND_ARTISTS.get(phrase, [])}
            pos = pos.replace(phrase, " ")
        if _has(neg, phrase):
            c["exclude_styles"] |= set(styles)
            c["exclude_families"] |= set(fams)
    # A specific sound beats the general word around it: "boyband pop" means boyband, not all pop.
    c["families"] -= {labels.FAMILY.get(st) for st in c["styles"]}
    for word, lang in LANGUAGES.items():
        if _has(pos, word):
            c["languages"].add(lang)
        if _has(neg, word):
            c["exclude_languages"].add(lang)
    for word, moods in MOODS.items():
        if _has(pos, word):
            c["moods"] |= set(moods)
    for word, ctx in CONTEXTS.items():
        if _has(pos, word):
            c["contexts"].add(ctx)

    loud = any(_has(pos, w) for w in LOUD)
    soft = any(_has(pos, w) for w in SOFT)
    if loud and not soft:
        c["energy"] = [4, 5]
    elif soft and not loud:
        c["energy"] = [1, 3]
    if _has(neg, "slow") or _has(neg, "sad") or _has(neg, "soft") or _has(neg, "ballads"):
        c["energy"][0] = max(c["energy"][0], 3)
    if _has(neg, "loud") or _has(neg, "hype") or _has(neg, "bangers") or _has(neg, "fast"):
        c["energy"][1] = min(c["energy"][1], 3)

    m = re.search(r"\b(?:(19|20)?(\d)0)'?s\b", raw)
    if m:
        decade = int((m.group(1) or ("19" if m.group(2) >= "5" else "20")) + m.group(2) + "0")
        c["years"] = [decade, decade + 9]
    elif _has(pos, "old") or _has(pos, "classics"):
        c["years"] = [0, 2012]
    elif _has(pos, "new") or _has(pos, "recent") or _has(pos, "latest"):
        c["years"] = [2022, 9999]

    m = re.search(r"\b(\d{1,3})\s*(?:songs|tracks)\b", raw)
    if m:
        c["size"] = max(5, min(100, int(m.group(1))))

    m = re.search(r"\bfrom (?:my )?([^,.;]+)", raw)
    if m:
        want = norm(m.group(1))
        for p in lib["playlists"]:
            if norm(p["name"]) and (norm(p["name"]) == want or want.startswith(norm(p["name"]) + " ")):
                c["source"] = p
                break

    # Artist names are matched on normalized text ("AC/DC" -> "acdc", "Måneskin" -> "maneskin").
    index = artist_index(lib)
    pos_n, neg_n = f" {norm(pos)} ", f" {norm(neg)} "
    for spoken, real in sorted(index.items(), key=lambda kv: -len(kv[0])):
        if _has(pos_n, spoken) and real not in c["artists"]:
            c["artists"].append(real)
            pos_n = pos_n.replace(spoken, " ")
        if _has(neg_n, spoken) and real not in c["exclude_artists"]:
            c["exclude_artists"].append(real)

    chips = []
    chips += sorted(c["languages"]) + sorted(c["families"]) + sorted(c["styles"])
    chips += sorted(c["moods"]) + sorted(c["contexts"])
    if c["energy"] != [1, 5]:
        chips.append(f"energy {c['energy'][0]}-{c['energy'][1]}")
    if c["years"]:
        chips.append(f"{c['years'][0]}-{c['years'][1]}" if c["years"][1] < 9999 else f"{c['years'][0]}+")
    chips += [f"like {a}" for a in c["artists"]]
    chips += [f"not {x}" for x in c["exclude_artists"] + sorted(c["exclude_styles"] | c["exclude_families"]
                                                               | c["exclude_languages"])]
    if c["source"]:
        chips.append(f"from {c['source']['name'].strip()}")
    c["chips"] = chips
    return c


def build(lib, text, eff=None):
    """Rank songs for a description. Returns {"constraints", "picks": [(track, why)], "family"}."""
    c = parse(text, lib)
    eff = eff or labels.merged()
    tracks = [t for t in lib["tracks"] if t["persistentID"] in eff and not t["disliked"]]
    if c["source"]:
        allowed = set(c["source"]["trackIDs"])
        tracks = [t for t in tracks if t["persistentID"] in allowed]

    def credits(t):
        return {norm(n) for n in credit_names(t)}

    excl_artists = {norm(a) for a in c["exclude_artists"]}
    seed_artists = {norm(a) for a in c["artists"]}

    def passes(t):
        lab = eff[t["persistentID"]]
        fam = labels.FAMILY.get(lab["style"], "other")
        if c["languages"] and lab["language"] not in c["languages"]:
            return False
        if lab["language"] in c["exclude_languages"] or lab["style"] in c["exclude_styles"] \
                or fam in c["exclude_families"] or credits(t) & excl_artists:
            return False
        if (c["styles"] or c["families"]) and lab["style"] not in c["styles"] and fam not in c["families"] \
                and not credits(t) & c["sound_artists"]:
            return False
        if not (c["energy"][0] <= lab["energy"] <= c["energy"][1]):
            return False
        if c["years"] and not (c["years"][0] <= (t["year"] or 0) <= c["years"][1]):
            return False
        return True

    pool = [t for t in tracks if passes(t)]
    model = None
    seeds = [t["persistentID"] for t in pool if credits(t) & seed_artists]
    if seeds:
        model = similarity.SimilarityModel(lib, eff, similarity.sound_vectors(lib))

    def score(t):
        lab = eff[t["persistentID"]]
        s, why = 0.0, []
        if c["moods"] and lab["mood"] in c["moods"]:
            s += 1.0
            why.append(lab["mood"])
        if c["contexts"] and set(lab["contexts"]) & c["contexts"]:
            s += 0.8
            why.append(", ".join(sorted(set(lab["contexts"]) & c["contexts"])))
        if c["styles"] and lab["style"] in c["styles"]:
            s += 0.5
        if credits(t) & seed_artists:
            s += 1.2
            why.append("by an artist you named")
        elif model and t["persistentID"] in model.index:
            sim = model.playlist_score(t["persistentID"], seeds, 5)
            s += sim
            if sim > 0.6:
                why.append("sounds like your picks")
        lo, hi = c["energy"]
        s += 0.3 * (1 - abs(lab["energy"] - (lo + hi) / 2) / 4)
        s += 0.15 * min(1, (t["playedCount"] or 0) / 20)  # songs you actually play
        return s, why

    scored = sorted(((score(t), t) for t in pool), key=lambda x: -x[0][0])

    # One sound per playlist: when no sound was named, keep the family the best matches share.
    family = None
    if not (c["styles"] or c["families"]) and scored:
        top = Counter(labels.FAMILY.get(eff[t["persistentID"]]["style"], "other") for _, t in scored[:30])
        family = top.most_common(1)[0][0]
        keep = {family} | ({"rnb"} if family == "rap" else set())  # rap and R&B sit together fine
        scored = [x for x in scored if labels.FAMILY.get(eff[x[1]["persistentID"]]["style"], "other") in keep]

    picks, seen = [], set()
    for (s, why), t in scored:
        k = song_key(t)
        if k in seen:
            continue
        seen.add(k)
        lab = eff[t["persistentID"]]
        picks.append((t, ", ".join(why) or f"{lab['style']}, energy {lab['energy']}"))
        if len(picks) >= c["size"]:
            break
    return {"constraints": c, "picks": picks, "family": family}


def name_options(text, picks, eff):
    """The user's own words first, then generated names for what's inside."""
    own = " ".join(w.capitalize() if w.islower() else w for w in text.strip().split())
    own = re.sub(r"[,.;]+$", "", own)
    ids = [t["persistentID"] for t, _ in picks]
    tracks = {t["persistentID"]: t for t, _ in picks}
    prof = names.profile(ids, tracks, eff)
    bucket = Counter(reorg.bucket(eff[i]) for i in ids).most_common(1)[0][0] if ids else "Party"
    opts = names.options(prof, bucket, seed=text)
    return ([own] if 0 < len(own) <= 40 else []) + opts, prof, bucket


def plan_ops(lib, text, picks, eff=None):
    eff = eff or labels.merged()
    opts, prof, bucket = name_options(text, picks, eff)
    name = opts[0] if opts else "New Playlist"
    return [
        {"op": "create_playlist", "name": name, "ref": "describe", "nameOptions": opts, "nameProfile": prof,
         "bucket": bucket, "reason": f"from your description: “{text.strip()}”"},
        {"op": "add_tracks", "playlist": {"ref": "describe", "name": name},
         "tracks": [{**plans.track_ref(t), "note": why} for t, why in picks],
         "reason": f"{len(picks)} songs matching “{text.strip()}”"},
    ]
