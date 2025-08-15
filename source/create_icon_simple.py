#!/usr/bin/env python3
"""
Script to create icon from a simple design using PIL
"""

from PIL import Image, ImageDraw

def create_tag_icon(ico_path):
    """Create a tag-shaped icon similar to the provided design"""
    
    # Create images for different sizes
    sizes = [16, 32, 48, 64, 128, 256]
    images = []
    
    for size in sizes:
        # Create a new image with transparent background
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Calculate proportions for the tag shape
        margin = size // 16
        tag_width = size - margin * 2
        tag_height = int(tag_width * 0.7)
        
        # Calculate tag position (centered)
        x = margin
        y = (size - tag_height) // 2
        
        # Create tag shape points
        corner_radius = size // 16
        tag_points = [
            # Main rectangle with rounded corners
            (x + corner_radius, y),
            (x + tag_width - corner_radius, y),
            (x + tag_width, y + corner_radius),
            (x + tag_width, y + tag_height - corner_radius),
            (x + tag_width - corner_radius, y + tag_height),
            (x + corner_radius, y + tag_height),
            (x, y + tag_height - corner_radius),
            (x, y + corner_radius)
        ]
        
        # Draw the tag shape in dark color
        draw.polygon(tag_points, fill=(26, 26, 26, 255))
        
        # Add the hole (circle)
        hole_size = size // 8
        hole_x = x + tag_width - hole_size * 2
        hole_y = y + hole_size
        
        draw.ellipse([hole_x, hole_y, hole_x + hole_size, hole_y + hole_size], 
                    fill=(255, 255, 255, 255))
        
        images.append(img)
    
    # Save as ICO with multiple sizes
    images[0].save(ico_path, format='ICO', sizes=[(img.width, img.height) for img in images])
    print(f"Created {ico_path} with sizes: {[img.size for img in images]}")

if __name__ == "__main__":
    create_tag_icon("icon.ico")
