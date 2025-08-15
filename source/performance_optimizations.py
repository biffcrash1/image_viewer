#!/usr/bin/env python3
"""
Performance optimizations for Image Viewer Application
Implements virtual scrolling and other performance improvements
"""

import tkinter as tk
from tkinter import ttk
import sqlite3
import os
from PIL import Image, ImageTk
import threading
import time
from collections import deque
import bisect

class VirtualScrolledImageList:
    """Virtual scrolling implementation for large image lists"""
    
    def __init__( self, parent, item_height=40, visible_buffer=5 ):
        self.parent = parent
        self.item_height = item_height
        self.visible_buffer = visible_buffer  # Extra items to render above/below viewport
        
        # Data storage
        self.items = []  # List of item data
        self.filtered_items = []  # Currently filtered/visible items
        self.rendered_items = {}  # Currently rendered widgets {index: widget_data}
        
        # Viewport tracking
        self.viewport_start = 0
        self.viewport_end = 0
        self.last_scroll_time = 0
        
        # Thumbnail caching
        self.thumbnail_cache = {}
        self.thumbnail_load_queue = deque()
        self.thumbnail_loading = False
        self.max_cache_size = 500
        
        # Create UI components
        self.setup_ui()
        
    def setup_ui( self ):
        """Setup the virtual scrolled list UI"""
        # Main frame
        self.frame = ttk.Frame( self.parent )
        
        # Canvas for scrolling
        self.canvas = tk.Canvas( self.frame, highlightthickness=0 )
        self.scrollbar = ttk.Scrollbar( self.frame, orient="vertical", command=self.on_scroll )
        self.canvas.configure( yscrollcommand=self.scrollbar.set )
        
        # Scrollable frame inside canvas
        self.scrollable_frame = ttk.Frame( self.canvas )
        self.canvas_window = self.canvas.create_window( 0, 0, anchor="nw", window=self.scrollable_frame )
        
        # Pack components
        self.canvas.pack( side="left", fill="both", expand=True )
        self.scrollbar.pack( side="right", fill="y" )
        
        # Bind events
        self.canvas.bind( "<Configure>", self.on_canvas_configure )
        self.canvas.bind( "<MouseWheel>", self.on_mousewheel )
        self.scrollable_frame.bind( "<Configure>", self.on_frame_configure )
        
        # Start update loop
        self.schedule_update()
        
    def set_items( self, items ):
        """Set the list of items to display"""
        self.items = items
        self.filtered_items = items[:]
        self.update_scroll_region()
        self.update_viewport()
        
    def filter_items( self, filter_func=None ):
        """Filter items based on a function"""
        if filter_func:
            self.filtered_items = [item for item in self.items if filter_func(item)]
        else:
            self.filtered_items = self.items[:]
        
        self.update_scroll_region()
        self.update_viewport()
        
    def update_scroll_region( self ):
        """Update the scrollable region size"""
        total_height = len( self.filtered_items ) * self.item_height
        self.canvas.configure( scrollregion=(0, 0, 0, total_height) )
        
    def on_canvas_configure( self, event ):
        """Handle canvas resize"""
        # Update scrollable frame width
        canvas_width = event.width
        self.canvas.itemconfig( self.canvas_window, width=canvas_width )
        self.update_viewport()
        
    def on_frame_configure( self, event ):
        """Handle frame resize"""
        self.canvas.configure( scrollregion=self.canvas.bbox("all") )
        
    def on_scroll( self, *args ):
        """Handle scrollbar movement"""
        self.canvas.yview( *args )
        self.last_scroll_time = time.time()
        self.update_viewport()
        
    def on_mousewheel( self, event ):
        """Handle mouse wheel scrolling"""
        self.canvas.yview_scroll( int(-1 * (event.delta / 120)), "units" )
        self.last_scroll_time = time.time()
        self.update_viewport()
        
    def update_viewport( self ):
        """Update which items should be visible in the viewport"""
        if not self.filtered_items:
            return
            
        # Get viewport bounds
        canvas_height = self.canvas.winfo_height()
        if canvas_height <= 1:
            return
            
        # Get scroll position
        top_fraction = self.canvas.canvasy( 0 ) / max(1, len(self.filtered_items) * self.item_height)
        visible_height = canvas_height / self.item_height
        
        # Calculate visible range with buffer
        start_index = max( 0, int(top_fraction * len(self.filtered_items)) - self.visible_buffer )
        end_index = min( len(self.filtered_items), 
                        int((top_fraction + visible_height / len(self.filtered_items)) * len(self.filtered_items)) + self.visible_buffer )
        
        if start_index != self.viewport_start or end_index != self.viewport_end:
            self.viewport_start = start_index
            self.viewport_end = end_index
            self.render_visible_items()
            
    def render_visible_items( self ):
        """Render only the items that should be visible"""
        # Remove items outside viewport
        to_remove = []
        for index in self.rendered_items:
            if index < self.viewport_start or index >= self.viewport_end:
                to_remove.append( index )
                
        for index in to_remove:
            self.remove_rendered_item( index )
            
        # Add items in viewport
        for index in range( self.viewport_start, self.viewport_end ):
            if index not in self.rendered_items and index < len( self.filtered_items ):
                self.render_item( index )
                
    def render_item( self, index ):
        """Render a single item at the given index"""
        if index >= len( self.filtered_items ):
            return
            
        item_data = self.filtered_items[index]
        
        # Create item frame
        item_frame = ttk.Frame( self.scrollable_frame )
        item_frame.place( x=0, y=index * self.item_height, relwidth=1.0, height=self.item_height )
        
        # Create content frame
        content_frame = ttk.Frame( item_frame )
        content_frame.pack( fill="both", expand=True, padx=5, pady=2 )
        
        # Thumbnail placeholder
        thumb_label = None
        if item_data.get( 'show_thumbnails', False ):
            thumb_label = tk.Label( content_frame, text="", bg='lightgray', width=6, height=3 )
            thumb_label.pack( side="left", padx=(0, 5) )
            
            # Queue thumbnail loading
            self.queue_thumbnail( item_data['filepath'], thumb_label, index )
            
        # Filename label
        filename = item_data.get( 'filename', 'Unknown' )
        text_label = tk.Label( content_frame, text=filename, anchor="w" )
        text_label.pack( side="left", fill="x", expand=True )
        
        # Store rendered item data
        self.rendered_items[index] = {
            'frame': item_frame,
            'content_frame': content_frame,
            'thumb_label': thumb_label,
            'text_label': text_label,
            'data': item_data
        }
        
        # Bind click events
        def on_click( event, idx=index ):
            self.on_item_click( idx, event )
            
        item_frame.bind( "<Button-1>", on_click )
        content_frame.bind( "<Button-1>", on_click )
        text_label.bind( "<Button-1>", on_click )
        if thumb_label:
            thumb_label.bind( "<Button-1>", on_click )
            
    def remove_rendered_item( self, index ):
        """Remove a rendered item from display"""
        if index in self.rendered_items:
            item = self.rendered_items[index]
            item['frame'].destroy()
            del self.rendered_items[index]
            
    def queue_thumbnail( self, filepath, label, index ):
        """Queue a thumbnail for loading"""
        if filepath and os.path.exists( filepath ):
            self.thumbnail_load_queue.append( (filepath, label, index) )
            if not self.thumbnail_loading:
                self.process_thumbnail_queue()
                
    def process_thumbnail_queue( self ):
        """Process thumbnail loading queue in background"""
        if not self.thumbnail_load_queue:
            self.thumbnail_loading = False
            return
            
        self.thumbnail_loading = True
        
        # Process one thumbnail
        filepath, label, index = self.thumbnail_load_queue.popleft()
        
        def load_thumbnail():
            try:
                # Check cache first
                if filepath in self.thumbnail_cache:
                    photo = self.thumbnail_cache[filepath]
                else:
                    # Load and create thumbnail
                    with Image.open( filepath ) as img:
                        img.thumbnail( (64, 64), Image.Resampling.LANCZOS )
                        photo = ImageTk.PhotoImage( img )
                        
                    # Cache thumbnail (with size limit)
                    if len( self.thumbnail_cache ) >= self.max_cache_size:
                        # Remove oldest entries
                        oldest_keys = list( self.thumbnail_cache.keys() )[:50]
                        for key in oldest_keys:
                            del self.thumbnail_cache[key]
                    
                    self.thumbnail_cache[filepath] = photo
                
                # Update label on main thread
                def update_label():
                    if label.winfo_exists():
                        label.configure( image=photo, text="" )
                        label.image = photo  # Keep reference
                        
                self.parent.after( 0, update_label )
                
            except Exception as e:
                print( f"Error loading thumbnail for {filepath}: {e}" )
                
            # Continue processing queue
            self.parent.after( 10, self.process_thumbnail_queue )
            
        # Load thumbnail in background thread
        threading.Thread( target=load_thumbnail, daemon=True ).start()
        
    def on_item_click( self, index, event ):
        """Handle item click - override in subclass"""
        if index < len( self.filtered_items ):
            item_data = self.filtered_items[index]
            print( f"Clicked item: {item_data.get('filename', 'Unknown')}" )
            
    def schedule_update( self ):
        """Schedule periodic updates"""
        # Only update if we haven't scrolled recently (debouncing)
        current_time = time.time()
        if current_time - self.last_scroll_time > 0.1:
            self.update_viewport()
            
        # Schedule next update
        self.parent.after( 50, self.schedule_update )


