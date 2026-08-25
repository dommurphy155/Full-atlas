# 🗺️ Personal Command Dashboard

**Difficulty:** Beginner

## Overview
Build a single-page web dashboard that gives you a beautiful at-a-glance view of your life: tasks, weather, calendar events, and quick links to your tools. Think of it as your personal command center — the page you open first every morning.

## Objectives
- A responsive single-page app with a clean, modern UI
- Displays a to-do list with add/complete/delete functionality
- Shows current weather for a configurable city
- Lists upcoming calendar events (mock data or real calendar API)
- Quick-access links to frequently used tools (GitHub, email, docs)
- Persistent data in the browser (localStorage)

## Features
- [ ] Task list with add, toggle-complete, and delete actions
- [ ] Tasks persist across page reloads via localStorage
- [ ] Weather widget showing current conditions and temperature
- [ ] Calendar section displaying the next 5 upcoming events
- [ ] Quick-link bar with at least 4 configurable shortcuts
- [ ] Dark/light theme toggle
- [ ] Responsive layout that works on mobile and desktop
- [ ] Clean, readable typography and spacing

## Technical Suggestions
- **HTML/CSS/JS** — vanilla is fine, or use a lightweight framework like Alpine.js or HTMX
- **Tailwind CSS** — for rapid, clean styling without writing custom CSS
- **Open-Meteo API** — free weather data, no API key required
- **localStorage** — built-in browser persistence, no backend needed

## Stretch Goals
- Add a Pomodoro timer widget
- Integrate a real calendar API (Google Calendar)
- Add a notes widget with rich text editing
- Export/import dashboard config as JSON
- Add keyboard shortcuts for quick task entry

## Learning Outcomes
By building this you'll naturally learn how to plan a multi-component UI, manage state in the browser, make API calls from the frontend, and think about user experience and responsive design. You'll also practice iterating on a project — starting with a working version and progressively adding features.

## AI Instructions
1. Analyse the repository structure before writing any code — check what already exists.
2. Create a detailed implementation plan before writing code. Break the dashboard into components (task list, weather, calendar, links).
3. Ask clarifying questions if the user's preferences are ambiguous (theme colours, default city, layout style).
4. Work iteratively — build the HTML skeleton first, then CSS, then each widget one at a time.
5. Explain major architectural decisions (why localStorage, why Tailwind, component structure).
6. Keep milestones logically separated: skeleton → tasks widget → weather → calendar → links → polish.
7. Avoid unnecessary complexity — no build tools, no frameworks unless they genuinely help.
8. Write clear documentation in a README with setup instructions and a screenshot placeholder.
9. Add sensible tests for utility functions (localStorage helpers, date formatting).
10. Refactor when improvements become obvious — don't let messy code pile up.
11. Pause after completing each major widget and summarise what was built.
