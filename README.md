# Pipeline

Pipeline finds job postings you actually qualify for, researches the company, writes your cover letter and screening answers, tailors your resume, then opens a browser and fills the application form. It stops and asks you before submitting anything.

It runs on your computer. Your resume and your answers stay there.

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

You do not need to know Python or have a GitHub account. Every command below is copy-paste.

---

## Before you start

You need **Python 3.9 or newer**. To check, open **Terminal** (Mac: press Cmd+Space, type `Terminal`, hit Enter. Windows: press Start, type `PowerShell`, hit Enter) and run:

```bash
python3 --version
```

If you see a version number like `Python 3.11.5`, you are set. If you see "command not found", install Python from [python.org/downloads](https://www.python.org/downloads/), then close and reopen Terminal.

Windows note: use `python` instead of `python3` in every command below.

---

## Install

Run these four commands in Terminal, one at a time, in order.

**1. Make a folder for Pipeline and go into it**

```bash
mkdir ~/pipeline && cd ~/pipeline
```

**2. Create an isolated Python environment**

```bash
python3 -m venv pipeline-env
```

**3. Turn it on**

```bash
source pipeline-env/bin/activate
```

On Windows PowerShell, that one command is different:

```powershell
pipeline-env\Scripts\Activate.ps1
```

Your prompt now starts with `(pipeline-env)`. That means it worked.

**4. Install Pipeline**

```bash
pip install git+https://github.com/alyananwar/Pipeline
```

Check it worked:

```bash
pipeline --help
```

### Every time you come back

Terminal forgets step 3 when you close it. So each new session, run:

```bash
cd ~/pipeline
source pipeline-env/bin/activate
```

If you ever see `command not found: pipeline`, that is all this is. Run those two lines and carry on.

---

## Set it up

Three steps, still in the `~/pipeline` folder.

**1. Create your config files**

```bash
pipeline init
```

That creates `config/profile.yml` (about you) and `.env` (your settings).

**2. Fill in `config/profile.yml`**

Open it in any text editor. On Mac, `open -e config/profile.yml` opens it in TextEdit.

It arrives filled with a fake example person so you can see the shape of it. Replace their details with yours.

The two parts that matter most:

**Your target roles.** Be specific about level. Pipeline matches on seniority, so "Software Engineer Intern" returns internships only, never Senior or Staff roles.

```yaml
target_roles:
  primary:
    - "Software Engineer Intern"
    - "Backend Engineer Intern"
```

**The company job boards to scan.** You get the slug from a company's careers page URL:

| Careers page URL                 | `source`     | `board`     |
| -------------------------------- | ------------ | ----------- |
| `boards.greenhouse.io/anthropic` | `greenhouse` | `anthropic` |
| `jobs.lever.co/spotify`          | `lever`      | `spotify`   |
| `jobs.ashbyhq.com/ramp`          | `ashby`      | `ramp`      |

```yaml
sources:
  - source: greenhouse
    board: anthropic
  - source: lever
    board: spotify
```

Add as many as you want. More boards means more jobs found.

Already have a resume you like? Point at it and Pipeline uses yours instead of building one:

```yaml
preferences:
  baseline_resume: "~/Documents/my-resume.pdf"
```

Leave that blank and Pipeline builds a clean one-page resume from your profile.

**3. Try it before setting up anything else**

```bash
pipeline scrape
```

This needs no account and no key. It scans your boards and shows what it found:

```
Scanned 770 postings across 3 boards. 6 matched your target roles, 6 new.
```

If that number looks right, keep going. If it is zero, Pipeline tells you why and what to change.

---

## Add an AI model

The writing stages need an AI model. Open `.env` and pick one.

**Free, runs on your own computer, nothing sent anywhere:**

Install [Ollama](https://ollama.com), then run `ollama pull llama3.1` once. Then put this in `.env`:

```bash
LLM_PROVIDER=ollama
LLM_MODEL=llama3.1
```

You do not need an account or a card for this option.

**Better writing, costs money per application:**

```bash
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-5
LLM_API_KEY=sk-ant-your-key-here
```

Also works with `openai`, `openrouter`, and `groq`. Groq has a free tier.

---

## Run it

```bash
pipeline run
```

That is the whole thing: find jobs, research the companies, score them against you, write the applications, fill the forms.

Press Ctrl+C any time to stop. Everything is saved. Run the same command again to pick up where you left off.

### Running one stage at a time

| Command             | What it does                          | Needs an AI model |
| ------------------- | ------------------------------------- | ----------------- |
| `pipeline scrape`   | Find postings on your boards          | No                |
| `pipeline research` | Read up on each company               | Yes               |
| `pipeline match`    | Score each job against you, 0 to 100  | Yes               |
| `pipeline generate` | Write cover letters, answers, resumes | Yes               |
| `pipeline apply`    | Open a browser and fill the forms     | Yes               |
| `pipeline status`   | Show where everything stands          | No                |
| `pipeline unblock`  | Show what is waiting on you           | No                |

Useful options:

```bash
pipeline run --no-apply      # do everything except open a browser
pipeline run --limit 10      # only handle 10 jobs, good for a first try
pipeline generate --min-score 80   # only write up strong matches
```

---

## Applying in a browser

The `apply` stage drives a real browser, which is a separate install:

```bash
pip install "scrapling[fetchers]"
scrapling install
```

The second command downloads browser files and takes a few minutes.

You can skip this entirely. Without it, Pipeline still finds jobs and writes every application, and you submit them yourself.

### It asks before submitting

By default Pipeline fills the form and then stops:

```
  Anthropic · awaiting submit
  Form filled with 9 fields. Review it, then confirm.
  [Enter] submit   [s] skip   [d] I finished it myself
  >
```

The browser tab stays open so you can read the form first. Nothing is sent until you press Enter.

### When it gets stuck

Some sites need a human. Pipeline stops, leaves the tab open, and tells you which one:

| It says              | What to do                                                      |
| -------------------- | --------------------------------------------------------------- |
| `account_creation`   | The site wants you to make an account first. Common on Workday. |
| `email_verification` | Check your inbox for a code, type it into the open tab.         |
| `captcha`            | Click the "I am not a robot" box in the open tab.               |
| `assessment`         | The job requires a timed test. That one is on you.              |
| `unknown_field`      | A question could not be answered from your profile.             |
| `awaiting_submit`    | The form is filled and waiting for you to look it over.         |

Run `pipeline unblock` any time to see the full list.

---

## When something goes wrong

| What you see                                  | What it means                                                  | Fix                                                           |
| --------------------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------- |
| `command not found: pipeline`                 | Terminal forgot your environment                               | `cd ~/pipeline` then `source pipeline-env/bin/activate`       |
| `error: externally-managed-environment`       | You skipped the venv step                                      | Go back to Install step 2 and do steps 2, 3, 4 in order       |
| `No profile at config/profile.yml`            | You are in the wrong folder                                    | `cd ~/pipeline`, then run the command again                   |
| `skipped greenhouse/xyz ... (check the slug)` | Board name typo                                                | Recheck the slug against the company's careers URL            |
| `Nothing matched`                             | Your target roles are too narrow, or those boards have nothing | Add more titles under `target_roles.primary`, add more boards |
| `LLM_API_KEY is not set`                      | No AI model configured yet                                     | See "Add an AI model" above, or use free Ollama               |
| Applying says it needs a browser              | Browser part is not installed                                  | `pip install "scrapling[fetchers]"` then `scrapling install`  |

Still stuck? [Open an issue](https://github.com/alyananwar/Pipeline/issues) and paste what you saw.

---

## What it will not do

- **Make things up.** Every claim comes from your profile. If the AI cannot answer a question honestly from what you wrote, it stops and asks you instead of guessing.
- **Answer work authorization questions you left blank.** Those are legal questions on a legal form. If you did not fill it in, Pipeline leaves it for you.
- **Log into anything.** It reads public job listings only, so it never touches an account of yours.
- **Submit without asking.** Unless you explicitly turn that on.

---

## Where your stuff lives

Everything stays in your `~/pipeline` folder:

```
config/profile.yml      you, in one file
.env                    your settings and key
data/pipeline.db        every job, score, and application
data/resumes/           the PDFs it made
data/research/          company notes you can read
```

The only thing that leaves your computer is what gets sent to the AI provider you chose, plus normal public web requests to job boards. With Ollama, not even that.

To delete everything, delete the folder.

---

## More detail

[docs/GUIDE.md](docs/GUIDE.md) covers how matching and scoring work, every command flag, which application systems it handles well, adding new job boards, and how to work on the code.

---

## License

MIT. See [LICENSE](LICENSE).

Design inspiration from [career-ops](https://github.com/santifer/career-ops) by Santiago Fernández de Valderrama, also MIT. Pipeline is an independent implementation and shares no code with it.
