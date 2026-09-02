import concurrent
import inspect
import json
import os
import random
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal
from html import unescape
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlsplit, urlunparse

import cloudscraper
import requests

try:
    import rookiepy
except ImportError:  # no wheel for this python yet - only browser-cookie login needs it
    rookiepy = None
from bs4 import BeautifulSoup as bs
from loguru import logger
from rich import print
from rich.traceback import install as rich_traceback_install

rich_traceback_install()

VERSION = "v1.2.1"


def get_user_data_path(filename):
    """Get the path for user data files """
    if getattr(sys, 'frozen', False):
        # If running as PyInstaller exe, put user data files next to the executable
        return os.path.join(os.path.dirname(sys.executable), filename)
    else:
        # If running as script, put files in current directory
        return os.path.join(os.path.abspath("."), filename)


# if getattr(sys, 'frozen', False):
#     # PyInstaller creates a temp folder and stores path in _MEIPASS
#     if hasattr(sys, '_MEIPASS'):
#         bundle_dir = sys._MEIPASS
#     else:
#         bundle_dir = os.path.dirname(sys.executable)
#     os.chdir(bundle_dir)

log_file_path = get_user_data_path("zerotuition.log")


logger.remove()
logger.add(log_file_path, rotation="10 MB", level="INFO", mode="w")
logger.info(f"Program started - {VERSION}")

scraper_dict: dict = {
    "Real Discount": "rd",
    "Courson": "cxyz",
    "IDownloadCoupons": "idc",
    "Tutorial Bar": "tb",
    "E-next": "en",
    "Discudemy": "du",
    "Udemy Freebies": "uf",
    "Course Joiner": "cj",
    "Course Vania": "cv",
    "Coupon Scorpion": "cs",
    "UdemyXpert": "ux",
    "ScrollCoupons": "sc",
}

LINKS = {
    "github": "https://github.com/techtanic/Discounted-Udemy-Course-Enroller",
    "support": "https://techtanic.github.io/duce/support",
    "discord": "https://discord.gg/wFsfhJh4Rh",
}


class LoginException(Exception):
    """Login Error

    Args:
        Exception (str): Exception Reason
    """

    pass


def resource_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


class Course:
    def __init__(self, title: str, url: str, site: str = None):
        self.title = title
        self.site = site
        self.url = None
        self.slug = None
        self.course_id = None
        self.coupon_code = None
        self.is_coupon_valid = False

        self.is_free = False
        self.is_valid = True
        self.is_excluded = False

        self.price = None
        self.instructors = []
        self.language = None
        self.category = None
        self.rating = None
        self.last_update = None

        self.retry = False
        self.retry_after = None
        self.ready_time = None
        self.error: str = None
        self.set_url(url)
        self.extract_coupon_code()

    def set_url(self, url: str):
        """Set course URL and normalize it"""
        self.url = self.normalize_link(url)
        self.set_slug()

    @staticmethod
    def normalize_link(url: str) -> str:
        parsed_url = urlparse(url)
        path = (
            parsed_url.path if parsed_url.path.endswith("/") else parsed_url.path + "/"
        )
        return urlunparse(
            (
                parsed_url.scheme,
                parsed_url.netloc,
                path,
                parsed_url.params,
                parsed_url.query,
                parsed_url.fragment,
            )
        )

    def set_slug(self):
        """Set course slug from URL"""
        parsed_url = urlparse(self.url)
        path_parts = parsed_url.path.split("/")
        if len(path_parts) > 2 and path_parts[1] == "course":
            slug = path_parts[2]
        elif len(path_parts) > 1:
            slug = path_parts[1]
        else:
            logger.error(f"Invalid URL format: {self.url}")
            slug = None
        logger.debug(f"Course slug: {slug}")
        self.slug = slug

    def extract_coupon_code(self):
        """Extract coupon code from URL if present"""
        params = parse_qs(urlsplit(self.url).query)
        self.coupon_code = params.get("couponCode", [None])[0]

    def __str__(self):
        return f"{self.title} - {self.url}"

    def set_metadata(self, dma):
        """Set course metadata from the data-module-args JSON"""
        try:

            if dma.get("view_restriction"):
                self.is_valid = False
                self.error = dma["serverSideProps"]["limitedAccess"]["errorMessage"][
                    "title"
                ]
                return
            course_data = dma["serverSideProps"]["course"]
            self.instructors = [
                i["absolute_url"].split("/")[-2]
                for i in course_data["instructors"]["instructors_info"]
                if i["absolute_url"]
            ]
            self.language = course_data["localeSimpleEnglishTitle"]
            self.category = dma["serverSideProps"]["topicMenu"]["breadcrumbs"][0][
                "title"
            ]
            self.rating = course_data["rating"]
            self.last_update = course_data["lastUpdateDate"]
            self.is_free = not course_data.get("isPaid", True)

        except Exception as e:
            traceback.print_exc()
            self.is_valid = False
            self.error = f"Error parsing course metadata: {str(e)}"

    def __eq__(self, other):
        if not isinstance(other, Course):
            return False
        return self.url == other.url

    def __hash__(self):
        return hash(self.url)


