#%%
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup, NavigableString
from urllib.parse import quote_plus
import re
import time
import csv
import traceback

# --- CONFIG ---

# Each dict is its own separate search round, run one after another, each
# producing its own CSV. Keywords within a round are combined with OR
# (LinkedIn's keyword box supports this boolean syntax directly), so a job
# matching ANY keyword in that round's list is included.
#
# "easy_apply" controls how each round treats Easy Apply jobs:
#   "exclude" -- drop Easy Apply jobs (default)
#   "only"    -- keep ONLY Easy Apply jobs
#   "both"    -- no filtering on Easy Apply either way
# Reposted jobs are always dropped in every round, regardless of this setting.
#
# "quote" wraps each keyword in quotes for an exact-phrase match (matters for
# multi-word phrases; harmless either way for single words).
ROUNDS = [
    {"keywords": ["C++"], "easy_apply": "exclude"},
    {"keywords": ["C++"], "easy_apply": "only"},
    {"keywords": ["Compiler"], "easy_apply": "both", "quote": True},
    {"keywords": ["Rust"], "easy_apply": "both"},
    {"keywords": ["Embedded"], "easy_apply": "both", "quote": True},
    {"keywords": ["Firmware"], "easy_apply": "both", "quote": True},
]

JOB_LOCATION = "United States"        # e.g. "Remote", "United States", "Toronto, Canada"

# Experience levels (LinkedIn codes as strings)
# 1 = Internship
# 2 = Entry level
# 3 = Associate
# 4 = Mid-Senior level
# 5 = Director
# 6 = Executive
EXPERIENCE_LEVELS = ["2", "3", "4"]          # e.g. ["2"] for Entry, ["2","3"] for Entry+Associate, [] for all

# Date posted filter:
# "any"  -> no filter
# "24h"  -> past 24 hours
# "week" -> past week
# "month"-> past month
DATE_POSTED = "24h"                    # change to "any", "24h", "week", "month"

# Stop collecting once this many unique jobs have been found (LinkedIn pages in
# batches of 25). Every page turn is another authenticated page load, which is
# what gets accounts rate-limited, so don't set this arbitrarily high.
MAX_RESULTS = 1000

# Hard safety net per round: stop paging after this many seconds no matter what
# (e.g. if LinkedIn keeps returning "recommended" filler pages, or a page load
# hangs) and save whatever was collected so far instead of running forever.
MAX_RUNTIME_SECONDS = 300

PAGE_SIZE = 25
PAGE_PAUSE = 7     # wait after loading a search-results page

# --- Build filters (shared across every round) ---

exp_param = ",".join(EXPERIENCE_LEVELS) if EXPERIENCE_LEVELS else ""

# Map DATE_POSTED to LinkedIn f_TPR values
date_param = ""
if DATE_POSTED == "24h":
    date_param = "r86400"
elif DATE_POSTED == "week":
    date_param = "r604800"
elif DATE_POSTED == "month":
    date_param = "r2592000"
# if "any", leave date_param empty

safe_location = JOB_LOCATION.replace(" ", "_").replace(",", "")
safe_exp = exp_param.replace(",", "_") if exp_param else "all"
safe_date = DATE_POSTED

# --- Setup Chrome ---
print("🚗 Starting Chrome...")
options = Options()
options.add_argument("--start-maximized")
options.add_argument("--incognito")   # 👈 Add this here
# options.add_argument("--headless")  # Uncomment to run headless
driver = webdriver.Chrome(options=options)
print("✅ Chrome started.")

# --- Log in once, before any of the search rounds ---
print("🌐 Loading linkedin.com/jobs ...")
driver.get("https://www.linkedin.com/jobs/")
print("✅ Page loaded.")
input("🔐 Log in manually, then press Enter to continue...")

# --- Parsing helpers ---
# NOTE: LinkedIn appears to A/B test two completely different job-search
# frontends for logged-in users:
#   "react" -- a newer app with hashed/regenerated CSS classes (e.g.
#              "_07047c01"). The one stable hook is the "componentkey"
#              attribute on each card, e.g. "job-card-component-ref-<jobId>".
#   "ember" -- an older app with real, stable BEM-style classes
#              (job-card-container, artdeco-entity-lockup__*) and a plain
#              "data-job-id" attribute on each card.
# Which one you get seems to depend on the session, so we detect it per page
# load and parse accordingly.


