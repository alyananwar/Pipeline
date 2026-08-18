# Pipeline

Find jobs, research the company, write the application, and apply. Runs on your machine, with your LLM and your resume.

Pipeline scans public job boards, scores every posting against your actual background, researches the company, writes a cover letter and screening answers grounded in that research, tailors a one-page resume, then opens a browser and fills the form. When it hits something only a human can do, it stops and tells you.

```
┌─ Pipeline ───────────────────────── 14 jobs · 3 blocked ─┐
│ ⣾ applying to Ramp · Software Engineer Intern            │
│                                                           │
│  JOB                      SOURCE      STAGE      STATUS   │
│  Security Analyst Intern  greenhouse  apply    ✓ submitted│
│  SOC Intern @ Cisco       lever       apply    ⏸ verify   │
│  Cyber Intern @ Deloitte  workday     apply    ⏸ account  │
│  IT Intern @ Adobe        ashby       generate ⣾ tailoring│
│                                                           │
│ ⚠ 3 BLOCKED · needs you                                   │
│   Cisco · email verification · code sent to your inbox    │
└───────────────────────────────────────────────────────────┘
```

## Install

```bash
pip install git+https://github.com/alyananwar/Pipeline
```

Python 3.9 or newer. That is the whole install for everything except the browser stage.

## Setup

Three steps.

**1. Create your config**

```bash
pipeline init
```

That writes `config/profile.yml` and `.env`.

**2. Fill in `config/profile.yml`**

Your details, the roles you want, and the boards to scan. Board slugs come straight out of a careers URL:

| Careers URL | Source | Board slug |
|---|---|---|
| `boards.greenhouse.io/anthropic` | `greenhouse` | `anthropic` |
| `jobs.lever.co/spotify` | `lever` | `spotify` |
| `jobs.ashbyhq.com/ramp` | `ashby` | `ramp` |

```yaml
sources:
  - source: greenhouse
    board: anthropic
  - source: lever
    board: spotify
```

Already have a resume? Point at it and Pipeline will use yours instead of building one:

```yaml
preferences:
  baseline_resume: "~/Documents/my-resume.pdf"
```

Leave it empty and Pipeline builds a one-page ATS-friendly resume from the `experience`, `projects`, `skills`, and `education` sections of your profile.

**3. Pick your LLM in `.env`**

```bash
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-5
LLM_API_KEY=sk-ant-...
```

Works with `anthropic`, `openai`, `openrouter`, `groq`, and `ollama`. For free and fully local, run [Ollama](https://ollama.com) and use:

```bash
LLM_PROVIDER=ollama
LLM_MODEL=llama3.1
```

No key needed and nothing leaves your machine.

## Use

```bash
pipeline run
```

That is the whole thing: scrape, research, score, write, apply. Stop it any time with Ctrl-C, progress is saved, run it again to pick up where it left off.

Run stages on their own when you want more control:

```bash
pipeline scrape                 # pull postings from your boards
pipeline research               # build company intel briefs
pipeline match                  # score every posting against your profile
pipeline generate               # cover letters, answers, tailored resumes
pipeline apply                  # open a browser and fill the forms
pipeline status                 # where everything stands
pipeline unblock                # what needs you right now
```

Useful flags:

```bash
pipeline run --no-apply         # everything except the browser
pipeline run --limit 10         # cap how many jobs per stage
pipeline generate --min-score 80
pipeline unblock --resolve 2    # mark blocked item 2 as done
```

## Applying in a browser

The apply stage needs a real browser, which is a separate install:

```bash
pip install "scrapling[fetchers]"
scrapling install
```

`scrapling install` downloads browser binaries and takes a few minutes. Skip it entirely if you only want Pipeline to find and write applications, and submit them yourself.

### Submitting

By default Pipeline **fills the form and stops at the submit button**:

```
  Anthropic · awaiting submit
  Form filled with 9 fields. Review it, then confirm.
  [Enter] submit   [s] skip   [d] I finished it myself
  >
```

The tab stays open the whole time. Change it with `--submit`:

| Mode | Behavior |
|---|---|
| `confirm` | Fill, then ask. **Default.** |
| `auto` | Submit without asking. |
| `never` | Fill only, never submit. |

Auto is there if you want it. Confirm is the default because a wrong answer cannot be unsubmitted.

### When it gets stuck

Pipeline detects and reports these, leaving the tab open so you can finish:

| Barrier | What it means |
|---|---|
| `account_creation` | Site wants an employer account first. Common on Workday and iCIMS. |
| `email_verification` | A code went to your inbox. |
| `captcha` | Bot check on the page. |
| `assessment` | The posting requires a timed test. |
| `unknown_field` | A required question could not be answered from your profile. |
| `login_expired` | The saved browser session signed out. |
| `awaiting_submit` | Form is filled and ready for your review. |

Nothing is lost when you quit. `pipeline unblock` shows the queue any time.

## How well does it apply?

Depends on the ATS.

| ATS | Coverage |
|---|---|
| Greenhouse, Lever, Ashby, Workable | Usually fills and submits the whole form |
| Workday, iCIMS, Taleo, SmartRecruiters | Fills what it can, stops at the account wall |
| Anything else | Reads the form, maps fields with the LLM, fills what it understands |

## Things it will not do

- **Invent experience.** Every claim comes from your profile. If the model cannot answer a question honestly from what you gave it, that becomes a barrier instead of a guess.
- **Answer work authorization questions you did not answer.** Those are legal questions on a legal form. If `work_authorization` is blank in your profile, Pipeline leaves the field for you.
- **Scrape sites that require a login.** The default sources are public JSON APIs. No accounts, no cookies, no risk to anything you own.

## Optional extras

**Better research.** Add a key to `.env` and the research stage gains general web search:

```bash
BRAVE_API_KEY=...
# or
SERPER_API_KEY=...
```

Without one, research still runs on the job description, the company's own site, and Hacker News.

**Browser-based research.** Set `PIPELINE_BROWSER=1` to let the research stage load JS-heavy and bot-protected pages. Needs the browser install above.

## Where your data lives

Everything is local:

```
config/profile.yml      you, in one file
.env                    your keys
data/pipeline.db        every job, score, and application
data/resumes/           generated PDFs
data/research/          company briefs as markdown
data/browser-profile/   browser session, so you log in once
```

The only thing that leaves your machine is what you send to the LLM provider you configured, plus ordinary public web requests. `config/profile.yml`, `.env`, and `data/` are gitignored.

## Development

```bash
git clone https://github.com/alyananwar/Pipeline
cd Pipeline
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Adding a job board means writing one function in `pipeline/sources.py` that returns `List[Job]`, then registering it in `SOURCES`. The three that ship are about forty lines each.

## License

MIT. See [LICENSE](LICENSE).

Design inspiration from [career-ops](https://github.com/santifer/career-ops) by Santiago Fernández de Valderrama, also MIT. Pipeline is an independent implementation and shares no code with it.
