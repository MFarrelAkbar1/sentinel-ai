# Skrip Video Demo — Sentinel-AI

Durasi target **5:00** (batas maksimum panitia). Format: PPT singkat di awal, sisanya demo
sistem yang benar-benar berjalan.

---

## Fakta yang menentukan bentuk skrip ini

Sebelum menyusun apa pun, dua angka ini harus dipahami — keduanya diukur dari eksekusi nyata:

| Yang dijalankan | Durasi | Konsekuensi untuk video |
|---|---|---|
| `python main.py demo` **dengan API key** | **179 detik** | ❌ Mustahil dijalankan live. Ini memakan 3 dari 5 menit yang kamu punya. |
| `python main.py demo` **offline** (deterministik) | **1 detik** | ✅ Aman dijalankan live |
| `python main.py analyze` dokumen tidak konsisten, offline | **1 detik** | ✅ Aman dijalankan live — termasuk loop 3× baca ulang |
| `python main.py trail <run_id>` | **instan** | ✅ Senjata utama video ini |

**Strateginya:** jalankan siklus LLM **sebelum** merekam. Di dalam video, putar ulang jejak
keputusannya dengan `trail` — instan, dan tetap merupakan catatan run asli, bukan simulasi.
Yang direkam live hanyalah bagian yang memang cepat.

> Jangan pernah merekam layar sambil menunggu 3 menit. Juri tidak menilai kesabaranmu, dan
> setiap detik menunggu adalah detik yang dicuri dari penjelasan.

---

## Peta waktu

| Waktu | Durasi | Segmen | Layar |
|---|---|---|---|
| 0:00–0:22 | 22 dtk | Masalah | PPT 1 |
| 0:22–0:45 | 23 dtk | Mengapa multi-agen | PPT 2 |
| 0:45–1:05 | 20 dtk | Arsitektur | PPT 3 |
| 1:05–1:23 | 18 dtk | Demo dimulai — konfigurasi terbuka | Dashboard |
| 1:23–1:52 | 29 dtk | Siklus berjalan + verifikasi aritmetik | Rekaman dipercepat |
| **1:52–2:45** | **53 dtk** | **Falsifikasi — hipotesis digugurkan** | Terminal `trail` |
| 2:45–3:12 | 27 dtk | Hasil: skor, temuan, pertanyaan | Dashboard |
| 3:12–3:35 | 23 dtk | Sitasi — setiap angka punya halaman | Lampiran teknis |
| 3:35–4:14 | 39 dtk | Koreksi diri saat pembacaan gagal | Terminal, **live** |
| 4:14–4:38 | 24 dtk | Otonomi — berjalan tanpa diperintah | Terminal, **live** |
| 4:38–5:00 | 22 dtk | Penutup + batasan | PPT 4 |

Naskah di bawah berjumlah **689 kata** — 4 menit 36 detik pada 150 kata per menit. Setiap
segmen sudah dicocokkan terhadap jatahnya sendiri, tidak hanya totalnya. Sisa **24 detik**
adalah jeda: waktu diam saat menunjuk layar. Jangan diisi.

Segmen falsifikasi mendapat jatah terbesar. Itu disengaja: seluruh sistem ini dibangun
untuk momen tersebut, dan itulah satu-satunya bagian yang tidak dimiliki peserta lain.

---

## Slide PPT (hanya 4 — total 65 detik)

| # | Isi | Gambar |
|---|---|---|
| 1 | Judul + masalah: ratusan halaman, puluhan jam, satu entitas | `logo-sentinel.png` |
| 2 | Mengapa multi-agen, bukan satu model | `mengapa-multiagent.png` |
| 3 | Arsitektur enam lapis | `arsitektursentinel.png` |
| 4 | Penutup: berjalan hari ini + batasan + disclaimer | *tanpa gambar* |

Jangan menambah slide. Setiap slide tambahan mengambil waktu dari demo, dan yang dinilai
adalah agennya yang berjalan — bukan deknya.

---

## Naskah

### 0:00–0:22 · Masalah — PPT 1

> Satu laporan tahunan emiten Bursa Efek Indonesia berisi ratusan halaman. Untuk menilainya,
> seorang analis butuh puluhan jam — untuk **satu** perusahaan.
>
> Kasus Garuda Indonesia dan Hanson International menunjukkan hal penting: indikatornya sudah
> ada di laporan yang dipublikasikan. Yang langka bukan datanya — melainkan kapasitas
> membacanya secara konsisten.

### 0:22–0:45 · Mengapa multi-agen — PPT 2

> Sentinel-AI menjalankan siklus itu tanpa perlu diperintah. Tapi kami tidak memakai satu
> model untuk semuanya, dan itu keputusan yang sengaja.
>
> Kalau satu model menangani ekstraksi sampai pelaporan, kesalahan di tahap awal terbawa
> sampai akhir tanpa ada yang memeriksanya. Di Sentinel-AI ada empat agen dengan tanggung
> jawab terpisah — dan keluaran satu agen menjadi masukan yang **diverifikasi** agen
> berikutnya.

