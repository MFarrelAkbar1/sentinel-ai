# Sentinel-AI — Entitas MVP (Sektor Properti & Real Estate)

Status: **draf**, perlu diverifikasi tim sebelum data-scraper mulai unduh massal.
Sumber: klasifikasi IDX-IC sektor H111 (Real Estate Development & Management), 92 emiten
total. Daftar mentahnya ditarik dari sumber pihak ketiga (bukan dicek langsung ke
idx.co.id), jadi **wajib disilangkan ke idx.co.id sebelum difinalkan** — komposisi sektor
bisa berubah karena delisting/relisting/IPO baru.

---

## Draf 18 entitas

### Kapitalisasi besar — baseline/kalibrasi (4)
Tujuan: menunjukkan sistem tidak menimbulkan alarm palsu pada emiten yang tata kelolanya
baik. Kalau salah satu dari empat ini keluar skor "High Risk", itu justru sinyal untuk cek
ulang ambang deteksi sebelum demo, bukan cek ulang perusahaannya.

| Ticker | Nama |
|---|---|
| BSDE | Bumi Serpong Damai Tbk |
| CTRA | Ciputra Development Tbk |
| PWON | Pakuwon Jati Tbk |
| SMRA | Summarecon Agung Tbk |

### Kecil-menengah — fokus utama demo (14)
Ini yang menjawab langsung §2.1 poin d proposal ("emiten kecil tidak diliput siapa pun").

| Ticker | Nama |
|---|---|
| MTLA | Metropolitan Land Tbk |
| BEST | Bekasi Fajar Industrial Estate Tbk |
| KIJA | Kawasan Industri Jababeka Tbk |
| JRPT | Jaya Real Property Tbk |
| DMAS | Puradelta Lestari Tbk |
| DUTI | Duta Pertiwi Tbk |
| GPRA | Perdana Gapura Prima Tbk |
| EMDE | Megapolitan Developments Tbk |
| GMTD | Gowa Makassar Tourism Development Tbk |
| MDLN | Modernland Realty Tbk |
| BKSL | Sentul City Tbk |
| DART | Duta Anggada Realty Tbk |
| ELTY | Bakrieland Development Tbk |
| PUDP | Pudjiadi Prestige Tbk |

---

## Kenapa yang ini, bukan yang lain

Dipilih dari 92 kandidat berdasarkan kemungkinan laporan tahunan 3 tahun terakhir
tersedia dan gampang diunduh (perusahaan cukup mapan untuk rutin publikasi), bukan
berdasarkan asumsi kondisi keuangan mereka saat ini — itu justru yang harus dibiarkan
sistem yang menemukan sendiri dari dokumen asli, bukan ditentukan di muka oleh saya atau
tim. Beberapa nama di daftar kecil-menengah (mis. DART, ELTY, BKSL) punya sejarah publik
terkait leverage/tata kelola di masa lalu — itu alasan bagus untuk dimasukkan sebagai
**kandidat pengujian**, bukan kesimpulan yang sudah ditentukan sebelumnya.

Nama-nama sangat kecil/tipis likuiditasnya dari 92 kandidat (mis. ARMY, BBSS, KBAG, KOCI,
NZIA, dll.) sengaja tidak dimasukkan draf awal — risiko laporan tahunannya tidak lengkap 3
tahun atau susah diakses lebih tinggi, dan itu blocker yang mahal ditemukan telat.

---

## Langkah verifikasi sebelum dikunci (tugas data-scraper, P0)

- [ ] Cek ulang ke idx.co.id: apakah 18 ticker ini masih tercatat aktif dan masih
      diklasifikasikan sektor Properti & Real Estate per Agustus 2026
- [ ] Cek ketersediaan laporan tahunan 3 tahun terakhir (2023–2025) untuk tiap ticker —
      kalau ada yang bolong, ganti dengan kandidat lain dari daftar 92
- [ ] Kalau ada yang delisting/suspend, coret dan gantikan
- [ ] Setelah 15–20 final terkonfirmasi, update `SENTINEL_WATCHLIST` di `.env` (saat ini
      masih default `GOTO,BUMI,TLKM` — bukan sektor properti sama sekali, perlu diganti)

## Perubahan konfigurasi terkait

`core/state.py::METRIC_VOCABULARY` dan `agents/orchestrator.py::SECTOR_PRIORITIES["properti"]`
sudah ada, tidak perlu dibuat baru — tinggal dipastikan alias kaption laporan keuangan
properti (mis. "persediaan real estat", "tanah untuk pengembangan", "uang muka penjualan")
sudah tercakup saat data-scraper mulai uji parsing dokumen asli.
