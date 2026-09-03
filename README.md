# Sentinel-AI

**Membaca Risiko Sebelum Menjadi Kerugian.**

Sistem multi-agen otonom untuk *financial due diligence* atas emiten Bursa Efek
Indonesia. Sentinel-AI membaca laporan tahunan, memverifikasi setiap angka
secara deterministik, mencari kontradiksi lintas dokumen dan lintas tahun, lalu
**menguji setiap hipotesisnya terhadap bukti penyangkal** sebelum menyusun
*Executive Due Diligence Report* yang seluruh klaimnya tertaut ke halaman sumber.

> Sentinel-AI adalah **alat riset dan penyaringan risiko, bukan penasihat
> investasi**. Sistem tidak memprediksi harga saham dan tidak memberikan
> rekomendasi beli, jual, atau tahan. Setiap temuan disajikan sebagai indikator
> yang perlu diklarifikasi. Keputusan tetap berada pada manusia.

BISA AI National AI Agent Challenge 2026 · Muhammad Farrel Akbar · Faundra Pratma Sukma · Muhammad Setiawan

---

## Mulai cepat

```bash
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt
copy .env.example .env                              # isi ANTHROPIC_API_KEY dan TAVILY_API_KEY

python main.py demo                                 # pipeline lengkap atas dokumen demo
streamlit run frontend/app.py                       # dashboard
pytest -q                                           # 73 pengujian
```

Tanpa API key sistem tetap berjalan pada **jalur deterministik**: ekstraksi
berbasis kaption, verifikasi aritmetik, tujuh pola deteksi, dan laporan
bersitasi tetap dihasilkan. Yang dilewati hanyalah adjudikasi bukti penyangkal
dan narasi eksekutif — dan laporan menyatakan hal itu secara eksplisit.

---

## Arsitektur

```
                          ┌──────────────────────┐
                          │  Streamlit Dashboard │
                          └──────────┬───────────┘
                                     │
                          ┌──────────▼───────────┐
                          │  Orchestrator Agent  │   LangGraph — state machine,
                          │   (agents/orchestr.) │   conditional edge, retry loop
                          └──────────┬───────────┘
             ┌───────────────────────┼───────────────────────┐
             │ paralel                                       │
   ┌─────────▼──────────┐                        ┌───────────▼──────────┐
   │ 1 Financial Auditor│                        │ 2 Market Intelligence│
   │ ekstraksi + verif. │                        │ berita + klaim       │
   └─────────┬──────────┘                        └───────────┬──────────┘
             └───────────────────────┬───────────────────────┘
                          ┌──────────▼───────────┐
                          │ 3 Red Flag Investig. │  hipotesis → falsifikasi
                          └──────────┬───────────┘
                          ┌──────────▼───────────┐
                          │ 4 Synthesizer        │  laporan berjenjang
                          └──────────────────────┘

  Tool layer:  document_parser (PyMuPDF/pdfplumber) · python_sandbox (Pandas)
               retrieval (LlamaIndex + Chroma, fallback BM25) · web_search (Tavily)
```

### Alur graph

```
START → plan → ingest ─┬─► financial_auditor ─┐
                       │                      ├─► join ─┬─(verifikasi gagal)─► audit_retry ─┐
                       └─► market_intelligence┘         │                          ▲        │
                                                        │                          └────────┘
                                                        └─(selesai)─► investigate → synthesize → finalize → END
```

Agen 1 dan 2 melakukan *fan-out* dari `ingest` pada superstep yang sama sehingga
benar-benar berjalan paralel; `join` menunggu keduanya. Loop pembacaan ulang
berada seluruhnya di cabang audit agar pembacaan ulang yang lambat tidak pernah
mendahului cabang pasar masuk ke investigator.

---

## Prinsip yang ditegakkan secara struktural

**1. Pemisahan penalaran dan komputasi.**
Model memilih *baris mana pada halaman mana* yang memuat sebuah metrik; Python
membaca digitnya. Skema tool ekstraksi hanya menerima **penunjuk** —
`candidate_id` dan `number_index` — sehingga model secara teknis tidak punya
tempat untuk menuliskan angka. Model yang tidak pernah mengetik angka tidak
dapat berhalusinasi angka.

