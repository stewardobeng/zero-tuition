"""
ZeroTuition web server.

Scrapes coupon sites on a schedule (no login needed for that part), enriches
each course from its Udemy landing page (thumbnail, language, paid/free),
optionally verifies coupons with a logged-in browser session on this machine,
and serves everything as a server-rendered page (no client JS required).

Run:  .venv-build/Scripts/python.exe server.py
Then open http://127.0.0.1:8000/
"""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from html import escape
from urllib.parse import quote

import requests
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from base import VERSION, LoginException, Scraper, Udemy, logger, scraper_dict

SCAN_INTERVAL_SECONDS = 30 * 60
HOST = "127.0.0.1"  # localhost only; change / reverse-proxy to publish
PORT = 8000
MAX_ROWS = 500
PRUNE_DAYS = 7
ENRICH_WORKERS = 3
ENRICH_DELAY = 0.4  # seconds between Udemy page fetches (per worker)
VERIFY_DELAY = 1.2  # seconds between coupon checks (single worker)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 "
    "Firefox/137.0"
)

DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "zerotuition.db"
)

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

db_lock = threading.Lock()
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row


def init_db():
    with db_lock:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS courses (
                url TEXT PRIMARY KEY,
                title TEXT,
                site TEXT,
                coupon_code TEXT,
                course_id TEXT,
                image TEXT,
                language TEXT,
                is_paid INTEGER,
                price REAL,
                verified INTEGER DEFAULT 0,   -- 0 unknown, 1 yes 100%, 2 no
                enrich_attempts INTEGER DEFAULT 0,
                first_seen TEXT,
                last_seen TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_courses_seen ON courses(first_seen);
            """
        )
        db.commit()


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def upsert_courses(items):
    with db_lock:
        db.executemany(
            """
            INSERT INTO courses (url, title, site, coupon_code, first_seen, last_seen)
            VALUES (:url, :title, :site, :coupon, :now, :now)
            ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                site=excluded.site,
                coupon_code=excluded.coupon_code,
                last_seen=excluded.last_seen
            """,
            [
                {
                    "url": c["url"],
                    "title": c["title"],
                    "site": c["site"],
                    "coupon": c["coupon_code"],
                    "now": now_iso(),
                }
                for c in items
            ],
        )
        db.commit()


def prune_old():
    cutoff = (datetime.now() - timedelta(days=PRUNE_DAYS)).isoformat(
        timespec="seconds"
    )
    with db_lock:
        db.execute("DELETE FROM courses WHERE last_seen < ?", (cutoff,))
        db.commit()


# --------------------------------------------------------------------------
# Scraping (per-site, incremental commit into the DB)
# --------------------------------------------------------------------------

_live = {}  # site -> {"progress", "length", "done", "error"}


def _safe_scrape(scraper, code, site):
    try:
        getattr(scraper, code)()
    except Exception:
        logger.exception(f"Unhandled error while scraping {site}")


def _run_site_scan(site):
    """Scrape one site and upsert its results immediately, reporting progress."""
    code = scraper_dict[site]
    scraper = Scraper([site])
    runner = threading.Thread(
        target=_safe_scrape, args=(scraper, code, site), daemon=True
    )
    runner.start()
    while runner.is_alive():
        done = bool(getattr(scraper, f"{code}_done", False))
        _live[site] = {
            "progress": getattr(scraper, f"{code}_progress", 0),
            "length": getattr(scraper, f"{code}_length", 0),
            "done": done,
            "error": bool(getattr(scraper, f"{code}_error", "")),
        }
        if done:
            break
        time.sleep(0.5)
    runner.join(timeout=30)
    items = []
    for course in getattr(scraper, f"{code}_data", []):
        items.append(
            {
                "url": course.url,
                "title": course.title,
                "site": site,
                "coupon_code": course.coupon_code or "",
            }
        )
    # de-dup within site
    uniq = {c["url"]: c for c in items}
    upsert_courses(list(uniq.values()))
    _live[site] = {
        "progress": len(uniq),
        "length": len(uniq),
        "done": True,
        "error": bool(getattr(scraper, f"{code}_error", "")),
    }
    logger.info(f"Site scan committed: {site} -> {len(uniq)} courses")


def _scan_job():
    global _last_scan, _scan_duration
    logger.info("Web scan starting")
    started = time.monotonic()
    _live.clear()
    try:
        threads = []
        for site in scraper_dict:
            t = threading.Thread(target=_run_site_scan, args=(site,), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(0.2)
        for t in threads:
            t.join()
        prune_old()
    finally:
        duration = round(time.monotonic() - started, 1)
        with db_lock:
            n = db.execute("SELECT COUNT(*) c FROM courses").fetchone()["c"]
        _last_scan = now_iso()
        _scan_duration = duration
        request_scan._running = False
        logger.info(f"Web scan finished in {duration}s, {n} courses in DB")


def request_scan() -> bool:
    if getattr(request_scan, "_running", False):
        return False
    request_scan._running = True
    threading.Thread(target=_scan_job, daemon=True).start()
    return True


# --------------------------------------------------------------------------
# Enrichment: thumbnail, language, paid/free from each course's Udemy page
# --------------------------------------------------------------------------

def _enrich_worker():
    while True:
        with db_lock:
            row = db.execute(
                """
                SELECT url FROM courses
                WHERE (image IS NULL OR language IS NULL OR is_paid IS NULL)
                  AND enrich_attempts < 3
                ORDER BY first_seen DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            time.sleep(5)
            continue
        url = row["url"]
        image = course_id = language = None
        is_paid = None
        try:
            r = requests.get(
                url, headers={"User-Agent": UA}, timeout=(15, 30)
            )
            soup = BeautifulSoup(r.content, "lxml")
            body = soup.find("body")
            og = soup.find("meta", property="og:image")
            if og and og.get("content"):
                image = og["content"]
            if body is not None:
                cid = body.get("data-clp-course-id")
                if cid:
                    course_id = cid
                dma_raw = body.get("data-module-args")
                if dma_raw:
                    course = (
                        json.loads(dma_raw)
                        .get("serverSideProps", {})
                        .get("course", {})
                    )
                    language = course.get("localeSimpleEnglishTitle")
                    if "isPaid" in course:
                        is_paid = 1 if course.get("isPaid") else 0
        except Exception as e:
            logger.error(f"Enrich failed for {url}: {e}")
        # save whatever we got; every pass counts an attempt so a course
        # whose page lacks some fields can't stall the whole queue
        with db_lock:
            db.execute(
                """UPDATE courses SET
                     image=COALESCE(?, image),
                     course_id=COALESCE(?, course_id),
                     language=COALESCE(?, language),
                     is_paid=COALESCE(?, is_paid),
                     enrich_attempts=enrich_attempts+1
                   WHERE url=?""",
                (image, course_id, language, is_paid, url),
            )
            db.commit()
        time.sleep(ENRICH_DELAY)


# --------------------------------------------------------------------------
# Verification: confirm the coupon really gives 100% off (needs a login)
# --------------------------------------------------------------------------

_verify_session = None
_verify_checked_at = 0.0
_login_backoff_until = 0.0  # Udemy throttles repeated logins; back off 45 min


def _session_is_logged_in(s):
    try:
        r = s.get(
            "https://www.udemy.com/api-2.0/contexts/me/?header=True", timeout=30
        )
        return r.json().get("header", {}).get("isLoggedIn", False)
    except Exception:
        return False


def _session_from_browser_cookies():
    try:
        import rookiepy

        cookies = rookiepy.to_cookiejar(rookiepy.load(["www.udemy.com"]))
        s = requests.Session()
        s.cookies.update(cookies)
        s.headers.update({"User-Agent": UA, "Referer": "https://www.udemy.com/"})
        if _session_is_logged_in(s):
            logger.info("Verification: using browser cookies")
            return s
        logger.warning("Browser cookies are logged out")
    except Exception as e:
        logger.warning(f"Could not read browser cookies: {e}")
    return None


def _session_from_saved_credentials():
    """Log in with the credentials saved by the desktop app, if any."""
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(here, "zerotuition-gui-settings.json"),
        os.path.join(here, "dist", "zerotuition-gui-settings.json"),
    ):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            continue
        email, password = cfg.get("email"), cfg.get("password")
        if not email or not password:
            continue
        try:
            u = Udemy("server")
            u.manual_login(email, password)
            s = requests.Session()
            s.cookies.update(u.cookie_dict)
            s.headers.update({"User-Agent": UA, "Referer": "https://www.udemy.com/"})
            if _session_is_logged_in(s):
                logger.info("Verification: logged in with saved credentials")
                return s
        except LoginException as e:
            if "Too many" in str(e):
                global _login_backoff_until
                _login_backoff_until = time.monotonic() + 45 * 60
                logger.warning(
                    "Udemy login throttled - retrying in 45 minutes. "
                    "Tip: logging into udemy.com in any browser on this "
                    "machine enables verification immediately."
                )
            else:
                logger.warning(f"Credential login rejected: {e}")
        except Exception as e:
            logger.warning(f"Credential login failed: {e}")
    return None


