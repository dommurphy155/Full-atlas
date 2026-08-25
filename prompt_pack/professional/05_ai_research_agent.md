# 🔬 AI Research Agent

**Difficulty:** Professional

## Overview
An autonomous research agent that takes a research question, searches multiple sources, reads and synthesises the findings, and produces a structured research report with citations. It works like a junior researcher that never sleeps — you give it a question, it gives you a paper.

## Objectives
- Accept natural language research questions
- Search multiple sources: web, academic papers, documentation, GitHub
- Read and extract key information from each source
- Synthesise findings into a structured report with citations
- Support iterative refinement: ask follow-up questions to improve results
- Save research history and build a personal knowledge base
- Export reports in multiple formats (Markdown, PDF, HTML)
- API endpoint for programmatic research queries
- Usage analytics and research history dashboard

## Features
- [ ] Web search integration (multiple search engines)
- [ ] Academic paper search (arXiv, Semantic Scholar, PubMed)
- [ ] Documentation search (Read the Docs, MDN, official docs)
- [ ] GitHub repository search and code analysis
- [ ] Source reading and key information extraction
- [ ] Multi-source synthesis and cross-referencing
- [ ] Structured report output with inline citations
- [ ] Report formats: Markdown, PDF, HTML
- [ ] Iterative refinement: "dig deeper into X", "compare A and B"
- [ ] Research history with searchable log
- [ ] Saved research collections and tags
- [ ] Source credibility scoring
- [ ] Duplicate detection across research sessions
- [ ] REST API for triggering research and retrieving results
- [ ] Research dashboard with analytics (most researched topics, sources used)
- [ ] Structured logging and error tracking
- [ ] Docker Compose for deployment
- [ ] Unit and integration tests

## Technical Suggestions
- **Python + FastAPI** — async backend, clean API structure
- **PostgreSQL** — research history, saved collections, metadata
- **Tavily, Serper, or Exa** — web search APIs
- **arXiv API + Semantic Scholar API** — academic paper search
- **BeautifulSoup + httpx** — web content extraction
- **sentence-transformers** — for source relevance scoring
- **Markdown + WeasyPrint or pdfkit** — PDF export
- **Redis** — caching search results and rate limiting
- **Jinja2** — report templates and dashboard
- **Docker + Docker Compose** — reproducible deployment
- **pytest + httpx** — testing
- **structlog** — structured JSON logging

## Stretch Goals
- Implement a "research agent" that autonomously follows leads across multiple searches
- Add a browser automation layer to access paywalled academic papers
- Build a citation graph that visualises relationships between sources
- Implement multi-agent research: parallel agents each search a different source type
- Add a Slack/Discord bot that delivers research results to a channel
- Implement a RAG pipeline that uses your saved research as context for new queries

## Learning Outcomes
You'll learn autonomous agent design, multi-source data aggregation, information extraction and synthesis, report generation, and how to build a system that does genuine intellectual work. This teaches you to think about agent reliability, source quality, and the architecture of systems that reason.

## AI Instructions
1. Analyse the repository structure before writing any code. Check for existing config, requirements, or setup files.
2. Create a detailed implementation plan: data models, search pipeline, synthesis engine, report generation, API structure.
3. Ask clarifying questions if requirements are ambiguous (default search sources, report format, export options).
4. Work iteratively — start with web search and report generation, then add academic search, then GitHub, then iterative refinement.
5. Explain major architectural decisions (why Tavily over Serper, how synthesis works, report template design).
6. Keep milestones logically separated: search → extraction → synthesis → report → history → API → polish.
7. Avoid unnecessary complexity — start with a single search source, add more later.
8. Write clear documentation: README with architecture diagram, setup steps, and research workflow guide.
9. Add tests for search orchestration, source extraction, synthesis logic, and report generation.
10. Refactor when improvements become obvious — keep the search pipeline modular so new source types can be added easily.
11. Pause after completing each major feature and summarise progress with what was built and what's next.
