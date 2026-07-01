"""
alturl_bulk_shorten.py
=======================
Script untuk membuat short URL secara massal di http://alturl.com/

CARA PAKAI
----------
1. Install dependency:
     pip install requests beautifulsoup4

2. (Opsional) Kalau setelah dicoba ternyata request DITOLAK tanpa header
   Proxy-Authorization, isi variabel PROXY_AUTH di bawah dengan value
   yang Anda lihat di DevTools -> Network -> Headers -> Request Headers
   -> "Proxy-Authorization: Basic xxxxxxx...".
   (Value itu semacam credential, JANGAN dibagikan ke orang lain / di-commit ke Github publik)

3. Edit daftar RAW_URLS di bawah kalau mau ganti daftar link.

4. Jalankan:
     python alturl_bulk_shorten.py

5. Hasilnya akan tersimpan di file "hasil_shorturl.csv" (kolom: url_asli, short_url, status)
   sekaligus ditampilkan di layar.

CATATAN PENTING
----------------
- alturl.com meng-acak NAMA FIELD form setiap kali halaman di-load (teknik
  anti-bot / honeypot). Karena itu script ini WAJIB mengambil ulang form
  (GET) sebelum setiap submit, tidak bisa pakai payload yang sama berkali-kali.
- Ada field honeypot bernama literal "longurl" yang HARUS dibiarkan kosong.
  Field asli untuk menaruh URL panjang adalah field text yang tidak
  disembunyikan (bukan type=hidden, bukan style display:none).
- Jika struktur HTML alturl.com berubah / heuristik deteksi field gagal,
  jalankan dulu dengan DEBUG=True untuk lihat field apa saja yang
  terdeteksi, lalu sesuaikan fungsi `find_real_field()` di bawah.
"""

import os
import re
import csv
import time
import sys
from bs4 import BeautifulSoup
import requests

# ==========================
# KONFIGURASI
# ==========================
BASE_URL = "http://alturl.com/"          # halaman form utama
POST_URL_FALLBACK = "http://alturl.com/make_url.php"  # dipakai kalau form action relatif

# Isi ini HANYA jika request selalu gagal tanpa header ini.
# Diambil dari environment variable PROXY_AUTH (di GitHub Actions: dari Secrets)
# supaya tidak perlu hardcode credential di dalam kode.
PROXY_AUTH = os.getenv("PROXY_AUTH", "")   # kosong = tidak dipakai

DELAY_BETWEEN_REQUESTS_SEC = 2.0   # jangan terlalu cepat, hindari rate-limit
OUTPUT_CSV = "hasil_shorturl.csv"
DEBUG = False   # set True untuk lihat detail field yang terdeteksi

HEADERS_BASE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
}

# ==========================
# DAFTAR URL YANG MAU DI-SHORTEN
# ==========================
RAW_URLS = [
    "https://raw.githubusercontent.com/phxnkhlay/markpom/main/.github/workflows/main.yml",
] + [
    f"https://raw.githubusercontent.com/phxnkhlay/markpom/main/MP{day:02d}JANUARI2027"
    for day in range(1, 32)
]


# ==========================
# HELPER
# ==========================
def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS_BASE)
    if PROXY_AUTH:
        s.headers.update({"Proxy-Authorization": PROXY_AUTH})
    return s


def is_hidden_input(tag) -> bool:
    """Cek apakah <input> ini disembunyikan dari user (honeypot)."""
    itype = (tag.get("type") or "text").lower()
    if itype == "hidden":
        return True
    style = (tag.get("style") or "").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return False is False and ("display:none" in style or "visibility:hidden" in style)
    cls = " ".join(tag.get("class") or []).lower()
    if "hidden" in cls or "hp" in cls or "honeypot" in cls:
        return True
    return False