class Scraper:
    """
    Scrapers: RD, CXYZ, IDC, TB, EN, DU, UF, CJ, CV, CS, UX, SC
    """

    def __init__(
        self,
        site_to_scrape: list = list(scraper_dict.keys()),
        debug: bool = False,
    ):
        self.sites = site_to_scrape
        self.debug = debug
        for site in self.sites:
            code_name = scraper_dict[site]
            setattr(self, f"{code_name}_length", 0)
            setattr(self, f"{code_name}_data", [])
            setattr(self, f"{code_name}_done", False)
            setattr(self, f"{code_name}_progress", 0)
            setattr(self, f"{code_name}_error", "")

    def get_scraped_courses(self, target: object) -> dict:
        logger.info(f"Starting scrape for sites: {self.sites}")
        threads = []
        scraped_data = set()
        for site in self.sites:
            logger.info(f"Scraping site: {site}")
            t = threading.Thread(
                target=target,
                args=(site,),
                daemon=True,
            )
            t.start()
            threads.append(t)
            time.sleep(0.2)
        for t in threads:
            t.join()
        logger.info("All scraping threads completed, combining results")
        for site in self.sites:
            courses: list[Course] = getattr(self, f"{scraper_dict[site]}_data")

            for course in courses:
                course.site = site
                scraped_data.add(course)

        logger.info(f"Scraping finished. Found {len(scraped_data)} unique courses.")
        return list(scraped_data)

    def append_to_list(self, title: str, link: str):
        target = getattr(self, f"{inspect.stack()[1].function}_data")
        course = Course(title, link)
        target.append(course)

    def fetch_page(self, url: str, headers: dict = None) -> requests.Response:
        for _ in range(3):
            try:
                return requests.get(url, headers=headers, timeout=(30, 30))
            except requests.exceptions.ConnectionError as e:
                logger.error(f"Connection error occurred: {url} - {str(e)}")
                time.sleep(3.5)
            except requests.exceptions.Timeout as e:
                logger.error(f"Timeout error occurred: {url} - {str(e)}")
                time.sleep(3.5)
            except requests.exceptions.SSLError as e:
                logger.error(f"SSL error occurred: {url} - {str(e)}")
                time.sleep(3.5)
    def fetch_json(self, url: str, headers: dict = None):
        """Fetch a URL and return parsed JSON, retrying bad responses"""
        for _ in range(3):
            try:
                r = self.fetch_page(url, headers=headers)
            except Exception as e:
                logger.error(f"Error fetching: {url} - {e}")
                time.sleep(2)
                continue
            if r is not None and r.status_code == 200:
                try:
                    return r.json()
                except requests.exceptions.JSONDecodeError:
                    logger.error(f"Invalid JSON response from: {url}")
            else:
                logger.error(f"Bad response from: {url}")
            time.sleep(2)
        return None

    def parse_html(self, content: str):
        return bs(content, "lxml")

    def set_attr(self, attr: str, value):
        site_code = inspect.stack()[1].function
        setattr(self, f"{site_code}_{attr}", value)

    def handle_exception(self):
        logger.exception("An error occurred")
        site_code = inspect.stack()[1].function
        error_trace = traceback.format_exc()
        setattr(self, f"{site_code}_error", error_trace)
        setattr(self, f"{site_code}_length", -1)
        setattr(self, f"{site_code}_done", True)

    def cleanup_link(self, link: str) -> str:
        parsed_url = urlparse(link)

        if parsed_url.netloc.lower() == "www.udemy.com" or parsed_url.netloc.lower() == "udemy.com":
            return link

        if parsed_url.netloc == "click.linksynergy.com":
            query_params = parse_qs(parsed_url.query)

            if "RD_PARM1" in query_params:
                return unquote(query_params["RD_PARM1"][0])
            elif "murl" in query_params:
                return unquote(query_params["murl"][0])
            else:
                logger.error(f"Unknown link format: {link}")
                return ""

        if parsed_url.netloc == "trk.udemy.com":
            query_params = parse_qs(parsed_url.query)

            if "u" in query_params:
                return unquote(query_params["u"][0])
            logger.error(f"Unknown link format: {link}")
            return ""

        raise ValueError(f"Unknown link format: {link}")

    def du(self):
        try:
            all_items = []
            head = {
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.159 Safari/537.36 Edg/92.0.902.84",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
                "referer": "https://www.discudemy.com",
            }

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_page = [
                    executor.submit(
                        self.fetch_page,
                        f"https://www.discudemy.com/all/{page}",
                        headers=head,
                    )
                    for page in range(1, 11)
                ]
                self.set_attr("length", 11)

                for i, future in enumerate(
                    concurrent.futures.as_completed(future_page)
                ):
                    response = future.result()
                    if response is None:
                        self.set_attr("progress", i + 1)
                        continue
                    content = response.content
                    soup = self.parse_html(content)
                    page_items = soup.find_all("a", {"class": "card-header"})
                    all_items.extend(page_items)
                    self.set_attr("progress", i + 1)

                self.set_attr("length", len(all_items))

            def _fetch_course_details(item, headers):
                """Helper method to fetch course details"""
                title = item.string
                url = item["href"].split("/")[-1]
                response = self.fetch_page(
                    f"https://www.discudemy.com/go/{url}", headers=headers
                )
                if response is None:
                    return None, None
                soup = self.parse_html(response.content)
                link = soup.find("div", {"class": "ui segment"}).a["href"]
                return title, link

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_course_details = [
                    executor.submit(_fetch_course_details, item, head)
                    for item in all_items
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_course_details)
                ):
                    try:
                        title, link = future.result()
                        if "udemy.com" in link:
                            link = self.cleanup_link(link)
                            self.append_to_list(title, link)
                    except Exception as e:
                        logger.error(f"Skipping invalid course: {e}")
                    self.set_attr("progress", i + 1)

        except:
            self.handle_exception()
        self.set_attr("done", True)

    def uf(self):
        try:
            all_items = []

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                self.set_attr("length", 5)
                future_page = [
                    executor.submit(
                        self.fetch_page,
                        f"https://www.udemyfreebies.com/free-udemy-courses/{page}",
                    )
                    for page in range(1, 6)
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_page)
                ):
                    response = future.result()
                    if response is None:
                        self.set_attr("progress", i + 1)
                        continue
                    content = response.content
                    soup = self.parse_html(content)
                    page_items = soup.find_all("a", {"class": "theme-img"})
                    all_items.extend(page_items)
                    self.set_attr("progress", i + 1)

            def _fetch_course_details(item):
                """Helper method to fetch course details"""
                title = item.img["alt"]
                link = requests.get(
                    f"https://www.udemyfreebies.com/out/{item['href'].split('/')[4]}"
                ).url
                return title, link

            self.set_attr("length", len(all_items))

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_course_details = [
                    executor.submit(_fetch_course_details, item) for item in all_items
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_course_details)
                ):
                    try:
                        title, link = future.result()
                        if "udemy.com" in link:
                            link = self.cleanup_link(link)
                            self.append_to_list(title, link)
                        else:
                            logger.error(f"Unknown link format: {link}")
                    except Exception as e:
                        logger.error(f"Skipping invalid course: {e}")
                    self.set_attr("progress", i + 1)
        except:
            self.handle_exception()
        self.set_attr("done", True)

    def tb(self):
        """Tutorial Bar is a Next.js app; coupon data ships inside its flight JSON"""
        try:
            response = self.fetch_page("https://tutorialbar.com/live-coupons")
            if response is None:
                self.set_attr("error", "Failed to download tutorialbar.com")
                self.set_attr("length", -1)
                self.set_attr("done", True)
                return
            content = response.text

            title_positions = [
                (m.start(), m.group(1))
                for m in re.finditer(r'\\"title\\":\\"(.{5,200}?)\\"', content)
            ]
            coupon_pattern = re.compile(
                r'\\"couponUrl\\":\\"(https://www\.udemy\.com/[^"\\]+)\\"'
            )

            seen = set()
            items = []
            for m in coupon_pattern.finditer(content):
                tail = content[m.end() : m.end() + 300]
                if 'is100PercentOff\\":true' not in tail:
                    continue
                if 'isExpired\\":true' in tail:
                    continue
                title = ""
                for pos, candidate in title_positions:
                    if pos < m.start():
                        title = candidate
                    else:
                        break
                link = m.group(1)
                if link in seen:
                    continue
                seen.add(link)
                items.append((title, link))

            self.set_attr("length", len(items))
            for i, (title, link) in enumerate(items):
                title = re.sub(
                    r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), title
                )
                title = title.replace('\\"', '"').replace("\\/", "/").strip()
                self.append_to_list(title, link)
                self.set_attr("progress", i + 1)
        except:
            self.handle_exception()
        self.set_attr("done", True)

    def rd(self):
        try:
            all_items = []
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.159 Safari/537.36 Edg/92.0.902.84",
                "Host": "cdn.real.discount",
                "Connection": "Keep-Alive",
                "dnt": "1",
                "referer": "https://www.real.discount/",
            }
            try:
                r = requests.get(
                    "https://cdn.real.discount/api/courses?page=1&limit=500&sortBy=sale_start&store=Udemy&freeOnly=true",
                    headers=headers,
                    timeout=(10, 30),
                ).json()
            except requests.exceptions.Timeout:
                self.set_attr("error", "Timeout")
                self.set_attr("length", -1)
                self.set_attr("done", True)
                return
            all_items.extend(r["items"])

            self.set_attr("length", len(all_items))
            for index, item in enumerate(all_items):
                self.set_attr("progress", index)
                if item["store"] == "Sponsored":
                    continue
                title: str = item["name"]
                link: str = item["url"]
                link = self.cleanup_link(link)
                if link:
                    self.append_to_list(title, link)

        except:
            self.handle_exception()
        self.set_attr("done", True)

    def cv(self):
        try:
            response = self.fetch_page("https://coursevania.com/courses/")
            if response is None:
                self.set_attr("error", "Failed to download coursevania.com")
                self.set_attr("length", -1)
                self.set_attr("done", True)
                return
            content = response.content

            try:
                nonce = re.search(
                    r"load_content\"\:\"(.*?)\"", content.decode("utf-8"), re.DOTALL
                ).group(1)
                logger.debug(f"Nonce: {nonce}")
            except IndexError:
                self.set_attr("error", "Nonce not found")
                self.set_attr("length", -1)
                self.set_attr("done", True)
                return
            r = requests.get(
                "https://coursevania.com/wp-admin/admin-ajax.php?&template=courses/grid&args={%22posts_per_page%22:%22500%22}&action=stm_lms_load_content&sort=date_high&nonce="
                + nonce
            ).json()

            soup = self.parse_html(r["content"])
            page_items = soup.find_all(
                "div", {"class": "stm_lms_courses__single--title"}
            )

            self.set_attr("length", len(page_items))

            def _fetch_course_details(item):
                """Helper method to fetch course details"""
                title = item.h5.string
                response = self.fetch_page(item.a["href"])
                if response is None:
                    return None, None
                soup = self.parse_html(response.content)
                link = soup.find(
                    "a",
                    {"class": "masterstudy-button-affiliate__link"},
                )["href"]
                return title, link

            with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
                future_course_details = [
                    executor.submit(_fetch_course_details, item) for item in page_items
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_course_details)
                ):
                    try:
                        title, link = future.result()
                        if "udemy.com" in link:
                            link = self.cleanup_link(link)
                            self.append_to_list(title, link)
                        else:
                            logger.error(f"Unknown link format: {link}")
                    except Exception as e:
                        logger.error(f"Skipping invalid course: {e}")
                    self.set_attr("progress", i + 1)
        except:
            self.handle_exception()
        self.set_attr("done", True)

    def idc(self):
        try:
            all_items = []
            self.set_attr("length", 3)

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_page = [
                    executor.submit(
                        self.fetch_json,
                        f"https://idownloadcoupon.com/wp-json/wp/v2/product?product_cat=15&per_page=100&page={page}",
                    )
                    for page in range(1, 4)
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_page)
                ):
                    try:
                        content = future.result()
                    except Exception as e:
                        logger.error(f"Skipping invalid response: {e}")
                        self.set_attr("progress", i + 1)
                        continue
                    if content:
                        all_items.extend(content)
                    self.set_attr("progress", i + 1)

            self.set_attr("length", len(all_items))

            def _fetch_course_details(item):
                """Helper method to fetch course details"""
                title = item["title"]["rendered"]
                link_num = item["id"]
                if link_num in [85, 81]:
                    return None, None
                link = f"https://idownloadcoupon.com/udemy/{link_num}/"
                r = requests.get(
                    link,
                    allow_redirects=False,
                )
                if "comidoc.net" in link or "comidoc.com" in link:
                    logger.info("Comidoc link: " + link)
                    return None, None
                try:
                    link = unquote(r.headers["Location"])
                except KeyError:
                    logger.error(f"No Location header found for {link}")
                    return None, None
                if  "comidoc.com" in link:
                    logger.info("Comidoc link: " + link)
                    return None, None
                link = self.cleanup_link(link)
                return title, link

            with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
                future_course_details = [
                    executor.submit(_fetch_course_details, item) for item in all_items
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_course_details)
                ):
                    try:
                        title, link = future.result()
                    except Exception as e:
                        logger.error(f"Skipping invalid course: {e}")
                        self.set_attr("progress", i + 1)
                        continue
                    if title and link:
                        self.append_to_list(title, link)
                    self.set_attr("progress", i + 1)
        except:
            self.handle_exception()
        self.set_attr("done", True)

    def en(self):
        try:
            all_items = []
            self.set_attr("length", 5)
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_page = [
                    executor.submit(
                        self.fetch_page,
                        f"https://jobs.e-next.in/course/udemy/{page}",
                    )
                    for page in range(1, 6)
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_page)
                ):
                    response = future.result()
                    if response is None:
                        self.set_attr("progress", i + 1)
                        continue
                    soup = self.parse_html(response.content)
                    page_items = soup.find_all(
                        "a", {"class": "btn btn-secondary btn-sm btn-block"}
                    )
                    all_items.extend(page_items)
                    self.set_attr("progress", i + 1)

            self.set_attr("length", len(all_items))
            with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
                future_course_details = [
                    executor.submit(
                        self.fetch_page,
                        item["href"],
                    )
                    for item in all_items
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_course_details)
                ):
                    try:
                        response = future.result()
                        if response is None:
                            self.set_attr("progress", i + 1)
                            continue
                        soup = self.parse_html(response.content)
                        title = soup.find("h3").string.strip()
                        try:
                            link = soup.find("a", {"class": "btn btn-primary"})["href"]
                        except:
                            with open("error.html", "w", encoding="utf-8") as f:
                                f.write(soup.prettify())
                        self.append_to_list(title, link)
                    except Exception as e:
                        logger.error(f"Skipping invalid course: {e}")
                    self.set_attr("progress", i + 1)
        except:
            self.handle_exception()
        self.set_attr("done", True)

    def cj(self):
        try:
            self.set_attr("length", 4)

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                # 'Accept-Encoding': 'gzip, deflate, br, zstd',
                "DNT": "1",
                "Sec-GPC": "1",
                "Alt-Used": "www.coursejoiner.com",
                "Connection": "keep-alive",
                # 'Cookie': 'ezosuibasgeneris-1=f7de3a73-8edf-4957-6bd7-c03d6192a105; cf_clearance=Vo1nMPpI9BOvaSvzT1RuuxxHQDU.SjH0Gvy_1Q5A8eA-1745306395-1.2.1.1-a7L2AgL7rcy4jHX.whQY0bjrjQwiz78KBWIOzX6_b8wBevOqdlK5yNLXDzSk1KJao2pu7ogq5pFL.TfdYmOQY3hz5c3Zk8BvRZVu0fyENuYVk1PNX.Q.UswXoe.LOSzsPpOBzySIOo5frr2Wv.ez2dE9GvPfPKG_a3WgmI.da5J94k2bQrs2w5tGdPlZgBNNuXlln_g9hIWQf8FNPXNjYQajWhZMZRJEqrwN6J8axTX8InJ_Fpt4wJaP6AvwcE28Lw6sgnWHLjVlrSdW9u.ZmTXvB7rDVVF5fKTSydwn5v0iI_4ch8TQPx6gFD_JHdnhTuVyzp64J.cKe1Uh53n_.DbRv8sCkUP9lfl_I2VGlog; ezoictest=stable; ezopvc_664594=1; ezoab_664594=mod24-c; active_template::664594=pub_site.1745306394; ezoadgid_664594=-1; wssplashchk=c03df4b443bf0a1a0365c55282e792b435f0599b.1745309995.1',
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
                "Priority": "u=4",
                "Pragma": "no-cache",
                "Cache-Control": "no-cache",
                # Requests doesn't support trailers
                # 'TE': 'trailers',
            }
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                future_page = [
                    executor.submit(
                        self.fetch_json,
                        f"https://www.coursejoiner.com/wp-json/wp/v2/posts?categories=74&per_page=100&page={page}",
                        headers=headers,
                    )
                    for page in range(1, 5)
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_page)
                ):
                    try:
                        content = future.result()
                    except Exception as e:
                        logger.error(f"Skipping invalid response: {e}")
                        self.set_attr("progress", i + 1)
                        continue
                    if not content:
                        logger.debug("No more coupons")
                        break
                    for item in content:
                        title = unescape(item["title"]["rendered"])
                        title = (
                            title.replace("–", "-")
                            .strip()
                            .removesuffix("- (Free Course)")
                            .strip()
                        )
                        rendered_content = item["content"]["rendered"]
                        soup = self.parse_html(rendered_content)
                        link = soup.find("a", string="APPLY HERE")

                        if link and link.has_attr("href"):
                            link = link["href"]
                            if "udemy.com" in link:
                                self.append_to_list(title, link)
                    self.set_attr("progress", i + 1)

        except:
            self.handle_exception()
        self.set_attr("done", True)

    def cxyz(self):
        try:
            self.set_attr("length", 10)
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_page = [
                    executor.submit(
                        requests.post,
                        f"https://courson.xyz/load-more-coupons",
                        json={"filters": {}, "offset": (page - 1) * 30},
                    )
                    for page in range(1, 11)
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_page)
                ):
                    try:
                        content = future.result().json()["coupons"]
                    except Exception as e:
                        logger.error(f"Skipping invalid response: {e}")
                        self.set_attr("progress", i + 1)
                        continue
                    if not content:
                        logger.debug("No more coupons")
                        break
                    for item in content:
                        title = item["headline"].strip(' "')
                        link = f"https://www.udemy.com/course/{item['id_name']}/?couponCode={item['coupon_code']}"
                        self.append_to_list(title, link)
                    self.set_attr("progress", i + 1)
        except:
            self.handle_exception()

        self.set_attr("done", True)

    def cs(self):
        """Coupon Scorpion: WP API for the post list, out.php redirect for the link"""
        try:
            all_items = []
            head = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "referer": "https://couponscorpion.com/",
            }

            # only the newest page is worth fetching - coupons here die within
            # days and out.php starts serving a generic fallback for dead ones
            self.set_attr("length", 1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future_page = [
                    executor.submit(
                        self.fetch_json,
                        f"https://couponscorpion.com/wp-json/wp/v2/posts?categories=21032&per_page=100&page={page}",
                        headers=head,
                    )
                    for page in range(1, 2)
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_page)
                ):
                    try:
                        content = future.result()
                    except Exception as e:
                        logger.error(f"Skipping invalid response: {e}")
                        self.set_attr("progress", i + 1)
                        continue
                    if content:
                        all_items.extend(content)
                    self.set_attr("progress", i + 1)

            self.set_attr("length", len(all_items))

            def _fetch_course_details(item):
                """Helper method to fetch course details"""
                title = unescape(item["title"]["rendered"]).strip()
                response = self.fetch_page(item["link"], headers=head)
                if response is None:
                    return None, None
                match = re.search(
                    r"scripts/udemy/out\.php\?go=\d+&s=[a-f0-9]+", response.text
                )
                if not match:
                    logger.error(f"No coupon link found in: {item['link']}")
                    return None, None
                r = requests.get(
                    f"https://couponscorpion.com/{match.group(0)}",
                    headers=head,
                    allow_redirects=False,
                )
                try:
                    link = r.headers["Location"]
                except KeyError:
                    logger.error(f"No redirect for: {item['link']}")
                    return None, None
                if "recommends/link/" in link:
                    # expired coupon or the site throttling us - same fallback
                    return title, None
                link = self.cleanup_link(link)
                return title, link

            consecutive_fallbacks = 0
            got_valid_link = False
            stop = False
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                future_course_details = [
                    executor.submit(_fetch_course_details, item) for item in all_items
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_course_details)
                ):
                    try:
                        title, link = future.result()
                    except Exception as e:
                        logger.error(f"Skipping invalid course: {e}")
                        self.set_attr("progress", i + 1)
                        continue
                    if link is None:
                        # every result being a fallback means we are throttled,
                        # not that every coupon died - bail out before the site
                        # hardens the block
                        consecutive_fallbacks += 1
                        if consecutive_fallbacks >= 10 and not got_valid_link:
                            logger.warning(
                                "Coupon Scorpion is serving fallback redirects - "
                                "stopping early to avoid a hard block"
                            )
                            stop = True
                            for f in future_course_details:
                                f.cancel()
                    else:
                        consecutive_fallbacks = 0
                        got_valid_link = True
                        if title:
                            self.append_to_list(title, link)
                    if stop:
                        break
                    self.set_attr("progress", i + 1)
        except:
            self.handle_exception()
        self.set_attr("done", True)

    def ux(self):
        """UdemyXpert: listing pages give course slugs, detail pages hold the link"""
        try:
            all_items = []

            self.set_attr("length", 5)
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_page = [
                    executor.submit(
                        self.fetch_page, f"https://udemyxpert.com/courses?page={page}"
                    )
                    for page in range(1, 6)
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_page)
                ):
                    response = future.result()
                    if response is None:
                        self.set_attr("progress", i + 1)
                        continue
                    soup = self.parse_html(response.content)
                    page_items = soup.find_all("a", {"class": "course-card-link"})
                    for item in page_items:
                        try:
                            title = item.img["alt"]
                            href = urljoin("https://udemyxpert.com/", item["href"])
                            all_items.append((title, href))
                        except (KeyError, TypeError):
                            continue
                    self.set_attr("progress", i + 1)

            self.set_attr("length", len(all_items))

            def _fetch_course_details(item):
                """Helper method to fetch course details"""
                title, href = item
                response = self.fetch_page(href)
                if response is None:
                    return None, None
                soup = self.parse_html(response.content)
                link = soup.find(
                    "a", href=re.compile(r"https://(www\.)?udemy\.com/")
                )["href"]
                if "couponCode" not in link:
                    raise ValueError(f"No couponCode in link: {link}")
                link = self.cleanup_link(link)
                return title, link

            with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
                future_course_details = [
                    executor.submit(_fetch_course_details, item) for item in all_items
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_course_details)
                ):
                    try:
                        title, link = future.result()
                    except Exception as e:
                        logger.error(f"Skipping invalid course: {e}")
                        self.set_attr("progress", i + 1)
                        continue
                    if title and link:
                        self.append_to_list(title, link)
                    self.set_attr("progress", i + 1)
        except:
            self.handle_exception()
        self.set_attr("done", True)

    def sc(self):
        """ScrollCoupons: 100%-off listing pages, deal_btn link on each course page"""
        try:
            all_items = []

            self.set_attr("length", 3)
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                future_page = [
                    executor.submit(
                        self.fetch_page,
                        f"https://scrollcoupons.com/store/udemy/100-off/{page}",
                    )
                    for page in range(1, 4)
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_page)
                ):
                    response = future.result()
                    if response is None:
                        self.set_attr("progress", i + 1)
                        continue
                    soup = self.parse_html(response.content)
                    page_items = soup.find_all("a", {"class": "a_udemy_course"})
                    for item in page_items:
                        try:
                            title = item["title"]
                            href = urljoin("https://scrollcoupons.com/", item["href"])
                            all_items.append((title, href))
                        except (KeyError, TypeError):
                            continue
                    self.set_attr("progress", i + 1)

            self.set_attr("length", len(all_items))

            def _fetch_course_details(item):
                """Helper method to fetch course details"""
                title, href = item
                response = self.fetch_page(href)
                if response is None:
                    return None, None
                soup = self.parse_html(response.content)
                link = soup.find("a", {"class": "deal_btn"})["href"]
                if "udemy.com" not in link:
                    raise ValueError(f"Unknown link format: {link}")
                link = self.cleanup_link(link)
                return title, link

            with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
                future_course_details = [
                    executor.submit(_fetch_course_details, item) for item in all_items
                ]
                for i, future in enumerate(
                    concurrent.futures.as_completed(future_course_details)
                ):
                    try:
                        title, link = future.result()
                    except Exception as e:
                        logger.error(f"Skipping invalid course: {e}")
                        self.set_attr("progress", i + 1)
                        continue
                    if title and link:
                        self.append_to_list(title, link)
                    self.set_attr("progress", i + 1)
        except:
            self.handle_exception()
        self.set_attr("done", True)


