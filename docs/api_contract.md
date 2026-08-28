# Sentinel-AI — Kontrak API (Backend ↔ Frontend)

Status: draf kerja untuk pembagian role 3 orang (backend / data & integrasi / frontend).
Tujuan: frontend bisa develop terhadap kontrak ini tanpa menunggu backend selesai, dan
menutup gap antara pitch deck (menjanjikan lapisan FastAPI) dengan kode saat ini
(Streamlit memanggil `agents.orchestrator.run_analysis()` langsung).

Kontrak ini dibaca dari data yang **sudah nyata dipakai** `frontend/app.py` — bukan desain
baru dari nol. Field dan bentuknya ditarik langsung dari `run_analysis()`,
`core/audit_trail.py`, dan `load_from_history()` yang sudah berjalan.

---

## 0. Keputusan desain yang perlu disepakati tim sebelum mulai

| Keputusan | Rekomendasi | Alasan |
|---|---|---|
| Sinkron vs asinkron | **Asinkron** — `POST /runs` langsung balas `202 Accepted` + `run_id`, klien poll `GET /runs/{run_id}` | Satu siklus memanggil LLM berkali-kali (ekstraksi, retry, falsifikasi, sintesis) — bisa puluhan detik sampai beberapa menit. Blocking request akan timeout di banyak reverse proxy/hosting gratis. |
| Auth | Tidak ada untuk MVP demo | Demo kompetisi, single-tenant, tidak ada data sensitif pengguna nyata. Tambahkan di Fase 2 kalau lanjut ke B2B. |
| Format run_id | String hex (sudah ada: `uuid.uuid4().hex[:12]`) | Konsisten dengan `AuditTrail` dan `Orchestrator` yang sudah pakai format ini. |
| Live trace saat pipeline jalan | Opsional/stretch — SSE `GET /runs/{run_id}/stream` | Bagus untuk demo (§8.2 "jejak penalaran yang terlihat"), tapi bukan blocker MVP. Polling `GET /runs/{run_id}/events` tiap 1–2 detik sudah cukup untuk kompetisi. |
| Mekanisme background job | **Disepakati: `BackgroundTasks` bawaan FastAPI** | Tidak butuh Celery/Redis untuk 15–20 entitas. Status run tidak perlu disimpan di memori terpisah — `AuditTrail.open_run()`/`close_run()` sudah menulis status ke tabel `runs` di SQLite, jadi `GET /runs/{run_id}` tetap bisa dibaca meski proses API restart di tengah jalan. |

---

## 1. Ringkasan endpoint

| Method | Path | Fungsi | Dipetakan dari |
|---|---|---|---|
| `POST` | `/runs` | Mulai siklus analisis baru | `agents.orchestrator.run_analysis()` |
| `POST` | `/runs/upload` | Unggah PDF sebelum mulai run | `frontend/app.py::sidebar()` mode unggah |
| `GET` | `/runs` | Riwayat siklus (untuk sidebar) | `core.audit_trail.list_runs()` |
| `GET` | `/runs/{run_id}` | Hasil lengkap satu siklus | `load_from_history()` |
| `GET` | `/runs/{run_id}/events` | Jejak audit saja (ringan, untuk live view) | `core.audit_trail.get_events()` |
| `GET` | `/runs/{run_id}/report` | Unduh laporan (`?format=markdown\|json`) | `report_paths` |
| `GET` | `/entities/{ticker}/trajectory` | Lintasan skor risiko historis | `core.audit_trail.score_trajectory()` |
| `GET` | `/status` | Status sistem (model, search, ambang) | `core.config.get_settings()` |
| `GET` | `/monitor/status` | Status pemantauan otonom (bukti "agent, bukan chatbot") | `scheduler/monitor.py` |
| `POST` | `/monitor/run-once` | Picu satu sapuan otonom manual (demo) | `python main.py monitor --once` |

---

## 2. Detail tiap endpoint

### `POST /runs` — mulai analisis

**Request**
```json
{
  "ticker": "GOTO",
  "company_name": "PT GoTo Gojek Tokopedia Tbk",
  "sector": "teknologi",
  "document_paths": ["data/documents/GOTO_laporan_tahunan_2025.pdf"],
  "trigger": "dashboard"
}
```
`trigger` ∈ `manual | dashboard | scheduler | new_document` — nilai ini sudah dipakai apa
adanya oleh `AuditTrail.open_run()`, tinggal diteruskan.

**Response `202 Accepted`**
```json
{ "run_id": "a1b2c3d4e5f6", "status": "running", "started_at": "2026-08-28T03:00:00Z" }
```