class OptimizedDatabaseManager:
    """Optimized database operations with proper indexing and caching"""
    
    def __init__( self, db_path ):
        self.db_path = db_path
        self.query_cache = {}
        self.cache_max_size = 100
        self.ensure_indexes()
        
    def ensure_indexes( self ):
        """Ensure proper database indexes exist for performance"""
        try:
            conn = sqlite3.connect( self.db_path )
            cursor = conn.cursor()
            
            # Create indexes for common queries
            indexes = [
                "CREATE INDEX IF NOT EXISTS idx_images_filename ON images(filename)",
                "CREATE INDEX IF NOT EXISTS idx_images_rating ON images(rating)",
                "CREATE INDEX IF NOT EXISTS idx_image_tags_image_id ON image_tags(image_id)",
                "CREATE INDEX IF NOT EXISTS idx_image_tags_tag_id ON image_tags(tag_id)",
                "CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name)",
                "CREATE INDEX IF NOT EXISTS idx_images_relative_path ON images(relative_path)"
            ]
            
            for index_sql in indexes:
                cursor.execute( index_sql )
                
            conn.commit()
            conn.close()
            
        except Exception as e:
            print( f"Error creating indexes: {e}" )
            
    def get_filtered_images( self, included_or_tags=None, included_and_tags=None, 
                           excluded_tags=None, min_rating=0, max_rating=10, 
                           limit=None, offset=0 ):
        """Get filtered images with optimized query and caching"""
        
        # Create cache key
        cache_key = f"{included_or_tags}_{included_and_tags}_{excluded_tags}_{min_rating}_{max_rating}_{limit}_{offset}"
        
        if cache_key in self.query_cache:
            return self.query_cache[cache_key]
            
        try:
            conn = sqlite3.connect( self.db_path )
            cursor = conn.cursor()
            
            # Build optimized query
            base_query = "SELECT DISTINCT i.relative_path, i.filename, i.rating FROM images i"
            conditions = ["1=1"]
            params = []
            
            # Rating filter
            if min_rating > 0 or max_rating < 10:
                conditions.append( "i.rating >= ? AND i.rating <= ?" )
                params.extend( [min_rating, max_rating] )
                
            # Exclude filter (use NOT EXISTS for better performance)
            if excluded_tags:
                conditions.append( """NOT EXISTS (
                    SELECT 1 FROM image_tags it 
                    JOIN tags t ON it.tag_id = t.id 
                    WHERE it.image_id = i.id AND t.name IN ({})
                )""".format( ','.join(['?'] * len(excluded_tags)) ) )
                params.extend( excluded_tags )
                
            # Include OR filter
            if included_or_tags:
                conditions.append( """EXISTS (
                    SELECT 1 FROM image_tags it 
                    JOIN tags t ON it.tag_id = t.id 
                    WHERE it.image_id = i.id AND t.name IN ({})
                )""".format( ','.join(['?'] * len(included_or_tags)) ) )
                params.extend( included_or_tags )
                
            # Include AND filter (all tags must be present)
            if included_and_tags:
                for tag in included_and_tags:
                    conditions.append( """EXISTS (
                        SELECT 1 FROM image_tags it 
                        JOIN tags t ON it.tag_id = t.id 
                        WHERE it.image_id = i.id AND t.name = ?
                    )""" )
                    params.append( tag )
                    
            # Combine query
            where_clause = " AND ".join( conditions )
            query = f"{base_query} WHERE {where_clause} ORDER BY i.filename"
            
            if limit:
                query += f" LIMIT ? OFFSET ?"
                params.extend( [limit, offset] )
                
            cursor.execute( query, params )
            results = cursor.fetchall()
            conn.close()
            
            # Cache result (with size limit)
            if len( self.query_cache ) >= self.cache_max_size:
                # Remove oldest entries
                oldest_keys = list( self.query_cache.keys() )[:20]
                for key in oldest_keys:
                    del self.query_cache[key]
                    
            self.query_cache[cache_key] = results
            return results
            
        except Exception as e:
            print( f"Error in optimized query: {e}" )
            return []
            
    def clear_cache( self ):
        """Clear query cache"""
        self.query_cache.clear()


