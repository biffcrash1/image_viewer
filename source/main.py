#!/usr/bin/env python3
"""
Image Viewer Application
A Python GUI application for viewing and cataloging image files with SQLite database support.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import sqlite3
import os
from PIL import Image, ImageTk, ExifTags
from PIL.ExifTags import TAGS
import threading
import time
from pathlib import Path
import json
import bisect
from collections import deque
import weakref

class TreeviewImageList:
    """Treeview-based image list that handles large datasets without coordinate limits"""
    
    def __init__( self, parent, item_height=50 ):
        self.parent = parent
        self.item_height = item_height
        
        # Reference to main application
        self.main_app = None
        
        # Data storage
        self.items = []
        self.filtered_items = []
        
        # Selection tracking
        self.selected_indices = set()
        self.selection_callbacks = []
        self.last_clicked_index = None
        
        # Thumbnail support
        self._thumbnail_cache = {}
        self.thumbnail_load_queue = deque()
        self.thumbnail_loading = False
        self.max_cache_size = 500
        
        # Priority-based thumbnail loading for TreeviewImageList
        self.priority_thumbnail_queue = []  # List of (priority, filepath, item_id) tuples
        self.visible_item_range = (0, 0)  # Track visible item range
        self.last_visible_center = -1  # Track scroll jumps
        self.thumbnail_size = (48, 48)  # Smaller to fit better in treeview rows
        self.cached_visible_items = []  # Cache visible items to avoid repeated bbox calls
        self.last_visible_update = 0  # Track when visible items were last updated
        self._processing_priority = False  # Flag to track if priority processing is active
        self._verifying_thumbnails = False  # Flag to prevent multiple simultaneous verifications
        
        # Debouncing for thumbnail loading
        self.thumbnail_load_delay = 50  # ms delay before loading (reduced from 200)
        self.pending_thumbnail_loads = {}  # {item_id: after_id}
        self.last_scroll_time = 0
        
        # Create UI components
        self.setup_ui()
        
        # Start periodic verification for visible thumbnails
        self.start_periodic_verification()
        
    def setup_ui( self ):
        """Setup the treeview-based list UI"""
        # Main frame
        self.frame = ttk.Frame( self.parent )
        
        # Create treeview with custom styling
        style = ttk.Style()
        style.configure( "ImageList.Treeview", rowheight=self.item_height )
        
        # Treeview for image list
        self.treeview = ttk.Treeview( self.frame, style="ImageList.Treeview", show='tree', selectmode='extended' )
        self.treeview.heading( '#0', text='Images', anchor='w' )
        
        # Scrollbar - use tk.Scrollbar with explicit width and styling for normal width
        # Create a frame to hold the scrollbar and ensure it takes up space
        scrollbar_frame = tk.Frame( self.frame, width=25, bg='lightgray' )
        scrollbar_frame.pack_propagate( False )  # Don't shrink to contents
        scrollbar = tk.Scrollbar( scrollbar_frame, orient="vertical", command=self.treeview.yview, width=25, bg='lightgray', troughcolor='white' )
        self.treeview.configure( yscrollcommand=scrollbar.set )
        
        # Pack components
        self.treeview.pack( side="left", fill="both", expand=True )
        scrollbar_frame.pack( side="right", fill="y" )
        scrollbar.pack( fill="both", expand=True )
        
        # Bind events
        self.treeview.bind( "<<TreeviewSelect>>", self.on_selection_changed )
        self.treeview.bind( "<Button-1>", self.on_click )
        self.treeview.bind( "<Double-Button-1>", self.on_double_click )
        
        # Bind keyboard events
        self.treeview.bind( "<Control-a>", self.select_all )
        self.treeview.bind( "<Control-A>", self.select_all )
        
        # Bind scroll events for thumbnail loading debouncing
        self.treeview.bind( "<MouseWheel>", self.on_scroll )
        scrollbar.bind( "<ButtonRelease-1>", self.on_scroll_release )
        
        # Also bind Ctrl+A to the main frame for better accessibility
        self.frame.bind( "<Control-a>", self.select_all )
        self.frame.bind( "<Control-A>", self.select_all )
        
    def set_items( self, items ):
        """Set the list of items to display"""
        self.items = items
        self.filtered_items = items[:]
        self.selected_indices.clear()
        self.refresh_treeview()
        
        # Update status if main app is available
        if self.main_app and hasattr( self.main_app, 'update_image_list_status' ):
            self.main_app.update_image_list_status()
        
    def filter_items( self, filter_func=None ):
        """Filter items based on a function"""
        if filter_func:
            self.filtered_items = [item for item in self.items if filter_func(item)]
        else:
            self.filtered_items = self.items[:]
        
        self.selected_indices.clear()
        self.refresh_treeview()
        
        # Update status if main app is available
        if self.main_app and hasattr( self.main_app, 'update_image_list_status' ):
            self.main_app.update_image_list_status()
        
    def refresh_treeview( self ):
        """Refresh the treeview with current filtered items"""
        # Clear existing items
        for item in self.treeview.get_children():
            self.treeview.delete( item )
            
        # Add filtered items
        for i, item_data in enumerate( self.filtered_items ):
            filename = item_data.get( 'filename', 'Unknown' )
            filepath = item_data.get( 'filepath' )
            show_thumbnails = item_data.get( 'show_thumbnails', False )
            
            # Insert item with filename
            item_id = str( i )
            self.treeview.insert( '', 'end', iid=item_id, text=filename )
            
            # Only set cached thumbnails immediately, don't queue all items
            if show_thumbnails and filepath and os.path.exists( filepath ):
                # Check if thumbnail is already cached
                if filepath in self._thumbnail_cache:
                    # Use cached thumbnail immediately
                    try:
                        self.treeview.item( item_id, image=self._thumbnail_cache[filepath] )

                    except Exception as e:
                        print( f"CACHED: Error applying cached thumbnail for {item_id}: {e}" )
        
        # After adding all items, load thumbnails for visible items only
        if any( item_data.get( 'show_thumbnails', False ) for item_data in self.filtered_items ):
            self.parent.after( 100, self.load_initial_visible_thumbnails )
    
    def load_initial_visible_thumbnails( self ):
        """Load thumbnails for initially visible items and preload adjacent ones"""
        try:
            # Get actually visible items
            visible_items = self.get_visible_treeview_items()
            
            if visible_items:
                visible_indices = [int(item) for item in visible_items if item.isdigit()]
                
                if visible_indices:
                    visible_start = min( visible_indices )
                    visible_end = max( visible_indices )
                    
                    # Define preload range for initial load
                    preload_range = 20
                    load_start = max( 0, visible_start - preload_range )
                    load_end = min( len( self.filtered_items ) - 1, visible_end + preload_range )
                    
                    # Queue thumbnails for visible + preload range
                    for index in range( load_start, load_end + 1 ):
                        if index < len( self.filtered_items ):
                            item_data = self.filtered_items[index]
                            filepath = item_data.get( 'filepath' )
                            show_thumbnails = item_data.get( 'show_thumbnails', False )
                            item_id = str( index )
                            
                            if show_thumbnails and filepath and os.path.exists( filepath ):
                                if filepath not in self._thumbnail_cache:
                                    # Queue for priority loading
                                    self.queue_thumbnail_load( filepath, item_id )
                                
        except Exception as e:
            print( f"Error loading initial visible thumbnails: {e}" )
        
        # Verify all visible items are covered after initial load
        self.parent.after( 300, self.verify_visible_thumbnails_loaded )
            
    def on_selection_changed( self, event ):
        """Handle treeview selection changes"""
        selected_items = self.treeview.selection()
        self.selected_indices = {int(item) for item in selected_items if item.isdigit()}
        
        # Notify callbacks
        for callback in self.selection_callbacks:
            callback( list(self.selected_indices) )
            
    def on_click( self, event ):
        """Handle click events for proper CTRL/SHIFT selection"""
        item = self.treeview.identify_row( event.y )
        if item and item.isdigit():
            index = int( item )
            
            if event.state & 0x4:  # Ctrl key
                if item in self.treeview.selection():
                    self.treeview.selection_remove( item )
                else:
                    self.treeview.selection_add( item )
                self.last_clicked_index = index
                
            elif event.state & 0x1:  # Shift key
                if hasattr( self, 'last_clicked_index' ) and self.last_clicked_index is not None:
                    start_idx = min( self.last_clicked_index, index )
                    end_idx = max( self.last_clicked_index, index )
                    
                    # Select range
                    items_to_select = [str(i) for i in range(start_idx, end_idx + 1) if i < len(self.filtered_items)]
                    self.treeview.selection_set( items_to_select )
                else:
                    self.treeview.selection_set( item )
                    self.last_clicked_index = index
            else:
                self.treeview.selection_set( item )
                self.last_clicked_index = index
                
        # Trigger selection change manually since we're overriding default behavior
        self.on_selection_changed( event )
        
    def on_double_click( self, event ):
        """Handle double click events"""
        item = self.treeview.identify_row( event.y )
        if item and item.isdigit() and hasattr( self, 'on_item_double_click' ):
            self.on_item_double_click( int(item), event )
            
    def add_selection_callback( self, callback ):
        """Add a callback for selection changes"""
        self.selection_callbacks.append( callback )
        
    def get_selected_items( self ):
        """Get currently selected items"""
        return [self.filtered_items[i] for i in self.selected_indices if i < len(self.filtered_items)]
        
    def update_selection_display( self ):
        """Update visual selection display - compatibility method"""
        # Treeview handles selection display automatically
        pass
        
    # Compatibility attributes for existing code
    @property
    def thumbnail_cache( self ):
        """Thumbnail cache compatibility"""
        return self._thumbnail_cache
        
    def load_thumbnail( self, filepath ):
        """Load and cache a thumbnail for the given filepath"""
        if filepath in self._thumbnail_cache:
            return self._thumbnail_cache[filepath]
            
        try:
            # Load and resize image
            with Image.open( filepath ) as img:
                # Convert to RGB if necessary
                if img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert( 'RGB' )
                
                # Create thumbnail
                img.thumbnail( self.thumbnail_size, Image.Resampling.LANCZOS )
                
                # Convert to PhotoImage for Tkinter
                photo = ImageTk.PhotoImage( img )
                
                # Cache the thumbnail
                self._thumbnail_cache[filepath] = photo
                
                # Clean cache if it gets too large
                if len( self._thumbnail_cache ) > self.max_cache_size:
                    self.cleanup_thumbnail_cache()
                    
                return photo
                
        except Exception as e:
            print( f"Error loading thumbnail for {filepath}: {e}" )
            return None
            
    def cleanup_thumbnail_cache( self ):
        """Remove old thumbnails from cache"""
        # Remove 25% of cached items
        items_to_remove = len( self._thumbnail_cache ) // 4
        cache_keys = list( self._thumbnail_cache.keys() )
        
        for key in cache_keys[:items_to_remove]:
            del self._thumbnail_cache[key]
            
    def queue_thumbnail_load( self, filepath, item_id ):
        """Queue a thumbnail for lazy loading with priority"""
        if not filepath or not os.path.exists( filepath ):
            return
            
        # Cancel any pending load for this item
        if item_id in self.pending_thumbnail_loads:
            self.parent.after_cancel( self.pending_thumbnail_loads[item_id] )
            
        # Calculate priority for this item
        priority = self.calculate_treeview_thumbnail_priority( item_id )
        
        # Add to priority queue
        import heapq
        heapq.heappush( self.priority_thumbnail_queue, (priority, filepath, item_id) )
        
        # Schedule thumbnail load after delay (but use priority queue)
        after_id = self.parent.after( self.thumbnail_load_delay, 
                                     lambda: self._process_priority_thumbnail_load() )
        self.pending_thumbnail_loads[item_id] = after_id
        
    def calculate_treeview_thumbnail_priority( self, item_id ):
        """Calculate priority for treeview thumbnail loading"""
        try:
            # Use cached visible items if recent, otherwise update
            import time
            current_time = time.time() * 1000  # Convert to milliseconds
            
            if not self.cached_visible_items or (current_time - self.last_visible_update > 100):
                self.cached_visible_items = self.get_visible_treeview_items()
                self.last_visible_update = current_time
            
            visible_items = self.cached_visible_items
            
            if visible_items:
                visible_indices = [int(item) for item in visible_items if item.isdigit()]
                
                if visible_indices:
                    visible_start = min( visible_indices )
                    visible_end = max( visible_indices )
                    visible_center = (visible_start + visible_end) // 2
                    
                    # Update visible range tracking
                    self.update_treeview_visible_range( visible_start, visible_end )
                    
                    if item_id.isdigit():
                        item_index = int( item_id )
                        
                        if visible_start <= item_index <= visible_end:
                            # Visible items get highest priority (0-50 based on distance from center)
                            distance_from_center = abs( item_index - visible_center )
                            return distance_from_center
                        else:
                            # Adjacent items get medium priority for preloading
                            preload_range = 20
                            if (visible_start - preload_range) <= item_index < visible_start:
                                # Items above visible range
                                distance = visible_start - item_index
                                return 100 + distance
                            elif visible_end < item_index <= (visible_end + preload_range):
                                # Items below visible range  
                                distance = item_index - visible_end
                                return 100 + distance
                            else:
                                # Non-adjacent items get very low priority
                                return 999
            
            return 999  # Default low priority for non-visible items
        except Exception as e:
            return 999  # Fallback priority
            
    def get_visible_treeview_items( self ):
        """Get only the items that are actually visible in the treeview viewport"""
        try:
            # Get the bounding box of the visible area
            visible_items = []
            
            # Get all items
            all_items = self.treeview.get_children()
            
            # Check which ones are actually visible
            for item in all_items:
                try:
                    # Get the bounding box of the item
                    bbox = self.treeview.bbox( item )
                    if bbox:  # bbox is None if item is not visible
                        visible_items.append( item )
                except:
                    # If bbox fails, item is not visible
                    continue
                    
            return visible_items
        except Exception as e:
            print( f"Error getting visible items: {e}" )
            return []
            
    def update_treeview_visible_range( self, start_index, end_index ):
        """Update visible range and detect scroll jumps for treeview"""
        old_start, old_end = self.visible_item_range
        self.visible_item_range = (start_index, end_index)
        
        new_center = (start_index + end_index) // 2
        
        # Clear queue on major scroll jumps
        if self.last_visible_center != -1:
            center_jump = abs( new_center - self.last_visible_center )
            if center_jump > 20:  # Smaller threshold for treeview
                self.priority_thumbnail_queue.clear()
                
        self.last_visible_center = new_center
        
    def _process_priority_thumbnail_load( self ):
        """Process priority-based thumbnail loading"""
        if not self.priority_thumbnail_queue:
            self._processing_priority = False
            if not self.thumbnail_loading:
                self.process_thumbnail_queue()  # Fallback to old queue
            return
            
        # Get highest priority thumbnail and process it directly
        import heapq
        priority, filepath, item_id = heapq.heappop( self.priority_thumbnail_queue )
        
        # Skip items with very low priority (non-adjacent items)
        if priority >= 999:
            # Continue processing if there are more items
            if self.priority_thumbnail_queue:
                self.parent.after( 50, self._process_priority_thumbnail_load )
            return
        
        # Set processing flag
        self._processing_priority = True
        
        # Process this thumbnail directly - allow multiple visible thumbnails to load in parallel
        self._load_thumbnail_directly( filepath, item_id )
        
        # Continue processing more items with different speeds based on priority
        if self.priority_thumbnail_queue:
            if priority < 50:  # Visible items
                self.parent.after( 1, self._process_priority_thumbnail_load )
            elif priority < 150:  # Preload items
                self.parent.after( 10, self._process_priority_thumbnail_load )
            else:
                self.parent.after( 50, self._process_priority_thumbnail_load )
            
    def _load_thumbnail_directly( self, filepath, item_id ):
        """Load a thumbnail directly, bypassing the FIFO queue"""
        # Don't set thumbnail_loading flag to allow parallel loading of visible items
        
        def load_thumbnail_async():
            """Load thumbnail in background thread"""
            try:
                # Check if item still exists
                if not self.treeview.exists( item_id ):
                    return
                    
                # Check cache first
                if filepath in self._thumbnail_cache:
                    photo = self._thumbnail_cache[filepath]
                else:
                    # Load thumbnail
                    photo = self.load_thumbnail( filepath )
                    
                if photo:
                    # Update UI on main thread
                    def update_ui():
                        try:
                            if self.treeview.exists( item_id ):
                                # Get current text to preserve it
                                current_text = self.treeview.item( item_id, 'text' )
                                # Set both image and text (image appears before text in treeview)
                                self.treeview.item( item_id, image=photo, text=current_text )

                                
                                # Force treeview to refresh this item
                                self.treeview.update_idletasks()
                            else:
                                print( f"THUMBNAIL: Item {item_id} no longer exists when trying to set thumbnail" )
                        except Exception as e:
                            print( f"THUMBNAIL: Error setting thumbnail for item {item_id}: {e}" )
                    self.parent.after( 0, update_ui )
                    
            except Exception as e:
                print( f"Error loading priority thumbnail for {filepath}: {e}" )
                
        # Load thumbnail in background thread
        threading.Thread( target=load_thumbnail_async, daemon=True ).start()
        
    def _continue_priority_processing( self ):
        """Continue processing the priority queue"""
        self.thumbnail_loading = False
        if self.priority_thumbnail_queue:
            self._process_priority_thumbnail_load()
        
    def _delayed_thumbnail_load( self, filepath, item_id ):
        """Actually load the thumbnail after the delay"""
        # Remove from pending loads
        if item_id in self.pending_thumbnail_loads:
            del self.pending_thumbnail_loads[item_id]
            
        # Only load if item still exists and is visible
        if self.treeview.exists( item_id ):
            # Use priority system instead of FIFO queue
            priority = self.calculate_treeview_thumbnail_priority( item_id )
            import heapq
            heapq.heappush( self.priority_thumbnail_queue, (priority, filepath, item_id) )
            
            if not self.thumbnail_loading:
                self._process_priority_thumbnail_load()
                
    def process_thumbnail_queue( self ):
        """Process thumbnail loading queue in background"""
        if not self.thumbnail_load_queue:
            self.thumbnail_loading = False
            return
            
        self.thumbnail_loading = True
        
        # Get next item from queue
        filepath, item_id = self.thumbnail_load_queue.popleft()
        
        def load_thumbnail_async():
            """Load thumbnail in background thread"""
            try:
                photo = self.load_thumbnail( filepath )
                if photo:
                    # Update treeview item with thumbnail on main thread
                    self.parent.after( 0, lambda: self.update_item_thumbnail( item_id, photo ) )
            except Exception as e:
                print( f"Error in thumbnail thread: {e}" )
            finally:
                # Continue processing queue after a short delay
                self.parent.after( 10, self.process_thumbnail_queue )
                
        # Load thumbnail in background thread
        threading.Thread( target=load_thumbnail_async, daemon=True ).start()
        
    def update_item_thumbnail( self, item_id, photo ):
        """Update treeview item with loaded thumbnail"""
        try:
            if self.treeview.exists( item_id ):
                # Get current text and add thumbnail
                current_text = self.treeview.item( item_id, 'text' )
                # Remove existing 📷 indicator if present
                if current_text.startswith( '📷 ' ):
                    current_text = current_text[2:]
                    
                # Set the image for the treeview item
                self.treeview.item( item_id, image=photo )
        except Exception as e:
            print( f"Error updating thumbnail for {item_id}: {e}" )
            
    def set_thumbnails_enabled( self, enabled ):
        """Enable or disable thumbnails for all items"""
        for i, item_data in enumerate( self.filtered_items ):
            item_data['show_thumbnails'] = enabled
            
        # Refresh the treeview to apply changes
        self.refresh_treeview()
        
    def clear_thumbnails( self ):
        """Clear all thumbnails from display"""
        for item_id in self.treeview.get_children():
            self.treeview.item( item_id, image='' )
            
    def load_visible_thumbnails( self ):
        """Load thumbnails for currently visible items only"""
        try:
            # Get the visible region of the treeview
            visible_items = self.treeview.get_children()
            
            # Queue thumbnails for visible items
            for item_id in visible_items:
                if item_id.isdigit():
                    index = int( item_id )
                    if index < len( self.filtered_items ):
                        item_data = self.filtered_items[index]
                        filepath = item_data.get( 'filepath' )
                        show_thumbnails = item_data.get( 'show_thumbnails', False )
                        
                        if show_thumbnails and filepath and os.path.exists( filepath ):
                            if filepath not in self._thumbnail_cache:
                                self.queue_thumbnail_load( filepath, item_id )
                                
        except Exception as e:
            print( f"Error loading visible thumbnails: {e}" )
            
    def on_scroll( self, event ):
        """Handle scroll events to track scrolling activity"""
        import time
        self.last_scroll_time = time.time()
        
        # Invalidate cached visible items on scroll
        self.cached_visible_items = []
        self.last_visible_update = 0
        
        # Cancel all pending thumbnail loads during scrolling
        self.cancel_pending_thumbnail_loads()
        
        # Clear priority queue to avoid loading thumbnails for old positions
        self.priority_thumbnail_queue.clear()
        
        # Schedule thumbnail loading check after scroll stops
        self.parent.after( self.thumbnail_load_delay + 25, self.check_scroll_stopped )
        
    def on_scroll_release( self, event ):
        """Handle scrollbar release"""
        self.on_scroll( event )
        
    def cancel_pending_thumbnail_loads( self ):
        """Cancel all pending thumbnail loads"""
        for after_id in self.pending_thumbnail_loads.values():
            self.parent.after_cancel( after_id )
        self.pending_thumbnail_loads.clear()
        
    def check_scroll_stopped( self ):
        """Check if scrolling has stopped and load visible thumbnails"""
        import time
        current_time = time.time()
        
        # If no recent scroll activity, load visible thumbnails
        if current_time - self.last_scroll_time >= (self.thumbnail_load_delay / 1000.0):
            self.load_visible_thumbnails_debounced()
            # Also schedule verification to catch any missed items
            self.parent.after( 400, self.verify_visible_thumbnails_loaded )
            
    def load_visible_thumbnails_debounced( self ):
        """Load thumbnails for visible items and preload adjacent items"""
        try:
            # Get actually visible items
            visible_items = self.get_visible_treeview_items()
            
            if visible_items:
                visible_indices = [int(item) for item in visible_items if item.isdigit()]
                
                if visible_indices:
                    visible_start = min( visible_indices )
                    visible_end = max( visible_indices )
                    
                    # Define preload range
                    preload_range = 20
                    load_start = max( 0, visible_start - preload_range )
                    load_end = min( len( self.filtered_items ) - 1, visible_end + preload_range )
                    
                    # Queue thumbnails for visible + preload range
                    for index in range( load_start, load_end + 1 ):
                        if index < len( self.filtered_items ):
                            item_data = self.filtered_items[index]
                            filepath = item_data.get( 'filepath' )
                            show_thumbnails = item_data.get( 'show_thumbnails', False )
                            item_id = str( index )
                            
                            if show_thumbnails and filepath and os.path.exists( filepath ):
                                if filepath not in self._thumbnail_cache:
                                    # Queue with priority based on visibility/distance
                                    self.queue_thumbnail_load( filepath, item_id )
                                
        except Exception as e:
            print( f"Error loading visible thumbnails (debounced): {e}" )
        
        # After initial loading, verify all visible items are covered
        self.parent.after( 200, self.verify_visible_thumbnails_loaded )
    
    def verify_visible_thumbnails_loaded( self ):
        """Verify all visible thumbnails are loaded or queued, re-queue any missing ones"""
        # Prevent multiple simultaneous verifications
        if self._verifying_thumbnails:
            return
        self._verifying_thumbnails = True
        
        try:
            visible_items = self.get_visible_treeview_items()
            missing_count = 0
            total_visible = 0
            loaded_count = 0
            pending_count = 0
            
            for item_id in visible_items:
                if item_id.isdigit():
                    index = int( item_id )
                    if index < len( self.filtered_items ):
                        item_data = self.filtered_items[index]
                        filepath = item_data.get( 'filepath' )
                        show_thumbnails = item_data.get( 'show_thumbnails', False )
                        
                        if show_thumbnails and filepath and os.path.exists( filepath ):
                            total_visible += 1
                            
                            # Check if thumbnail is actually DISPLAYED in treeview (not just cached)
                            is_displayed = False
                            try:
                                # Check if treeview item has an image set
                                item_info = self.treeview.item( item_id )
                                is_displayed = bool( item_info.get( 'image' ) )
                            except:
                                is_displayed = False
                                
                            is_pending = item_id in self.pending_thumbnail_loads
                            is_queued = any( item_id == queued_id for _, _, queued_id in self.priority_thumbnail_queue )
                            
                            if is_displayed:
                                loaded_count += 1
                            elif is_pending or is_queued:
                                pending_count += 1
                            else:
                                # Missing or not displayed - re-queue with priority 0
                                missing_count += 1
                                import heapq
                                heapq.heappush( self.priority_thumbnail_queue, (0, filepath, item_id) )
                                
                                # Also trigger immediate processing if not already running
                                if not hasattr( self, '_processing_priority' ) or not self._processing_priority:
                                    self._processing_priority = True
                                    self.parent.after( 1, self._process_priority_thumbnail_load )
            
            if missing_count > 0:
                # Schedule another verification in case more are missed
                self.parent.after( 200, self.verify_visible_thumbnails_loaded )
            elif pending_count > 0:
                # Still have pending items, check again soon
                self.parent.after( 500, self.verify_visible_thumbnails_loaded )
                                
        except Exception as e:
            print( f"Error verifying visible thumbnails: {e}" )
        finally:
            # Reset verification flag
            self._verifying_thumbnails = False
    
    def start_periodic_verification( self ):
        """Start periodic verification of visible thumbnails"""
        self.periodic_verification()
    
    def periodic_verification( self ):
        """Periodically verify visible thumbnails are loaded"""
        try:
            # Only run periodic verification if we have items and thumbnails are enabled
            if (self.filtered_items and 
                any( item_data.get( 'show_thumbnails', False ) for item_data in self.filtered_items )):
                
                visible_items = self.get_visible_treeview_items()
                if visible_items:
                    # Check if any visible items are missing thumbnails
                    missing_any = False
                    for item_id in visible_items:
                        if item_id.isdigit():
                            index = int( item_id )
                            if index < len( self.filtered_items ):
                                item_data = self.filtered_items[index]
                                filepath = item_data.get( 'filepath' )
                                show_thumbnails = item_data.get( 'show_thumbnails', False )
                                
                                if show_thumbnails and filepath and os.path.exists( filepath ):
                                    # Check if thumbnail is actually displayed
                                    is_displayed = False
                                    try:
                                        item_info = self.treeview.item( item_id )
                                        is_displayed = bool( item_info.get( 'image' ) )
                                    except:
                                        is_displayed = False
                                    
                                    if not is_displayed and item_id not in self.pending_thumbnail_loads:
                                        missing_any = True
                                        break
                    
                    # If missing any, run full verification
                    if missing_any:
                        self.verify_visible_thumbnails_loaded()
        
        except Exception as e:
            print( f"Error in periodic verification: {e}" )
        
        # Schedule next periodic check
        self.parent.after( 2000, self.periodic_verification )  # Every 2 seconds

    def select_all( self, event ):
        """Select all items in the filtered list"""
        if self.filtered_items:
            # Select all filtered items
            all_item_ids = [str(i) for i in range(len(self.filtered_items))]
            self.treeview.selection_set( all_item_ids )
            
            # Update selected indices
            self.selected_indices = set(range(len(self.filtered_items)))
            
            # Notify callbacks
            for callback in self.selection_callbacks:
                callback( list(self.selected_indices) )
            
            # Update status if main app is available
            if self.main_app and hasattr( self.main_app, 'update_image_list_status' ):
                self.main_app.update_image_list_status()
            
            return "break"  # Prevent default behavior


class ImageViewer:
    def __init__( self, root ):
        self.root = root
        self.root.title( "Image Viewer" )
        # Window geometry will be set by restore_window_geometry()
        
        # Application state
        self.current_database = None
        self.current_database_path = None
        self.current_image = None
        self.startup_complete = False  # Flag to prevent saving state during startup
        self.current_directory = None
        self.fullscreen_window = None
        self.fullscreen_images = []
        self.fullscreen_index = 0
        self.previous_tab = None
        self.current_browse_image = None
        self.current_database_image = None
        self.browse_folder_images = []
        self.browse_image_index = 0
        self.settings_file = "settings.json"
        self.current_browse_directory = None
        self._rating_repeat_timer = None # For long press arrow key rating changes
        
        # Lazy loading cache
        self.image_metadata_cache = {}  # Cache for image metadata (rating, dimensions, tags)
        self.tag_cache = {}  # Cache for tag data
        self.cache_max_size = 1000  # Maximum items to keep in cache
        
        # Fullscreen lazy loading
        self.fullscreen_filenames = []  # Store filenames instead of full paths
        self.fullscreen_paths_cache = {}  # Cache for resolved paths
        
        # Options settings
        self.show_thumbnails = tk.BooleanVar( value=False )  # Default to no thumbnails
        self.thumbnail_cache = {}  # Cache for 64x64 thumbnails
        self.thumbnail_load_queue = []  # Queue of items waiting for thumbnail loading
        self.thumbnail_loading = False  # Flag to prevent concurrent loading
        self.visible_items_timer = {}  # Track how long items have been visible
        self.visibility_check_timer = None  # Timer for periodic visibility checks
        
        # Supported image formats
        self.supported_formats = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'}
        
        self.setup_ui()
        self.setup_database_menu()
        self.load_settings()
        
        # Restore window geometry and active tab after everything is set up
        self.root.after( 100, self.restore_window_geometry )
        self.root.after( 150, self.restore_active_tab )
        # Mark startup as complete after all restoration is done
        self.root.after( 200, self.complete_startup )
        
        # Bind window close event to save settings
        self.root.protocol( "WM_DELETE_WINDOW", self.on_closing )
        
    def setup_ui( self ):
        """Initialize the main user interface"""
        # Create notebook for tabs
        self.notebook = ttk.Notebook( self.root )
        self.notebook.pack( fill=tk.BOTH, expand=True )
        
        # Browse tab
        self.browse_frame = ttk.Frame( self.notebook )
        self.notebook.add( self.browse_frame, text="Browse" )
        self.setup_browse_tab()
        
        # Database tab
        self.database_frame = ttk.Frame( self.notebook )
        self.notebook.add( self.database_frame, text="Database" )
        self.setup_database_tab()
        
        # Bind tab change event to restore positions when database tab is selected
        self.notebook.bind( "<<NotebookTabChanged>>", self.on_tab_changed )
        
    def setup_database_menu( self ):
        """Setup the database dropdown menu and options menu"""
        menubar = tk.Menu( self.root )
        self.root.config( menu=menubar )
        
        database_menu = tk.Menu( menubar, tearoff=0 )
        menubar.add_cascade( label="Database", menu=database_menu )
        
        database_menu.add_command( label="Create Database", command=self.create_database )
        database_menu.add_command( label="Create Database Here", command=self.create_database_here )
        database_menu.add_command( label="Open Database", command=self.open_database )
        database_menu.add_command( label="Rescan", command=self.rescan_database )
        database_menu.add_separator()
        database_menu.add_command( label="Remove Duplicates from Database", command=self.remove_database_duplicates )
        
        # Options menu
        options_menu = tk.Menu( menubar, tearoff=0 )
        menubar.add_cascade( label="Options", menu=options_menu )
        
        options_menu.add_checkbutton( label="Show Thumbnails", variable=self.show_thumbnails, 
                                    command=self.on_thumbnails_toggle )
        
        # Help menu
        help_menu = tk.Menu( menubar, tearoff=0 )
        menubar.add_cascade( label="Help", menu=help_menu )
        
        help_menu.add_command( label="TODO List", command=self.show_todo_list )
        
    def setup_browse_tab( self ):
        """Setup the Browse tab interface"""
        # Create paned window for two columns
        paned = ttk.PanedWindow( self.browse_frame, orient=tk.HORIZONTAL )
        paned.pack( fill=tk.BOTH, expand=True )
        
        # Left column - Image preview
        left_frame = ttk.Frame( paned )
        paned.add( left_frame, weight=1 )
        
        ttk.Label( left_frame, text="Image Preview" ).pack( pady=5 )
        
        # File path display for browse tab
        self.browse_path_label = ttk.Label( left_frame, text="", font=('TkDefaultFont', 8), foreground='gray', wraplength=400 )
        self.browse_path_label.pack( pady=(0, 5) )
        
        self.browse_preview_label = ttk.Label( left_frame, text="No image selected" )
        self.browse_preview_label.pack( fill=tk.BOTH, expand=True )
        
        # Bind double-click, mouse wheel, and resize events to preview label
        self.browse_preview_label.bind( "<Double-Button-1>", self.on_browse_preview_double_click )
        self.browse_preview_label.bind( "<MouseWheel>", self.on_browse_preview_scroll )
        # Also bind alternative mouse wheel events for better cross-platform support
        self.browse_preview_label.bind( "<Button-4>", lambda e: self.on_browse_preview_scroll_up( e ) )
        self.browse_preview_label.bind( "<Button-5>", lambda e: self.on_browse_preview_scroll_down( e ) )
        self.browse_preview_label.bind( "<Configure>", self.on_browse_preview_resize )
        self.browse_preview_label.bind( "<Button-1>", lambda e: self.browse_preview_label.focus_set() )
        
        # Ensure the label can receive focus for mouse wheel events and keyboard shortcuts
        self.browse_preview_label.bind( "<Enter>", lambda e: self.browse_preview_label.focus_set() )
        # Make label focusable
        self.browse_preview_label.config( takefocus=True )
        
        # Also bind mouse wheel to the left frame to catch events
        left_frame.bind( "<MouseWheel>", self.on_browse_preview_scroll )
        # Also bind to Button-4 and Button-5 for the frame
        left_frame.bind( "<Button-4>", lambda e: self.on_browse_preview_scroll_up( e ) )
        left_frame.bind( "<Button-5>", lambda e: self.on_browse_preview_scroll_down( e ) )

        
        # Add keyboard rating shortcuts for browse preview
        self.browse_preview_label.bind( "<Key-1>", lambda e: self.rate_current_browse_image( 1 ) )
        self.browse_preview_label.bind( "<Key-2>", lambda e: self.rate_current_browse_image( 2 ) )
        self.browse_preview_label.bind( "<Key-3>", lambda e: self.rate_current_browse_image( 3 ) )
        self.browse_preview_label.bind( "<Key-4>", lambda e: self.rate_current_browse_image( 4 ) )
        self.browse_preview_label.bind( "<Key-5>", lambda e: self.rate_current_browse_image( 5 ) )
        self.browse_preview_label.bind( "<Key-6>", lambda e: self.rate_current_browse_image( 6 ) )
        self.browse_preview_label.bind( "<Key-7>", lambda e: self.rate_current_browse_image( 7 ) )
        self.browse_preview_label.bind( "<Key-8>", lambda e: self.rate_current_browse_image( 8 ) )
        self.browse_preview_label.bind( "<Key-9>", lambda e: self.rate_current_browse_image( 9 ) )
        self.browse_preview_label.bind( "<Key-0>", lambda e: self.rate_current_browse_image( 10 ) )
        self.browse_preview_label.bind( "<Key-Left>", lambda e: self.adjust_current_browse_rating( -1 ) )
        self.browse_preview_label.bind( "<Key-Right>", lambda e: self.adjust_current_browse_rating( 1 ) )
        self.browse_preview_label.bind( "<KeyPress-Left>", self.on_rating_arrow_press )
        self.browse_preview_label.bind( "<KeyRelease-Left>", self.on_rating_arrow_release )
        self.browse_preview_label.bind( "<KeyPress-Right>", self.on_rating_arrow_press )
        self.browse_preview_label.bind( "<KeyRelease-Right>", self.on_rating_arrow_release )
        
        # Right column - Directory tree
        right_frame = ttk.Frame( paned )
        paned.add( right_frame, weight=1 )
        
        # Drive selection header
        drive_frame = ttk.Frame( right_frame )
        drive_frame.pack( fill=tk.X, pady=5 )
        
        ttk.Label( drive_frame, text="Drive:" ).pack( side=tk.LEFT, padx=(5, 2) )
        
        self.drive_var = tk.StringVar()
        self.drive_combo = ttk.Combobox( drive_frame, textvariable=self.drive_var, state="readonly", width=10 )
        self.drive_combo.pack( side=tk.LEFT, padx=2 )
        self.drive_combo.bind( "<<ComboboxSelected>>", self.on_drive_changed )
        
        # Populate drives
        self.populate_drives()
        
        ttk.Label( right_frame, text="Directory Structure" ).pack( pady=(10, 5) )
        
        # Directory tree with scrollbar
        tree_frame = ttk.Frame( right_frame )
        tree_frame.pack( fill=tk.BOTH, expand=True )
        
        self.browse_tree = ttk.Treeview( tree_frame, selectmode='extended' )
        tree_scrollbar = ttk.Scrollbar( tree_frame, orient=tk.VERTICAL, command=self.browse_tree.yview )
        self.browse_tree.configure( yscrollcommand=tree_scrollbar.set )
        
        self.browse_tree.pack( side=tk.LEFT, fill=tk.BOTH, expand=True )
        tree_scrollbar.pack( side=tk.RIGHT, fill=tk.Y )
        
        # Bind tree events
        self.browse_tree.bind( "<<TreeviewSelect>>", self.on_browse_tree_select )
        self.browse_tree.bind( "<Double-1>", self.on_browse_tree_double_click )
        self.browse_tree.bind( "<Button-1>", self.on_browse_tree_click )
        self.browse_tree.bind( "<Button-3>", self.on_browse_tree_right_click )
        
        # Initialize selection tracking for browse tree
        self.browse_tree_last_clicked = None
        
        # Initial directory will be loaded after settings are loaded
        
    def setup_database_tab( self ):
        """Setup the Database tab interface"""
        # Database name header
        self.database_name_label = ttk.Label( self.database_frame, text="No database open", font=('TkDefaultFont', 10, 'bold') )
        self.database_name_label.pack( pady=(5, 5) )
        
        # Database action buttons
        button_frame = ttk.Frame( self.database_frame )
        button_frame.pack( fill=tk.X, padx=10, pady=(0, 10) )
        
        ttk.Button( button_frame, text="Open Database", command=self.open_database ).pack( side=tk.LEFT, padx=(0, 5) )
        ttk.Button( button_frame, text="Create Database", command=self.create_database ).pack( side=tk.LEFT, padx=5 )
        
        # Recent databases dropdown
        recent_frame = ttk.Frame( self.database_frame )
        recent_frame.pack( fill=tk.X, padx=10, pady=(0, 5) )
        
        ttk.Label( recent_frame, text="Recent Databases:" ).pack( side=tk.LEFT, padx=(0, 5) )
        self.recent_databases_var = tk.StringVar()
        self.recent_databases_combo = ttk.Combobox( recent_frame, textvariable=self.recent_databases_var, state="readonly", width=50 )
        self.recent_databases_combo.pack( side=tk.LEFT, padx=(0, 5), fill=tk.X, expand=True )
        self.recent_databases_combo.bind( "<<ComboboxSelected>>", self.on_recent_database_selected )
        
        ttk.Button( recent_frame, text="Open Selected", command=self.open_selected_recent_database ).pack( side=tk.RIGHT )
        
        # Create paned window for two columns
        paned = ttk.PanedWindow( self.database_frame, orient=tk.HORIZONTAL )
        paned.pack( fill=tk.BOTH, expand=True )
        
        # Left column - Image preview
        left_frame = ttk.Frame( paned )
        paned.add( left_frame, weight=1 )
        
        ttk.Label( left_frame, text="Image Preview" ).pack( pady=5 )
        
        # File path display for database tab
        self.database_path_label = ttk.Label( left_frame, text="", font=('TkDefaultFont', 8), foreground='gray', wraplength=400 )
        self.database_path_label.pack( pady=(0, 5) )
        
        self.database_preview_label = ttk.Label( left_frame, text="No image selected" )
        self.database_preview_label.pack( fill=tk.BOTH, expand=True )
        
        # Bind double-click, mouse wheel, and resize events to preview label
        self.database_preview_label.bind( "<Double-Button-1>", self.on_database_preview_double_click )
        self.database_preview_label.bind( "<MouseWheel>", self.on_database_preview_scroll )
        # Also bind alternative mouse wheel events for better cross-platform support
        self.database_preview_label.bind( "<Button-4>", lambda e: self.on_database_preview_scroll_up( e ) )
        self.database_preview_label.bind( "<Button-5>", lambda e: self.on_database_preview_scroll_down( e ) )
        self.database_preview_label.bind( "<Configure>", self.on_database_preview_resize )
        self.database_preview_label.bind( "<Button-1>", lambda e: self.database_preview_label.focus_set() )
        
        # Ensure the label can receive focus for mouse wheel events and keyboard shortcuts
        self.database_preview_label.bind( "<Enter>", lambda e: self.database_preview_label.focus_set() )
        # Make label focusable
        self.database_preview_label.config( takefocus=True )
        
        # Also bind mouse wheel to the left frame to catch events
        left_frame.bind( "<MouseWheel>", self.on_database_preview_scroll )
        # Also bind to Button-4 and Button-5 for the frame
        left_frame.bind( "<Button-4>", lambda e: self.on_database_preview_scroll_up( e ) )
        left_frame.bind( "<Button-5>", lambda e: self.on_database_preview_scroll_down( e ) )

        
        # Add keyboard rating shortcuts for database preview (local to preview label)
        self.database_preview_label.bind( "<Key-1>", lambda e: self.rate_current_database_image( 1 ) )
        self.database_preview_label.bind( "<Key-2>", lambda e: self.rate_current_database_image( 2 ) )
        self.database_preview_label.bind( "<Key-3>", lambda e: self.rate_current_database_image( 3 ) )
        self.database_preview_label.bind( "<Key-4>", lambda e: self.rate_current_database_image( 4 ) )
        self.database_preview_label.bind( "<Key-5>", lambda e: self.rate_current_database_image( 5 ) )
        self.database_preview_label.bind( "<Key-6>", lambda e: self.rate_current_database_image( 6 ) )
        self.database_preview_label.bind( "<Key-7>", lambda e: self.rate_current_database_image( 7 ) )
        self.database_preview_label.bind( "<Key-8>", lambda e: self.rate_current_database_image( 8 ) )
        self.database_preview_label.bind( "<Key-9>", lambda e: self.rate_current_database_image( 9 ) )
        self.database_preview_label.bind( "<Key-0>", lambda e: self.rate_current_database_image( 10 ) )
        
        # Add global keyboard rating shortcuts that work from anywhere in the database tab
        def handle_global_rating( event, rating ):
            # Only handle if we're in the database tab and have a current selection
            if (self.notebook.index( self.notebook.select() ) == 1 and  # Database tab is selected
                hasattr( self, 'selected_image_files' ) and self.selected_image_files):
                self.rate_current_database_image( rating )
                return "break"  # Prevent further event propagation
                
        self.root.bind_all( "<Key-1>", lambda e: handle_global_rating( e, 1 ) )
        self.root.bind_all( "<Key-2>", lambda e: handle_global_rating( e, 2 ) )
        self.root.bind_all( "<Key-3>", lambda e: handle_global_rating( e, 3 ) )
        self.root.bind_all( "<Key-4>", lambda e: handle_global_rating( e, 4 ) )
        self.root.bind_all( "<Key-5>", lambda e: handle_global_rating( e, 5 ) )
        self.root.bind_all( "<Key-6>", lambda e: handle_global_rating( e, 6 ) )
        self.root.bind_all( "<Key-7>", lambda e: handle_global_rating( e, 7 ) )
        self.root.bind_all( "<Key-8>", lambda e: handle_global_rating( e, 8 ) )
        self.root.bind_all( "<Key-9>", lambda e: handle_global_rating( e, 9 ) )
        self.root.bind_all( "<Key-0>", lambda e: handle_global_rating( e, 10 ) )
        self.database_preview_label.bind( "<Key-Left>", lambda e: self.adjust_current_database_rating( -1 ) )
        self.database_preview_label.bind( "<Key-Right>", lambda e: self.adjust_current_database_rating( 1 ) )
        self.database_preview_label.bind( "<KeyPress-Left>", self.on_rating_arrow_press )
        self.database_preview_label.bind( "<KeyRelease-Left>", self.on_rating_arrow_release )
        self.database_preview_label.bind( "<KeyPress-Right>", self.on_rating_arrow_press )
        self.database_preview_label.bind( "<KeyRelease-Right>", self.on_rating_arrow_release )
        
        # Right column - Tag filters and image list
        right_frame = ttk.Frame( paned )
        paned.add( right_frame, weight=2 )
        
        # Create vertical paned window for resizable sections
        vertical_paned = ttk.PanedWindow( right_frame, orient=tk.VERTICAL )
        vertical_paned.pack( fill=tk.BOTH, expand=True, padx=5, pady=5 )
        
        # Top section - Split into Tag Filters, Image Tags, and File Tags
        top_section = ttk.Frame( vertical_paned )
        vertical_paned.add( top_section, weight=1 )
        
        # Left side - Tag filter section (1/3 width)
        tag_frame = ttk.LabelFrame( top_section, text="Tag Filters" )
        tag_frame.pack( side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 2) )
        
        # Middle - Image Tags section (1/3 width)
        image_tags_frame = ttk.LabelFrame( top_section, text="Image Tags" )
        image_tags_frame.pack( side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(2, 2) )
        
        # Right side - File Tags section (1/3 width)
        file_tags_frame = ttk.LabelFrame( top_section, text="File Tags" )
        file_tags_frame.pack( side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(2, 0) )
        
        # File tags display with scrollable text
        file_tags_text_frame = ttk.Frame( file_tags_frame )
        file_tags_text_frame.pack( fill=tk.BOTH, expand=True, padx=5, pady=5 )
        
        # Create text widget with scrollbar
        self.file_tags_text = tk.Text( file_tags_text_frame, wrap=tk.WORD, 
                                      state=tk.DISABLED, height=10, 
                                      font=('TkDefaultFont', 9) )
        file_tags_scrollbar = ttk.Scrollbar( file_tags_text_frame, orient=tk.VERTICAL, 
                                           command=self.file_tags_text.yview )
        self.file_tags_text.configure( yscrollcommand=file_tags_scrollbar.set )
        
        self.file_tags_text.pack( side=tk.LEFT, fill=tk.BOTH, expand=True )
        file_tags_scrollbar.pack( side=tk.RIGHT, fill=tk.Y )
        
        # Initialize tag editing variables
        self.image_tag_checkboxes = {}  # Dictionary to store tag checkbox variables
        self.selected_image_files = []  # Currently selected files for tag editing
        self.processing_tag_change = False  # Flag to prevent double-processing
        
        # Existing tags section with scrollable checkboxes
        existing_tags_frame = ttk.LabelFrame( image_tags_frame, text="Existing Tags" )
        existing_tags_frame.pack( fill=tk.BOTH, expand=True, padx=5, pady=(5, 2) )
        
        # Scrollable frame for tag checkboxes
        tag_canvas_frame = ttk.Frame( existing_tags_frame )
        tag_canvas_frame.pack( fill=tk.BOTH, expand=True, pady=2 )
        
        self.image_tag_canvas = tk.Canvas( tag_canvas_frame, height=120 )
        self.image_tag_canvas.configure( highlightthickness=0 )  # Remove border
        image_tag_scrollbar = ttk.Scrollbar( tag_canvas_frame, orient=tk.VERTICAL, command=self.image_tag_canvas.yview )
        self.image_tag_scrollable_frame = ttk.Frame( self.image_tag_canvas )
        
        self.image_tag_scrollable_frame.bind( "<Configure>", lambda e: self.image_tag_canvas.configure( scrollregion=self.image_tag_canvas.bbox( "all" ) ) )
        self.image_tag_canvas.create_window( (0, 0), window=self.image_tag_scrollable_frame, anchor="nw" )
        self.image_tag_canvas.configure( yscrollcommand=image_tag_scrollbar.set )
        
        self.image_tag_canvas.pack( side=tk.LEFT, fill=tk.BOTH, expand=True )
        image_tag_scrollbar.pack( side=tk.RIGHT, fill=tk.Y )
        
        # Enable mouse wheel scrolling
        def on_image_tag_canvas_scroll( event ):
            self.image_tag_canvas.yview_scroll( int( -1 * (event.delta / 120) ), "units" )
        self.image_tag_canvas.bind( "<MouseWheel>", on_image_tag_canvas_scroll )
        
        # Rating section - pack at bottom before Apply button
        rating_frame = ttk.Frame( image_tags_frame )
        rating_frame.pack( side=tk.BOTTOM, fill=tk.X, padx=5, pady=2 )
        
        ttk.Label( rating_frame, text="Rating:" ).pack( anchor=tk.W )
        self.image_rating_var = tk.IntVar( value=0 )
        self.image_rating_scale = tk.Scale( rating_frame, from_=0, to=10, orient=tk.HORIZONTAL, 
                                           variable=self.image_rating_var, 
                                           command=self.on_image_rating_changed )
        self.image_rating_scale.pack( fill=tk.X, pady=(2, 0) )
        
        # Override click behavior to jump to position instead of increment/decrement
        self.image_rating_scale.bind( "<Button-1>", self.on_rating_scale_click )
        
        # New tags section - pack at bottom before Rating
        new_tags_frame = ttk.Frame( image_tags_frame )
        new_tags_frame.pack( side=tk.BOTTOM, fill=tk.X, padx=5, pady=2 )
        
        ttk.Label( new_tags_frame, text="Add New Tags:" ).pack( anchor=tk.W )
        self.image_new_tags_entry = tk.Entry( new_tags_frame )
        self.image_new_tags_entry.pack( fill=tk.X, pady=(2, 0) )
        
        # Bind Enter key to apply new tags
        self.image_new_tags_entry.bind( "<Return>", lambda e: self.apply_image_tag_changes() )
        
        # Apply button - ensure it's always at the bottom of image_tags_frame
        apply_frame = ttk.Frame( image_tags_frame )
        apply_frame.pack( side=tk.BOTTOM, fill=tk.X, padx=5, pady=(2, 5) )
        
        self.image_apply_button = ttk.Button( apply_frame, text="Apply", command=self.apply_image_tag_changes, state='disabled' )
        self.image_apply_button.pack( side=tk.RIGHT )
        
        # Create frame for tag filter with proper grid layout
        tag_filter_frame = ttk.Frame( tag_frame )
        tag_filter_frame.pack( fill=tk.BOTH, expand=True, padx=5, pady=5 )
        
        # Configure grid columns with fixed widths
        tag_filter_frame.grid_columnconfigure( 0, minsize=80 )  # Include (OR) column
        tag_filter_frame.grid_columnconfigure( 1, minsize=80 )  # Include (AND) column
        tag_filter_frame.grid_columnconfigure( 2, minsize=60 )  # Exclude column  
        tag_filter_frame.grid_columnconfigure( 3, weight=1 )    # Tag name column
        
        # Rating filter section
        rating_filter_frame = ttk.Frame( tag_filter_frame )
        rating_filter_frame.grid( row=0, column=0, columnspan=4, sticky="ew", padx=5, pady=(0, 5) )
        
        ttk.Label( rating_filter_frame, text="Rating Filter:" ).pack( side=tk.LEFT, padx=(0, 10) )
        
        # Min rating
        ttk.Label( rating_filter_frame, text="Min:" ).pack( side=tk.LEFT, padx=(0, 5) )
        self.min_rating_var = tk.IntVar( value=0 )
        self.min_rating_scale = tk.Scale( rating_filter_frame, from_=0, to=10, orient=tk.HORIZONTAL, 
                                         variable=self.min_rating_var, length=80,
                                         command=self.on_rating_filter_changed )
        self.min_rating_scale.pack( side=tk.LEFT, padx=(0, 10) )
        
        # Max rating  
        ttk.Label( rating_filter_frame, text="Max:" ).pack( side=tk.LEFT, padx=(0, 5) )
        self.max_rating_var = tk.IntVar( value=10 )
        self.max_rating_scale = tk.Scale( rating_filter_frame, from_=0, to=10, orient=tk.HORIZONTAL,
                                         variable=self.max_rating_var, length=80,
                                         command=self.on_rating_filter_changed )
        self.max_rating_scale.pack( side=tk.LEFT, padx=(0, 10) )
        
        # Rating separator
        ttk.Separator( tag_filter_frame, orient='horizontal' ).grid( row=1, column=0, columnspan=4, sticky="ew", pady=2 )
        
        # Headers
        ttk.Label( tag_filter_frame, text="Include (OR)" ).grid( row=2, column=0, sticky="w", padx=5, pady=2 )
        ttk.Label( tag_filter_frame, text="Include (AND)" ).grid( row=2, column=1, sticky="w", padx=5, pady=2 )
        ttk.Label( tag_filter_frame, text="Exclude" ).grid( row=2, column=2, sticky="w", padx=5, pady=2 )
        ttk.Label( tag_filter_frame, text="Tag Name" ).grid( row=2, column=3, sticky="w", padx=5, pady=2 )
        
        # Scrollable frame for tag rows
        canvas_frame = ttk.Frame( tag_filter_frame )
        canvas_frame.grid( row=3, column=0, columnspan=4, sticky="nsew", pady=5 )
        tag_filter_frame.grid_rowconfigure( 3, weight=1 )
        
        self.tag_canvas = tk.Canvas( canvas_frame, height=150 )
        self.tag_canvas.configure( highlightthickness=0 )  # Remove border
        tag_scrollbar = ttk.Scrollbar( canvas_frame, orient=tk.VERTICAL, command=self.tag_canvas.yview )
        self.tag_scrollable_frame = ttk.Frame( self.tag_canvas )
        
        # Configure scrollable frame
        self.tag_scrollable_frame.bind( "<Configure>", lambda e: self.tag_canvas.configure( scrollregion=self.tag_canvas.bbox( "all" ) ) )
        self.tag_canvas.create_window( (0, 0), window=self.tag_scrollable_frame, anchor="nw" )
        self.tag_canvas.configure( yscrollcommand=tag_scrollbar.set )
        
        # Configure scrollable frame columns to match parent
        self.tag_scrollable_frame.grid_columnconfigure( 0, minsize=80 )  # Include (OR)
        self.tag_scrollable_frame.grid_columnconfigure( 1, minsize=80 )  # Include (AND)
        self.tag_scrollable_frame.grid_columnconfigure( 2, minsize=60 )  # Exclude
        self.tag_scrollable_frame.grid_columnconfigure( 3, weight=1 )    # Tag name
        
        self.tag_canvas.pack( side=tk.LEFT, fill=tk.BOTH, expand=True )
        tag_scrollbar.pack( side=tk.RIGHT, fill=tk.Y )
        
        # Clear filters button - ensure it's always at the bottom of tag_frame
        filter_button_frame = ttk.Frame( tag_frame )
        filter_button_frame.pack( side=tk.BOTTOM, fill=tk.X, pady=5 )
        
        ttk.Button( filter_button_frame, text="Clear All Filters", command=self.clear_filters ).pack( side=tk.LEFT, padx=2 )
        
        # Image list section
        image_frame = ttk.LabelFrame( vertical_paned, text="Filtered Images" )
        vertical_paned.add( image_frame, weight=2 )
        
        # Store references to paned windows for size management
        self.vertical_paned = vertical_paned
        self.horizontal_paned = paned  # Reference to the main horizontal paned window
        
        # Configure minimum sizes for paned window sections with dynamic constraints
        def configure_pane_constraints():
            # Calculate minimum height needed for essential UI elements
            # Base height: buttons + labels + padding = ~160px
            # Scrollable areas: minimum 2 rows = ~50px each = 100px
            # Total minimum: ~260px
            min_top_height = 260
            min_bottom_height = 100  # Minimum for image list
            
            try:
                # Try ttk.PanedWindow pane configuration
                vertical_paned.pane( 0, minsize=min_top_height )
                vertical_paned.pane( 1, minsize=min_bottom_height )
            except:
                # Fallback - set initial sash position
                vertical_paned.after( 100, lambda: vertical_paned.sashpos( 0, min_top_height + 50 ) )
        
        # Apply constraints and restore positions after window is fully initialized
        self.root.after( 100, configure_pane_constraints )
        
        # Ensure restoration happens when window is fully visible
        def delayed_restore():
            self.root.after( 100, self.restore_paned_positions )
        
        self.root.after( 500, delayed_restore )
        
        # Add resize handler to enforce minimum scrollable area heights and save positions
        def on_paned_configure( event ):
            self.enforce_scrollable_minimums()
            # Save positions after a short delay to avoid excessive saves during dragging
            if hasattr( self, '_save_timer' ):
                self.root.after_cancel( self._save_timer )
            self._save_timer = self.root.after( 1000, self.save_paned_positions_only )
        
        vertical_paned.bind( "<Configure>", on_paned_configure )
        
        # Image listbox with scrollbar
        image_list_frame = ttk.Frame( image_frame )
        image_list_frame.pack( fill=tk.BOTH, expand=True )
        
        # Add status label at the top of the image list
        self.image_list_status_label = ttk.Label( image_list_frame, text="0 total items, 0 selected", font=('TkDefaultFont', 9) )
        self.image_list_status_label.pack( side=tk.TOP, pady=(0, 5) )
        
        # Add keyboard shortcut hint
        shortcut_hint = ttk.Label( image_list_frame, text="Ctrl+A: Select all images", font=('TkDefaultFont', 8), foreground='gray' )
        shortcut_hint.pack( side=tk.TOP, pady=(0, 5) )
        
        # Add debug button for testing
        debug_button = ttk.Button( image_list_frame, text="Debug Viewport", command=self.debug_virtual_scrolling )
        debug_button.pack( side=tk.BOTTOM, pady=(2, 0) )
        
        # Create treeview-based image list that handles large datasets properly
        self.virtual_image_list = TreeviewImageList( image_list_frame, item_height=68 )
        self.virtual_image_list.frame.pack( fill=tk.BOTH, expand=True )
        
        # Set reference to main app for thumbnail generation
        self.virtual_image_list.main_app = self
        
        # Set up callbacks for selection and double-click
        self.virtual_image_list.add_selection_callback( self.on_virtual_selection_changed )
        self.virtual_image_list.on_item_double_click = self.on_virtual_double_click
        
        # Compatibility attributes for existing code
        self.image_list_items = []  # Will be synced with virtual list
        self.selected_image_indices = []  # Track selected indices for compatibility
        
        # Initialize database_image_listbox property for compatibility
        self.database_image_listbox = self.DatabaseImageListboxCompat( self )
        
        # Initialize tag filters and checkbox tracking
        self.included_or_tags = set()
        self.included_and_tags = set()
        self.excluded_tags = set()
        self.tag_checkboxes = {}  # Dictionary to store checkbox variables
        self.all_include_or_var = tk.BooleanVar()
        self.all_include_and_var = tk.BooleanVar()
        self.all_exclude_var = tk.BooleanVar()
        
    def update_image_list_status( self ):
        """Update the status label showing image count and selection info"""
        if hasattr( self, 'virtual_image_list' ) and hasattr( self, 'image_list_status_label' ):
            total_images = len( self.virtual_image_list.filtered_items )
            selected_count = len( self.virtual_image_list.selected_indices )
            
            status_text = f"{total_images} total items, {selected_count} selected"
            self.image_list_status_label.configure( text=status_text )
            
    def debug_virtual_scrolling( self ):
        """Debug method to print virtual scrolling information immediately"""
        if hasattr( self, 'virtual_image_list' ):
            vlist = self.virtual_image_list
            print( "\n=== VIRTUAL SCROLLING DEBUG ===" )
            print( f"Total filtered items: {len(vlist.filtered_items)}" )
            print( f"Rendered items: {len(vlist.rendered_items)}" )
            print( f"Viewport: start={vlist.viewport_start}, end={vlist.viewport_end}" )
            print( f"Percentage through list: {(vlist.viewport_end / len(vlist.filtered_items) * 100):.1f}%" )
            
            # Check data integrity around problematic area
            print( "\nChecking data integrity around index 500..." )
            for i in [499, 500, 501, 502, 503]:
                if i < len( vlist.filtered_items ):
                    item = vlist.filtered_items[i]
                    filename = item.get( 'filename', 'NO_FILENAME' )
                    filepath = item.get( 'filepath', 'NO_FILEPATH' )
                    exists = os.path.exists( filepath ) if filepath else False
                    print( f"Index {i}: {filename}, exists: {exists}" )
            
            print( "\nChecking data integrity near the end..." )
            end_indices = [2930, 2931, 2932, 2933, 2934]
            for i in end_indices:
                if i < len( vlist.filtered_items ):
                    item = vlist.filtered_items[i]
                    filename = item.get( 'filename', 'NO_FILENAME' )
                    filepath = item.get( 'filepath', 'NO_FILEPATH' )
                    exists = os.path.exists( filepath ) if filepath else False
                    print( f"Index {i}: {filename}, exists: {exists}" )
            
            # Test scrolling to problem area
            print( "\nTesting scroll to index 500 area..." )
            try:
                # Scroll to around index 500 (500/2935 ≈ 0.17)
                vlist.canvas.yview_moveto( 0.17 )
                vlist.update_viewport()
                print( f"At 17% position - Viewport: start={vlist.viewport_start}, end={vlist.viewport_end}" )
                
            except Exception as e:
                print( f"Error testing scroll to 500 area: {e}" )
                
            print( "=== END DEBUG ===\n" )
        
    def populate_drives( self ):
        """Populate the drive selection combobox with available drives"""
        import string
        drives = []
        
        # Check for available drives on Windows
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists( drive ):
                drives.append( drive )
                
        self.drive_combo['values'] = drives
        
        # Set default to C: if available, otherwise first available drive
        if "C:\\" in drives:
            self.drive_var.set( "C:\\" )
        elif drives:
            self.drive_var.set( drives[0] )
            
    def on_drive_changed( self, event=None ):
        """Handle drive selection change"""
        selected_drive = self.drive_var.get()
        if selected_drive:
            self.load_directory_tree( selected_drive )
            
    def load_directory_tree( self, start_path=None ):
        """Load directory structure into the tree view"""
        if start_path is None:
            start_path = self.drive_var.get() if hasattr( self, 'drive_var' ) and self.drive_var.get() else "C:\\"
            
        self.browse_tree.delete( *self.browse_tree.get_children() )
        
        try:
            self.populate_tree( "", start_path )
            self.current_browse_directory = start_path
        except PermissionError:
            messagebox.showerror( "Error", f"Permission denied accessing {start_path}" )
            
    def load_directory_tree_and_expand( self, target_directory ):
        """Load directory tree from drive root and expand to target directory"""
        # Get the drive root
        drive = os.path.splitdrive( target_directory )[0] + "\\"
        
        # Load tree from drive root
        self.load_directory_tree( drive )
        
        # Expand tree to target directory
        self.expand_tree_to_path( target_directory )
        
    def expand_tree_to_path( self, target_path ):
        """Expand tree nodes to show the target path"""
        # Normalize the path
        target_path = os.path.normpath( target_path )
        drive = os.path.splitdrive( target_path )[0] + "\\"
        
        # Get path components relative to drive
        relative_path = os.path.relpath( target_path, drive )
        if relative_path == '.':
            return  # Already at drive root
            
        path_parts = relative_path.split( os.sep )
        current_path = drive
        current_item = ""
        
        # Find and expand each part of the path
        for part in path_parts:
            current_path = os.path.join( current_path, part )
            
            # Find the tree item for this path component
            if current_item == "":
                # Looking in root level
                children = self.browse_tree.get_children()
            else:
                # Looking in current item's children
                children = self.browse_tree.get_children( current_item )
                
            # Find the matching child
            found_item = None
            for child in children:
                child_path = self.browse_tree.item( child )['values'][0]
                if os.path.normpath( child_path ) == os.path.normpath( current_path ):
                    found_item = child
                    break
                    
            if found_item:
                # Expand this item if it's a directory
                if os.path.isdir( current_path ):
                    self.browse_tree.item( found_item, open=True )
                    # Trigger expansion to load children
                    self.on_tree_expand_for_path( found_item )
                    
                current_item = found_item
                
                # If this is the final target, select it
                if os.path.normpath( current_path ) == os.path.normpath( target_path ):
                    self.browse_tree.selection_set( found_item )
                    self.browse_tree.focus( found_item )
                    self.browse_tree.see( found_item )
            else:
                break  # Path not found, stop expanding
                
    def on_tree_expand_for_path( self, item ):
        """Expand tree item for path navigation (similar to on_tree_expand but without event)"""
        children = self.browse_tree.get_children( item )
        
        if len( children ) == 1 and self.browse_tree.item( children[0] )['text'] == "Loading...":
            # Remove dummy child and populate real children
            self.browse_tree.delete( children[0] )
            path = self.browse_tree.item( item )['values'][0]
            self.populate_tree( item, path )
            
    def populate_tree( self, parent, path ):
        """Recursively populate the directory tree"""
        try:
            items = sorted( os.listdir( path ) )
            for item in items:
                if item.startswith( '.' ):
                    continue
                    
                item_path = os.path.join( path, item )
                
                if os.path.isdir( item_path ):
                    # Directory
                    node = self.browse_tree.insert( parent, tk.END, text=item, values=[item_path], tags=["directory"] )
                    # Add a dummy child to make it expandable
                    self.browse_tree.insert( node, tk.END, text="Loading..." )
                    self.browse_tree.bind( "<<TreeviewOpen>>", self.on_tree_expand )
                elif self.is_image_file( item_path ):
                    # Image file
                    self.browse_tree.insert( parent, tk.END, text=item, values=[item_path], tags=["image"] )
        except PermissionError:
            pass
            
    def on_tree_expand( self, event ):
        """Handle tree node expansion"""
        item = self.browse_tree.focus()
        children = self.browse_tree.get_children( item )
        
        if len( children ) == 1 and self.browse_tree.item( children[0] )['text'] == "Loading...":
            # Remove dummy child and populate real children
            self.browse_tree.delete( children[0] )
            path = self.browse_tree.item( item )['values'][0]
            self.populate_tree( item, path )
            
    def is_image_file( self, filepath ):
        """Check if file is a supported image format"""
        return Path( filepath ).suffix.lower() in self.supported_formats
        
    def is_file_in_database( self, filepath ):
        """Check if a file exists in the current database"""
        if not self.current_database_path:
            return False
            
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            relative_path = os.path.relpath( filepath, self.current_database )
            cursor.execute( "SELECT id FROM images WHERE relative_path = ?", (relative_path,) )
            result = cursor.fetchone()
            
            conn.close()
            return result is not None
        except Exception:
            return False
        
    def on_browse_tree_click( self, event ):
        """Handle click events for proper CTRL/SHIFT multi-selection in browse tree"""
        item = self.browse_tree.identify_row( event.y )
        if not item:
            return
            
        if event.state & 0x4:  # Ctrl key - toggle selection
            if item in self.browse_tree.selection():
                self.browse_tree.selection_remove( item )
            else:
                self.browse_tree.selection_add( item )
            self.browse_tree_last_clicked = item
            
        elif event.state & 0x1:  # Shift key - range selection
            if self.browse_tree_last_clicked:
                # Get all items in the tree
                all_items = self.get_all_tree_items( self.browse_tree )
                
                try:
                    start_idx = all_items.index( self.browse_tree_last_clicked )
                    end_idx = all_items.index( item )
                    
                    # Select range
                    start, end = min( start_idx, end_idx ), max( start_idx, end_idx )
                    items_to_select = all_items[start:end+1]
                    self.browse_tree.selection_set( items_to_select )
                except ValueError:
                    # Item not found, just select the clicked item
                    self.browse_tree.selection_set( item )
            else:
                self.browse_tree.selection_set( item )
            self.browse_tree_last_clicked = item
            
        else:  # Normal click - single selection
            self.browse_tree.selection_set( item )
            self.browse_tree_last_clicked = item
            
    def get_all_tree_items( self, tree, parent="" ):
        """Get all items in tree in display order"""
        items = []
        for child in tree.get_children( parent ):
            items.append( child )
            if tree.item( child, "open" ):  # If expanded, include children
                items.extend( self.get_all_tree_items( tree, child ) )
        return items

    def on_browse_tree_select( self, event ):
        """Handle selection in browse tree"""
        selection = self.browse_tree.selection()
        if selection:
            item = selection[0]
            filepath = self.browse_tree.item( item )['values'][0]
            
            if self.is_image_file( filepath ):
                self.current_browse_image = filepath
                self.current_browse_directory = os.path.dirname( filepath )
                self.update_browse_folder_images( filepath )
                self.display_image_preview( filepath, self.browse_preview_label )
                # Save the directory containing the selected image
                self.save_directory_only( self.current_browse_directory )
            elif os.path.isdir( filepath ):
                # User selected a directory
                self.current_browse_directory = filepath
                self.save_directory_only( filepath )
                
    def on_browse_tree_double_click( self, event ):
        """Handle double click in browse tree"""
        selection = self.browse_tree.selection()
        if selection:
            item = selection[0]
            filepath = self.browse_tree.item( item )['values'][0]
            
            if self.is_image_file( filepath ):
                self.enter_fullscreen_mode( filepath )
                
    def on_browse_tree_right_click( self, event ):
        """Handle right click in browse tree - show context menu"""
        item = self.browse_tree.identify_row( event.y )
        if not item:
            return
            
        # If right-clicked item is not selected, select it (clear other selections)
        if item not in self.browse_tree.selection():
            self.browse_tree.selection_set( item )
            self.browse_tree_last_clicked = item
            
        # Show context menu based on selection
        self.show_browse_context_menu( event )
        
    def show_browse_context_menu( self, event ):
        """Show context menu for browse tree selection"""
        if not self.current_database_path:
            # No database open - limited options
            context_menu = tk.Menu( self.root, tearoff=0 )
            selection = self.browse_tree.selection()
            if len( selection ) == 1:
                item = selection[0]
                filepath = self.browse_tree.item( item )['values'][0]
                if os.path.isdir( filepath ):
                    context_menu.add_command( label="Create Database Here", 
                                            command=lambda: self.create_database_in_directory( filepath ) )
            
            if context_menu.index( tk.END ) is not None:  # Menu has items
                try:
                    context_menu.tk_popup( event.x_root, event.y_root )
                finally:
                    context_menu.grab_release()
            return
            
        # Database is open - show tag editing options
        context_menu = tk.Menu( self.root, tearoff=0 )
        selection = self.browse_tree.selection()
        
        if len( selection ) == 1:
            # Single selection
            item = selection[0]
            filepath = self.browse_tree.item( item )['values'][0]
            
            if self.is_image_file( filepath ):
                context_menu.add_command( label="Edit Tags...", 
                                        command=lambda: self.show_tag_dialog( filepath ) )
            elif os.path.isdir( filepath ):
                context_menu.add_command( label="Edit Tags for Directory (Recursive)...", 
                                        command=lambda: self.show_directory_tag_dialog( filepath ) )
                context_menu.add_separator()
                context_menu.add_command( label="Create Database Here", 
                                        command=lambda: self.create_database_in_directory( filepath ) )
        else:
            # Multiple selection - check if any are images in database
            image_files = []
            for item in selection:
                filepath = self.browse_tree.item( item )['values'][0]
                if self.is_image_file( filepath ) and self.is_file_in_database( filepath ):
                    image_files.append( filepath )
                    
            if image_files:
                context_menu.add_command( label=f"Edit Tags for {len(image_files)} Images...", 
                                        command=lambda: self.show_multi_tag_dialog( image_files ) )
        
        if context_menu.index( tk.END ) is not None:  # Menu has items
            try:
                context_menu.tk_popup( event.x_root, event.y_root )
            finally:
                context_menu.grab_release()
                
    def on_browse_preview_double_click( self, event ):
        """Handle double click on browse preview image"""
        if self.current_browse_image and os.path.exists( self.current_browse_image ):
            self.enter_fullscreen_mode( self.current_browse_image )
            
    def update_browse_folder_images( self, selected_filepath ):
        """Update the list of images in the current folder for preview scrolling"""
        folder_path = os.path.dirname( selected_filepath )
        self.browse_folder_images = []
        
        try:
            # Get all image files in the folder
            for file in sorted( os.listdir( folder_path ) ):
                file_path = os.path.join( folder_path, file )
                if self.is_image_file( file_path ):
                    self.browse_folder_images.append( file_path )
                    
            # Set current index
            if selected_filepath in self.browse_folder_images:
                self.browse_image_index = self.browse_folder_images.index( selected_filepath )
            else:
                self.browse_image_index = 0
                
        except OSError:
            self.browse_folder_images = [selected_filepath] if selected_filepath else []
            self.browse_image_index = 0
            
    def on_browse_preview_scroll( self, event ):
        """Handle mouse wheel scrolling over browse preview image"""
        if not self.browse_folder_images:
            return
            
        if event.delta > 0:
            # Scroll up - previous image (don't wrap)
            if self.browse_image_index > 0:
                self.browse_image_index -= 1
                self.current_browse_image = self.browse_folder_images[self.browse_image_index]
                self.display_image_preview( self.current_browse_image, self.browse_preview_label )
        else:
            # Scroll down - next image (don't wrap)
            if self.browse_image_index < len( self.browse_folder_images ) - 1:
                self.browse_image_index += 1
                self.current_browse_image = self.browse_folder_images[self.browse_image_index]
                self.display_image_preview( self.current_browse_image, self.browse_preview_label )
                
    def on_browse_preview_scroll_up( self, event ):
        """Handle scroll up (Button-4) for browse preview"""
        if self.browse_folder_images and self.browse_image_index > 0:
            self.browse_image_index -= 1
            self.current_browse_image = self.browse_folder_images[self.browse_image_index]
            self.display_image_preview( self.current_browse_image, self.browse_preview_label )
            
    def on_browse_preview_scroll_down( self, event ):
        """Handle scroll down (Button-5) for browse preview"""
        if self.browse_folder_images and self.browse_image_index < len( self.browse_folder_images ) - 1:
            self.browse_image_index += 1
            self.current_browse_image = self.browse_folder_images[self.browse_image_index]
            self.display_image_preview( self.current_browse_image, self.browse_preview_label )
                
    def on_browse_preview_resize( self, event ):
        """Handle resize events for browse preview label"""
        # Only process resize events for the label itself, not child widgets
        if event.widget == self.browse_preview_label:
            # If there's a current image, redisplay it with the new size
            if hasattr( self.browse_preview_label, 'current_image_path' ) and self.browse_preview_label.current_image_path:
                self.display_image_preview( self.browse_preview_label.current_image_path, self.browse_preview_label )
                
    def on_database_preview_resize( self, event ):
        """Handle resize events for database preview label"""
        # Only process resize events for the label itself, not child widgets
        if event.widget == self.database_preview_label:
            # If there's a current image, redisplay it with the new size
            if hasattr( self.database_preview_label, 'current_image_path' ) and self.database_preview_label.current_image_path:
                self.display_image_preview( self.database_preview_label.current_image_path, self.database_preview_label )
                
    def on_database_preview_scroll( self, event ):
        """Handle mouse wheel scrolling over database preview image"""
        if not self.current_database_path:
            return
            
        # Get current selection and total items
        current_selection = self.database_image_listbox.curselection()
        total_items = self.database_image_listbox.size()
        
        if total_items == 0:
            return
            
        if current_selection:
            current_index = current_selection[0]
        else:
            current_index = 0
            
        if event.delta > 0:
            # Scroll up - previous image (don't wrap)
            if current_index > 0:
                new_index = current_index - 1
            else:
                return  # Already at first image
        else:
            # Scroll down - next image (don't wrap)
            if current_index < total_items - 1:
                new_index = current_index + 1
            else:
                return  # Already at last image
                
        # Select the new item and auto-scroll the listbox to keep it visible
        self.database_image_listbox.selection_clear( 0, tk.END )
        self.database_image_listbox.selection_set( new_index )
        self.database_image_listbox.see( new_index )  # Auto-scroll to ensure the selected item is visible
        
        # Trigger the selection event to update preview and tags
        self.on_database_image_select( None )
        
    def on_database_preview_scroll_up( self, event ):
        """Handle scroll up (Button-4) for database preview"""
        if not self.current_database_path:
            return
            
        current_selection = self.database_image_listbox.curselection()
        if current_selection:
            current_index = current_selection[0]
            if current_index > 0:
                new_index = current_index - 1
                self.database_image_listbox.selection_clear( 0, tk.END )
                self.database_image_listbox.selection_set( new_index )
                self.database_image_listbox.see( new_index )
                self.on_database_image_select( None )
                
    def on_database_preview_scroll_down( self, event ):
        """Handle scroll down (Button-5) for database preview"""
        if not self.current_database_path:
            return
            
        current_selection = self.database_image_listbox.curselection()
        total_items = self.database_image_listbox.size()
        if current_selection and total_items > 0:
            current_index = current_selection[0]
            if current_index < total_items - 1:
                new_index = current_index + 1
                self.database_image_listbox.selection_clear( 0, tk.END )
                self.database_image_listbox.selection_set( new_index )
                self.database_image_listbox.see( new_index )
                self.on_database_image_select( None )
                
    def apply_exif_orientation( self, image ):
        """Apply EXIF orientation to rotate image correctly"""
        try:
            # Try the newer Pillow method first (available in Pillow 6.0+)
            if hasattr(image, 'getexif'):
                exif = image.getexif()
                orientation = exif.get(0x0112)  # 0x0112 is the EXIF orientation tag
            else:
                # Fallback to older method
                exif = image._getexif()
                orientation = None
                if exif is not None:
                    # Find the orientation tag
                    for tag, value in ExifTags.TAGS.items():
                        if value == 'Orientation':
                            orientation = exif.get(tag)
                            break
            
            if orientation:
                # Apply rotation based on orientation value
                if orientation == 2:
                    # Horizontal flip
                    image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                elif orientation == 3:
                    # 180 degree rotation
                    image = image.rotate(180, expand=True)
                elif orientation == 4:
                    # Vertical flip
                    image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                elif orientation == 5:
                    # Horizontal flip + 90 degree rotation
                    image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                    image = image.rotate(90, expand=True)
                elif orientation == 6:
                    # 90 degree rotation (clockwise)
                    image = image.rotate(270, expand=True)
                elif orientation == 7:
                    # Horizontal flip + 270 degree rotation
                    image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                    image = image.rotate(270, expand=True)
                elif orientation == 8:
                    # 270 degree rotation (counter-clockwise)
                    image = image.rotate(90, expand=True)
                        
        except (AttributeError, KeyError, TypeError, OSError):
            # No EXIF data, orientation tag, or other error - return original image
            pass
        
        return image

    def extract_image_file_tags( self, filepath ):
        """Extract keywords/tags from image file metadata (EXIF, IPTC, XMP)"""
        tags = []
        
        try:
            with Image.open( filepath ) as img:
                # Try to get EXIF data
                if hasattr( img, 'getexif' ):
                    exif = img.getexif()
                    
                    # Look for keywords in EXIF data
                    # EXIF tag 0x9286 is UserComment, sometimes contains keywords
                    # EXIF tag 0x010E is ImageDescription, sometimes contains keywords
                    # EXIF tag 0x9C9C is XPKeywords (Windows XP keywords)
                    # EXIF tag 0x9C9D is XPSubject
                    # EXIF tag 0x9C9E is XPComment
                    
                    for tag_id, value in exif.items():
                        tag_name = TAGS.get( tag_id, tag_id )
                        
                        # Check for keywords in various EXIF fields
                        if tag_name in ['ImageDescription', 'UserComment', 'XPKeywords', 'XPSubject', 'XPComment']:
                            if isinstance( value, bytes ):
                                try:
                                    # Try to decode bytes to string
                                    value = value.decode( 'utf-8', errors='ignore' ).strip()
                                except:
                                    continue
                            elif isinstance( value, str ):
                                value = value.strip()
                            else:
                                continue
                            
                            if value:
                                # Split on common separators for keywords
                                keywords = []
                                for separator in [';', ',', '|', '\n', '\r']:
                                    if separator in value:
                                        keywords = [k.strip() for k in value.split( separator )]
                                        break
                                else:
                                    # No separator found, treat as single keyword if reasonable length
                                    if len( value ) < 100:  # Reasonable keyword length
                                        keywords = [value]
                                
                                tags.extend( [k for k in keywords if k and len( k ) > 0] )
                
                # Try to get additional metadata using info dictionary
                if hasattr( img, 'info' ) and img.info:
                    # Look for IPTC keywords
                    if 'keywords' in img.info:
                        keywords = img.info['keywords']
                        if isinstance( keywords, (list, tuple) ):
                            tags.extend( [str( k ).strip() for k in keywords if k] )
                        elif isinstance( keywords, str ):
                            tags.extend( [k.strip() for k in keywords.split( ';' ) if k.strip()] )
                    
                    # Look for other metadata fields that might contain keywords
                    for key in ['subject', 'description', 'comment']:
                        if key in img.info:
                            value = img.info[key]
                            if isinstance( value, str ) and value.strip():
                                # Split on common separators
                                keywords = [k.strip() for k in value.split( ';' ) if k.strip()]
                                tags.extend( keywords )
        
        except Exception as e:
            print( f"Error extracting tags from {filepath}: {e}" )
        
        # Remove duplicates while preserving order
        seen = set()
        unique_tags = []
        for tag in tags:
            tag_lower = tag.lower()
            if tag_lower not in seen:
                seen.add( tag_lower )
                unique_tags.append( tag )
        
        return unique_tags

    def update_file_tags_display( self, filepath=None ):
        """Update the file tags display with tags from the selected image file"""
        # Check if the widget exists (might be called before UI is fully initialized)
        if not hasattr( self, 'file_tags_text' ):
            return
            
        # Clear the text widget
        self.file_tags_text.configure( state=tk.NORMAL )
        self.file_tags_text.delete( 1.0, tk.END )
        
        if filepath and os.path.exists( filepath ):
            try:
                # Extract tags from the image file
                file_tags = self.extract_image_file_tags( filepath )
                
                if file_tags:
                    # Display the tags
                    tags_text = "\n".join( f"• {tag}" for tag in file_tags )
                    self.file_tags_text.insert( tk.END, tags_text )
                else:
                    self.file_tags_text.insert( tk.END, "No tags found in image file" )
                    
            except Exception as e:
                error_msg = f"Error reading file tags:\n{str(e)}"
                self.file_tags_text.insert( tk.END, error_msg )
        else:
            if not filepath:
                self.file_tags_text.insert( tk.END, "No image selected" )
            else:
                self.file_tags_text.insert( tk.END, "Image file not found" )
        
        # Make text widget read-only again
        self.file_tags_text.configure( state=tk.DISABLED )

    def display_image_preview( self, filepath, label_widget ):
        """Display image preview in the specified label widget"""
        # Update the corresponding file path label
        if label_widget == self.browse_preview_label:
            self.browse_path_label.configure( text=filepath )
        elif label_widget == self.database_preview_label:
            self.database_path_label.configure( text=filepath )
            
        try:
            image = Image.open( filepath )
            
            # Apply EXIF orientation correction
            image = self.apply_exif_orientation( image )
            
            # Get the available space in the label widget
            label_widget.update_idletasks()  # Ensure geometry is updated
            available_width = label_widget.winfo_width()
            available_height = label_widget.winfo_height()
            
            # Use a minimum size if the widget hasn't been sized yet
            if available_width <= 1 or available_height <= 1:
                available_width = 400
                available_height = 400
            else:
                # Leave some padding around the image
                available_width -= 20
                available_height -= 20
                
            # Calculate the scale factor to fit the image in the available space
            img_width, img_height = image.size
            scale_width = available_width / img_width
            scale_height = available_height / img_height
            scale_factor = min( scale_width, scale_height )
            
            # Calculate new dimensions
            new_width = int( img_width * scale_factor )
            new_height = int( img_height * scale_factor )
            
            # Resize the image
            resized_image = image.resize( (new_width, new_height), Image.Resampling.LANCZOS )
            
            photo = ImageTk.PhotoImage( resized_image )
            label_widget.configure( image=photo, text="" )
            label_widget.image = photo  # Keep a reference
            label_widget.current_image_path = filepath  # Store the current image path for resize events
            
        except Exception as e:
            label_widget.configure( image="", text=f"Error loading image:\n{str(e)}" )
            label_widget.image = None
            label_widget.current_image_path = None
            # Clear the path label on error
            if label_widget == self.browse_preview_label:
                self.browse_path_label.configure( text="" )
            elif label_widget == self.database_preview_label:
                self.database_path_label.configure( text="" )
            
    def enter_fullscreen_mode( self, filepath ):
        """Enter fullscreen mode for viewing images with lazy loading for large databases"""
        self.previous_tab = self.notebook.index( self.notebook.select() )
        
        # Determine which tab we're in and get appropriate image list
        current_tab = self.notebook.index( self.notebook.select() )
        
        if current_tab == 1 and self.current_database_path:  # Database tab
            # Use lazy loading approach - store filenames instead of resolving all paths
            self.fullscreen_filenames = []
            self.fullscreen_images = []  # Keep for compatibility but will be populated lazily
            self.fullscreen_paths_cache.clear()  # Clear path cache
            
            # Get current filtered filenames from listbox
            for i in range( self.database_image_listbox.size() ):
                self.fullscreen_filenames.append( self.database_image_listbox.get( i ) )
            
            # Find current image index by filename
            current_filename = os.path.basename( filepath ) if filepath else ""
            try:
                self.fullscreen_index = self.fullscreen_filenames.index( current_filename ) if current_filename in self.fullscreen_filenames else 0
            except ValueError:
                # Fallback: add current image as the only item
                self.fullscreen_filenames = [current_filename] if current_filename else []
                self.fullscreen_index = 0
            
            # Cache the current image path
            if current_filename and filepath:
                self.fullscreen_paths_cache[current_filename] = filepath
                
        else:  # Browse tab or fallback
            # Get list of images in the same directory
            directory = os.path.dirname( filepath )
            self.fullscreen_images = []
            
            try:
                for file in sorted( os.listdir( directory ) ):
                    file_path = os.path.join( directory, file )
                    if self.is_image_file( file_path ):
                        self.fullscreen_images.append( file_path )
                        
                self.fullscreen_index = self.fullscreen_images.index( filepath ) if filepath in self.fullscreen_images else 0
            except (ValueError, OSError):
                self.fullscreen_images = [filepath]
                self.fullscreen_index = 0
            
        # Create fullscreen window
        self.fullscreen_window = tk.Toplevel( self.root )
        self.fullscreen_window.title( "Fullscreen View" )
        self.fullscreen_window.state( 'zoomed' )  # Maximize on Windows
        self.fullscreen_window.configure( bg='black' )
        
        # Fullscreen image label
        self.fullscreen_label = tk.Label( self.fullscreen_window, bg='black' )
        self.fullscreen_label.pack( fill=tk.BOTH, expand=True )
        
        # Bind events
        self.fullscreen_window.bind( "<Double-Button-1>", self.exit_fullscreen_mode )
        self.fullscreen_window.bind( "<Button-3>", self.on_fullscreen_right_click )
        self.fullscreen_window.bind( "<MouseWheel>", self.on_fullscreen_scroll )
        
        # Add keyboard navigation
        self.fullscreen_window.bind( "<Key-Up>", self.on_fullscreen_previous )
        self.fullscreen_window.bind( "<Key-Down>", self.on_fullscreen_next )
        self.fullscreen_window.bind( "<Key-space>", self.on_fullscreen_next )
        self.fullscreen_window.bind( "<Key-Left>", self.on_fullscreen_previous )
        self.fullscreen_window.bind( "<Key-Right>", self.on_fullscreen_next )
        self.fullscreen_window.bind( "<Key-Escape>", self.exit_fullscreen_mode )
        
        # Add rating shortcuts to fullscreen mode
        self.fullscreen_window.bind( "<Key-1>", lambda e: self.rate_current_fullscreen_image( 1 ) )
        self.fullscreen_window.bind( "<Key-2>", lambda e: self.rate_current_fullscreen_image( 2 ) )
        self.fullscreen_window.bind( "<Key-3>", lambda e: self.rate_current_fullscreen_image( 3 ) )
        self.fullscreen_window.bind( "<Key-4>", lambda e: self.rate_current_fullscreen_image( 4 ) )
        self.fullscreen_window.bind( "<Key-5>", lambda e: self.rate_current_fullscreen_image( 5 ) )
        self.fullscreen_window.bind( "<Key-6>", lambda e: self.rate_current_fullscreen_image( 6 ) )
        self.fullscreen_window.bind( "<Key-7>", lambda e: self.rate_current_fullscreen_image( 7 ) )
        self.fullscreen_window.bind( "<Key-8>", lambda e: self.rate_current_fullscreen_image( 8 ) )
        self.fullscreen_window.bind( "<Key-9>", lambda e: self.rate_current_fullscreen_image( 9 ) )
        self.fullscreen_window.bind( "<Key-0>", lambda e: self.rate_current_fullscreen_image( 10 ) )
        
        # Use Ctrl+Left/Right for rating adjustment in fullscreen to avoid conflict with navigation
        self.fullscreen_window.bind( "<Control-Key-Left>", lambda e: self.adjust_current_fullscreen_rating( -1 ) )
        self.fullscreen_window.bind( "<Control-Key-Right>", lambda e: self.adjust_current_fullscreen_rating( 1 ) )
        
        self.fullscreen_window.focus_set()
        
        # Display current image
        self.display_fullscreen_image()
        
    def on_fullscreen_previous( self, event ):
        """Navigate to previous image in fullscreen mode"""
        max_images = len( self.fullscreen_filenames ) if self.fullscreen_filenames else len( self.fullscreen_images )
        if max_images and self.fullscreen_index > 0:
            self.fullscreen_index -= 1
            self.display_fullscreen_image()
            
    def on_fullscreen_next( self, event ):
        """Navigate to next image in fullscreen mode"""
        max_images = len( self.fullscreen_filenames ) if self.fullscreen_filenames else len( self.fullscreen_images )
        if max_images and self.fullscreen_index < max_images - 1:
            self.fullscreen_index += 1
            self.display_fullscreen_image()
        
    def display_fullscreen_image( self ):
        """Display the current image in fullscreen mode with lazy loading"""
        # Use lazy loading approach for database images
        filepath = self.get_fullscreen_image_path( self.fullscreen_index )
        
        if not filepath:
            return
        
        try:
            image = Image.open( filepath )
            
            # Apply EXIF orientation correction
            image = self.apply_exif_orientation( image )
            
            # Get screen dimensions
            screen_width = self.fullscreen_window.winfo_screenwidth()
            screen_height = self.fullscreen_window.winfo_screenheight()
            
            # Calculate size to fit screen while maintaining aspect ratio
            image_ratio = image.width / image.height
            screen_ratio = screen_width / screen_height
            
            if image_ratio > screen_ratio:
                # Image is wider than screen ratio
                new_width = screen_width
                new_height = int( screen_width / image_ratio )
            else:
                # Image is taller than screen ratio
                new_height = screen_height
                new_width = int( screen_height * image_ratio )
                
            image = image.resize( (new_width, new_height), Image.Resampling.LANCZOS )
            photo = ImageTk.PhotoImage( image )
            
            self.fullscreen_label.configure( image=photo )
            self.fullscreen_label.image = photo
            
            # Update window title
            filename = os.path.basename( filepath )
            self.fullscreen_window.title( f"Fullscreen View - {filename} ({self.fullscreen_index + 1}/{len(self.fullscreen_images)})" )
            
        except Exception as e:
            self.fullscreen_label.configure( image="", text=f"Error loading image: {str(e)}", fg='white' )
            self.fullscreen_label.image = None
            
    def on_fullscreen_scroll( self, event ):
        """Handle mouse wheel in fullscreen mode with lazy loading"""
        max_images = len( self.fullscreen_filenames ) if self.fullscreen_filenames else len( self.fullscreen_images )
        if not max_images:
            return
            
        if event.delta > 0:
            # Scroll up - previous image (don't wrap)
            if self.fullscreen_index > 0:
                self.fullscreen_index -= 1
                self.display_fullscreen_image()
        else:
            # Scroll down - next image (don't wrap)
            if self.fullscreen_index < max_images - 1:
                self.fullscreen_index += 1
                self.display_fullscreen_image()
        
    def exit_fullscreen_mode( self, event=None ):
        """Exit fullscreen mode and return to previous tab"""
        if self.fullscreen_window:
            self.fullscreen_window.destroy()
            self.fullscreen_window = None
            
        if self.previous_tab is not None:
            self.notebook.select( self.previous_tab )
            
    def on_fullscreen_right_click( self, event ):
        """Handle right click in fullscreen mode"""
        if self.fullscreen_images and self.fullscreen_index < len( self.fullscreen_images ):
            filepath = self.fullscreen_images[self.fullscreen_index]
            self.show_tag_dialog( filepath )
            

        
    def on_tab_changed( self, event ):
        """Handle tab change events"""
        current_tab = self.notebook.index( self.notebook.select() )
        if current_tab == 1:  # Database tab
            self.refresh_database_view()
            # Restore paned positions when switching to database tab
            self.root.after( 100, self.restore_paned_positions )
        
        # Save the active tab state whenever it changes
        self.save_active_tab_only()
            
    def create_database( self ):
        """Create a new database for a selected directory"""
        directory = filedialog.askdirectory( title="Select directory to catalog" )
        if not directory:
            return
            
        # Ask for database name with directory name as default
        default_name = os.path.basename( directory )
        db_name = simpledialog.askstring( "Database Name", "Enter name for the new database:", initialvalue=default_name )
        if not db_name:
            return
            
        if not db_name.endswith( '.db' ):
            db_name += '.db'
            
        db_path = os.path.join( directory, db_name )
        
        try:
            # Create database
            conn = sqlite3.connect( db_path )
            cursor = conn.cursor()
            
            # Create tables
            cursor.execute( '''
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    width INTEGER,
                    height INTEGER,
                    rating INTEGER DEFAULT 0,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''' )
            
            cursor.execute( '''
                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL
                )
            ''' )
            
            cursor.execute( '''
                CREATE TABLE IF NOT EXISTS image_tags (
                    image_id INTEGER,
                    tag_id INTEGER,
                    FOREIGN KEY (image_id) REFERENCES images (id),
                    FOREIGN KEY (tag_id) REFERENCES tags (id),
                    PRIMARY KEY (image_id, tag_id)
                )
            ''' )
            
            # Scan directory for images with progress tracking
            # Close connection - worker thread will create its own
            conn.close()
            
            # Scan directory for images with progress tracking
            # Database state and tab switch will be handled after scan completion
            self.scan_directory_for_images_with_progress( db_path, directory )
            
            # Success message will be shown by the threading completion handler
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to create database: {str(e)}" )
    
    def ensure_database_indexes( self, conn=None ):
        """Ensure database indexes exist for better query performance"""
        try:
            if conn is None:
                if not self.current_database:
                    return
                conn = self.current_database
            
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
            print( "Database indexes ensured for optimal performance" )
            
        except Exception as e:
            print( f"Error creating database indexes: {e}" )
    
    def on_virtual_selection_changed( self, selected_indices ):
        """Handle selection changes in virtual image list"""
        self.selected_image_indices = selected_indices
        
        # Update status label
        self.update_image_list_status()
        
        if selected_indices:
            # Get the first selected item
            if selected_indices[0] < len( self.virtual_image_list.filtered_items ):
                item_data = self.virtual_image_list.filtered_items[selected_indices[0]]
                # Use the filepath directly from item_data instead of looking up by filename
                # This fixes the duplicate filename issue
                filepath = item_data.get( 'filepath' )
                
                if len( selected_indices ) == 1:
                    # Single selection - show preview
                    if filepath and os.path.exists( filepath ):
                        self.current_database_image = filepath
                        self.display_image_preview( filepath, self.database_preview_label )
                        self.selected_image_files = [filepath]
                        self.load_image_tags_for_editing()
                        # Update file tags display for single selection
                        self.update_file_tags_display( filepath )
                else:
                    # Multiple selection - show count
                    filepaths = []
                    for idx in selected_indices:
                        if idx < len( self.virtual_image_list.filtered_items ):
                            item = self.virtual_image_list.filtered_items[idx]
                            # Use filepath directly instead of looking up by filename
                            path = item.get( 'filepath' )
                            if path and os.path.exists( path ):
                                filepaths.append( path )
                    
                    self.selected_image_files = filepaths
                    self.current_database_image = None
                    self.database_preview_label.configure( image="", text=f"{len(selected_indices)} images selected" )
                    self.database_preview_label.image = None
                    self.database_path_label.configure( text="" )
                    self.load_image_tags_for_editing()
                    # Clear file tags display for multiple selection
                    self.update_file_tags_display( None )
        else:
            # No selection
            self.current_database_image = None
            self.selected_image_files = []
            self.database_preview_label.configure( image="", text="No selection" )
            self.database_preview_label.image = None
            self.database_path_label.configure( text="" )
            # Clear file tags display for no selection
            self.update_file_tags_display( None )
    
    def on_virtual_double_click( self, index, event ):
        """Handle double click in virtual image list"""
        if index < len( self.virtual_image_list.filtered_items ):
            item_data = self.virtual_image_list.filtered_items[index]
            filename = item_data['filename']
            filepath = self.find_image_path( filename )
            if filepath:
                self.enter_fullscreen_mode( filepath )
            
    def create_database_here( self ):
        """Create a new database in the currently browsed directory"""
        if not self.current_browse_directory:
            messagebox.showwarning( "Warning", "No directory is currently selected in the browse tab" )
            return
            
        directory = self.current_browse_directory
        
        # Ask for database name with directory name as default
        default_name = os.path.basename( directory )
        db_name = simpledialog.askstring( "Database Name", f"Enter name for the new database in:\n{directory}", initialvalue=default_name )
        if not db_name:
            return
            
        if not db_name.endswith( '.db' ):
            db_name += '.db'
            
        db_path = os.path.join( directory, db_name )
        
        # Check if database already exists
        if os.path.exists( db_path ):
            if not messagebox.askyesno( "Database Exists", f"Database {db_name} already exists in this directory. Overwrite?" ):
                return
                
        try:
            # Create database
            conn = sqlite3.connect( db_path )
            cursor = conn.cursor()
            
            # Create tables
            cursor.execute( '''
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    width INTEGER,
                    height INTEGER,
                    rating INTEGER DEFAULT 0,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''' )
            
            cursor.execute( '''
                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL
                )
            ''' )
            
            cursor.execute( '''
                CREATE TABLE IF NOT EXISTS image_tags (
                    image_id INTEGER,
                    tag_id INTEGER,
                    FOREIGN KEY (image_id) REFERENCES images (id),
                    FOREIGN KEY (tag_id) REFERENCES tags (id),
                    PRIMARY KEY (image_id, tag_id)
                )
            ''' )
            
            # Scan directory for images with progress tracking
            # Close connection - worker thread will create its own
            conn.close()
            
            # Scan directory for images with progress tracking
            # Database state and tab switch will be handled after scan completion
            self.scan_directory_for_images_with_progress( db_path, directory )
            
            # Success message will be shown by the threading completion handler
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to create database: {str(e)}" )
            
            
    def create_database_in_directory( self, directory_path ):
        """Create a database in the specified directory"""
        # Ask for database name with directory name as default
        default_name = os.path.basename( directory_path )
        db_name = simpledialog.askstring( "Database Name", f"Enter name for the new database in:\n{directory_path}", initialvalue=default_name )
        if not db_name:
            return
            
        if not db_name.endswith( '.db' ):
            db_name += '.db'
            
        db_path = os.path.join( directory_path, db_name )
        
        # Check if database already exists
        if os.path.exists( db_path ):
            if not messagebox.askyesno( "Database Exists", f"Database {db_name} already exists in this directory. Overwrite?" ):
                return
                
        try:
            # Create database
            conn = sqlite3.connect( db_path )
            cursor = conn.cursor()
            
            # Create tables
            cursor.execute( '''
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    width INTEGER,
                    height INTEGER,
                    rating INTEGER DEFAULT 0,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''' )
            
            cursor.execute( '''
                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL
                )
            ''' )
            
            cursor.execute( '''
                CREATE TABLE IF NOT EXISTS image_tags (
                    image_id INTEGER,
                    tag_id INTEGER,
                    FOREIGN KEY (image_id) REFERENCES images (id),
                    FOREIGN KEY (tag_id) REFERENCES tags (id),
                    PRIMARY KEY (image_id, tag_id)
                )
            ''' )
            
            # Scan directory for images with progress tracking
            # Close connection - worker thread will create its own
            conn.close()
            
            # Scan directory for images with progress tracking
            # Database state and tab switch will be handled after scan completion
            self.scan_directory_for_images_with_progress( db_path, directory_path )
            
            # Success message will be shown by the threading completion handler
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to create database: {str(e)}" )
            
    def scan_directory_for_images( self, cursor, directory ):
        """Scan directory recursively for image files and add to database"""
        for root, dirs, files in os.walk( directory ):
            for file in files:
                filepath = os.path.join( root, file )
                if self.is_image_file( filepath ):
                    try:
                        # Get image dimensions
                        with Image.open( filepath ) as img:
                            width, height = img.size
                            
                        # Calculate relative path
                        relative_path = os.path.relpath( filepath, directory )
                        
                        # Insert into database
                        cursor.execute( '''
                            INSERT INTO images (filename, relative_path, width, height)
                            VALUES (?, ?, ?, ?)
                        ''', (file, relative_path, width, height) )
                        
                    except Exception as e:
                        print( f"Error processing {filepath}: {e}" )
                        
    def scan_directory_for_images_with_progress( self, db_path, directory ):
        """Scan directory recursively for image files and add to database with progress reporting"""
        # Create progress dialog in main thread
        progress_dialog = self.create_progress_dialog( "Creating Database", "Scanning directory..." )
        
        # Make sure the dialog is visible and on top
        progress_dialog['window'].lift()
        progress_dialog['window'].attributes('-topmost', True)
        progress_dialog['window'].focus_force()
        self.root.update()
        
        # Create thread-safe data container
        thread_data = {
            'db_path': db_path,
            'directory': directory,
            'progress_dialog': progress_dialog,
            'exception': None,
            'completed': False
        }
        
        # Start background thread for scanning
        scan_thread = threading.Thread( target=self._scan_worker_thread, args=(thread_data,), daemon=True )
        scan_thread.start()
        
        # Start asynchronous monitoring from main thread
        self._schedule_progress_monitoring( thread_data, scan_thread )
    
    def _scan_worker_thread( self, thread_data ):
        """Worker thread for scanning directory and processing images"""
        conn = None
        try:
            db_path = thread_data['db_path']
            directory = thread_data['directory']
            
            # Create database connection in worker thread
            conn = sqlite3.connect( db_path )
            cursor = conn.cursor()
            
            # First pass: count total files for progress calculation
            total_files = 0
            all_image_files = []
            
            # Count image files
            for root, dirs, files in os.walk( directory ):
                for file in files:
                    filepath = os.path.join( root, file )
                    if self.is_image_file( filepath ):
                        all_image_files.append( filepath )
                        total_files += 1
                        
            # Update progress dialog from worker thread (thread-safe)
            thread_data['total_files'] = total_files
            thread_data['current_files'] = all_image_files
            thread_data['phase'] = 'processing'
            
            # Second pass: process files with batch inserts for better performance
            processed = 0
            successful = 0
            batch_size = 200  # Process images in batches for better performance
            batch_data = []
            
            for filepath in all_image_files:
                # Check for cancellation
                if thread_data['progress_dialog'].get( 'cancelled', False ):
                    thread_data['exception'] = Exception( "Operation cancelled by user" )
                    return
                    
                try:
                    # Get image dimensions
                    with Image.open( filepath ) as img:
                        width, height = img.size
                        
                    # Calculate relative path
                    relative_path = os.path.relpath( filepath, directory )
                    filename = os.path.basename( filepath )
                    
                    # Add to batch
                    batch_data.append( (filename, relative_path, width, height) )
                    successful += 1
                    
                except Exception as e:
                    print( f"Error processing {filepath}: {e}" )
                    
                processed += 1
                
                # Process batch when it reaches batch_size or at the end
                if len( batch_data ) >= batch_size or processed == total_files:
                    if batch_data:
                        # Batch insert for better performance
                        cursor.executemany( '''
                            INSERT INTO images (filename, relative_path, width, height)
                            VALUES (?, ?, ?, ?)
                        ''', batch_data )
                        
                        # Commit batch
                        conn.commit()
                        
                        # Clear batch
                        batch_data = []
                
                # Update progress
                thread_data['processed'] = processed
                thread_data['successful'] = successful
                    
        except Exception as e:
            thread_data['exception'] = e
        finally:
            if conn:
                conn.close()
            thread_data['completed'] = True
            print( f"Worker thread completed. Processed: {processed}, Successful: {successful}, Total: {total_files}" )
    
    def _schedule_progress_monitoring( self, thread_data, scan_thread ):
        """Start asynchronous monitoring of background thread progress"""
        # Schedule the first progress check
        self.root.after( 100, lambda: self._check_progress( thread_data, scan_thread ) )
    
    def _check_progress( self, thread_data, scan_thread ):
        """Check progress and schedule next update - non-blocking"""
        try:
            # Update progress dialog based on thread data
            if 'total_files' in thread_data:
                total_files = thread_data['total_files']
                processed = thread_data.get( 'processed', 0 )
                
                if thread_data.get( 'phase' ) == 'processing':
                    self.update_progress_dialog( 
                        thread_data['progress_dialog'], 
                        processed, 
                        total_files, 
                        f"Processed {processed}/{total_files} images" 
                    )
                else:
                    self.update_progress_dialog( 
                        thread_data['progress_dialog'], 
                        0, 
                        total_files, 
                        "Processing images..." 
                    )
            
            # Check if thread is still running and not cancelled
            if scan_thread.is_alive() and not thread_data['progress_dialog'].get( 'cancelled', False ):
                # Schedule next progress check
                self.root.after( 100, lambda: self._check_progress( thread_data, scan_thread ) )
            elif thread_data.get( 'completed', False ) or thread_data['progress_dialog'].get( 'cancelled', False ):
                # Thread completed or was cancelled - do one final progress update then finalize
                if thread_data.get( 'completed', False ) and 'total_files' in thread_data:
                    # Show final progress update
                    total_files = thread_data['total_files']
                    processed = thread_data.get( 'processed', 0 )
                    self.update_progress_dialog( 
                        thread_data['progress_dialog'], 
                        processed, 
                        total_files, 
                        f"Completed {processed}/{total_files} images" 
                    )
                # Finalize after a brief delay to show completion
                self.root.after( 200, lambda: self._finalize_scan_operation( thread_data, scan_thread ) )
                
        except Exception as e:
            print( f"Error in progress monitoring: {e}" )
            self._finalize_scan_operation( thread_data, scan_thread )
    
    def _finalize_scan_operation( self, thread_data, scan_thread ):
        """Finalize the scan operation - close dialog and show completion"""
        try:
            # Wait for thread to complete properly
            if scan_thread.is_alive():
                scan_thread.join( timeout=2.0 )  # Give more time for completion
            
            # Make sure progress shows 100% completion before closing
            if 'total_files' in thread_data and 'processed' in thread_data:
                total_files = thread_data['total_files']
                processed = thread_data['processed']
                self.update_progress_dialog( 
                    thread_data['progress_dialog'], 
                    processed, 
                    total_files, 
                    f"Completed {processed}/{total_files} images" 
                )
                # Give a moment for the user to see 100% completion
                self.root.after( 500, lambda: self._complete_finalization( thread_data, scan_thread ) )
            else:
                self._complete_finalization( thread_data, scan_thread )
                                   
        except Exception as e:
            print( f"Error finalizing scan operation: {e}" )
            self._complete_finalization( thread_data, scan_thread )
    
    def _complete_finalization( self, thread_data, scan_thread ):
        """Complete the finalization after showing 100% progress"""
        try:
            # Close progress dialog
            self.close_progress_dialog( thread_data['progress_dialog'] )
            
            # Handle any exceptions that occurred in the worker thread
            if thread_data.get( 'exception' ):
                if "cancelled" not in str( thread_data['exception'] ).lower():
                    messagebox.showerror( "Database Creation Error", str( thread_data['exception'] ) )
                return
            
            # Show completion message only if operation completed successfully AND thread is actually done
            if (not thread_data['progress_dialog'].get( 'cancelled', False ) and 
                thread_data.get( 'completed', False ) and 
                not scan_thread.is_alive()):
                
                total_files = thread_data.get( 'total_files', 0 )
                processed = thread_data.get( 'processed', 0 )
                successful = thread_data.get( 'successful', 0 )
                failed = processed - successful
                
                # Open the newly created database
                db_path = thread_data['db_path']
                directory = thread_data['directory']
                
                self.current_database_path = db_path
                self.current_database = directory
                self.notebook.select( 1 )  # Switch to Database tab (this will call refresh_database_view via on_tab_changed)
                
                # Save the database state and update recent databases
                self.save_paned_positions_only()
                self.root.after(100, self.update_recent_databases_dropdown)
                
                message = f"Database creation completed!\n\n"
                message += f"Files scanned: {total_files}\n"
                message += f"Successfully added: {successful}\n"
                if failed > 0:
                    message += f"Failed/Skipped: {failed}\n"
                    message += f"(Corrupted images, videos, or unsupported formats)"
                
                messagebox.showinfo( "Database Creation Complete", message )
                                   
        except Exception as e:
            print( f"Error completing finalization: {e}" )
    
    def create_progress_dialog( self, title, message ):
        """Create a progress dialog window"""
        progress_window = tk.Toplevel( self.root )
        progress_window.title( title )
        progress_window.geometry( "400x150" )
        progress_window.resizable( False, False )
        progress_window.transient( self.root )
        progress_window.grab_set()
        
        # Center the dialog
        progress_window.geometry( "+%d+%d" % (
            self.root.winfo_rootx() + 50,
            self.root.winfo_rooty() + 50
        ))
        
        # Message label
        message_label = ttk.Label( progress_window, text=message )
        message_label.pack( pady=10 )
        
        # Progress bar
        progress_var = tk.DoubleVar()
        progress_bar = ttk.Progressbar( progress_window, variable=progress_var, maximum=100 )
        progress_bar.pack( pady=10, padx=20, fill=tk.X )
        
        # Status label
        status_label = ttk.Label( progress_window, text="Initializing..." )
        status_label.pack( pady=5 )
        
        # Cancel button
        cancel_button = ttk.Button( progress_window, text="Cancel" )
        cancel_button.pack( pady=10 )
        
        # Store dialog components
        dialog_data = {
            'window': progress_window,
            'message_label': message_label,
            'progress_var': progress_var,
            'progress_bar': progress_bar,
            'status_label': status_label,
            'cancel_button': cancel_button,
            'cancelled': False,
            'total': 0
        }
        
        # Bind cancel button
        cancel_button.configure( command=lambda: self.cancel_progress_dialog( dialog_data ) )
        
        return dialog_data
    
    def update_progress_dialog( self, dialog_data, current, total, status_text ):
        """Update progress dialog with current status"""
        if dialog_data['cancelled']:
            return
            
        try:
            # Update progress bar
            if total > 0:
                percentage = (current / total) * 100
                dialog_data['progress_var'].set( percentage )
            
            # Update status text
            dialog_data['status_label'].configure( text=status_text )
            
            # Force GUI update
            dialog_data['window'].update()
            
        except tk.TclError:
            # Dialog was closed
            pass
    
    def cancel_progress_dialog( self, dialog_data ):
        """Mark progress dialog as cancelled"""
        dialog_data['cancelled'] = True
        dialog_data['status_label'].configure( text="Cancelling..." )
        dialog_data['cancel_button'].configure( state='disabled' )
        dialog_data['window'].update()
    
    def close_progress_dialog( self, dialog_data ):
        """Close the progress dialog"""
        try:
            dialog_data['window'].destroy()
        except tk.TclError:
            # Dialog already closed
            pass
                        
    def open_database( self ):
        """Open an existing database file"""
        db_path = filedialog.askopenfilename( 
            title="Select database file",
            filetypes=[("Database files", "*.db"), ("All files", "*.*")]
        )
        
        if not db_path:
            return
        
        self.open_database_file( db_path )
    
    def open_database_file( self, db_path ):
        """Open a specific database file"""
        try:
            # Test database connection
            conn = sqlite3.connect( db_path )
            cursor = conn.cursor()
            cursor.execute( "SELECT name FROM sqlite_master WHERE type='table'" )
            tables = cursor.fetchall()
            conn.close()
            
            required_tables = {'images', 'tags', 'image_tags'}
            existing_tables = {table[0] for table in tables}
            
            if not required_tables.issubset( existing_tables ):
                messagebox.showerror( "Error", "Invalid database file - missing required tables" )
                return
            
            # Ensure database indexes exist for better performance
            conn = sqlite3.connect( db_path )
            self.ensure_database_indexes( conn )
            conn.close()
                
            self.current_database_path = db_path
            self.current_database = os.path.dirname( db_path )
            self.notebook.select( 1 )  # Switch to Database tab
            
            # Always refresh database view (in case tab was already selected)
            self.refresh_database_view()
            
            # Save the database state immediately
            self.save_paned_positions_only()
            # Small delay to ensure settings are written
            self.root.after(100, self.update_recent_databases_dropdown)
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to open database: {str(e)}" )
            
    def rescan_database( self ):
        """Rescan the database directory for new/removed images"""
        if not self.current_database_path:
            messagebox.showwarning( "Warning", "No database is currently open" )
            return
            
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            # Get current images in database
            cursor.execute( "SELECT id, relative_path FROM images" )
            db_images = {row[1]: row[0] for row in cursor.fetchall()}
            
            # Scan directory for current images and collect new ones for batch processing
            current_images = set()
            new_images_batch = []
            images_to_delete = []
            
            for root, dirs, files in os.walk( self.current_database ):
                for file in files:
                    filepath = os.path.join( root, file )
                    if self.is_image_file( filepath ):
                        relative_path = os.path.relpath( filepath, self.current_database )
                        current_images.add( relative_path )
                        
                        # Collect new images for batch processing
                        if relative_path not in db_images:
                            try:
                                with Image.open( filepath ) as img:
                                    width, height = img.size
                                    
                                new_images_batch.append( (file, relative_path, width, height) )
                            except Exception as e:
                                print( f"Error processing {filepath}: {e}" )
                                
            # Batch insert new images
            if new_images_batch:
                cursor.executemany( '''
                    INSERT INTO images (filename, relative_path, width, height)
                    VALUES (?, ?, ?, ?)
                ''', new_images_batch )
                                
            # Collect images to delete for batch processing
            for relative_path, image_id in db_images.items():
                if relative_path not in current_images:
                    images_to_delete.append( (image_id,) )
                    
            # Batch delete images that no longer exist
            if images_to_delete:
                # Delete associated tags first
                cursor.executemany( "DELETE FROM image_tags WHERE image_id = ?", images_to_delete )
                # Then delete images
                cursor.executemany( "DELETE FROM images WHERE id = ?", images_to_delete )
                    
            conn.commit()
            conn.close()
            
            self.refresh_database_view()
            
            messagebox.showinfo( "Success", "Database rescan completed successfully" )
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to rescan database: {str(e)}" )
            
    def remove_database_duplicates( self ):
        """Scan for and remove duplicate database entries pointing to the same file"""
        if not self.current_database_path:
            messagebox.showwarning( "Warning", "No database is currently open" )
            return
        
        try:
            # Create progress dialog
            progress_window = tk.Toplevel( self.root )
            progress_window.title( "Scanning for Duplicates" )
            progress_window.geometry( "400x150" )
            progress_window.transient( self.root )
            progress_window.grab_set()
            progress_window.resizable( False, False )
            
            # Center the progress window
            progress_window.geometry( "+%d+%d" % (self.root.winfo_rootx() + 100, self.root.winfo_rooty() + 100) )
            
            # Progress widgets
            progress_label = ttk.Label( progress_window, text="Scanning database for duplicate entries..." )
            progress_label.pack( pady=10 )
            
            progress_bar = ttk.Progressbar( progress_window, mode='indeterminate' )
            progress_bar.pack( padx=20, pady=10, fill=tk.X )
            progress_bar.start()
            
            status_label = ttk.Label( progress_window, text="Please wait..." )
            status_label.pack( pady=5 )
            
            # Process in background thread
            import threading
            
            def scan_duplicates():
                try:
                    conn = sqlite3.connect( self.current_database_path )
                    cursor = conn.cursor()
                    
                    # Find duplicates by relative_path
                    cursor.execute( """
                        SELECT relative_path, COUNT(*) as count, GROUP_CONCAT(id) as ids
                        FROM images 
                        GROUP BY relative_path 
                        HAVING COUNT(*) > 1
                        ORDER BY relative_path
                    """ )
                    duplicates = cursor.fetchall()
                    
                    conn.close()
                    
                    # Update UI on main thread
                    self.root.after( 0, lambda: self.show_duplicate_results( progress_window, duplicates ) )
                    
                except Exception as e:
                    # Show error on main thread
                    self.root.after( 0, lambda: self.show_duplicate_error( progress_window, str(e) ) )
            
            # Start scanning
            thread = threading.Thread( target=scan_duplicates, daemon=True )
            thread.start()
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to start duplicate scan: {str(e)}" )
    
    def show_duplicate_results( self, progress_window, duplicates ):
        """Show the results of the duplicate scan"""
        try:
            progress_window.destroy()
            
            if not duplicates:
                messagebox.showinfo( "No Duplicates Found", "No duplicate entries were found in the database." )
                return
            
            # Calculate total duplicates to remove (keep the highest ID for each file)
            total_duplicates = sum( count - 1 for _, count, _ in duplicates )
            
            # Show confirmation dialog
            message = f"Found {len(duplicates)} files with duplicate entries.\n"
            message += f"Total duplicate entries to remove: {total_duplicates}\n\n"
            message += "This will keep the most recent entry (highest ID) for each file.\n"
            message += "No actual image files will be deleted.\n\n"
            message += "Do you want to remove the duplicate database entries?"
            
            if messagebox.askyesno( "Remove Duplicates?", message ):
                self.delete_duplicate_entries( duplicates )
        
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to show duplicate results: {str(e)}" )
    
    def show_duplicate_error( self, progress_window, error_msg ):
        """Show error from duplicate scan"""
        progress_window.destroy()
        messagebox.showerror( "Error", f"Failed to scan for duplicates: {error_msg}" )
    
    def delete_duplicate_entries( self, duplicates ):
        """Delete the duplicate entries from the database"""
        try:
            # Create progress dialog for deletion
            progress_window = tk.Toplevel( self.root )
            progress_window.title( "Removing Duplicates" )
            progress_window.geometry( "400x150" )
            progress_window.transient( self.root )
            progress_window.grab_set()
            progress_window.resizable( False, False )
            
            # Center the progress window
            progress_window.geometry( "+%d+%d" % (self.root.winfo_rootx() + 100, self.root.winfo_rooty() + 100) )
            
            # Progress widgets
            progress_label = ttk.Label( progress_window, text="Removing duplicate entries..." )
            progress_label.pack( pady=10 )
            
            progress_bar = ttk.Progressbar( progress_window, maximum=len(duplicates), value=0 )
            progress_bar.pack( padx=20, pady=10, fill=tk.X )
            
            status_label = ttk.Label( progress_window, text="Processing..." )
            status_label.pack( pady=5 )
            
            # Process deletion in background
            import threading
            
            def delete_duplicates():
                try:
                    conn = sqlite3.connect( self.current_database_path )
                    cursor = conn.cursor()
                    
                    total_deleted = 0
                    
                    for i, (relative_path, count, ids_str) in enumerate( duplicates ):
                        # Parse the IDs and sort them
                        ids = [int(id_str) for id_str in ids_str.split(',')]
                        ids.sort()
                        
                        # Keep the highest ID, delete the rest
                        ids_to_delete = ids[:-1]  # All except the last (highest)
                        
                        # Delete the duplicate entries and their associated tags
                        for id_to_delete in ids_to_delete:
                            # Delete associated image_tags first (foreign key constraint)
                            cursor.execute( "DELETE FROM image_tags WHERE image_id = ?", (id_to_delete,) )
                            
                            # Delete the image entry
                            cursor.execute( "DELETE FROM images WHERE id = ?", (id_to_delete,) )
                        
                        total_deleted += len( ids_to_delete )
                        
                        # Update progress on main thread
                        progress = i + 1
                        self.root.after( 0, lambda p=progress, path=relative_path: self.update_deletion_progress( 
                            progress_bar, status_label, p, len(duplicates), path ) )
                    
                    conn.commit()
                    conn.close()
                    
                    # Show completion on main thread
                    self.root.after( 0, lambda: self.show_deletion_complete( progress_window, total_deleted ) )
                    
                except Exception as e:
                    # Show error on main thread
                    self.root.after( 0, lambda: self.show_deletion_error( progress_window, str(e) ) )
            
            # Start deletion
            thread = threading.Thread( target=delete_duplicates, daemon=True )
            thread.start()
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to start duplicate removal: {str(e)}" )
    
    def update_deletion_progress( self, progress_bar, status_label, current, total, current_path ):
        """Update the deletion progress display"""
        progress_bar['value'] = current
        status_label.configure( text=f"Processing {current}/{total}: {os.path.basename(current_path)}" )
    
    def show_deletion_complete( self, progress_window, total_deleted ):
        """Show completion of duplicate deletion"""
        progress_window.destroy()
        
        message = f"Successfully removed {total_deleted} duplicate entries from the database.\n\n"
        message += "The database view will be refreshed to reflect the changes."
        
        messagebox.showinfo( "Duplicates Removed", message )
        
        # Refresh the database view
        self.refresh_database_view()
        self.refresh_filtered_images()
    
    def show_deletion_error( self, progress_window, error_msg ):
        """Show error from duplicate deletion"""
        progress_window.destroy()
        messagebox.showerror( "Error", f"Failed to remove duplicates: {error_msg}" )
            
    def refresh_database_view( self ):
        """Refresh the database tab view"""
        if not self.current_database_path:
            # Clear the view when no database is open
            self.database_name_label.configure( text="No database open" )
            self.clear_image_list()
            self.database_preview_label.configure( image="", text="No database open" )
            self.database_preview_label.image = None
            self.database_path_label.configure( text="" )
            self.clear_image_tag_interface()
            return
            
        try:
            # Clear cache when refreshing database view
            self.clear_cache()
            
            # Update database name label
            db_name = os.path.basename( self.current_database_path )
            db_directory = os.path.basename( self.current_database )
            self.database_name_label.configure( text=f"Database: {db_name} (in {db_directory})" )
            
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            # Load only tags that are actually used by files in the database
            cursor.execute( """
                SELECT DISTINCT t.name 
                FROM tags t 
                INNER JOIN image_tags it ON t.id = it.tag_id 
                INNER JOIN images i ON it.image_id = i.id 
                ORDER BY t.name
            """ )
            tags = [row[0] for row in cursor.fetchall()]
            
            # Clear existing tag checkboxes
            for widget in self.tag_scrollable_frame.winfo_children():
                widget.destroy()
            self.tag_checkboxes.clear()
            
            # Create "all" pseudo tag row
            row = 0
            all_include_or_cb = tk.Checkbutton( self.tag_scrollable_frame, variable=self.all_include_or_var, command=self.on_all_include_or_changed )
            all_include_or_cb.grid( row=row, column=0, sticky="w", padx=5, pady=1 )
            
            all_include_and_cb = tk.Checkbutton( self.tag_scrollable_frame, variable=self.all_include_and_var, command=self.on_all_include_and_changed )
            all_include_and_cb.grid( row=row, column=1, sticky="w", padx=5, pady=1 )
            
            all_exclude_cb = tk.Checkbutton( self.tag_scrollable_frame, variable=self.all_exclude_var, command=self.on_all_exclude_changed )
            all_exclude_cb.grid( row=row, column=2, sticky="w", padx=5, pady=1 )
            
            ttk.Label( self.tag_scrollable_frame, text="all", font=('TkDefaultFont', 9, 'italic') ).grid( row=row, column=3, sticky="w", padx=5, pady=1 )
            
            # Create checkbox rows for each tag
            for i, tag in enumerate( tags, start=1 ):
                # Create variables for this tag
                include_or_var = tk.BooleanVar()
                include_and_var = tk.BooleanVar()
                exclude_var = tk.BooleanVar()
                
                # Create checkboxes directly in scrollable frame
                include_or_cb = tk.Checkbutton( self.tag_scrollable_frame, variable=include_or_var, command=lambda t=tag: self.on_tag_include_or_changed( t ) )
                include_or_cb.grid( row=i, column=0, sticky="w", padx=5, pady=1 )
                
                include_and_cb = tk.Checkbutton( self.tag_scrollable_frame, variable=include_and_var, command=lambda t=tag: self.on_tag_include_and_changed( t ) )
                include_and_cb.grid( row=i, column=1, sticky="w", padx=5, pady=1 )
                
                exclude_cb = tk.Checkbutton( self.tag_scrollable_frame, variable=exclude_var, command=lambda t=tag: self.on_tag_exclude_changed( t ) )
                exclude_cb.grid( row=i, column=2, sticky="w", padx=5, pady=1 )
                
                ttk.Label( self.tag_scrollable_frame, text=tag ).grid( row=i, column=3, sticky="w", padx=5, pady=1 )
                
                # Store checkbox variables
                self.tag_checkboxes[tag] = {
                    'include_or_var': include_or_var,
                    'include_and_var': include_and_var,
                    'exclude_var': exclude_var,
                    'include_or_cb': include_or_cb,
                    'include_and_cb': include_and_cb,
                    'exclude_cb': exclude_cb
                }
                
            # Always ensure images are visible when database is first opened
            # Clear any existing tag filters first
            self.included_or_tags.clear()
            self.included_and_tags.clear()
            self.excluded_tags.clear()
            
            # Reset all checkbox states
            self.all_include_or_var.set( False )
            self.all_include_and_var.set( False )
            self.all_exclude_var.set( False )
            
            # Refresh to show all images (no filters = show all)
            self.refresh_filtered_images()
            
            conn.close()
            
        except Exception as e:
            print( f"Error refreshing database view: {e}" )
            
    def refresh_filtered_images( self, preserve_selection=None ):
        """Refresh the filtered image list based on current tag filters"""
        if not self.current_database_path:
            return
        
        # Store current selection if not provided
        if preserve_selection is None:
            preserve_selection = []
            if hasattr( self, 'virtual_image_list' ) and self.virtual_image_list:
                # Get current selection from virtual list
                for index in sorted( self.virtual_image_list.selected_indices ):
                    if index < len( self.virtual_image_list.filtered_items ):
                        filename = self.virtual_image_list.filtered_items[index]['filename']
                        preserve_selection.append( filename )
            
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            # Get rating filter values
            min_rating = self.min_rating_var.get()
            max_rating = self.max_rating_var.get()
            
            # Build complex query for OR/AND/EXCLUDE logic plus rating filter
            has_tag_filters = self.included_or_tags or self.included_and_tags or self.excluded_tags
            has_rating_filter = min_rating > 0 or max_rating < 10
            
            if not has_tag_filters and not has_rating_filter:
                # No filters - show all images
                query = "SELECT DISTINCT i.relative_path, i.filename FROM images i ORDER BY i.filename"
                params = []

            else:
                # Start with all images
                query = "SELECT DISTINCT i.relative_path, i.filename FROM images i WHERE 1=1"
                params = []
                
                # Apply rating filter
                if has_rating_filter:
                    query += " AND i.rating >= ? AND i.rating <= ?"
                    params.extend( [min_rating, max_rating] )
                
                # Apply EXCLUDE filter (highest priority - exclude any image with excluded tags)
                if self.excluded_tags:
                    placeholders = ','.join( ['?'] * len( self.excluded_tags ) )
                    query += f" AND i.id NOT IN (SELECT it.image_id FROM image_tags it JOIN tags t ON it.tag_id = t.id WHERE t.name IN ({placeholders}))"
                    params.extend( self.excluded_tags )
                
                # Apply OR and AND logic
                include_conditions = []
                
                # Include (OR) - images that have ANY of these tags
                if self.included_or_tags:
                    placeholders = ','.join( ['?'] * len( self.included_or_tags ) )
                    include_conditions.append( f"i.id IN (SELECT it.image_id FROM image_tags it JOIN tags t ON it.tag_id = t.id WHERE t.name IN ({placeholders}))" )
                    params.extend( self.included_or_tags )
                
                # Include (AND) - images that have ALL of these tags
                if self.included_and_tags:
                    and_condition = f"i.id IN (SELECT it.image_id FROM image_tags it JOIN tags t ON it.tag_id = t.id WHERE t.name IN ({','.join(['?'] * len(self.included_and_tags))}) GROUP BY it.image_id HAVING COUNT(DISTINCT t.name) = ?)"
                    include_conditions.append( and_condition )
                    params.extend( self.included_and_tags )
                    params.append( len( self.included_and_tags ) )
                
                # Combine OR and AND conditions
                if include_conditions:
                    query += " AND (" + " OR ".join( include_conditions ) + ")"
                
                query += " ORDER BY i.filename"
            
            cursor.execute( query, params )
            images = cursor.fetchall()
            
            # Clear and populate the virtual image list
            self.clear_image_list()
            
            # Create item data for virtual scrolling
            virtual_items = []
            for relative_path, filename in images:
                filepath = os.path.join( self.current_database, relative_path ) if relative_path else None
                virtual_items.append( {
                    'filename': filename,
                    'filepath': filepath,
                    'show_thumbnails': self.show_thumbnails.get()
                } )
            
            # Set items in virtual list
            self.virtual_image_list.set_items( virtual_items )
            
            # Update status label
            self.update_image_list_status()
            
            # Handle selection restoration
            filtered_filenames = [filename for relative_path, filename in images]
            
            # If we have a preserved selection, try to restore it
            if preserve_selection and filtered_filenames:
                restored_indices = []
                
                for filename in preserve_selection:
                    if filename in filtered_filenames:
                        try:
                            index = filtered_filenames.index( filename )
                            restored_indices.append( index )
                        except ValueError:
                            pass
                if restored_indices:
                    # Restore selection in virtual list
                    self.virtual_image_list.selected_indices = set( restored_indices )
                    self.virtual_image_list.update_selection_display()
                    
                    # For TreeviewImageList, also update the actual treeview selection
                    if hasattr( self.virtual_image_list, 'treeview' ):
                        # Clear current treeview selection
                        self.virtual_image_list.treeview.selection_remove(
                            self.virtual_image_list.treeview.selection()
                        )
                        # Set new treeview selection
                        for index in restored_indices:
                            if 0 <= index < len( self.virtual_image_list.filtered_items ):
                                item_id = str( index )
                                self.virtual_image_list.treeview.selection_add( item_id )
                    # Trigger selection callback
                    self.on_virtual_selection_changed( restored_indices )
                else:
                    # None of the preserved selection is in filtered list - select first
                    if filtered_filenames:
                        self.virtual_image_list.selected_indices = {0}
                        self.virtual_image_list.update_selection_display()
                        
                        # For TreeviewImageList, also update the actual treeview selection
                        if hasattr( self.virtual_image_list, 'treeview' ):
                            self.virtual_image_list.treeview.selection_remove(
                                self.virtual_image_list.treeview.selection()
                            )
                            self.virtual_image_list.treeview.selection_add( "0" )
                        
                        self.on_virtual_selection_changed( [0] )
            elif filtered_filenames and not preserve_selection:
                # No preserved selection - use smart preview logic
                current_filename = None
                if self.current_database_image:
                    current_filename = os.path.basename( self.current_database_image )
                    
                if current_filename and current_filename in filtered_filenames:
                    # Current image is still in filtered list - select it
                    try:
                        current_index = filtered_filenames.index( current_filename )
                        self.virtual_image_list.selected_indices = {current_index}
                        self.virtual_image_list.update_selection_display()
                        self.on_virtual_selection_changed( [current_index] )
                    except ValueError:
                        pass
                else:
                    # Current image is not in filtered list - select first image
                    if filtered_filenames:
                        self.virtual_image_list.selected_indices = {0}
                        self.virtual_image_list.update_selection_display()
                        self.on_virtual_selection_changed( [0] )
            
            if not filtered_filenames:
                # No images in filtered list - clear preview
                self.current_database_image = None
                self.database_preview_label.configure( image="", text="No images match filters" )
                self.database_preview_label.image = None
                self.database_path_label.configure( text="" )
                self.selected_image_files = []
                self.clear_image_tag_interface()
                
            conn.close()
            
            # Start continuous visibility checking for thumbnails
            if self.show_thumbnails.get():
                self.start_visibility_checking()
            
            # Update status label to reflect current filter results
            self.update_image_list_status()
            
        except Exception as e:
            print( f"Error refreshing filtered images: {e}" )
            
    def on_database_image_select( self, event ):
        """Handle selection in database image list"""
        selection = self.database_image_listbox.curselection()
        if selection and self.current_database:
            if len( selection ) == 1:
                # Single selection - show preview and load tags for editing
                index = selection[0]
                filename = self.database_image_listbox.get( index )
                
                # Find full path
                filepath = self.find_image_path( filename )
                if filepath:
                    self.current_database_image = filepath
                    self.display_image_preview( filepath, self.database_preview_label )
                    self.selected_image_files = [filepath]
                    self.load_image_tags_for_editing()
                    # Update file tags display for single selection (after loading tags)
                    self.update_file_tags_display( filepath )
            else:
                # Multiple selection - load tags for bulk editing
                filenames = [self.database_image_listbox.get( i ) for i in selection]
                filepaths = [self.find_image_path( f ) for f in filenames]
                self.selected_image_files = [f for f in filepaths if f]  # Remove None values
                self.current_database_image = None
                self.database_preview_label.configure( image="", text=f"{len(selection)} images selected" )
                self.database_preview_label.image = None
                self.database_path_label.configure( text="" )
                self.load_image_tags_for_editing()
                # Clear file tags display for multiple selection
                self.update_file_tags_display( None )
        else:
            # No selection - clear tag editing interface and file tags display
            self.selected_image_files = []
            self.clear_image_tag_interface()
            self.update_file_tags_display( None )
            
    def load_image_tags_for_editing( self ):
        """Load tags for the selected images into the editing interface with lazy loading"""
        # Don't reload during immediate tag changes - the UI already reflects user intent
        if self.processing_tag_change:
            return
            
        if not self.selected_image_files or not self.current_database_path:
            self.clear_image_tag_interface()
            return
            
        try:
            # Use lazy loading to get image metadata
            image_data = {}
            ratings = []
            
            for filepath in self.selected_image_files:
                metadata = self.load_image_metadata_lazy( filepath )
                if metadata:
                    image_data[filepath] = {'id': metadata['id'], 'rating': metadata['rating']}
                    ratings.append( metadata['rating'] )
            
            if not image_data:
                self.clear_image_tag_interface()
                return
                
            # Handle ratings
            if ratings:
                if len( set( ratings ) ) == 1:
                    # All same rating
                    self.image_rating_var.set( ratings[0] )
                    self.image_rating_scale.configure( state='normal' )
                else:
                    # Different ratings - grey out scale
                    self.image_rating_scale.configure( state='disabled' )
                    self.image_rating_var.set( 0 )
            
            # Use lazy loading to get all available tags
            all_tags = self.load_all_tags_lazy()
            
            # For each tag, count how many selected images have it using cached metadata
            tag_counts = {}
            total_images = len( image_data )
            
            for tag_id, tag_name in all_tags:
                count = 0
                for filepath in self.selected_image_files:
                    metadata = self.get_cached_image_metadata( filepath )
                    if metadata and tag_name in metadata['tags']:
                        count += 1
                tag_counts[tag_id] = count
                
            # Clear existing checkboxes
            for widget in self.image_tag_scrollable_frame.winfo_children():
                widget.destroy()
            self.image_tag_checkboxes.clear()
            
            # Create checkboxes for each tag
            for tag_id, tag_name in all_tags:
                count = tag_counts[tag_id]
                
                # Create frame for this tag
                tag_frame = ttk.Frame( self.image_tag_scrollable_frame )
                tag_frame.pack( fill=tk.X, pady=1 )
                
                if count == total_images:
                    # All images have this tag - normal checked checkbox
                    tag_var = tk.BooleanVar( value=True )
                    checkbox = tk.Checkbutton( tag_frame, text=tag_name, variable=tag_var,
                                             command=lambda tid=tag_id: self.on_image_tag_changed( tid ) )
                    checkbox.pack( side=tk.LEFT, anchor=tk.W )
                    
                    self.image_tag_checkboxes[tag_id] = {
                        'var': tag_var,
                        'name': tag_name,
                        'checkbox': checkbox,
                        'state': 'common',
                        'frame': tag_frame
                    }
                elif count > 0:
                    # Some images have this tag - greyed out checkbox
                    tag_var = tk.BooleanVar( value=False )
                    checkbox = tk.Checkbutton( tag_frame, text=tag_name, variable=tag_var, 
                                             fg='grey', selectcolor='lightgrey',
                                             command=lambda tid=tag_id: self.on_image_partial_checkbox_clicked( tid ) )
                    checkbox.pack( side=tk.LEFT, anchor=tk.W )
                    
                    self.image_tag_checkboxes[tag_id] = {
                        'var': tag_var,
                        'name': tag_name,
                        'checkbox': checkbox,
                        'state': 'partial',
                        'frame': tag_frame
                    }
                else:
                    # No images have this tag - normal unchecked checkbox
                    tag_var = tk.BooleanVar( value=False )
                    checkbox = tk.Checkbutton( tag_frame, text=tag_name, variable=tag_var,
                                             command=lambda tid=tag_id: self.on_image_tag_changed( tid ) )
                    checkbox.pack( side=tk.LEFT, anchor=tk.W )
                    
                    self.image_tag_checkboxes[tag_id] = {
                        'var': tag_var,
                        'name': tag_name,
                        'checkbox': checkbox,
                        'state': 'none',
                        'frame': tag_frame
                    }
            
            # Enable apply button
            self.image_apply_button.configure( state='normal' )
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to load tags for editing: {str(e)}" )
            self.clear_image_tag_interface()
            
    def clear_image_tag_interface( self ):
        """Clear the tag editing interface"""
        # Clear checkboxes
        for widget in self.image_tag_scrollable_frame.winfo_children():
            widget.destroy()
        self.image_tag_checkboxes.clear()
        
        # Clear new tags entry
        self.image_new_tags_entry.delete( 0, tk.END )
        
        # Reset rating
        self.image_rating_var.set( 0 )
        self.image_rating_scale.configure( state='normal' )
        
        # Disable apply button
        self.image_apply_button.configure( state='disabled' )
        
        # Note: File tags display is managed separately and not cleared here
        
    def on_image_partial_checkbox_clicked( self, tag_id ):
        """Handle clicking on a greyed out (partial) checkbox in the image tags interface"""
        # Prevent double-processing
        if self.processing_tag_change:
            return
            
        if tag_id in self.image_tag_checkboxes and self.image_tag_checkboxes[tag_id]['state'] == 'partial':
            tag_data = self.image_tag_checkboxes[tag_id]
            tag_name = tag_data['name']
            tag_frame = tag_data['frame']
            current_value = tag_data['var'].get()
            
            # Set flag to prevent double-processing
            self.processing_tag_change = True
            try:
                # Destroy the old greyed checkbox
                tag_data['checkbox'].destroy()
                
                # Create a new normal checkbox with the current state
                new_var = tk.BooleanVar( value=current_value )
                new_checkbox = tk.Checkbutton( tag_frame, text=tag_name, variable=new_var,
                                             command=lambda tid=tag_id: self.on_image_tag_changed( tid ) )
                new_checkbox.pack( side=tk.LEFT, anchor=tk.W )
                
                # Update the stored data
                self.image_tag_checkboxes[tag_id] = {
                    'var': new_var,
                    'name': tag_name,
                    'checkbox': new_checkbox,
                    'state': 'common' if current_value else 'none',
                    'frame': tag_frame
                }
                
                # Apply the change immediately
                self.apply_single_tag_change( tag_id, current_value )
            finally:
                # Clear the flag after a delay to allow all refresh operations to complete
                def clear_partial_flag():
                    self.processing_tag_change = False
                self.root.after( 100, clear_partial_flag )
            
    def on_image_tag_changed( self, tag_id ):
        """Handle immediate tag checkbox changes"""
        # Prevent double-processing
        if self.processing_tag_change:
            return
            
        if tag_id in self.image_tag_checkboxes:
            tag_data = self.image_tag_checkboxes[tag_id]
            is_checked = tag_data['var'].get()
            
            # Set flag to prevent double-processing
            self.processing_tag_change = True
            try:
                self.apply_single_tag_change( tag_id, is_checked )
            finally:
                # Clear the flag after a delay to allow all refresh operations to complete
                def clear_flag():
                    self.processing_tag_change = False
                self.root.after( 100, clear_flag )
            
    def apply_single_tag_change( self, tag_id, is_checked ):
        """Apply a single tag change immediately to selected images"""
        if not self.selected_image_files or not self.current_database_path:
            return
            
        # Check if we have a large selection that might cause freezing
        if len( self.selected_image_files ) > 50:
            # For large selections, show a warning and ask for confirmation
            response = messagebox.askyesno( 
                "Large Selection Warning", 
                f"You have {len(self.selected_image_files)} images selected. "
                "Applying tag changes to this many images may take a while and could temporarily freeze the app. "
                "Do you want to continue?"
            )
            if not response:
                return
                
            # For very large selections, use async processing
            if len( self.selected_image_files ) > 100:
                self.root.after( 1, lambda: self.apply_single_tag_change_async( tag_id, is_checked ) )
                return
            
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            # Optimize: Get all image IDs in a single query instead of one per file
            database_dir = os.path.dirname( self.current_database_path )
            relative_paths = [os.path.relpath( filepath, database_dir ) 
                            for filepath in self.selected_image_files]
            
            # Use a single query with IN clause for better performance
            placeholders = ','.join( ['?'] * len( relative_paths ) )
            cursor.execute( f"SELECT id, relative_path FROM images WHERE relative_path IN ({placeholders})", 
                          relative_paths )
            results = cursor.fetchall()
            
            # Create a mapping of relative_path to image_id, using the highest ID for duplicates
            path_to_id = {}
            for image_id, rel_path in results:
                if rel_path not in path_to_id or image_id > path_to_id[rel_path]:
                    path_to_id[rel_path] = image_id
            
            image_ids = [path_to_id[rel_path] for rel_path in relative_paths if rel_path in path_to_id]
            
            if not image_ids:
                conn.close()
                return
                
            # Apply the tag change using batch operations
            if is_checked:
                # Batch add tag to images
                batch_data = [(image_id, tag_id) for image_id in image_ids]
                cursor.executemany( "INSERT OR IGNORE INTO image_tags (image_id, tag_id) VALUES (?, ?)", 
                                  batch_data )
            else:
                # Batch remove tag from images
                batch_data = [(image_id, tag_id) for image_id in image_ids]
                cursor.executemany( "DELETE FROM image_tags WHERE image_id = ? AND tag_id = ?", 
                                  batch_data )
            
            conn.commit()
            conn.close()
            
            # Invalidate cache for all affected images BEFORE refreshing views
            for filepath in self.selected_image_files:
                self.invalidate_image_cache( filepath )
            
            # For large selections, only refresh filtered images, not the entire database view
            if len( self.selected_image_files ) > 20:
                self.refresh_filtered_images()
            else:
                # For small selections, refresh everything
                self.refresh_database_view()
                self.refresh_filtered_images()
            # Note: load_image_tags_for_editing() is called automatically by 
            # on_virtual_selection_changed() when refresh_filtered_images() runs
            
        except Exception as e:
            print( f"Error applying tag change: {e}" )
            messagebox.showerror( "Error", f"Failed to apply tag change: {str(e)}" )
            
    def apply_single_tag_change_async( self, tag_id, is_checked ):
        """Apply tag changes asynchronously for very large selections to prevent freezing"""
        try:
            # Show progress dialog
            progress_window = tk.Toplevel( self.root )
            progress_window.title( "Applying Tag Changes" )
            progress_window.geometry( "400x150" )
            progress_window.transient( self.root )
            progress_window.grab_set()
            
            # Center the progress window
            progress_window.geometry( "+%d+%d" % (self.root.winfo_rootx() + 50, self.root.winfo_rooty() + 50) )
            
            # Progress label
            progress_label = ttk.Label( progress_window, text="Applying tag changes to images..." )
            progress_label.pack( pady=20 )
            
            # Progress bar
            progress_bar = ttk.Progressbar( progress_window, mode='indeterminate' )
            progress_bar.pack( fill=tk.X, padx=20, pady=10 )
            progress_bar.start()
            
            # Status label
            status_label = ttk.Label( progress_window, text=f"Processing {len(self.selected_image_files)} images..." )
            status_label.pack( pady=10 )
            
            # Process in background
            def process_tags():
                try:
                    conn = sqlite3.connect( self.current_database_path )
                    cursor = conn.cursor()
                    
                    # Get all image IDs in a single query
                    relative_paths = [os.path.relpath( filepath, os.path.dirname( self.current_database_path ) ) 
                                    for filepath in self.selected_image_files]
                    
                    placeholders = ','.join( ['?'] * len( relative_paths ) )
                    cursor.execute( f"SELECT id, relative_path FROM images WHERE relative_path IN ({placeholders})", 
                                  relative_paths )
                    results = cursor.fetchall()
                    
                    path_to_id = {row[1]: row[0] for row in results}
                    image_ids = [path_to_id[rel_path] for rel_path in relative_paths if rel_path in path_to_id]
                    
                    if image_ids:
                        # Apply tag changes
                        if is_checked:
                            batch_data = [(image_id, tag_id) for image_id in image_ids]
                            cursor.executemany( "INSERT OR IGNORE INTO image_tags (image_id, tag_id) VALUES (?, ?)", 
                                              batch_data )
                        else:
                            batch_data = [(image_id, tag_id) for image_id in image_ids]
                            cursor.executemany( "DELETE FROM image_tags WHERE image_id = ? AND tag_id = ?", 
                                              batch_data )
                        
                        conn.commit()
                        
                        # Invalidate cache
                        for filepath in self.selected_image_files:
                            self.invalidate_image_cache( filepath )
                    
                    conn.close()
                    
                    # Update UI in main thread
                    self.root.after( 0, lambda: self.finish_async_tag_change( progress_window ) )
                    
                except Exception as e:
                    # Show error in main thread
                    self.root.after( 0, lambda: self.show_async_error( progress_window, str(e) ) )
            
            # Start processing in background
            import threading
            thread = threading.Thread( target=process_tags, daemon=True )
            thread.start()
            
        except Exception as e:
            print( f"Error setting up async tag change: {e}" )
            messagebox.showerror( "Error", f"Failed to set up async tag change: {str(e)}" )
            
    def finish_async_tag_change( self, progress_window ):
        """Finish the async tag change operation"""
        progress_window.destroy()
        # Only refresh filtered images for large selections
        self.refresh_filtered_images()
        # Note: load_image_tags_for_editing() is called automatically by 
        # on_virtual_selection_changed() when refresh_filtered_images() runs
        messagebox.showinfo( "Success", "Tag changes applied successfully!" )
        
    def show_async_error( self, progress_window, error_msg ):
        """Show error from async operation"""
        progress_window.destroy()
        messagebox.showerror( "Error", f"Failed to apply tag changes: {error_msg}" )
            
    def on_rating_scale_click( self, event ):
        """Handle mouse click on rating scale to jump to position"""
        # Calculate the clicked position as a rating value
        scale_width = self.image_rating_scale.winfo_width()
        click_x = event.x
        
        # Calculate rating based on click position (0-10 range)
        if scale_width > 0:
            rating = round( (click_x / scale_width) * 10 )
            rating = max( 0, min( 10, rating ) )  # Clamp to valid range
            
            # Set the slider position and trigger the rating change
            self.image_rating_var.set( rating )
            self.on_image_rating_changed( str( rating ) )
    
    def on_image_rating_changed( self, value=None ):
        """Handle immediate rating changes in the Image Tags frame"""
        if not self.current_database_path or not self.selected_image_files:
            return
        
        # Don't apply if slider is disabled (mixed ratings)
        if self.image_rating_scale['state'] == 'disabled':
            return
            
        # Check if we have a large selection that might cause freezing
        if len( self.selected_image_files ) > 50:
            # For large selections, show a warning and ask for confirmation
            response = messagebox.askyesno( 
                "Large Selection Warning", 
                f"You have {len(self.selected_image_files)} images selected. "
                "Applying rating changes to this many images may take a while and could temporarily freeze the app. "
                "Do you want to continue?"
            )
            if not response:
                return
                
            # For very large selections, use async processing
            if len( self.selected_image_files ) > 100:
                self.root.after( 1, lambda: self.apply_rating_changes_async( self.image_rating_var.get() ) )
                return
            
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            rating = self.image_rating_var.get()
            
            # Optimize: Update all images in a single query instead of one per file
            relative_paths = [os.path.relpath( filepath, os.path.dirname( self.current_database_path ) ) 
                            for filepath in self.selected_image_files]
            
            # Use a single query with IN clause for better performance
            placeholders = ','.join( ['?'] * len( relative_paths ) )
            cursor.execute( f"UPDATE images SET rating = ? WHERE relative_path IN ({placeholders})", 
                          [rating] + relative_paths )
            
            conn.commit()
            
            # Invalidate cache for all affected images
            for filepath in self.selected_image_files:
                if filepath in self.image_metadata_cache:
                    del self.image_metadata_cache[filepath]
            
            # Only refresh if rating filters are actually active, otherwise skip refresh entirely
            min_rating = self.min_rating_var.get()
            max_rating = self.max_rating_var.get()
            has_rating_filter = min_rating > 0 or max_rating < 10
            
            if has_rating_filter:
                # Rating filters are active, so we need to refresh to update the filtered list
                current_selection_indices = list( self.database_image_listbox.curselection() )
                current_filenames = []
                for index in current_selection_indices:
                    current_filenames.append( self.database_image_listbox.get( index ) )
                
                self.refresh_filtered_images( preserve_selection=current_filenames )
            # If no rating filters are active, no need to refresh at all - selection will stay stable
            
        except Exception as e:
            print( f"Error updating image rating: {e}" )
            messagebox.showerror( "Error", f"Failed to update image ratings: {str(e)}" )
        finally:
            conn.close()
            
    def apply_rating_changes_async( self, rating ):
        """Apply rating changes asynchronously for very large selections to prevent freezing"""
        try:
            # Show progress dialog
            progress_window = tk.Toplevel( self.root )
            progress_window.title( "Applying Rating Changes" )
            progress_window.geometry( "400x150" )
            progress_window.transient( self.root )
            progress_window.grab_set()
            
            # Center the progress window
            progress_window.geometry( "+%d+%d" % (self.root.winfo_rootx() + 50, self.root.winfo_rooty() + 50) )
            
            # Progress label
            progress_label = ttk.Label( progress_window, text="Applying rating changes to images..." )
            progress_label.pack( pady=20 )
            
            # Progress bar
            progress_bar = ttk.Progressbar( progress_window, mode='indeterminate' )
            progress_bar.pack( fill=tk.X, padx=20, pady=10 )
            progress_bar.start()
            
            # Status label
            status_label = ttk.Label( progress_window, text=f"Processing {len(self.selected_image_files)} images..." )
            status_label.pack( pady=10 )
            
            # Process in background
            def process_ratings():
                try:
                    conn = sqlite3.connect( self.current_database_path )
                    cursor = conn.cursor()
                    
                    # Update all images in a single query
                    relative_paths = [os.path.relpath( filepath, os.path.dirname( self.current_database_path ) ) 
                                    for filepath in self.selected_image_files]
                    
                    placeholders = ','.join( ['?'] * len( relative_paths ) )
                    cursor.execute( f"UPDATE images SET rating = ? WHERE relative_path IN ({placeholders})", 
                                  [rating] + relative_paths )
                    
                    conn.commit()
                    conn.close()
                    
                    # Invalidate cache for all affected images
                    for filepath in self.selected_image_files:
                        if filepath in self.image_metadata_cache:
                            del self.image_metadata_cache[filepath]
                    
                    # Update UI in main thread
                    self.root.after( 0, lambda: self.finish_async_rating_change( progress_window ) )
                    
                except Exception as e:
                    # Show error in main thread
                    self.root.after( 0, lambda: self.show_async_rating_error( progress_window, str(e) ) )
            
            # Start processing in background
            import threading
            thread = threading.Thread( target=process_ratings, daemon=True )
            thread.start()
            
        except Exception as e:
            print( f"Error setting up async rating change: {e}" )
            messagebox.showerror( "Error", f"Failed to set up async rating change: {str(e)}" )
            
    def finish_async_rating_change( self, progress_window ):
        """Finish the async rating change operation"""
        progress_window.destroy()
        # Only refresh filtered images for large selections
        self.refresh_filtered_images()
        messagebox.showinfo( "Success", "Rating changes applied successfully!" )
        
    def show_async_rating_error( self, progress_window, error_msg ):
        """Show error from async rating operation"""
        progress_window.destroy()
        messagebox.showerror( "Error", f"Failed to apply rating changes: {error_msg}" )
    
    def apply_image_tag_changes( self ):
        """Apply new tags to selected images (rating changes are now immediate)"""
        if not self.selected_image_files or not self.current_database_path:
            return
            
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            # Get image IDs for the selected files
            image_data = {}
            for filepath in self.selected_image_files:
                relative_path = os.path.relpath( filepath, os.path.dirname( self.current_database_path ) )
                cursor.execute( "SELECT id FROM images WHERE relative_path = ?", (relative_path,) )
                result = cursor.fetchone()
                if result:
                    image_data[filepath] = {'id': result[0]}
            
            if not image_data:
                messagebox.showerror( "Error", "No valid images found in database" )
                return
                
            changes_made = False
            
            # Add new tags (existing tag checkboxes and rating changes are handled immediately)
            new_tags_text = self.image_new_tags_entry.get().strip()
            if new_tags_text:
                new_tags = [tag.strip() for tag in new_tags_text.split( ',' ) if tag.strip()]
                
                # Batch insert new tags
                tag_batch = [(tag_name,) for tag_name in new_tags]
                cursor.executemany( "INSERT OR IGNORE INTO tags (name) VALUES (?)", tag_batch )
                
                for tag_name in new_tags:
                    # Get tag ID
                    cursor.execute( "SELECT id FROM tags WHERE name = ?", (tag_name,) )
                    tag_id = cursor.fetchone()[0]
                    
                    # Batch add tag to all selected images
                    image_tag_batch = [(img_data['id'], tag_id) for img_data in image_data.values()]
                    cursor.executemany( "INSERT OR IGNORE INTO image_tags (image_id, tag_id) VALUES (?, ?)", 
                                      image_tag_batch )
                changes_made = True
            
            if changes_made:
                conn.commit()
                
                # Invalidate cache for all affected images
                for filepath in self.selected_image_files:
                    self.invalidate_image_cache( filepath )
                
                # Clear new tags entry
                self.image_new_tags_entry.delete( 0, tk.END )
                
                # Refresh views
                self.refresh_database_view()
                self.refresh_filtered_images()
                
                # Reload the tag editing interface to reflect changes
                self.load_image_tags_for_editing()
                

            
            conn.close()
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to apply changes: {str(e)}" )
                
    def on_database_image_double_click( self, event ):
        """Handle double click in database image list"""
        selection = self.database_image_listbox.curselection()
        if selection and self.current_database:
            index = selection[0]
            filename = self.database_image_listbox.get( index )
            
            filepath = self.find_image_path( filename )
            if filepath:
                self.enter_fullscreen_mode( filepath )
                

                
    def on_database_preview_double_click( self, event ):
        """Handle double click on database preview image"""
        if self.current_database_image and os.path.exists( self.current_database_image ):
            self.enter_fullscreen_mode( self.current_database_image )
                
    def find_image_path( self, filename ):
        """Find the full path of an image file by filename"""
        if not self.current_database_path:
            return None
            
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            cursor.execute( "SELECT relative_path FROM images WHERE filename = ?", (filename,) )
            result = cursor.fetchone()
            conn.close()
            
            if result:
                return os.path.join( self.current_database, result[0] )
                
        except Exception as e:
            print( f"Error finding image path: {e}" )
            
        return None
    
    def get_image_paths_batch( self, filenames ):
        """Efficiently get full paths for multiple filenames using a single batch query"""
        if not self.current_database_path or not filenames:
            return []
            
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            # Create placeholders for the IN clause
            placeholders = ','.join( ['?'] * len( filenames ) )
            query = f"SELECT filename, relative_path FROM images WHERE filename IN ({placeholders})"
            
            cursor.execute( query, filenames )
            results = cursor.fetchall()
            conn.close()
            
            # Create a mapping of filename to full path
            filename_to_path = {}
            for filename, relative_path in results:
                filename_to_path[filename] = os.path.join( self.current_database, relative_path )
            
            # Return paths in the same order as the input filenames
            full_paths = []
            for filename in filenames:
                if filename in filename_to_path:
                    full_paths.append( filename_to_path[filename] )
            
            return full_paths
                
        except Exception as e:
            print( f"Error getting image paths in batch: {e}" )
            return []
    
    def get_fullscreen_image_path( self, index ):
        """Lazily get the full path for a fullscreen image at the given index"""
        if self.current_database_path and 0 <= index < len( self.fullscreen_filenames ):
            filename = self.fullscreen_filenames[index]
            
            # Check cache first
            if filename in self.fullscreen_paths_cache:
                return self.fullscreen_paths_cache[filename]
            
            # Resolve path from database
            full_path = self.find_image_path( filename )
            if full_path:
                # Cache the result
                self.fullscreen_paths_cache[filename] = full_path
                return full_path
        
        # Fallback to traditional approach for browse tab
        if 0 <= index < len( self.fullscreen_images ):
            return self.fullscreen_images[index]
            
        return None
        

            

        
    def on_all_include_or_changed( self ):
        """Handle 'all' include (OR) checkbox change"""
        include_all = self.all_include_or_var.get()
        
        # Update all individual tag include (OR) checkboxes
        for tag, checkboxes in self.tag_checkboxes.items():
            checkboxes['include_or_var'].set( include_all )
            if include_all:
                self.included_or_tags.add( tag )
                # Uncheck exclude and include (AND) if include (OR) is checked
                checkboxes['exclude_var'].set( False )
                checkboxes['include_and_var'].set( False )
                self.excluded_tags.discard( tag )
                self.included_and_tags.discard( tag )
            else:
                self.included_or_tags.discard( tag )
                
        self.refresh_filtered_images()
        
    def on_all_include_and_changed( self ):
        """Handle 'all' include (AND) checkbox change"""
        include_all = self.all_include_and_var.get()
        
        # Update all individual tag include (AND) checkboxes
        for tag, checkboxes in self.tag_checkboxes.items():
            checkboxes['include_and_var'].set( include_all )
            if include_all:
                self.included_and_tags.add( tag )
                # Uncheck exclude and include (OR) if include (AND) is checked
                checkboxes['exclude_var'].set( False )
                checkboxes['include_or_var'].set( False )
                self.excluded_tags.discard( tag )
                self.included_or_tags.discard( tag )
            else:
                self.included_and_tags.discard( tag )
                
        self.refresh_filtered_images()
        
    def on_all_exclude_changed( self ):
        """Handle 'all' exclude checkbox change"""
        exclude_all = self.all_exclude_var.get()
        
        # Update all individual tag exclude checkboxes
        for tag, checkboxes in self.tag_checkboxes.items():
            checkboxes['exclude_var'].set( exclude_all )
            if exclude_all:
                self.excluded_tags.add( tag )
                # Uncheck include (OR) and include (AND) if exclude is checked
                checkboxes['include_or_var'].set( False )
                checkboxes['include_and_var'].set( False )
                self.included_or_tags.discard( tag )
                self.included_and_tags.discard( tag )
            else:
                self.excluded_tags.discard( tag )
                
        self.refresh_filtered_images()
        
    def on_tag_include_or_changed( self, tag ):
        """Handle individual tag include (OR) checkbox change"""
        include_checked = self.tag_checkboxes[tag]['include_or_var'].get()
        
        if include_checked:
            self.included_or_tags.add( tag )
            # Uncheck exclude and include (AND) for this tag
            self.tag_checkboxes[tag]['exclude_var'].set( False )
            self.tag_checkboxes[tag]['include_and_var'].set( False )
            self.excluded_tags.discard( tag )
            self.included_and_tags.discard( tag )
            # Uncheck "all" include (OR) since not all are selected
            self.all_include_or_var.set( False )
        else:
            self.included_or_tags.discard( tag )
            
        self.refresh_filtered_images()
        
    def on_tag_include_and_changed( self, tag ):
        """Handle individual tag include (AND) checkbox change"""
        include_checked = self.tag_checkboxes[tag]['include_and_var'].get()
        
        if include_checked:
            self.included_and_tags.add( tag )
            # Uncheck exclude and include (OR) for this tag
            self.tag_checkboxes[tag]['exclude_var'].set( False )
            self.tag_checkboxes[tag]['include_or_var'].set( False )
            self.excluded_tags.discard( tag )
            self.included_or_tags.discard( tag )
            # Uncheck "all" include (AND) since not all are selected
            self.all_include_and_var.set( False )
        else:
            self.included_and_tags.discard( tag )
            
        self.refresh_filtered_images()
        
    def on_tag_exclude_changed( self, tag ):
        """Handle individual tag exclude checkbox change"""
        exclude_checked = self.tag_checkboxes[tag]['exclude_var'].get()
        
        if exclude_checked:
            self.excluded_tags.add( tag )
            # Uncheck include (OR) and include (AND) for this tag
            self.tag_checkboxes[tag]['include_or_var'].set( False )
            self.tag_checkboxes[tag]['include_and_var'].set( False )
            self.included_or_tags.discard( tag )
            self.included_and_tags.discard( tag )
            # Uncheck "all" exclude since not all are selected
            self.all_exclude_var.set( False )
        else:
            self.excluded_tags.discard( tag )
            
        self.refresh_filtered_images()
        
    def on_rating_filter_changed( self, value=None ):
        """Handle rating filter changes"""
        # Ensure min <= max
        min_val = self.min_rating_var.get()
        max_val = self.max_rating_var.get()
        
        if min_val > max_val:
            if value and value == str(min_val):
                # User changed min, adjust max
                self.max_rating_var.set( min_val )
            else:
                # User changed max, adjust min
                self.min_rating_var.set( max_val )
        
        self.refresh_filtered_images()
    
    def clear_filters( self ):
        """Clear all tag filters and reset rating filters"""
        self.included_or_tags.clear()
        self.included_and_tags.clear()
        self.excluded_tags.clear()
        
        # Reset rating filters to full range
        self.min_rating_var.set( 0 )
        self.max_rating_var.set( 10 )
        
        # Clear all checkboxes
        self.all_include_or_var.set( False )
        self.all_include_and_var.set( False )
        self.all_exclude_var.set( False )
        
        for tag, checkboxes in self.tag_checkboxes.items():
            checkboxes['include_or_var'].set( False )
            checkboxes['include_and_var'].set( False )
            checkboxes['exclude_var'].set( False )
            
        self.refresh_filtered_images()
        
    def show_tag_dialog( self, filepath ):
        """Show dialog for adding/editing tags for an image"""
        if not self.current_database_path:
            messagebox.showwarning( "Warning", "No database is currently open" )
            return
            
        dialog = TagDialog( self.root, filepath, self.current_database_path )
        self.root.wait_window( dialog.dialog )
        
        # Refresh views after tag changes
        self.refresh_database_view()
        self.refresh_tag_filters()
        
    def show_multi_tag_dialog( self, filepaths ):
        """Show dialog for adding/editing tags for multiple images"""
        if not self.current_database_path:
            messagebox.showwarning( "Warning", "No database is currently open" )
            return
            
        dialog = MultiTagDialog( self.root, filepaths, self.current_database_path )
        self.root.wait_window( dialog.dialog )
        
        # Refresh views after tag changes
        self.refresh_database_view()
        self.refresh_tag_filters()
        
    def show_directory_tag_dialog( self, directory_path ):
        """Show dialog for adding/editing tags for all images in a directory recursively"""
        if not self.current_database_path:
            messagebox.showwarning( "Warning", "No database is currently open" )
            return
            
        # Find all image files in directory recursively that are in the database
        image_files = []
        try:
            for root, dirs, files in os.walk( directory_path ):
                for file in files:
                    filepath = os.path.join( root, file )
                    if self.is_image_file( filepath ) and self.is_file_in_database( filepath ):
                        image_files.append( filepath )
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to scan directory: {str(e)}" )
            return
            
        if not image_files:
            messagebox.showinfo( "No Images", "No images found in the directory that are in the current database." )
            return
            
        # Show confirmation dialog
        result = messagebox.askyesno( 
            "Confirm Directory Tag Edit",
            f"Edit tags for {len(image_files)} images found in:\n{directory_path}\n\nContinue?"
        )
        
        if result:
            dialog = MultiTagDialog( self.root, image_files, self.current_database_path )
            self.root.wait_window( dialog.dialog )
            
            # Refresh views after tag changes
            self.refresh_database_view()
            self.refresh_tag_filters()
        
    def refresh_tag_filters( self ):
        """Refresh the tag filters to add new tags and remove unused ones"""
        if not self.current_database_path:
            return
            
        # Simply refresh the entire database view which will rebuild tag filters
        self.refresh_database_view()
        
        # Also refresh the filtered images list based on current filter settings
        self.refresh_filtered_images()
        
    def load_settings( self ):
        """Load application settings from file"""
        directory_loaded = False
        
        try:
            if os.path.exists( self.settings_file ):
                with open( self.settings_file, 'r' ) as f:
                    settings = json.load( f )
                    
                last_directory = settings.get( 'last_directory' )
                if last_directory and os.path.exists( last_directory ):
                    # Set the drive dropdown to match the saved directory
                    drive = os.path.splitdrive( last_directory )[0] + "\\"
                    if hasattr( self, 'drive_var' ) and drive in self.drive_combo['values']:
                        self.drive_var.set( drive )
                    # Load the tree from drive root and expand to saved directory
                    self.load_directory_tree_and_expand( last_directory )
                    directory_loaded = True
                    
        except Exception as e:
            print( f"Error loading settings: {e}" )
            
        # If no settings loaded or directory doesn't exist, use default
        if not directory_loaded:
            self.load_directory_tree()
            
    def save_current_directory( self, directory ):
        """Save the current directory and window state to settings"""
        try:
            settings = {}
            if os.path.exists( self.settings_file ):
                with open( self.settings_file, 'r' ) as f:
                    settings = json.load( f )
                    
            settings['last_directory'] = directory
            self.save_paned_positions( settings )
            self.save_window_geometry( settings )
            self.save_active_tab( settings )
            self.save_current_database( settings )
            
            with open( self.settings_file, 'w' ) as f:
                json.dump( settings, f, indent=2 )
                
        except Exception as e:
            print( f"Error saving settings: {e}" )
    
    def save_directory_only( self, directory ):
        """Save only the directory to settings without affecting other settings"""
        try:
            settings = {}
            if os.path.exists( self.settings_file ):
                with open( self.settings_file, 'r' ) as f:
                    settings = json.load( f )
                    
            settings['last_directory'] = directory
            # Don't call save_window_geometry or save_active_tab here
            # This method should only update the directory, preserving all other settings
            
            with open( self.settings_file, 'w' ) as f:
                json.dump( settings, f, indent=2 )
                
        except Exception as e:
            print( f"Error saving directory: {e}" )
    
    def save_paned_positions( self, settings ):
        """Save paned window positions to settings"""
        try:
            # Save vertical paned position (database tab)
            if hasattr( self, 'vertical_paned' ):
                pos = self.vertical_paned.sashpos( 0 )
                if pos > 0:  # Only save valid positions
                    settings["vertical_paned_pos"] = pos
            
            # Save horizontal paned position (main database paned window)
            if hasattr( self, 'horizontal_paned' ):
                pos = self.horizontal_paned.sashpos( 0 )
                if pos > 0:  # Only save valid positions
                    settings["horizontal_paned_pos"] = pos
                    
        except Exception as e:
            print( f"Error saving paned positions: {e}" )
    
    def save_window_geometry( self, settings ):
        """Save window position and size to settings"""
        try:
            # Get current window geometry
            geometry = self.root.geometry()  # Returns format like "800x600+100+50"

            
            # Parse geometry string - handle negative coordinates
            if 'x' in geometry:
                # Split into size and position parts
                # Handle negative coordinates by using rsplit and manual parsing
                if '+' in geometry or '-' in geometry[geometry.find('x')+1:]:
                    size_part = geometry.split('+')[0].split('-')[0]  # Get the size part
                    
                    # Find position part after size
                    pos_start = len(size_part)
                    pos_part = geometry[pos_start:]  # Everything after size
                    
                    # Parse coordinates, handling negative values
                    coords = []
                    current_coord = ""
                    for i, char in enumerate(pos_part):
                        if char in '+-' and i > 0:
                            if current_coord:
                                coords.append(int(current_coord))
                            current_coord = char
                        else:
                            current_coord += char
                    if current_coord:
                        coords.append(int(current_coord))
                    
                    if len(coords) >= 2:
                        width, height = size_part.split('x')
                        x_pos, y_pos = coords[0], coords[1]
                        
                        # Don't save invalid or tiny window sizes
                        if int(width) > 100 and int(height) > 100:
                            settings['window'] = {
                                'width': int( width ),
                                'height': int( height ),
                                'x': int( x_pos ),
                                'y': int( y_pos )
                            }
                        # else: silently skip invalid sizes
                    
        except Exception as e:
            print( f"Error saving window geometry: {e}" )
    
    def save_active_tab( self, settings ):
        """Save the currently active tab to settings"""
        try:
            # Get the currently selected tab
            current_tab = self.notebook.index( self.notebook.select() )
            tab_name = "browse" if current_tab == 0 else "database"
            settings['active_tab'] = tab_name
            
        except Exception as e:
            print( f"Error saving active tab: {e}" )
    
    def save_current_database( self, settings ):
        """Save the currently open database and update recent databases list"""
        try:
            # Save current database path
            if self.current_database_path:
                settings['current_database'] = self.current_database_path
                
                # Update recent databases list
                recent_databases = settings.get( 'recent_databases', [] )
                
                # Remove current database if it's already in the list
                if self.current_database_path in recent_databases:
                    recent_databases.remove( self.current_database_path )
                
                # Add current database to the front of the list
                recent_databases.insert( 0, self.current_database_path )
                
                # Keep only the 5 most recent
                settings['recent_databases'] = recent_databases[:5]
            else:
                # No database currently open
                settings['current_database'] = None
            
            # Save rating filter values
            if hasattr( self, 'min_rating_var' ) and hasattr( self, 'max_rating_var' ):
                            settings['rating_filter'] = {
                'min': self.min_rating_var.get(),
                'max': self.max_rating_var.get()
            }
            settings['show_thumbnails'] = self.show_thumbnails.get()
                
        except Exception as e:
            print( f"Error saving current database: {e}" )
    
    def save_active_tab_only( self ):
        """Save only the active tab state to settings (called when tab changes)"""
        # Don't save tab state during startup
        if not self.startup_complete:
            return
            
        try:
            settings = {}
            if os.path.exists( self.settings_file ):
                with open( self.settings_file, 'r' ) as f:
                    settings = json.load( f )
            
            self.save_active_tab( settings )
            
            with open( self.settings_file, 'w' ) as f:
                json.dump( settings, f, indent=2 )
                
        except Exception as e:
            print( f"Error saving active tab: {e}" )
    
    def restore_paned_positions( self ):
        """Restore paned window positions from settings"""
        try:
            if not os.path.exists( self.settings_file ):
                return
                
            with open( self.settings_file, 'r' ) as f:
                settings = json.load( f )
            
            # Restore vertical paned position (database tab)
            if hasattr( self, 'vertical_paned' ) and "vertical_paned_pos" in settings:
                pos = settings["vertical_paned_pos"]
                if pos > 260:  # Ensure minimum constraints are respected
                    self.vertical_paned.sashpos( 0, pos )
            
            # Restore horizontal paned position (main database paned window)
            if hasattr( self, 'horizontal_paned' ) and "horizontal_paned_pos" in settings:
                pos = settings["horizontal_paned_pos"]
                if pos > 100:  # Ensure reasonable minimum
                    self.horizontal_paned.sashpos( 0, pos )
                        
        except Exception as e:
            print( f"Error restoring paned positions: {e}" )
    
    def restore_window_geometry( self ):
        """Restore window position and size from settings"""
        try:

            if not os.path.exists( self.settings_file ):

                self.set_default_window_geometry()
                return
                
            with open( self.settings_file, 'r' ) as f:
                settings = json.load( f )
            
            if 'window' not in settings:

                self.set_default_window_geometry()
                return
                
            window_settings = settings['window']
            width = window_settings.get( 'width', 1000 )
            height = window_settings.get( 'height', 700 )
            x = window_settings.get( 'x', 100 )
            y = window_settings.get( 'y', 100 )

            
            # Validate position is on screen
            if self.is_position_valid( x, y, width, height ):
                geometry = f"{width}x{height}+{x}+{y}"

                self.root.geometry( geometry )
            else:

                self.set_default_window_geometry()
                
        except Exception as e:
            print( f"Error restoring window geometry: {e}" )
            self.set_default_window_geometry()
    
    def restore_active_tab( self ):
        """Restore the active tab from settings"""
        try:
            if not os.path.exists( self.settings_file ):
                return  # Default to browse tab (index 0)
                
            with open( self.settings_file, 'r' ) as f:
                settings = json.load( f )
            
            if 'active_tab' not in settings:
                return  # Default to browse tab
                
            active_tab = settings['active_tab']
            if active_tab == "database":
                self.notebook.select( 1 )  # Select database tab
            else:
                self.notebook.select( 0 )  # Select browse tab (default)
                
        except Exception as e:
            print( f"Error restoring active tab: {e}" )
            # Default to browse tab on error
            self.notebook.select( 0 )
    
    def on_recent_database_selected( self, event ):
        """Handle selection from recent databases dropdown - open database immediately"""
        self.open_selected_recent_database()
    
    def open_selected_recent_database( self ):
        """Open the database selected in the recent databases dropdown"""
        selected_display = self.recent_databases_var.get()
        if not selected_display:
            return
            
        # Extract the full path from the display name format: "filename.db (directory)"
        try:
            # Parse the display format to get the actual path
            if " (" in selected_display and selected_display.endswith( ")" ):
                filename = selected_display.split( " (" )[0]
                directory = selected_display.split( " (" )[1][:-1]  # Remove the closing parenthesis
                selected_path = os.path.join( directory, filename )
            else:
                # Fallback - assume it's already a full path
                selected_path = selected_display
                
            if os.path.exists( selected_path ):
                self.open_database_file( selected_path )
            else:
                messagebox.showerror( "Error", f"Database file not found: {selected_path}" )
                # Remove the non-existent database from recent list
                self.remove_from_recent_databases( selected_path )
                
        except Exception as e:
            messagebox.showerror( "Error", f"Error opening database: {str(e)}" )
    
    def remove_from_recent_databases( self, database_path ):
        """Remove a database from the recent databases list"""
        try:
            settings = {}
            if os.path.exists( self.settings_file ):
                with open( self.settings_file, 'r' ) as f:
                    settings = json.load( f )
            
            recent_databases = settings.get( 'recent_databases', [] )
            if database_path in recent_databases:
                recent_databases.remove( database_path )
                settings['recent_databases'] = recent_databases
                
                with open( self.settings_file, 'w' ) as f:
                    json.dump( settings, f, indent=2 )
                
                # Update the dropdown
                self.update_recent_databases_dropdown()
                
        except Exception as e:
            print( f"Error removing database from recent list: {e}" )
    
    def update_recent_databases_dropdown( self ):
        """Update the recent databases dropdown with current list"""
        try:
            if not hasattr( self, 'recent_databases_combo' ):
                return  # UI not initialized yet
                
            settings = {}
            if os.path.exists( self.settings_file ):
                with open( self.settings_file, 'r' ) as f:
                    settings = json.load( f )
            
            recent_databases = settings.get( 'recent_databases', [] )
            
            # Filter out databases that no longer exist
            existing_databases = [db for db in recent_databases if os.path.exists( db )]
            
            # Create display names (just the filename) but keep full paths as values
            display_values = []
            for db_path in existing_databases:
                filename = os.path.basename( db_path )
                display_values.append( f"{filename} ({os.path.dirname( db_path )})" )
            
            # Try multiple approaches to force combobox refresh
            try:
                # Method 1: Clear and set values
                self.recent_databases_combo['values'] = ()
                self.recent_databases_combo['values'] = display_values
                
                # Method 2: Set the variable first, then force selection update
                if display_values:
                    self.recent_databases_var.set( display_values[0] )
                    # Force the combobox to show the new value
                    self.recent_databases_combo.selection_clear()
                    self.recent_databases_combo.icursor(0)
                else:
                    self.recent_databases_var.set( '' )
                
                # Method 3: Force widget state change to trigger refresh
                current_state = self.recent_databases_combo['state']
                self.recent_databases_combo.configure(state='normal')
                self.recent_databases_combo.configure(state=current_state)
                
                # Method 4: Force focus and update
                self.recent_databases_combo.update_idletasks()
                self.root.update_idletasks()
                
            except Exception as refresh_error:
                print(f"Error in combobox refresh: {refresh_error}")
                # Fallback: recreate the combobox if normal refresh fails
                if hasattr(self, 'recent_databases_combo'):
                    try:
                        parent_frame = self.recent_databases_combo.master
                        self.recent_databases_combo.destroy()
                        
                        self.recent_databases_combo = ttk.Combobox( parent_frame, textvariable=self.recent_databases_var, state="readonly", width=50 )
                        self.recent_databases_combo.configure( values=display_values )
                        self.recent_databases_combo.pack( side=tk.LEFT, padx=(0, 5), fill=tk.X, expand=True )
                        self.recent_databases_combo.bind( "<<ComboboxSelected>>", self.on_recent_database_selected )
                        
                        if display_values:
                            self.recent_databases_var.set( display_values[0] )
                        else:
                            self.recent_databases_var.set( '' )
                    except Exception as recreate_error:
                        print(f"Error recreating combobox: {recreate_error}")
                
        except Exception as e:
            print( f"Error updating recent databases dropdown: {e}" )
    
    def prompt_restore_database( self ):
        """Prompt user to restore the last open database if it exists"""
        try:
            if not os.path.exists( self.settings_file ):
                return
                
            with open( self.settings_file, 'r' ) as f:
                settings = json.load( f )
            
            current_database = settings.get( 'current_database' )
            if current_database and os.path.exists( current_database ):
                # Ask user if they want to reopen the database
                db_name = os.path.basename( current_database )
                result = messagebox.askyesno( 
                    "Restore Database", 
                    f"Would you like to reopen the previously used database?\n\n{db_name}",
                    icon='question'
                )
                
                if result:
                    self.open_database_file( current_database )
                    
        except Exception as e:
            print( f"Error prompting for database restore: {e}" )
    
    def restore_rating_filters( self ):
        """Restore rating filter values from settings"""
        try:
            if not os.path.exists( self.settings_file ):
                return
                
            with open( self.settings_file, 'r' ) as f:
                settings = json.load( f )
            
            rating_filter = settings.get( 'rating_filter' )
            if rating_filter and hasattr( self, 'min_rating_var' ) and hasattr( self, 'max_rating_var' ):
                min_val = rating_filter.get( 'min', 0 )
                max_val = rating_filter.get( 'max', 10 )
                
                # Validate values
                if 0 <= min_val <= 10 and 0 <= max_val <= 10 and min_val <= max_val:
                    self.min_rating_var.set( min_val )
                    self.max_rating_var.set( max_val )
                    
        except Exception as e:
            print( f"Error restoring rating filters: {e}" )
    
    def restore_thumbnail_setting( self ):
        """Restore the thumbnail setting from saved settings"""
        try:
            with open( self.settings_file, 'r' ) as f:
                settings = json.load( f )
                
            show_thumbnails = settings.get( 'show_thumbnails', False )
            self.show_thumbnails.set( show_thumbnails )
                    
        except Exception as e:
            print( f"Error restoring thumbnail setting: {e}" )
    
    def complete_startup( self ):
        """Mark startup as complete to enable state saving"""
        self.startup_complete = True
        # Update recent databases dropdown after startup
        self.update_recent_databases_dropdown()
        # Restore rating filters
        self.restore_rating_filters()
        # Restore thumbnail setting
        self.restore_thumbnail_setting()
        # Prompt to restore database after a short delay
        self.root.after( 500, self.prompt_restore_database )
    
    # Lazy Loading Cache Management
    def clear_cache( self ):
        """Clear all cached data"""
        self.image_metadata_cache.clear()
        self.tag_cache.clear()
    
    def get_cached_image_metadata( self, filepath ):
        """Get cached image metadata or None if not cached"""
        return self.image_metadata_cache.get( filepath )
    
    def cache_image_metadata( self, filepath, metadata ):
        """Cache image metadata with size limit"""
        if len( self.image_metadata_cache ) >= self.cache_max_size:
            # Remove oldest entries (simple FIFO eviction)
            oldest_keys = list( self.image_metadata_cache.keys() )[:100]
            for key in oldest_keys:
                del self.image_metadata_cache[key]
        
        self.image_metadata_cache[filepath] = metadata
    
    def get_cached_tags( self ):
        """Get cached tag data or None if not cached"""
        return self.tag_cache.get( 'all_tags' )
    
    def cache_tags( self, tags ):
        """Cache tag data"""
        self.tag_cache['all_tags'] = tags
    
    def invalidate_image_cache( self, filepath ):
        """Invalidate cache entry for a specific image"""
        if filepath in self.image_metadata_cache:
            del self.image_metadata_cache[filepath]
    
    def on_thumbnails_toggle( self ):
        """Handle the Show Thumbnails option toggle"""
        # Update thumbnail setting for all items in virtual list
        if hasattr( self, 'virtual_image_list' ) and self.virtual_image_list:
            show_thumbs = self.show_thumbnails.get()
            
            # Use the TreeviewImageList method to enable/disable thumbnails
            if hasattr( self.virtual_image_list, 'set_thumbnails_enabled' ):
                self.virtual_image_list.set_thumbnails_enabled( show_thumbs )
            
            # Clear thumbnail cache if disabling thumbnails
            if not show_thumbs:
                self.virtual_image_list.thumbnail_cache.clear()
                if hasattr( self.virtual_image_list, 'clear_thumbnails' ):
                    self.virtual_image_list.clear_thumbnails()
        
        # Refresh the filtered images list to show/hide thumbnails
        if self.current_database_path:
            self.refresh_filtered_images()
    
    def show_todo_list( self ):
        """Show the TODO list dialog"""
        # Create TODO dialog window
        todo_window = tk.Toplevel( self.root )
        todo_window.title( "TODO List - Planned Features" )
        todo_window.geometry( "600x500" )
        todo_window.transient( self.root )
        todo_window.grab_set()
        todo_window.resizable( True, True )
        
        # Center the window
        todo_window.geometry( "+%d+%d" % (self.root.winfo_rootx() + 100, self.root.winfo_rooty() + 50) )
        
        # Main frame
        main_frame = ttk.Frame( todo_window )
        main_frame.pack( fill=tk.BOTH, expand=True, padx=10, pady=10 )
        
        # Title label
        title_label = ttk.Label( main_frame, text="Planned Features and Improvements", 
                                font=('TkDefaultFont', 12, 'bold') )
        title_label.pack( pady=(0, 10) )
        
        # Create scrollable text area for TODO items
        text_frame = ttk.Frame( main_frame )
        text_frame.pack( fill=tk.BOTH, expand=True )
        
        # Text widget with scrollbar
        todo_text = tk.Text( text_frame, wrap=tk.WORD, font=('TkDefaultFont', 10), 
                            state=tk.DISABLED, bg='#f8f8f8', relief=tk.FLAT, 
                            borderwidth=1, highlightthickness=1 )
        scrollbar = ttk.Scrollbar( text_frame, orient=tk.VERTICAL, command=todo_text.yview )
        todo_text.configure( yscrollcommand=scrollbar.set )
        
        todo_text.pack( side=tk.LEFT, fill=tk.BOTH, expand=True )
        scrollbar.pack( side=tk.RIGHT, fill=tk.Y )
        
        # TODO items
        todo_items = [
            "Search for duplicate files at different paths",
            "Sort images in filtered Images (and random order)",
            "Speed up large databases",
            "Fix caching when quick scrolling in image preview window",
            "Add database stats view that shows total files, total directories etc."
        ]
        
        # Populate the text widget
        todo_text.configure( state=tk.NORMAL )
        
        # Add header
        todo_text.insert( tk.END, "The following features and improvements are planned for future releases:\n\n" )
        
        # Add each TODO item
        for i, item in enumerate( todo_items, 1 ):
            todo_text.insert( tk.END, f"{i}. {item}\n\n" )
        
        # Add footer
        todo_text.insert( tk.END, "\nThese items are listed in no particular order of priority. " )
        todo_text.insert( tk.END, "Some may be implemented sooner than others based on user feedback and development priorities." )
        
        todo_text.configure( state=tk.DISABLED )
        
        # Button frame
        button_frame = ttk.Frame( main_frame )
        button_frame.pack( fill=tk.X, pady=(10, 0) )
        
        # Close button
        close_button = ttk.Button( button_frame, text="Close", command=todo_window.destroy )
        close_button.pack( side=tk.RIGHT )
        
        # Focus the window
        todo_window.focus_set()
    
    def start_visibility_checking( self ):
        """Start the continuous visibility checking for thumbnails"""
        # Stop any existing timer
        if self.visibility_check_timer:
            self.root.after_cancel( self.visibility_check_timer )
        
        # Clear existing timers
        self.visible_items_timer.clear()
        
        # Start checking
        self.check_visible_thumbnails()
    
    def get_thumbnail( self, filepath, size=(64, 64) ):
        """Generate or retrieve cached thumbnail for an image"""
        # Check thumbnail cache first
        cache_key = f"{filepath}_{size[0]}x{size[1]}"
        if cache_key in self.thumbnail_cache:
            return self.thumbnail_cache[cache_key]
        
        if not os.path.exists( filepath ):
            return None
        
        try:
            # Load and resize image
            with Image.open( filepath ) as img:
                # Apply EXIF orientation correction
                img = self.apply_exif_orientation( img )
                
                # Create thumbnail maintaining aspect ratio
                img.thumbnail( size, Image.Resampling.LANCZOS )
                
                # Convert to PhotoImage for Tkinter
                photo = ImageTk.PhotoImage( img )
                
                # Cache the thumbnail (limit cache size)
                if len( self.thumbnail_cache ) >= 500:  # Increased cache size for better performance
                    # Remove oldest entries
                    oldest_keys = list( self.thumbnail_cache.keys() )[:100]
                    for key in oldest_keys:
                        del self.thumbnail_cache[key]
                
                self.thumbnail_cache[cache_key] = photo
                return photo
                
        except Exception as e:
            print( f"Error creating thumbnail for {filepath}: {e}" )
            return None
    
    def clear_image_list( self ):
        """Clear all items from the image list"""
        if hasattr( self, 'virtual_image_list' ) and self.virtual_image_list:
            # Clear virtual list
            self.virtual_image_list.set_items( [] )
        
        # Clear compatibility attributes
        self.image_list_items.clear()
        self.selected_image_indices.clear()
    
    def add_image_list_item( self, filename, filepath=None ):
        """Add an item to the image list - compatibility method for virtual scrolling"""
        # This method is now handled by virtual scrolling in refresh_filtered_images
        # Keep for compatibility but functionality moved to virtual list
        return None
    
    def on_scrollbar_move( self, *args ):
        """Handle scrollbar movement"""
        # Move the canvas view
        self.image_list_canvas.yview( *args )
        # Visibility checking runs continuously, no need to trigger here
    
    def check_visible_thumbnails( self ):
        """Check for visible items and track how long they've been visible"""
        if not self.show_thumbnails.get():
            return
        
        current_time = time.time()
        visible_items = self.get_visible_image_items()
        visible_item_ids = {id(item) for item in visible_items}
        
        # Remove items that are no longer visible from the timer
        items_to_remove = []
        for item_id in self.visible_items_timer:
            if item_id not in visible_item_ids:
                items_to_remove.append( item_id )
        
        for item_id in items_to_remove:
            del self.visible_items_timer[item_id]
        
        # Update timers for currently visible items
        for item in visible_items:
            item_id = id( item )
            if item_id not in self.visible_items_timer:
                # First time seeing this item
                self.visible_items_timer[item_id] = current_time
            else:
                # Check if item has been visible for 200ms
                time_visible = current_time - self.visible_items_timer[item_id]
                if (time_visible >= 0.3 and  # Increased delay to reduce CPU usage 
                    not item['thumbnail_loaded'] and 
                    item not in self.thumbnail_load_queue and 
                    item['filepath'] and 
                    os.path.exists( item['filepath'] )):
                    # Add to queue after 200ms delay
                    self.thumbnail_load_queue.append( item )
        
        # Start processing if we have items and not already running
        if self.thumbnail_load_queue and not self.thumbnail_loading:
            self.root.after( 10, self.process_thumbnail_queue )
        
        # Schedule next visibility check
        if self.visibility_check_timer:
            self.root.after_cancel( self.visibility_check_timer )
        self.visibility_check_timer = self.root.after( 200, self.check_visible_thumbnails )  # Reduced frequency for better performance
    
    def process_thumbnail_queue( self ):
        """Process the thumbnail loading queue lazily"""
        if not self.thumbnail_load_queue or not self.show_thumbnails.get():
            self.thumbnail_loading = False
            return
            
        self.thumbnail_loading = True
        
        # Get the next item to process (prioritize visible items)
        item_to_load = None
        visible_items = self.get_visible_image_items()
        
        # First, try to find a visible item that needs thumbnail loading
        for item in self.thumbnail_load_queue:
            if item in visible_items and not item['thumbnail_loaded']:
                item_to_load = item
                break
        
        # If no visible items need loading, take the first item from queue
        if not item_to_load:
            for item in self.thumbnail_load_queue:
                if not item['thumbnail_loaded']:
                    item_to_load = item
                    break
        
        if item_to_load:
            self.load_single_thumbnail( item_to_load )
            # Remove from queue if loaded or failed
            if item_to_load in self.thumbnail_load_queue:
                self.thumbnail_load_queue.remove( item_to_load )
        
        # Continue processing queue
        if self.thumbnail_load_queue:
            self.root.after( 50, self.process_thumbnail_queue )  # Small delay between loads
        else:
            self.thumbnail_loading = False
    
    def load_single_thumbnail( self, item_data ):
        """Load thumbnail for a single item"""
        if not item_data['filepath'] or not os.path.exists( item_data['filepath'] ):
            item_data['thumbnail_loaded'] = True  # Mark as processed even if failed
            return
        
        try:
            thumbnail = self.get_thumbnail( item_data['filepath'] )
            if thumbnail and item_data['thumb_label']:
                # Update the placeholder with the actual thumbnail
                item_data['thumb_label'].configure( image=thumbnail, text="", width=0, height=0 )
                item_data['thumb_label'].image = thumbnail  # Keep reference
                item_data['thumbnail_loaded'] = True
        except Exception as e:
            print( f"Error loading thumbnail for {item_data['filename']}: {e}" )
            item_data['thumbnail_loaded'] = True  # Mark as processed even if failed
            # Remove from queue to prevent retries
            if item_data in self.thumbnail_load_queue:
                self.thumbnail_load_queue.remove( item_data )
    
    def get_visible_image_items( self ):
        """Get list of currently visible image items in the canvas"""
        if not hasattr( self, 'image_list_canvas' ):
            return []
        
        try:
            # Get canvas viewport
            canvas_top = self.image_list_canvas.canvasy( 0 )
            canvas_bottom = canvas_top + self.image_list_canvas.winfo_height()
            
            visible_items = []
            for item in self.image_list_items:
                if item['frame'] and item['frame'].winfo_exists():
                    item_top = item['frame'].winfo_y()
                    item_bottom = item_top + item['frame'].winfo_height()
                    
                    # Check if item is visible in viewport
                    if item_bottom >= canvas_top and item_top <= canvas_bottom:
                        visible_items.append( item )
            
            return visible_items
        except Exception:
            return []
    
    def on_image_list_click( self, index, event ):
        """Handle click on image list item"""
        if 0 <= index < len( self.image_list_items ):
            # Handle multi-selection with Ctrl/Shift
            if event.state & 0x4:  # Ctrl key
                # Toggle selection
                self.toggle_image_list_selection( index )
            elif event.state & 0x1:  # Shift key
                # Range selection
                if self.selected_image_indices:
                    start = min( self.selected_image_indices )
                    end = max( index, start )
                    self.clear_image_list_selection()
                    for i in range( start, end + 1 ):
                        self.select_image_list_item( i )
                else:
                    self.select_image_list_item( index )
            else:
                # Single selection
                self.clear_image_list_selection()
                self.select_image_list_item( index )
            
            # Trigger selection event
            self.on_database_image_select( None )
    
    def on_image_list_double_click( self, index, event ):
        """Handle double click on image list item"""
        if 0 <= index < len( self.image_list_items ):
            filename = self.image_list_items[index]['filename']
            filepath = self.find_image_path( filename )
            if filepath:
                self.enter_fullscreen_mode( filepath )
    
    def select_image_list_item( self, index ):
        """Select an image list item"""
        if 0 <= index < len( self.image_list_items ):
            item = self.image_list_items[index]
            if not item['selected']:
                item['selected'] = True
                item['content_frame'].configure( bg='lightblue' )
                # Update all child widgets to match selection color
                for child in item['content_frame'].winfo_children():
                    child.configure( bg='lightblue' )
                if index not in self.selected_image_indices:
                    self.selected_image_indices.append( index )
    
    def deselect_image_list_item( self, index ):
        """Deselect an image list item"""
        if 0 <= index < len( self.image_list_items ):
            item = self.image_list_items[index]
            if item['selected']:
                item['selected'] = False
                item['content_frame'].configure( bg='white' )
                # Update all child widgets to match deselection color
                for child in item['content_frame'].winfo_children():
                    child.configure( bg='white' )
                if index in self.selected_image_indices:
                    self.selected_image_indices.remove( index )
    
    def toggle_image_list_selection( self, index ):
        """Toggle selection of an image list item"""
        if 0 <= index < len( self.image_list_items ):
            if self.image_list_items[index]['selected']:
                self.deselect_image_list_item( index )
            else:
                self.select_image_list_item( index )
    
    def clear_image_list_selection( self ):
        """Clear all selections in the image list"""
        for index in list( self.selected_image_indices ):
            self.deselect_image_list_item( index )
    
    # Compatibility methods to work with existing code that expects listbox interface
    class DatabaseImageListboxCompat:
        """Compatibility wrapper to make virtual image list work like old listbox"""
        def __init__( self, parent ):
            self.parent = parent
        
        def curselection( self ):
            """Return selected indices like listbox.curselection()"""
            if hasattr( self.parent, 'virtual_image_list' ) and self.parent.virtual_image_list:
                return tuple( sorted( self.parent.virtual_image_list.selected_indices ) )
            return tuple( self.parent.selected_image_indices )
        
        def get( self, index ):
            """Get filename at index like listbox.get()"""
            if hasattr( self.parent, 'virtual_image_list' ) and self.parent.virtual_image_list:
                if 0 <= index < len( self.parent.virtual_image_list.filtered_items ):
                    return self.parent.virtual_image_list.filtered_items[index]['filename']
            return ""
        
        def size( self ):
            """Return number of items like listbox.size()"""
            if hasattr( self.parent, 'virtual_image_list' ) and self.parent.virtual_image_list:
                return len( self.parent.virtual_image_list.filtered_items )
            return 0
        
        def selection_clear( self, start, end=None ):
            """Clear selection like listbox.selection_clear()"""
            if hasattr( self.parent, 'virtual_image_list' ) and self.parent.virtual_image_list:
                if start == 0 and end == tk.END:
                    self.parent.virtual_image_list.selected_indices.clear()
                    self.parent.virtual_image_list.update_selection_display()
                    
                    # For TreeviewImageList, also clear the actual treeview selection
                    if hasattr( self.parent.virtual_image_list, 'treeview' ):
                        self.parent.virtual_image_list.treeview.selection_remove(
                            self.parent.virtual_image_list.treeview.selection()
                        )
        
        def selection_set( self, index ):
            """Set selection like listbox.selection_set()"""
            if hasattr( self.parent, 'virtual_image_list' ) and self.parent.virtual_image_list:
                self.parent.virtual_image_list.selected_indices.add( index )
                self.parent.virtual_image_list.update_selection_display()
                
                # For TreeviewImageList, also update the actual treeview selection
                if hasattr( self.parent.virtual_image_list, 'treeview' ):
                    if 0 <= index < len( self.parent.virtual_image_list.filtered_items ):
                        item_id = str( index )
                        self.parent.virtual_image_list.treeview.selection_set( item_id )
        
        def see( self, index ):
            """Scroll to make item visible like listbox.see()"""
            if hasattr( self.parent, 'virtual_image_list' ) and self.parent.virtual_image_list:
                if 0 <= index < len( self.parent.virtual_image_list.filtered_items ):
                    # For TreeviewImageList, use the treeview's see method
                    if hasattr( self.parent.virtual_image_list, 'treeview' ):
                        # Get the item ID for this index
                        children = self.parent.virtual_image_list.treeview.get_children()
                        if index < len( children ):
                            item_id = children[index]
                            self.parent.virtual_image_list.treeview.see( item_id )
        
        def bind( self, event, callback ):
            """Bind events - for compatibility, but events are handled in the new system"""
            pass  # Events are handled by individual item frames
        
        def unbind( self, event ):
            """Unbind events - for compatibility"""
            pass  # Events are handled by individual item frames
    
    # database_image_listbox is now initialized directly in setup_database_tab
    
    def load_image_metadata_lazy( self, filepath ):
        """Lazily load image metadata (rating, tags, dimensions) with caching"""
        # Check cache first
        cached = self.get_cached_image_metadata( filepath )
        if cached:
            return cached
        
        # Load from database
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            # Get image basic info
            relative_path = os.path.relpath( filepath, self.current_database )
            cursor.execute( "SELECT id, rating, width, height FROM images WHERE relative_path = ? ORDER BY id DESC", (relative_path,) )
            result = cursor.fetchone()  # This will get the highest (most recent) ID
            
            if not result:
                conn.close()
                return None
            
            image_id, rating, width, height = result
            
            # Get image tags
            cursor.execute( '''
                SELECT t.name FROM tags t
                JOIN image_tags it ON t.id = it.tag_id
                WHERE it.image_id = ?
                ORDER BY t.name
            ''', (image_id,) )
            tags = [row[0] for row in cursor.fetchall()]
            
            conn.close()
            
            # Create metadata object
            metadata = {
                'id': image_id,
                'rating': rating or 0,
                'width': width,
                'height': height,
                'tags': tags,
                'filepath': filepath
            }
            
            # Cache the metadata
            self.cache_image_metadata( filepath, metadata )
            
            return metadata
            
        except Exception as e:
            print( f"Error loading metadata for {filepath}: {e}" )
            return None
    
    def load_all_tags_lazy( self ):
        """Lazily load all available tags with caching"""
        # Check cache first
        cached = self.get_cached_tags()
        if cached:
            return cached
        
        # Load from database
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            # Get only tags that are actually used by files in the database
            cursor.execute( """
                SELECT DISTINCT t.id, t.name 
                FROM tags t 
                INNER JOIN image_tags it ON t.id = it.tag_id 
                INNER JOIN images i ON it.image_id = i.id 
                ORDER BY t.name
            """ )
            tags = cursor.fetchall()
            
            conn.close()
            
            # Cache the tags
            self.cache_tags( tags )
            
            return tags
            
        except Exception as e:
            print( f"Error loading tags: {e}" )
            return []
    
    def set_default_window_geometry( self ):
        """Set default window size and center it on screen"""
        try:
            default_width = 1000
            default_height = 700
            
            # Get screen dimensions
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            
            # Center the window
            x = (screen_width - default_width) // 2
            y = (screen_height - default_height) // 2
            
            geometry = f"{default_width}x{default_height}+{x}+{y}"

            self.root.geometry( geometry )
            
        except Exception as e:
            print( f"Error setting default geometry: {e}" )
    
    def is_position_valid( self, x, y, width, height ):
        """Check if window position is valid for multi-monitor setups"""
        try:
            # Get primary screen dimensions
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            
            # Basic sanity checks first
            if width <= 0 or height <= 0:
                return False
            if width > screen_width * 3 or height > screen_height * 3:
                return False  # Unreasonably large window
                
            # For multi-monitor setups, be very permissive with coordinates
            # Modern setups can have monitors arranged in various configurations:
            # - Secondary monitor to the right: x can be 1920, 2560, 3840, etc.
            # - Secondary monitor to the left: x can be negative (-1920, etc.)
            # - Secondary monitor above: y can be negative
            # - Secondary monitor below: y can be large positive
            
            # Allow very wide range for X coordinates (horizontal multi-monitor)
            max_x = screen_width * 6  # Support up to 6 monitors horizontally
            min_x = -screen_width * 3  # Support monitors to the left
            
            # Allow reasonable range for Y coordinates (vertical arrangements less common)
            max_y = screen_height * 3  # Support stacked monitors
            min_y = -screen_height * 2  # Support monitors above
            
            if x < min_x or x > max_x:
                return False
            if y < min_y or y > max_y:
                return False
            
            # Additional check: ensure at least part of window would be theoretically visible
            # Window is completely off-screen if:
            # - Right edge is before virtual desktop left edge
            # - Left edge is after virtual desktop right edge  
            # - Bottom edge is before virtual desktop top edge
            # - Top edge is after virtual desktop bottom edge
            
            # For now, if it passes the basic range checks above, accept it
            # tkinter will handle placing it appropriately if the monitor is disconnected
            return True
            
        except Exception as e:
            print( f"Error validating position: {e}" )
            return False
    
    def enforce_scrollable_minimums( self ):
        """Ensure scrollable areas maintain minimum height (2 rows ≈ 50px)"""
        try:
            min_canvas_height = 50  # Minimum height for 2 rows
            
            # Enforce minimum height for tag filter canvas
            if hasattr( self, 'tag_canvas' ):
                current_height = self.tag_canvas.winfo_height()
                if current_height > 1 and current_height < min_canvas_height:
                    self.tag_canvas.configure( height=min_canvas_height )
            
            # Enforce minimum height for image tag canvas
            if hasattr( self, 'image_tag_canvas' ):
                current_height = self.image_tag_canvas.winfo_height()
                if current_height > 1 and current_height < min_canvas_height:
                    self.image_tag_canvas.configure( height=min_canvas_height )
                    
        except Exception as e:
            print( f"Error enforcing scrollable minimums: {e}" )
    

    
    def save_paned_positions_only( self ):
        """Save paned positions, window geometry, tab state, and database to settings (called during resize)"""
        try:
            settings = {}
            if os.path.exists( self.settings_file ):
                with open( self.settings_file, 'r' ) as f:
                    settings = json.load( f )
            
            self.save_paned_positions( settings )
            self.save_window_geometry( settings )
            self.save_active_tab( settings )
            self.save_current_database( settings )
            
            with open( self.settings_file, 'w' ) as f:
                json.dump( settings, f, indent=2 )
                
        except Exception as e:
            print( f"Error saving paned positions: {e}" )
            
    def on_closing( self ):
        """Handle application closing"""
        # Save current directory and all state before closing
        if self.current_browse_directory:
            self.save_current_directory( self.current_browse_directory )
        elif hasattr( self, 'drive_var' ) and self.drive_var.get():
            self.save_current_directory( self.drive_var.get() )
        else:
            # If no directory to save, still save other state (window, paned positions, active tab)
            self.save_paned_positions_only()
        
        self.root.destroy()

    # Rating methods for keyboard shortcuts
    def rate_current_browse_image( self, rating ):
        """Rate the currently displayed browse image"""
        if not self.current_browse_image:
            return
            
        # Check if current image is in a database
        if not self.current_database_path:
            # Create a database in the current directory for rating
            db_path = os.path.join( self.current_browse_directory, "ratings.db" )
            if not os.path.exists( db_path ):
                try:
                    conn = sqlite3.connect( db_path )
                    cursor = conn.cursor()
                    
                    # Create tables
                    cursor.execute( """CREATE TABLE images (
                        id INTEGER PRIMARY KEY,
                        filename TEXT UNIQUE,
                        relative_path TEXT,
                        width INTEGER,
                        height INTEGER,
                        rating INTEGER DEFAULT 0
                    )""" )
                    
                    cursor.execute( """CREATE TABLE tags (
                        id INTEGER PRIMARY KEY,
                        name TEXT UNIQUE
                    )""" )
                    
                    cursor.execute( """CREATE TABLE image_tags (
                        image_id INTEGER,
                        tag_id INTEGER,
                        FOREIGN KEY (image_id) REFERENCES images (id),
                        FOREIGN KEY (tag_id) REFERENCES tags (id),
                        PRIMARY KEY (image_id, tag_id)
                    )""" )
                    
                    conn.commit()
                    conn.close()
                    
                    # Open the new database
                    self.open_database_file( db_path )
                    
                except Exception as e:
                    print( f"Error creating rating database: {e}" )
                    return
        
        self._rate_image_by_path( self.current_browse_image, rating )
    
    def rate_current_database_image( self, rating ):
        """Rate the currently displayed database image"""
        if not self.current_database_image or not self.current_database_path:
            return
        
        self._rate_image_by_path( self.current_database_image, rating )
    
    def rate_current_fullscreen_image( self, rating ):
        """Rate the currently displayed fullscreen image"""
        if not self.fullscreen_images or self.fullscreen_index >= len( self.fullscreen_images ):
            return
        
        current_image = self.fullscreen_images[self.fullscreen_index]
        self._rate_image_by_path( current_image, rating )
    
    def _rate_image_by_path( self, image_path, rating ):
        """Helper method to rate an image by its file path"""
        if not self.current_database_path:
            return
            
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            # Get relative path
            relative_path = os.path.relpath( image_path, os.path.dirname( self.current_database_path ) )
            filename = os.path.basename( image_path )
            
            # Check if image exists in database
            cursor.execute( "SELECT id FROM images WHERE filename = ? OR relative_path = ?", (filename, relative_path) )
            result = cursor.fetchone()
            
            if result:
                # Update existing image
                cursor.execute( "UPDATE images SET rating = ? WHERE id = ?", (rating, result[0]) )
                
                # Invalidate cache entry for this image so it gets fresh data next time
                if image_path in self.image_metadata_cache:
                    del self.image_metadata_cache[image_path]
            else:
                # Add new image to database
                try:
                    image = Image.open( image_path )
                    width, height = image.size
                    image.close()
                    
                    cursor.execute( "INSERT INTO images (filename, relative_path, width, height, rating) VALUES (?, ?, ?, ?, ?)",
                                  (filename, relative_path, width, height, rating) )
                except Exception as e:
                    print( f"Error adding image to database: {e}" )
                    return
            
            conn.commit()
            
            # Update UI if this is the selected image in database tab
            if self.selected_image_files and image_path in self.selected_image_files:
                self.image_rating_var.set( rating )
            
            # Check if we need to refresh filtered images (only if rating filters are active)
            min_rating = self.min_rating_var.get()
            max_rating = self.max_rating_var.get()
            has_rating_filter = min_rating > 0 or max_rating < 10
            
            if has_rating_filter:
                # Rating filters are active, need to refresh to potentially hide/show items
                self.refresh_filtered_images()
            else:
                # No rating filters, just update the rating display without full refresh
                # The rating change doesn't affect which items are shown
                pass
            
        except Exception as e:
            print( f"Error rating image: {e}" )
        finally:
            conn.close()
    
    def adjust_current_browse_rating( self, delta ):
        """Adjust the rating of the current browse image by delta"""
        if not self.current_browse_image or not self.current_database_path:
            return
        
        current_rating = self._get_image_rating( self.current_browse_image )
        new_rating = max( 0, min( 10, current_rating + delta ) )
        self.rate_current_browse_image( new_rating )
    
    def adjust_current_database_rating( self, delta ):
        """Adjust the rating of the current database image by delta"""
        if not self.current_database_image or not self.current_database_path:
            return
        
        current_rating = self._get_image_rating( self.current_database_image )
        new_rating = max( 0, min( 10, current_rating + delta ) )
        self.rate_current_database_image( new_rating )
    
    def adjust_current_fullscreen_rating( self, delta ):
        """Adjust the rating of the current fullscreen image by delta"""
        if not self.fullscreen_images or self.fullscreen_index >= len( self.fullscreen_images ):
            return
        
        current_image = self.fullscreen_images[self.fullscreen_index]
        current_rating = self._get_image_rating( current_image )
        new_rating = max( 0, min( 10, current_rating + delta ) )
        self.rate_current_fullscreen_image( new_rating )
    
    def _get_image_rating( self, image_path ):
        """Get the current rating of an image"""
        if not self.current_database_path:
            return 0
        
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            relative_path = os.path.relpath( image_path, os.path.dirname( self.current_database_path ) )
            filename = os.path.basename( image_path )
            
            cursor.execute( "SELECT rating FROM images WHERE filename = ? OR relative_path = ?", (filename, relative_path) )
            result = cursor.fetchone()
            
            return result[0] if result else 0
            
        except Exception as e:
            print( f"Error getting image rating: {e}" )
            return 0
        finally:
            conn.close()
    
    def on_rating_arrow_press( self, event ):
        """Handle arrow key press for rating adjustment with long press support"""
        if event.keysym == 'Left':
            delta = -1
        elif event.keysym == 'Right':
            delta = 1
        else:
            return
        
        # Determine which rating adjustment method to use based on focus
        widget = event.widget
        if widget == self.browse_preview_label:
            self.adjust_current_browse_rating( delta )
            adjust_method = lambda: self.adjust_current_browse_rating( delta )
        elif widget == self.database_preview_label:
            self.adjust_current_database_rating( delta )
            adjust_method = lambda: self.adjust_current_database_rating( delta )
        else:
            return
        
        # Start repeat timer for long press (500ms intervals)
        self._rating_repeat_timer = self.root.after( 500, self._rating_repeat, adjust_method )
    
    def on_rating_arrow_release( self, event ):
        """Handle arrow key release to stop long press rating adjustment"""
        if self._rating_repeat_timer:
            self.root.after_cancel( self._rating_repeat_timer )
            self._rating_repeat_timer = None
    
    def _rating_repeat( self, adjust_method ):
        """Repeat rating adjustment for long press"""
        adjust_method()
        # Schedule next repeat
        self._rating_repeat_timer = self.root.after( 500, self._rating_repeat, adjust_method )

