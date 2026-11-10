# Mahjong Soul rank UI assets

Rank emblems and the UI sprites come from the official Mahjong Soul web client
resource manifest recorded in `ORIGIN.json`. The additional UI assets use
`res/atlas/myres.atlas` (`v0.11.101.w`) and its original PNG sheets. Numeric glyphs
come from the client's `youyuan` and `hanyi` bitmap fonts in
`res/atlas/bitmapfont.atlas` (`v0.11.243.w`). Only the numeric/punctuation/unit
glyphs used by this bot are retained.

`ORIGIN.json` records the source URL, atlas frame or glyph identifier, dimensions
and SHA-256 of every PNG. `ui_*` files are direct atlas crops, and `font_*.json`
retains original glyph advances/offsets. Rendering resizes these local assets;
there are no game-resource requests during user queries. Artwork and font assets
belong to their original rights holders and are separate from the code license.

The shared `ms_level.py` applies the upstream `level.ts` rank boundaries and score
conversion. Minor ranks 1–3 use three original bright/dark flower slots; celestial
minor ranks 1–20 use the original blue Lv/digit sprites, never stars. Modern
celestial scores and deltas use 100 internal units per soul point, displayed with
one decimal. Legacy 10601/20601 conversion is kept separate and its rounded
result delta is shown only when it can be established exactly.

PT and soul-point result totals are never added to one another. A historical
record with no usable rank retains a neutral raw “积分” unit. The unit of a game
result comes from its recorded pre-game rank, independently of a later profile
snapshot or promotion.

Reference algorithm:
https://github.com/SAPikachu/amae-koromo/blob/master/src/data/types/level.ts
