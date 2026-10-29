# AI Usage, Problems & Improvements

> **Note to self before submitting:** this is a draft. Edit every section so it is *true for you* —
> mention the real problems you hit, the real commands you ran, and anything you changed.
> Interviewers will ask about these exact points.

## 1. Which AI tools I used

| Tool | Purpose |
|---|---|
| **Claude (Anthropic)** | Planned the architecture, generated the first version of the code, tests and documentation, and explained the concepts to me |
| **Google Gemini / Groq API** *(delete the one you did not use)* | The LLM *inside* the application that interprets the user's question |
| *(add others, e.g. ChatGPT, Copilot, Cursor — only if you really used them)* | |

## 2. How I used AI during development

1. **Understanding the task** – I gave the assignment, the job description and the CSV to Claude and asked it to explain what was required, since I was new to this.
2. **Design decision** – I chose **"LLM as interpreter" instead of text-to-SQL**: the LLM only returns a small JSON plan; fixed, parameterised SQL computes every number. Reason: reliability and safety (an LLM-written query can silently give a wrong number or contain destructive SQL).
3. **Code generation** – Claude generated the modules (`database`, `analytics`, `planner`, `interpreter`, `llm`, Flask app, UI). *(Describe what you reviewed/changed yourself.)*
4. **Testing** – Tests compare the app's SQLite results with an *independent pandas calculation*, so a bug in either side is detected. The LLM is faked in tests so they run offline.
5. **Review** – I read each file and can explain it. *(Only keep this sentence if it is true — make it true before the interview.)*
6. **LLM prompt** – The prompt in `bi/llm.py` lists the allowed plan shapes, the valid values from the database, a few examples, and tells the model to treat the user's text as data.

## 3. Problems encountered and how they were debugged

| # | Problem | How it was found | Fix |
|---|---|---|---|
| 1 | **Dirty data**: `TXN0034` has quantity 0, `TXN0051` has no unit price, `TXN0033` is *Returned* | Inspected the CSV (`pandas` `isna()`, `value_counts()`) | Row-level validation; invalid rows go to a `rejected_records` table with a reason; Returned rows are kept but excluded from revenue |
| 2 | **Date format**: `03-01-2026` is DD-MM-YYYY, easily misread as MM-DD | Looked at values like `25-01-2026` (no month 25) | Parsed with an explicit `format="%d-%m-%Y"`; stored as ISO dates plus a `month` column |
| 3 | **Wrong answers instead of errors**: "revenue for Smartphone" silently returned the *overall total*; "revenue of each category" returned a total; "Top 3 products" returned only the top 1; "from Jan to March" was treated as a comparison | Manually testing many phrasings against the running API | Added an unknown-entity guard, a breakdown keyword rule, a top-N refusal, and a clarification message for ambiguous month phrasing — each with a regression test |
| 4 | **LLM output is untrusted**: it may return prose, markdown fences, wrong intent names or invented product names | Reasoning about failure modes; tests with a faked LLM | JSON extraction that tolerates fences; a strict plan validator (whitelists + values must exist in the DB); automatic fallback to the rule-based interpreter |
| 5 | **Destructive requests** ("delete all sales records") | Security thinking while designing | Blocked before the LLM (HTTP 403), plus a read-only SQLite connection; a test proves the row count is unchanged |
| 6 | *(add a real problem you faced, e.g. API key / model name / port / install error)* | | |

**General debugging approach:** reproduce with the smallest input → read the error / response → check my assumption against the data → fix → add a test so it cannot return.

## 4. What I would improve with another day

* More question types: units sold, order counts, average order value, top-N, date ranges.
* Follow-up questions with conversation memory ("and for the South region?").
* Charts in the UI.
* A small evaluation set to measure the LLM's plan accuracy and compare models/prompts.
* CI (GitHub Actions) and a Dockerfile.
* Optional text-to-SQL mode with a proper SQL parser/validator (single SELECT, table & column allow-list) behind a feature flag.

## 5. What works / what doesn't (honest status)

* ✅ All five example questions are answered, with filters; 32 automated tests pass.
* ✅ Works fully **without** an API key (rule-based interpreter).
* ⚠️ The live LLM call (Gemini / OpenAI-compatible) is covered by tests with mocked HTTP responses. *(After you run it with your own key, update this line: "verified with <provider/model> on <date>".)*
* ⚠️ Rule-based interpretation is keyword-based; unusual phrasings may be refused rather than answered.
