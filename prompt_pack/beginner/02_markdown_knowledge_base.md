# 📖 Markdown Knowledge Base

**Difficulty:** Beginner

## Overview
A personal knowledge base built entirely in Markdown files, with a simple web interface for browsing, searching, and organising your notes. No database, no backend — just files on disk and a tiny web server.

## Objectives
- Create, edit, and delete Markdown notes through a web UI
- Full-text search across all notes
- Tag-based organisation system
- Preview rendered Markdown in the browser
- Notes stored as plain `.md` files on disk
- Simple search index built on file scanning

## Features
- [ ] Web UI for listing all notes with titles and previews
- [ ] Create new notes with a title and Markdown content
- [ ] Edit existing notes inline or in a dedicated editor
- [ ] Delete notes with confirmation
- [ ] Full-text search across all note content
- [ ] Tag system — assign tags to notes, filter by tag
- [ ] Markdown preview panel (rendered HTML side-by-side or toggled)
- [ ] Notes persisted as `.md` files in a `notes/` directory
- [ ] Simple search index that rebuilds on changes

## Technical Suggestions
- **Python + Flask/FastAPI** — lightweight server, serves files and handles search
- **Markdown** — `markdown` or `mistune` Python library for rendering
- **SQLite** — optional, for the search index if you want to level up
- **Plain `.md` files** — the source of truth, no lock-in
- **HTMX or vanilla JS** — for dynamic UI without a heavy frontend framework

## Stretch Goals
- Add note linking (`[[note-name]]` wikilinks that become clickable)
- Implement a tag cloud or note graph visualization
- Add version history (diff between saves)
- Export notes as a single PDF or HTML document
- Add a dark mode with syntax-highlighted Markdown preview

## Learning Outcomes
You'll learn how to design a simple but effective data model (files as database), build a CRUD interface, implement search from scratch, and think about information architecture. This project teaches the fundamentals of building tools for yourself — the most valuable kind of software.

## AI Instructions
1. Analyse the existing repository structure before writing any code.
2. Create a detailed implementation plan: data model (file structure), API endpoints, UI layout.
3. Ask clarifying questions if requirements are ambiguous (default note location, search behaviour, tag syntax).
4. Work iteratively — start with file storage and listing, then add search, then tags, then preview.
5. Explain major architectural decisions (why flat files over a database, how search indexing works).
6. Keep milestones logically separated: file I/O → listing UI → search → tags → preview → polish.
7. Avoid unnecessary complexity — no ORM, no complex frontend frameworks unless they genuinely help.
8. Write clear documentation: README with setup, usage, and file structure explanation.
9. Add tests for the search function, tag parsing, and file CRUD operations.
10. Refactor when improvements become obvious — keep the code clean as features are added.
11. Pause after completing each major feature and summarise progress.
