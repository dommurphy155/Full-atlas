# 📧 AI Email Assistant

**Difficulty:** Intermediate

## Overview
An email automation assistant that reads your inbox, categorises messages, drafts intelligent replies, and manages your email workflow. It's like having a personal email secretary that learns your style.

## Objectives
- Connect to an email account (IMAP) and read incoming messages
- Automatically categorise emails (personal, work, newsletters, spam)
- Draft intelligent reply suggestions based on email content
- Summarise long email threads
- Manage a todo list extracted from email action items
- Send replies via SMTP with your approved tone

## Features
- [ ] IMAP email reading with folder/label support
- [ ] Auto-categorisation of incoming emails
- [ ] AI-generated reply suggestions (one-click or edit-then-send)
- [ ] Thread summarisation for long email conversations
- [ ] Action item extraction → todo list
- [ ] SMTP sending with configurable tone (formal, casual, direct)
- [ ] Configurable rules (auto-reply to certain senders, auto-tag, auto-archive)
- [ ] Web dashboard for managing rules and reviewing AI suggestions
- [ ] SQLite database for email history and rules

## Technical Suggestions
- **Python + imaplib/smtplib** — standard library for email
- **FastAPI** — for the web dashboard
- **OpenAI-compatible API** — for AI summarisation and reply drafting
- **SQLite** — for rules, email history, and extracted action items
- **python-dotenv** — for managing email credentials via environment variables
- **asyncio** — for handling multiple email operations efficiently

## Stretch Goals
- Add calendar integration — extract meeting requests and create events
- Implement email prioritisation based on sender importance
- Add a "snooze" feature that delays notifications
- Build a Chrome extension for quick email actions from Gmail

## Learning Outcomes
You'll learn IMAP/SMTP protocols, async programming for I/O-bound tasks, AI prompt engineering for text processing, and how to build a system that interacts with external services reliably. You'll also think about privacy and security when handling email credentials.

## AI Instructions
1. Analyse the repository structure before writing any code.
2. Create a detailed implementation plan: email I/O layer, AI integration, dashboard, database schema.
3. Ask clarifying questions if requirements are ambiguous (email provider, AI provider, tone preferences).
4. Work iteratively — start with IMAP reading and categorisation, then add AI replies, then dashboard.
5. Explain major architectural decisions (how email credentials are secured, how AI prompts are structured).
6. Keep milestones logically separated: IMAP reading → categorisation → AI replies → dashboard → rules → polish.
7. Avoid unnecessary complexity — start with basic email reading, add features based on real needs.
8. Write clear documentation with setup instructions and security notes (credential management).
9. Add tests for email parsing, categorisation logic, and rule evaluation.
10. Refactor when improvements become obvious — keep the code clean and well-structured.
11. Pause after completing each major feature and summarise progress.
