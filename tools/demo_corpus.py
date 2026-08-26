"""Synthetic demo filing generator.

The competition demo has to run in a room with unreliable Wi-Fi and cannot
depend on a live BEI download. This module builds a small annual report in the
layout conventions of an Indonesian filing — ``dalam jutaan Rupiah`` headers,
note-reference columns, parenthesised negatives, comparative year columns — so
every stage of the pipeline exercises the same code paths it would on a real
document.

The entity is **PT ABC Indonesia Tbk (ABCI)** and it is fictional, deliberately.
Fabricating a filing for a real listed company and planting risk indicators in
it would produce a document that reads as genuine and defames a real issuer.
For a demo against a real emiten, download that company's actual annual report
from idx.co.id and pass the path to ``run_analysis``.

The planted signals are internally consistent — every accounting identity holds,
so the verification layer passes and the red flags are genuine economic signals
rather than arithmetic breaks:

* operating cash flow negative while net income is positive (FY2025)
* receivables growing ~39% against ~10% revenue growth
* leverage and short-term liquidity deteriorating
* an emphasis-of-matter paragraph in the auditor's report
* a related-party note dense enough to register

A partial explanation for the receivables jump is planted in the notes, on a
different page, so Agent 3's counter-evidence search has something real to find.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None  # type: ignore

DEMO_TICKER = "ABCI"
DEMO_COMPANY = "PT ABC Indonesia Tbk"
DEMO_SECTOR = "konsumer"

#: All figures in millions of Rupiah, matching the "dalam jutaan Rupiah" header.
FY2025: dict[str, float] = {
    "kas_dan_setara_kas": 182_000, "piutang_usaha": 1_640_000, "persediaan": 690_000,
    "aset_lancar": 2_900_000, "aset_tidak_lancar": 3_500_000, "total_aset": 6_400_000,
    "liabilitas_jangka_pendek": 3_010_000, "liabilitas_jangka_panjang": 1_370_000,
    "total_liabilitas": 4_380_000, "total_ekuitas": 2_020_000,
    "pendapatan": 4_850_000, "beban_pokok_pendapatan": 3_640_000, "laba_bruto": 1_210_000,
    "beban_usaha": 780_000, "laba_usaha": 430_000, "laba_sebelum_pajak": 290_000,
    "beban_pajak": 76_000, "laba_bersih": 214_000,
    "arus_kas_operasi": -145_000, "arus_kas_investasi": -260_000, "arus_kas_pendanaan": 425_000,
    "kas_awal_periode": 162_000, "kas_akhir_periode": 182_000,
}

FY2024: dict[str, float] = {
    "kas_dan_setara_kas": 162_000, "piutang_usaha": 1_180_000, "persediaan": 610_000,
    "aset_lancar": 2_090_000, "aset_tidak_lancar": 3_020_000, "total_aset": 5_110_000,
    "liabilitas_jangka_pendek": 1_910_000, "liabilitas_jangka_panjang": 1_360_000,
    "total_liabilitas": 3_270_000, "total_ekuitas": 1_840_000,
    "pendapatan": 4_420_000, "beban_pokok_pendapatan": 3_310_000, "laba_bruto": 1_110_000,
    "beban_usaha": 720_000, "laba_usaha": 390_000, "laba_sebelum_pajak": 268_000,
    "beban_pajak": 71_000, "laba_bersih": 197_000,
    "arus_kas_operasi": 96_000, "arus_kas_investasi": -210_000, "arus_kas_pendanaan": 190_000,
    "kas_awal_periode": 86_000, "kas_akhir_periode": 162_000,
}


def _rp(value: float) -> str:
    """Format as an Indonesian statement figure: dots as thousands, () negative."""
    formatted = f"{abs(value):,.0f}".replace(",", ".")
    return f"({formatted})" if value < 0 else formatted


def _row(caption: str, note: str, current: float, prior: float) -> str:
    return f"{caption:<48}{note:>8}{_rp(current):>18}{_rp(prior):>18}"


def _page_balance_sheet() -> str:
    a, b = FY2025, FY2024
    return "\n".join([
        f"{DEMO_COMPANY} DAN ENTITAS ANAK",
        "LAPORAN POSISI KEUANGAN KONSOLIDASIAN",
        "Per 31 Desember 2025 dan 2024",
        "(Dinyatakan dalam jutaan Rupiah, kecuali dinyatakan lain)",
        "",
        f"{'':<48}{'Catatan':>8}{'2025':>18}{'2024':>18}",
        "",
        "ASET",
        "ASET LANCAR",
        _row("Kas dan setara kas", "2c,4", a["kas_dan_setara_kas"], b["kas_dan_setara_kas"]),
        _row("Piutang usaha - neto", "2d,5", a["piutang_usaha"], b["piutang_usaha"]),
        _row("Persediaan", "2e,6", a["persediaan"], b["persediaan"]),
        _row("Uang muka dan biaya dibayar di muka", "7", 388_000, 138_000),
        _row("Jumlah aset lancar", "", a["aset_lancar"], b["aset_lancar"]),
        "",
        "ASET TIDAK LANCAR",
        _row("Aset tetap - neto", "2f,8", 2_910_000, 2_540_000),
        _row("Aset takberwujud", "9", 340_000, 300_000),
        _row("Aset pajak tangguhan", "10", 250_000, 180_000),
        _row("Jumlah aset tidak lancar", "", a["aset_tidak_lancar"], b["aset_tidak_lancar"]),
        "",
        _row("JUMLAH ASET", "", a["total_aset"], b["total_aset"]),
        "",
        "LIABILITAS DAN EKUITAS",
        "LIABILITAS JANGKA PENDEK",
        _row("Utang usaha", "11", 1_180_000, 940_000),
        _row("Pinjaman bank jangka pendek", "12", 1_520_000, 730_000),
        _row("Beban akrual dan liabilitas lain", "13", 310_000, 240_000),
        _row("Jumlah liabilitas jangka pendek", "", a["liabilitas_jangka_pendek"], b["liabilitas_jangka_pendek"]),
        "",
        "LIABILITAS JANGKA PANJANG",
        _row("Pinjaman bank jangka panjang", "12", 1_150_000, 1_170_000),
        _row("Liabilitas imbalan kerja", "14", 220_000, 190_000),
        _row("Jumlah liabilitas jangka panjang", "", a["liabilitas_jangka_panjang"], b["liabilitas_jangka_panjang"]),
        "",
        _row("JUMLAH LIABILITAS", "", a["total_liabilitas"], b["total_liabilitas"]),
        "",
        "EKUITAS",
        _row("Modal saham", "15", 900_000, 900_000),
        _row("Tambahan modal disetor", "15", 310_000, 310_000),
        _row("Saldo laba", "", 810_000, 630_000),
        _row("JUMLAH EKUITAS", "", a["total_ekuitas"], b["total_ekuitas"]),
        "",
        _row("JUMLAH LIABILITAS DAN EKUITAS", "", a["total_aset"], b["total_aset"]),
    ])


def _page_income_statement() -> str:
    a, b = FY2025, FY2024
    return "\n".join([
        f"{DEMO_COMPANY} DAN ENTITAS ANAK",
        "LAPORAN LABA RUGI DAN PENGHASILAN KOMPREHENSIF LAIN KONSOLIDASIAN",
        "Untuk tahun-tahun yang berakhir 31 Desember 2025 dan 2024",
        "(Dinyatakan dalam jutaan Rupiah, kecuali laba per saham)",
        "",
        f"{'':<48}{'Catatan':>8}{'2025':>18}{'2024':>18}",
        "",
        _row("Pendapatan", "2i,16", a["pendapatan"], b["pendapatan"]),
        _row("Beban pokok pendapatan", "17", -a["beban_pokok_pendapatan"], -b["beban_pokok_pendapatan"]),
        _row("Laba bruto", "", a["laba_bruto"], b["laba_bruto"]),
        "",
        _row("Beban penjualan", "18", -430_000, -395_000),
        _row("Beban umum dan administrasi", "19", -350_000, -325_000),
        _row("Jumlah beban usaha", "", -a["beban_usaha"], -b["beban_usaha"]),
        _row("Laba usaha", "", a["laba_usaha"], b["laba_usaha"]),
        "",
        _row("Beban keuangan", "20", -168_000, -134_000),
        _row("Penghasilan lain-lain - neto", "21", 28_000, 12_000),
        _row("Laba sebelum pajak penghasilan", "", a["laba_sebelum_pajak"], b["laba_sebelum_pajak"]),
        _row("Beban pajak penghasilan", "22", -a["beban_pajak"], -b["beban_pajak"]),
        _row("Laba tahun berjalan", "", a["laba_bersih"], b["laba_bersih"]),
        "",
        _row("Penghasilan komprehensif lain", "", 0, 0),
        _row("Jumlah laba komprehensif tahun berjalan", "", a["laba_bersih"], b["laba_bersih"]),
        "",
        "Laba per saham dasar (Rupiah penuh)                                      23,78             21,89",
    ])


def _page_cash_flow() -> str:
    a, b = FY2025, FY2024
    return "\n".join([
        f"{DEMO_COMPANY} DAN ENTITAS ANAK",
        "LAPORAN ARUS KAS KONSOLIDASIAN",
        "Untuk tahun-tahun yang berakhir 31 Desember 2025 dan 2024",
        "(Dinyatakan dalam jutaan Rupiah)",
        "",
        f"{'':<48}{'Catatan':>8}{'2025':>18}{'2024':>18}",
        "",
        "ARUS KAS DARI AKTIVITAS OPERASI",
        _row("Penerimaan dari pelanggan", "", 4_390_000, 4_355_000),
        _row("Pembayaran kepada pemasok dan karyawan", "", -4_240_000, -4_045_000),
        _row("Pembayaran beban keuangan", "", -168_000, -134_000),
        _row("Pembayaran pajak penghasilan", "", -127_000, -80_000),
        _row("Kas neto diperoleh dari aktivitas operasi", "", a["arus_kas_operasi"], b["arus_kas_operasi"]),
        "",
        "ARUS KAS DARI AKTIVITAS INVESTASI",
        _row("Perolehan aset tetap", "8", -245_000, -196_000),
        _row("Perolehan aset takberwujud", "9", -15_000, -14_000),
        _row("Kas neto digunakan untuk aktivitas investasi", "", a["arus_kas_investasi"], b["arus_kas_investasi"]),
        "",
        "ARUS KAS DARI AKTIVITAS PENDANAAN",
        _row("Penerimaan pinjaman bank", "12", 890_000, 420_000),
        _row("Pembayaran pinjaman bank", "12", -431_000, -196_000),
        _row("Pembayaran dividen", "15", -34_000, -34_000),
        _row("Kas neto diperoleh dari aktivitas pendanaan", "", a["arus_kas_pendanaan"], b["arus_kas_pendanaan"]),
        "",
        _row("Kenaikan neto kas dan setara kas", "", 20_000, 76_000),
        _row("Kas dan setara kas awal tahun", "", a["kas_awal_periode"], b["kas_awal_periode"]),
        _row("Kas dan setara kas akhir tahun", "", a["kas_akhir_periode"], b["kas_akhir_periode"]),
    ])


def _page_auditor_opinion() -> str:
    return "\n".join([
        "LAPORAN AUDITOR INDEPENDEN",
        "",
        "Nomor: 00123/2.1090/AU.1/05/1234-1/1/III/2026",
        "",
        f"Kepada para pemegang saham {DEMO_COMPANY}",
        "",
        "Opini",
        "",
        "Kami telah mengaudit laporan keuangan konsolidasian PT ABC Indonesia Tbk dan entitas",
        "anaknya, yang terdiri dari laporan posisi keuangan konsolidasian tanggal 31 Desember",
        "2025, serta laporan laba rugi, laporan perubahan ekuitas dan laporan arus kas",
        "konsolidasian untuk tahun yang berakhir pada tanggal tersebut.",
        "",
        "Menurut opini kami, laporan keuangan konsolidasian terlampir menyajikan secara wajar,",
        "dalam semua hal yang material, posisi keuangan konsolidasian Grup tanggal",
        "31 Desember 2025 sesuai dengan Standar Akuntansi Keuangan di Indonesia.",
        "",
        "Penekanan Suatu Hal",
        "",
        "Kami mengarahkan perhatian pada Catatan 12 atas laporan keuangan konsolidasian",
        "mengenai pinjaman bank jangka pendek sebesar Rp1.520.000 juta yang akan jatuh tempo",
        "dalam dua belas bulan ke depan, sementara arus kas neto dari aktivitas operasi pada",
        "tahun berjalan bernilai negatif. Kondisi tersebut, bersama dengan hal lain yang",
        "diungkapkan pada Catatan 12, mengindikasikan adanya ketidakpastian material yang",
        "dapat menimbulkan keraguan signifikan atas kemampuan Grup mempertahankan",
        "kelangsungan usahanya. Manajemen telah menyusun rencana yang diuraikan pada",
        "Catatan 12. Opini kami tidak dimodifikasi sehubungan dengan hal tersebut.",
        "",
        "Kantor Akuntan Publik Sejahtera, Wibawa & Rekan",
        "Jakarta, 20 Maret 2026",
    ])


def _page_notes_receivables() -> str:
    return "\n".join([
        "CATATAN ATAS LAPORAN KEUANGAN KONSOLIDASIAN",
        "Untuk tahun yang berakhir 31 Desember 2025",
        "(Dinyatakan dalam jutaan Rupiah)",
        "",
        "5. PIUTANG USAHA",
        "",
        "                                                             2025              2024",
        "Pihak ketiga                                            1.245.000         1.010.000",
        "Pihak berelasi (Catatan 26)                               438.000           195.000",
        "Dikurangi penyisihan penurunan nilai                      (43.000)          (25.000)",
        "Jumlah piutang usaha - neto                             1.640.000         1.180.000",
        "",
        "Analisis umur piutang usaha sebelum penyisihan adalah sebagai berikut:",
        "",
        "                                                             2025              2024",
        "Belum jatuh tempo                                         820.000           735.000",
        "Jatuh tempo 1 - 30 hari                                    395.000           260.000",
        "Jatuh tempo 31 - 90 hari                                   285.000           145.000",
        "Jatuh tempo lebih dari 90 hari                             183.000            65.000",
        "Jumlah                                                   1.683.000         1.205.000",
        "",
        "Peningkatan saldo piutang usaha pada tahun berjalan terutama berasal dari kontrak",
        "distribusi dengan dua pelanggan institusional yang ditandatangani pada kuartal",
        "keempat 2025, dengan syarat pembayaran 90 hari sejak penyerahan. Manajemen",
        "mengubah kebijakan syarat pembayaran pelanggan dari 60 hari menjadi 90 hari untuk",
        "pelanggan institusional terpilih guna mendukung perluasan jaringan distribusi.",
        "Sampai dengan tanggal laporan auditor, sebesar Rp312.000 juta dari saldo piutang",
        "tanggal 31 Desember 2025 telah diterima pembayarannya.",
    ])


def _page_notes_related_party() -> str:
    return "\n".join([
        "CATATAN ATAS LAPORAN KEUANGAN KONSOLIDASIAN (lanjutan)",
        "(Dinyatakan dalam jutaan Rupiah)",
        "",
        "26. TRANSAKSI DAN SALDO DENGAN PIHAK BERELASI",
        "",
        "Dalam kegiatan usaha normal, Grup melakukan transaksi dengan pihak berelasi.",
        "",
        "a. Sifat hubungan dengan pihak berelasi",
        "",
        "PT Sentosa Niaga Utama          Pemegang saham pengendali",
        "PT Abadi Logistik Nusantara     Entitas sepengendali",
        "PT Karya Distribusi Prima       Entitas sepengendali",
        "",
        "b. Transaksi dengan pihak berelasi",
        "",
        "                                                             2025              2024",
        "Penjualan kepada pihak berelasi                           742.000           388.000",
        "Pembelian dari pihak berelasi                             318.000           264.000",
        "Beban jasa logistik pihak berelasi                        126.000            94.000",
        "",
        "c. Saldo dengan pihak berelasi",
        "",
        "Piutang usaha kepada pihak berelasi                        438.000           195.000",
        "Utang usaha kepada pihak berelasi                          186.000           152.000",
        "",
        "Persentase penjualan kepada pihak berelasi terhadap jumlah pendapatan konsolidasian",
        "adalah 15,3% pada tahun 2025 dan 8,8% pada tahun 2024.",
        "",
        "Transaksi dengan pihak berelasi dilakukan berdasarkan persyaratan yang disepakati",
        "kedua belah pihak. Penetapan harga pada transaksi penjualan kepada pihak berelasi",
        "mengacu pada daftar harga distributor yang berlaku umum.",
    ])


def _page_notes_borrowings() -> str:
    return "\n".join([
        "CATATAN ATAS LAPORAN KEUANGAN KONSOLIDASIAN (lanjutan)",
        "(Dinyatakan dalam jutaan Rupiah)",
        "",
        "12. PINJAMAN BANK",
        "",
        "                                                             2025              2024",
        "Pinjaman modal kerja - jangka pendek                     1.520.000           730.000",
        "Pinjaman investasi - jangka panjang                      1.150.000         1.170.000",
        "Jumlah pinjaman bank                                     2.670.000         1.900.000",
        "",
        "Kenaikan pinjaman modal kerja pada tahun berjalan digunakan untuk mendanai",
        "kebutuhan modal kerja sehubungan dengan perluasan jaringan distribusi.",
        "",
        "Rencana manajemen atas kelangsungan usaha",
        "",
        "Manajemen telah menyusun rencana untuk mengelola liabilitas jangka pendek yang akan",
        "jatuh tempo, yang mencakup:",
        "",
        "a. Fasilitas kredit modal kerja yang belum ditarik sebesar Rp450.000 juta dari dua",
        "   bank nasional, berlaku sampai dengan 30 September 2026;",
        "b. Perpanjangan jatuh tempo atas pinjaman modal kerja sebesar Rp600.000 juta yang",
        "   telah memperoleh persetujuan prinsip dari bank pemberi pinjaman pada",
        "   Februari 2026;",
        "c. Percepatan penagihan piutang usaha melalui program insentif pembayaran dini.",
        "",
        "Manajemen berkeyakinan bahwa rencana tersebut memadai untuk memenuhi kewajiban",
        "Grup yang akan jatuh tempo dalam dua belas bulan ke depan.",
    ])


def _page_mda() -> str:
    return "\n".join([
        "ANALISIS DAN PEMBAHASAN MANAJEMEN",
        "",
        "Tinjauan Kinerja Keuangan 2025",
        "",
        "Pendapatan konsolidasian tahun 2025 tercatat sebesar Rp4.850.000 juta, tumbuh 9,7%",
        "dibandingkan Rp4.420.000 juta pada tahun 2024. Pertumbuhan tersebut terutama",
        "didorong oleh perluasan jaringan distribusi ke wilayah Indonesia bagian timur dan",
        "penambahan dua pelanggan institusional pada kuartal keempat.",
        "",
        "Laba tahun berjalan tercatat sebesar Rp214.000 juta, meningkat 8,6% dibandingkan",
        "Rp197.000 juta pada tahun sebelumnya.",
        "",
        "Likuiditas dan Sumber Pendanaan",
        "",
        "Arus kas neto dari aktivitas operasi pada tahun 2025 tercatat negatif sebesar",
        "Rp145.000 juta, dibandingkan positif Rp96.000 juta pada tahun 2024. Kondisi ini",
        "terutama disebabkan oleh peningkatan saldo piutang usaha sehubungan dengan",
        "perubahan syarat pembayaran pelanggan institusional dan peningkatan persediaan",
        "untuk mendukung perluasan jaringan distribusi.",
        "",
        "Kebutuhan modal kerja dipenuhi melalui fasilitas pinjaman bank jangka pendek yang",
        "meningkat menjadi Rp1.520.000 juta pada akhir tahun 2025.",
        "",
        "Prospek Usaha",
        "",
        "Manajemen menargetkan pertumbuhan pendapatan pada kisaran dua digit pada tahun 2026",
        "dan perbaikan arus kas operasi seiring normalisasi siklus penagihan piutang.",
    ])


def _pages(corrupt_balance: bool = False) -> list[tuple[str, str]]:
    pages = [
        ("Sampul", "\n".join([
            "LAPORAN TAHUNAN 2025",
            "",
            DEMO_COMPANY,
            "",
            "Entitas fiktif yang dibuat untuk demonstrasi Sentinel-AI.",
            "Dokumen ini bukan laporan keuangan perusahaan tercatat mana pun.",
            "",
            "Bursa Efek Indonesia · Kode: ABCI · Sektor: Barang Konsumen Primer",
        ])),
        ("Opini Auditor", _page_auditor_opinion()),
        ("Neraca", _page_balance_sheet()),
        ("Laba Rugi", _page_income_statement()),
        ("Arus Kas", _page_cash_flow()),
        ("Catatan - Piutang", _page_notes_receivables()),
        ("Catatan - Pinjaman", _page_notes_borrowings()),
        ("Catatan - Pihak Berelasi", _page_notes_related_party()),
        ("MD&A", _page_mda()),
    ]
    if corrupt_balance:
        # Break the balance-sheet identity on the equity line only. Every other
        # figure stays intact, so the retry loop has to isolate the broken row
        # rather than re-read the whole filing — and, when the attempts run out,
        # report exactly those rows as unextractable.
        balance = pages[2][1].replace(
            _rp(FY2025["total_ekuitas"]), _rp(FY2025["total_ekuitas"] - 470_000)
        )
        pages[2] = ("Neraca (tidak konsisten)", balance)
    return pages


def build_demo_pdf(
    output_path: Path | str | None = None,
    overwrite: bool = False,
    corrupt_balance: bool = False,
) -> Path:
    """Render the synthetic filing to a PDF and return its path.

    ``corrupt_balance=True`` produces a variant whose equity line does not
    reconcile — used to demonstrate (and test) the verification-driven retry
    loop and the ``tidak dapat diekstraksi dengan andal`` outcome.
    """
    if fitz is None:  # pragma: no cover
        raise RuntimeError("PyMuPDF diperlukan untuk membuat dokumen demo.")

    default_name = (
        "ABCI_Laporan_Tahunan_2025_DEMO_TIDAK_KONSISTEN.pdf" if corrupt_balance
        else "ABCI_Laporan_Tahunan_2025_DEMO.pdf"
    )
    path = Path(output_path or PROJECT_ROOT / "data" / "documents" / default_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return path

    document = fitz.open()
    for _, body in _pages(corrupt_balance):
        page = document.new_page(width=842, height=595)   # A4 landscape: wide columns
        page.insert_textbox(
            fitz.Rect(36, 36, 806, 559), body,
            fontname="cour", fontsize=8.2, align=0,
        )
    document.save(str(path))
    document.close()
    return path


def build_demo_news_fixtures(output_dir: Path | str | None = None) -> Path:
    """Offline market-intelligence fixtures for the same fictional entity."""
    import json

    directory = Path(output_dir or PROJECT_ROOT / "data" / "fixtures")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"news_{DEMO_TICKER}.json"

    payload: list[dict[str, Any]] = [
        {
            "title": "ABCI Targetkan Pertumbuhan Pendapatan Dua Digit dan Arus Kas Operasi Positif pada 2026",
            "url": "https://www.idx.co.id/id/perusahaan-tercatat/keterbukaan-informasi/demo-abci-2026",
            "snippet": (
                "Direktur Utama PT ABC Indonesia Tbk (ABCI) menyatakan perseroan menargetkan "
                "pertumbuhan pendapatan dua digit pada 2026. Manajemen juga menyampaikan bahwa "
                "arus kas operasi diperkirakan kembali positif seiring normalisasi siklus "
                "penagihan piutang."
            ),
            "published": "2026-03-24",
        },
        {
            "title": "Manajemen ABCI: Likuiditas Terjaga, Fasilitas Kredit Belum Ditarik Rp450 Miliar",
            "url": "https://www.kontan.co.id/news/demo-abci-likuiditas-2026",
            "snippet": (
                "Direktur Keuangan ABCI menyampaikan posisi likuiditas perseroan terjaga, dengan "
                "fasilitas kredit modal kerja belum ditarik sebesar Rp450 miliar dan persetujuan "
                "prinsip perpanjangan jatuh tempo pinjaman."
            ),
            "published": "2026-03-26",
        },
        {
            "title": "Auditor Beri Penekanan Suatu Hal atas Laporan Keuangan ABCI 2025",
            "url": "https://www.bisnis.com/demo-abci-opini-auditor-2025",
            "snippet": (
                "Kantor akuntan publik memberikan paragraf penekanan suatu hal atas laporan "
                "keuangan ABCI 2025 terkait pinjaman jangka pendek yang jatuh tempo dan arus kas "
                "operasi yang negatif. Opini tidak dimodifikasi."
            ),
            "published": "2026-03-21",
        },
        {
            "title": "Diskusi ABCI: Piutang Naik Tajam, Bagaimana Penagihannya?",
            "url": "https://stockbit.com/post/demo-abci-diskusi",
            "snippet": "Ramai dibahas kenaikan piutang ABCI di forum ritel.",
            "published": "2026-03-28",
        },
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def ensure_demo_corpus(overwrite: bool = False) -> dict[str, str]:
    """Create both the demo filing and its news fixtures."""
    return {
        "pdf": str(build_demo_pdf(overwrite=overwrite)),
        "news": str(build_demo_news_fixtures()),
        "ticker": DEMO_TICKER,
        "company_name": DEMO_COMPANY,
        "sector": DEMO_SECTOR,
    }


if __name__ == "__main__":
    created = ensure_demo_corpus(overwrite=True)
    print("Dokumen demo dibuat:")
    for key, value in created.items():
        print(f"  {key}: {value}")
