# Sentinel-AI — To-Do per Role (Tahap 2)

Konteks: Tahap 1 sudah lolos. Penilaian Tahap 2 = 30% AI Agent Implementation + 10% Q&A
sesi juri (§8.5 proposal). Semua item di bawah diprioritaskan terhadap dua angka itu, bukan
terhadap jumlah fitur.

Prioritas: **P0** = blocker demo/penilaian, kerjakan duluan. **P1** = memperkuat skor kalau
P0 semua sudah beres. **P2** = bagus untuk dimiliki, jangan dikerjakan sebelum P0/P1 selesai.

---

## Backend — Agent & Orchestration Engineer

Pemilik: `agents/orchestrator.py`, `agents/financial_auditor.py`, `agents/red_flag_investigator.py`,
`core/state.py`, `tools/python_sandbox.py`, `scheduler/monitor.py`.

- [ ] **P0** — Install dependencies di environment bersih (`pip install -r requirements.txt`), jalankan `pytest -q`, catat hasil sebenarnya (jangan asumsikan klaim "73 pengujian" di README masih akurat sampai diverifikasi ulang)
- [ ] **P0** — Jalankan `python main.py demo` end-to-end, baca laporan yang dihasilkan, pastikan masuk akal
- [ ] **P0** — Jalankan varian `build_demo_pdf(overwrite=True, corrupt_balance=True)` dan rekam prosesnya — ini bukti self-correction (§8.2 poin 3), harus ada rekaman nyata sebelum video pitch/demo dibuat
- [ ] **P0** — Bangun FastAPI app sesuai `docs/api_contract.md`: `POST /runs` (pakai `BackgroundTasks`), `GET /runs`, `GET /runs/{run_id}`, `GET /runs/{run_id}/events`, `GET /runs/{run_id}/report`
- [ ] **P0** — Tulis Pydantic response models untuk endpoint di atas, sesuai bentuk field yang sudah didokumentasikan di kontrak (supaya frontend tidak perlu menunggu untuk mulai integrasi — sepakati model dulu, isi implementasi belakangan)
- [ ] **P1** — Endpoint `GET /entities/{ticker}/trajectory`, `GET /status`
- [ ] **P1** — Endpoint `GET /monitor/status` dan `POST /monitor/run-once` — bukti "pemicu tanpa manusia" tanpa harus buka terminal saat presentasi ke juri
- [ ] **P1** — Verifikasi `scheduler/monitor.py` benar-benar mendeteksi dokumen baru dalam 15 menit dan cron 03:00 jalan; siapkan log yang bisa ditunjukkan sebagai bukti
- [ ] **P1** — Cari/rekayasa skenario dokumen demo yang menghasilkan hipotesis **digugurkan** (bukan cuma bertahan) — cek apakah `demo_corpus.py` sudah punya ini; kalau belum, ini blocker untuk bukti §8.2 poin 4
- [ ] **P2** — SSE `GET /runs/{run_id}/stream` untuk live trace (stretch, hanya kalau P0/P1 sudah aman)
- [ ] Siapkan diri untuk Q&A juri seputar pilihan arsitektur (§8.3): kenapa SQLite bukan Postgres, kenapa Chroma bukan pgvector, kenapa retry loop dibatasi 3 percobaan

---

## Data & Integration — Scraper/Retrieval Engineer

Pemilik: `agents/market_intelligence.py`, `tools/web_search.py`, `tools/document_parser.py`,
`tools/retrieval.py`, `tools/demo_corpus.py`.

- [ ] **P0** — Tentukan bersama tim: 1 sektor + 15–20 entitas riil untuk MVP (proposal §6.4 sudah janji ini ke juri — kalau belum ada daftar konkret, ini yang paling mendesak)
- [ ] **P0** — Unduh laporan tahunan riil 15–20 entitas itu dari idx.co.id, uji `document_parser.py` terhadap PDF asli (bukan cuma demo corpus fiktif) — README sendiri menyebut parsing tabel keuangan sebagai "tantangan teknis utama proyek ini", jadi harus divalidasi terhadap dokumen nyata sebelum diklaim selesai
- [ ] **P0** — Implementasikan `POST /runs/upload` (pindahkan logika yang sudah ada di `frontend/app.py::sidebar()` mode "Unggah laporan tahunan", jangan tulis ulang dari nol)
- [ ] **P0** — Cek `core/state.py::METRIC_VOCABULARY` mencakup kaption yang dipakai sektor yang dipilih tim; kalau sektor bukan "konsumer" (default demo), kemungkinan perlu tambah alias
- [ ] **P1** — Verifikasi `tools/retrieval.py` — pastikan backend Chroma benar-benar aktif (bukan fallback BM25 diam-diam) di environment demo; kalau model embedding tidak tersedia, itu harus diketahui sebelum hari-H, bukan ditemukan saat demo
- [ ] **P1** — Tambah Google News RSS sebagai pelengkap Tavily di `web_search.py` (gratis, tanpa auth, menambah cakupan media Indonesia) — **jangan** investasikan waktu ke scraping Threads/Twitter, lihat catatan di bawah
- [ ] **P1** — Review tabel tier kredibilitas di `core/config.py`, tambah domain media Indonesia relevan untuk sektor yang dipilih
- [ ] **P2** — Kalau waktu tersisa: eksplorasi Reddit API resmi (PRAW) sebagai sinyal komunitas tambahan — nilai rendah, jangan prioritaskan
- [ ] **Catatan tetap berlaku**: jangan kejar scraping Threads/Twitter — API resmi Threads tidak mendukung search publik by-keyword, X API baca berbayar sejak 2023, dan keduanya bertentangan dengan desain sistem sendiri yang sudah memberi bobot rendah (~0.15) ke sumber forum/sosial

