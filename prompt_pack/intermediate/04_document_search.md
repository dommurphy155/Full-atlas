# 🔍 AI Document Search Engine

**Difficulty:** Intermediate

## Overview
Build a local document search engine that lets you drop in PDFs, Markdown files, and text documents, then query them with natural language. It finds the most relevant passages across your entire document collection.

## Objectives
- Accept PDF, Markdown, and plain text file uploads
- Extract text content from documents
- Build a searchable index of document content
- Natural language search that returns relevant passages
- Rerank results using AI semantic understanding
- Web interface for uploading, searching, and browsing results

## Features
- [ ] File upload (PDF, MD, TXT) with drag-and-drop
- [ ] Text extraction from PDFs (PyMuPDF or pdfplumber)
- [ ] Chunking documents into searchable segments
- [ ] Vector embeddings for semantic search
- [ ] Keyword + semantic hybrid search
- [ ] Relevance-ranked results with source document and page
- [ ] Web dashboard for managing documents and running searches
- [ ] Document metadata (title, source, date added)
- [ ] Delete documents and rebuild index

## Technical Suggestions
- **Python + FastAPI** — backend for processing and serving
- **sentence-transformers** — for local embeddings (no API key needed)
- **FAISS or ChromaDB** — for vector storage and similarity search
- **PyMuPDF** — for PDF text extraction
- **HTMX + Tailwind** — for the dashboard UI
- **SQLite** — for document metadata

## Stretch Goals
- Add a chat interface that answers questions across all documents
- Implement document clustering to auto-organise by topic
- Add a RAG pipeline that uses LLM to synthesise answers from multiple documents
- Support image OCR for scanned PDFs

## Learning Outcomes
You'll learn about vector embeddings, semantic search, document processing pipelines, and how modern AI search systems work under the hood. This is a foundational project for anyone interested in RAG (Retrieval-Augmented Generation).

## AI Instructions
1. Analyse the repository structure before writing any code.
2. Create a detailed implementation plan: ingestion pipeline, embedding model, search engine, UI.
3. Ask clarifying questions if requirements are ambiguous (embedding model, chunk size, search type).
4. Work iteratively — start with text extraction and keyword search, then add embeddings, then semantic search.
5. Explain major architectural decisions (why FAISS over Chroma, chunk size rationale).
6. Keep milestones logically separated: text extraction → keyword search → embeddings → semantic search → UI → polish.
7. Avoid unnecessary complexity — start with simple cosine similarity, add reranking later.
8. Write clear documentation with setup and usage instructions.
9. Add tests for text extraction, embedding generation, and search ranking.
10. Refactor when improvements become obvious — keep the code clean and modular.
11. Pause after completing each major feature and summarise progress.
