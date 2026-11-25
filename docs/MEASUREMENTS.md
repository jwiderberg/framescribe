# Measurements

The numbers behind framescribe's choices of speech engine, chunking, OCR and change detection, with what was
measured and on what input. They are results of the project's development, not guarantees: your recordings, your
machine and your load will differ.

Nothing in this file quotes the recordings it was measured on. Where a result depends on the content (which words an
engine got wrong), only counts are given.

## Inputs and machine

- **Machine:** MacBook Pro, Apple M3 Max, 36 GB. A shared, busy machine: the load average was 4 to 17 during most
  runs, so the timings are upper bounds, not quiet-machine numbers.
- **Recording:** a 1 h 53 min (6769 s) screen recording of a video call: a web application shared on screen, 720p,
  about 600 kbps, 24 fps, an embedded caption track (1521 cues, produced by the call software's own speech
  recognition). English.
- **Slice:** for the engine, OCR and change-detection comparisons, a 10-minute slice (600 s) from the middle of that
  recording, 16 kHz mono audio, the shared-screen area cropped to 960 x 516 pixels.
- **Speech model on disk:** `mlx-community/parakeet-tdt-0.6b-v3`, 2.5 GB: the weights file `model.safetensors` is
  2,508,288,736 bytes and `config.json` 244,093 bytes (read from the Hugging Face cache). This is the figure the README
  and `framescribe setup` quote for the download and the disk space.
- Numbers marked "extrapolated" were calculated, not measured.

## 1. Speech engines (10-minute slice)

