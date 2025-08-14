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
import threading
from pathlib import Path
import json

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
        """Setup the database dropdown menu"""
        menubar = tk.Menu( self.root )
        self.root.config( menu=menubar )
        
        database_menu = tk.Menu( menubar, tearoff=0 )
        menubar.add_cascade( label="Database", menu=database_menu )
        
        database_menu.add_command( label="Create Database", command=self.create_database )
        database_menu.add_command( label="Create Database Here", command=self.create_database_here )
        database_menu.add_command( label="Open Database", command=self.open_database )
        database_menu.add_command( label="Rescan", command=self.rescan_database )
        
    def setup_browse_tab( self ):
        """Setup the Browse tab interface"""
        # Create paned window for two columns
        paned = ttk.PanedWindow( self.browse_frame, orient=tk.HORIZONTAL )
        paned.pack( fill=tk.BOTH, expand=True )
        
        # Left column - Image preview
        left_frame = ttk.Frame( paned )
        paned.add( left_frame, weight=1 )
        
        ttk.Label( left_frame, text="Image Preview" ).pack( pady=5 )
        self.browse_preview_label = ttk.Label( left_frame, text="No image selected" )
        self.browse_preview_label.pack( fill=tk.BOTH, expand=True )
        
        # Bind double-click, mouse wheel, and resize events to preview label
        self.browse_preview_label.bind( "<Double-Button-1>", self.on_browse_preview_double_click )
        self.browse_preview_label.bind( "<MouseWheel>", self.on_browse_preview_scroll )
        self.browse_preview_label.bind( "<Configure>", self.on_browse_preview_resize )
        self.browse_preview_label.bind( "<Button-1>", lambda e: self.browse_preview_label.focus_set() )
        
        # Ensure the label can receive focus for mouse wheel events and keyboard shortcuts
        self.browse_preview_label.bind( "<Enter>", lambda e: self.browse_preview_label.focus_set() )
        # Make label focusable
        self.browse_preview_label.config( takefocus=True )
        
        # Also bind mouse wheel to the left frame to catch events
        left_frame.bind( "<MouseWheel>", self.on_browse_preview_scroll )
        
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
        
        self.browse_tree = ttk.Treeview( tree_frame )
        tree_scrollbar = ttk.Scrollbar( tree_frame, orient=tk.VERTICAL, command=self.browse_tree.yview )
        self.browse_tree.configure( yscrollcommand=tree_scrollbar.set )
        
        self.browse_tree.pack( side=tk.LEFT, fill=tk.BOTH, expand=True )
        tree_scrollbar.pack( side=tk.RIGHT, fill=tk.Y )
        
        # Bind tree events
        self.browse_tree.bind( "<<TreeviewSelect>>", self.on_browse_tree_select )
        self.browse_tree.bind( "<Double-1>", self.on_browse_tree_double_click )
        self.browse_tree.bind( "<Button-3>", self.on_browse_tree_right_click )
        
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
        self.database_preview_label = ttk.Label( left_frame, text="No image selected" )
        self.database_preview_label.pack( fill=tk.BOTH, expand=True )
        
        # Bind double-click, mouse wheel, and resize events to preview label
        self.database_preview_label.bind( "<Double-Button-1>", self.on_database_preview_double_click )
        self.database_preview_label.bind( "<MouseWheel>", self.on_database_preview_scroll )
        self.database_preview_label.bind( "<Configure>", self.on_database_preview_resize )
        self.database_preview_label.bind( "<Button-1>", lambda e: self.database_preview_label.focus_set() )
        
        # Ensure the label can receive focus for mouse wheel events and keyboard shortcuts
        self.database_preview_label.bind( "<Enter>", lambda e: self.database_preview_label.focus_set() )
        # Make label focusable
        self.database_preview_label.config( takefocus=True )
        
        # Also bind mouse wheel to the left frame to catch events
        left_frame.bind( "<MouseWheel>", self.on_database_preview_scroll )
        
        # Add keyboard rating shortcuts for database preview
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
        
        # Top section - Split into Tag Filters and Image Tags
        top_section = ttk.Frame( vertical_paned )
        vertical_paned.add( top_section, weight=1 )
        
        # Left side - Tag filter section
        tag_frame = ttk.LabelFrame( top_section, text="Tag Filters" )
        tag_frame.pack( side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 2) )
        
        # Right side - Image Tags section
        image_tags_frame = ttk.LabelFrame( top_section, text="Image Tags" )
        image_tags_frame.pack( side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(2, 0) )
        
        # Initialize tag editing variables
        self.image_tag_checkboxes = {}  # Dictionary to store tag checkbox variables
        self.selected_image_files = []  # Currently selected files for tag editing
        
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
        
        self.database_image_listbox = tk.Listbox( image_list_frame, selectmode=tk.EXTENDED )
        image_scrollbar = ttk.Scrollbar( image_list_frame, orient=tk.VERTICAL, command=self.database_image_listbox.yview )
        self.database_image_listbox.configure( yscrollcommand=image_scrollbar.set )
        
        self.database_image_listbox.pack( side=tk.LEFT, fill=tk.BOTH, expand=True )
        image_scrollbar.pack( side=tk.RIGHT, fill=tk.Y )
        
        # Bind database image list events
        self.database_image_listbox.bind( "<<ListboxSelect>>", self.on_database_image_select )
        self.database_image_listbox.bind( "<Double-1>", self.on_database_image_double_click )
        self.database_image_listbox.bind( "<MouseWheel>", self.on_database_preview_scroll )
        
        # Initialize tag filters and checkbox tracking
        self.included_or_tags = set()
        self.included_and_tags = set()
        self.excluded_tags = set()
        self.tag_checkboxes = {}  # Dictionary to store checkbox variables
        self.all_include_or_var = tk.BooleanVar()
        self.all_include_and_var = tk.BooleanVar()
        self.all_exclude_var = tk.BooleanVar()
        
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
        """Handle right click in browse tree"""
        item = self.browse_tree.identify_row( event.y )
        if item:
            filepath = self.browse_tree.item( item )['values'][0]
            
            # Select the item that was right-clicked
            self.browse_tree.selection_set( item )
            
            if self.is_image_file( filepath ):
                self.show_tag_dialog( filepath )
            elif os.path.isdir( filepath ):
                self.show_directory_context_menu( event, filepath )
                
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
                
        # Select the new item
        self.database_image_listbox.selection_clear( 0, tk.END )
        self.database_image_listbox.selection_set( new_index )
        self.database_image_listbox.see( new_index )  # Ensure it's visible
        
        # Trigger the selection event to update preview and tags
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

    def display_image_preview( self, filepath, label_widget ):
        """Display image preview in the specified label widget"""
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
            
    def enter_fullscreen_mode( self, filepath ):
        """Enter fullscreen mode for viewing images"""
        self.previous_tab = self.notebook.index( self.notebook.select() )
        
        # Determine which tab we're in and get appropriate image list
        current_tab = self.notebook.index( self.notebook.select() )
        
        if current_tab == 1 and self.current_database_path:  # Database tab
            # Use filtered images from database
            self.fullscreen_images = []
            
            # Get current filtered filenames from listbox
            filtered_filenames = []
            for i in range( self.database_image_listbox.size() ):
                filtered_filenames.append( self.database_image_listbox.get( i ) )
            
            # Convert filenames to full paths
            for filename in filtered_filenames:
                full_path = self.find_image_path( filename )
                if full_path:
                    self.fullscreen_images.append( full_path )
            
            # Find current image index
            try:
                self.fullscreen_index = self.fullscreen_images.index( filepath ) if filepath in self.fullscreen_images else 0
            except ValueError:
                self.fullscreen_images = [filepath] if filepath else []
                self.fullscreen_index = 0
                
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
        if self.fullscreen_images and self.fullscreen_index > 0:
            self.fullscreen_index -= 1
            self.display_fullscreen_image()
            
    def on_fullscreen_next( self, event ):
        """Navigate to next image in fullscreen mode"""
        if self.fullscreen_images and self.fullscreen_index < len( self.fullscreen_images ) - 1:
            self.fullscreen_index += 1
            self.display_fullscreen_image()
        
    def display_fullscreen_image( self ):
        """Display the current image in fullscreen mode"""
        if not self.fullscreen_images or self.fullscreen_index >= len( self.fullscreen_images ):
            return
            
        filepath = self.fullscreen_images[self.fullscreen_index]
        
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
        """Handle mouse wheel in fullscreen mode"""
        if not self.fullscreen_images:
            return
            
        if event.delta > 0:
            # Scroll up - previous image (don't wrap)
            if self.fullscreen_index > 0:
                self.fullscreen_index -= 1
                self.display_fullscreen_image()
        else:
            # Scroll down - next image (don't wrap)
            if self.fullscreen_index < len( self.fullscreen_images ) - 1:
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
            
        db_name = simpledialog.askstring( "Database Name", "Enter name for the new database:" )
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
            self.scan_directory_for_images_with_progress( cursor, directory )
            
            conn.commit()
            conn.close()
            
            # Open the newly created database
            self.current_database_path = db_path
            self.current_database = directory
            self.refresh_database_view()
            self.notebook.select( 1 )  # Switch to Database tab
            # Save the database state immediately
            self.save_paned_positions_only()
            # Small delay to ensure settings are written
            self.root.after(100, self.update_recent_databases_dropdown)
            
            messagebox.showinfo( "Success", f"Database created successfully at {db_path}" )
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to create database: {str(e)}" )
            
    def create_database_here( self ):
        """Create a new database in the currently browsed directory"""
        if not self.current_browse_directory:
            messagebox.showwarning( "Warning", "No directory is currently selected in the browse tab" )
            return
            
        directory = self.current_browse_directory
        
        db_name = simpledialog.askstring( "Database Name", f"Enter name for the new database in:\n{directory}" )
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
            self.scan_directory_for_images_with_progress( cursor, directory )
            
            conn.commit()
            conn.close()
            
            # Open the newly created database
            self.current_database_path = db_path
            self.current_database = directory
            self.refresh_database_view()
            self.notebook.select( 1 )  # Switch to Database tab
            # Save the database state immediately
            self.save_paned_positions_only()
            # Small delay to ensure settings are written
            self.root.after(100, self.update_recent_databases_dropdown)
            
            messagebox.showinfo( "Success", f"Database created successfully at {db_path}" )
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to create database: {str(e)}" )
            
    def show_directory_context_menu( self, event, directory_path ):
        """Show context menu for directory right-click"""
        context_menu = tk.Menu( self.root, tearoff=0 )
        context_menu.add_command( label="Create Database Here", command=lambda: self.create_database_in_directory( directory_path ) )
        
        try:
            context_menu.tk_popup( event.x_root, event.y_root )
        finally:
            context_menu.grab_release()
            
    def create_database_in_directory( self, directory_path ):
        """Create a database in the specified directory"""
        db_name = simpledialog.askstring( "Database Name", f"Enter name for the new database in:\n{directory_path}" )
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
            self.scan_directory_for_images_with_progress( cursor, directory_path )
            
            conn.commit()
            conn.close()
            
            # Open the newly created database
            self.current_database_path = db_path
            self.current_database = directory_path
            self.refresh_database_view()
            self.notebook.select( 1 )  # Switch to Database tab
            
            messagebox.showinfo( "Success", f"Database created successfully at {db_path}" )
            
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
                        
    def scan_directory_for_images_with_progress( self, cursor, directory ):
        """Scan directory recursively for image files and add to database with progress reporting"""
        # First pass: count total files for progress calculation
        total_files = 0
        all_image_files = []
        
        progress_dialog = self.create_progress_dialog( "Creating Database", "Scanning directory..." )
        self.root.update()
        
        try:
            # Count image files
            for root, dirs, files in os.walk( directory ):
                for file in files:
                    filepath = os.path.join( root, file )
                    if self.is_image_file( filepath ):
                        all_image_files.append( filepath )
                        total_files += 1
                        
            # Update progress dialog
            progress_dialog['total'] = total_files
            self.update_progress_dialog( progress_dialog, 0, total_files, "Processing images..." )
            
            # Second pass: process files with progress updates
            processed = 0
            for filepath in all_image_files:
                if progress_dialog.get( 'cancelled', False ):
                    raise Exception( "Operation cancelled by user" )
                    
                try:
                    # Get image dimensions
                    with Image.open( filepath ) as img:
                        width, height = img.size
                        
                    # Calculate relative path
                    relative_path = os.path.relpath( filepath, directory )
                    filename = os.path.basename( filepath )
                    
                    # Insert into database
                    cursor.execute( '''
                        INSERT INTO images (filename, relative_path, width, height)
                        VALUES (?, ?, ?, ?)
                    ''', (filename, relative_path, width, height) )
                    
                except Exception as e:
                    print( f"Error processing {filepath}: {e}" )
                    
                processed += 1
                
                # Update progress every 10 files or on last file
                if processed % 10 == 0 or processed == total_files:
                    self.update_progress_dialog( progress_dialog, processed, total_files, 
                                               f"Processed {processed}/{total_files} images" )
                    
        finally:
            self.close_progress_dialog( progress_dialog )
    
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
                
            self.current_database_path = db_path
            self.current_database = os.path.dirname( db_path )
            self.refresh_database_view()
            self.notebook.select( 1 )  # Switch to Database tab
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
            
            # Scan directory for current images
            current_images = set()
            for root, dirs, files in os.walk( self.current_database ):
                for file in files:
                    filepath = os.path.join( root, file )
                    if self.is_image_file( filepath ):
                        relative_path = os.path.relpath( filepath, self.current_database )
                        current_images.add( relative_path )
                        
                        # Add new images
                        if relative_path not in db_images:
                            try:
                                with Image.open( filepath ) as img:
                                    width, height = img.size
                                    
                                cursor.execute( '''
                                    INSERT INTO images (filename, relative_path, width, height)
                                    VALUES (?, ?, ?, ?)
                                ''', (file, relative_path, width, height) )
                            except Exception as e:
                                print( f"Error adding {filepath}: {e}" )
                                
            # Remove images that no longer exist
            for relative_path, image_id in db_images.items():
                if relative_path not in current_images:
                    cursor.execute( "DELETE FROM image_tags WHERE image_id = ?", (image_id,) )
                    cursor.execute( "DELETE FROM images WHERE id = ?", (image_id,) )
                    
            conn.commit()
            conn.close()
            
            self.refresh_database_view()
            
            messagebox.showinfo( "Success", "Database rescan completed successfully" )
            
        except Exception as e:
            messagebox.showerror( "Error", f"Failed to rescan database: {str(e)}" )
            
    def refresh_database_view( self ):
        """Refresh the database tab view"""
        if not self.current_database_path:
            # Clear the view when no database is open
            self.database_name_label.configure( text="No database open" )
            self.database_image_listbox.delete( 0, tk.END )
            self.database_preview_label.configure( image="", text="No database open" )
            self.database_preview_label.image = None
            self.clear_image_tag_interface()
            return
            
        try:
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
                
            # Load filtered images
            self.refresh_filtered_images()
            
            conn.close()
            
        except Exception as e:
            print( f"Error refreshing database view: {e}" )
            
    def refresh_filtered_images( self, preserve_selection=None ):
        """Refresh the filtered image list based on current tag filters"""
        if not self.current_database_path:
            return
        
        # Store current selection if not provided
        if preserve_selection is None and hasattr( self, 'database_image_listbox' ):
            current_selection = list( self.database_image_listbox.curselection() )
            preserve_selection = []
            for index in current_selection:
                preserve_selection.append( self.database_image_listbox.get( index ) )
            
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
            
            self.database_image_listbox.delete( 0, tk.END )
            for relative_path, filename in images:
                self.database_image_listbox.insert( tk.END, filename )
                
            # Handle selection restoration
            filtered_filenames = [filename for relative_path, filename in images]
            
            # If we have a preserved selection, try to restore it
            if preserve_selection and filtered_filenames:
                # Temporarily unbind the select event to prevent interference
                self.database_image_listbox.unbind( "<<ListboxSelect>>" )
                
                self.database_image_listbox.selection_clear( 0, tk.END )
                restored_any = False
                
                for filename in preserve_selection:
                    if filename in filtered_filenames:
                        try:
                            index = filtered_filenames.index( filename )
                            self.database_image_listbox.selection_set( index )
                            restored_any = True
                        except ValueError:
                            pass
                
                if restored_any:
                    # Ensure the first selected item is visible
                    first_selected = self.database_image_listbox.curselection()
                    if first_selected:
                        self.database_image_listbox.see( first_selected[0] )
                else:
                    # None of the preserved selection is in filtered list - select first
                    if filtered_filenames:
                        self.database_image_listbox.selection_set( 0 )
                        self.database_image_listbox.see( 0 )
                
                # Re-bind the select event
                self.database_image_listbox.bind( "<<ListboxSelect>>", self.on_database_image_select )
                
            elif filtered_filenames and not preserve_selection:
                # No preserved selection - use smart preview logic
                current_filename = None
                if self.current_database_image:
                    current_filename = os.path.basename( self.current_database_image )
                    
                if current_filename and current_filename in filtered_filenames:
                    # Current image is still in filtered list - select it
                    try:
                        current_index = filtered_filenames.index( current_filename )
                        self.database_image_listbox.selection_set( current_index )
                        self.database_image_listbox.see( current_index )
                    except ValueError:
                        pass
                else:
                    # Current image is not in filtered list - select first image
                    self.database_image_listbox.selection_set( 0 )
                    self.database_image_listbox.see( 0 )
                    
                    # Update preview to show first image
                    first_filename = filtered_filenames[0]
                    first_filepath = self.find_image_path( first_filename )
                    if first_filepath:
                        self.current_database_image = first_filepath
                        self.display_image_preview( first_filepath, self.database_preview_label )
                        self.selected_image_files = [first_filepath]
                        self.load_image_tags_for_editing()
            
            if not filtered_filenames:
                # No images in filtered list - clear preview
                self.current_database_image = None
                self.database_preview_label.configure( image="", text="No images match filters" )
                self.database_preview_label.image = None
                self.selected_image_files = []
                self.clear_image_tag_interface()
                
            conn.close()
            
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
            else:
                # Multiple selection - load tags for bulk editing
                filenames = [self.database_image_listbox.get( i ) for i in selection]
                filepaths = [self.find_image_path( f ) for f in filenames]
                self.selected_image_files = [f for f in filepaths if f]  # Remove None values
                self.current_database_image = None
                self.database_preview_label.configure( image="", text=f"{len(selection)} images selected" )
                self.database_preview_label.image = None
                self.load_image_tags_for_editing()
        else:
            # No selection - clear tag editing interface
            self.selected_image_files = []
            self.clear_image_tag_interface()
            
    def load_image_tags_for_editing( self ):
        """Load tags for the selected images into the editing interface"""
        if not self.selected_image_files or not self.current_database_path:
            self.clear_image_tag_interface()
            return
            
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            # Get image IDs and their data
            image_data = {}
            ratings = []
            
            for filepath in self.selected_image_files:
                relative_path = os.path.relpath( filepath, os.path.dirname( self.current_database_path ) )
                cursor.execute( "SELECT id, rating FROM images WHERE relative_path = ?", (relative_path,) )
                result = cursor.fetchone()
                
                if result:
                    image_data[filepath] = {'id': result[0], 'rating': result[1] or 0}
                    ratings.append( result[1] or 0 )
            
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
            total_images = len( image_data )
            
            for tag_id, tag_name in all_tags:
                # Count how many of the selected images have this tag
                placeholders = ','.join( ['?' for _ in image_data.values()] )
                query = f"""
                    SELECT COUNT(*) FROM image_tags 
                    WHERE tag_id = ? AND image_id IN ({placeholders})
                """
                params = [tag_id] + [img['id'] for img in image_data.values()]
                cursor.execute( query, params )
                count = cursor.fetchone()[0]
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
            
            conn.close()
            
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
        
    def on_image_partial_checkbox_clicked( self, tag_id ):
        """Handle clicking on a greyed out (partial) checkbox in the image tags interface"""
        if tag_id in self.image_tag_checkboxes and self.image_tag_checkboxes[tag_id]['state'] == 'partial':
            tag_data = self.image_tag_checkboxes[tag_id]
            tag_name = tag_data['name']
            tag_frame = tag_data['frame']
            current_value = tag_data['var'].get()
            
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
            
    def on_image_tag_changed( self, tag_id ):
        """Handle immediate tag checkbox changes"""
        if tag_id in self.image_tag_checkboxes:
            tag_data = self.image_tag_checkboxes[tag_id]
            is_checked = tag_data['var'].get()
            self.apply_single_tag_change( tag_id, is_checked )
            
    def apply_single_tag_change( self, tag_id, is_checked ):
        """Apply a single tag change immediately to selected images"""
        if not self.selected_image_files or not self.current_database_path:
            return
            
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            # Get image IDs for the selected files
            image_ids = []
            for filepath in self.selected_image_files:
                relative_path = os.path.relpath( filepath, os.path.dirname( self.current_database_path ) )
                cursor.execute( "SELECT id FROM images WHERE relative_path = ?", (relative_path,) )
                result = cursor.fetchone()
                if result:
                    image_ids.append( result[0] )
            
            if not image_ids:
                conn.close()
                return
                
            # Apply the tag change
            for image_id in image_ids:
                if is_checked:
                    # Add tag to image
                    cursor.execute( "INSERT OR IGNORE INTO image_tags (image_id, tag_id) VALUES (?, ?)", 
                                  (image_id, tag_id) )
                else:
                    # Remove tag from image
                    cursor.execute( "DELETE FROM image_tags WHERE image_id = ? AND tag_id = ?", 
                                  (image_id, tag_id) )
            
            conn.commit()
            conn.close()
            
            # Refresh views to reflect changes
            self.refresh_database_view()
            self.refresh_filtered_images()
            
        except Exception as e:
            print( f"Error applying tag change: {e}" )
            
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
            
        try:
            conn = sqlite3.connect( self.current_database_path )
            cursor = conn.cursor()
            
            rating = self.image_rating_var.get()
            
            # Get image IDs and update ratings
            for filepath in self.selected_image_files:
                relative_path = os.path.relpath( filepath, os.path.dirname( self.current_database_path ) )
                cursor.execute( "UPDATE images SET rating = ? WHERE relative_path = ?", (rating, relative_path) )
            
            conn.commit()
            
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
        finally:
            conn.close()
    
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
                
                for tag_name in new_tags:
                    # Insert tag if it doesn't exist
                    cursor.execute( "INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,) )
                    
                    # Get tag ID
                    cursor.execute( "SELECT id FROM tags WHERE name = ?", (tag_name,) )
                    tag_id = cursor.fetchone()[0]
                    
                    # Add tag to all selected images
                    for img_data in image_data.values():
                        cursor.execute( "INSERT OR IGNORE INTO image_tags (image_id, tag_id) VALUES (?, ?)", 
                                      (img_data['id'], tag_id) )
                changes_made = True
            
            if changes_made:
                conn.commit()
                
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
    
    def complete_startup( self ):
        """Mark startup as complete to enable state saving"""
        self.startup_complete = True
        # Update recent databases dropdown after startup
        self.update_recent_databases_dropdown()
        # Restore rating filters
        self.restore_rating_filters()
        # Prompt to restore database after a short delay
        self.root.after( 500, self.prompt_restore_database )
    
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
            
            # Store current selection to preserve it after refresh (for database tab)
            current_filenames = []
            if hasattr( self, 'database_image_listbox' ):
                current_selection_indices = list( self.database_image_listbox.curselection() )
                for index in current_selection_indices:
                    current_filenames.append( self.database_image_listbox.get( index ) )
            
            # Refresh filtered images with preserved selection (for database tab)
            if hasattr( self, 'database_image_listbox' ) and current_filenames:
                self.refresh_filtered_images( preserve_selection=current_filenames )
            else:
                self.refresh_filtered_images()
            
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
