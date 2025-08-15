#!/usr/bin/env python3
"""
Script to convert SVG icon to ICO format for Windows executable
"""

import cairosvg
from PIL import Image
import io

def svg_to_ico(svg_path, ico_path):
    """Convert SVG to ICO format with multiple sizes"""
    
    # Common icon sizes for Windows
    sizes = [16, 32, 48, 64, 128, 256]
    images = []
    
    for size in sizes:
        # Convert SVG to PNG bytes at specific size
        png_bytes = cairosvg.svg2png(url=svg_path, output_width=size, output_height=size)
        
        # Create PIL Image from PNG bytes
        img = Image.open(io.BytesIO(png_bytes))
        images.append(img)
    
    # Save as ICO with multiple sizes
    images[0].save(ico_path, format='ICO', sizes=[(img.width, img.height) for img in images])
    print(f"Created {ico_path} with sizes: {[img.size for img in images]}")

if __name__ == "__main__":
    svg_to_ico("icon.svg", "icon.ico")