def find_job_cards(soup):
    els = soup.find_all(attrs={"componentkey": lambda v: v and v.startswith("job-card-component-ref-")})
    if els:
        seen = set()
        cards = []
        for e in els:
            cid = e["componentkey"]
            if cid not in seen:
                seen.add(cid)
                cards.append(e)
        return "react", cards

    els = soup.find_all(attrs={"data-job-id": True})
    if els:
        seen = set()
        cards = []
        for e in els:
            jid = e["data-job-id"]
            if jid not in seen:
                seen.add(jid)
                cards.append(e)
        return "ember", cards

    return None, []


def find_title_company_location(card):
    # Inside every "react" card there is exactly one div whose three direct
    # children are [title-div, company-div, location-p].
    for div in card.find_all("div"):
        children = div.find_all(recursive=False)
        if len(children) == 3 and children[0].name == "div" and children[1].name == "div" and children[2].name == "p":
            return children
    return None, None, None


def extract_title(title_div):
    aria_span = title_div.find("span", attrs={"aria-hidden": "true"})
    if aria_span:
        for c in aria_span.contents:
            if isinstance(c, NavigableString) and c.strip():
                return c.strip()
    return title_div.get_text(strip=True)


def extract_posted(card):
    text = card.get_text(separator="|", strip=True)
    m = re.search(r"Posted ([^|]+?ago)", text)
    return m.group(1) if m else ""


# The search-results page embeds LinkedIn's own raw API data for hydration,
# which includes a literal `"repostedJob":true/false` field per job right next
# to its id -- e.g. `"trackingUrn":"urn:li:jobPosting:4427487971","repostedJob":true`.
# That's a much more reliable signal than guessing at visible text, and it's
# already on this page -- no need to open each job individually to check.
def find_reposted_status(html):
    """job_id -> True/False for every job this page actually has the field
    for. Not every card has it (promoted/sponsored listings seem to come from
    a different pipeline that omits it entirely) -- those just won't be keys
    in the returned dict, which callers need to handle as "unknown"."""
    return {
        job_id: value == "true"
        for job_id, value in re.findall(r'"trackingUrn":"urn:li:jobPosting:(\d+)","repostedJob":(true|false)', html)
    }


def check_job_page_reposted(job_url):
    """Fallback for jobs the search page didn't carry repost status for (seen
    with promoted listings). The "Reposted" badge only shows in the single-job
    detail view -- never on the list card itself -- so this has to actually
    open that job's own page and look at its visible text directly."""
    try:
        driver.get(job_url)
        time.sleep(3)
        return "reposted" in driver.page_source.lower()
    except Exception:
        return False  # can't verify -- default to keeping it, same as "unknown"


def parse_react_card(card):
    job_id = card["componentkey"].split("-")[-1]
    title_div, company_div, loc_p = find_title_company_location(card)
    if title_div is None:
        return None
    return {
        "job_id": job_id,
        "job_title": extract_title(title_div),
        "job_url": f"https://www.linkedin.com/jobs/view/{job_id}/",
        "company_name": company_div.get_text(strip=True),
        "location": loc_p.get_text(strip=True),
        "posted": extract_posted(card),
    }


def parse_ember_card(card):
    job_id = card.get("data-job-id")
    if not job_id:
        return None

    a = card.find("a", href=re.compile(r"/jobs/view/"))
    title = ""
    if a and a.get("aria-label"):
        title = re.sub(r"\s+with verification$", "", a["aria-label"]).strip()
    if not title:
        strong = card.find("strong")
        title = strong.get_text(strip=True) if strong else ""

    href = a["href"] if a else f"/jobs/view/{job_id}/"
    job_url = href if href.startswith("http") else f"https://www.linkedin.com{href.split('?')[0]}"

    company_el = card.find(class_=lambda c: c and "artdeco-entity-lockup__subtitle" in c)
    loc_el = card.find(class_=lambda c: c and "artdeco-entity-lockup__caption" in c)
    footer = card.find(class_=lambda c: c and "job-card-list__footer-wrapper" in c)
    footer_text = footer.get_text(" ", strip=True) if footer else ""
    m = re.search(r"(\d+\s+(?:minute|hour|day|week|month)s?\s+ago)", footer_text)

    return {
        "job_id": job_id,
        "job_title": title,
        "job_url": job_url,
        "company_name": company_el.get_text(strip=True) if company_el else "",
        "location": loc_el.get_text(strip=True) if loc_el else "",
        "posted": m.group(1) if m else "",
    }