def get_fresh_form(session: requests.Session):
    """
    Ambil halaman form alturl.com, lalu kembalikan:
      - post_url: URL tujuan submit (action penuh, termasuk ?action=xxxx)
      - fields: dict semua field name -> default value (untuk decoy)
      - real_field_name: nama field text yang VISIBLE (tempat menaruh long URL)
      - method: GET/POST
    """
    resp = session.get(BASE_URL, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    form = soup.find("form")
    if form is None:
        raise RuntimeError("Tidak ketemu <form> di halaman alturl.com. Cek DEBUG output.")

    action = form.get("action") or POST_URL_FALLBACK
    if action.startswith("/"):
        action = "http://alturl.com" + action
    elif not action.startswith("http"):
        action = POST_URL_FALLBACK

    method = (form.get("method") or "POST").upper()

    fields = {}
    real_field_name = None
    visible_text_inputs = []

    for tag in form.find_all(["input", "textarea"]):
        name = tag.get("name")
        if not name:
            continue
        value = tag.get("value", "")
        fields[name] = value

        itype = (tag.get("type") or "text").lower()
        if tag.name == "textarea" or itype == "text":
            if not is_hidden_input(tag) and name.lower() != "longurl":
                visible_text_inputs.append(name)

    if DEBUG:
        print("== DEBUG: semua field terdeteksi ==")
        for k, v in fields.items():
            print(f"  {k!r} = {v!r}")
        print("== DEBUG: kandidat field visible (bukan honeypot) ==")
        print(visible_text_inputs)

    if len(visible_text_inputs) == 1:
        real_field_name = visible_text_inputs[0]
    elif len(visible_text_inputs) > 1:
        # Ambil yang pertama sebagai default, tapi kasih tahu user untuk cek manual
        real_field_name = visible_text_inputs[0]
        print(f"⚠️  Ada {len(visible_text_inputs)} kandidat field visible: {visible_text_inputs}. "
              f"Memakai '{real_field_name}'. Jalankan DEBUG=True kalau hasil salah.")
    else:
        raise RuntimeError(
            "Tidak berhasil menentukan field asli untuk long URL. "
            "Jalankan dengan DEBUG=True lalu cek HTML form-nya manual."
        )

    return action, method, fields, real_field_name


def extract_short_url(html: str) -> str | None:
    """Cari short URL di HTML hasil response."""
    soup = BeautifulSoup(html, "html.parser")

    # 1) Coba cari <input> yang value-nya berupa short url alturl.com
    for tag in soup.find_all("input"):
        val = tag.get("value", "")
        if re.match(r"^https?://alturl\.com/[a-zA-Z0-9]+/?$", val.strip()):
            return val.strip()

    # 2) Coba cari <a href="...alturl.com/xxxx">
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if re.match(r"^https?://alturl\.com/[a-zA-Z0-9]+/?$", href):
            return href

    # 3) Fallback: regex langsung di teks HTML mentah
    m = re.search(r"https?://alturl\.com/[a-zA-Z0-9]{4,}", html)
    if m:
        return m.group(0)

    return None


def shorten_one(session: requests.Session, long_url: str) -> tuple[str, str]:
    """
    Kembalikan (short_url, status_text)
    """
    try:
        post_url, method, fields, real_field = get_fresh_form(session)
    except Exception as e:
        return "", f"GAGAL ambil form: {e}"

    payload = dict(fields)          # mulai dari semua default (termasuk honeypot kosong)
    payload[real_field] = long_url  # isi field asli dengan URL panjang
    if "longurl" in payload:
        payload["longurl"] = ""     # pastikan honeypot tetap kosong

    try:
        if method == "GET":
            resp = session.get(post_url, params=payload, timeout=20)
        else:
            resp = session.post(post_url, data=payload, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        return "", f"GAGAL submit: {e}"

    short_url = extract_short_url(resp.text)
    if short_url:
        return short_url, "OK"

    if DEBUG:
        with open("debug_last_response.html", "w", encoding="utf-8") as f:
            f.write(resp.text)
        print("⚠️  Short URL tidak ketemu di response. HTML disimpan ke debug_last_response.html")

    return "", "GAGAL: short URL tidak ditemukan di response"


# ==========================
# MAIN
# ==========================
def main():
    session = build_session()
    results = []

    total = len(RAW_URLS)
    for idx, long_url in enumerate(RAW_URLS, start=1):
        print(f"\n({idx}/{total}) Memproses: {long_url}")
        short_url, status = shorten_one(session, long_url)
        print(f"  -> {status} | {short_url}")
        results.append((long_url, short_url, status))
        time.sleep(DELAY_BETWEEN_REQUESTS_SEC)

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["url_asli", "short_url", "status"])
        writer.writerows(results)

    print(f"\n🎯 Selesai. Hasil tersimpan di: {OUTPUT_CSV}")

    ok_count = sum(1 for _, _, s in results if s == "OK")
    print(f"   Berhasil: {ok_count}/{total}")


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        print("⚠️  Script ini pakai type hint 'tuple[str, str]' -> butuh Python 3.9+.")
    main()
