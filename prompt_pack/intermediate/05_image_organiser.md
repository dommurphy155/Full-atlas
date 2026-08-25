# 📸 Local Image Organiser

**Difficulty:** Intermediate

## Overview
A desktop application that watches your image folders, automatically organises photos by date and content, deduplicates, and lets you search by visual similarity. It's like Google Photos but fully local and private.

## Objectives
- Scan directories for image files (JPEG, PNG, WebP)
- Auto-organise photos into date-based folders (YYYY/MM/DD)
- Detect and remove duplicate or near-duplicate images
- Extract text from images via OCR
- Search images by visual similarity using embeddings
- Web dashboard for browsing and managing the collection

## Features
- [ ] Recursive directory scanning for images
- [ ] Auto-organisation by EXIF date into YYYY/MM/DD folders
- [ ] Duplicate detection (perceptual hashing)
- [ ] OCR text extraction from images
- [ ] Visual similarity search (find similar photos)
- [ ] Web dashboard for browsing organised photos
- [ ] Tagging and metadata editing
- [ ] Export organised collection to a new directory

## Technical Suggestions
- **Python + FastAPI** — backend for processing and serving
- **Pillow** — image processing and EXIF reading
- **imagehash** — perceptual hashing for duplicate detection
- **sentence-transformers** — for visual similarity embeddings
- **Tesseract OCR** — for text extraction from images
- **HTMX + Tailwind** — for the dashboard UI
- **SQLite** — for metadata and tags

## Stretch Goals
- Add face detection and clustering
- Implement automatic photo enhancement (colour correction, noise reduction)
- Build a timeline view that shows photos chronologically
- Add geolocation mapping if EXIF GPS data is present

## Learning Outcomes
You'll learn about image processing, perceptual hashing, computer vision embeddings, EXIF metadata handling, and building a system that processes files at scale. This teaches you to work with binary data and think about storage and retrieval of rich media.

## AI Instructions
1. Analyse the repository structure before writing any code.
2. Create a detailed implementation plan: scanning pipeline, organisation logic, search backend, UI.
3. Ask clarifying questions if requirements are ambiguous (image formats, duplicate threshold, storage location).
4. Work iteratively — start with scanning and EXIF reading, then organisation, then duplicate detection.
5. Explain major architectural decisions (why perceptual hashing, how embeddings work for images).
6. Keep milestones logically separated: scanning → organisation → dedup → OCR → similarity search → UI → polish.
7. Avoid unnecessary complexity — start with simple date-based organisation, add features incrementally.
8. Write clear documentation with setup and usage instructions.
9. Add tests for image processing, duplicate detection, and EXIF parsing.
10. Refactor when improvements become obvious — keep the code clean and modular.
11. Pause after completing each major feature and summarise progress.