### 0:45–1:05 · Arsitektur — PPT 3

> Ini arsitekturnya. Yang paling menentukan ada di lapis kelima: **penalaran dan komputasi
> dipisah**. Model hanya memilih baris mana yang memuat sebuah angka. Yang membaca digitnya
> adalah Python.
>
> Skema tool-nya hanya menerima penunjuk baris — model tidak punya tempat untuk mengetik
> angka. Model yang tidak pernah mengetik angka tidak bisa berhalusinasi angka.

### 1:05–1:23 · Demo dimulai — Dashboard

**Layar:** dashboard di layar awal, sorot panel **Status sistem** di kiri.

> Kita mulai. Perhatikan panel kiri: sistem menyatakan terbuka model apa yang aktif, apakah
> pencarian web tersedia, dan ambang keyakinan yang dipakai. Pengguna selalu tahu dalam
> kondisi apa laporan ini dihasilkan.
>
> Dokumennya PT ABC Indonesia — entitas fiktif, sengaja, agar kami tidak menanam indikator
> risiko atas nama perusahaan nyata.

### 1:23–1:52 · Siklus berjalan — rekaman dipercepat 8×

**Layar:** rekaman terminal saat siklus asli berjalan, dipercepat. Beri label di sudut:
`dipercepat 8× · durasi asli 2 menit 59 detik`.

> Siklus penuh berjalan sekitar tiga menit — ini dipercepat delapan kali.
>
> Agen 1 dan Agen 2 berjalan **paralel**: satu membaca laporan keuangan, satu mengumpulkan
> berita. Dan di sinilah lapisan yang menentukan — **enam belas dari enam belas identitas
> akuntansi diverifikasi dengan Python**. Bukan ditanyakan ke model. Dihitung.

### 1:52–2:45 · Falsifikasi — inti video

**Layar:** terminal, jalankan `python main.py trail <run_id>`. Gulir ke langkah 028–036.
Perbesar font. Diam sejenak di baris `H2: digugurkan`.

> Sekarang bagian yang paling penting.
>
> Agen 3 menjalankan tujuh pola deteksi dan menemukan empat dugaan. Sistem lain akan berhenti
> di sini dan melaporkan keempatnya sebagai temuan.
>
> Sentinel-AI tidak. Untuk setiap dugaan, agen merumuskan bukti apa yang akan
> **membatalkannya**, lalu mencarinya di bagian **lain** dokumen yang sama — dengan halaman
> asal dugaan sengaja dikecualikan.
>
> Lihat hipotesis H2. Piutang tumbuh tiga puluh sembilan persen, pendapatan hanya sembilan
> koma tujuh. Mencurigakan. Agen menyusun kueri sendiri, menarik enam kutipan, dan
> menemukan penjelasannya: perubahan syarat pembayaran dari enam puluh ke sembilan puluh
> hari untuk pelanggan institusional baru.
>
> **Hipotesisnya digugurkan. Oleh agen itu sendiri.**
>
> Dari empat dugaan, dua tidak sampai ke ringkasan eksekutif — satu dibuang, satu melemah di
> bawah ambang. Sistem yang tidak pernah membuang dugaannya sendiri bukan sedang menyaring;
> ia hanya memindahkan beban verifikasi kembali ke manusia.

### 2:45–3:12 · Hasil — Dashboard

**Layar:** panel skor komposit dan daftar temuan.

> Hasilnya: skor tujuh puluh delapan koma tujuh dari seratus. Skornya terbalik — makin tinggi
> makin aman.
>
> Perhatikan tiga angka di bawah gauge: pola dijalankan, temuan dipertahankan, dan
> **hipotesis digugurkan**. Angka ketiga itu yang membuat dua angka pertama layak dipercaya.
>
> Dan setiap temuan tidak berhenti sebagai peringatan — ia diakhiri dengan **pertanyaan
> spesifik yang bisa langsung diajukan ke manajemen**.

### 3:12–3:35 · Sitasi — Lampiran teknis

**Layar:** tab **Angka terekstraksi** di lampiran teknis. Sorot kolom `Hal.` dan
`Baris sumber`.

> Setiap angka bisa ditelusuri: periode, status verifikasi, **halaman asalnya**, strategi
> pembacaan yang berhasil, dan baris sumber persis seperti tertulis di dokumen.
>
> Perhatikan baris berstatus `belum_diverifikasi`. Sistem tidak menyembunyikan angka yang
> tidak masuk ke dalam identitas akuntansi mana pun. Yang tidak bisa diverifikasi, dinyatakan
> demikian.

### 3:35–4:14 · Koreksi diri — LIVE

**Layar:** terminal. Jalankan langsung — hanya butuh 1 detik:

```bash
python main.py analyze ABCI --pdf data/documents/ABCI_Laporan_Tahunan_2025_DEMO_TIDAK_KONSISTEN.pdf --name "PT ABC Indonesia Tbk" --sector konsumer
```

