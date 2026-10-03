# Apple Music Organizer

Local-first organizer for the Music app on macOS. It reads your library through
Music's own scripting interface, adds metadata, proposes changes as reviewable
plans, and only touches your library when you approve a plan.

> Status: experimental. The read-only parts (scan, report, metadata, discovery) are
> stable; the playlist-fit and split models are still being tuned and evaluated.

## Setup

- macOS with the Music app; Python 3.9+ with NumPy and SciPy (the Xcode Command Line
  Tools Python already has both). No other dependencies, no accounts or API keys.
- `python3 organizer.py scan` reads your library; macOS asks once to let the
  terminal control Music.
- Mood/energy/style labels come from an AI pass (see below). Without them, the
  label-based features (split, playlist fit) have nothing to work with; you can add
  your own with `label`.

## In the Music app

Run `python3 organizer.py install-scripts` once. The actions appear in Music's
Scripts menu (the scroll icon). If that menu doesn't show, turn on Script Editor →
Settings → General → "Show Script menu in menu bar"; the actions are then in that
menu whenever Music is in front.

| Action | What it does |
|---|---|
| Where Do These Belong | Select songs → up to 3 fitting playlists each → tick, confirm |
| Discover From Selection | Select songs → lesser-known songs charting now in that vibe → opens them in Music |
| Artist Playlist… | Type an artist → their best songs for your taste → playlist (songs you don't have open one by one for +) |
| Move Song… | Select songs in a playlist made by a split → pick the group they belong in (or none); remembered for the next split |
| Review & Apply Changes | Pick a plan → tick changes → confirm → applied, verified, undo plan saved |
| Refresh Scan & Metadata | Rescan, fetch metadata for new songs, fill artist playlists with songs you've added |

The first change asks macOS for permission to control Music; allow it.

## Command line

```
python3 organizer.py scan                 # read the library (read-only)
python3 organizer.py report               # playlists, duplicates, overlap, genres
python3 organizer.py enrich               # Apple catalog match, preview audio, MusicBrainz
python3 organizer.py enrich lastfm        # listener tags; needs LASTFM_API_KEY=... in .env
python3 organizer.py split "Calling"      # plan vibe playlists from a big mixed one
python3 organizer.py artist "Travis Scott"
python3 organizer.py suggest              # artist-playlist gaps, homes for unplaylisted songs
python3 organizer.py discover [--fresh]   # charting now (or new releases) near your taste
python3 organizer.py plans | review PLAN | approve PLAN 1,3-5 | drop PLAN OP 2,7 | apply PLAN
python3 organizer.py label TRACK_ID --energy 2 --mood chill   # your overrides always win
python3 organizer.py check-labels         # compare energy labels with measured audio
python3 organizer.py evaluate             # benchmark playlist fit on your own playlists
python3 -m unittest discover tests        # tests against a fake Music app
```

## How "Where do these belong?" decides

Each song is compared with the songs in every playlist (language, style, mood,
energy, measured sound, shared artists); a playlist scores by its 5 closest songs.
Artist playlists only take that artist; soundtrack-style playlists only take songs
from matching albums. On a leave-one-out test over the author's library (hide a song
from its playlist, see where the model puts it) the right playlist came first 86.8%
of the time, against 70.7% for comparing with a playlist's average and 40.6% for
always picking the biggest playlist. Run `evaluate` to measure it on yours.

## How splits decide, and how far to trust them

Splits put each song in a vibe group (Gym, Party, Cruise, Feels, Late Night) from
its energy, mood and context labels; songs that don't fit the playlist's main sound
(folk in a pop playlist) get their own group or stay put. `evaluate` checks the
result against measured preview audio, which the split never looks at: so far groups
sound only slightly more alike than random groups from the same playlist (about
x1.1 overall, some groups below x1.0). Data-driven clustering and similarity-based
refinement were tried and did no better on that independent check, so the simpler,
explainable rules stay. A timbre fingerprint (MFCCs) was also tried: it neither
improved playlist fit (85.5–86.6% vs 86.8%) nor showed the split groups sounding
alike, so it is measured and stored but not used. Last.fm listener tags give an outside check on
energy: where listeners clearly tag a song calm or energetic (39 songs in the
author's library) they agree with the energy labels (correlation 0.79), and every
planned group leans the expected way (Party/Gym tagged energetic, Late Night calm).
Treat split plans as a starting point to review, not a
verdict; `label` overrides and re-running `split` are the main way to improve them.

## Safety

- Every change is a plan op you approve; `apply` (or the dialog's Apply) asks again.
- After each op the playlist is re-read from Music; an op counts as done only if the
  change is really there. A failure stops the batch.
- Smart, special and library playlists are never edited. Only playlists this tool
  created can be deleted. Removing a song from a playlist never removes it from the library.
- Every op is logged to `data/audit.jsonl` and an undo plan is written.
- `data/` (your library, labels, plans, caches) is git-ignored.

## Where the data comes from

| Source | What | Kind |
|---|---|---|
| Music app | titles, artists, albums, genres, plays, favourites, playlists | known |
| Apple catalog (iTunes Search) | catalog IDs, explicitness, 30 s preview | known |
| Preview analysis | loudness, brightness, bass, pulse, pace, tempo estimate | measured (estimate) |
| MusicBrainz | artist country, type, crowd tags | known (crowd) |
| Claude | mood, energy, context, language, style + confidence | AI-inferred |
| You (`label`) | overrides | manual |
| Deezer, Apple charts, ListenBrainz | related artists, fan counts, charts | discovery only |

## License

MIT, see [LICENSE](LICENSE).
