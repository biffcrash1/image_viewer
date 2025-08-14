# Image Viewer Application

A Python GUI application for viewing and cataloging image files with SQLite database support.

## Features

- **Browse Tab**: Navigate directory structure and preview images
- **Database Tab**: View cataloged images with tag-based filtering
- **Fullscreen Mode**: View images in fullscreen with mouse wheel navigation
- **Tag Management**: Add and manage tags for images in the database
- **Database Operations**: Create, open, and rescan image databases

## Requirements

- Python 3.7+
- tkinter (usually included with Python)
- Pillow (PIL)
- sqlite3 (included with Python)

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Run the application:
```bash
python main.py
```

## Usage

### Browse Tab
- Navigate the directory tree on the right side
- Single-click an image file to preview it on the left
- Double-click an image file to open it in fullscreen mode
- Right-click an image file to add tags (requires an open database)

### Database Tab
- Use tag filters to find specific images
- Click "Include Selected" to show only images with selected tags
- Click "Exclude Selected" to hide images with selected tags
- Click "Clear Filters" to show all images

### Database Menu
- **Create Database**: Select a directory and create a new database of all images
- **Open Database**: Open an existing database file
- **Rescan**: Update the database with new/removed files

### Fullscreen Mode
- Double-click any image to enter fullscreen mode
- Use mouse wheel to navigate through images in the directory
- Double-click to exit fullscreen mode
- Right-click to add tags to the current image

## Supported Image Formats

- JPEG (.jpg, .jpeg)
- PNG (.png)
- GIF (.gif)
- BMP (.bmp)
- TIFF (.tiff)
- WebP (.webp)