> Lalu apa yang terjadi kalau pembacaannya gagal? Ini dokumen yang sengaja dibuat tidak
> konsisten — neracanya tidak seimbang.
>
> Percobaan satu, strategi `table_first`: neraca meleset empat ratus tujuh puluh miliar.
> Agen memutuskan membaca ulang — dan perhatikan, **hanya tiga metrik yang bermasalah**, bukan
> seluruh dokumen. Percobaan dua, strategi berbeda: masih gagal. Percobaan tiga: masih gagal.
>
> Dan inilah yang dilakukan sistem: ketiga angka itu **ditandai tidak dapat diekstraksi dengan
> andal**. Pola deteksi yang bergantung padanya **tidak dijalankan**, dan laporan menyatakan
> itu secara eksplisit.
>
> Sistem ini memilih diam daripada menebak.

### 4:14–4:38 · Otonomi — LIVE

**Layar:** terminal, `python main.py monitor --once`.

> Terakhir: tidak ada yang mengetik apa pun untuk memulai semua ini.
>
> Dua pemicu berjalan sendiri — sapuan terjadwal setiap pukul tiga pagi, dan deteksi dokumen
> baru setiap lima belas menit. Setiap berkas disidik dengan hash isinya, jadi siklus hanya
> berjalan kalau dokumennya benar-benar berubah.
>
> Menganalisis ulang laporan yang tidak berubah hanya menghabiskan token untuk laporan yang
> identik.

### 4:38–5:00 · Penutup — PPT 4

> Sentinel-AI berjalan hari ini: empat agen, tujuh puluh tiga pengujian lulus, siklus penuh
> dari dokumen sampai laporan bersitasi.
>
> Yang belum: laporan hasil pindai, dan kalibrasi ambang terhadap data historis. Kami
> menyebutkannya karena sistem yang menyembunyikan batasnya tidak bisa dipercaya soal apa pun.
>
> Sentinel-AI adalah alat penyaringan risiko, **bukan penasihat investasi**. Ia menyiapkan
> pertanyaan yang tepat. Keputusannya tetap milik manusia.

---

## Persiapan sebelum merekam

### H-1

- [ ] Jalankan `python main.py demo` **dengan API key aktif**. Rekam layarnya untuk dipakai
      di segmen 1:23 (dipercepat 8×). Catat `run_id` yang keluar.
- [ ] Verifikasi hasilnya bagus: **hipotesis gugur harus ≥ 1**. Kalau 0, ulangi — tanpa itu
      segmen inti video hilang.
- [ ] Bangun dokumen varian tidak konsisten:
      ```python
      from tools.demo_corpus import build_demo_pdf
      build_demo_pdf(overwrite=True, corrupt_balance=True)
      ```
- [ ] Jalankan `python main.py runs` — pastikan riwayat berisi beberapa siklus supaya grafik
      **Lintasan skor risiko** di dashboard tidak kosong.
- [ ] Uji `python main.py trail <run_id>` dan catat nomor langkah falsifikasi (di run acuan:
      **028–036**).

### Sesaat sebelum rekam

- [ ] Font terminal diperbesar — minimal 16pt. Jejak audit tidak berguna kalau tidak terbaca.
- [ ] Terminal dimaksimalkan, dashboard di tab browser terpisah yang **sudah** memuat hasil.
- [ ] Rekam 1920×1080. Matikan notifikasi.
- [ ] Bersihkan layar dari baris berisik yang bukan milik Sentinel — peringatan `HF_TOKEN`
      dan progress bar `Loading weights` dari HuggingFace muncul saat indeks dibangun.

---

## Rencana cadangan

| Kalau ini gagal | Lakukan ini |
|---|---|
| API key habis kuota saat H-1 | Pakai jalur deterministik untuk seluruh video. Segmen falsifikasi diganti dengan `trail` atas run lama yang sudah berhasil — jejaknya tersimpan permanen di SQLite. |
| Run LLM menghasilkan 0 hipotesis gugur | Ulangi run. Kalau tetap 0, pakai `trail 6403d85c277b` — run yang terdokumentasi di Bagian 8 dokumentasi. |
| Demo live gagal saat rekaman | Kedua perintah live hanya butuh 1 detik. Ulangi take, jangan dipaksakan. |
| Video melebihi 5 menit | Potong segmen otonomi (4:14–4:38) lebih dulu, lalu segmen sitasi. **Jangan pernah** memotong segmen falsifikasi. |

---

## Prinsip penyuntingan

1. **Falsifikasi adalah videonya.** Kalau juri hanya mengingat satu hal, itu harus momen
   agen membuang hipotesisnya sendiri. Beri jeda di situ. Jangan buru-buru.
2. **Tunjukkan, jangan klaim.** Setiap pernyataan dalam naskah punya sesuatu di layar yang
   membuktikannya pada saat yang sama.
3. **Batasan disebut, bukan disembunyikan.** Ini menaikkan kredibilitas, dan mendahului
   pertanyaan juri yang pasti datang.
4. **Jangan pernah mempercepat sesuatu tanpa memberi label.** Rekaman yang dipercepat harus
   diberi keterangan durasi aslinya. Kredibilitas seluruh video bergantung pada juri percaya
   bahwa yang mereka lihat benar-benar berjalan.
