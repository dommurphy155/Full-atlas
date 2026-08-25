# 🧠 AI-Powered Notes App

**Difficulty:** Beginner

## Overview
A notes application that uses AI to help you write better notes — summarising long text, generating tags automatically, and suggesting related notes. It's your second brain, powered by AI but fully local.

## Objectives
- Create and edit rich-text notes with a clean editor
- AI-powered summarisation of long notes
- Automatic tag generation based on note content
- Related notes suggestions (semantic similarity)
- Search across all notes with AI-assisted relevance ranking
- Local-first storage with optional cloud sync

## Features
- [ ] Rich text editor (bold, italic, headings, lists, code blocks)
- [ ] Save and delete notes with confirmation
- [ ] AI summarisation — paste long text, get a concise summary
- [ ] Auto-tag generation — AI suggests 3-5 tags per note
- [ ] Related notes — AI finds notes with similar content
- [ ] Full-text search with AI relevance scoring
- [ ] Notes stored as local files (Markdown or JSON)
- [ ] Optional export to Markdown or PDF

## Technical Suggestions
- **Python + FastAPI** — backend for AI processing and file management
- **OpenAI-compatible API** — for summarisation, tagging, and embeddings (via Atlas proxy or direct)
- **Sentence-transformers** — for local semantic similarity (no API needed)
- **Markdown** — as the storage format for portability
- **HTMX or vanilla JS** — for the frontend
- **Optional: SQLite** — for search indexing if scaling up

## Stretch Goals
- Add a daily note/journal mode with date-based organisation
- Implement note linking with `[[wikilinks]]`
- Add a "today's highlights" view that surfaces recent and related notes
- Build a CLI tool for quick note creation from the terminal
- Add end-to-end encryption for privacy-sensitive notes

## Learning Outcomes
You'll learn how to integrate AI APIs into a practical application, design a file-based storage system, implement semantic search, and think about the user experience of AI-assisted tools — understanding when AI helps and when it gets in the way.

## AI Instructions
1. Analyse the repository structure before writing any code.
2. Create a detailed implementation plan: data model, AI integration points, UI layout.
3. Ask clarifying questions if requirements are ambiguous (AI provider, storage format, editor type).
4. Work iteratively — start with basic CRUD, then add AI summarisation, then tags, then related notes.
5. Explain major architectural decisions (why this AI provider, how embeddings work locally vs via API).
6. Keep milestones logically separated: CRUD → AI summarise → auto-tag → related notes → search → polish.
7. Avoid unnecessary complexity — start with simple file storage, only add a database if needed.
8. Write clear documentation including how to configure the AI API key.
9. Add tests for the AI integration functions (mock the API calls).
10. Refactor when improvements become obvious — keep the code clean as features are added.
11. Pause after completing each major feature and summarise progress.
