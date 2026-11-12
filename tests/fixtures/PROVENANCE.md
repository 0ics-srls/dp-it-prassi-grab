# Fixtures of the grab class

| File | Source | Facts asserted by the tests |
|---|---|---|
| `ade-ottobre-2025-risoluzioni.html` | `GET https://www.agenziaentrate.gov.it/portale/ottobre-2025-risoluzioni`, browser User-Agent, 2026-11-12 (122,812 bytes) | 13 cards: 1 header ("Ottobre 2025") + 12 risoluzioni (n. 50, 52..62; there is no 51), every one with a PDF |
| `ade-ottobre-2025-circolari.html` | `.../ottobre-2025-circolari`, same day | 1 card, the header only: an empty month, not an error |
| `ade-luglio-2025-circolari.html` | `.../luglio-2025-circolari`, same day | 3 cards: header "Foglia Luglio 2025 circolari" + circolare n. 11 and n. 10 |
| `ade-ottobre-2025-provvedimenti.html` | `.../ottobre-2025-provvedimenti`, same day | 8 cards: header "Ottobre 2025 Provvedimenti" + 7 provvedimenti; 4 link a PDF directly (`fa-file-pdf-o`), 3 link a detail page (`/portale/-/...`, `fa-folder-o`) |
| `ade-*.reale.html` | the branch-53 fixtures of the legacy parsers (`tests/LexCorpus.Acquisition.AdePortale.Tests/Fixtures`), real pages recorded by the legacy | risoluzioni 2021-05 (10, odd title shapes), circolari 2021-05 (2), risoluzioni 2024-05 (7), provvedimenti 2024-03 (10 cards, 9 entries), one provvedimento detail page (main PDF + Modello + Istruzioni) |

All pages are under 200 KB each and are used untrimmed.