| Engine and build | Wall time | Realtime factor | Peak memory | Words out |
|---|---|---|---|---|
| parakeet-mlx 0.7.0, `mlx-community/parakeet-tdt-0.6b-v3`, 120 s chunks with 15 s overlap | 7.25 s | 82.7x | process 1.26 GB; MLX peak 3.26 GB | 1155 |
| parakeet-mlx, no chunking | 17.44 s | 34.4x | process 1.30 GB; MLX peak 15.07 GB | 1150 |
| mlx-whisper 0.4.3, OpenAI's distilled large-v3-turbo, 8-bit | 18.16 s | 33.0x | process 1.35 GB; MLX peak 2.01 GB | 1151 |
| WhisperKit CLI, debug build, CoreML large-v3 (Argmax's speed-optimised build), CPU and Neural Engine | 87.7 s | 6.95x | process 790 MB | 1106 |
| WhisperKit, first run of the model | 286 s, of which 200.6 s was CoreML compilation | 2.1x | process 1.89 GB | not recorded |

- Model load for parakeet was 0.35 s with the model on disk.
- Extrapolated to the full recording: parakeet chunked about 82 s, mlx-whisper 8-bit about 205 s, WhisperKit (debug
  build) about 16 min. The pipeline's own run on the full recording is in section 6.
- Parakeet decoded without chunking needs about 15 GB of MLX memory for 10 minutes of audio, so a long file must be
  chunked.
- The WhisperKit build was a debug build; a release build was not measured, so 6.95x is a floor for the engine.
- The fp16 mlx-whisper model was not measured (download budget), so its quality is unknown; the 8-bit one produced
  the outright garbles among the local engines.

### Agreement between engines

Word-sequence distance (1 minus the Dice coefficient on normalised word sequences; 0 means identical):

| Pair | Distance |
|---|---|
| parakeet and the embedded captions | 0.074 |
| parakeet and WhisperKit | 0.092 |
| mlx-whisper and WhisperKit | 0.097 |
| mlx-whisper and the embedded captions | 0.101 |
| WhisperKit and the embedded captions | 0.104 |
| parakeet and mlx-whisper | 0.105 |

- The engines disagreed in about 95 regions of the 10-minute slice (parakeet against mlx-whisper).
- Filler words (uh, um, ah, hmm, er) in the slice: captions 43, parakeet 38, mlx-whisper 15, WhisperKit 4. Parakeet
  and the caption track keep disfluencies; the two Whisper variants mostly remove them, and with them repeated words,
  which loses timing.
- Of 16 contested passages judged by hand against the on-screen text and the context, WhisperKit was right in 10,
  parakeet in 5 and mlx-whisper 8-bit in 2; in 4 passages every local engine was wrong. Parakeet's errors were
  mostly dropped or merged function words and split compounds; mlx-whisper produced invented phrases.
- No engine heard the name of the application shown on screen correctly. One substituted the name of a different,
  real vendor. This is why the report keeps the spoken form and tells the reader that the on-screen spelling is the
  authority.
- Matched-word start-time offsets against parakeet (median absolute): mlx-whisper 0.14 s, WhisperKit 0.20 s,
  captions 0.36 s (captions only have four-second buckets; word times inside them are interpolated).

Decision: parakeet is the supported path (fast, keeps fillers, closest to the captions). WhisperKit gave the best
sentences on contested words but is about 12 times slower, so it is optional.

## 2. OCR (10 frames, 960 x 516 crop, one per minute)

Recall is against 355 hand-read labels (18 to 41 per frame: navigation items, tab names, headings, buttons, chart
labels, sentences). A label counts as found when its lower-cased alphanumeric form is a substring of the frame's
recognised text. Latency is the mean over the 10 frames after a warm-up call.

| Engine | Scale | Mean latency per frame | Label recall |
|---|---|---|---|
| Apple Vision, accurate | 1x | 0.297 s | 80.6% (286 of 355) |
| Apple Vision, accurate | 2x (Lanczos) | 0.345 s | 89.9% (319 of 355) |
| Apple Vision, fast | 1x | 0.061 s | 7.0% |
| Apple Vision, fast | 2x | 0.144 s | 11.3% |
| RapidOCR (PP-OCRv6 small, ONNX, CPU) | 1x | 1.215 s | 91.0% |
| RapidOCR | 2x | 1.685 s | 91.5% |

- Chosen: Apple Vision, accurate level, on a 2x Lanczos upscale. RapidOCR is 3.5 to 4.9 times slower for 0.9 to 1.6
  points more recall; Vision's fast level reads only large headings.
- Error kinds seen in the frames: a capital I read as a lower-case l in the sans-serif font (10 of the misses for
  each engine), small-text character errors that the 2x upscale mostly fixes, dropped rows at 1x, and wrapped
  sentences returned as separate boxes. RapidOCR merges neighbouring labels without a space and has isolated garbage;
  it finds tiny items such as avatar initials that Vision misses.
- Peak process memory of the script with all engines loaded: 2.06 GB.

## 3. Change detection (2 fps, gray crop, 10-minute slice)

| Measurement | Result |
|---|---|
| ffmpeg software decode, `fps=2`, crop, gray rawvideo | 2.90 s (about 205x realtime) |
| the same with `-hwaccel videotoolbox` | 20.1 to 27.5 s (22 to 30x realtime), 7 to 9 times slower |
| decode, pipe 1200 frames and diff them in Python (numpy, 960 x 516) | 2.90 s wall time, peak process memory 383 MB |
| extrapolated to the full recording | about 33 s |

The signal is the fraction of pixels whose gray value changes by more than 16 between consecutive samples. Over 1199
pairs: 696 below 0.0005, 175 in 0.0005 to 0.002, 175 in 0.002 to 0.01, 59 in 0.01 to 0.05, 86 in 0.05 to 0.2, 8 above
0.2; the median was 0.0003. Compression noise and a moving pointer (about 0.0006) are below 0.002; a toast sliding in
or out is about 0.02; scrolls are 0.07 to 0.25; page and dialog switches are 0.5 to 0.83.

States found in 10 minutes by threshold (an event opens above the first value and settles below the second for two
consecutive pairs; events within 1 s merge; a state is kept only if it differs from the previous kept state by more
than 0.01):

| Open above / settle below | States |
|---|---|
| 0.003 / 0.001 | 36 |
| 0.005 / 0.002 | 48 |
| **0.01 / 0.002** | **49** |
| 0.02 / 0.005 | 38 |
| 0.05 / 0.005 | 33 |

Chosen: 0.01 and 0.002, about one state per 12 s on this recording. A hand check of contact sheets of the first 24
states found real page and dialog changes and real scrolls; the early classifier mislabelled two page and dialog
changes as scrolls and gave a low magnitude to one navigation between sparse pages, so the kind of change is a hint,
not a measurement.

## 4. Vision-language models (not used)

Two local models in LM Studio were tried on 5 frames with 2 prompts each (describe the frame; what is the speaker
referring to).

| Model | Load | Per call (10 calls) | Decode speed | Memory |
|---|---|---|---|---|
| `google/gemma-4-e4b` | 14.1 s cold | mean 22.2 s | mean 35.6 tokens/s | 6.33 GB resident |
| `google/gemma-4-26b-a4b`, context 8192 | 21.9 s | mean 24.8 s, range 17.6 to 50.8 s | mean 41.4 tokens/s | about 18 GB |

- 86% and 92% of the completion tokens were reasoning tokens, so latency was dominated by reasoning.
- The 26B model was refused at its default context (about 35 GB needed) and loaded at a context of 8192.
- The frame is encoded to roughly 280 tokens, so neither model reads small interface text; one invented a label.
  Neither answer quoted small text reliably. Referent task: the smaller model 4 correct and 1 partial of 5, the larger
  5 of 5 (one debatable).
- At one call per state this would take about 18 minutes per 10 minutes of video (extrapolated). No model is part of
  the pipeline.

## 5. Chunk length and context for parakeet (full recording)

Configurations were decoded with the pipeline's own chunking code (`scripts/asr_chunking.py`) and compared, with the
pipeline's completeness check, against three independent transcripts of the same audio: WhisperKit large-v3, the
embedded captions, and a second parakeet decode in 40 s windows that keep their middle 20 s. A missing phrase is a gap:
a run of a reference's words where the configuration has no words and leaves room for them. Configuration names give
the target chunk length, the digital silence before the chunk (pad), and the seconds of audio decoded before the
cut (L) and after the chunk's end (R); words found in that context are dropped. 30 s chunks have a 40 s maximum, 20 s
chunks a 30 s maximum.

| Configuration | Words | Chunks | Longest chunk (s) | Phrases missing against any reference | Missing in 2 or more references | Chunks of 10+ words without punctuation | Wall time (s) |
|---|---|---|---|---|---|---|---|
| 120 s, pad 0.5, L1 R0 | 10599 | 57 | 134.8 | 170 | 83 | 0 | 88.5 |
| 30 s, pad 0.5, L1 R0 | 10753 | 225 | 37.0 | 108 | 33 | 2 | 164.8 |
| 30 s, pad 0, L0 R0 | 10724 | 225 | 37.0 | 112 | 37 | 0 | 177.1 |
| 30 s, pad 0, L1 R0 | 10780 | 225 | 38.2 | 111 | 35 | 3 | 182.2 |
| 30 s, pad 0, L0 R2 | 10742 | 225 | 38.2 | 115 | 33 | 0 | 139.6 |
| 30 s, pad 0, L1 R2 | 10780 | 225 | 38.2 | 104 | 32 | 4 | 161.4 |
| 30 s, pad 0, L2 R2 | 10774 | 225 | 37.0 | 114 | 30 | 5 | 199.4 |
| 30 s, pad 0.5, L1 R2 | 10759 | 225 | 37.0 | 105 | 31 | 3 | 125.5 |
| 20 s, pad 0, L1 R2 | 10795 | 342 | 29.8 | 108 | 25 | 14 | 132.6 |
| 30 s, pad 0, L1 R2, retry chain | 10786 | 225 | 38.2 | 111 | 31 | 0 | 137.2 |
| 30 s, pad 0, L2 R2, retry chain | 10758 | 225 | 37.0 | 118 | 31 | 0 | 133.1 |
| 30 s, pad 0, L1 R2, retry chain, seam de-duplication (final) | 10784 | 225 | 38.2 | 111 | 31 | 0 | 119.0 |

The first nine rows used an earlier retry for chunks without punctuation; the next two use the pipeline's retry chain;
the last uses the final pipeline code, which also keeps a word once when both chunks around a cut decoded it.

Findings:

- 120 s chunks miss far more than 30 s chunks: 170 phrases against 104 to 118, and 83 confirmed by two references
  against 30 to 37.
- Digital silence before a chunk does not help, and decoding without context on either side misses more. Context after
  the chunk's end lowers the number of sentence breaks forced at cuts (115 to 135 of 224 cuts, against 142 for
  WhisperKit at the same moments; up to 165 without it).
