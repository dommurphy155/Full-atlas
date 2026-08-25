# 🏢 Team Knowledge Platform

**Difficulty:** Professional

## Overview
A self-hosted knowledge management platform for teams — think Notion meets a search engine. It ingests documents from multiple sources, chunks and embeds them, and provides a powerful search interface with AI-powered summarisation and Q&A. Built for teams that need their institutional knowledge to actually be findable.

## Objectives
- Multi-source document ingestion (URLs, files, folders, Notion export)
- Automatic chunking and embedding generation
- Hybrid search: keyword + semantic relevance
- AI-powered summarisation of search results
- Q&A interface that answers questions from the knowledge base
- Team workspaces with role-based access
- Document versioning and change tracking
- Usage analytics and popular content insights
- REST API for programmatic access
- Admin dashboard for managing sources and users

## Features
- [ ] Document ingestion from URLs (web scraping)
- [ ] File upload: PDF, DOCX, Markdown, TXT, CSV
- [ ] Folder sync: watch a directory for new/changed files
- [ ] Notion export import (.json)
- [ ] Automatic text chunking with configurable overlap
- [ ] Embedding generation via local model or API provider
- [ ] Vector store: ChromaDB or FAISS for similarity search
- [ ] Hybrid search: BM25 keyword + vector semantic
- [ ] AI summarisation of top search results
- [ ] Q&A mode: ask a question, get an answer with citations
- [ ] Team workspaces with separate knowledge bases
- [ ] Role-based access: viewer, editor, admin
- [ ] Document versioning with diff view
- [ ] Change tracking: who added/edited what and when
- [ ] Usage analytics: most searched, most viewed, top contributors
- [ ] REST API for all operations (ingest, search, manage)
- [ ] Admin dashboard for workspace and user management
- [ ] Structured logging and error tracking
- [ ] Docker Compose for deployment
- [ ] Unit and integration tests

## Technical Suggestions
- **Python + FastAPI** — async backend, clean API structure
- **PostgreSQL** — metadata, user management, documents
- **ChromaDB or FAISS** — vector storage and similarity search
- **sentence-transformers** — local embeddings (no API cost)
- **BeautifulSoup + httpx** — web scraping for URL ingestion
- **Unstructured** — document parsing for PDF/DOCX/MD
- **Redis** — caching search results and rate limiting
- **Jinja2** — admin dashboard templates
- **Docker + Docker Compose** — reproducible deployment
- **pytest + httpx** — testing framework
- **structlog** — structured JSON logging

## Stretch Goals
- Implement a Slack bot that answers questions from the knowledge base
- Add a browser extension that highlights and saves web content to the platform
- Build a "knowledge graph" that maps relationships between documents and topics
- Implement RAG pipeline that uses LLM to synthesise multi-document answers
- Add real-time collaborative editing on documents
- Implement an API rate limiter per team and per user

## Learning Outcomes
You'll learn document processing pipelines, vector search, RAG fundamentals, multi-tenant architecture, and how to build a product that teams actually adopt. This teaches you to handle messy real-world data and build systems that scale with a team.

## AI Instructions
1. Analyse the repository structure before writing any code. Check for existing config, requirements, or setup files.
2. Create a detailed implementation plan: database schema, ingestion pipeline, search engine, API routes, auth model.
3. Ask clarifying questions if requirements are ambiguous (embedding model choice, chunk size, vector store).
4. Work iteratively — start with document ingestion and storage, then search, then AI features, then team features.
5. Explain major architectural decisions (why ChromaDB over Pinecone, chunk size rationale, hybrid search approach).
6. Keep milestones logically separated: ingestion → storage → search → AI features → team features → analytics → polish.
7. Avoid unnecessary complexity — start with file upload only, add URL and folder sync later.
8. Write clear documentation: README with architecture diagram, setup steps, and usage guide.
9. Add tests for document parsing, embedding generation, search ranking, and access control.
10. Refactor when improvements become obvious — keep the ingestion pipeline modular for new source types.
11. Pause after completing each major feature and summarise progress with what was built and what's next.
