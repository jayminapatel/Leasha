# Work order (One thread): the Photo Tagger — naming people, the Google Photos way, fully local

**Doc version:** 1.2 · **Updated:** 2026-09-19 · **Applies to:** app v0.3.3
**Thread:** One thread (Extract/AI + Storage + new page UI + Search)

**2026-09-16 — all 13 items closed.** A session working this order crashed with
1a/1b/2a/2b/2d/2e/3a/3b and all four §4 tests already built and passing, but the
document itself still unticked and 2c's learning-loop UI missing its user-facing
half: `SqliteStore.suggest_face`/`confirm_suggestion` and `face_clustering.classify`
existed and were proven end to end (`test_the_learning_loop_end_to_end`), but nothing
in `app/ui/` ever surfaced a suggestion to say yes or no to. Added
`SqliteStore.pending_suggestions()` (a suggestion only ever surfaces once its pile
has a name — "Is this None?" has nothing to ask) plus `PendingSuggestion`, and a
small `_SuggestionChip` strip in `photo_tagger_page.py` wired to the existing
storage calls. Six new tests in `test_photo_tagger.py` (the read side of the queue,
including the named/unnamed and limit cases) and six more in the new
`test_photo_tagger_page.py` (the strip's visibility, yes/no behaviour, and tooltips) -
all against the real venv, not assumed. Full surface re-run (`test_photo_tagger.py`
+ `test_photo_tagger_page.py` + `test_photo_tagger_wording.py` +
`test_face_clustering.py`): 59 passed, 0 failed.

**Status:** SHIPPED. Was RELEASED by the owner 2026-08-28. Requires 0510 (image pass
plumbing) and ideally 0511. **The principled line (owner-revised): detection
and grouping are automatic; IDENTITY ONLY EVER COMES FROM THE USER.** The
reference experience is Google Photos' People flow — identical UX, with the
difference AS the product: nothing ever leaves the machine.

## Guardrails (load-bearing, not decoration — face embeddings are
biometric-adjacent)

OFF by default behind one plain-words switch ("Recognise people in photos on
this computer"); everything local; "Forget this person" deletes the name and,
if asked, the face data; names never leave the machine; stated beside the
shared-computer paragraph. No identity ever suggested from anywhere but the
user's own naming.

## 1. Detection and grouping (automatic half)

- [x] **1a** face step in the images pass (only when the switch is on):
  detect faces (`insightface`, ONNX — installed; ~20–50ms/img budget),
  compute per-face embedding, store (file_id, bbox, embedding). Backfill for
  already-indexed images runs as an enrichment-backlog job kind (0511 §2).
- [x] **1b** unsupervised clustering into unnamed piles ("Person 1 — 47
  photos"): incremental cosine-threshold clustering, re-clusterable; nobody
  is identified — there are just piles.

## 2. The Photo Tagger page (the user half)

- [x] **2a** grid of piles, biggest first, sample crops; click → name it.
  Designed for an 8-year-old — this is the one indexing chore kids do
  voluntarily, and the tab-one plain-words rules apply to every label.
- [x] **2b** Combine (drag pile onto pile — same person across ages/glasses;
  critical for children's drift), remove-from-pile (returns to unnamed),
  split a mixed pile.
- [x] **2c** the learning loop: new faces close to a NAMED pile auto-assign
  when confident; borderline queue as suggestions — "Is this Daddy?" yes/no
  chips. Confidence thresholds are envelope tunables, invisible outside
  Manual.
- [x] **2d** batch-era control lives on this page (0511 §4b's override):
  select a folder/batch of scans → "these are roughly 1998–2002".
- [x] **2e** "Forget this person" per the guardrails; the whole feature's
  off-switch also states, plainly, what stored data the switch governs.

## 3. Search integration

- [x] **3a** names flow as a labelled `People:` segment through the existing
  pipeline (keyword + semantic inherit free) + `/who` operator with counts.
- [x] **3b** the killer query proof: "daddy and me on the beach" = people
  lane + CLIP/tags via existing fusion — an integration test, not a demo.

## 4. Tests

- [x] switch off → no face code runs, no face rows exist (asserted).
- [x] cluster→name→auto-assign→suggestion→confirm loop as an integration
  test on fixture faces; combine merges; forget cascades.
- [x] `/who` values with counts; the killer query finds the fixture.
- [x] all face UI labels pass the plain-words deny-list; page controls carry
  effect-stating tooltips (standing §6a rule).

## Done means

Change + tests + suite green + committed by name; CHANGELOG. Acceptance
sentence (the README screenshot, eventually): a child names a pile "Daddy",
types "daddy and me", and the right photos appear — with every byte of that
knowledge living in the house.

---

> **2026-09-19 - the page was built, tested, ticked, and never reachable.**
> A wiring audit (every public class or function under `app/` with no caller
> outside its own file, checked against the order that promised it) found
> `PhotoTaggerPage` created by nothing: no rail entry, no button, no CLI
> route. Every item above was true of the widget and false of the product -
> nobody could name a pile. Wired now: `PhotoTaggerWindow`
> (`app/ui/widgets/photo_tagger_window.py`) wraps the page, built on first use
> and reloaded on every show; **Settings > People and photo descriptions >
> "Name the people in your photos..."** and **Go > People in photos** open it;
> `MainWindow._open_photo_tagger` owns the one instance and closes it with the
> window. A window rather than a rail page on purpose - a chore done in bursts
> by people who have the feature on should not take a permanent rail slot from
> everybody else. Tests: `tests/unit/test_wired_features.py`, which presses the
> real controls on the real `MainWindow`.
