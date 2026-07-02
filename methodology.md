# Methodology

**Context:** ~60 hours of work (3 weeks × ~5 days × 4h). The capture harness is already
built and live-validated, so this time goes to finishing the data, building the models,
and running the experiments — not building the harness.

## What's being studied
- **Main task:** label each *flow* (one conversation between two machines) as
  **Zoom audio / Zoom signaling / background / other**.
- **Headline result:** Zoom audio vs Zoom signaling — the genuinely hard one (both are
  encrypted and both go to Zoom's servers).
- **Easy baseline result:** Zoom vs background (kept as a simpler secondary finding).
- **Exploratory extra:** estimate how many people are on a call from the traffic timeline.
- **Models:** ET-BERT (main) plus two simple models — Random Forest and GRU — as baselines.

## What we train on
- **One flow = one training example.** Its label comes from the harness's labeler.
- **Two views of each flow:**
  - ET-BERT → the raw **bytes of the first few packets** (addresses/ports blanked out).
  - Random Forest / GRU → a handful of **summary numbers** (packet count, total bytes,
    duration, packet-size stats, gaps between packets, up/down ratio).
- Only the **start of each flow plus a few numbers** is ever used — which is why the full
  packet captures are mostly waste and can be trimmed/archived.

---

## Week 1 — Lock the data + build the spine
- **Day 1:** turn noise volume down (config edit) + finish the one quality-check flag.
- **Day 1–2:** run the 20–30 call pilot on AWS (runs while you work); auto-label it.
- **Day 2–3:** build the spine — split each capture into flows, attach the label, and pull
  out both views (start-of-flow bytes + summary numbers). Everything else reuses this.
- **Day 3:** from the pilot, measure size-per-flow and packets-per-flow → set the real
  dataset size (~100 calls) and the "save only the first part of each packet" cut-off.
- **Day 4–5:** start the ~100-call run generating in the background; meanwhile build the
  two simple models on pilot data to prove the spine works end-to-end.
- ✅ **Deliverable:** working pipeline + first baseline numbers + real data generating.

## Week 2 — Core results, done properly
- Split by **whole call** (no call appears in both train and test); keep call-length spread
  equal across train and test.
- Run the 4-way result (Zoom audio / signaling / background / other) with the simple models
  on the full dataset.
- Stand up **ET-BERT** on the same flows. Budget **2 full days** — it's the fiddly part.
- Run the **headline** result: Zoom audio vs Zoom signaling.
- Report everything at the realistic **~5% Zoom mix**, using precision/recall (not just
  accuracy — it exposes false alarms).
- ✅ **Deliverable:** main results table — simple models + ET-BERT, 4-way + the hard 2-way,
  at the realistic mix.

## Week 3 — Sharp questions + write-up
- **Early-detection curve:** accuracy using the first 5 / 10 / 15 / 30 packets; show where
  it flattens. *(free — reuses the same captures)*
- **Real-world test:** record 3–5 real human Zoom calls; use them **only as a test** → does
  the model transfer beyond bots? *(small cost)*
- **Honest comparison:** does the simple Random Forest match ET-BERT? What gets confused
  (video-noise vs Zoom audio)? *(free)*
- **One more if time:** realistic-mix sweep, OR train-on-one-noise-blend/test-another, OR
  train-on-2-party/test-on-3-party. *(free)*
- **Last 2–3 days:** make the plots/tables and write the methodology + results.
- ✅ **Deliverable:** full results + 2–3 analyses + written method/results.

---

## Rules to protect the grade
- Build the simple models **before** ET-BERT — they're the safety net if ET-BERT eats time.
- Generate data in the background; never let it block programming.
- Pick only **2–3** of the Week-3 extras — depth beats breadth.
- Choose packet-count / settings on a **validation** split; report final numbers on a test
  set you never touched.
- If time runs short, cut ET-BERT extras **before** cutting rigor — a clean baseline result
  with solid method outscores a half-working transformer.