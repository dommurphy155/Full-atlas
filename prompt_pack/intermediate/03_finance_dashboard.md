# 📊 Personal Finance Dashboard

**Difficulty:** Intermediate

## Overview
A self-hosted dashboard that tracks your spending, categorises transactions, and gives you insights into your financial habits. It's private, local, and yours — no bank sharing your data with third parties.

## Objectives
- Import bank transaction data (CSV/OFX/QIF export)
- Auto-categorise transactions into spending categories
- Visualise spending trends with charts
- Set budgets per category with alerts
- Track net worth over time
- Generate monthly financial summaries

## Features
- [ ] CSV/OFX/QIF import for bank transactions
- [ ] Auto-categorisation (food, transport, housing, entertainment, etc.)
- [ ] Manual category override and re-categorisation
- [ ] Budget setting per category with overspend alerts
- [ ] Spending charts (weekly, monthly, yearly views)
- [ ] Net worth tracking with asset/liability input
- [ ] Monthly financial summary report
- [ ] Data export (CSV, PDF report)
- [ ] Web dashboard with interactive charts

## Technical Suggestions
- **Python + FastAPI** — backend for data processing and API
- **Chart.js** — client-side charts, no server rendering needed
- **Pandas** — for CSV parsing and data manipulation
- **SQLite** — for storing transactions and budget rules
- **Plain HTML/CSS/JS** — for the dashboard frontend
- **Tailwind CSS** — for clean styling

## Stretch Goals
- Add receipt scanning via AI (extract amounts from photos)
- Implement recurring transaction detection
- Add multi-currency support with exchange rate tracking
- Build a mobile-first responsive design
- Add goal tracking (save for a trip, pay off debt)

## Learning Outcomes
You'll learn data parsing and transformation, building interactive dashboards, working with financial data responsibly, and designing a system that handles real-world messy data (inconsistent formats, missing fields, edge cases).

## AI Instructions
1. Analyse the repository structure before writing any code.
2. Create a detailed implementation plan: data model, import pipeline, dashboard layout.
3. Ask clarifying questions if requirements are ambiguous (currency, categories, chart library).
4. Work iteratively — start with CSV import and storage, then categorisation, then charts.
5. Explain major architectural decisions (why Pandas for data, why SQLite for storage).
6. Keep milestones logically separated: import → storage → categorisation → charts → budgets → polish.
7. Avoid unnecessary complexity — start with CSV import, add OFX/QIF later if needed.
8. Write clear documentation with setup instructions and data format guides.
9. Add tests for transaction parsing, categorisation logic, and budget alerts.
10. Refactor when improvements become obvious — keep the code clean as features are added.
11. Pause after completing each major feature and summarise progress.