**2. Setiap klaim punya sumber.**
`core/state.py::Citation` hanya memiliki tiga bentuk sah — `document`
(wajib halaman + kutipan verbatim), `computation` (wajib formula), dan `web`
(wajib URL) — dan divalidasi Pydantic. Tidak ada jalur keempat, sehingga angka
tanpa asal-usul tidak dapat direpresentasikan.

**3. Ketidakpastian yang jujur.**
Angka yang tetap memecahkan identitas akuntansi setelah seluruh strategi
pembacaan habis ditandai `tidak_dapat_diekstraksi_dengan_andal`. Label itu
mengalir sampai ke laporan, dan pola deteksi yang bergantung padanya **tidak
dijalankan**.

**4. Jejak audit wajib.**
Setiap keputusan agen, tool yang dipanggil, hasilnya, dan alasan graph berpindah
tercatat di SQLite dengan timestamp — termasuk hipotesis yang digugurkan.

---

## Empat agen

### Agen 1 — Financial Auditor (`agents/financial_auditor.py`)

Mengekstraksi laporan posisi keuangan, laba rugi, dan arus kas, lalu
memverifikasinya dengan Pandas. Delapan identitas akuntansi diuji per periode:

| Identitas | Formula |
|---|---|
| `persamaan_neraca` | `total_aset = total_liabilitas + total_ekuitas` |
| `subtotal_aset` | `total_aset = aset_lancar + aset_tidak_lancar` |
| `subtotal_liabilitas` | `total_liabilitas = jangka_pendek + jangka_panjang` |
| `laba_bruto` | `laba_bruto = pendapatan − beban_pokok` |
| `laba_usaha` | `laba_usaha = laba_bruto − beban_usaha` |
| `laba_bersih` | `laba_bersih = laba_sebelum_pajak − beban_pajak` |
| `rekonsiliasi_arus_kas` | `kas_akhir = kas_awal + operasi + investasi + pendanaan` |
| `kas_neraca_vs_arus_kas` | `kas_neraca = kas_akhir_periode` |

Ketidakcocokan memicu pembacaan ulang **hanya pada baris yang terlibat**, dengan
strategi yang meningkat: `table_first → text_layout → line_anchored → pdfplumber`.

Hal-hal khas laporan Indonesia yang ditangani di lapisan ini:

- `1.234.567` dibaca 1234567, `(1.234)` dibaca −1234, `12,5` dibaca 12.5
- header *"dalam jutaan Rupiah"* dideteksi dan skala diterapkan sebelum
  perbandingan — membandingkan angka berskala dengan yang tidak adalah cara
  paling umum sebuah pembaca otomatis "menemukan" neraca tidak seimbang yang
  sebenarnya seimbang
- kolom referensi Catatan (`2c,4`) dikenali sebagai bukan nilai
- kaption terpanjang menang: `Jumlah aset lancar` tidak pernah terbaca sebagai
  `Jumlah aset`, dan `Jumlah liabilitas dan ekuitas` (yang memuat angka total
  aset) ditolak
- beban yang disajikan dalam kurung dicatat sebagai magnitudo, karena kurung
  menandai pengurangan, bukan beban bernilai negatif

### Agen 2 — Market Intelligence (`agents/market_intelligence.py`)

Berjalan paralel dengan Agen 1. Setiap sumber membawa **tier dan bobot
kredibilitas** (`idx.co.id`/`ojk.go.id` = 1.00; media kredibel ≈ 0.70–0.85;
forum ≈ 0.15). Klaim manajemen diekstraksi dalam bentuk yang **dapat diuji** —
menyimpan metrik penguji dan arah yang diklaim — sehingga Agen 3 dapat
menyilangkannya dengan angka terverifikasi. Sumber tier 3–4 tidak pernah menjadi
klaim; ia hanya konteks.