# LinkedIn's results list is virtualized: even within one fetched batch of 25,
# only the cards that have scrolled into view actually get their content
# rendered -- the rest sit as empty placeholder <li class="occludable-update">
# elements until you scroll past them. So before reading the page we scroll
# the results list (whichever element actually scrolls) all the way down to
# force everything in the current batch to render.
RENDER_ALL_CARDS_JS = """
const callback = arguments[arguments.length - 1];
function findScrollParent(el) {
    while (el && el !== document.body && el !== document.documentElement) {
        const style = getComputedStyle(el);
        if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 10) return el;
        el = el.parentElement;
    }
    return document.scrollingElement || document.documentElement;
}
const anchor = document.querySelector('[data-job-id], [componentkey^="job-card-component-ref-"]');
if (!anchor) { callback(false); }
const container = findScrollParent(anchor);
let lastScrollTop = -1;
let stableCount = 0;
let iterations = 0;
function step() {
    iterations++;
    container.scrollTop = container.scrollHeight;
    setTimeout(() => {
        if (container.scrollTop === lastScrollTop) {
            stableCount++;
        } else {
            stableCount = 0;
        }
        lastScrollTop = container.scrollTop;
        if (stableCount >= 2 || iterations >= 15) {
            callback(true);
        } else {
            step();
        }
    }, 700);
}
step();
"""


def render_all_cards(driver):
    driver.set_script_timeout(20)
    try:
        driver.execute_async_script(RENDER_ALL_CARDS_JS)
    except Exception:
        pass


def run_search(round_cfg):
    """Runs one round, catching anything unexpected so one round crashing
    doesn't take down the rest (or skip cleanup at the end of the script)."""
    label = f"{', '.join(round_cfg['keywords'])} [easy_apply={round_cfg.get('easy_apply', 'exclude')}]"
    try:
        _run_one_round(round_cfg)
    except Exception as e:
        print(f"💥 Round '{label}' crashed: {e}")
        traceback.print_exc()


