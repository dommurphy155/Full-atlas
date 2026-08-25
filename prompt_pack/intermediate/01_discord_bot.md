# 🤖 Discord Bot with AI Integration

**Difficulty:** Intermediate

## Overview
A Discord bot that does more than respond to commands — it actually helps your server run. It moderates, welcomes new members, provides AI-powered assistance, and logs activity. This is a real production bot you'd be proud to run on your community server.

## Objectives
- Respond to slash commands for common server operations
- AI-powered Q&A assistant that answers questions using your server's context
- Welcome messages for new members with role assignment
- Moderation tools: auto-mod for spam, toxic content detection, slow mode
- Activity logging with a searchable dashboard
- Configurable via a web dashboard (not just commands)

## Features
- [ ] Slash commands: `/help`, `/ping`, `/info`, `/roll`, `/weather`
- [ ] AI assistant: `/ask <question>` — answers using a knowledge base
- [ ] Welcome messages with configurable templates and auto-role assignment
- [ ] Auto-mod: detect spam patterns, toxic language, and invite links
- [ ] Slow mode per channel with configurable durations
- [ ] Activity logging: message counts, active users, top channels
- [ ] Web dashboard for configuration (bot settings, channels, roles)
- [ ] Persistent configuration in a SQLite database
- [ ] Graceful error handling and rate limiting

## Technical Suggestions
- **Python + discord.py** — the standard Discord library, well-maintained
- **FastAPI** — for the web dashboard
- **SQLite** — lightweight persistence for config and logs
- **OpenAI-compatible API** — for the AI assistant (via Atlas proxy)
- **asyncio** — Discord bots are inherently async
- **Docker** — for easy deployment to a VPS

## Stretch Goals
- Add multi-server support with per-server configuration
- Implement a plugin system so community members can add commands
- Add voice channel activity tracking
- Build a moderation dashboard with case management
- Add automatic thread creation for active discussions

## Learning Outcomes
You'll learn async programming, API integration, database design, bot architecture, and how to build a system that runs continuously. This teaches you to think about reliability, error handling, and user experience in a persistent application.

## AI Instructions
1. Analyse the repository structure before writing any code.
2. Create a detailed implementation plan: bot architecture, command structure, database schema.
3. Ask clarifying questions if requirements are ambiguous (server size, moderation strictness, AI provider).
4. Work iteratively — start with command framework, then add AI assistant, then moderation, then dashboard.
5. Explain major architectural decisions (why discord.py over next.py, how the AI integration works).
6. Keep milestones logically separated: command framework → AI assistant → moderation → dashboard → database → polish.
7. Avoid unnecessary complexity — start simple, add features based on real needs.
8. Write clear documentation: README with setup, bot invite link, and configuration guide.
9. Add tests for command handlers, moderation logic, and database operations.
10. Refactor when improvements become obvious — keep the code clean and well-structured.
11. Pause after completing each major feature and summarise progress.