### Agen 3 — Red Flag Investigator (`agents/red_flag_investigator.py`)

Pembeda utama sistem. Tujuh pola deteksi berjalan atas angka terverifikasi:

| Pola | Sinyal | Ambang |
|---|---|---|
| `arus_kas_vs_laba` | AKO negatif, laba bersih positif | tanda berlawanan |
| `piutang_vs_pendapatan` | pertumbuhan piutang ≫ pendapatan | selisih ≥ 15 pp |
| `kualitas_laba_akrual` | rasio akrual terhadap total aset | > 10% |
| `leverage_solvabilitas` | DER / rasio lancar / lonjakan leverage | DER > 2, CR < 1 |
| `pihak_berelasi` | intensitas transaksi afiliasi di catatan | ≥ 3 bagian |
| `opini_auditor` | modifikasi opini, kelangsungan usaha | frasa spesifik |
| `klaim_vs_angka` | klaim manajemen vs angka terverifikasi | arah berlawanan |

Pola menghasilkan **hipotesis, bukan temuan**. Untuk setiap hipotesis, agen
merumuskan bukti apa yang akan **menggugurkannya**, mencarinya di bagian lain
dokumen (halaman asal hipotesis dikecualikan dari pencarian), lalu menilai:

- **digugurkan** — kutipan menjelaskan anomali secara memadai; hipotesis dibuang
  dan alasannya dicatat
- **melemah** — penjelasan sebagian; keyakinan diturunkan
- **bertahan** — tidak ada penjelasan; naik menjadi temuan

Dua hal berbeda dibedakan di sini, dan perbedaannya menggerakkan keyakinan ke
arah yang berlawanan. Bila pencarian **tidak menemukan kandidat sama sekali**,
keyakinan hanya dipertahankan, tidak dinaikkan — tidak pernah mencari bukan
konfirmasi. Bila kandidat **ditemukan lalu dinilai tidak menjelaskan apa pun**,
keyakinan naik 5% (dibatasi 0,95): bantahan yang diuji dan ditolak adalah bukti
yang lebih kuat daripada ketiadaan bantahan. Severitas diturunkan dari keyakinan
akhir ini, sehingga temuan yang sama dapat berpindah tingkat antara eksekusi
dengan dan tanpa model — perbedaan itu nyata dan disengaja, bukan
ketidakstabilan.

### Agen 4 — Synthesizer (`agents/synthesizer.py`)

Menyusun laporan berjenjang, memberi tingkat keyakinan, dan **menahan** temuan di
bawah ambang (`SENTINEL_MIN_CONFIDENCE`, default 0.55). Temuan yang ditahan tidak
dihapus — ia tetap muncul di lampiran teknis beserta alasan penahanannya,
sehingga keputusan kalibrasi dapat ditelaah.

Skor komposit **terbalik: makin tinggi makin aman** (High < 60 · Medium 60–84 ·
Low ≥ 85), dihitung sebagai `100 − Σ(bobot_pola × keyakinan × severitas)`.
Bila cakupan verifikasi terlalu rendah, skor ditandai `assessable: false` — sebuah
laporan yang angkanya tidak dapat diverifikasi bukan surat keterangan sehat.

---

## Struktur proyek

