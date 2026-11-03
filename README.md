# LinkedIn Job Scraper

> **Note:** This code was written by [Claude](https://claude.com/claude-code) (Anthropic's AI assistant) via Claude Code.

A Selenium + BeautifulSoup script that searches LinkedIn Jobs with your filters and saves the results to CSV files. You log in by hand, and the script does the paging, parsing and filtering.

## What it does

1. Opens Chrome in incognito mode at `linkedin.com/jobs` and waits for you to log in manually. It never handles your credentials.
2. Runs each search "round" defined in `ROUNDS`, one after another. Keywords within a round are OR-combined into a single LinkedIn query.
3. For each results page it:
   - scrolls the virtualized results list so all 25 cards actually render,
   - detects which LinkedIn frontend it got (the newer **React** layout or the older **Ember** layout) and parses cards accordingly,
   - extracts the job title, company, location, posted time and job URL.
4. Filters the jobs:
   - **Reposted jobs are always dropped.** Repost status is read from LinkedIn's embedded `"repostedJob"` data. When that field is missing (common for promoted listings), the script opens the job's own page to check.
   - **Easy Apply** jobs are filtered per round (`exclude`, `only`, or `both`).
5. Pages through results until it reaches `MAX_RESULTS`, a page with no new jobs, or the `MAX_RUNTIME_SECONDS` safety limit.
6. Writes one CSV per round (`job_title, company_name, job_url`), plus an HTML snapshot of the first results page for debugging.

## Requirements

- Python 3.9+
- Google Chrome
- Python packages: `selenium`, `beautifulsoup4`

## Setup

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

```bash
python linkedinjobsscraper.py
```

When Chrome opens, log in to LinkedIn, go back to the terminal and press **Enter**. Progress is printed for every page, and Chrome closes when all rounds are done.

## Configuration

Edit the `CONFIG` section at the top of `linkedinjobsscraper.py`:

| Setting | Description |
|---|---|
| `ROUNDS` | List of searches. Each has `keywords` (list), `easy_apply` (`"exclude"` / `"only"` / `"both"`), and optional `quote` (exact-phrase match). |
| `JOB_LOCATION` | e.g. `"United States"`, `"Remote"`, `"Toronto, Canada"` |
| `EXPERIENCE_LEVELS` | LinkedIn codes: `1` Internship, `2` Entry, `3` Associate, `4` Mid-Senior, `5` Director, `6` Executive. `[]` means all levels. |
| `DATE_POSTED` | `"any"`, `"24h"`, `"week"`, or `"month"` |
| `MAX_RESULTS` | Stop a round after this many unique jobs |
| `MAX_RUNTIME_SECONDS` | Time limit per round. Whatever was collected so far is saved. |
| `PAGE_PAUSE` | Seconds to wait after each page load |

Example round:

```python
ROUNDS = [
    {"keywords": ["C++"], "easy_apply": "exclude"},
    {"keywords": ["Embedded", "Firmware"], "easy_apply": "both", "quote": True},
]
```

## Output

Files are named from the search parameters:

```
linkedin_jobs_<keyword>_<location>_<exp-levels>_<date>[_EasyApplyOnly|_AllApply].csv
linkedin_jobs_<keyword>_<location>_<exp-levels>_<date>[_EasyApplyOnly|_AllApply].html
```

For example: `linkedin_jobs_Compiler_United_States_2_3_4_24h_AllApply.csv`

## Caveats

- Every page load is an authenticated request. Keep `MAX_RESULTS` and the number of rounds reasonable to avoid having your account rate-limited.
- LinkedIn changes its markup often and A/B tests different frontends, so the parsers may need updating over time.
- Scraping LinkedIn may violate its Terms of Service. Use at your own risk and for personal use only.
