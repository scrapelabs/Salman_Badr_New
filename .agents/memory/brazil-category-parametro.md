---
name: Brazil (CBT) category parametro switch
description: tenisintegrado torneio_painel_jogo serves the DEFAULT category's games unless you fetch each category's own id_parametro via getGruposJogos first
---

The `torneio_painel_jogo` bracket panel does NOT switch categories by the
`id_categoria` POST field alone. Posting a different `id_categoria` while reusing
the *initial* `id_parametro` returns the **default** category's games every time
— only the echoed heading (`<h4>`, the selected `<option>`) changes. Because the
parser takes one `draw_name` per page and applies it to every game on it, the
same physical match (same site game id) then leaks under *every* category
heading (e.g. a male-named winner surfacing under a "Feminino" draw).

**Fix:** the category `<select>` is a `form-ajax-select` with
`data-ajax-call=".../torneio_painel_jogo/getGruposJogos"`, `data-ajax-target="id_parametro"`.
Per category, `POST getGruposJogos {id_categoria, id_torneio}` -> JSON
`{parametro_id: label}` (drop the `{"":"Selecionar"}` placeholder). Use THAT
category-specific `id_parametro` in the panel POST. Verified live: each category
then returns its own, **zero-overlap** game set.

**Dedup:** key on the site's own game id + player names + score (NOT
`draw_name`/`round`/`date`). The round walk re-serves the same game across round
views, so match-id-based dedup is required to collapse them; a genuine rematch
carries a *different* game id so it is not wrongly merged. The game id is parsed
for the dedup key only — it is deliberately NOT an output column (user asked to
remove the "Match ID" column).

**Why:** user reported "all data is wrong" — rows duplicated across ages/genders
with mismatched winners. Root cause was the missing `getGruposJogos` step (this
affects the original production spider too, which reused the page's initial
parametro), compounded by an over-strict port dedup key that had added
`draw_name`/`round`/`date`/`tournament_url` and so failed to collapse the leaked
duplicates.