def _get_verify_session():
    """Build a requests session from this machine's browser cookies."""
    global _verify_session, _verify_checked_at
    if time.monotonic() - _verify_checked_at < 600:
        return _verify_session
    if time.monotonic() < _login_backoff_until:
        return None
    _verify_checked_at = time.monotonic()
    s = _session_from_browser_cookies() or _session_from_saved_credentials()
    if s is not None:
        logger.info("Verification session ready")
    else:
        logger.warning(
            "Verification unavailable: no logged-in browser cookies and no "
            "saved credentials"
        )
    _verify_session = s
    return s


def verification_available():
    return _get_verify_session() is not None


def _verify_worker():
    while True:
        s = _get_verify_session()
        if s is None:
            time.sleep(60)
            continue
        with db_lock:
            row = db.execute(
                """
                SELECT url, course_id, coupon_code FROM courses
                WHERE verified=0 AND course_id IS NOT NULL
                  AND coupon_code != ''
                ORDER BY first_seen DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            time.sleep(10)
            continue
        url, cid, code = row["url"], row["course_id"], row["coupon_code"]
        ok = None
        price = None
        try:
            r = s.get(
                "https://www.udemy.com/api-2.0/course-landing-components/"
                f"{cid}/me/?components=purchase,redeem_coupon&couponCode={code}",
                timeout=30,
            )
            d = r.json()
            purchase = d.get("purchase", {}).get("data", {})
            lp = purchase.get("list_price", {}).get("amount")
            if lp is not None:
                price = lp
            discount = purchase.get("pricing_result", {}).get("discount_percent")
            attempts = d.get("redeem_coupon", {}).get("discount_attempts") or [{}]
            status = attempts[0].get("status")
            ok = discount == 100 and status == "applied"
        except Exception as e:
            logger.error(f"Verify failed for {url}: {e}")
        with db_lock:
            if ok is None:
                pass  # transient error; leave unknown, will retry later
            else:
                db.execute(
                    "UPDATE courses SET verified=?, price=? WHERE url=?",
                    (1 if ok else 2, price, url),
                )
            db.commit()
        time.sleep(VERIFY_DELAY)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

CSS = """
:root { --bg:#0b0e14; --panel:#141822; --panel2:#1d2330; --line:#262d3d;
        --accent:#a435f0; --accent2:#c77dff; --text:#e6e9ef; --muted:#98a2b3;
        --green:#22c55e; --red:#ef4444; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font:15px/1.5 "Segoe UI",system-ui,sans-serif; }
a { color:inherit; text-decoration:none; }
.top { display:flex; align-items:center; gap:12px; padding:14px 22px;
       background:var(--panel); border-bottom:1px solid var(--line);
       position:sticky; top:0; z-index:5; }
.logo { width:34px; height:34px; border-radius:9px; background:var(--accent);
       color:#fff; display:flex; align-items:center; justify-content:center;
       font-weight:800; font-size:14px; }
.brand { font-size:20px; font-weight:800; letter-spacing:.3px; }
.brand span { color:var(--accent2); }
.topsearch { flex:1; display:flex; gap:8px; max-width:640px; margin:0 auto; }
.topsearch input { flex:1; background:var(--panel2); border:1px solid var(--line);
       color:var(--text); border-radius:999px; padding:9px 18px; font-size:14px; }
button, .btn { background:var(--accent); color:#fff; border:0; border-radius:999px;
       padding:9px 18px; font-weight:700; font-size:14px; cursor:pointer; }
button:disabled { opacity:.45; cursor:default; }
.layout { display:flex; gap:20px; max-width:1400px; margin:0 auto;
       padding:20px 22px 60px; align-items:flex-start; }
.side { width:250px; flex-shrink:0; background:var(--panel); border:1px solid var(--line);
       border-radius:14px; padding:16px; position:sticky; top:76px; }
.side h3 { margin:14px 0 8px; font-size:12px; text-transform:uppercase;
       letter-spacing:.8px; color:var(--muted); }
.side h3:first-child { margin-top:0; }
.fitem { display:flex; justify-content:space-between; align-items:center;
       padding:6px 8px; border-radius:8px; font-size:13.5px; color:var(--text); }
.fitem:hover { background:var(--panel2); }
.fitem .n { color:var(--muted); font-size:12px; }
.fitem.on { background:var(--panel2); color:var(--accent2); font-weight:700; }
.box { display:flex; align-items:center; gap:8px; padding:6px 8px; font-size:13.5px; }
.box .st { width:34px; text-align:center; border-radius:6px; font-size:11.5px;
       padding:2px 0; font-weight:700; }
.st.on { background:#14351f; color:var(--green); }
.st.off { background:var(--panel2); color:var(--muted); }
.note { font-size:11.5px; color:var(--muted); padding:4px 8px; line-height:1.45; }
.main { flex:1; min-width:0; }
.scanpanel { background:var(--panel); border:1px solid var(--accent); border-radius:12px;
       padding:12px 16px; margin-bottom:16px; }
.livesites { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
.lsite { font-size:12px; background:var(--panel2); border-radius:6px; padding:4px 10px;
       color:var(--muted); }
.lsite .ok { color:var(--green); } .lsite .num { color:var(--accent2); }
.err { color:var(--red); }
.spin { display:inline-block; width:13px; height:13px; border:2px solid var(--accent2);
       border-top-color:transparent; border-radius:50%; vertical-align:-2px;
       animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.statbar { display:flex; flex-wrap:wrap; gap:8px 26px; color:var(--muted);
       font-size:13px; margin:0 2px 16px; align-items:center; }
.statbar b { color:var(--text); }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(230px,1fr));
       gap:16px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:14px;
       overflow:hidden; transition:transform .12s, border-color .12s; display:block; }
.card:hover { transform:translateY(-2px); border-color:var(--accent); }
.thumb { position:relative; aspect-ratio:16/9; background:linear-gradient(135deg,#1d2330,#2a1e3f);
       overflow:hidden; }
.thumb img { width:100%; height:100%; object-fit:cover; display:block; }
.off { position:absolute; left:8px; top:8px; background:var(--green); color:#04240f;
       font-size:11px; font-weight:800; padding:3px 8px; border-radius:6px; }
.vr { position:absolute; right:8px; top:8px; font-size:10.5px; font-weight:700;
       padding:3px 8px; border-radius:6px; background:rgba(10,12,18,.8); }
.vr.yes { color:var(--green); } .vr.no { color:var(--red); }
.vr.uk { color:var(--muted); }
.cbody { padding:11px 13px 13px; }
.ctitle { color:#ffffff; font-weight:650; font-size:13.8px; line-height:1.35;
       min-height:56px; }
.cmeta { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; align-items:center; }
.badge { font-size:10.5px; background:var(--panel2); color:var(--muted);
       border-radius:5px; padding:2px 7px; }
.code { font-size:11px; color:var(--accent2); background:#241332;
       border:1px dashed var(--accent); border-radius:5px; padding:2px 7px; }
.cfoot { display:flex; justify-content:space-between; align-items:center; margin-top:10px; }
.price { font-size:12px; color:var(--muted); }
.price s { color:#6b7280; }
.free { color:var(--green); font-weight:700; font-size:12px; }
.enroll { display:inline-block; background:var(--accent); color:#fff; font-weight:700;
       font-size:12.5px; border-radius:8px; padding:7px 14px; }
.empty { color:var(--muted); text-align:center; padding:60px 20px; background:var(--panel);
       border-radius:14px; border:1px dashed var(--line); line-height:1.7; }
.foot { color:var(--muted); font-size:11.5px; margin-top:34px; text-align:center; }
@media (max-width:900px) { .layout { flex-direction:column; }
  .side { width:100%; position:static; } }
"""


def esc(s) -> str:
    return escape(str(s), quote=True)


def render_page(q: str = "", site: str = "", lang: int = 1, ver: int = 1,
                paid: int = 1) -> str:
    with db_lock:
        total = db.execute("SELECT COUNT(*) c FROM courses").fetchone()["c"]
        n_enriched = db.execute(
            "SELECT COUNT(*) c FROM courses WHERE language IS NOT NULL"
        ).fetchone()["c"]
        n_verified = db.execute(
            "SELECT COUNT(*) c FROM courses WHERE verified=1"
        ).fetchone()["c"]
        n_failed = db.execute(
            "SELECT COUNT(*) c FROM courses WHERE verified=2"
        ).fetchone()["c"]
        sites = db.execute(
            "SELECT site, COUNT(*) n FROM courses GROUP BY site ORDER BY site"
        ).fetchall()

    # scan meta is kept in-memory
    global _last_scan, _scan_duration
    last_scan, scan_duration = _last_scan, _scan_duration
    scanning = getattr(request_scan, "_running", False)
    live = _live_sites() if scanning else []

    where = ["1=1"]
    args: list = []
    if q.strip():
        where.append("instr(lower(title), ?) > 0")
        args.append(q.strip().casefold())
    if site in scraper_dict:
        where.append("site = ?")
        args.append(site)
    # if verification hasn't produced results yet (or can't run), relax the
    # filter instead of showing an empty page forever
    ver_eff = bool(ver) and (n_verified > 0 or verification_available())

    def build_where(use_lang, use_paid, use_ver):
        w = ["1=1"]
        a: list = []
        if q.strip():
            w.append("instr(lower(title), ?) > 0")
            a.append(q.strip().casefold())
        if site in scraper_dict:
            w.append("site = ?")
            a.append(site)
        if use_lang:
            w.append("language = 'English'")
        if use_paid:
            w.append("is_paid = 1")
        if use_ver:
            w.append("verified = 1")
        return " AND ".join(w), a

    where, args = build_where(lang, paid, ver_eff)
    rows = []
    with db_lock:
        for r in db.execute(
            "SELECT url,title,site,coupon_code,image,language,is_paid,price,verified "
            f"FROM courses WHERE {where} "
            "ORDER BY first_seen DESC, url LIMIT ?",
            (*args, MAX_ROWS),
        ):
            rows.append(dict(r))
        total_matching = db.execute(
            f"SELECT COUNT(*) c FROM courses WHERE {where}", args
        ).fetchone()["c"]

    # while enrichment/verification are still filling in, a strict filter
    # combination can match nothing - relax it visibly rather than showing
    # an empty page
    relax_note = ""
    if not rows and (lang or paid) and total:
        where, args = build_where(0, 0, ver_eff)
        with db_lock:
            for r in db.execute(
                "SELECT url,title,site,coupon_code,image,language,is_paid,price,verified "
                f"FROM courses WHERE {where} "
                "ORDER BY first_seen DESC, url LIMIT ?",
                (*args, MAX_ROWS),
            ):
                rows.append(dict(r))
            total_matching = db.execute(
                f"SELECT COUNT(*) c FROM courses WHERE {where}", args
            ).fetchone()["c"]
        if rows:
            relax_note = (
                '<span class="err">language/paid filters warming up as courses '
                "are analyzed &mdash; showing everything for now</span>"
            )
    if ver and not ver_eff:
        relax_note += (
            '<span class="err">verification warming up &mdash; showing '
            "unverified courses for now</span>"
        )

    def _href(params):
        # keep explicit zeros (filter OFF states) but drop empty values
        pairs = [
            f"{k}={quote(str(v))}"
            for k, v in params.items()
            if v is not None and v != ""
        ]
        return "/?" + "&".join(pairs) if pairs else "/"

    def tlink(label, params, on, extra=""):
        return (
            f'<a class="fitem{" on" if on else ""}" href="{_href(params)}">{label}'
            f'<span class="n">{extra}</span></a>'
        )

    base_params = {"q": q, "lang": lang, "ver": ver, "paid": paid}
    site_links = [tlink("All sites", {**base_params, "site": ""}, not site, total)]
    for s in sites:
        site_links.append(
            tlink(
                esc(s["site"]),
                {**base_params, "site": s["site"]},
                site == s["site"],
                s["n"],
            )
        )

    def toggle(label, key, cur, hint=""):
        new = {**base_params, "site": site, key: 0 if cur else 1}
        return (
            f'<a class="box" href="{_href(new)}"><span class="st {"on" if cur else "off"}">'
            f'{"ON" if cur else "OFF"}</span> {label}</a>'
            + (f'<div class="note">{hint}</div>' if hint else "")
        )

    scan_panel = ""
    if scanning:
        rows_html = "".join(
            '<span class="lsite">{} {}</span>'.format(
                esc(s["site"]),
                (
                    '<span class="err">error</span>'
                    if s["error"]
                    else '<span class="ok">&#10003; {}</span>'.format(s["length"])
                    if s["done"]
                    else '<span class="num">{}/{}...</span>'.format(
                        s["progress"], s["length"] or "?"
                    )
                    if s["length"]
                    else "starting..."
                ),
            )
            for s in live
        )
        scan_panel = (
            '<div class="scanpanel"><div><span class="spin"></span> '
            "<b>Scanning the coupon sites&hellip;</b> &mdash; new courses appear "
            "as each site finishes. This page refreshes itself.</div>"
            f'<div class="livesites">{rows_html}</div></div>'
        )

    cards = []
    for c in rows:
        img = (
            f'<img src="{esc(c["image"])}" loading="lazy" '
            "onerror=\"this.style.display='none'\">"
            if c["image"]
            else ""
        )
        if c["verified"] == 1:
            vtag = '<span class="vr yes">&#10003; 100% OFF</span>'
        elif c["verified"] == 2:
            vtag = '<span class="vr no">not free</span>'
        else:
            vtag = '<span class="vr uk">checking...</span>'
        coupon = (
            f'<span class="code">{esc(c["coupon_code"])}</span>'
            if c["coupon_code"]
            else '<span class="badge">no code needed</span>'
        )
        lang_badge = (
            f'<span class="badge">{esc(c["language"])}</span>'
            if c["language"]
            else ""
        )
        price_html = (
            f'<span class="price"><s>{c["price"]:.0f} USD</s></span>'
            if c["price"]
            else ""
        )
        cards.append(
            '<a class="card" href="{url}" target="_blank" rel="noopener">'
            '<div class="thumb">{img}<span class="off">FREE</span>{vtag}</div>'
            '<div class="cbody"><div class="ctitle">{title}</div>'
            '<div class="cmeta"><span class="badge">{site}</span>{lang}{coupon}</div>'
            '<div class="cfoot">{price}<span class="enroll">Enroll &rarr;</span></div>'
            "</div></a>".format(
                url=esc(c["url"]),
                img=img,
                vtag=vtag,
                title=esc(c["title"]),
                site=esc(c["site"]),
                lang=lang_badge,
                coupon=coupon,
                price=price_html,
            )
        )
    listing = (
        '<div class="grid">' + "".join(cards) + "</div>"
        if cards
        else '<div class="empty"><b>No courses match the current filters.</b><br>'
        f"{total} courses collected &middot; {n_enriched} enriched &middot; "
        f"{n_verified} verified 100% off so far.<br>"
        "Verification and enrichment run in the background and fill in over "
        'time &mdash; or click the filters in the sidebar to include unverified '
        "courses.</div>"
    )
    more = (
        ""
        if total_matching <= MAX_ROWS
        else f'<div class="empty">Showing {MAX_ROWS} of {total_matching} matches &mdash; refine to see more.</div>'
    )
    listing += more

    refresh_secs = 15 if scanning else 60
    ver_note = (
        ""
        if verification_available()
        else '<div class="note">Verification needs a logged-in udemy.com session '
        "in a browser on this machine.</div>"
    )

    sidebar = (
        '<aside class="side">'
        '<h3>Coupon status</h3>'
        + toggle("Verified 100% off only", "ver", ver,
                 f"{n_verified} verified &middot; {n_failed} failed &middot; rest checking")
        + toggle("Paid courses only (skip always-free)", "paid", paid)
        + toggle("English only", "lang", lang)
        + "<h3>Sites</h3>" + "".join(site_links)
        + "<h3>Progress</h3>"
        + f'<div class="note">{total} courses collected<br>{n_enriched} enriched with details<br>'
        + f"{n_verified} verified 100% off<br>Verification: "
        + ("active</div>" if verification_available() else "waiting for login</div>" + ver_note)
        + '<h3>Rescan</h3><form method="post" action="/scan">'
        + ('<button type="submit" disabled>Scanning...</button>' if scanning
           else '<button type="submit">Scan coupon sites now</button>')
        + "</form></aside>"
    )

    statbar = (
        '<div class="statbar">'
        f"<span><b>{total_matching}</b> courses match</span>"
        f"<span>Last scan: <b>{esc((_last_scan or 'never').replace('T', ' '))}</b></span>"
        + (f"<span>took <b>{_scan_duration}s</b></span>" if _scan_duration else "")
        + f"<span>auto-rescan every <b>{SCAN_INTERVAL_SECONDS // 60} min</b></span>"
        + "<span>page auto-refreshes</span>"
        + relax_note
        + "</div>"
    )

    search = (
        '<form class="topsearch" method="get" action="/">'
        f'<input name="q" value="{esc(q)}" placeholder="Search courses...">'
        f'<input type="hidden" name="site" value="{esc(site)}">'
        f'<input type="hidden" name="lang" value="{lang}">'
        f'<input type="hidden" name="ver" value="{ver}">'
        f'<input type="hidden" name="paid" value="{paid}">'
        '<button type="submit">Search</button></form>'
    )

    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f'<meta http-equiv="refresh" content="{refresh_secs}">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>ZeroTuition &mdash; free course coupons</title>"
        f"<style>{CSS}</style></head><body>"
        '<div class="top"><div class="logo">$0</div>'
        '<div class="brand">Zero<span>Tuition</span></div>'
        f"{search}</div>"
        '<div class="layout">'
        f"{sidebar}"
        '<main class="main">'
        f"{scan_panel}{statbar}{listing}"
        '<div class="foot">ZeroTuition v' + esc(VERSION) +
        " &middot; fork of techtanic&#39;s Discounted-Udemy-Course-Enroller "
        "(AGPL-3.0) &middot; coupons maintained by third-party sites</div>"
        "</main></div></body></html>"
    )


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------


def scheduler():
    time.sleep(1)
    request_scan()
    next_run = time.monotonic() + SCAN_INTERVAL_SECONDS
    while True:
        time.sleep(1)
        if time.monotonic() >= next_run:
            request_scan()
            next_run = time.monotonic() + SCAN_INTERVAL_SECONDS


app = FastAPI(title="ZeroTuition")
_last_scan = None
_scan_duration = 0


def _live_sites():
    return [dict(v, site=s) for s, v in _live.items()]


@app.get("/api/courses")
def api_courses():
    with db_lock:
        rows = [dict(r) for r in db.execute(
            "SELECT url,title,site,coupon_code,image,language,is_paid,price,verified "
            "FROM courses ORDER BY first_seen DESC LIMIT 1000"
        )]
        total = db.execute("SELECT COUNT(*) c FROM courses").fetchone()["c"]
    return {"courses": rows, "total": total, "last_scan": _last_scan,
            "version": VERSION}


@app.post("/scan")
def scan_post():
    request_scan()
    return RedirectResponse("/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def index(q: str = "", site: str = "", lang: int = 1, ver: int = 1, paid: int = 1):
    return HTMLResponse(
        content=render_page(q, site, lang, ver, paid),
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    init_db()
    threading.Thread(target=_enrich_worker, daemon=True).start()
    threading.Thread(target=_verify_worker, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    logger.info(f"ZeroTuition web server starting on http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
