# 🌤️ Weather Dashboard with Forecast

**Difficulty:** Beginner

## Overview
A focused weather application that shows current conditions and a 7-day forecast for multiple saved locations. It goes beyond a simple display — it tracks your locations, remembers your preferences, and presents the data beautifully.

## Objectives
- Display current weather for a configurable location
- Show a 7-day forecast with high/low temperatures and conditions
- Support multiple saved locations with easy switching
- Remember user preferences (default location, temperature unit)
- Clean, attractive UI that makes weather data enjoyable to read

## Features
- [ ] Current weather display (temperature, condition, humidity, wind)
- [ ] 7-day forecast with icons and min/max temperatures
- [ ] Add/remove/save multiple locations
- [ ] Toggle between Celsius and Fahrenheit
- [ ] Remember preferences across sessions (localStorage or config file)
- [ ] Weather icons that match conditions (sun, rain, snow, clouds)
- [ ] Responsive design — works on phone and desktop
- [ ] Auto-detection of user's location (optional, via IP geolocation)

## Technical Suggestions
- **Python + FastAPI/Flask** — simple backend serving the UI and proxying weather API calls
- **Open-Meteo API** — free, no API key needed, supports forecasts
- **Plain HTML/CSS/JS** — keep it simple, no framework needed for this scope
- **Tailwind CSS** — for quick, clean styling
- **localStorage** — for persisting locations and preferences

## Stretch Goals
- Add a "feels like" temperature and UV index
- Implement weather alerts or severe weather warnings
- Add a temperature chart/graph for the week
- Support weather-based clothing recommendations
- Add offline caching so the dashboard works without internet

## Learning Outcomes
You'll learn how to integrate external APIs, manage user preferences, design a clean data display, and think about user experience — making data not just functional but pleasant to interact with.

## AI Instructions
1. Analyse the repository structure before writing any code.
2. Create a detailed implementation plan: API integration, data flow, UI layout.
3. Ask clarifying questions if requirements are ambiguous (default location, temperature unit, icon style).
4. Work iteratively — start with a single location display, then add forecast, then multi-location support.
5. Explain major architectural decisions (why Open-Meteo over other weather APIs, how preferences are stored).
6. Keep milestones logically separated: single location → forecast → multi-location → preferences → polish.
7. Avoid unnecessary complexity — no database unless it genuinely adds value.
8. Write clear documentation with setup instructions and API key notes (if any).
9. Add tests for the weather data parsing and location management functions.
10. Refactor when improvements become obvious — keep the code clean as features are added.
11. Pause after completing each major feature and summarise progress.