class Udemy:
    def __init__(self, interface: str, debug: bool = False):
        self.interface = interface
        # self.client = cloudscraper.CloudScraper()
        self.client = requests.session()
        headers = {
            "User-Agent": "okhttp/4.9.2 UdemyAndroid 8.9.2(499) (phone)",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en;q=0.5",
            "Referer": "https://www.udemy.com/",
            "X-Requested-With": "XMLHttpRequest",
            "DNT": "1",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }

        self.client.headers.update(headers)
        self.debug = debug

        self.successfully_enrolled_c = 0
        self.already_enrolled_c = 0
        self.expired_c = 0
        self.excluded_c = 0
        self.failed_c = 0
        self.amount_saved_c = Decimal(0)

        self.course: Course = None

        # Log program start
        logger.info(f"Program started - {self.interface} mode")

    def print(self, content: str, color: str = "red", **kargs):
        content = str(content)

        with open("log.txt", "a", encoding="utf-8") as f:
            if kargs.get("end") == None:
                f.write(content + "\n")
            else:
                f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if self.debug:
            print(
                content,
                **kargs,
            )

    def get_date_from_utc(self, d: str):
        utc_dt = datetime.strptime(d, "%Y-%m-%dT%H:%M:%SZ")
        dt = utc_dt.replace(tzinfo=timezone.utc).astimezone(tz=None)
        return dt.strftime("%B %d, %Y")

    def get_now_to_utc(self):
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    def load_settings(self):
        settings_file = get_user_data_path(f"zerotuition-{self.interface}-settings.json")
        try:
            with open(settings_file) as f:
                self.settings = json.load(f)
        except FileNotFoundError:
            with open(
                resource_path(f"default-zerotuition-{self.interface}-settings.json")
            ) as f:
                self.settings = json.load(f)
        if (
            self.interface == "cli" and "use_browser_cookies" not in self.settings
        ):  # v2.1
            self.settings.get("use_browser_cookies", False)
        # v2.2
        if "course_update_threshold_months" not in self.settings:
            self.settings["course_update_threshold_months"] = 24  # 2 years

        # v2.3.3

        if "Vietnamese" not in self.settings["languages"]:
            self.settings["languages"]["Vietnamese"] = True

        for site in scraper_dict:
            if site not in self.settings["sites"]:
                self.settings["sites"][site] = True

        self.settings["languages"] = dict(
            sorted(self.settings["languages"].items(), key=lambda item: item[0])
        )
        self.save_settings()
        self.title_exclude = "\n".join(self.settings["title_exclude"])
        self.instructor_exclude = "\n".join(self.settings["instructor_exclude"])

    def save_settings(self):
        settings_file = get_user_data_path(f"zerotuition-{self.interface}-settings.json")
        with open(settings_file, "w") as f:
            json.dump(self.settings, f, indent=4)

    def compare_versions(self, version1, version2):
        v1_parts = list(map(int, version1.split(".")))
        v2_parts = list(map(int, version2.split(".")))
        max_length = max(len(v1_parts), len(v2_parts))
        v1_parts.extend([0] * (max_length - len(v1_parts)))
        v2_parts.extend([0] * (max_length - len(v2_parts)))

        for v1, v2 in zip(v1_parts, v2_parts):
            if v1 < v2:
                return -1
            elif v1 > v2:
                return 1
        return 0

    def check_for_update(self) -> tuple[str, str]:
        """Returns window titles; logs if the upstream project has a newer release."""
        logger.info("Checking for updates...")
        c_version = VERSION.removeprefix("v")
        try:
            r = requests.get(
                "https://api.github.com/repos/techtanic/Discounted-Udemy-Course-Enroller/releases/latest",
                timeout=15,
            )
            if r.status_code == 200:
                latest = r.json()["tag_name"].removeprefix("v")
                logger.info(
                    f"ZeroTuition version: {c_version}, upstream DUCE version: {latest}"
                )
                if self.compare_versions(c_version, latest) == -1:
                    logger.info(
                        "The upstream project has a newer release - consider "
                        "porting its fixes"
                    )
            else:
                logger.error("Failed to fetch latest version info")
        except Exception as e:
            logger.warning(f"Update check failed: {e}")
        return (f"Login {c_version}", f"ZeroTuition {c_version}")

    # Authentication and session management
    def make_cookies(self, client_id: str, access_token: str, csrf_token: str):
        self.cookie_dict = dict(
            client_id=client_id,
            access_token=access_token,
            csrf_token=csrf_token,
        )

    def save_session(self):
        """Persist login cookies so future runs can skip the login flow."""
        try:
            path = get_user_data_path(
                f"zerotuition-{self.interface}-session.json"
            )
            with open(path, "w") as f:
                json.dump(self.cookie_dict, f, indent=2)
        except Exception:
            logger.exception("Failed to save session")

    def load_saved_session(self) -> bool:
        """Restore a previously saved session. Returns True if found."""
        try:
            path = get_user_data_path(
                f"zerotuition-{self.interface}-session.json"
            )
            with open(path) as f:
                self.cookie_dict = json.load(f)
            return True
        except Exception:
            return False

    def clear_saved_session(self):
        try:
            path = get_user_data_path(
                f"zerotuition-{self.interface}-session.json"
            )
            os.remove(path)
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Failed to clear saved session")

    def fetch_cookies(self):
        """Gets cookies from browser
        Sets cookies_dict, cookie_jar
        """
        if rookiepy is None:
            raise LoginException(
                "Browser cookies package not installed for this Python version - "
                "use manual login instead"
            )
        logger.info("Fetching cookies from browser")
        cookies = rookiepy.to_cookiejar(rookiepy.load(["www.udemy.com"]))
        self.cookie_dict: dict = requests.utils.dict_from_cookiejar(cookies)
        self.cookie_jar = cookies
        logger.info("Cookies fetched")

    def manual_login(self, email: str, password: str):
        """Manual Login to Udemy using email and password and sets cookies
        Args:
            email (str): Email
            password (str): Password
        Raises:
            LoginException: Login Error
        """
        # s = cloudscraper.CloudScraper()
        logger.info("Trying to login with email and password")
        s = requests.session()
        r = s.get(
            "https://www.udemy.com/join/signup-popup/?locale=en_US&response_type=html&next=https%3A%2F%2Fwww.udemy.com%2Flogout%2F",
            headers={"User-Agent": "okhttp/4.9.2 UdemyAndroid 8.9.2(499) (phone)"},
            # headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0",
            #     'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            #     'Accept-Language': 'en-US,en;q=0.5',
            #     #'Accept-Encoding': 'gzip, deflate, br',
            #     'DNT': '1',
            #     'Connection': 'keep-alive',
            #     'Upgrade-Insecure-Requests': '1',
            #     'Sec-Fetch-Dest': 'document',
            #     'Sec-Fetch-Mode': 'navigate',
            #     'Sec-Fetch-Site': 'none',
            #     'Sec-Fetch-User': '?1',
            #     'Pragma': 'no-cache',
            #     'Cache-Control': 'no-cache'},
        )
        try:
            csrf_token = r.cookies["csrftoken"]
        except:
            if self.debug:
                logger.error(r.text)
        data = {
            "csrfmiddlewaretoken": csrf_token,
            "locale": "en_US",
            "email": email,
            "password": password,
        }

        # ss = requests.session()
        s.cookies.update(r.cookies)
        s.headers.update(
            {
                "User-Agent": "okhttp/4.9.2 UdemyAndroid 8.9.2(499) (phone)",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-GB,en;q=0.5",
                "Referer": "https://www.udemy.com/join/login-popup/?passwordredirect=True&response_type=json",
                "Origin": "https://www.udemy.com",
                "DNT": "1",
                "Host": "www.udemy.com",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Pragma": "no-cache",
                "Cache-Control": "no-cache",
            }
        )
        s = cloudscraper.create_scraper(sess=s)
        r = s.post(
            "https://www.udemy.com/join/login-popup/?passwordredirect=True&response_type=json",
            data=data,
            allow_redirects=False,
        )
        if r.text.__contains__("returnUrl"):
            self.make_cookies(
                r.cookies["client_id"], r.cookies["access_token"], csrf_token
            )
        else:
            login_error = r.json()["error"]["data"]["formErrors"][0]
            if login_error[0] == "Y":
                raise LoginException("Too many logins per hour try later")
            elif login_error[0] == "T":
                raise LoginException("Email or password incorrect")
            else:
                raise LoginException(login_error)

    def get_session_info(self):
        """Get Session info
        Sets Client Session, currency and name
        """
        logger.info("Getting session info")
        s = cloudscraper.CloudScraper()
        # headers = {
        #     "authorization": "Bearer " + self.cookie_dict["access_token"],
        #     "accept": "application/json, text/plain, */*",
        #     "x-requested-with": "XMLHttpRequest",
        #     "x-forwarded-for": str(
        #         ".".join(map(str, (random.randint(0, 255) for _ in range(4))))
        #     ),
        #     "x-udemy-authorization": "Bearer " + self.cookie_dict["access_token"],
        #     "content-type": "application/json;charset=UTF-8",
        #     "origin": "https://www.udemy.com",
        #     "referer": "https://www.udemy.com/",
        #     "dnt": "1",
        #     "User-Agent": "okhttp/4.9.2 UdemyAndroid 8.9.2(499) (phone)",
        # }
        headers = {
            "User-Agent": "okhttp/4.9.2 UdemyAndroid 8.9.2(499) (phone)",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-GB,en;q=0.5",
            "Referer": "https://www.udemy.com/",
            "X-Requested-With": "XMLHttpRequest",
            "DNT": "1",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }
        r = s.get(
            "https://www.udemy.com/api-2.0/contexts/me/?header=True",
            cookies=self.cookie_dict,
            headers=headers,
        )
        r = r.json()
        if not r["header"]["isLoggedIn"]:
            logger.error("Login Failed: " + str(r))
            raise LoginException("Login Failed")

        self.display_name: str = r["header"]["user"]["display_name"]
        r = s.get(
            "https://www.udemy.com/api-2.0/shopping-carts/me/",
            headers=headers,
            cookies=self.cookie_dict,
        )
        r = r.json()

        self.currency: str = r["user"]["credit"]["currency_code"]

        s = cloudscraper.CloudScraper()
        s.cookies.update(self.cookie_dict)
        s.headers.update(headers)
        s.keep_alive = False
        self.client = s
        logger.info("Session info retrieved")
        self.get_enrolled_courses()
        self.save_session()

    def get_enrolled_courses(self):
        """Get enrolled courses
        Sets enrolled_courses

        {slug:enrollment_time}
        """
        logger.info("Getting enrolled courses")
        next_page = "https://www.udemy.com/api-2.0/users/me/subscribed-courses/?ordering=-enroll_time&fields[course]=enrollment_time,url&page_size=100"
        courses = {}
        while next_page:
            r = self.client.get(
                next_page,
            )
            try:
                r = r.json()
            except requests.exceptions.JSONDecodeError:
                logger.error(r.text)
                raise Exception("JSON Decode Error in get_enrolled_courses")
            for course in r["results"]:
                slug = course["url"].split("/")[2]
                if slug == "draft":
                    slug = course["url"].split("/")[3]
                courses[slug] = course["enrollment_time"]
            next_page = r["next"]
        self.enrolled_courses = courses
        logger.info(f"Enrolled courses: {len(courses)}")

    # Course filtering and exclusion logic
    def is_keyword_excluded(self) -> bool:
        """Check if the course title contains any excluded keywords"""
        title = self.course.title
        title_words = title.casefold().split()
        for word in title_words:
            word = word.casefold()
            if word in self.title_exclude:
                return True
        return False

    def is_instructor_excluded(self) -> bool:
        """Check if the course instructor is in the excluded list"""
        instructors = self.course.instructors
        for instructor in instructors:
            if instructor in self.settings["instructor_exclude"]:
                return True
        return False

    def is_course_updated(self) -> bool:
        """Check if the course is updated within the threshold months"""
        last_update = self.course.last_update
        if not last_update:
            return True
        current_date = datetime.now()
        last_update_date = datetime.strptime(last_update, "%Y-%m-%d")
        # Calculate the difference in years and months
        years = current_date.year - last_update_date.year
        months = current_date.month - last_update_date.month
        days = current_date.day - last_update_date.day

        # Adjust the months and years if necessary
        if days < 0:
            months -= 1

        if months < 0:
            years -= 1
            months += 12

        # Calculate the total month difference
        month_diff = years * 12 + months
        return month_diff < self.settings["course_update_threshold_months"]

    def is_course_excluded(self):

        if not self.is_course_updated():
            logger.info(f"Course excluded: Last updated {self.course.last_update}")
        elif self.is_instructor_excluded():
            logger.info(f"Instructor excluded: {self.course.instructors[0]}")
        elif self.is_keyword_excluded():
            logger.info("Keyword Excluded")
        elif self.course.category not in self.categories:
            logger.info(f"Category excluded: {self.course.category}")
        elif self.course.language not in self.languages:
            logger.info(f"Language excluded: {self.course.language}")
        elif self.course.rating < self.min_rating:
            logger.info(f"Low rating: {self.course.rating}")
        else:
            return
        self.course.is_excluded = True

    def is_user_dumb(self) -> bool:
        self.sites = []
        for key in scraper_dict:
            if self.settings["sites"].get(key):
                self.sites.append(key)
        self.categories = [
            key for key, value in self.settings["categories"].items() if value
        ]
        self.languages = [
            key for key, value in self.settings["languages"].items() if value
        ]
        self.instructor_exclude = self.settings["instructor_exclude"]
        self.title_exclude = self.settings["title_exclude"]
        self.min_rating = self.settings["min_rating"]
        return not all([bool(self.sites), bool(self.categories), bool(self.languages)])

    # Course data retrieval
    def get_course_id(self):
        """Set course_id and metadata and is_excluded"""
        if self.course.course_id:
            return
        url = re.sub(r"\W+$", "", unquote(self.course.url))
        r = None
        for _ in range(3):
            try:
                r = self.client.get(url)
            except requests.exceptions.ConnectionError:
                r = None
                continue
            except Exception as e:
                logger.error(f"Error fetching course ID: {e}")
                logger.error(f"Course URL: {url}")
                r = None
                continue

        if r is None:
            logger.error("Failed to fetch course ID after 3 attempts")
            self.course.is_valid = False
            self.course.error = "Failed to fetch course ID: Report to developer"
            return

        self.course.set_url(r.url)
        soup = bs(r.content, "lxml")
        course_id = soup.find("body").get("data-clp-course-id", "invalid")
        if course_id == "invalid":
            self.course.is_valid = False
            self.course.error = "Course ID not found: Report to developer"
            return

        self.course.course_id = course_id
        dma = json.loads(soup.find("body")["data-module-args"])
        if self.debug:
            os.makedirs("debug/", exist_ok=True)
            with open("debug/dma.json", "w") as f:
                json.dump(dma, f, indent=4)
        self.course.set_metadata(dma)
        if not self.course.is_valid:
            return
        self.is_course_excluded()

    def check_course(self):
        if self.course.price != None:
            return
        url = f"https://www.udemy.com/api-2.0/course-landing-components/{self.course.course_id}/me/?components=purchase"
        if self.course.coupon_code:
            url += f",redeem_coupon&couponCode={self.course.coupon_code}"
        logger.debug(f"Checking course: {url}")
        for _ in range(3):
            try:
                r = self.client.get(url)
                r = r.json()
                break
            except requests.exceptions.ConnectionError:
                r = None
            except Exception as e:
                logger.error(f"Error fetching course data: {e}")
                logger.error(f"Course ID: {self.course.course_id}")
                logger.error(f"Coupon Code: {self.course.coupon_code}")
                logger.error(f"URL: {url}")
                r = None
        amount = (
            r.get("purchase", {})
            .get("data", {})
            .get("list_price", {})
            .get("amount", None)
        )
        self.course.price = Decimal(str(amount)) if amount is not None else None
        if self.course.price is None:
            logger.error(f"Course not found {self.course.course_id}")
            logger.error("Report to developer")
            raise Exception("Course not found")

        if self.course.coupon_code and "redeem_coupon" in r:
            discount = r["purchase"]["data"]["pricing_result"]["discount_percent"]
            status = r["redeem_coupon"]["discount_attempts"][0]["status"]
            self.course.is_coupon_valid = discount == 100 and status == "applied"

    def save_course(self):
        if self.settings["save_txt"]:
            try:
                self.txt_file.write(f"{str(self.course)}\n")
                self.txt_file.flush()
                os.fsync(self.txt_file.fileno())
            except Exception as e:
                logger.exception(f"Error writing course to file: {e}")

    def is_already_enrolled(self):
        """Check if the course is already enrolled."""
        # Ensure course URL is valid before splitting
        slug = self.course.slug
        if not slug or not isinstance(slug, str):
            logger.error("SLUG NOT FOUND")
            return False
        return slug in self.enrolled_courses

    def start_new_enroll(
        self,
    ):  # no queue, bulk checkout - Now filters courses first, Experimental
        """Filters scraped courses based on validity, settings, and coupon status."""
        logger.info("Starting enrollment process")
        self.setup_txt_file()

        courses: list[Course] = self.scraped_data
        self.total_courses = len(courses)
        self.valid_courses: list[Course] = []
        self.total_courses_processed = 0  # Track progress for UI display

        for index, current_course in enumerate(courses):
            self.course = current_course
            self.total_courses_processed = (
                index + 1
            )  # Update processing counter for UI thread

            logger.info(
                f"Processing course {index + 1} / {self.total_courses}: {str(self.course)}"
            )
            try:
                if self.is_already_enrolled():
                    logger.info(
                        f"Already enrolled on {self.get_date_from_utc(self.enrolled_courses[self.course.slug])}"
                    )
                    self.already_enrolled_c += 1
                else:
                    self.get_course_id()
                    if not self.course.is_valid:
                        logger.error(f"Invalid: {self.course.error}")
                        self.excluded_c += 1

                    elif self.is_already_enrolled():
                        logger.info(
                            f"Already enrolled on {self.get_date_from_utc(self.enrolled_courses[self.course.slug])}"
                        )
                        self.already_enrolled_c += 1
                    elif self.course.is_excluded:
                        self.excluded_c += 1

                    elif self.course.is_free:

                        if self.settings["discounted_only"]:
                            logger.info(
                                "Free course excluded (discounted only setting)",
                                color="light blue",
                            )
                            self.excluded_c += 1
                        else:
                            self.free_checkout()
                            if self.course.status:
                                logger.success("Successfully Subscribed")
                                self.successfully_enrolled_c += 1
                                self.save_course()
                            else:
                                logger.info(
                                    "Unknown Error: Report this link to the developer",
                                )
                                self.expired_c += 1

                    elif not self.course.is_free:
                        self.check_course()
                        if not self.course.is_coupon_valid:
                            logger.info("Coupon Expired")
                            self.expired_c += 1

                    if self.course.is_coupon_valid:
                        self.valid_courses.append(self.course)
                        logger.info("Added for enrollment")

                    self.update_progress()
                    if len(self.valid_courses) >= 5:
                        self.bulk_checkout()
                        self.valid_courses.clear()
            except Exception as e:
                logger.exception(f"Error processing course {self.course}: {e}")
                self.failed_c += 1
            self.update_progress()

        if self.valid_courses:
            self.bulk_checkout()
            self.valid_courses.clear()
        logger.info("Enrollment process completed")
        logger.info(
            f"Successfully Enrolled: {self.successfully_enrolled_c}\nAlready Enrolled: {self.already_enrolled_c}\nExpired: {self.expired_c}\nExcluded: {self.excluded_c}\nFailed: {self.failed_c}"
        )

    def setup_txt_file(self):
        if self.settings["save_txt"]:
            os.makedirs("Courses/", exist_ok=True)
            self.txt_file = open(
                f"Courses/{time.strftime('%Y-%m-%d--%H-%M')}.txt", "w", encoding="utf-8"
            )

    def bulk_checkout(self):
        logger.info("Enrolling in courses...")
        items = []
        for course in self.valid_courses:
            if not course.is_free:
                items.append(
                    {
                        "buyable": {"id": str(course.course_id), "type": "course"},
                        "discountInfo": {"code": course.coupon_code},
                        "price": {"amount": 0, "currency": self.currency.upper()},
                    }
                )
        if not items:
            logger.error("No courses to enroll in")
            return

        payload = {
            "checkout_environment": "Marketplace",
            "checkout_event": "Submit",
            "payment_info": {
                "method_id": "0",
                "payment_method": "free-method",
                "payment_vendor": "Free",
            },
            "shopping_info": {"items": items, "is_cart": True},
        }
        headers = {
            "User-Agent": "okhttp/4.10.0 UdemyAndroid 9.7.0(515) (phone)",
            # "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:119.0) Gecko/20100101 Firefox/119.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US",
            # "Referer": f"https://www.udemy.com/payment/checkout/express/course/{self.course.course_id}/?discountCode={self.course.coupon_code}",
            "Referer": "https://www.udemy.com/payment/checkout/",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "x-checkout-is-mobile-app": "false",
            "Origin": "https://www.udemy.com",
            "Host": "www.udemy.com",
            "DNT": "1",
            "Sec-GPC": "1",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Priority": "u=0",
            "X-CSRF-Token": self.client.cookies.get(
                "csrftoken", domain="www.udemy.com"
            ),
        }
        for _ in range(0, 3):
            r = self.client.post(
                "https://www.udemy.com/payment/checkout-submit/",
                json=payload,
                headers=headers,
            )
            if r.headers.get("retry-after"):
                logger.error(
                    "Udemy is rate limiting checkout (Retry-After). Waiting 60 seconds"
                )
                time.sleep(60)
            try:
                if r.status_code == 504:
                    r = {"status": "succeeded", "message": "Request Timeout"}
                else:
                    r = r.json()
            except Exception as e:
                logger.exception(
                    f"Unknown Error during bulk checkout: {e}\nResponse: {r.text} {payload}"
                )
                return
            if r.get("status") == "succeeded":
                for course in self.valid_courses:
                    self.course = course
                    self.enrolled_courses[self.course.slug] = self.get_now_to_utc()
                    self.amount_saved_c += (
                        Decimal(str(course.price))
                        if course.price is not None
                        else Decimal(0)
                    )
                    self.successfully_enrolled_c += 1
                    self.save_course()
                logger.success(
                    f"Successfully Enrolled To {len(self.valid_courses)} Courses :)",
                    color="green",
                )
                return
            logger.error(f"Bulk checkout failed {_+1}: {r}, Retrying...")
            self.client.get("https://www.udemy.com/payment/checkout/", headers=headers)
            time.sleep(5 + _)

        logger.error(f"Bulk checkout failed after 3 attempts: {r}")
        logger.error("Enrolling the batch courses one by one instead...")
        for course in list(self.valid_courses):
            self.checkout_single(course)

    def checkout_single(self, course):
        """Enroll in a single course; fallback when a bulk batch fails"""
        payload = {
            "checkout_environment": "Marketplace",
            "checkout_event": "Submit",
            "payment_info": {
                "method_id": "0",
                "payment_method": "free-method",
                "payment_vendor": "Free",
            },
            "shopping_info": {
                "items": [
                    {
                        "buyable": {"id": str(course.course_id), "type": "course"},
                        "discountInfo": {"code": course.coupon_code},
                        "price": {"amount": 0, "currency": self.currency.upper()},
                    }
                ],
                "is_cart": True,
            },
        }
        headers = {
            "User-Agent": "okhttp/4.10.0 UdemyAndroid 9.7.0(515) (phone)",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US",
            "Referer": "https://www.udemy.com/payment/checkout/",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "x-checkout-is-mobile-app": "false",
            "Origin": "https://www.udemy.com",
            "Host": "www.udemy.com",
            "DNT": "1",
            "Sec-GPC": "1",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Priority": "u=0",
            "X-CSRF-Token": self.client.cookies.get(
                "csrftoken", domain="www.udemy.com"
            ),
        }
        for _ in range(0, 3):
            r = self.client.post(
                "https://www.udemy.com/payment/checkout-submit/",
                json=payload,
                headers=headers,
            )
            if r.headers.get("retry-after"):
                logger.error(
                    "Udemy is rate limiting checkout (Retry-After). Waiting 60 seconds"
                )
                time.sleep(60)
            try:
                if r.status_code == 504:
                    r = {"status": "succeeded", "message": "Request Timeout"}
                else:
                    r = r.json()
            except Exception as e:
                logger.exception(
                    f"Unknown Error during single checkout: {e}\nResponse: {r.text}"
                )
                return False
            if r.get("status") == "succeeded":
                self.course = course
                self.enrolled_courses[course.slug] = self.get_now_to_utc()
                self.amount_saved_c += (
                    Decimal(str(course.price))
                    if course.price is not None
                    else Decimal(0)
                )
                self.successfully_enrolled_c += 1
                self.save_course()
                logger.success("Successfully Enrolled To Course :)")
                return True
            logger.error(f"Single checkout failed {_+1}: {r}")
            self.client.get("https://www.udemy.com/payment/checkout/", headers=headers)
            time.sleep(3 + _)
        logger.error(f"Checkout failed for course: {course.url}")
        self.failed_c += 1
        return False

    def free_checkout(self):
        self.client.get(
            f"https://www.udemy.com/course/subscribe/?courseId={self.course.course_id}"
        )
        r = self.client.get(
            f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{self.course.course_id}/?fields%5Bcourse%5D=%40default%2Cbuyable_object_type%2Cprimary_subcategory%2Cis_private"
        )

        if r.headers.get("retry-after"):
            logger.error(
                "Something really bad happened during free checkout, Please report this to the developer"
            )
            logger.error(r.text)
            raise Exception(
                "Something really bad happened during free checkout, Please report this to the developer"
            )
        if r.status_code == 503:
            logger.error(r.text)
            self.course.status = True
            return
        r = r.json()
        self.course.status = r.get("_class") == "course"

    # def handle_course_enrollment(self):
    #     self.course.retry = False
    #     self.course.ready_time = None
    #     self.course.retry_after = None

    #     """Process a course for enrollment"""
    #     # Check if already enrolled
    #     slug = self.course.url.split("/")[4]
    #     if slug in self.enrolled_courses:
    #         self.print(
    #             f"You purchased this course on {self.get_date_from_utc(self.enrolled_courses[slug])}",
    #             color="light blue",
    #         )
    #         self.already_enrolled_c += 1
    #         return

    #     # Get course details and validate
    #     self.get_course_id()
    #     if not self.course.is_valid:
    #         self.print(self.course.error, color="red")
    #         self.excluded_c += 1

    #     elif self.course.retry:
    #         self.print("Retrying...", color="red")
    #         time.sleep(1)
    #         self.handle_course_enrollment()
    #         self.retry = False
    #     elif self.course.is_excluded:
    #         self.excluded_c += 1
    #     else:
    #         # Handle free vs paid courses
    #         if self.course.is_free:
    #             self.handle_free_course()
    #         else:
    #             self.handle_discounted_course()

    # def handle_free_course(self):
    #     """Handle enrollment for a free course"""
    #     if self.settings["discounted_only"]:
    #         self.print("Free course excluded", color="light blue")
    #         self.excluded_c += 1
    #     else:
    #         self.free_checkout()
    #         if self.course.status:
    #             self.print("Successfully Subscribed", color="green")
    #             self.successfully_enrolled_c += 1
    #             self.save_course()
    #         else:
    #             self.print(
    #                 "Unknown Error: Report this link to the developer", color="red"
    #             )
    #             self.expired_c += 1

    # def handle_discounted_course(self):
    #     """Handle enrollment for a discounted course"""
    #     self.check_course()
    #     if self.course.retry:
    #         self.print("Retrying...", color="red")
    #         time.sleep(1)
    #         self.handle_discounted_course()
    #         self.retry = False
    #     if self.course.is_coupon_valid:
    #         self.process_coupon()
    #     else:
    #         self.print("Coupon Expired", color="red")
    #         self.expired_c += 1

    # def discounted_checkout(self):
    #     payload = {
    #         "checkout_environment": "Marketplace",
    #         "checkout_event": "Submit",
    #         "payment_info": {
    #             "method_id": "0",
    #             "payment_method": "free-method",
    #             "payment_vendor": "Free",
    #         },
    #         "shopping_info": {
    #             "items": [
    #                 {
    #                     "buyable": {"id": self.course.course_id, "type": "course"},
    #                     "discountInfo": {"code": self.course.coupon_code},
    #                     "price": {"amount": 0, "currency": self.currency.upper()},
    #                 }
    #             ],
    #             "is_cart": False,
    #         },
    #     }
    #     headers = {
    #         "User-Agent": "okhttp/4.9.2 UdemyAndroid 8.9.2(499) (phone)",
    #         "Accept": "application/json, text/plain, */*",
    #         "Accept-Language": "en-US",
    #         "Referer": f"https://www.udemy.com/payment/checkout/express/course/{self.course.course_id}/?discountCode={self.course.coupon_code}",
    #         "Content-Type": "application/json",
    #         "X-Requested-With": "XMLHttpRequest",
    #         "x-checkout-is-mobile-app": "true",
    #         "Origin": "https://www.udemy.com",
    #         "DNT": "1",
    #         "Sec-GPC": "1",
    #         "Connection": "keep-alive",
    #         "Sec-Fetch-Dest": "empty",
    #         "Sec-Fetch-Mode": "cors",
    #         "Sec-Fetch-Site": "same-origin",
    #         "Priority": "u=0",
    #     }
    #     r = self.client.post(
    #         "https://www.udemy.com/payment/checkout-submit/",
    #         json=payload,
    #         headers=headers,
    #     )
    #     try:
    #         retry_after = r.headers.get("retry-after")
    #         r = r.json()
    #         if retry_after:
    #             self.course.set_retry_after(int(retry_after))
    #             return
    #     except Exception as e:
    #         if self.debug:
    #             logger.error(e)
    #         self.print(r.text, color="red")
    #         self.print("Unknown Error: Report this to the developer", color="red")
    #         self.course.status = "failed"
    #         self.course.error = "Unknown Error: Report this to the developer"
    #         return
    #     self.course.status = r.get("status")
    #     self.course.error = r.get("message")

    # def process_coupon(self):
    #     self.discounted_checkout()
    #     if self.course.retry_after:
    #         return
    #     elif self.course.status == "succeeded":
    #         self.print("Successfully Enrolled To Course :)", color="green")
    #         self.successfully_enrolled_c += 1
    #         self.enrolled_courses[self.course.course_id] = self.get_now_to_utc()
    #         self.amount_saved_c += (
    #             Decimal(str(self.course.price))
    #             if self.course.price is not None
    #             else Decimal(0)
    #         )
    #         self.save_course()
    #         time.sleep(2)
    #     elif self.course.status == "failed":
    #         message = self.course.error
    #         if "item_already_subscribed" in message:
    #             self.print("Already Enrolled", color="light blue")
    #             self.already_enrolled_c += 1
    #         else:
    #             self.print("Unknown Error: Report this to the developer", color="red")
    #             self.print(self.course.error, color="red")
    #     else:
    #         self.print("Unknown Error: Report this to the developer", color="red")
    #         self.print(self.course.error, color="red")

    # def start_enrolling(self):
    #     self.initialize_counters()
    #     self.setup_txt_file()

    #     # Create a queue of all courses
    #     course_queue: deque[Course] = deque()
    #     courses = self.scraped_data
    #     total_courses = len(courses)
    #     # for site, courses in self.scraped_data.items():
    #     # self.print(f"\nSite: {site} [{len(courses)}]", color="cyan")
    #     courses_list: list[Course] = list(courses)
    #     # random.shuffle(courses_list)
    #     course_queue.extend(courses_list)

    #     processed_count = 0
    #     while course_queue:
    #         self.course = course_queue.popleft()

    #         # Check if this course has a ready time and needs to wait
    #         if self.course.should_retry():
    #             # Put it back in queue if not ready
    #             course_queue.append(self.course)
    #             continue

    #         self.print_course_info(processed_count, total_courses)

    #         try:
    #             self.handle_course_enrollment()
    #             if self.course.ready_time:
    #                 # Put back in queue
    #                 course_queue.insert(self.course.retry_after // 2, self.course)
    #                 self.print(
    #                     f"Request Throttled. Will retry after {self.course.retry_after} seconds",
    #                     color="yellow",
    #                 )
    #             else:
    #                 processed_count += 1
    #         except Exception as e:
    #             logger.exception(f"Error processing course {self.course}: {e}")
    #             processed_count += 1
