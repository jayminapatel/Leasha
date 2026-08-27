# Work order (One thread): the Photo Tagger — naming people, the Google Photos way, fully local

**Doc version:** 1.0 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
**Thread:** One thread (Extract/AI + Storage + new page UI + Search)
**Status:** RELEASED by the owner 2026-08-28. Requires 0510 (image pass
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

- [ ] **1a** face step in the images pass (only when the switch is on):
  detect faces (`insightface`, ONNX — installed; ~20–50ms/img budget),
  compute per-face embedding, store (file_id, bbox, embedding). Backfill for
  already-indexed images runs as an enrichment-backlog job kind (0511 §2).
- [ ] **1b** unsupervised clustering into unnamed piles ("Person 1 — 47
  photos"): incremental cosine-threshold clustering, re-clusterable; nobody
  is identified — there are just piles.

## 2. The Photo Tagger page (the user half)

- [ ] **2a** grid of piles, biggest first, sample crops; click → name it.
  Designed for an 8-year-old — this is the one indexing chore kids do
  voluntarily, and the tab-one plain-words rules apply to every label.
- [ ] **2b** Combine (drag pile onto pile — same person across ages/glasses;
  critical for children's drift), remove-from-pile (returns to unnamed),
  split a mixed pile.
- [ ] **2c** the learning loop: new faces close to a NAMED pile auto-assign
  when confident; borderline queue as suggestions — "Is this Daddy?" yes/no
  chips. Confidence thresholds are envelope tunables, invisible outside
  Manual.
- [ ] **2d** batch-era control lives on this page (0511 §4b's override):
  select a folder/batch of scans → "these are roughly 1998–2002".
- [ ] **2e** "Forget this person" per the guardrails; the whole feature's
  off-switch also states, plainly, what stored data the switch governs.

## 3. Search integration

- [ ] **3a** names flow as a labelled `People:` segment through the existing
  pipeline (keyword + semantic inherit free) + `/who` operator with counts.
- [ ] **3b** the killer query proof: "daddy and me on the beach" = people
  lane + CLIP/tags via existing fusion — an integration test, not a demo.

## 4. Tests

- [ ] switch off → no face code runs, no face rows exist (asserted).
- [ ] cluster→name→auto-assign→suggestion→confirm loop as an integration
  test on fixture faces; combine merges; forget cascades.
- [ ] `/who` values with counts; the killer query finds the fixture.
- [ ] all face UI labels pass the plain-words deny-list; page controls carry
  effect-stating tooltips (standing §6a rule).

## Done means

Change + tests + suite green + committed by name; CHANGELOG. Acceptance
sentence (the README screenshot, eventually): a child names a pile "Daddy",
types "daddy and me", and the right photos appear — with every byte of that
knowledge living in the house.
