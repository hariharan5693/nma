# NMA — New Market Onboarding Agent

Agentic pipeline that onboards a **new market** into the MBR/LOKI ecosystem.
A user uploads a new market's raw Excel/CSV files; the agent profiles them,
maps their columns against the PMI **global template**, and returns a
**mapping + gap analysis** (matched / unmapped / missing-mandatory fields per
domain sheet) plus a **market-readiness verdict** — all driven by the LLM.

Built as a **LangGraph Platform** application (not Bedrock AgentCore), runnable
locally with `langgraph dev` and deployable to PMI's self-hosted LangSmith.

---

## Architecture

```
START
  → load_template      parse the bundled global template into a structured schema
  → ingest_files       profile every uploaded sheet (columns, dtypes, sample rows)
  → classify_files     [LLM] map each uploaded file/sheet → template domain sheet
  → map_columns        [LLM] map source columns → STANDARD_FIELD_NAME + confidence
  → gap_analysis       [code] matched / unmapped-extra / missing (by M/C/O flag) per sheet
  → summarize          [LLM] executive onboarding summary + readiness verdict
  → END
```

State is based on `MessagesState`, so conversation memory works locally
(in-memory checkpointer) and automatically with PostgreSQL when deployed —
and the agent can be registered in LiteLLM with `protocol: "a2a"` for
stateful multi-turn follow-ups about the mapping.

---

## Project layout

```
nma/
├── langgraph.json              deployment config (graph = src/agent/graph.py:graph)
├── pyproject.toml              dependencies
├── .env / .env.example         LiteLLM + LangSmith config (.env gitignored)
├── src/agent/
│   ├── graph.py                compiled `graph` (Context + State + nodes + edges)
│   ├── state.py                OnboardingState (MessagesState + pipeline fields)
│   ├── core/                   settings.py, llm.py (ChatOpenAI → LiteLLM /v1)
│   ├── services/               template_parser.py, excel_reader.py
│   ├── models/                 schemas.py (TemplateField, ColumnMapping, GapReport, ...)
│   ├── nodes/                  the 6 pipeline nodes
│   └── prompts/                system_prompts.py, user_prompts.py
├── templates/global_template.xlsx   bundled standard template
├── data/sample_india/          real India files for end-to-end testing
├── interface/app.py            Streamlit upload UI (local testing)
├── scripts/                    test_connectivity.py, run_local.py
└── tests/
```

---

## Local setup

```bash
# From the nma/ directory, Python 3.11+
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -e .                   # install the agent package
pip install -e ".[local,dev]"      # + Streamlit UI and test tools

# Fill in the two keys in .env (LANGSMITH_API_KEY, OPENAI_API_KEY)
```

### 1. Verify connectivity first
```bash
python scripts/test_connectivity.py
```
Confirms LiteLLM `/v1` and LangSmith are reachable and the model responds.

### 2. Run the graph

**Option A — CLI over a folder of files:**
```bash
python scripts/run_local.py --market "Philippines" --folder data/sample_india
```

**Option B — LangGraph Studio:**
```bash
langgraph dev
```
Opens Studio at the printed URL; test the `agent` graph interactively.
Traces appear in the LangSmith project `new_market_onboarding`.

**Option C — Streamlit upload UI (user-facing):**
```bash
streamlit run interface/app.py
```

---

## Deployment (later — see .kiro/steering/nma-deployment.md)

Tenant `mmiana-dev`, DEV environment. Build → push to Artifactory →
create LangSmith deployment → register in LiteLLM (`protocol: "a2a"`).