- Whether a chunk keeps its punctuation depends on the exact audio the model sees and is not monotonic in the
  context. A retry chain over a few contexts left no chunk without punctuation.
- 20 s chunks miss slightly fewer phrases but bring back chunks without punctuation (14) and garbled words near cuts
  (10). Rejected.

Chosen: 30 s target, 40 s maximum, no padding, 1 s of context before the cut and 2 s after the chunk, with the retry
chain. Among the configurations without unpunctuated chunks it missed the fewest phrases (111) and tied for the fewest
confirmed by two references (31), with no duplicated or garbled text at seams and no split word.

### Casing at cuts

The context does not stop the model from starting a sentence at a cut. Measured as the share of cuts where the first
word after the cut is capitalised although the word before it ends no sentence (`scripts/asr_casing.py`):

| | Share |
|---|---|
| first word after a cut, before taking its casing from the other pass | 24% |
| the same, after (pass 1 13.4%, pass 2 12.0%) | 12 to 13% |
| words after pauses that are not cuts | about 2% |

The remaining capitals are mostly where the model left out a full stop before the cut or the other pass has no
matching word at that time.

### Candidates the cross-check does not fill

On the full recording, with the second pass and the captions as sources: 74 candidates were dropped because the primary
transcript leaves too little silence for them (67 of them single words); 48 more that had room were dropped because
they only repeat a neighbouring word or lie inside a caption cue the primary already covers (47 of them single words).

