import csv, html, io, re, uuid
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, render_template_string, request

app = Flask(__name__)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
BAD_END = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; EmailExtractor/1.0)"}
PATHS = ["", "/contact", "/contact-us", "/contact.html", "/contact-us.html",
         "/about", "/about-us", "/get-in-touch"]
JUNK = ("sentry", "wixpress", "example.", "domain.com", "yourdomain", "email.com", "u003e")
MAX_SITES = 100
CACHE = {}


def norm_url(u):
    u = u.strip().strip(",;\"'")
    if not u or " " in u:
        return ""
    u = u if u.startswith("http") else "http://" + u
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}/" if p.netloc else ""


@lru_cache(maxsize=512)
def robots_for(origin):
    try:
        r = requests.get(origin + "/robots.txt", headers=HEADERS, timeout=5)
        if r.status_code != 200:
            return None
        rp = RobotFileParser()
        rp.parse(r.text.splitlines())
        return rp
    except Exception:
        return None


def allowed(url):
    p = urlparse(url)
    rp = robots_for(f"{p.scheme}://{p.netloc}")
    return True if rp is None else rp.can_fetch(HEADERS["User-Agent"], url)


def decode_cf(hexstr):
    try:
        b = bytes.fromhex(hexstr)
        return "".join(chr(c ^ b[0]) for c in b[1:])
    except Exception:
        return ""


def extract_from_html(raw):
    emails, phones = set(), set()
    soup = BeautifulSoup(raw, "html.parser")
    for a in soup.select('a[href^="mailto:"]'):
        emails.update(EMAIL_RE.findall(html.unescape(a["href"])))
    for a in soup.select('a[href^="tel:"]'):
        phones.add(a["href"][4:].strip())
    for el in soup.select("[data-cfemail]"):
        emails.update(EMAIL_RE.findall(decode_cf(el["data-cfemail"])))
    emails.update(EMAIL_RE.findall(html.unescape(raw)))  # raw source: catches script/JSON data
    clean = {e.lower().strip(".") for e in emails}
    clean = {e for e in clean if not e.endswith(BAD_END) and not any(j in e for j in JUNK) and len(e) < 80}
    return clean, phones


def scrape(site):
    emails, phones = set(), set()
    base = site if site.endswith("/") else site + "/"
    for path in PATHS:
        url = urljoin(base, path.lstrip("/"))
        try:
            if not allowed(url):
                continue
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                continue
            e, p = extract_from_html(r.text)
            emails |= e
            phones |= p
        except Exception:
            continue
    return {"website": site, "emails": ", ".join(sorted(emails)), "phones": ", ".join(sorted(phones))}


def urls_from_csv(data):
    text = data.decode("utf-8-sig", errors="ignore")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0]]
    col = next((i for i, h in enumerate(head) if h in ("website", "url", "site", "websiteurl", "web")), None)
    if col is None:
        col = next((i for i, h in enumerate(head) if "website" in h or "url" in h), None)
    if col is None:  # no header match: take every cell that looks like a domain
        return [c for r in rows for c in r if "." in c and " " not in c]
    return [r[col] for r in rows[1:] if len(r) > col]


PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Email Extractor</title><style>
body{font-family:system-ui;margin:16px;max-width:1000px}
textarea,input,button{padding:10px;font-size:16px;margin:4px 0;width:100%;box-sizing:border-box}
.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;margin-top:12px}
td,th{border:1px solid #ccc;padding:6px;text-align:left;font-size:14px}</style>
<h2>Email Extractor</h2>
<form method=post enctype=multipart/form-data>
<textarea name=urls rows=7 placeholder="Paste websites, one per line">{{ urls }}</textarea>
<p>or upload a CSV (e.g. from Apify) with a website column:</p>
<input type=file name=file accept=".csv">
<button>Extract (max {{ mx }})</button></form>
{% if error %}<p style="color:red">{{ error }}</p>{% endif %}
{% if rows is not none %}
<p>{{ rows|length }} sites checked, {{ found }} with emails. <a href="/csv/{{ rid }}">Download CSV</a></p>
<div class=wrap><table><tr><th>Website<th>Emails<th>Phones</tr>
{% for r in rows %}<tr><td>{{ r.website }}<td>{{ r.emails }}<td>{{ r.phones }}</tr>{% endfor %}
</table></div>{% endif %}
"""


@app.route("/", methods=["GET", "POST"])
def index():
    urls, rows, rid, error, found = "", None, None, None, 0
    if request.method == "POST":
        urls = request.form.get("urls", "")
        raw = urls.splitlines()
        f = request.files.get("file")
        if f and f.filename:
            raw += urls_from_csv(f.read())
        lst = list(dict.fromkeys(u for u in map(norm_url, raw) if u))
        if not lst:
            error = "Paste at least one website or upload a CSV."
        else:
            note = f" (only the first {MAX_SITES} were checked)" if len(lst) > MAX_SITES else ""
            with ThreadPoolExecutor(max_workers=10) as ex:
                rows = list(ex.map(scrape, lst[:MAX_SITES]))
            found = sum(1 for r in rows if r["emails"])
            rid = uuid.uuid4().hex
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=["website", "emails", "phones"])
            w.writeheader()
            w.writerows(rows)
            CACHE[rid] = buf.getvalue()
            if len(CACHE) > 20:
                CACHE.pop(next(iter(CACHE)))
            error = note or None
    return render_template_string(PAGE, urls=urls, rows=rows, rid=rid, error=error, found=found, mx=MAX_SITES)


@app.route("/csv/<rid>")
def dl(rid):
    return Response(CACHE.get(rid, ""), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=emails.csv"})


if __name__ == "__main__":
    app.run(debug=True)
