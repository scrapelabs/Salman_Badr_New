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

**Dedup:** key on the *physical* match — tournament + date + (order-independent,
so partner/serving order can't matter) player names + normalized score —
excluding BOTH `draw_name` and the site game id (`_dedup_key` in
`brazil_results.py`). Do NOT key on the game id: it is **sometimes blank**, and a
blank/mismatched id let the same-match-under-a-*wrong*-draw duplicate leak
through. The physical key collapses every re-serving (category-heading leak +
round-walk) regardless of the id. Verified on a live 10,973-row run: unique keys
== row count (no over-collapse; a genuine rematch of the same two players in the
same event/day with an identical score doesn't happen). The game id is still
parsed but is neither a dedup input nor an output column (user wants "Match ID"
kept out of the CSV, and explicitly did NOT want a DB store for it either).

**Why:** user reported "all data is wrong" — rows duplicated across ages/genders
with mismatched winners. Root cause was the missing `getGruposJogos` step (this
affects the original production spider too, which reused the page's initial
parametro), compounded by an over-strict port dedup key that had added
`draw_name`/`round`/`date`/`tournament_url`. A later fix keyed on the game id +
players + score, but the game id is intermittently blank, so wrong-draw dups
still slipped through whenever it was missing — hence the move to the id-free
physical-identity key above.