class TagDialog:
    def __init__( self, parent, filepath, database_path ):
        self.filepath = filepath
        self.database_path = database_path
        self.filename = os.path.basename( filepath )
        
        # Create dialog window
        self.dialog = tk.Toplevel( parent )
        self.dialog.title( f"Tags for {self.filename}" )
        self.dialog.geometry( "450x600" )
        self.dialog.transient( parent )
        self.dialog.grab_set()
        
        # Center the dialog
        self.dialog.geometry( "+%d+%d" % (parent.winfo_rootx() + 50, parent.winfo_rooty() + 50) )
        
        self.setup_dialog()
        self.load_tags()
        
    def setup_dialog( self ):
        """Setup the tag dialog interface"""
        # Existing tags section (fixed height to ensure buttons remain visible)
        existing_frame = ttk.LabelFrame( self.dialog, text="Existing Tags" )
        existing_frame.pack( fill=tk.X, padx=10, pady=5 )
        
        # Scrollable frame for tag checkboxes with fixed height
        tag_canvas_frame = ttk.Frame( existing_frame )
        tag_canvas_frame.pack( fill=tk.X, pady=5 )
        
        self.tag_canvas = tk.Canvas( tag_canvas_frame, height=200 )
        tag_scrollbar = ttk.Scrollbar( tag_canvas_frame, orient=tk.VERTICAL, command=self.tag_canvas.yview )
        self.tag_scrollable_frame = ttk.Frame( self.tag_canvas )
        
        self.tag_scrollable_frame.bind( "<Configure>", lambda e: self.tag_canvas.configure( scrollregion=self.tag_canvas.bbox( "all" ) ) )
        self.tag_canvas.create_window( (0, 0), window=self.tag_scrollable_frame, anchor="nw" )
        self.tag_canvas.configure( yscrollcommand=tag_scrollbar.set )
        
        self.tag_canvas.pack( side=tk.LEFT, fill=tk.BOTH, expand=True )
        tag_scrollbar.pack( side=tk.RIGHT, fill=tk.Y )
        
        # Enable mouse wheel scrolling
        def on_canvas_scroll( event ):
            self.tag_canvas.yview_scroll( int( -1 * (event.delta / 120) ), "units" )
        self.tag_canvas.bind( "<MouseWheel>", on_canvas_scroll )
        
        # New tags section
        new_frame = ttk.LabelFrame( self.dialog, text="Add New Tags" )
        new_frame.pack( fill=tk.X, padx=10, pady=5 )
        
        ttk.Label( new_frame, text="Enter new tags (comma-separated):" ).pack( anchor=tk.W )
        self.new_tags_entry = tk.Entry( new_frame, width=50 )
        self.new_tags_entry.pack( fill=tk.X, pady=5 )
        
        # Rating section
        rating_frame = ttk.LabelFrame( self.dialog, text="Rating (1-10)" )
        rating_frame.pack( fill=tk.X, padx=10, pady=5 )
        
        self.rating_var = tk.IntVar( value=0 )
        rating_scale = tk.Scale( rating_frame, from_=0, to=10, orient=tk.HORIZONTAL, variable=self.rating_var )
        rating_scale.pack( fill=tk.X )
        
        # Buttons (always visible at bottom)
        button_frame = ttk.Frame( self.dialog )
        button_frame.pack( side=tk.BOTTOM, fill=tk.X, padx=10, pady=10 )
        
        ttk.Button( button_frame, text="Save", command=self.save_tags ).pack( side=tk.RIGHT, padx=5 )
        ttk.Button( button_frame, text="Cancel", command=self.dialog.destroy ).pack( side=tk.RIGHT )
        
    def load_tags( self ):
        """Load existing tags and current image tags"""
        try:
            conn = sqlite3.connect( self.database_path )
            cursor = conn.cursor()
            
            # Get image ID
            relative_path = os.path.relpath( self.filepath, os.path.dirname( self.database_path ) )
            cursor.execute( "SELECT id, rating FROM images WHERE relative_path = ?", (relative_path,) )
            result = cursor.fetchone()
            
            if not result:
                messagebox.showerror( "Error", "Image not found in database" )
                self.dialog.destroy()
                return
                
            self.image_id = result[0]
            self.rating_var.set( result[1] or 0 )
            
            # Get only tags that are actually used by files in the database
            cursor.execute( """
                SELECT DISTINCT t.id, t.name 
                FROM tags t 
                INNER JOIN image_tags it ON t.id = it.tag_id 
                INNER JOIN images i ON it.image_id = i.id 
                ORDER BY t.name
            """ )
            all_tags = cursor.fetchall()
            
            # Get current image tags
            cursor.execute( '''
                SELECT t.name FROM tags t
                JOIN image_tags it ON t.id = it.tag_id
                WHERE it.image_id = ?
            ''', (self.image_id,) )
            current_tags = {row[0] for row in cursor.fetchall()}
            
            # Clear existing checkboxes
            for widget in self.tag_scrollable_frame.winfo_children():
                widget.destroy()
                
            # Create checkboxes for each tag
            self.tag_checkboxes = {}
            for i, (tag_id, tag_name) in enumerate( all_tags ):
                tag_var = tk.BooleanVar( value=tag_name in current_tags )
                
                tag_frame = ttk.Frame( self.tag_scrollable_frame )
                tag_frame.pack( fill=tk.X, pady=1 )
                
                checkbox = tk.Checkbutton( tag_frame, text=tag_name, variable=tag_var )
                checkbox.pack( side=tk.LEFT, anchor=tk.W )
                
                self.tag_checkboxes[tag_id] = {
                    'var': tag_var,
                    'name': tag_name,
                    'checkbox': checkbox
                }
                    
            conn.close()
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to load tags: {str(e)}" )
            self.dialog.destroy()
            
    def save_tags( self ):
        """Save tag changes to database"""
        try:
            conn = sqlite3.connect( self.database_path )
            cursor = conn.cursor()
            
            # Update rating
            cursor.execute( "UPDATE images SET rating = ? WHERE id = ?", (self.rating_var.get(), self.image_id) )
            
            # Clear existing tags for this image
            cursor.execute( "DELETE FROM image_tags WHERE image_id = ?", (self.image_id,) )
            
            # Add selected existing tags
            for tag_id, tag_data in self.tag_checkboxes.items():
                if tag_data['var'].get():
                    cursor.execute( "INSERT INTO image_tags (image_id, tag_id) VALUES (?, ?)", (self.image_id, tag_id) )
                    
            # Add new tags
            new_tags_text = self.new_tags_entry.get().strip()
            if new_tags_text:
                new_tags = [tag.strip() for tag in new_tags_text.split( ',' ) if tag.strip()]
                
                for tag_name in new_tags:
                    # Insert tag if it doesn't exist
                    cursor.execute( "INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,) )
                    
                    # Get tag ID
                    cursor.execute( "SELECT id FROM tags WHERE name = ?", (tag_name,) )
                    tag_id = cursor.fetchone()[0]
                    
                    # Link tag to image
                    cursor.execute( "INSERT OR IGNORE INTO image_tags (image_id, tag_id) VALUES (?, ?)", (self.image_id, tag_id) )
                    
            conn.commit()
            conn.close()
            
            self.dialog.destroy()
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to save tags: {str(e)}" )

class MultiTagDialog:
    def __init__( self, parent, filepaths, database_path ):
        self.filepaths = filepaths
        self.database_path = database_path
        self.filenames = [os.path.basename( fp ) for fp in filepaths]
        
        # Create dialog window
        self.dialog = tk.Toplevel( parent )
        self.dialog.title( f"Tags for {len(filepaths)} images" )
        self.dialog.geometry( "550x700" )
        self.dialog.transient( parent )
        self.dialog.grab_set()
        
        # Center the dialog
        self.dialog.geometry( "+%d+%d" % (parent.winfo_rootx() + 50, parent.winfo_rooty() + 50) )
        
        self.setup_dialog()
        self.load_tags()
        
    def setup_dialog( self ):
        """Setup the multi-tag dialog interface"""
        # File list section
        files_frame = ttk.LabelFrame( self.dialog, text=f"Selected Files ({len(self.filepaths)})" )
        files_frame.pack( fill=tk.X, padx=10, pady=5 )
        
        files_listbox = tk.Listbox( files_frame, height=4 )
        files_scrollbar = ttk.Scrollbar( files_frame, orient=tk.VERTICAL, command=files_listbox.yview )
        files_listbox.configure( yscrollcommand=files_scrollbar.set )
        
        for filename in self.filenames:
            files_listbox.insert( tk.END, filename )
            
        files_listbox.pack( side=tk.LEFT, fill=tk.BOTH, expand=True )
        files_scrollbar.pack( side=tk.RIGHT, fill=tk.Y )
        
        # Existing tags section (fixed height to ensure buttons remain visible)
        existing_frame = ttk.LabelFrame( self.dialog, text="Existing Tags" )
        existing_frame.pack( fill=tk.X, padx=10, pady=5 )
        
        # Scrollable frame for tag checkboxes with fixed height
        tag_canvas_frame = ttk.Frame( existing_frame )
        tag_canvas_frame.pack( fill=tk.X, pady=5 )
        
        self.tag_canvas = tk.Canvas( tag_canvas_frame, height=250 )
        tag_scrollbar = ttk.Scrollbar( tag_canvas_frame, orient=tk.VERTICAL, command=self.tag_canvas.yview )
        self.tag_scrollable_frame = ttk.Frame( self.tag_canvas )
        
        self.tag_scrollable_frame.bind( "<Configure>", lambda e: self.tag_canvas.configure( scrollregion=self.tag_canvas.bbox( "all" ) ) )
        self.tag_canvas.create_window( (0, 0), window=self.tag_scrollable_frame, anchor="nw" )
        self.tag_canvas.configure( yscrollcommand=tag_scrollbar.set )
        
        self.tag_canvas.pack( side=tk.LEFT, fill=tk.BOTH, expand=True )
        tag_scrollbar.pack( side=tk.RIGHT, fill=tk.Y )
        
        # Enable mouse wheel scrolling
        def on_canvas_scroll( event ):
            self.tag_canvas.yview_scroll( int( -1 * (event.delta / 120) ), "units" )
        self.tag_canvas.bind( "<MouseWheel>", on_canvas_scroll )
        
        # New tags section
        new_frame = ttk.LabelFrame( self.dialog, text="Add New Tags" )
        new_frame.pack( fill=tk.X, padx=10, pady=5 )
        
        ttk.Label( new_frame, text="Enter new tags (comma-separated):" ).pack( anchor=tk.W )
        self.new_tags_entry = tk.Entry( new_frame, width=50 )
        self.new_tags_entry.pack( fill=tk.X, pady=5 )
        
        # Rating section
        rating_frame = ttk.LabelFrame( self.dialog, text="Rating (1-10)" )
        rating_frame.pack( fill=tk.X, padx=10, pady=5 )
        
        self.rating_var = tk.IntVar( value=0 )
        self.rating_scale = tk.Scale( rating_frame, from_=0, to=10, orient=tk.HORIZONTAL, variable=self.rating_var )
        self.rating_scale.pack( fill=tk.X )
        
        # Buttons (always visible at bottom)
        button_frame = ttk.Frame( self.dialog )
        button_frame.pack( side=tk.BOTTOM, fill=tk.X, padx=10, pady=10 )
        
        ttk.Button( button_frame, text="Save", command=self.save_tags ).pack( side=tk.RIGHT, padx=5 )
        ttk.Button( button_frame, text="Cancel", command=self.dialog.destroy ).pack( side=tk.RIGHT )
        
    def load_tags( self ):
        """Load existing tags and analyze common/partial tags across selected images"""
        try:
            conn = sqlite3.connect( self.database_path )
            cursor = conn.cursor()
            
            # Get image IDs and their ratings
            self.image_data = {}
            ratings = []
            
            for filepath in self.filepaths:
                relative_path = os.path.relpath( filepath, os.path.dirname( self.database_path ) )
                cursor.execute( "SELECT id, rating FROM images WHERE relative_path = ?", (relative_path,) )
                result = cursor.fetchone()
                
                if result:
                    self.image_data[filepath] = {'id': result[0], 'rating': result[1] or 0}
                    ratings.append( result[1] or 0 )
                    
            # Handle ratings
            if ratings:
                unique_ratings = set( ratings )
                if len( unique_ratings ) == 1:
                    # All images have same rating
                    self.rating_var.set( ratings[0] )
                else:
                    # Different ratings - grey out scale
                    self.rating_scale.configure( state='disabled', bg='lightgrey' )
                    self.rating_var.set( 0 )
                    
            # Get only tags that are actually used by files in the database
            cursor.execute( """
                SELECT DISTINCT t.id, t.name 
                FROM tags t 
                INNER JOIN image_tags it ON t.id = it.tag_id 
                INNER JOIN images i ON it.image_id = i.id 
                ORDER BY t.name
            """ )
            all_tags = cursor.fetchall()
            
            # For each tag, count how many selected images have it
            tag_counts = {}
            total_images = len( [img for img in self.image_data.values()] )
            
            for tag_id, tag_name in all_tags:
                cursor.execute( '''
                    SELECT COUNT(*) FROM image_tags it 
                    WHERE it.tag_id = ? AND it.image_id IN ({})
                '''.format( ','.join( ['?'] * len( self.image_data ) ) ), 
                [tag_id] + [img['id'] for img in self.image_data.values()] )
                
                count = cursor.fetchone()[0]
                tag_counts[tag_name] = count
                
            # Clear existing checkboxes
            for widget in self.tag_scrollable_frame.winfo_children():
                widget.destroy()
                
            # Create checkboxes with visual indicators
            self.tag_checkboxes = {}
            for tag_id, tag_name in all_tags:
                count = tag_counts[tag_name]
                
                tag_frame = ttk.Frame( self.tag_scrollable_frame )
                tag_frame.pack( fill=tk.X, pady=1 )
                
                if count == total_images:
                    # All images have this tag - normal checked checkbox
                    tag_var = tk.BooleanVar( value=True )
                    checkbox = tk.Checkbutton( tag_frame, text=tag_name, variable=tag_var )
                    checkbox.pack( side=tk.LEFT, anchor=tk.W )
                    
                    self.tag_checkboxes[tag_id] = {
                        'var': tag_var,
                        'name': tag_name,
                        'checkbox': checkbox,
                        'state': 'common',
                        'frame': tag_frame
                    }
                elif count > 0:
                    # Some images have this tag - greyed out checkbox
                    tag_var = tk.BooleanVar( value=False )
                    checkbox = tk.Checkbutton( tag_frame, text=tag_name, variable=tag_var, 
                                             fg='grey', selectcolor='lightgrey',
                                             command=lambda tid=tag_id: self.on_partial_checkbox_clicked( tid ) )
                    checkbox.pack( side=tk.LEFT, anchor=tk.W )
                    
                    self.tag_checkboxes[tag_id] = {
                        'var': tag_var,
                        'name': tag_name,
                        'checkbox': checkbox,
                        'state': 'partial',
                        'frame': tag_frame
                    }
                else:
                    # No images have this tag - normal unchecked checkbox
                    tag_var = tk.BooleanVar( value=False )
                    checkbox = tk.Checkbutton( tag_frame, text=tag_name, variable=tag_var )
                    checkbox.pack( side=tk.LEFT, anchor=tk.W )
                    
                    self.tag_checkboxes[tag_id] = {
                        'var': tag_var,
                        'name': tag_name,
                        'checkbox': checkbox,
                        'state': 'none',
                        'frame': tag_frame
                    }
                    
            conn.close()
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to load tags: {str(e)}" )
            self.dialog.destroy()
            
    def on_partial_checkbox_clicked( self, tag_id ):
        """Handle clicking on a greyed out (partial) checkbox"""
        if tag_id in self.tag_checkboxes and self.tag_checkboxes[tag_id]['state'] == 'partial':
            tag_data = self.tag_checkboxes[tag_id]
            tag_name = tag_data['name']
            tag_frame = tag_data['frame']
            current_value = tag_data['var'].get()
            
            # Destroy the old greyed checkbox
            tag_data['checkbox'].destroy()
            
            # Create a new normal checkbox with the current state
            new_var = tk.BooleanVar( value=current_value )
            new_checkbox = tk.Checkbutton( tag_frame, text=tag_name, variable=new_var )
            new_checkbox.pack( side=tk.LEFT, anchor=tk.W )
            
            # Update the stored data
            self.tag_checkboxes[tag_id] = {
                'var': new_var,
                'name': tag_name,
                'checkbox': new_checkbox,
                'state': 'common' if current_value else 'none',
                'frame': tag_frame
            }
            
    def save_tags( self ):
        """Save tag changes for all selected images"""
        try:
            conn = sqlite3.connect( self.database_path )
            cursor = conn.cursor()
            
            # Update ratings if scale is enabled
            if self.rating_scale['state'] != 'disabled':
                rating = self.rating_var.get()
                for img_data in self.image_data.values():
                    cursor.execute( "UPDATE images SET rating = ? WHERE id = ?", (rating, img_data['id']) )
            
            # Get selected tags from checkboxes
            selected_tag_ids = []
            for tag_id, tag_data in self.tag_checkboxes.items():
                if tag_data['var'].get():
                    selected_tag_ids.append( tag_id )
            
            # Update tags for all images
            for img_data in self.image_data.values():
                image_id = img_data['id']
                
                # Clear existing tags for this image
                cursor.execute( "DELETE FROM image_tags WHERE image_id = ?", (image_id,) )
                
                # Add selected tags
                for tag_id in selected_tag_ids:
                    cursor.execute( "INSERT INTO image_tags (image_id, tag_id) VALUES (?, ?)", (image_id, tag_id) )
            
            # Add new tags
            new_tags_text = self.new_tags_entry.get().strip()
            if new_tags_text:
                new_tags = [tag.strip() for tag in new_tags_text.split( ',' ) if tag.strip()]
                
                for tag_name in new_tags:
                    # Insert tag if it doesn't exist
                    cursor.execute( "INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,) )
                    
                    # Get tag ID
                    cursor.execute( "SELECT id FROM tags WHERE name = ?", (tag_name,) )
                    tag_id = cursor.fetchone()[0]
                    
                    # Link tag to all selected images
                    for img_data in self.image_data.values():
                        cursor.execute( "INSERT OR IGNORE INTO image_tags (image_id, tag_id) VALUES (?, ?)", (img_data['id'], tag_id) )
            
            conn.commit()
            conn.close()
            
            self.dialog.destroy()
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to save tags: {str(e)}" )



def main():
    root = tk.Tk()
    app = ImageViewer( root )
    root.mainloop()

if __name__ == "__main__":
    main()
