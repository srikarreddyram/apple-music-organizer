"""Dialog flows behind the Music Scripts menu actions.

The menu scripts only read the current selection and start `organizer.py ui ...`
in the background. Everything else happens here, in a separate process, so the
Music app never waits on itself. Dialogs are standard macOS ones via osascript.

Nothing changes in Music unless you tick items and confirm in a dialog; every
change goes through the same plan, verification, audit log and undo as the CLI.
"""
import json
import math
import subprocess
import sys
from collections import Counter
from pathlib import Path

import artist
import discover
import labels
import metadata
import plans
import suggest

DATA = Path(__file__).parent / "data"
INVENTORY = DATA / "inventory.json"
TITLE = "Music Organizer"


# --- Dialogs -------------------------------------------------------------------

def osa(script, *args):
    proc = subprocess.run(["osascript", "-e", script, *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip()


def choose(items, prompt, multiple=False, ok="OK", preselect_all=False):
    """Pick from a list; returns chosen indexes (empty if cancelled)."""
    if not items:
        return []
    labelled = [f"{i + 1}. {s}" for i, s in enumerate(items)]
    script = f'''
on run argv
    activate
    set picks to choose from list (items 3 thru -1 of argv) with title "{TITLE}" with prompt (item 1 of argv) ¬
        OK button name (item 2 of argv) {"with multiple selections allowed" if multiple else ""} ¬
        {"default items (items 3 thru -1 of argv)" if preselect_all else ""}
    if picks is false then return ""
    set AppleScript's text item delimiters to linefeed
    return picks as text
end run'''
    code, out = osa(script, prompt, ok, *labelled)
    if code or not out:
        return []
    return [int(line.split(".", 1)[0]) - 1 for line in out.splitlines()]


def alert(message, buttons=("OK",), default=None):
    btns = "{" + ", ".join(json.dumps(b) for b in buttons) + "}"
    script = f'''
on run argv
    activate
    set r to display dialog (item 1 of argv) with title "{TITLE}" buttons {btns} ¬
        default button {json.dumps(default or buttons[-1])}
    return button returned of r
end run'''
    code, out = osa(script, message)
    return out if code == 0 else None


def notify(message):
    osa(f'on run argv\ndisplay notification (item 1 of argv) with title "{TITLE}"\nend run', message)


def load_lib():
    return json.loads(INVENTORY.read_text())


def confirm_and_apply(plan):
    """Ask once more, then apply the plan's approved ops and report what was verified."""
    todo = [op for op in plan["ops"] if op["approved"] and op["status"] == "pending"]
    tracks = sum(len(op.get("tracks") or []) for op in todo)
    creates = sum(op["op"] == "create_playlist" for op in todo)
    summary = f"{len(todo)} changes" + (f", {creates} new playlists" if creates else "") + \
              (f", {tracks} song additions/removals" if tracks else "")
    if alert(f"Apply to your Music library?\n\n{summary}\n\nPlan: {plan['id']}",
             ("Cancel", "Apply"), "Apply") != "Apply":
        notify("Nothing changed.")
        return
    done, undo = plans.apply_plan(plan)
    ok = sum(op["status"] == "applied" for op in done)
    failed = [op for op in done if op["status"] == "failed"]
    msg = f"{ok} of {len(done)} changes applied and verified."
    if failed:
        msg += f"\n\nStopped at op {failed[0]['n']}: {failed[0]['result']['detail']}"
    if undo:
        msg += f"\n\nAn undo plan was saved ({undo['id']}); it's listed under Review & Apply."
    alert(msg)
    rescan()


def rescan():
    from organizer import scan  # organizer imports this module, so import late
    return scan()


# --- Review & apply ------------------------------------------------------------

def op_line(op):
    count = f" ({len(op['tracks'])} songs)" if op.get("tracks") else ""
    verb = {"create_playlist": "Create", "add_tracks": "Add to", "remove_tracks": "Remove from",
            "delete_playlist": "Delete"}[op["op"]]
    return f"{verb} {plans.target_label(op)}{count}"


def review():
    pending = [p for p in plans.all_plans() if any(op["status"] == "pending" for op in p["ops"])]
    if not pending:
        alert("No pending changes. Use “Where do these belong?” or ask Claude for suggestions.")
        return
    pending.sort(key=lambda p: p["createdAt"], reverse=True)
    pick = choose([f"{p['title']}  ({sum(o['status'] == 'pending' for o in p['ops'])} pending)  [{p['id']}]"
                   for p in pending], "Which plan do you want to review?", ok="Review")
    if not pick:
        return
    plan = pending[pick[0]]
    ops = [op for op in plan["ops"] if op["status"] == "pending"]
    picks = choose([op_line(op) for op in ops],
                   f"{plan['title']}\nTick the changes to apply. Track lists: organizer.py review {plan['id']}",
                   multiple=True, ok="Continue")
    if not picks:
        return
    chosen = {ops[i]["n"] for i in picks}
    # An add to a new playlist needs its create op too.
    for i in picks:
        ref = (ops[i].get("playlist") or {}).get("ref")
        if ref:
            chosen |= {o["n"] for o in ops if o["op"] == "create_playlist" and o.get("ref") == ref}
    for op in plan["ops"]:
        if op["status"] == "pending":
            op["approved"] = op["n"] in chosen
    plans.save_plan(plan)
    confirm_and_apply(plan)


# --- Where do these belong? ----------------------------------------------------

ALBUM_STOPWORDS = {"original", "motion", "picture", "soundtrack", "deluxe", "edition", "version", "the",
                   "from", "music", "album", "single", "presents", "series", "season", "expanded", "remastered"}


def album_words(album):
    return {w for w in suggest.norm(album).split() if len(w) >= 4 and w not in ALBUM_STOPWORDS}


def theme_word(ids, tracks):
    """A word shared by most album names (e.g. "spider"), marking a soundtrack/theme playlist."""
    words = Counter(w for i in ids if i in tracks for w in album_words(tracks[i]["album"]))
    if words:
        word, n = words.most_common(1)[0]
        if n / max(len(ids), 1) >= 0.6:
            return word
    return None


def sound_vectors(lib):
    """Standardised audio vectors per track id (empty until `enrich audio` has run)."""
    import numpy as np
    from artist import vector
    audio = metadata.load("audio")
    vecs = {t["persistentID"]: vector(audio.get(t["persistentID"], {})) for t in lib["tracks"]}
    vecs = {k: v for k, v in vecs.items() if v}
    if len(vecs) < 50:
        return {}
    x = np.array(list(vecs.values()), dtype=float)
    mean, std = x.mean(axis=0), x.std(axis=0) + 1e-9
    return {k: (np.array(v) - mean) / std for k, v in vecs.items()}


def profiles(lib, eff, z=None):
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    z = z or {}
    display = {suggest.norm(n) for t in tracks.values() for n in suggest.credit_names(t)}
    out = {}
    for p in filter(suggest.editable, lib["playlists"]):
        ids = list(dict.fromkeys(p["trackIDs"]))
        artist, _ = suggest.playlist_artist(p, tracks, display)
        labs = [eff[i] for i in ids if i in eff]
        if len(labs) < 3 and not artist:
            continue
        n = max(len(labs), 1)
        out[p["persistentID"]] = {
            "playlist": p, "ids": set(ids), "artist": artist, "theme": theme_word(ids, tracks),
            "mood": Counter(l["mood"] for l in labs), "style": Counter(l["style"] for l in labs),
            "language": Counter(l["language"] for l in labs),
            "genre": Counter(tracks[i]["genre"] for i in ids if i in tracks),
            "energy": sum(l["energy"] for l in labs) / n, "n": n, "size": len(ids),
            "sound": sum(z[i] for i in ids if i in z) / sum(1 for i in ids if i in z)
            if sum(1 for i in ids if i in z) >= 3 else None,
        }
    return out


def fit(track, label, prof, z=None):
    """0..1 fit of a track for a playlist, with a short reason. `z`: the track's sound vector."""
    if prof["artist"]:  # artist playlists only take that artist's songs
        if prof["artist"] in suggest.credited(track):
            return 1.0, f"artist playlist for {prof['artist'].title()}"
        return 0.0, "different artist"
    if prof["theme"] and prof["theme"] not in album_words(track["album"]):
        return 0.0, f"themed playlist ({prof['theme']})"
    if not label:
        share = prof["genre"].get(track["genre"], 0) / max(prof["size"], 1)
        return 0.6 * share, f"{share:.0%} {track['genre']}"
    n = prof["n"]
    lang = prof["language"].get(label["language"], 0) / n
    style = prof["style"].get(label["style"], 0) / n
    mood = prof["mood"].get(label["mood"], 0) / n
    energy = 1 - abs(label["energy"] - prof["energy"]) / 4
    sound = None
    if z is not None and prof.get("sound") is not None:
        d2 = float(((z - prof["sound"]) ** 2).mean())  # ~1 for an average distance
        sound = math.exp(-d2)
    # Measured sound, when there is one, shares the weight with the energy label.
    feel = energy if sound is None else 0.4 * energy + 0.6 * sound
    main_lang, main_n = prof["language"].most_common(1)[0]
    if main_lang != "english" and main_n / n >= 0.7:
        # A language playlist (Telugu Tunes, South...): language decides, vibe fine-tunes.
        score = lang * (0.6 + 0.4 * (0.5 * style + 0.2 * mood + 0.3 * feel))
    else:
        # Mixed playlists: style relative to the playlist's main style gates, mood and sound refine.
        style_rel = style / (prof["style"].most_common(1)[0][1] / n)
        mood_rel = mood / (prof["mood"].most_common(1)[0][1] / n)
        score = lang * style_rel ** 0.5 * (0.45 + 0.2 * mood_rel + 0.35 * feel)
    why = f"{lang:.0%} {label['language']}, {style:.0%} {label['style']}"
    why += f", sounds {sound:.0%} alike" if sound is not None else f", energy ~{prof['energy']:.1f}"
    return score, why


def belong(ids):
    lib = load_lib()
    tracks = {t["persistentID"]: t for t in lib["tracks"]}
    eff = labels.merged()
    unknown = [i for i in ids if i not in tracks]
    if unknown:
        alert(f"{len(unknown)} of the selected songs aren't in the last scan. Run “Refresh scan & metadata” "
              "first, then try again.")
        ids = [i for i in ids if i in tracks]
    if not ids:
        return
    z = sound_vectors(lib)
    profs = profiles(lib, eff, z)
    rows = []
    for tid in ids:
        t = tracks[tid]
        scored = sorted(((*fit(t, eff.get(tid), pr, z.get(tid)), pr) for pr in profs.values()
                         if tid not in pr["ids"]),
                        key=lambda x: -x[0])
        for score, why, pr in scored[:3]:
            if score >= 0.35:
                rows.append((tid, pr["playlist"], score, why))
    if not rows:
        alert("No playlist is a clear fit for these songs.")
        return
    items = [f"{tracks[tid]['name']} – {tracks[tid]['artist']}  →  {p['name'].strip()}   ({why})"
             for tid, p, score, why in rows]
    picks = choose(items, "Best-fitting playlists (up to 3 per song). Tick the ones to add to:",
                   multiple=True, ok="Add")
    if not picks:
        return
    by_playlist = {}
    for i in picks:
        tid, p, score, why = rows[i]
        by_playlist.setdefault(p["persistentID"], (p, []))[1].append(tid)
    ops = [{"op": "add_tracks", "playlist": {"id": pid, "name": p["name"]},
            "tracks": [plans.track_ref(tracks[t]) for t in tids], "reason": "chosen in “Where do these belong?”"}
           for pid, (p, tids) in by_playlist.items()]
    plan = plans.new_plan("Where do these belong", ops, lib)
    for op in plan["ops"]:
        op["approved"] = True
    plans.save_plan(plan)
    confirm_and_apply(plan)


# --- Discover from selection ------------------------------------------------------

def discover_from(ids):
    lib = load_lib()
    chosen = [t for t in lib["tracks"] if t["persistentID"] in set(ids)]
    if not chosen:
        alert("Select some songs in Music first (and run “Refresh” if they're new).")
        return
    notify(f"Looking for songs like {len(chosen)} selected… this can take a minute.")
    result = discover.trending({**lib, "tracks": chosen}, seeds=min(10, len(chosen) * 2), limit=25,
                               owned=lib["tracks"], progress=lambda m: None)
    discover.save(result)
    picks = result["picks"]
    if not picks:
        alert("Nothing charting right now matched these songs. Try selecting a few more.")
        return
    items = [f"{p['name']} – {p['artist']}  ({'; '.join((p.get('charts') or [])[:1]) or 'ListenBrainz'})"
             for p in picks]
    sel = choose(items, "Charting now, close to your selection, from lesser-known artists.\n"
                        "Tick songs to open in Music (add them there with +):", multiple=True, ok="Open")
    for i in sel:
        if picks[i].get("url"):
            subprocess.run(["open", picks[i]["url"]])


# --- Artist playlist ---------------------------------------------------------------

def ask(prompt, default=""):
    script = f'''
on run argv
    activate
    set r to display dialog (item 1 of argv) with title "{TITLE}" default answer (item 2 of argv) ¬
        buttons {{"Cancel", "Go"}} default button "Go"
    return text returned of r
end run'''
    code, out = osa(script, prompt, default)
    return out.strip() if code == 0 else None


def artist_playlist():
    name = ask("Which artist? I'll pick their best songs for your taste.")
    if not name:
        return
    lib = load_lib()
    notify(f"Listening to {name}'s catalog… this takes about a minute.")
    result = artist.best_songs(lib, name, progress=lambda m: None)
    picks = result["picks"]
    items = [f"{'✓' if p['libraryID'] else '+'} {p['name']}" for p in picks]
    sel = choose(items, f"{result['artist']}: best for you (✓ already yours, + needs adding in Music).\n"
                        "Untick anything you don't want:", multiple=True, ok="Create", preselect_all=True)
    if not sel:
        return
    result["picks"] = [picks[i] for i in sel]
    ops, waiting = artist.plan_ops(lib, result)
    plan = plans.new_plan(f"Artist playlist: {result['artist']}", ops, lib)
    plan["waiting"] = waiting
    for op in plan["ops"]:
        op["approved"] = True
    plans.save_plan(plan)
    confirm_and_apply(plan)
    if not waiting or plan["ops"][0]["status"] != "applied":
        return
    if alert(f"{len(waiting)} songs aren't in your library yet. I'll open them one at a time in Music; "
             "click + (Add to Library) on each, then come back here.", ("Skip", "Start"), "Start") != "Start":
        return
    for i, w in enumerate(waiting, 1):
        subprocess.run(["open", w["url"]])
        if alert(f"{i}/{len(waiting)}: {w['name']}\n\nAdd it with + in Music, then continue.",
                 ("Stop", "Next"), "Next") != "Next":
            break
    notify("Checking your library for the added songs…")
    lib = rescan()
    ops, still = artist.fill_ops(lib, plan)
    plan["waiting"] = still
    plans.save_plan(plan)
    if not ops:
        alert("None of them showed up in the library yet. Run “Refresh” later; "
              "the playlist will be filled from Review & Apply.")
        return
    fill = plans.new_plan(f"Fill {plan['title']}", ops, lib)
    for op in fill["ops"]:
        op["approved"] = True
    plans.save_plan(fill)
    confirm_and_apply(fill)


# --- Refresh -----------------------------------------------------------------------

def refresh():
    notify("Rescanning your library…")
    old = {t["persistentID"] for t in load_lib()["tracks"]} if INVENTORY.exists() else set()
    lib = rescan()
    new = [t for t in lib["tracks"] if t["persistentID"] not in old]
    notify(f"{len(lib['tracks'])} tracks scanned, {len(new)} new. Fetching metadata in the background…")
    metadata.enrich_apple(lib, progress=lambda m: None)
    metadata.enrich_audio(lib, progress=lambda m: None)
    metadata.enrich_musicbrainz(lib, progress=lambda m: None)
    for plan in plans.all_plans():  # artist playlists waiting on songs you've now added
        ops, still = artist.fill_ops(lib, plan)
        if ops:
            fill = plans.new_plan(f"Fill {plan['title']}", ops, lib)
            plans.save_plan(fill)
            plan["waiting"] = still
            plans.save_plan(plan)
            notify(f"{len(ops[0]['tracks'])} songs ready for {plan['title']} – see Review & Apply.")
    unlabelled = [t for t in lib["tracks"] if t["persistentID"] not in labels.merged()]
    msg = "Metadata is up to date."
    if unlabelled:
        msg += f" {len(unlabelled)} songs have no mood/energy labels yet – ask Claude to label them."
    notify(msg)


def main(action, ids):
    try:
        {"review": lambda: review(), "belong": lambda: belong(ids),
         "discover": lambda: discover_from(ids), "refresh": lambda: refresh(),
         "artist": lambda: artist_playlist()}[action]()
    except Exception as e:  # noqa: BLE001 - surface any failure to the user instead of dying silently
        alert(f"Something went wrong:\n\n{e}\n\nDetails are in data/ui.log.")
        raise


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