```
sentinel-ai/
├── agents/
│   ├── orchestrator.py          # LangGraph state machine
│   ├── financial_auditor.py     # Agen 1 — ekstraksi + loop verifikasi
│   ├── market_intelligence.py   # Agen 2 — Tavily + bobot kredibilitas
│   ├── red_flag_investigator.py # Agen 3 — deteksi + falsifikasi
│   └── synthesizer.py           # Agen 4 — laporan + keyakinan + supresi
├── tools/
│   ├── document_parser.py       # PyMuPDF, fallback pdfplumber
│   ├── python_sandbox.py        # verifikasi aritmetik deterministik (Pandas)
│   ├── retrieval.py             # LlamaIndex + Chroma, fallback BM25
│   ├── web_search.py            # Tavily + tier kredibilitas
│   └── demo_corpus.py           # generator dokumen demo (entitas fiktif)
├── core/
│   ├── state.py                 # skema state LangGraph + kosakata metrik
│   ├── config.py                # konfigurasi + tier kredibilitas
│   ├── llm.py                   # lapisan abstraksi model
│   ├── formatting.py            # format angka Indonesia
│   ├── audit_trail.py           # pencatatan keputusan (SQLite)
│   └── report_generator.py      # kompilasi laporan berjenjang
├── scheduler/monitor.py         # APScheduler — pemicu otonom
├── frontend/app.py              # dashboard Streamlit
├── tests/
│   ├── test_verification.py     # 58 uji lapisan deterministik
│   └── test_pipeline.py         # 15 uji integrasi pipeline + graph
├── data/                        # dokumen, indeks, laporan, SQLite
└── main.py                      # CLI
```

Tiga berkas berada di luar daftar awal dan ditambahkan karena arsitekturnya
menuntutnya: `core/llm.py` (lapisan abstraksi penyedia model yang disebut pada
tabel mitigasi risiko laporan teknis), `core/formatting.py`, dan
`tools/demo_corpus.py` (agar demo dapat berjalan tanpa jaringan).

---

## CLI

```bash
python main.py demo                                  # pipeline lengkap + jejak audit langsung
python main.py demo --rebuild                        # buat ulang dokumen demo
python main.py analyze GOTO --pdf laporan.pdf \
       --name "PT GoTo Gojek Tokopedia Tbk" --sector teknologi
python main.py runs                                  # riwayat siklus
python main.py trail <run_id>                        # putar ulang jejak audit
python main.py monitor --once                        # satu sapuan otonom
python main.py monitor                               # pemantauan berkelanjutan
```

## Pemantauan otonom

```bash
python -m scheduler.monitor --tickers ABCI,GOTO --doc-dir data/documents
```

Dua pemicu berjalan: sapuan terjadwal (`SENTINEL_MONITOR_CRON`, default 03:00)
dan deteksi dokumen baru tiap 15 menit. Setiap entitas disidik dengan hash isi
berkas, sehingga siklus hanya berjalan ketika dokumen benar-benar berubah —
menganalisis ulang laporan yang tidak berubah hanya menghabiskan token untuk
laporan yang identik.

---

## Demo untuk kompetisi

`python main.py demo` menjalankan pipeline atas **PT ABC Indonesia Tbk (ABCI)**,
sebuah **entitas fiktif** yang dibangkitkan `tools/demo_corpus.py` dalam
konvensi tata letak laporan BEI. Entitas dibuat fiktif secara sengaja:
membangkitkan laporan keuangan atas nama emiten sungguhan lalu menanam indikator
risiko di dalamnya akan menghasilkan dokumen yang terbaca sebagai asli dan
mencemarkan nama perusahaan yang nyata.

Seluruh identitas akuntansi pada dokumen demo konsisten, sehingga red flag yang
muncul adalah sinyal ekonomi yang nyata, bukan kesalahan pembacaan:

- arus kas operasi negatif (−Rp145 miliar) meski laba bersih positif (Rp214 miliar)
- piutang tumbuh 39,0% terhadap pendapatan yang tumbuh 9,7%
- rasio lancar 0,96x dan DER 2,17x
- paragraf penekanan suatu hal dan kelangsungan usaha pada opini auditor

Penjelasan parsial atas lonjakan piutang sengaja ditanam pada Catatan 5 — di
halaman yang berbeda — agar langkah pencarian bukti penyangkal punya sesuatu yang
nyata untuk ditemukan.