### `POST /runs/upload` — unggah dokumen

**Request**: `multipart/form-data`, field `ticker` + satu atau lebih file `files[]` (PDF).

**Response `201 Created`**
```json
{ "paths": ["data/documents/GOTO_laporan-tahunan.pdf"] }
```
Perilaku penyimpanan sama dengan yang sudah ada di sidebar: `{ticker}_{nama_file_asli}`
di bawah `settings.doc_dir`.

### `GET /runs?ticker=&limit=50` — riwayat

**Response `200`**
```json
{
  "runs": [
    {
      "run_id": "a1b2c3d4e5f6",
      "ticker": "GOTO",
      "company_name": "PT GoTo Gojek Tokopedia Tbk",
      "sector": "teknologi",
      "trigger": "dashboard",
      "started_at": "2026-08-28T03:00:00Z",
      "finished_at": "2026-08-28T03:04:12Z",
      "status": "completed",
      "risk_score": 62.0,
      "risk_band": "Medium Risk",
      "flags_retained": 3,
      "hypotheses_dropped": 2,
      "patterns_run": 7,
      "report_path": "data/reports/GOTO_a1b2c3d4e5f6.md"
    }
  ]
}
```
Kolom persis skema tabel `runs` di `core/audit_trail.py` — tidak perlu transformasi.

### `GET /runs/{run_id}` — hasil lengkap

Response adalah objek `RunResult` — bentuknya **identik** dengan apa yang dikembalikan
`run_pipeline()`/`load_from_history()` di frontend sekarang, supaya migrasi Streamlit ke
klien HTTP tidak mengubah bentuk data yang sudah dikonsumsi panel-panel dashboard:

```json
{
  "run_id": "a1b2c3d4e5f6",
  "ticker": "GOTO",
  "company_name": "PT GoTo Gojek Tokopedia Tbk",
  "sector": "teknologi",
  "periods": ["2023", "2024", "2025"],
  "documents": [{ "doc_id": "...", "page_count": 340, "parser": "pymupdf", "default_scale_label": "jutaan Rupiah" }],
  "metrics": [
    { "key": "piutang_usaha", "period": "2025", "value": 812000, "scale": 1000000,
      "status": "terverifikasi", "page": 142, "strategy": "table_first", "source_line": "..." }
  ],
  "unreliable_metrics": ["kas_neraca_vs_arus_kas"],
  "verifications": [
    { "name": "persamaan_neraca", "period": "2025", "formula": "total_aset = total_liabilitas + total_ekuitas",
      "expected": 0, "actual": 0, "difference": 0, "passed": true, "skipped": false, "skip_reason": null }
  ],
  "indicators": [ { "label": "DER", "period": "2025", "value": 2.17, "formula": "...", "inputs": {} } ],
  "news": [ { "title": "...", "source_tier": 2, "source_weight": 0.85, "source_label": "media kredibel", "published": "2026-08-01", "url": "..." } ],
  "management_claims": [ { "claim": "...", "claim_type": "ekspansi", "direction": "positif", "checkable_metrics": ["piutang_usaha"], "source_tier": 1, "url": "..." } ],
  "findings": [
    {
      "id": "F-01", "title": "Piutang tumbuh jauh melampaui pendapatan", "pattern": "piutang_vs_pendapatan",
      "severity": "tinggi", "confidence": 0.82, "suppressed": false, "suppression_reason": null,
      "narrative": "...", "question_for_management": "...",
      "evidence": [ { "summary": "...", "citation": { "kind": "document", "document_id": "...", "page": 142, "quote": "..." } } ],
      "counter_evidence_considered": []
    }
  ],
  "dropped_hypotheses": [
    { "id": "H-04", "pattern": "arus_kas_vs_laba", "statement": "...", "verdict_reason": "Dijelaskan oleh Catatan 5: reklasifikasi investasi." }
  ],
  "risk_score": {
    "score": 62.0, "band": "Medium Risk", "assessable": true, "coverage_note": null,
    "patterns_run": 7, "flags_retained": 3, "hypotheses_dropped": 2,
    "breakdown": [ { "pattern": "piutang_vs_pendapatan", "severity": "tinggi", "confidence": 0.82, "weight": 15, "penalty": 12.3 } ]
  },
  "report": { "executive": { "headline": "...", "summary": "...", "questions": ["..."] }, "meta": { "generated_at": "...", "narrative_source": "llm" }, "annex": { "audit_trail": [] } },
  "report_paths": { "markdown": "data/reports/GOTO_a1b2c3d4e5f6.md", "json": "data/reports/GOTO_a1b2c3d4e5f6.json" },
  "audit_trail": [ { "seq": 1, "timestamp": "...", "agent": "orchestrator", "action": "susun_rencana_investigasi", "tool": null, "decision": "...", "detail": "...", "duration_ms": 12 } ],
  "duration_seconds": 47.2,
  "token_usage": { "primary": 18420, "light": 640 }
}
```