---

## Frontend — Dashboard & API Client Engineer

Pemilik: `frontend/app.py`, `core/report_generator.py`.

- [ ] **P0** — Baca `docs/api_contract.md` detail, sepakati response models dengan backend sebelum backend selesai implementasi (supaya kerja paralel, bukan berurutan)
- [ ] **P0** — Ganti `from agents.orchestrator import run_analysis` dan pemanggilan langsung `core.audit_trail.*` di `frontend/app.py` dengan HTTP client (`httpx`/`requests`) ke endpoint kontrak — panel (`score_panel`, `findings_panel`, `annex_panel`) tidak perlu ditulis ulang karena bentuk data sengaja disamakan
- [ ] **P0** — Implementasikan polling untuk `POST /runs` yang asinkron: progress bar sekarang (`run_pipeline()`) perlu diganti pola poll `GET /runs/{run_id}` tiap 1–2 detik sampai `status != "running"`
- [ ] **P1** — Tambah UI untuk endpoint `/monitor/status` — panel kecil yang menunjukkan watchlist, jadwal cron, sapuan terakhir; ini permukaan visual dari bukti "pemicu tanpa manusia" yang paling gampang ditunjukkan ke juri tanpa buka terminal
- [ ] **P1** — Tambah tombol "Jalankan sapuan otonom sekarang" yang memanggil `POST /monitor/run-once` — berguna untuk demo langsung
- [ ] **P1** — Regression check menyeluruh setelah migrasi: pastikan tampilan identik dengan versi sebelum FastAPI (skor, gauge, tabel bukti, hipotesis digugurkan, disclaimer)
- [ ] **P2** — Polish visual mengikuti branding pitch deck (skema NAVY/GOLD sudah konsisten, tinggal cek across semua state baru — loading, error)
- [ ] Siapkan dashboard dalam kondisi yang enak direkam layar — ini yang dipakai untuk 40 detik segmen "rekaman layar agent yang benar-benar berjalan" kalau ada kebutuhan materi video tambahan di Tahap 2

---

## Lintas-role (siapa saja, koordinasi bersama)

- [ ] **P0** — Rapikan branch: kerja tersebar di `farrel`, `faundra`, `awan` — merge ke `main` sebelum dinilai, `main` harus mencerminkan kondisi kerja terbaru
- [ ] **P0** — Pastikan `.env` tidak ter-commit (API key aktif ada di sana) — cek `.gitignore` menangkapnya
- [ ] **P0** — Konfirmasi akses repo GitHub untuk panitia/juri kalau repo private
- [ ] **P1** — Sync mingguan singkat: minimal satu orang selain pemegang backend paham alur `orchestrator.py`, supaya tidak ada single point of failure sebelum demo day
- [ ] **P1** — Latihan bersama 5 pertanyaan antisipasi juri (§8.3 proposal) + pertanyaan tambahan seputar keputusan arsitektur nyata di kode
- [ ] **P2** — Bersihkan file besar yang tidak perlu di root repo (`sentinelai.txt`, docx proposal) supaya kesan pertama repo rapi saat dibuka juri

---

## Definition of done untuk Tahap 2 (checklist ringkas)

Sebelum menganggap "siap demo", empat hal ini (§8.2 proposal) harus **terlihat**, bukan
cuma ada di kode:

- [ ] Log sapuan terjadwal jalan tanpa ada yang mengetik (jam 03:00 atau trigger dokumen baru)
- [ ] Jejak penalaran per agen terlihat rapi di UI (tujuan → rencana → tool → hasil → keputusan)
- [ ] Rekaman nyata: verifikasi aritmetik gagal → strategi ekstraksi berganti → berhasil
- [ ] Rekaman nyata: hipotesis red flag ditemukan → dicari bukti penyangkal → **digugurkan** oleh agen itu sendiri