Untuk demo atas emiten sungguhan, unduh laporan tahunannya dari
[idx.co.id](https://www.idx.co.id) dan jalankan `python main.py analyze <TICKER> --pdf <berkas>`.

**Varian tidak konsisten** untuk mendemonstrasikan loop pembacaan ulang:

```python
from tools.demo_corpus import build_demo_pdf
build_demo_pdf(overwrite=True, corrupt_balance=True)
```

Sistem akan mencoba tiga strategi pembacaan, gagal merekonsiliasi baris ekuitas,
lalu melaporkannya sebagai `tidak dapat diekstraksi dengan andal` alih-alih
menebak.

---

## Konfigurasi

| Variabel | Default | Keterangan |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Tanpa ini sistem berjalan deterministik |
| `SENTINEL_MODEL_PRIMARY` | `claude-sonnet-4-6` | Penalaran: ekstraksi, falsifikasi, sintesis |
| `SENTINEL_MODEL_LIGHT` | `claude-haiku-4-5` | Klasifikasi murah (strategi model berjenjang) |
| `TAVILY_API_KEY` | — | Tanpa ini Agen 2 memakai fixture / dilewati |
| `SENTINEL_OFFLINE` | `0` | `1` menonaktifkan seluruh panggilan jaringan |
| `SENTINEL_VERIFY_REL_TOLERANCE` | `0.005` | Toleransi relatif identitas akuntansi |
| `SENTINEL_MAX_EXTRACTION_ATTEMPTS` | `3` | Batas pembacaan ulang |
| `SENTINEL_MIN_CONFIDENCE` | `0.55` | Ambang temuan naik ke ringkasan eksekutif |
| `SENTINEL_MONITOR_CRON` | `0 3 * * *` | Jadwal sapuan otonom |
| `SENTINEL_WATCHLIST` | `GOTO,BUMI,TLKM` | Entitas yang dipantau |

---

## Pengujian

```bash
pytest -q                                  # 73 pengujian
pytest tests/test_verification.py -q       # lapisan deterministik
pytest tests/test_pipeline.py -q           # pipeline + graph + falsifikasi
```

Pengujian integrasi menggunakan **stub LLM**, bukan panggilan jaringan. Itu bukan
sekadar soal CI: stub hanya bisa mengembalikan *penunjuk* baris dan kolom, dan
asersinya memastikan angka yang dihasilkan memang berasal dari dokumen. Stub yang
mencoba mengembalikan angka tidak punya tempat untuk menaruhnya — kontrak
arsitekturnya diuji, bukan hanya dijelaskan.

---

## Deployment

**Hugging Face Spaces** (Streamlit SDK): unggah repositori, atur
`ANTHROPIC_API_KEY` dan `TAVILY_API_KEY` sebagai *Repository secrets*, entry
point `frontend/app.py`.

**Railway**: `pip install -r requirements.txt` lalu
`streamlit run frontend/app.py --server.port $PORT --server.address 0.0.0.0`.
Untuk pemantauan otonom, jalankan `python -m scheduler.monitor` sebagai proses
kedua.

Jalur peningkatan yang sudah disiapkan di kode: SQLite → PostgreSQL (skema
sengaja polos), Chroma → pgvector (hanya kelas *vector store* yang berubah), dan
penyedia LLM lain melalui `core/llm.py` tanpa menyentuh logika agen.

---

## Batasan yang diketahui

- Kosakata metrik (`core/state.py::METRIC_VOCABULARY`) mencakup pos utama
  laporan keuangan; emiten sektor keuangan memakai kaption berbeda dan
  memerlukan penambahan alias.
- Backend retrieval turun ke BM25 bila model embedding tidak tersedia. Kaption
  laporan keuangan Indonesia sangat terstandardisasi sehingga penurunannya kecil,
  tetapi pencarian parafrase melemah; backend yang dipakai dicatat di jejak audit.
- Laporan hasil pindai (tanpa lapisan teks) belum didukung; OCR belum masuk
  pipeline.
- Ambang pola dikalibrasi terhadap konvensi penyaringan umum, bukan terhadap
  basis data temuan historis. Kalibrasi berbasis data adalah pekerjaan Tahap 2
  pada peta jalan.

---

## Sumber

Rujukan kuantitatif, studi kasus (PT Garuda Indonesia Tbk, PT Hanson
International Tbk), dan pilihan teknologi tercantum pada
`files/SENTINEL-AI-LAST-REPORT.pdf`.
