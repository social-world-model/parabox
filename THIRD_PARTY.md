# Third-party provenance and licenses

Original Parabox code and documentation use the root [MIT license](LICENSE) unless marked otherwise. Bundled Apache-2.0 material retains that license and its notices. The package expression `MIT AND Apache-2.0` describes this combination; it is not a choice of license for every file.

## Distributed material

| Material | Location and provenance | Retained terms or review requirement |
| -- | -- | -- |
| Formal Conjectures statements | `parabox/tasks/lean/image/Problems/`; adapted from revision `1b567c2f96e2aa86328094c4b9ca57f72a0d886e` | Apache-2.0; [license](licenses/Formal-Conjectures-Apache-2.0.txt) and [statement attribution](licenses/Formal-Conjectures-Attribution.txt) |
| Mathlib/Lean declaration and documentation index | `data/corpus/mathlib-v4.33.1.json`; v4.33.1 sources | Apache-2.0; [Mathlib license](licenses/Mathlib-Apache-2.0.txt), [Lean license](licenses/Lean-Apache-2.0.txt) and [prose-source attribution](licenses/Corpus-Attribution.txt) |
| Model-generated text | Runtime seeds, packaged transmission and wiki-founder assets, note constants, historical documents and `report/decks/spread-of-misalignment/evidence/relay-example.json` | Provider-specific access terms; review retained excerpts and any proposed transcript export separately from the code license |
| Bibliography and reported incidents | `report/decks/spread-of-misalignment/ref.bib` and cited publications | Attribute claims to their sources; inclusion does not confer rights to reproduce papers or imply endorsement |
| Psy High browser UI screenshot | `report/game-interface/e04-psy-high/paywall.png`; captured from [Choice of Games](https://www.choiceofgames.com/psy-high/) on 2026-10-30 | Publisher content retains its rights and is not relicensed under the root MIT license. Included as evidence of the public paid-content boundary; no game source or story chapters are included. |

Raw model runs remain local, and internal reviewer outputs are excluded. Generated declaration signatures in the corpus are pretty-printed; prose-bearing entries map to pinned upstream sources with retained copyright and author headers.

## Build-time dependencies

Lean/Mathlib runtimes, Python, pytest and operating-system layers are downloaded during image builds rather than included as a vendored dependency tree. The Python task image specifies pytest 8.3.4. Preserve dependency revisions, image digests and applicable notices when distributing images; the source package's attribution inventory is not a transitive image-license inventory.

## Recorded verification

License evidence was inspected on 2026-10-12. All twelve imported Formal Conjectures statement headers were checked at the pinned revision; mappings, copyright notices and adaptation notices are retained in the attribution file. The pinned root `NOTICE` URL returned 404. Neither the pinned Mathlib nor Lean tree contained a root `NOTICE`.

The retained license-copy SHA-256 values are:

| Copy | SHA-256 |
| -- | -- |
| Formal Conjectures | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| Mathlib | `b40930bbcf80744c86c46a12bc9da056641d722716c378f5659b9e555ef833e1` |
| Lean | `8b28515ffffc5c0fe2807d8ae3735b00b324d9b7ce807dd63ff6ac8922fbce7e` |

This record documents known provenance, not a grant of third-party rights or a legal certification. Review additional imports and distributions on their own terms.
