# Pipeline: the full guide

The [README](../README.md) gets you running. This covers how the thing actually works and every option it has.

- [How a job moves through Pipeline](#how-a-job-moves-through-pipeline)
- [Matching: why it found what it found](#matching-why-it-found-what-it-found)
- [Scoring](#scoring)
- [Research](#research)
- [Writing](#writing)
- [The resume builder](#the-resume-builder)
- [Applying](#applying)
- [Every command and flag](#every-command-and-flag)
- [profile.yml reference](#profileyml-reference)
- [.env reference](#env-reference)
- [Cost](#cost)
- [Adding a job board](#adding-a-job-board)
- [Privacy](#privacy)
- [Working on the code](#working-on-the-code)

---

## How a job moves through Pipeline

Every posting has a stage. `pipeline status` shows the counts.

```
scraped → researched → matched → generated → applying → submitted
                                                     ↘ blocked
```

- **scraped** found on a board, nothing done yet
- **researched** the company has a brief written
- **matched** scored 0 to 100 against your profile
- **generated** cover letter, answers, and resume are written
- **submitted** the application went in
- **blocked** something needs you, see `pipeline unblock`

Stages only move forward. Re-running `pipeline scrape` never resets a job you already submitted, so running the whole thing daily is safe.

Everything is in `data/pipeline.db`, an ordinary SQLite file. Delete it to start over.

---

## Matching: why it found what it found

Scraping every posting and asking the AI to score all of them would work, and it would also be expensive. So there is a cheap keyword filter first, and the AI only sees what survives it.

The filter has two gates. A posting has to pass both.

### Gate 1: seniority

Job titles advertise a level. Pipeline sorts them into buckets:

| Bucket | Words that trigger it                                    |
| ------ | -------------------------------------------------------- |
| intern | intern, internship, co-op, trainee                       |
| entry  | junior, jr, entry, grad, graduate, associate, apprentice |
| mid    | no level word in the title                               |
| senior | senior, sr, lead, II, III, IV                            |
| staff  | staff, principal, distinguished, fellow, architect       |
| lead   | manager, director, head, VP, chief                       |

Your target and the posting have to land in the same bucket. The one exception: **entry and mid are treated as the same market**, so someone targeting "Software Engineer" still sees "Junior Software Engineer" and vice versa.

This gate is the difference between a tool that works and one that wastes your money. Without it, "Software Engineer Intern" shares two of three words with "Staff Software Engineer, Inference", so a fuzzy match hands a student dozens of Staff and Principal roles and bills an AI call to reject every one. On a real three-board scan that was 77 senior roles out of 107 matches, against one genuine internship.

When a title says "Staff + Sr. Software Engineer", the higher level wins.

### Gate 2: keywords

Level words are stripped out (they already did their job in gate 1), and what remains has to overlap with the posting title:

- At least half the remaining words have to appear
- And always at least two distinct words, when your target has two or more

The two-word floor matters. Half of two words is one word, so without the floor "Software Engineer" would accept every "AI Engineer" and "Quality Engineer" on the board.

Words match on shared stems, so "engineer" matches "engineering". Prefix matching only kicks in at four characters, so short words do not start matching everything.

### Getting more or fewer results

More: add titles under `target_roles.primary`, and add boards under `sources`. Different phrasings of the same job help, since companies name things inconsistently.

```yaml
target_roles:
  primary:
    - "Software Engineer Intern"
    - "Software Engineering Intern"
    - "Backend Engineer Intern"
    - "Cybersecurity Intern"
    - "Security Analyst Intern"
```

Fewer: use longer, more specific titles.

`pipeline scrape` always reports how many postings it looked at against how many matched, so you can tell "my filter is too narrow" apart from "these boards have nothing right now".

---

## Scoring

Everything that passes the filter goes to the AI with your profile and gets a score.

| Score     | Meaning                                                           |
| --------- | ----------------------------------------------------------------- |
| 85 to 100 | Strong fit, you meet the core requirements and the level is right |
| 70 to 84  | Good fit, worth applying, minor gaps                              |
| 50 to 69  | A stretch, real gap somewhere                                     |
| 0 to 49   | Poor fit, wrong level or wrong field                              |

The scorer is told to be strict about level and honest about gaps. Under-scoring a bad fit costs you nothing. Over-scoring wastes your time and your name with that company.

If a posting requires work authorization you do not have, the score is capped at 40 and the reason lands in `concerns`.

Only jobs at or above `min_score_to_generate` (default 70) get written up. Change it in `profile.yml`, or override per run:

```bash
pipeline generate --min-score 80
```

Read the scores and reasoning:

```bash
sqlite3 data/pipeline.db \
  "SELECT m.score, j.company, j.title, m.reasoning
   FROM matches m JOIN jobs j ON j.external_id = m.job_external_id
   ORDER BY m.score DESC LIMIT 20"
```

---

## Research

Before writing anything, Pipeline builds a brief on each company so the cover letter can reference something real. Briefs are cached per company and written to `data/research/<company>.md` for you to read.

Three tiers of sources:

**Always on, no key needed**

- The job description itself
- Recent Hacker News stories about the company (funding, launches, outages, how engineers talk about the place)
- The company's own site, when the posting lives there rather than on an ATS

**With a search key in `.env`**

```bash
BRAVE_API_KEY=...
# or
SERPER_API_KEY=...
```

Adds general web search. Both have free tiers.

**With the browser installed**

```bash
PIPELINE_BROWSER=1
```

Lets research load JavaScript-heavy and bot-protected pages.

The research prompt is told the material is unverified, to ignore anything about a different company with a similar name, and to return empty rather than invent. If nothing useful turns up, the brief says so and the cover letter stays general instead of making things up.

---

## Writing

Per job, Pipeline generates:

- A cover letter, 150 to 220 words, three paragraphs
- Answers to the four questions almost every form asks
- Your contact and link fields, straight from your profile, never from the AI
- A tailored resume

The voice rules are enforced in the prompt: plain and direct, no buzzwords, no "passionate about", no em-dashes, and never a claim your profile does not support.

### Questions it did not expect

When the form asks something not in the answer bank, Pipeline asks the AI to answer it and to say whether your profile genuinely supports that answer. If the answer is no, the question becomes a barrier and waits for you rather than getting a bluff.

### Work authorization

These come from your profile only:

```yaml
preferences:
  work_authorization: "US citizen"
  requires_sponsorship: false
```

Leave `work_authorization` blank and Pipeline leaves those fields empty on every form. They are legal questions on a legal document, and a guess there is a false statement. That is deliberate and not configurable.

---

## The resume builder

Set `preferences.baseline_resume` to your own PDF and this whole section is skipped. Pipeline uses your file and never modifies it.

Otherwise it builds one from `experience`, `projects`, `skills`, and `education`. The output is deliberately plain, because resume parsers are:

- Real text rather than images
- A single column, with no tables or graphics
- Standard fonts
- One page

Skills get reordered per job so the ones the posting named come first. Nothing is added. Only skills already in your profile appear.

### One page is checked rather than assumed

After rendering, Pipeline counts the pages. If it runs long, it re-renders once with slightly tighter spacing, never below 9pt body text. If it still does not fit, `pipeline generate` tells you:

```
2 resume(s) ran past one page. Recruiters skim one page for a student or new grad.
  Trim config/profile.yml: fewer bullets per entry, or drop the oldest one.
```

Shrinking the type until it fits would hide a real problem, so Pipeline reports it instead.

---

## Applying

`pipeline apply` opens a real browser and fills forms. It needs the browser extra:

```bash
pip install "scrapling[fetchers]"
scrapling install
```

Your browser session is saved in `data/browser-profile/`, so a site you log into once stays logged in for later runs.

### How well it does, by system

| System                                 | What to expect                                                     |
| -------------------------------------- | ------------------------------------------------------------------ |
| Greenhouse, Lever, Ashby, Workable     | Usually fills and submits the whole form                           |
| Workday, iCIMS, Taleo, SmartRecruiters | Fills what it can, stops at the account wall                       |
| Anything else                          | Reads the form, maps fields with the AI, fills what it understands |

### Submit modes

| Mode      | Behavior                         |
| --------- | -------------------------------- |
| `confirm` | Fill, then ask you. **Default.** |
| `auto`    | Submit without asking.           |
| `never`   | Fill only, never submit.         |

```bash
pipeline apply --submit auto
pipeline apply --submit never
pipeline apply --headless      # hide the browser window
```

Confirm is the default because a wrong answer cannot be unsubmitted.

### Barriers

| Barrier              | Meaning                                                     |
| -------------------- | ----------------------------------------------------------- |
| `account_creation`   | Site wants an employer account first                        |
| `email_verification` | A code went to your inbox                                   |
| `captcha`            | Bot check on the page                                       |
| `assessment`         | The posting requires a timed test                           |
| `unknown_field`      | A required question could not be answered from your profile |
| `login_expired`      | The saved browser session signed out                        |
| `awaiting_submit`    | Form is filled and ready for your review                    |

When one hits, the tab stays open, you get a terminal bell and a desktop notification on Mac, and you get three choices: submit, skip, or "I finished it myself".

Nothing is lost if you quit. `pipeline unblock` shows the queue, and `pipeline unblock --resolve 2` marks item 2 done.

If Pipeline is running unattended with no terminal attached, barriers are recorded and it moves on rather than hanging forever.

---

## Every command and flag

Global, works on any command:

```
--profile PATH    use a different profile.yml
--quiet           plain output instead of the live display
--debug           verbose logging
```

```bash
pipeline init
```

Creates `config/profile.yml` and `.env` in the current folder. Never overwrites an existing one.

```bash
pipeline scrape
```

Pulls postings from your boards. No AI model needed.

```bash
pipeline research [--limit N]
```

Builds a brief per company. Cached, so companies already researched are skipped.

```bash
pipeline match [--limit N]
```

Scores each posting. `--limit` is a total across researched and unresearched jobs, not per stage.

```bash
pipeline generate [--limit N] [--min-score N]
```

Writes cover letters, answers, and resumes for everything above the threshold.

```bash
pipeline apply [--limit N] [--submit confirm|auto|never] [--headless]
```

Opens a browser and fills forms.

```bash
pipeline run [--limit N] [--min-score N] [--submit MODE] [--headless] [--no-apply]
```

All of the above in order. `--no-apply` stops before the browser and prints status.

```bash
pipeline status
pipeline unblock [--resolve N]
```

Where things stand, and what needs you. Neither needs an AI model.

`--limit` must be 1 or more. Leave it off for no limit.

---

## profile.yml reference

```yaml
candidate:
  full_name: "Jordan Rivera" # required
  email: "jordan@example.com" # required
  phone: "+1-555-0100"
  location: "Austin, TX"
  linkedin: "linkedin.com/in/jordanrivera"
  github: "github.com/jordanrivera"
  portfolio_url: "https://jordanrivera.example.com"

target_roles:
  primary: # required, at least one
    - "Software Engineer Intern"
  archetypes: # optional, broader buckets
    - name: "Platform / Infrastructure Intern"
      fit: "secondary"

narrative:
  headline: "One line about you. Becomes the resume summary."
  exit_story: >
    A short paragraph on your background and what you are looking for.
  superpowers:
    - "What you are actually good at"
  proof_points:
    - name: "ProjectName"
      hero_metric: "The number that makes it real"

preferences:
  min_score_to_generate: 70 # jobs below this are never written up
  work_authorization: "US citizen" # leave blank and forms are left blank
  requires_sponsorship: false
  baseline_resume: "" # your own PDF, or blank to build one

sources: # required, at least one
  - source: greenhouse # greenhouse | lever | ashby
    board: anthropic

# Everything below feeds the resume builder only.
# Skip it if you set baseline_resume.
experience:
  - title: "Software Engineering Intern"
    company: "Northwind Systems"
    location: "Austin, TX"
    dates: "Jun 2026 – Aug 2026"
    bullets:
      - "What you did and what changed because of it."

projects:
  - name: "OrderFlow"
    tech: "Python, Postgres, Redis"
    link: "github.com/jordanrivera/orderflow"
    bullets:
      - "What it does and the number that proves it."

skills:
  languages: ["Python", "Go", "SQL"]
  infrastructure: ["Docker", "PostgreSQL", "Linux"]

education:
  - degree: "B.S. Computer Science"
    school: "University of Texas at Austin"
    location: "Austin, TX"
    dates: "Expected May 2028"
    bullets:
      - "Relevant coursework"
```

Bullets should say what changed, not what you were assigned. "Cut data lag from 12 hours to 4 minutes" beats "responsible for data pipeline work".

---

## .env reference

```bash
# Required for research, match, generate, and apply.
LLM_PROVIDER=anthropic        # anthropic | openai | openrouter | groq | ollama
LLM_MODEL=claude-sonnet-5
LLM_API_KEY=sk-ant-...
LLM_BASE_URL=                 # only for a custom or self-hosted endpoint

# Optional: adds general web search to research.
BRAVE_API_KEY=
SERPER_API_KEY=

# Optional: let research load JS-heavy pages. Needs the browser install.
PIPELINE_BROWSER=1

# Optional: put the database somewhere else.
PIPELINE_DB=data/pipeline.db
```

Anything already set in your real environment wins over the file.

Ollama is the only provider that needs no key, because it runs on your machine. Everything else fails fast at startup if the key is missing, rather than twenty seconds into a scan.

---

## Cost

Per job that makes it past the filter, roughly:

- 1 research call per **company**, cached and reused
- 1 scoring call per job
- 2 writing calls per job that clears the threshold
- Occasional extra calls for unexpected form questions

So a scan that surfaces 20 jobs across 12 companies and writes up 6 of them is around 12 + 20 + 12 calls.

Ways to spend less:

- Use Ollama. Free, and nothing leaves your machine.
- Use Groq. Fast and has a free tier.
- Raise `min_score_to_generate` so fewer jobs get written up.
- Use `--limit` while you are still tuning your profile.
- Keep target titles specific. The seniority gate already does the heavy lifting here.

Start with `pipeline run --limit 5` to see what a run costs before turning it loose.

---

## Adding a job board

One function, about forty lines. It takes a board slug and your target titles, and returns a `SourceResult`.

```python
# in pipeline/sources.py

def fetch_workable(board: str, titles: Sequence[str]) -> SourceResult:
    payload = _get_json(
        "https://apply.workable.com/api/v1/widget/accounts/{0}".format(board)
    )
    listings = payload.get("jobs", [])
    jobs: List[Job] = []
    for item in listings:
        title = (item.get("title") or "").strip()
        if not title or not title_matches(title, titles):
            continue
        location = (item.get("location") or {}).get("city") or ""
        jobs.append(
            Job(
                external_id=make_id("workable", board, title, location),
                source="workable",
                company=board,
                title=title,
                url=item.get("url") or "",
                apply_url=item.get("application_url") or item.get("url") or "",
                location=location or None,
                description=strip_html(item.get("description")),
            )
        )
    return SourceResult(jobs, len(listings))


SOURCES = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workable": fetch_workable,     # add it here
}
```

Rules for a new source:

- Return `SourceResult(jobs, seen)`. The `seen` count is what lets the CLI explain a small result set.
- Call `title_matches(title, titles)` so the seniority gate applies.
- Use `make_id(...)` so re-scraping does not create duplicates.
- Raise `SourceError(msg, is_missing=True)` for a 404, so a typo'd slug is reported differently from a network problem.
- Use public endpoints only, with no logins or cookies.

---

## Privacy

Everything lives in your project folder:

```
config/profile.yml      you
.env                    your keys
data/pipeline.db        every job, score, and application
data/resumes/           generated PDFs
data/research/          company briefs
data/browser-profile/   browser session
```

`config/profile.yml`, `.env`, and `data/` are all gitignored, so none of it can be committed by accident.

What leaves your machine:

- Your profile and job descriptions, sent to the AI provider you configured. With Ollama, nothing.
- Ordinary public web requests to job boards and company sites.
- Whatever you submit, sent to the employer, same as applying by hand.

There is no telemetry and no server.

---

## Working on the code

```bash
git clone https://github.com/alyananwar/Pipeline
cd Pipeline
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

The suite runs offline and takes under a second. Network calls in tests are monkeypatched, because a test that needs the internet is a test that fails on a plane.

Layout:

```
pipeline/
  cli.py         commands and terminal output
  sources.py     job boards, and the matching filter
  research.py    company briefs
  matcher.py     fit scoring
  generate.py    cover letters and answers
  resume.py      PDF builder
  apply.py       browser automation
  barriers.py    detecting walls, pure functions over page text
  storage.py     SQLite
  tracker.py     the live display
  settings.py    config loading
  models.py      shared dataclasses
```

Two things worth knowing before changing `apply.py`:

1. Scrapling runs `page_action` inside its own try/except and swallows exceptions. Outcomes travel back through `self._outcome`, not by raising.
2. The browser tab only exists while `page_action` runs. Parking a tab for a human means blocking inside it, which is what `_wait_for_human` does.

And one before changing `tracker.py` or any terminal output: job titles come off third-party boards and go through Rich, which reads `[` as markup. Untrusted text gets `escape()`d or printed with `markup=False`. A title containing `[/x]` used to crash the whole run.