# Example integration function
def integrate_optimizations( image_viewer_instance ):
    """Integrate optimizations into existing ImageViewer"""
    
    # Replace the image list with virtual scrolled version
    def create_optimized_image_list( parent ):
        virtual_list = VirtualScrolledImageList( parent, item_height=50 )
        
        # Override item click to integrate with existing functionality
        def on_optimized_item_click( index, event ):
            if hasattr( image_viewer_instance, 'on_database_image_select' ):
                # Simulate selection in original listbox for compatibility
                if hasattr( image_viewer_instance, 'database_image_listbox' ):
                    image_viewer_instance.database_image_listbox.selection_clear( 0, tk.END )
                    image_viewer_instance.database_image_listbox.selection_set( index )
                image_viewer_instance.on_database_image_select( event )
                
        virtual_list.on_item_click = on_optimized_item_click
        return virtual_list
        
    return create_optimized_image_list


if __name__ == "__main__":
    # Example usage
    root = tk.Tk()
    root.title( "Virtual Scrolled List Demo" )
    root.geometry( "600x400" )
    
    # Create virtual list
    virtual_list = VirtualScrolledImageList( root )
    virtual_list.frame.pack( fill="both", expand=True )
    
    # Add sample data
    sample_items = []
    for i in range( 10000 ):  # 10,000 items for testing
        sample_items.append( {
            'filename': f"image_{i:05d}.jpg",
            'filepath': f"/path/to/image_{i:05d}.jpg",
            'show_thumbnails': i % 3 == 0  # Show thumbnails for every 3rd item
        } )
    
    virtual_list.set_items( sample_items )
    
    root.mainloop()