## 6. The pipeline on the full recording

Measured with the pipeline in this repository on the whole 1 h 53 min recording (6769 s of audio, 1521 caption cues),
load average 6 to 17, so the times are upper bounds.

| Stage | Wall time | Result |
|---|---|---|
| `asr.parakeet` | 429 s (pass 1 251 s, pass 2 174 s, model loaded once) | 10,827 words in 225 chunks (longest 38 s), 11 kept without punctuation because no punctuated decode had as many words; second pass 10,824 words in 226 chunks, 4 kept without punctuation; no chunk without words in either pass |
| `asr` (`--asr parakeet`) | 1.0 s | cross-checked against the second pass and the captions: 10,955 words, of which 128 (1.2%) filled in (100 from the second pass, 28 from the captions); 8 confirmed, 59 unconfirmed and 6 rejected gaps |
| `visual` | 164 s | 763 states (6.8 per minute), 22 changes cut off before settling, 2 held back by the state-rate safeguard, 130 pointer samples |
| `ocr` | 261 s for 763 key frames (0.34 s per frame) | 53 lines per state (median) |
| `report` | 0.4 s | 54 steps, 10,955 words in SAID lines, about 67,000 words and 450 KB in `report.md`, all 763 key frames linked |

Kinds of change: 171 scroll, 130 page, 260 partial, 201 minor, 1 initial.

A second run with the same inputs loads every stage from the cache and rewrites no output file except `run.json`.

Total for a recording of this length: about 12 minutes on this machine when it is not otherwise busy, of which the
speech stage is about 5 (an earlier run of the speech stage, with the model load, took 295 s at a load average of 7 to
9; the table above is slower because the machine was busier).

## 7. Memory of the speech stage

MLX keeps every buffer it frees in a cache whose default size is unbounded. Chunks differ in length, so freed buffers
rarely fit the next decode.

| | Before | After (cache limited to 512 MB) |
|---|---|---|
| MLX cache after 10 minutes of audio (56 decodes over two passes) | grew by about 0.5 GB per decode, to 26.1 GB | flat at about 505 MB |
| MLX active memory | flat at about 1.2 GB | flat at 1236 MB |
| lowest system free memory | 29% | 81% or more |
| time of the decode loop | 23.4 s | 19.6 s |
| words | | identical |

Chunked decoding with 120 s chunks (no pipeline) peaked at 3.26 GB of MLX memory (section 1).

## 8. Visual and OCR stages: hand checks

Checked by hand on a 10-minute window of the full recording (60 states), part of a second window and 20 pointer
samples:

- About one state in five in the first window was a near-duplicate of the one before it (a small highlight or an
  animation); they have kind `minor`.
- One continuous scroll session that goes down and back up gave one state; one such case was found in 48 sampled
  frames of the second window.
- Scroll shifts of 25 px or less (29 of 172 scroll states) are doubtful; the text shift found by OCR agreed with the
  visual shift within 8 px for 85 of the 144 scroll states where both exist.
- 22 changes were cut off after 10 s without settling; `settled` is false for 20 of the resulting states.
- Pointer: of 20 samples, 14 were on the pointer and 6 were not (3 at the edge of a dark sidebar, 3 on a hovering
  button or a toast's close icon). 706 of 764 states in that run have no pointer sample, by design.
- OCR makes character errors in small text and misses icon-only items; no line of running text was missed in the three
  frames read. Roughly one on-screen line in five or six has a misread.
