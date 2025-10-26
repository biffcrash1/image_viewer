#!/usr/bin/env python3
"""
Script to apply performance optimizations to the existing ImageViewer application
"""

import re
import os
from pathlib import Path

def apply_performance_patches():
    """Apply performance optimizations to image_viewer.py"""
    
    # Read the original file
    with open( 'image_viewer.py', 'r', encoding='utf-8' ) as f:
        content = f.read()
    
    # Create backup
    backup_path = 'image_viewer.py.backup'
    if not os.path.exists( backup_path ):
        with open( backup_path, 'w', encoding='utf-8' ) as f:
            f.write( content )
        print( f"Created backup: {backup_path}" )
    
    # Apply patches
    patches = [
        # 1. Add virtual scrolling imports
        {
            'search': r'import json',
            'replace': '''import json
import bisect
from collections import deque
import weakref'''
        },
        
        # 2. Add performance settings to __init__
        {
            'search': r'self\.cache_max_size = 1000  # Maximum items to keep in cache',
            'replace': '''self.cache_max_size = 1000  # Maximum items to keep in cache
        
        # Virtual scrolling settings
        self.virtual_scrolling = True  # Enable virtual scrolling for large lists
        self.virtual_item_height = 50  # Height of each virtual list item
        self.virtual_buffer_size = 10  # Extra items to render above/below viewport
        self.rendered_items = {}  # Currently rendered virtual items
        self.viewport_start = 0
        self.viewport_end = 0
        self.last_scroll_time = 0
        
        # Performance optimizations
        self.db_query_cache = {}  # Cache database queries
        self.db_cache_max_size = 50
        self.thumbnail_batch_size = 3  # Load thumbnails in batches
        self.scroll_debounce_delay = 100  # Debounce scroll events (ms)'''
        },
        
        # 3. Optimize database queries with indexing
        {
            'search': r'def create_database_tables\( self \):',
            'replace': '''def ensure_database_indexes( self ):
        """Ensure proper database indexes exist for performance"""
        if not self.current_database:
            return
            
        try:
            cursor = self.current_database.cursor()
            
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
                
            self.current_database.commit()
            print( "Database indexes ensured for optimal performance" )
            
        except Exception as e:
            print( f"Error creating indexes: {e}" )
    
    def create_database_tables( self ):'''
        },
        
        # 4. Add call to ensure indexes after database creation
        {
            'search': r'self\.current_database\.commit\(\)\s+print\( f"Created database: \{database_path\}" \)',
            'replace': '''self.current_database.commit()
            self.ensure_database_indexes()  # Add performance indexes
            print( f"Created database: {database_path}" )'''
        },
        
        # 5. Optimize visibility checking frequency
        {
            'search': r'self\.visibility_check_timer = self\.root\.after\( 100, self\.check_visible_thumbnails \)',
            'replace': '''self.visibility_check_timer = self.root.after( 200, self.check_visible_thumbnails )  # Reduced frequency'''
        },
        
        # 6. Add scroll debouncing
        {
            'search': r'def on_scrollbar_move\( self, \*args \):',
            'replace': '''def on_scrollbar_move( self, *args ):
        """Handle scrollbar movement with debouncing"""
        self.last_scroll_time = time.time()
        # Move the canvas view
        self.image_list_canvas.yview( *args )
        
        # Debounce thumbnail loading during fast scrolling
        if hasattr( self, '_scroll_debounce_timer' ):
            self.root.after_cancel( self._scroll_debounce_timer )
        self._scroll_debounce_timer = self.root.after( self.scroll_debounce_delay, self._on_scroll_settled )
    
    def _on_scroll_settled( self ):
        """Called when scrolling has settled"""
        # Trigger thumbnail loading for newly visible items
        if self.show_thumbnails.get():
            self.check_visible_thumbnails()
    
    def on_scrollbar_move_original( self, *args ):'''
        },
        
        # 7. Optimize thumbnail loading with batching
        {
            'search': r'def process_thumbnail_queue\( self \):',
            'replace': '''def process_thumbnail_queue( self ):
        """Process the thumbnail loading queue with batching"""
        if not self.thumbnail_load_queue or not self.show_thumbnails.get():
            self.thumbnail_loading = False
            return
            
        self.thumbnail_loading = True
        
        # Process multiple items in batch for better performance
        batch_size = min( self.thumbnail_batch_size, len(self.thumbnail_load_queue) )
        batch_items = []
        
        for _ in range( batch_size ):
            if self.thumbnail_load_queue:
                batch_items.append( self.thumbnail_load_queue.pop(0) )
        
        def load_thumbnails_batch():
            """Load thumbnails in background thread"""
            for item in batch_items:
                if not item['thumbnail_loaded'] and item['filepath'] and os.path.exists( item['filepath'] ):
                    try:
                        photo = self.get_thumbnail( item['filepath'] )
                        if photo and item['thumb_label'] and item['thumb_label'].winfo_exists():
                            # Update on main thread
                            def update_thumbnail( photo=photo, item=item ):
                                if item['thumb_label'].winfo_exists():
                                    item['thumb_label'].configure( image=photo, text="" )
                                    item['thumb_label'].image = photo
                                    item['thumbnail_loaded'] = True
                            
                            self.root.after( 0, update_thumbnail )
                            
                    except Exception as e:
                        print( f"Error loading thumbnail: {e}" )
            
            # Continue processing if queue has more items
            if self.thumbnail_load_queue:
                self.root.after( 50, self.process_thumbnail_queue )
            else:
                self.thumbnail_loading = False
        
        # Start batch loading in background
        threading.Thread( target=load_thumbnails_batch, daemon=True ).start()
        
    def process_thumbnail_queue_original( self ):'''
        },
        
        # 8. Add database query caching
        {
            'search': r'def refresh_filtered_images\( self, preserve_selection=None \):',
            'replace': '''def get_cached_query_result( self, query, params ):
        """Get cached database query result"""
        cache_key = f"{query}_{str(params)}"
        
        if cache_key in self.db_query_cache:
            return self.db_query_cache[cache_key]
        
        # Execute query
        cursor = self.current_database.cursor()
        cursor.execute( query, params )
        result = cursor.fetchall()
        
        # Cache result (with size limit)
        if len( self.db_query_cache ) >= self.db_cache_max_size:
            # Remove oldest entries
            oldest_keys = list( self.db_query_cache.keys() )[:10]
            for key in oldest_keys:
                del self.db_query_cache[key]
        
        self.db_query_cache[cache_key] = result
        return result
    
    def clear_query_cache( self ):
        """Clear database query cache"""
        self.db_query_cache.clear()
        
    def refresh_filtered_images( self, preserve_selection=None ):'''
        },
        
        # 9. Use cached queries in refresh_filtered_images
        {
            'search': r'cursor\.execute\( query, params \)\s+results = cursor\.fetchall\(\)',
            'replace': '''# Use cached query for better performance
            results = self.get_cached_query_result( query, params )'''
        },
        
        # 10. Optimize thumbnail cache management
        {
            'search': r'if len\( self\.thumbnail_cache \) >= 200:  # Limit thumbnail cache',
            'replace': '''if len( self.thumbnail_cache ) >= 300:  # Increased thumbnail cache size'''
        }
    ]
    
    # Apply each patch
    modified_content = content
    patches_applied = 0
    
    for i, patch in enumerate( patches ):
        if re.search( patch['search'], modified_content ):
            modified_content = re.sub( patch['search'], patch['replace'], modified_content )
            patches_applied += 1
            print( f"Applied patch {i+1}: {patch['search'][:50]}..." )
        else:
            print( f"Warning: Could not apply patch {i+1}: {patch['search'][:50]}..." )
    
    # Write modified content
    if patches_applied > 0:
        with open( 'image_viewer.py', 'w', encoding='utf-8' ) as f:
            f.write( modified_content )
        print( f"\nApplied {patches_applied} performance patches to image_viewer.py" )
        print( "Performance improvements include:" )
        print( "- Database indexing for faster queries" )
        print( "- Query result caching" )
        print( "- Scroll debouncing" )
        print( "- Batched thumbnail loading" )
        print( "- Optimized cache management" )
        print( "\nRestart your application to see the improvements!" )
    else:
        print( "No patches were applied. The code may have already been modified." )

if __name__ == "__main__":
    print( "Applying performance optimizations to Image Viewer..." )
    apply_performance_patches()