def _run_one_round(round_cfg):
    """Runs one full search round for the given round config, and writes its
    own CSV (and a debug HTML snapshot of the first results page)."""
    job_keywords = round_cfg["keywords"]
    easy_apply_mode = round_cfg.get("easy_apply", "exclude")  # "exclude" | "only" | "both"
    quote = round_cfg.get("quote", False)

    run_start = time.time()

    def elapsed_str():
        e = time.time() - run_start
        return f"{int(e // 60)}m {int(e % 60)}s"

    if quote:
        keyword_query = " OR ".join(f'"{k}"' for k in job_keywords)
    else:
        keyword_query = " OR ".join(job_keywords)
    base_url = (
        "https://www.linkedin.com/jobs/search/?"
        f"keywords={quote_plus(keyword_query)}&"
        f"location={quote_plus(JOB_LOCATION)}"
    )
    if exp_param:
        base_url += f"&f_E={quote_plus(exp_param)}"
    if date_param:
        base_url += f"&f_TPR={date_param}"

    mode_suffix = {"exclude": "", "only": "_EasyApplyOnly", "both": "_AllApply"}[easy_apply_mode]
    safe_keyword = "_".join(k.replace(" ", "") for k in job_keywords)
    output_html = f"linkedin_jobs_{safe_keyword}_{safe_location}_{safe_exp}_{safe_date}{mode_suffix}.html"
    output_csv = f"linkedin_jobs_{safe_keyword}_{safe_location}_{safe_exp}_{safe_date}{mode_suffix}.csv"

    print(f"\n===== 🔎 Round: {', '.join(job_keywords)} [easy_apply={easy_apply_mode}] =====")
    print(f"🔗 Using URL:\n{base_url}\n")

    # --- Collect job cards across result pages ---
    jobs_by_id = {}
    seen_ids = set()  # every job id we've encountered, accepted or filtered out
    start = 0
    while len(jobs_by_id) < MAX_RESULTS:
        if time.time() - run_start > MAX_RUNTIME_SECONDS:
            print(f"⏱️ Hit the {MAX_RUNTIME_SECONDS}s time limit -- stopping here, keeping what was already collected.")
            break

        page_url = f"{base_url}&start={start}" if start else base_url
        try:
            driver.get(page_url)
            time.sleep(PAGE_PAUSE)
            render_all_cards(driver)
            html = driver.page_source
        except Exception as e:
            print(f"⚠️ Failed to load start={start} ({e}). Stopping here, keeping what was already collected.")
            break

        if start == 0:
            with open(output_html, "w", encoding="utf-8") as f:
                f.write(html)

        try:
            soup = BeautifulSoup(html, "html.parser")
            layout, cards = find_job_cards(soup)
            reposted_status = find_reposted_status(html)
        except Exception as e:
            print(f"⚠️ Failed to parse start={start} ({e}). Stopping here, keeping what was already collected.")
            traceback.print_exc()
            break

        if not cards:
            print("✅ No more job cards found.")
            break

        new_count = 0
        skipped_easy_apply = 0
        skipped_reposted = 0
        checked_individually = 0
        progress = 0  # new ids seen on this page, whether kept or filtered
        for card in cards:
            try:
                job_id = card["componentkey"].split("-")[-1] if layout == "react" else card.get("data-job-id")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                progress += 1

                is_easy_apply = "easy apply" in card.get_text(" ", strip=True).lower()
                if easy_apply_mode == "exclude" and is_easy_apply:
                    skipped_easy_apply += 1
                    continue
                if easy_apply_mode == "only" and not is_easy_apply:
                    skipped_easy_apply += 1
                    continue
                # easy_apply_mode == "both" -> no filtering either way

                parsed = parse_react_card(card) if layout == "react" else parse_ember_card(card)
                if not parsed:
                    continue

                # The hidden per-job JSON field is the reliable signal when
                # present, but some cards (seen with promoted listings) don't
                # carry it at all -- for those, the "Reposted" badge only ever
                # shows up in that job's own detail view, never on the list
                # card itself, so it has to be checked on that job's own page.
                reposted = reposted_status.get(job_id)
                if reposted is None:
                    if time.time() - run_start > MAX_RUNTIME_SECONDS:
                        reposted = False  # out of time -- can't verify, keep it
                    else:
                        checked_individually += 1
                        reposted = check_job_page_reposted(parsed["job_url"])
                if reposted:
                    skipped_reposted += 1
                    continue

                jobs_by_id[job_id] = parsed
                new_count += 1
            except Exception as e:
                print(f"   ⚠️ Skipping one card that failed to parse: {e}")

        easy_apply_label = "skipped as Easy Apply" if easy_apply_mode == "exclude" else "skipped as non-Easy-Apply"
        print(
            f"📄 [{elapsed_str()}] start={start}: '{layout}' layout, {len(cards)} cards on page, "
            f"{new_count} new, {skipped_easy_apply} {easy_apply_label}, {skipped_reposted} skipped as Reposted "
            f"({checked_individually} checked individually) (total {len(jobs_by_id)})"
        )
        if progress == 0:
            print("✅ No new jobs on this page -- stopping.")
            break
        start += PAGE_SIZE

    print(f"✅ Collected {len(jobs_by_id)} unique jobs.")

    jobs = list(jobs_by_id.values())

    # --- Save to CSV ---
    if jobs:
        try:
            with open(output_csv, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["job_title", "company_name", "job_url"])
                for job in jobs:
                    writer.writerow([job["job_title"], job["company_name"], job["job_url"]])
            print(f"📁 Saved {len(jobs)} jobs to {output_csv}")
        except Exception as e:
            print(f"⚠️ Failed to write {output_csv}: {e}")
            traceback.print_exc()
    else:
        print("⚠️ No jobs extracted.")

    print(f"⏱️ Round time: {elapsed_str()}")


for round_cfg in ROUNDS:
    run_search(round_cfg)

print("\n🏁 All rounds finished. Closing Chrome...")
try:
    driver.quit()
except Exception as e:
    print(f"⚠️ driver.quit() failed ({e}) -- Chrome may need closing manually.")
print("✅ Done.")