Selagi `status == "running"`, endpoint ini boleh balas versi parsial (field yang sudah
tersedia) dengan `"status": "running"` di root — memudahkan panel skor/temuan tampil
progresif kalau nanti mau dibuat live, tapi ini opsional untuk MVP; versi minimal cukup
balas `404`/`202` sampai `finalize_node` selesai.

### `GET /runs/{run_id}/events`

Payload ringan — dipakai kalau UI mau polling jejak audit tanpa menarik ulang seluruh
`RunResult` (mis. tab "Jejak audit" auto-refresh tiap beberapa detik saat run masih
`running`). Bentuknya persis daftar dict dari `core.audit_trail.get_events()`.

### `GET /runs/{run_id}/report?format=markdown|json`

Serve isi file dari `report_paths` apa adanya (`Content-Type: text/markdown` atau
`application/json`) — mengganti `st.download_button` yang sekarang baca file lokal
langsung.

### `GET /entities/{ticker}/trajectory?limit=12`

```json
{ "ticker": "GOTO", "points": [ { "started_at": "2026-07-01T03:00:00Z", "risk_score": 58.0, "risk_band": "Medium Risk" } ] }
```
Peta langsung dari `score_trajectory()`.

### `GET /status`

```json
{
  "model_primary": "claude-sonnet-4-6", "model_light": "claude-haiku-4-5",
  "has_llm": true, "has_search": true,
  "min_confidence": 0.55, "verify_rel_tolerance": 0.005
}
```

### `GET /monitor/status` dan `POST /monitor/run-once`

Bukan sekadar nice-to-have — ini permukaan yang membuktikan §8.2 "pemicu tanpa manusia"
ke juri tanpa harus intip terminal server:
```json
{
  "watchlist": ["GOTO", "BUMI", "TLKM"],
  "cron": "0 3 * * *",
  "last_sweep_at": "2026-08-28T03:00:04Z",
  "next_sweep_at": "2026-08-29T03:00:00Z",
  "last_sweep_runs": ["a1b2c3d4e5f6"]
}
```
`POST /monitor/run-once` memicu `python main.py monitor --once` secara terprogram —
berguna untuk tombol "Jalankan sapuan otonom sekarang" di demo langsung, tanpa switch ke
terminal di depan juri.

---

## 3. Pembagian kerja implementasi

| Bagian | Siapa | Catatan |
|---|---|---|
| Route handlers + Pydantic response models | **Backend** | Tinggal membungkus fungsi yang sudah ada (`run_analysis`, `list_runs`, `get_events`, `score_trajectory`) — logika agen tidak berubah sama sekali. |
| Background task queue untuk `POST /runs` | **Backend** | `BackgroundTasks` FastAPI bawaan cukup untuk skala MVP (15–20 entitas); tidak perlu Celery/Redis di tahap ini. |
| `/runs/upload`, penyimpanan file | **Data & integrasi** | Sudah ada logikanya di `sidebar()` — pindahkan saja, bukan tulis ulang. |
| `/monitor/*` | **Backend** (beririsan dengan `scheduler/monitor.py`) | Endpoint ini murni pembungkus tipis di atas modul yang sudah jalan. |
| Klien HTTP di Streamlit (ganti import langsung → `requests`/`httpx`) | **Frontend** | Ganti `from agents.orchestrator import run_analysis` dengan pemanggilan endpoint; bentuk data di panel (`score_panel`, `findings_panel`, `annex_panel`) **tidak perlu diubah** karena kontrak di atas sengaja disamakan bentuknya dengan yang sudah dikonsumsi kode itu sekarang.

---

## 4. Yang sengaja tidak dimasukkan MVP ini

- Autentikasi/otorisasi — tidak relevan untuk demo kompetisi single-tenant.
- Rate limiting — tidak perlu untuk 15–20 entitas MVP.
- WebSocket/SSE penuh — polling `GET /runs/{run_id}` cukup; jadikan item Fase 2 kalau
  waktu tersisa dan tim ingin dashboard terasa lebih "hidup" saat demo.
